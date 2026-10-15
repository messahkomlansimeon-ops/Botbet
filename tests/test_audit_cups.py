"""tools/audit_cups.py sur données FICTIVES : inversion de la proba en moyenne, lecture du nombre réel, biais retrouvé, lots, sélection, lecture seule (empreinte intacte). Jamais le vrai
cache ni le vrai .env. Lancer : python -m tests.test_audit_cups"""
import json
import os
import random
import sqlite3
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-acups-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-acups-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import audit_cups as A  # noqa: E402
from kelly import strength  # noqa: E402


def draw(rng, mean, alpha):
    pmf = strength.nb_pmf(mean, alpha)
    u, c = rng.random(), 0.0
    for k, p in enumerate(pmf):
        c += p
        if u <= c:
            return k
    return len(pmf) - 1


def fake_rows(bias, n=60, seed=3, alpha=0.15):
    """n matchs de LDC : vraie moyenne de corners 10, le modèle croit 10 x bias ; trois lignes par match (Over et Under de plusieurs points) ; le marché a raison."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        val = draw(rng, 10.0, alpha)
        for pt in (8.5, 9.5, 10.5):
            for name in ("Over", "Under"):
                pov = strength.p_over(strength.nb_pmf(10.0 * bias, alpha), pt)
                pm = pov if name == "Over" else 1 - pov
                px = strength.p_over(strength.nb_pmf(10.0, alpha), pt)
                px = px if name == "Over" else 1 - px
                win = (val > pt) == (name == "Over")
                rows.append({"key": f"m{i}|alternate_totals_corners|{pt}|{name}", "id": f"m{i}", "sport_key": "soccer_uefa_champs_league", "market": "alternate_totals_corners",
                             "name": name, "point": pt, "p0": pm, "pm": 0.5 + 0.5 * (pm - 0.5), "px": px, "final": f"{val} corners", "result": "win" if win else "loss", "cup": True})
    return rows


def test_inversion_and_actual():
    for a in (0.0, 0.15):
        for mu in (3.0, 10.0):
            p = strength.p_over(strength.nb_pmf(mu, a), mu - 0.5 if mu > 5 else 2.5)
            assert abs(A.reconstruct_mean(p, mu - 0.5 if mu > 5 else 2.5, a) - mu) < 0.02, (a, mu)
    assert A.reconstruct_mean(0.995, 9.5, 0.0) is None and A.reconstruct_mean(None, 9.5, 0.0) is None
    assert A.actual_value({"final": "9 corners", "result": "win", "market": "alternate_totals_corners"}) == 9.0
    assert A.actual_value({"final": "2-3", "result": "loss", "market": "totals"}) == 5 and A.actual_value({"final": "2-3", "result": "loss", "market": "team_totals_away"}) == 3
    assert A.actual_value({"final": "2 tirs", "result": None, "market": "team_shots_home"}) is None, "ligne non réglée : pas de nombre réel"
    print("ok  inversion de la proba en moyenne (Poisson et binomiale négative) ; nombre réel lu dans `final`")


def test_bias_found():
    lines = A.prepare(fake_rows(0.8), A.UEFA)
    us = A.units(lines, "p", 0.15)
    res = A.ratio_ci(us)
    assert res[0] == 60 and res[2] > 1.0, res                       # le modèle croit 8, le réel est 10 : rapport ~ 1,25, borne basse au-dessus de 1
    nt, ok = A.valid(us)
    assert nt == 60 and ok == 1.0, (nt, ok)
    assert "confirmé" in A.verdict(res, ok)
    mk = A.ratio_ci(A.units(lines, "px", 0.15))
    assert mk[2] < 1.0 < mk[3], mk                                   # le marché a raison : 1 dans l'intervalle
    fair = A.ratio_ci(A.units(A.prepare(fake_rows(1.0), A.UEFA), "p", 0.15))
    assert fair[2] < 1.0 < fair[3], fair
    few = A.ratio_ci(A.units(A.prepare(fake_rows(0.8, n=10), A.UEFA), "p", 0.15))
    assert "indication" in A.verdict(few, 1.0)
    print(f"ok  biais retrouvé (rapport {res[1]:.2f}) ; marché et modèle juste : pas de biais ; moins de 30 matchs : indication")


def test_selection_and_report():
    rows = fake_rows(0.8)
    shown = [dict(r, src="fair.json") for r in rows if r["name"] == "Under" and r["point"] == 8.5][:20]
    for r in shown:
        r["cup"] = False
    others = [dict(r, src="cup_log.json") for r in rows if r["key"] not in {s["key"] for s in shown}]
    tmp = tempfile.mkdtemp(prefix="kelly-acups-db-")
    path = os.path.join(tmp, "kelly.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE docs (name TEXT PRIMARY KEY, body TEXT NOT NULL, updated TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)")
    for nm, doc in (("fair.json", shown), ("cup_log.json", others), ("ensemble_log.json", [])):
        con.execute("insert into docs values (?,?,?,1)", (nm, json.dumps([{k: v for k, v in r.items() if k != "src"} for r in doc]), "2026-10-15"))
    con.commit()
    con.close()
    before = A.sha(path)
    text = A.report(path, "soccer_epl")
    assert A.sha(path) == before, "la source n'est jamais modifiée"
    assert "corners" in text and "journal de coupe" in text and "sélection" in text and "NON CALCULÉ" in text, text
    assert "biais à la baisse confirmé" in text
    assert "Aucune ligne du journal de coupe" not in text
    lines = A.prepare(others + [dict(r, src="fair.json", final=None, result=None) for r in others[:6]], A.UEFA)
    assert sum(1 for l in lines if l["shown"]) == 6 and all(l["val"] is not None for l in lines if l["shown"]), "montrée = dans fair.json, même si le règlement vient d'un journal"
    assert A.units([dict(l, raw=False) for l in lines], "p", 0.15) == [], "une proba déjà corrigée n'est pas une mesure du modèle brut"
    assert A.prepare([dict(r, point=9.0) for r in rows], A.UEFA) == [], "ligne entière écartée (push)"
    assert "inversion non contrôlée" in A.verdict((60, 1.3, 1.2, 1.4, 0.7), None)
    print("ok  rapport complet sur une base fictive : lots, sélection, niveau des ligues non calculé, source intacte")


if __name__ == "__main__":
    test_inversion_and_actual()
    test_bias_found()
    test_selection_and_report()
    print("tests audit des coupes : OK")
