"""Rejeu historique de l'audit (tools/audit_replay.py), hors ligne, sur des CSV football-data FICTIFS (tests/synth.py) : aucune fuite d'information (falsifier
les cotes de clôture de matchs plus tardifs ne change aucune prédiction des matchs d'avant ; le marché mis en commun est l'OUVERTURE, la clôture ne sert que
de cible), l'échauffement n'est jamais noté, la mise en commun vaut le marché à poids 0 et le modèle à poids 1, la saison réservée ne se lit qu'une fois
(--final exige --ledger et refuse de relire), aucune écriture hors du fichier de trace. Lancer : python -m tests.test_audit_replay"""
import copy
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-replay-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-replay-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_replay as R  # noqa: E402

from kelly.sources import footballdata as fd  # noqa: E402
from tests import synth  # noqa: E402


def matches(seasons=("2324", "2425", "2526")):
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s), "E0", s)
        assert not probs, probs
        out += rows
    return out


MS = matches()


def test_math():
    p, q = [0.5, 0.3, 0.2], [0.4, 0.4, 0.2]
    for a, b in zip(R.pool3(p, q, 0.0), q):
        assert abs(a - b) < 1e-9
    for a, b in zip(R.pool3(p, q, 1.0), p):
        assert abs(a - b) < 1e-9
    assert abs(sum(R.pool3(p, q, 0.25)) - 1) < 1e-12
    assert R.kl3(q, q) == 0 and R.kl3(q, p) > 0 and R.kl2(0.3, 0.3) == 0
    assert R.ll2(0.8, 1) < R.ll2(0.2, 1), "plus la proba du bon côté est haute, plus la log-loss baisse"
    print("ok  mise en commun (poids 0 = marché, 1 = modèle), KL, log-loss")


def test_windows_and_fields():
    recs, cnt = R.replay(MS, "E0", R.WARMUP_END, R.RESERVED_FROM)
    assert recs and cnt["predicted"] == len(recs) and cnt["no_model"] == 0, cnt
    assert all(R.WARMUP_END <= r["date"] < R.RESERVED_FROM for r in recs), "ni échauffement ni saison réservée dans la fenêtre de développement"
    r = recs[0]
    assert set(r["pred"]) == {"modèle seul", "ouverture Pinnacle", "ouverture + 12 % modèle", "ouverture + 25 % modèle"}
    assert abs(sum(r["q_o3"]) - 1) < 1e-9 and abs(sum(r["q_c3"]) - 1) < 1e-9
    fin, _ = R.replay(MS, "E0", R.RESERVED_FROM, R.RESERVED_TO)
    assert fin and all(x["date"] >= R.RESERVED_FROM for x in fin) and not {x["id"] for x in fin} & {x["id"] for x in recs}
    print(f"ok  fenêtres : {len(recs)} matchs de développement, {len(fin)} de saison réservée, échauffement jamais noté")


def set_close(m, price):
    for mk in ("1x2", "ou25"):
        for book, vals in ((m.get("odds") or {}).get(mk, {}).get("close") or {}).items():
            vals[:] = [price] * len(vals)


def test_no_leak():
    base, _ = R.replay(MS, "E0", R.WARMUP_END, R.RESERVED_FROM)
    cut = sorted(r["date"] for r in base)[len(base) // 2]                         # milieu de la fenêtre
    bad = copy.deepcopy(MS)
    n_bad = 0
    for m in bad:
        if m["date"] >= cut:                                                        # falsifie TOUTES les clôtures d'à partir de `cut`
            set_close(m, 1.5)
            n_bad += 1
    assert n_bad > 50
    alt, _ = R.replay(bad, "E0", R.WARMUP_END, R.RESERVED_FROM)
    by_id = {r["id"]: r for r in alt}
    early = [r for r in base if r["date"] < cut]
    assert early, "des matchs avant la coupure"
    for r in early:
        a = by_id[r["id"]]
        assert a["p3"] == r["p3"] and a["po"] == r["po"], "le modèle d'un match ne dépend d'aucune clôture postérieure"
        assert a["pred"] == r["pred"] and a["q_o3"] == r["q_o3"], "le marché mis en commun est l'ouverture, pas la clôture"
    late = [r for r in base if r["date"] >= cut]
    assert any(by_id[r["id"]]["q_c3"] != r["q_c3"] for r in late), "la cible (clôture) a bien changé là où on l'a falsifiée"
    assert any(by_id[r["id"]]["p3"] != r["p3"] for r in late), "la falsification ATTEINT le modèle (ancrage sur les matchs joués) : sinon le test ne prouverait rien"
    # sa PROPRE clôture non plus : on falsifie la clôture d'UN seul match, sa prédiction ne bouge pas (celles d'après peuvent, via l'ancrage des matchs joués)
    target = base[len(base) // 2]
    one = copy.deepcopy(MS)
    hit = [m for m in one if f"E0|{m['date']}|{m['home']}|{m['away']}" == target["id"]]
    assert len(hit) == 1
    set_close(hit[0], 1.2)
    one_alt = {r["id"]: r for r in R.replay(one, "E0", R.WARMUP_END, R.RESERVED_FROM)[0]}
    a = one_alt[target["id"]]
    assert a["q_c3"] != target["q_c3"] and a["p3"] == target["p3"] and a["po"] == target["po"] and a["pred"] == target["pred"], "un match ne se prédit pas avec sa clôture"
    assert all(one_alt[r["id"]]["p3"] == r["p3"] for r in base if r["date"] <= target["date"]), "ni les matchs d'avant"
    assert any(one_alt[r["id"]]["p3"] != r["p3"] for r in base if r["date"] > target["date"]), "et la falsification d'un match joué atteint bien les suivants"
    # le modèle de la fin de fenêtre PEUT changer : il s'ajuste sur les clôtures des matchs DÉJÀ JOUÉS (ancrage marché), c'est voulu
    print("ok  aucune fuite : clôtures falsifiées à partir de la mi-fenêtre, aucune prédiction d'avant ne bouge ; la clôture ne sert que de cible")


def test_report_and_ledger():
    loader = lambda divs: {"E0": MS}                                                # noqa: E731
    out = tempfile.mkdtemp(prefix="kelly-replay-ledger-")
    ledger = os.path.join(out, "reserve.json")
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert R.main(["--divs", "E0"], loader=loader) == 0
    txt = buf.getvalue()
    for s in ("REJEU FOOTBALL-DATA", "1N2 (log-loss", "Plus/moins 2,5", "Verdict 1N2", "Verdict plus/moins 2,5", "Paris simulés", "Limites", "RIEN des marchés mous"):
        assert s in txt, s
    assert "SAISON RÉSERVÉE" not in txt and os.listdir(out) == [], "le développement ne lit pas la saison réservée et n'écrit aucun fichier"
    assert R.main(["--final", "--divs", "E0"], loader=loader) == 2, "--final sans --ledger : refus"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert R.main(["--final", "--ledger", ledger, "--divs", "E0"], loader=loader) == 0
    assert "LECTURE UNIQUE" in buf.getvalue() and os.path.exists(ledger)
    assert R.main(["--final", "--ledger", ledger, "--divs", "E0"], loader=loader) == 2, "une saison réservée ne se relit pas"
    print("ok  rapport complet ; --final exige --ledger, écrit sa trace une fois et refuse de relire")


def main():
    test_math()
    test_windows_and_fields()
    test_no_leak()
    test_report_and_ledger()
    print("tests rejeu historique : OK")


if __name__ == "__main__":
    main()
