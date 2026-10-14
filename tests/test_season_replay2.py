"""Rejeu v2 de la voie `saison` (tools/season_replay2.py), hors ligne et sur des matchs FICTIFS avec cotes de clôture : mêmes matchs pour tous les modèles, agrégat des cinq familles, règle de
décision et départage du ledger v2, résultat écrit UNE fois (v2.final) sans toucher au rejeu v1, pré-déclaration v2 intacte. Jamais le vrai cache. Lancer : python -m tests.test_season_replay2"""
import json
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-sreplay2-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-sreplay2-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import season_replay2 as R  # noqa: E402
from tests.test_season_replay import fake_matches  # noqa: E402
from kelly import strength  # noqa: E402


def with_odds(ms):
    """Cotes de clôture fictives (1N2 et plus/moins 2,5), marge de 5 % ; les buts attendus dépendent des équipes (sinon l'a priori de marché vaudrait la moyenne de ligue)."""
    out = []
    for m in ms:
        fh, fa = 0.7 + 0.1 * (int(m["home"][1:]) % 7), 0.7 + 0.1 * (int(m["away"][1:]) % 7)
        ph, pa, po = strength._h_a_o(1.5 * fh / fa ** 0.5, 1.2 * fa / fh ** 0.5)
        pd = 1 - ph - pa
        o = lambda p: round(1 / (p * 1.05), 3)     # noqa: E731
        out.append(dict(m, odds={"1x2": {"close": {"ps": [o(ph), o(pd), o(pa)]}}, "ou25": {"close": {"ps": [o(po), o(1 - po)]}}}))
    return out


def test_replay_and_table():
    recs, cnt = R.replay(with_odds(fake_matches()), "E0", step_days=30)
    assert recs and cnt["scored"] == len(recs) and cnt["errors"] == 0, cnt
    assert all(set(v) == set(R.MODELS) for r in recs for v in r["nll"].values()), "tous les modèles sur chaque famille notée"
    R.with_aggregate(recs)
    full = [r for r in recs if "agg" in r["nll"]]
    assert full and all(all(f in r["nll"] for f in R.FAMILIES) for r in full)
    r0 = full[0]
    assert abs(r0["nll"]["agg"]["b"] - sum(r0["nll"][f]["b"] for f in R.FAMILIES)) < 1e-9, "agrégat = somme des cinq familles"
    tab = R.table(recs)
    d = tab["agg"]["c - a"]
    xs = [r["nll"]["agg"]["c"] - r["nll"]["agg"]["a"] for r in full]
    assert d["n"] == len(xs) and abs(d["mean"] - sum(xs) / len(xs)) < 1e-9
    assert set(tab) == set(R.KEYS) and set(tab["corners"]) == {f"{x} - {y}" for x, y in R.DECISION + R.REFERENCE}
    # a, b, c, d diffèrent bien entre eux (les options sont branchées) : sinon le rejeu ne mesurerait rien
    assert any(abs(r["nll"]["corners"]["a"] - r["nll"]["corners"]["b"]) > 1e-9 for r in recs if "corners" in r["nll"])
    assert any(abs(r["nll"]["corners"]["a"] - r["nll"]["corners"]["c"]) > 1e-9 for r in recs if "corners" in r["nll"])
    print(f"ok  rejeu v2 : {cnt['scored']} matchs notés, mêmes matchs pour a, b, c, d et la référence, agrégat = somme des cinq familles, contrastes de décision et de référence")
    return recs, cnt, tab


def test_replay_leak():
    """Niveau rejeu : modifier les cotes de clôture des matchs à partir d'une date ne change aucune note des matchs antérieurs à cette date, ni aucune note de (a) et (c) (sans a priori de marché)."""
    ms = with_odds(fake_matches())
    cut = sorted(m["date"] for m in ms)[len(ms) * 2 // 3]
    pert = [dict(m, odds={"1x2": {"close": {"ps": [1.3, 6.0, 12.0]}}, "ou25": {"close": {"ps": [1.4, 3.0]}}}) if m["date"] >= cut else m for m in ms]
    r0, _ = R.replay(ms, "E0", step_days=3)
    strength._xg_memo.clear()                                   # la mémoire des attentes implicites est clée par (division, date, équipes), pas par les cotes
    r1, _ = R.replay(pert, "E0", step_days=3)
    assert [r["id"] for r in r0] == [r["id"] for r in r1], "mêmes matchs notés"          # (le même couple peut se jouer deux fois le même jour : on compare par position, pas par identifiant)
    pairs = list(zip(r0, r1))
    assert all(b["nll"] == a["nll"] for a, b in pairs if a["date"] < cut) and any(a["date"] < cut for a, _ in pairs), "notes antérieures à la date identiques"
    assert all(b["nll"][f][m] == a["nll"][f][m] for a, b in pairs for f in a["nll"] for m in ("a", "c", "strength_hl110")), "(a), (c) et la référence ne lisent jamais les cotes"
    assert any(b["nll"] != a["nll"] for a, b in pairs if a["date"] >= cut), "après la date, les cotes antérieures modifiées changent (b) et (d) : l'a priori est branché"
    print("ok  fuite au niveau du rejeu : cotes modifiées à partir d'une date, notes antérieures identiques ; (a), (c) et la référence ne lisent jamais les cotes")


def fake_table(means):
    """Tableau fictif : `means[(clé, « X - a »)]` = (moyenne, demi-marge, n) ; le reste neutre."""
    tab = {k: {f"{x} - {y}": {"n": 500, "mean": 0.0, "half": 0.01} for x, y in R.DECISION + R.REFERENCE} for k in R.KEYS}
    for (key, c), (m, h, n) in means.items():
        tab[key][c] = {"n": n, "mean": m, "half": h}
    return tab


def test_decision_rule():
    ok = {("agg", "b - a"): (-0.05, 0.02, 500), ("agg", "c - a"): (-0.03, 0.02, 500), ("agg", "d - a"): (-0.08, 0.02, 500)}
    passed, chosen, det = R.decide(fake_table(ok))
    assert passed == ["b", "c", "d"] and chosen == "d", "plusieurs passent : la plus petite moyenne d'agrégat"
    tie = {("agg", "b - a"): (-0.05, 0.02, 500), ("agg", "c - a"): (-0.05, 0.02, 500)}
    assert R.decide(fake_table(tie))[1] == "b", "égalité : la plus simple"
    worse = ok | {("corners", "d - a"): (0.03, 0.01, 500)}
    p, ch, det = R.decide(fake_table(worse))
    assert "d" not in p and det["d"]["families_worse"] == ["corners"] and ch == "b", "une famille strictement pire exclut la configuration"
    indecis = {("agg", "b - a"): (-0.01, 0.02, 500)}
    assert R.decide(fake_table(indecis)) == ([], None, R.decide(fake_table(indecis))[2]), "agrégat indécis : (a) reste"
    small = {("agg", "b - a"): (-0.05, 0.02, 500), ("corners", "b - a"): (0.5, 0.01, 12)}
    assert R.decide(fake_table(small))[1] == "b", "moins de 30 matchs dans une famille : elle ne compte pas pour la condition « jamais pire »"
    print("ok  règle du ledger v2 : agrégat strictement meilleur, aucune famille strictement pire, plus petite moyenne puis plus simple")


def test_ledger_matches_tool():
    led = json.load(open(R.LEDGER))["v2"]
    pr = led["prereg"]
    assert R.sha(pr) == led["prereg_sha"], "pré-déclaration v2 inchangée depuis son commit"
    assert set(R.MODELS) == set(pr["models"]) and list(R.FAMILIES) == pr["families"] and R.K == 12.0 and R.RESERVED_FROM == "2025-07-01" and R.MIN_PRIOR == 7
    assert [f"{x} - {y}" for x, y in R.DECISION] == pr["decision_contrasts"] and [f"{x} - {y}" for x, y in R.REFERENCE] == pr["reference_contrasts"]
    assert len(R.DECISION) * len(R.KEYS) == 18 and len(R.REFERENCE) * len(R.KEYS) == 24 and pr["total_comparisons"] == 42
    from kelly import config as C
    assert pr["market_prior_b"]["elasticity_e"] == C.SEASON_MKT_ELASTICITY and C.SEASON_MKT_SHRINK == 6.0
    v1 = json.load(open(R.LEDGER))
    print("ok  l'outil suit le ledger v2 (modèles, contrastes, 18 + 24 comparaisons, k = 12, élasticités 0,5, rétrécissement 6) et la pré-déclaration est intacte")
    return v1


def test_final_once(recs, cnt, tab):
    real_before = open(R.LEDGER, "rb").read()
    path = os.path.join(tempfile.mkdtemp(prefix="kelly-sreplay2-ledger-"), "season_ledger.json")
    fresh = json.load(open(R.LEDGER))
    fresh["v2"].pop("final", None)
    json.dump(fresh, open(path, "w"), ensure_ascii=False)
    doc = R.write_final(tab, cnt, ["E0"], recs, ledger_path=path)
    f = doc["v2"]["final"]
    assert f["counts"]["scored"] == cnt["scored"] and f["rule"]["applied"] is False and f["same_set_as_v1"] is False
    assert doc["prereg"] == fresh["prereg"] and doc["final"] == fresh["final"] and doc["prereg_sha"] == fresh["prereg_sha"], "le rejeu v1 n'est pas touché"
    try:
        R.write_final(tab, cnt, ["E0"], recs, ledger_path=path)
        raise AssertionError("une seconde écriture doit être refusée")
    except SystemExit as e:
        assert "déjà écrit" in str(e)
    alt = json.load(open(path))
    alt["v2"].pop("final")
    alt["v2"]["prereg"]["rule"] = "autre règle"
    alt_path = path + ".alt"
    json.dump(alt, open(alt_path, "w"), ensure_ascii=False)
    try:
        R.write_final(tab, cnt, ["E0"], recs, ledger_path=alt_path)
        raise AssertionError("une pré-déclaration altérée doit être refusée")
    except SystemExit as e:
        assert "a changé" in str(e)
    assert open(R.LEDGER, "rb").read() == real_before, "le vrai ledger n'est pas touché par les tests"
    print("ok  ledger : v2.final s'écrit UNE seule fois, pré-déclaration altérée refusée, rejeu v1 intact")


def test_guards():
    for bad in (["--final", "--divs", "E0"], ["--final", "--smoke"]):
        try:
            R.main(bad)
            raise AssertionError(bad)
        except SystemExit as e:
            assert "--final se lance sur toutes les divisions" in str(e)
    print("ok  garde-fous : --final refuse --divs / --smoke")


def main():
    recs, cnt, tab = test_replay_and_table()
    R.with_aggregate(recs)
    test_decision_rule()
    test_replay_leak()
    test_ledger_matches_tool()
    test_final_once(recs, cnt, R.table(recs))
    test_guards()
    print("tests rejeu saison v2 : OK")


if __name__ == "__main__":
    main()
