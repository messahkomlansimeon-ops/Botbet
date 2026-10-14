"""Tests hors ligne du réglage automatique (kelly/tune.py) et de son usage par strength.py / prematch.py.
Lancer : python -m tests.test_tune   (données fictives : dérive de force à demi-vie connue, tests/synth.py pour les cotes)"""
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import date, timedelta

TMP = tempfile.mkdtemp(prefix="kelly-tune-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["ENSEMBLE_FROZEN_MODEL"] = "none"       # ces tests fixent la mécanique d'avant le gel (mélange du modèle à 20 % dans prematch.apply, réglage hebdomadaire des poids de buts) : le gel est dans tests/test_frozen_model.py
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import config as C, prematch, store, strength as S, tune  # noqa: E402
from kelly.sources import footballdata as fd  # noqa: E402

def corrupt(name):
    """Document illisible dans la base (JSON cassé), comme un ancien fichier abîmé."""
    with store._db() as con:
        con.execute("INSERT OR REPLACE INTO docs (name, body, updated) VALUES (?, '{pas du json', 'x')", (name,))


from tests import synth  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def drift_matches(seed, half_life_days, sd, seasons=4, n=20, H=1.3, m=1.25):
    """Saisons fictives dont les forces (log attaque / défense) suivent un processus AR(1) hebdomadaire d'écart-type stationnaire `sd`
    et d'autocorrélation divisée par 2 en `half_life_days` jours (None : forces fixes). Pas de cotes."""
    rng = random.Random(seed)
    teams = [f"Team {chr(65 + i)}" for i in range(n)]
    rho = 0.5 ** (7 / half_life_days) if half_life_days else 1.0
    s = sd * math.sqrt(1 - rho * rho)
    att = {t: rng.gauss(0, sd) for t in teams}
    dfn = {t: rng.gauss(0, sd) for t in teams}
    out = []
    for se in range(seasons):
        for k, day in enumerate(synth.rounds(teams)):
            d = date(2022, 8, 6) + timedelta(days=365 * se + 7 * k)
            for t in teams:
                att[t] = rho * att[t] + rng.gauss(0, s)
                dfn[t] = rho * dfn[t] + rng.gauss(0, s)
            for h, a in day:
                hg = synth.poisson(rng, m * H * math.exp(att[h] + dfn[a]))
                ag = synth.poisson(rng, m * math.exp(att[a] + dfn[h]))
                out.append({"date": d.isoformat(), "div": "E0", "home": h, "away": a, "hg": hg, "ag": ag,
                            "ftr": "H" if hg > ag else "A" if hg < ag else "D", "referee": None, "odds": {}})
    return out


def synth_matches(seasons=("2223", "2324", "2425")):
    """Saisons de tests/synth.py (avec cotes d'ouverture et de clôture Pinnacle / moyenne)."""
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s), "E0", s)
        assert not probs, probs
        out += rows
    return out


def test_half_life_prefers_truth():
    """Données à dérive rapide (demi-vie 25 j) : une demi-vie courte bat une longue ; forces fixes : l'inverse."""
    cands = (45, 110, 330)
    for truth, sd, seed in ((25, 0.5, 2), (None, 0.25, 3)):
        ms = drift_matches(seed, truth, sd)
        tf = (date.fromisoformat(ms[-1]["date"]) - timedelta(days=365)).isoformat()
        recs = {h: tune.evaluate(ms, tf, h, step_days=14, div="E0") for h in cands}
        tab = tune.score_table(recs)
        ll = {h: tab[h]["ll"] for h in cands}
        assert all(tab[h]["n"] == tab[110]["n"] >= 300 for h in cands), tab
        if truth:
            assert ll[45] < ll[330] - 0.005, ll
            chosen, gain, best = tune.pick_half_life(tab)
            assert best == 45 and chosen in (45, 110) and gain >= 0, (chosen, gain, best, ll)
        else:
            assert ll[330] < ll[45] - 0.005, ll
            chosen, gain, best = tune.pick_half_life(tab)
            assert best in (110, 330) and chosen in (110, 330), (chosen, best, ll)
        print(f"  dérive {truth or 'nulle'} : log-loss " + " ".join(f"{h} j {ll[h]:.4f}" for h in cands) + f" -> garde {chosen} j")
    # marge anti-bruit : un gain sous la marge laisse 110
    tab = {60: {"ll": 0.8700}, 110: {"ll": 0.8710}, 220: {"ll": 0.8705}}
    assert tune.pick_half_life(tab, margin=0.002)[0] == 110 and tune.pick_half_life(tab, margin=0.0005)[0] == 60
    assert tune.pick_half_life({110: {"ll": 0.9}, 330: {"ll": 0.9}}) == (110, 0.0, 110)
    print("demi-vie : OK")


def rec(rng, truth3, truth_o, model3, model_o, book3, book_o):
    """Un match fictif : issue tirée dans la vraie loi -> (k, plus de 2,5, p modèle 1N2, p modèle +2,5, p books 1N2, p books +2,5)."""
    r, k = rng.random(), 2
    for i, p in enumerate(truth3):
        if r < p:
            k = i
            break
        r -= p
    return (k, 1 if rng.random() < truth_o else 0, model3, model_o, book3, book_o)


def norm(v):
    s = sum(v)
    return [x / s for x in v]


def test_goal_weight_search():
    rng = random.Random(5)
    ws = tune.grid()
    assert ws[0] == 0.0 and ws[-1] == 0.5 and len(ws) == 11 and close(ws[1], 0.05)
    # 1. le modèle est du bruit pur, les books connaissent la vraie loi : poids 0
    noise = []
    for _ in range(4000):
        t3 = norm([rng.uniform(0.5, 2.0), rng.uniform(0.5, 1.5), rng.uniform(0.5, 2.0)])
        to = rng.uniform(0.3, 0.7)
        m3 = norm([rng.uniform(0.1, 1.0) for _ in range(3)])
        noise.append(rec(rng, t3, to, m3, rng.uniform(0.1, 0.9), t3, to))
    c = tune.weight_curves(noise)
    assert c["n1"] == c["no"] == 4000 and len(c["ll1"]) == len(c["llo"]) == 11
    b = tune.best_weight([c])
    assert b[0] == 0.0 and close(b[1], b[2]), b
    # 2. le modèle porte un vrai signal que les books n'ont pas (books = loi « floue », modèle = vraie loi + petit bruit) : poids > 0
    sig = []
    for _ in range(4000):
        t3 = norm([rng.uniform(0.3, 2.5), rng.uniform(0.5, 1.5), rng.uniform(0.3, 2.5)])
        to = rng.uniform(0.2, 0.8)
        blur3 = norm([0.5 * x + 0.5 / 3 for x in t3])
        m3 = norm([x * math.exp(rng.gauss(0, 0.1)) for x in t3])
        sig.append(rec(rng, t3, to, m3, min(0.97, max(0.03, to + rng.gauss(0, 0.03))), blur3, 0.5 + 0.4 * (to - 0.5)))
    c2 = tune.weight_curves(sig)
    b2 = tune.best_weight([c2])
    assert b2[0] >= 0.3 and b2[1] < b2[2] - 0.01, b2
    assert tune.best_weight([c2], "1x2")[0] > 0 and tune.best_weight([c2], "ou25")[0] > 0
    # lignes sans cote d'ouverture : ignorées ; aucune cote : pas de poids
    nobook = [(r[0], r[1], r[2], r[3], None, None) for r in sig[:50]]
    cn = tune.weight_curves(nobook + [None])
    assert cn["n1"] == cn["no"] == 0 and tune.best_weight([cn]) is None
    # 3. rétrécissement vers le poids global : peu de matchs = proche du global, beaucoup = proche du brut
    big = tune.weight_curves(noise)
    small = tune.weight_curves(sig[:200])
    C.TUNE_SHRINK_N, old = 600, C.TUNE_SHRINK_N
    try:
        g, per = tune.solve_weights({"AAA": {"gw": big}, "BBB": {"gw": small}, "CCC": {"skipped": "x"}})
        raw_small = tune.best_weight([small])[0]
        assert 0.0 <= g <= 0.5 and set(per) == {"AAA", "BBB"}
        assert abs(per["BBB"] - g) <= abs(raw_small - g) + 1e-9
        assert per["BBB"] == round((200 * raw_small + 600 * g) / 800, 3)
        mid = {"gw": dict(small, n1=100, no=100)}                     # sous TUNE_MIN_TEST : non mesurée
        assert "DDD" not in tune.solve_weights({"AAA": {"gw": big}, "DDD": mid})[1]
    finally:
        C.TUNE_SHRINK_N = old
    print(f"poids des buts : bruit -> {b[0]}, signal -> {b2[0]} (log-loss {b2[2]:.4f} -> {b2[1]:.4f}) : OK")


def test_book_probs():
    ms = synth_matches(("2223",))
    p3, po = tune.book_probs(ms[0])
    assert p3 and po is not None and close(sum(p3), 1, 1e-9) and 0 < po < 1
    o = ms[0]["odds"]
    assert close(p3[0], fd.no_vig(o["1x2"]["open"]["ps"])[0], 1e-12) and close(po, fd.no_vig(o["ou25"]["open"]["ps"])[0], 1e-12)
    avg_only = {"odds": {"1x2": {"open": {"avg": [2.0, 3.5, 4.0]}}, "ou25": {"open": {"avg": [1.9, 2.0]}}}}
    q3, qo = tune.book_probs(avg_only)
    assert close(q3[0], fd.no_vig([2.0, 3.5, 4.0])[0], 1e-12) and close(qo, fd.no_vig([1.9, 2.0])[0], 1e-12)
    assert tune.book_probs({"odds": {}}) == (None, None) and tune.book_probs({}) == (None, None)
    print("cotes d'ouverture : OK")


def with_cfg(**kw):
    """Remplace des réglages de config, renvoie de quoi les restaurer."""
    old = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    return old


def reset_data():
    shutil.rmtree(store.DATA_DIR, ignore_errors=True)
    S.reload()


def test_tune_div_and_file_roundtrip():
    reset_data()
    ms = synth_matches()
    old = with_cfg(TUNE_HALF_LIVES=(60, 110, 330), TUNE_STEP_DAYS=28, TUNE_MIN_TEST=100)
    try:
        # historique trop court / vide : ligue ignorée avec une raison
        assert "trop court" in tune.tune_div("E0", [m for m in ms if m["date"] < "2023-03-01"])["skipped"]
        assert "aucun" in tune.tune_div("E0", [])["skipped"]
        r = tune.tune_div("E0", ms)
        assert r["n"] >= 300 and r["chosen"] in (60, 110, 330) and set(r["hl"]) == {"60", "110", "330"}
        assert r["gw"]["n1"] > 150 and r["gw"]["no"] > 150 and r["n_hl"] < r["n"] and r["best"] in (60, 110, 330) and r["gain"] >= 0
        assert S.HALF_LIFE == 110 and r["chosen"] == 110 or r["gain"] >= C.TUNE_MARGIN          # 110 gardé, sauf gain net
        res = tune.run(["E0", "SP1"], lambda d: ms if d == "E0" else [], seconds=600, force=True)
        assert res["done"] == ["E0"] and "SP1" in res["skipped"] and not res["pending"], res
        data = store.load(tune.FILE, None)
        assert set(data) == {"built", "half_life", "goal_weight", "report"} and data["built"][:2] == "20"
        assert data["half_life"] == {"E0": r["chosen"]} and "_global" in data["goal_weight"] and 0 <= data["goal_weight"]["E0"] <= 0.5
        assert data["report"]["E0"]["chosen"] == r["chosen"] and "skipped" in data["report"]["SP1"]
        txt = tune.format_report(res)
        assert "E0 : demi-vie" in txt and "poids des buts" in txt and "SP1 ignorée" in txt and "Corners et cartons" in txt, txt
        # relu par strength (mémoire ; la date du fichier peut ne pas avoir bougé : reload)
        S.reload()
        assert S.tuning()["half_life"] == {"E0": r["chosen"]} and S.hl_for("E0") == r["chosen"]
        assert close(S.goal_weight("E0"), data["goal_weight"]["E0"]) and S.goal_weight("SP1") is None
        print(txt)
    finally:
        with_cfg(**old)
    print("tune_div / tuning.json : OK")


def test_strength_and_prematch_use_tuned_values():
    reset_data()
    # sans fichier : replis
    assert S.tuning() == {} and S.hl_for("E0") == S.HALF_LIFE and S.hl_for(None) == S.HALF_LIFE and S.goal_weight("E0") is None
    ms = drift_matches(4, None, 0.25, seasons=3)
    on = date(2025, 5, 1)
    assert S.get("E0", on, loader=lambda d: ms)["hl"] == S.HALF_LIFE
    # réglages mesurés : la ligue réglée change de demi-vie, les autres gardent 110
    store.save(tune.FILE, {"built": "2026-01-01T00:00:00", "half_life": {"E0": 220, "SP1": 2, "I1": "x", "D1": True},
                           "goal_weight": {"_global": 0.05, "E0": 0.35, "SP1": -1, "I1": float("nan")}, "report": {}})
    S.reload()
    assert S.hl_for("E0") == 220 and S.hl_for("SP1") == S.HALF_LIFE and S.hl_for("I1") == S.HALF_LIFE and S.hl_for("D1") == S.HALF_LIFE
    assert S.goal_weight("E0") == 0.35 and S.goal_weight("SP1") is None and S.goal_weight("I1") is None and S.goal_weight("F1") is None
    m1 = S.get("E0", on, loader=lambda d: ms)
    assert m1["hl"] == 220 and "|hl220" in m1["key"], (m1["hl"], m1["key"])          # le cache du jour est refait avec la nouvelle demi-vie
    store.save(tune.FILE, {"half_life": {"E0": 330}})                                    # fichier modifié : relu sans reload (même processus)
    assert S.hl_for("E0") == 330
    corrupt(tune.FILE)
    assert S.tuning() == {} and S.hl_for("E0") == S.HALF_LIFE                            # fichier illisible : replis
    # prematch : poids des buts de la ligue du pari, repli sur config
    store.save(tune.FILE, {"half_life": {}, "goal_weight": {"E0": 0.35}, "report": {}})
    S.reload()
    model = S.build(ms, on, div="E0")
    pred = S.predict(model, "Team A", "Team B", on)
    f = {"deep": pred}
    pick = {"market": "totals", "name": "Over", "point": 2.5, "prob": 0.5, "odds": 2.0, "edge": 0.0, "score": 0.0, "weight": 1.0,
            "why": "", "home": "Team A", "away": "Team B", "sport_key": "soccer_epl"}
    q = pred["qs"]["goals"]
    a = prematch.assess(pick, f, None)
    assert a["kind"] == "goals" and close(a["w"], 0.35 * prematch.qscale(q)), a["w"]
    b = prematch.assess(dict(pick, market="btts", name="Yes", point=None), f, None)
    assert b["kind"] == "btts" and close(b["w"], C.MODEL_WEIGHT["goals"] * prematch.qscale(q))      # les 2 marquent : poids de config, jamais celui du plus/moins de 2,5 (non mesuré)
    for sk in ("soccer_spain_la_liga", None, "soccer_unknown"):                       # ligue non mesurée / inconnue / sans clé : config
        assert close(prematch.assess(dict(pick, sport_key=sk), f, None)["w"], C.MODEL_WEIGHT["goals"] * prematch.qscale(q))
    prematch.apply(pick, a)
    assert close(pick["prob"], (1 - a["w"]) * 0.5 + a["w"] * a["p"], 1e-9)
    store.remove(tune.FILE)
    S.reload()
    assert close(prematch.assess(pick | {"prob": 0.5}, f, None)["w"], C.MODEL_WEIGHT["goals"] * prematch.qscale(q))
    print("strength / prematch : valeurs mesurées et replis : OK")


def test_time_budget_and_resume():
    reset_data()
    clk = [0.0]
    old_clock, old_td = tune.clock, tune.tune_div
    tune.clock = lambda: clk[0]
    calls = []

    def fake(div, matches, deadline=None):
        calls.append(div)
        clk[0] += 10.0                                           # chaque ligue « dure » 10 s
        return {"n": 300, "chosen": 150, "gain": 0.004, "best": 150, "hl": {"110": {"ll": 0.9}, "150": {"ll": 0.896}},
                "gw": {"n1": 300, "no": 300, "ll1": [1.0] * 11, "llo": [0.7] * 11}}

    tune.tune_div = fake
    try:
        divs = ["E0", "SP1", "I1", "D1", "F1"]
        res = tune.run(divs, lambda d: [], seconds=25, force=True)
        assert calls == ["E0", "SP1", "I1"] and res["pending"] == ["D1", "F1"] and res["done"] == ["E0", "SP1", "I1"], (calls, res)
        assert res["seconds"] == 30.0                                # une ligue commencée va au bout, aucune n'est commencée après
        d = store.load(tune.FILE, None)
        assert set(d["half_life"]) == {"E0", "SP1", "I1"}            # fichier partiel déjà utilisable
        assert tune.run(divs, lambda d: [], seconds=0)["done"] == [] and calls == ["E0", "SP1", "I1"]          # budget nul : rien
        # reprise : les ligues jamais réglées d'abord (ordre d'importance), les récentes sont sautées
        calls.clear()
        res = tune.run(divs, lambda d: [], seconds=1000)
        assert calls == ["D1", "F1"] and not res["pending"], calls
        d = store.load(tune.FILE, None)
        assert set(d["half_life"]) == set(divs) and d["goal_weight"]["_global"] == 0.0
        # tout est frais : rien à faire ; force : tout refait, les plus anciennes d'abord
        calls.clear()
        assert tune.run(divs, lambda d: [], seconds=1000)["done"] == [] and not calls
        tune.run(divs, lambda d: [], seconds=1000, force=True)
        assert calls == ["E0", "SP1", "I1", "D1", "F1"], calls
        # le budget est aussi respecté DANS une ligue (vraie évaluation, horloge qui dépasse l'échéance) : ligue abandonnée, pas de rapport partiel
        tune.tune_div = old_td
        ms = synth_matches()
        reset_data()
        t = [0.0]

        def tick():
            t[0] += 1.0
            return t[0]

        tune.clock = tick
        res = tune.run(["E0"], lambda d: ms, seconds=3)
        assert res["done"] == [] and res["pending"] == ["E0"], res
        assert "E0" not in (store.load(tune.FILE, None)["report"]), "pas de résultat partiel"
        # une ligue qui plante n'arrête pas les autres
        tune.clock = lambda: 0.0
        res = tune.run(["E0", "SP1"], lambda d: 1 / 0 if d == "E0" else [], seconds=100, force=True)
        assert "ZeroDivisionError" in res["skipped"]["E0"] and "SP1" in res["skipped"]
    finally:
        tune.clock, tune.tune_div = old_clock, old_td
    print("budget de temps et reprise : OK")


def test_main_and_cli():
    reset_data()
    # main ne lève jamais : en cas d'échec, code 1
    old_run = tune.run
    tune.run = lambda **kw: 1 / 0
    try:
        assert tune.main([]) == 1
    finally:
        tune.run = old_run
    # python bot.py --tune hors ligne, sans CSV en cache : rapport (rien à régler), code 0, pas de crash
    env = dict(os.environ, DATA_DIR=os.path.join(TMP, "cli-data"), SOURCES_CACHE_DIR=os.path.join(TMP, "cli-cache"),
               ODDS_CACHE_DIR=os.path.join(TMP, "cli-odds"), TUNE_SECONDS="60")
    env.pop("TELEGRAM_TOKEN", None)
    r = subprocess.run([sys.executable, "bot.py", "--tune"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "Réglage Kelly" in r.stdout and "0 ligue(s) réglée(s)" in r.stdout and "aucun historique" in r.stdout, r.stdout
    with closing(sqlite3.connect(os.path.join(TMP, "cli-data", store.DB_FILE))) as con:
        assert con.execute("SELECT 1 FROM docs WHERE name='tuning.json'").fetchone()
    print("bot.py --tune : OK")


def main():
    test_book_probs()
    test_goal_weight_search()
    test_half_life_prefers_truth()
    test_tune_div_and_file_roundtrip()
    test_strength_and_prematch_use_tuned_values()
    test_time_budget_and_resume()
    test_main_and_cli()
    shutil.rmtree(TMP, ignore_errors=True)
    print("tests réglage : OK")


if __name__ == "__main__":
    main()
