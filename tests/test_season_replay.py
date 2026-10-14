"""Rejeu de la voie `saison` (tools/season_replay.py), hors ligne et sur des matchs FICTIFS : vraisemblance binomiale négative, règle du 8e match, jamais un match de la fenêtre
réservée, écarts appariés, résultat écrit UNE seule fois dans le ledger. Jamais le vrai cache. Lancer : python -m tests.test_season_replay"""
import json
import math
import os
import random
import shutil
import sys
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-sreplay-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-sreplay-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import season_replay as R  # noqa: E402
from kelly import strength  # noqa: E402


def fake_matches(n_teams=12, seed=3, start=date(2024, 8, 10), rounds=3):
    """Saison fictive (double aller-retour) puis quelques matchs APRÈS la date réservée : ils ne doivent jamais être notés."""
    rnd = random.Random(seed)
    teams = [f"T{i}" for i in range(n_teams)]
    out, day = [], start

    def pois(mean):
        x, p, L = 0, 1.0, math.exp(-mean)
        while True:
            p *= rnd.random()
            if p <= L:
                return x
            x += 1
    pairs = [(h, a) for r in range(rounds) for h in teams for a in teams if h != a]
    rnd.shuffle(pairs)
    for i, (h, a) in enumerate(pairs):
        if i % 5 == 0:
            day += timedelta(days=1)
        d = day.isoformat()
        hg, ag = pois(1.5), pois(1.2)
        out.append({"date": d, "div": "E0", "season": "2425", "home": h, "away": a, "hg": hg, "ag": ag, "ftr": "H" if hg > ag else "A" if hg < ag else "D", "referee": None,
                    "hs": pois(13), "as": pois(10), "hst": pois(5), "ast": pois(4), "hf": pois(11), "af": pois(11), "hc": pois(5.5), "ac": pois(4.5),
                    "hy": pois(1.8), "ay": pois(2.1), "hr": 0, "ar": 0, "odds": {}})
    return out


def test_nb_logpmf():
    for mu, al in ((3.2, 0.0), (3.2, 0.2), (0.7, 0.05), (9.0, 0.4)):
        pmf = strength.nb_pmf(mu, al, cap=40)
        for k in (0, 1, 4, 9):
            assert abs(math.exp(R.nb_logpmf(k, mu, al)) - pmf[k]) < 1e-9, (mu, al, k)
    assert abs(R.nll_side((2, 3), 2.0, 3.0, 0.0) + R.nb_logpmf(2, 2.0, 0.0) + R.nb_logpmf(3, 3.0, 0.0)) < 1e-12
    print("ok  log-vraisemblance binomiale négative = strength.nb_pmf (Poisson à alpha 0), somme domicile + extérieur")


def test_replay_rules():
    ms = fake_matches()
    late = [dict(ms[0], date="2025-07-02", home="T0", away="T1"), dict(ms[1], date="2026-01-05", home="T2", away="T3")]       # fenêtre réservée : jamais notés
    recs, cnt = R.replay(ms + late, "E0", step_days=30)
    assert recs and cnt["scored"] == len(recs) and cnt["too_early"] > 0, cnt
    assert all(r["date"] < R.RESERVED_FROM for r in recs), "aucun match de la fenêtre réservée"
    assert not any(r["date"] in ("2025-07-02", "2026-01-05") for r in recs)
    # chaque match noté : les deux équipes avaient déjà joué >= 7 matchs de la saison
    played = {}
    first_scored = {}
    for m in sorted(ms, key=lambda m: m["date"]):
        rid = f"E0|{m['date']}|{m['home']}|{m['away']}"
        n_h, n_a = played.get(m["home"], 0), played.get(m["away"], 0)
        first_scored[rid] = min(n_h, n_a)
        played[m["home"]], played[m["away"]] = n_h + 1, n_a + 1
    assert all(first_scored[r["id"]] >= R.MIN_PRIOR for r in recs), "règle du 8e match"
    assert all(set(r["nll"]) <= set(R.FAMILIES) and all(set(v) == set(R.MODELS) for v in r["nll"].values()) for r in recs), "tous les modèles sur chaque famille notée (appariés)"
    tab = R.table(recs)
    fam = next(f for f in R.FAMILIES if tab[f]["saison_k6 - strength_hl110"]["n"])
    d = tab[fam]["saison_k6 - strength_hl110"]
    xs = [r["nll"][fam]["saison_k6"] - r["nll"][fam]["strength_hl110"] for r in recs if fam in r["nll"]]
    assert d["n"] == len(xs) and abs(d["mean"] - sum(xs) / len(xs)) < 1e-9
    assert tab[fam]["strength_hl45 - strength_hl110"]["n"] == d["n"]
    print(f"ok  rejeu : {cnt['scored']} matchs notés, jamais la fenêtre réservée, 8e match et plus, tous les modèles sur les mêmes matchs ; écarts appariés par famille")
    return recs, cnt, tab


def test_ledger_matches_tool():
    led = json.load(open(R.LEDGER))["prereg"]
    assert list(R.KS) == [m for m in led["models"] if m.startswith("saison")] and set(R.MODELS) == set(led["models"])
    assert [f"{a} - {b}" for a, b in R.CONTRASTS] == led["contrasts"]
    assert len(R.FAMILIES) * len(R.CONTRASTS) == led["comparisons"] and led["data"]["window"].startswith("matchs datés AVANT 2025-07-01") and R.RESERVED_FROM == "2025-07-01"
    assert R.MIN_PRIOR == 7 and "7 matchs" in led["data"]["scored_from"]
    import hashlib
    assert hashlib.sha256(json.dumps(led, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == json.load(open(R.LEDGER))["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    print("ok  l'outil suit le ledger pré-déclaré (modèles, contrastes, 25 comparaisons, fenêtre, 8e match) et la pré-déclaration est intacte")


def main():
    test_nb_logpmf()
    recs, cnt, tab = test_replay_rules()
    path = os.path.join(tempfile.mkdtemp(prefix="kelly-sreplay-ledger-"), "season_ledger.json")
    shutil.copy(R.LEDGER, path)
    doc = R.write_final(tab, cnt, ["E0"], recs, ledger_path=path)
    assert doc["final"]["counts"]["scored"] == cnt["scored"] and doc["prereg"] == json.load(open(R.LEDGER))["prereg"]
    try:
        R.write_final(tab, cnt, ["E0"], recs, ledger_path=path)
        raise AssertionError("une seconde écriture doit être refusée")
    except SystemExit as e:
        assert "déjà écrit" in str(e)
    print("ok  ledger : le résultat s'écrit UNE seule fois, la pré-déclaration reste intacte")
    assert json.load(open(R.LEDGER))["final"] is None, "le vrai ledger n'est pas touché par les tests"
    test_ledger_matches_tool()
    # garde-fous de l'outil
    for bad in (["--final", "--divs", "E0"], ["--final", "--smoke"]):
        try:
            R.main(bad)
            raise AssertionError(bad)
        except SystemExit as e:
            assert "--final se lance sur toutes les divisions" in str(e)
    ms = fake_matches()
    real = R.score_match
    calls = {"n": 0}

    def flaky(models, m, d):
        calls["n"] += 1
        if calls["n"] % 7 == 0:
            raise ValueError("donnée dégénérée")
        return real(models, m, d)
    R.score_match = flaky
    try:
        recs2, cnt2 = R.replay(ms, "E0", step_days=30)
    finally:
        R.score_match = real
    assert cnt2["errors"] > 0 and cnt2["first_error"].startswith("E0 ") and cnt2["scored"] > 0, cnt2
    print("ok  garde-fous : --final refuse --divs / --smoke, un match en erreur est écarté (compté) sans perdre le rejeu")
    print("tests rejeu saison : OK")


if __name__ == "__main__":
    main()
