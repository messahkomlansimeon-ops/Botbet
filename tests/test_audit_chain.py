"""tools/audit_chain.py (chaîne means / calib / leg_discount, step1_ledger.json) sur une saison FICTIVE (tests/synth.py, aucun CSV réel, aucun réseau) : les couches éteintes redonnent EXACTEMENT les
chiffres bruts (C1 = C2 = C0), la couche means ne change que les moyennes, calib ne change que les probabilités, les lignes évaluées sont les mêmes dans les trois configurations, un
drapeau retire sa couche, leg_discount est accepté sans effet, aucune donnée d'après le coup d'envoi (jours d'avant seulement), grappes par match. Lancer : python -m tests.test_audit_chain"""
import argparse
import json
import os
import sys
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-auditchain-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-auditchain-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_chain as AC  # noqa: E402
from tests import synth  # noqa: E402
from kelly.sources import footballdata as fd  # noqa: E402

_CACHE = {}


def matches():
    if "m" not in _CACHE:
        out = []
        for s in ("2223", "2324", "2425"):
            out += fd.parse_csv(synth.season_csv(synth.STARTS[s], seed=int(s), teams=synth.TEAMS[:12]), "E0", s)[0]
        _CACHE["m"] = out
    return _CACHE["m"]


def run(layers):
    old = fd.cached_history
    fd.cached_history = lambda divs=None, seasons=None: matches()
    try:
        return AC.replay_div(("E0", layers, None, time.time() + 600))
    finally:
        fd.cached_history = old


def test_layers_off_is_raw():
    recs, cnt = run(set())
    assert cnt["scored"] > 150 and cnt["errors"] == 0, cnt
    assert all(r["date"] < AC.RESERVED_FROM for r in recs), "aucun match de la fenêtre réservée"
    picks = [p for r in recs for p in r["picks"]]
    assert picks and all(p["p"]["C0"] == p["p"]["C1"] == p["p"]["C2"] for p in picks), "couches éteintes : C1 = C2 = C0 à l'identique"
    assert all(v["C0"] == v["C1"] for r in recs for v in r["nll"].values())
    print("ok  couches éteintes : C1 = C2 = C0 exactement (comptes et probabilités)")


def test_means_changes_counts_and_calib_changes_probabilities():
    both, _ = run({"means", "calib"})
    means_only, _ = run({"means"})
    calib_only, _ = run({"calib"})
    raw, _ = run(set())
    assert [r["id"] for r in both] == [r["id"] for r in raw], "mêmes matchs notés dans toutes les configurations"
    # means : les comptes changent (une fois les facteurs mesurés), les probabilités C1 aussi ; calib seule : les comptes ne changent jamais
    assert any(v["C1"] != v["C0"] for r in means_only for v in r["nll"].values()), "means doit changer les comptes une fois BOOT_MIN matchs vus"
    assert all(v["C1"] == v["C0"] for r in calib_only for v in r["nll"].values()), "calib seule ne touche pas les comptes"
    assert any(p["p"]["C1"] != p["p"]["C0"] for r in means_only for p in r["picks"])
    assert all(p["p"]["C2"] == p["p"]["C1"] for r in means_only for p in r["picks"]), "sans calib : C2 = C1"
    assert all(p["p"]["C1"] == p["p"]["C0"] for r in calib_only for p in r["picks"]), "sans means : C1 = C0"
    assert any(p["p"]["C2"] != p["p"]["C1"] for r in calib_only for p in r["picks"]), "calib change les probabilités une fois assez de résultats comptés"
    # mêmes lignes dans les trois configurations : les lignes sont choisies sur les chiffres bruts
    key = lambda r: [(p["fam"], p["sens"], p["y"]) for p in r["picks"]]  # noqa: E731
    assert [key(r) for r in both] == [key(r) for r in raw] == [key(r) for r in means_only]
    # jamais l'avenir : les premiers matchs notés n'ont aucune correction des moyennes mesurée (BOOT_MIN matchs d'une famille d'abord) ; le tout premier jour n'a rien de calibré
    first = both[:20]
    assert all(v["C1"] == v["C0"] for r in first for v in r["nll"].values()) and all(p["p"]["C1"] == p["p"]["C0"] for r in first for p in r["picks"])
    assert all(p["p"]["C2"] == p["p"]["C0"] for p in both[0]["picks"]), "le premier match noté n'a aucun résultat d'avant à calibrer"
    print("ok  means ne change que les comptes et les probabilités, calib que les probabilités ; mêmes lignes ; début du rejeu sans correction (jours d'avant seulement)")


def test_table_report_and_flags():
    recs, _ = run({"means", "calib"})
    tab = AC.table(recs, {"means", "calib"})
    n, m, h = tab["count"]["corners"]
    assert n > 150 and m is not None and h is not None
    txt = AC.report(tab, {"scored": len(recs), "too_early": 0, "no_model": 0, "timeout": 0, "errors": 0}, ["E0"], {"means", "calib"}, ["", "", ""])
    assert "1. means (C1 - C0)" in txt and "3. calib (C2 - C1)" in txt and "leg_discount : non évaluable en rejeu" in txt and "corners" in txt, txt
    ns = argparse.Namespace(no_means=True, no_calib=False, no_leg_discount=True)
    layers, flags = AC.layers_from(ns)
    assert layers == {"calib"} and flags == ["--no-means", "", "--no-leg-discount"]
    assert AC.layers_from(argparse.Namespace(no_means=False, no_calib=True, no_leg_discount=False))[0] == {"means"}
    assert "retirées : --no-means, --no-leg-discount" in AC.report(tab, {"scored": 1, "too_early": 0, "no_model": 0, "timeout": 0, "errors": 0}, ["E0"], layers, flags)
    # verdict : 30 matchs et IC hors de zéro
    assert AC.verdict(100, -0.01, 0.005) == "MIEUX" and AC.verdict(100, 0.01, 0.005) == "PIRE" and AC.verdict(100, 0.001, 0.005) == "indécis" and AC.verdict(10, -0.5, 0.01) == "non lisible"
    # grappes : deux lignes du même match = une grappe
    n, m, h = AC.stats([1.0, 1.0, -1.0, -1.0], ["a", "a", "b", "b"])
    assert n == 4 and h is not None and h > AC.stats([1.0, 1.0, -1.0, -1.0], [1, 2, 3, 4])[2]
    print("ok  tableau, rapport, drapeaux --no-means / --no-calib / --no-leg-discount, verdict, grappes par match")


def run_with(ms, layers):
    old = fd.cached_history
    fd.cached_history = lambda divs=None, seasons=None: ms
    try:
        return AC.replay_div(("E0", layers, None, time.time() + 600))
    finally:
        fd.cached_history = old


def test_no_future_and_constants():
    assert AC.RESERVED_FROM == "2025-07-01" and AC.STEP_DAYS == 3 and AC.MIN_PRIOR == 7 and AC.SEASONS == ("2223", "2324", "2425") and AC.MIN_MATCHES == 30      # ledger : fenêtre, lots, 8e match
    ms = matches()
    full, _ = run_with(ms, {"means", "calib"})
    dates = sorted({r["date"] for r in full})
    cut_day = dates[len(dates) // 2]
    # (1) matchs d'APRÈS la coupure retirés : tout ce qui est noté avant est identique (aucune dépendance à l'avenir)
    trunc, _ = run_with([m for m in ms if m["date"] < cut_day], {"means", "calib"})
    before = [r for r in full if r["date"] < cut_day]
    assert json.dumps(before, sort_keys=True) == json.dumps(trunc, sort_keys=True), "les enregistrements d'avant la coupure ne dépendent pas de l'avenir"
    # (2) le résultat d'un match change : les matchs du MÊME JOUR et d'avant sont inchangés (un match ne voit que les jours d'avant)
    victim = next(m for m in ms if m["date"] == cut_day)
    flipped = [dict(m, hg=m["hg"] + 3, hc=(m.get("hc") or 0) + 9) if m is victim else m for m in ms]
    alt, _ = run_with(flipped, {"means", "calib"})
    vid = f"E0|{victim['date']}|{victim['home']}|{victim['away']}"                  # son propre enregistrement change (son résultat est le sien) : il est écarté de la comparaison
    same_or_before = [r for r in full if r["date"] <= cut_day and r["id"] != vid]
    assert same_or_before and json.dumps(same_or_before, sort_keys=True) == json.dumps([r for r in alt if r["date"] <= cut_day and r["id"] != vid], sort_keys=True), "même jour : aucun accès au résultat d'un autre match du jour"
    assert json.dumps(full, sort_keys=True) != json.dumps(alt, sort_keys=True), "le test change bien quelque chose pour les jours d'après"
    print("ok  aucun avenir : tronquer ou modifier un résultat ne change ni les matchs d'avant ni ceux du même jour ; constantes = ledger")


def test_signs_and_orientation():
    def rec(i, nll_c0, nll_c1, p, y, fam="corners", sens="plus"):
        return {"id": f"m{i}", "date": "2024-10-01", "div": "E0", "nll": {fam: {"C0": nll_c0, "C1": nll_c1}}, "picks": [{"fam": fam, "sens": sens, "y": y, "p": p}]}
    # C1 meilleur sur les comptes (nll plus petite), C2 meilleur en log-loss que C1, C1 meilleur que C0
    recs = [rec(i, 2.0, 1.5, {"C0": 0.6, "C1": 0.7, "C2": 0.9}, 1) for i in range(40)]
    tab = AC.table(recs, {"means", "calib"})
    n, m, h = tab["count"]["corners"]
    assert n == 40 and m == -0.5, (n, m)                                           # C1 - C0 négatif = la couche améliore
    for contrast, expect_sign in ((("C1", "C0"), -1), (("C2", "C1"), -1), (("C2", "C0"), -1)):
        n, m, h = tab["ll"][(contrast, "corners", "plus")]
        assert n == 40 and m * expect_sign > 0, (contrast, m)
    assert tab["ll"][(("C2", "C1"), "corners", "moins")][0] == 0 and tab["ll"][(("C2", "C1"), "all", "all")][0] == 40
    # un point par match : 3 lignes d'un match = UNE valeur (moyenne), pas trois
    r3 = {"id": "m0", "date": "2024-10-01", "div": "E0", "nll": {}, "picks": [{"fam": "corners", "sens": "plus", "y": 1, "p": {"C0": 0.5, "C1": 0.5, "C2": 0.9}} for _ in range(3)]}
    assert AC.table([r3], {"calib"})["ll"][(("C2", "C1"), "all", "all")][0] == 1
    # grappes par match
    assert AC.stats([1.0, 1.0, -1.0, -1.0], ["a", "a", "b", "b"])[2] > AC.stats([1.0, 1.0, -1.0, -1.0], [1, 2, 3, 4])[2]
    assert abs(AC.logloss(0.8, 1) - 0.2231435513) < 1e-9 and abs(AC.logloss(0.8, 0) - 1.6094379124) < 1e-9, "log-loss binaire"
    print("ok  signes des contrastes (négatif = la couche améliore), un point par match, log-loss, grappes")


def test_calib_learns_from_p1_and_correct_orientation():
    # données fictives où les comptes sont mélangés entre les matchs (indépendants des équipes) : le modèle est trop confiant, calib (bien orientée) doit AMÉLIORER la log-loss
    import random
    ms = [dict(m) for m in matches()]
    rng = random.Random(7)
    cols = [c for c in ("hc", "ac", "hy", "ay", "hs", "as", "hst", "ast", "hf", "af") if ms[0].get(c) is not None]
    for c in cols:
        vals = [m[c] for m in ms]
        rng.shuffle(vals)
        for m, v in zip(ms, vals):
            m[c] = v
    recs, _ = run_with(ms, {"calib"})
    n, m, h = AC.table(recs, {"calib"})["ll"][(("C2", "C1"), "all", "all")]
    assert n > 150 and m < 0 and m + h < 0.002, (n, m, h)
    print("ok  calib sur comptes mélangés (modèle trop confiant) : la log-loss s'améliore (orientation du résultat correcte)")


def test_combo_legs_line():
    import json
    import sqlite3
    tmp = tempfile.mkdtemp(prefix="kelly-auditchain-db-")
    con = sqlite3.connect(os.path.join(tmp, "kelly.db"))
    con.execute("create table docs (name text primary key, body text)")
    con.execute("insert into docs values (?, ?)", ("calib.json", json.dumps({"_combo": [100, 60, 66.0]})))
    con.commit()
    con.close()
    txt = AC.combo_legs_line(tmp)
    assert "100 jambes" in txt and "60.0% gagnées pour 66.0% annoncées" in txt, txt
    assert "0.909" in txt and "surestimées" in txt
    assert "absente" in AC.combo_legs_line(tempfile.mkdtemp())
    print("ok  leg_discount : ligne d'information tirée des jambes de combinés réglées de la sauvegarde")


def main():
    test_no_future_and_constants()
    test_signs_and_orientation()
    test_calib_learns_from_p1_and_correct_orientation()
    test_layers_off_is_raw()
    test_combo_legs_line()
    test_means_changes_counts_and_calib_changes_probabilities()
    test_table_report_and_flags()
    print("tests audit_chain : OK")


if __name__ == "__main__":
    main()
