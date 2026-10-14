"""Section « B. Marchés de buts » (hors ligne) : avis sharp élargis (Betfair, Matchbook), « probable » au lieu de « valeur » sans avis sharp (et aucune mise sans
espérance au prix 1xBet sans marge), ancrage du modèle de force sur les cotes de clôture, réglage qui tourne vraiment (rétention des CSV « new »), fin du double
rapprochement vers 50 %, ligne choisie à l'espérance au prix 1xBet, xG dans les pronostics sans cote, binomiale négative bivariée et rho dans le repli « 2 marquent »,
ainsi que les petits restes des vagues A et C : poids de marché des corners d'une équipe / cartons jaunes, arbitre dans /analyse, règlement des joueurs par identifiant.
Lancer : python -m tests.test_goal_markets"""
import json
import math
import os
import random
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-goalmk-")
os.environ["ENSEMBLE_FROZEN_MODEL"] = "none"       # ces tests fixent le comportement D'AVANT le gel du poids du modèle (audit du 14/10) : un ensemble vide le reproduit exactement
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-goalmk-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-goalmk-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, combos as CB, config as C, ensemble as E, fair, fmt, money, odds, prematch, settle, stats, store, strength as S, tune, xprice  # noqa: E402
from kelly.sources import footballdata as fd, state  # noqa: E402

from tests import synth  # noqa: E402


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def synth_matches(seasons=("2223", "2324", "2425")):
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s), "E0", s)
        assert not probs, probs
        out += rows
    return out


MS = synth_matches()


def prono(market="totals", name="Over", point=1.5, p0=0.76, alts=None, **kw):
    c = {"key": f"m|{market}||{point if point is not None else ''}|{name}", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B",
         "commence": "2026-10-09T19:00:00Z", "start": "19:00", "league": "Premier League", "event": "A - B", "market": market, "name": name, "point": point,
         "who": None, "group": "soccer", "pick": "Plus de 1,5 buts", "prob": p0, "p0": p0, "min_odds": 1.4, "conf": 0.6, "n_min": 8, "score": 0.2,
         "alts": alts or [[name, point, p0, None]]}
    return c | kw


def pair(market, name, point, a, b):
    other = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}[name]
    return {(market, name, point): a, (market, other, point): b}


# ---------- 1. règle de valeur sans Pinnacle ----------

def test_sharp_books_and_probable():
    assert {"pinnacle", "betfair_ex_eu", "matchbook"} <= set(C.SHARP_BOOKS)
    p, src, n = odds.consensus([("betfair_ex_eu", 0.60), ("matchbook", 0.62), ("unibet", 0.55), ("coolbet", 0.57)])
    assert src == "sharp" and n == 2 and close(p, 0.7 * 0.61 + 0.3 * 0.56), (p, src, n)
    assert odds.consensus([("pinnacle", 0.6), ("betfair_ex_eu", 0.62), ("x", 0.5)])[1] == "pinnacle"
    assert odds.consensus([("unibet", 0.55), ("coolbet", 0.57)])[1:] == ("consensus", 0)
    assert E.sharp_of({"prob_book": 0.6, "prob_src": "sharp", "n_sharp": 1}) == (0.6, "sharp", 1)
    # avec sharp : la règle de valeur d'avant (cote x proba retenue >= 1,03)
    v = E.value_of("totals", 0.76, 0.66, 0.74, 1.45)
    assert v["ok"] and v["kind"] == "value" and v["ev"] > 0.03
    # sans sharp : même proba retenue, mais jamais « valeur » ; « probable » seulement si assez probable et pas bradé
    v = E.value_of("totals", 0.76, 0.66, None, 1.45)
    assert v["ok"] and v["kind"] == "probable" and v["ev"] < 0.03 and v["ev_x"] < 0 and close(v["ev_x"], 0.66 * 1.45 - 1)
    for price in (1.15, 1.45, 1.80, 2.60):                     # quelle que soit la cote, jamais « valeur » sans sharp
        assert E.value_of("totals", 0.76, 0.66, None, price)["kind"] != "value"
    assert E.value_of("totals", 0.55, 0.52, None, 1.9)["why"] == "not_probable"
    assert E.value_of("btts", 0.30, 0.52, None, 3.0)["why"] == "veto", "le veto vaut aussi sans sharp"
    assert E.value_of("totals", 0.76, None, None, 1.45)["why"] == "no_market"
    # corners (marché léger) : la règle d'avant (le modèle compte pour moitié)
    assert E.value_of("alternate_totals_corners", 0.75, 0.55, None, 2.0)["kind"] == "value"
    print("ok  Betfair / Matchbook = avis sharp ; sans avis sharp : « probable », jamais « valeur »")


def test_probable_flow_display_and_stakes():
    C.ENSEMBLE_ENABLED = True
    base = prono(p0=0.76, alts=[["Over", 1.5, 0.76, None], ["Under", 1.5, 0.24, None]])
    px = pair("totals", "Over", 1.5, 1.45, 2.9)
    refs = {k: [900, 1 + i, 0] for i, k in enumerate(px)}
    nos = dict(base)
    assert xprice.price(nos, px, refs) == "value" and nos["xkind"] == "probable" and nos["xev_x"] < 0 and CB.probable(nos) and CB.viable(nos)
    assert 0.6 <= nos["prob"] < 0.76 and nos["min_odds"] <= nos["xodds"] == 1.45
    sh = dict(base, psh={"over|1.5": [0.74, "betfair", 1, None]})
    assert xprice.price(sh, px, refs) == "value" and sh["xkind"] == "value" and not CB.probable(sh) and sh["p_sharp"] == 0.74
    # « probable » n'est jamais une raison « stats » d'un combiné
    assert not CB.stats_allowed(nos | {"pm": {"kind": "goals", "pgap": 0.2}}) and CB.stats_allowed(sh | {"pm": {"kind": "goals", "pgap": 0.2}})
    # affichage : 🎯 et « probable », jamais « valeur » ni 💎 pour la ligne sans sharp
    text = str(fmt.pronos([nos]))
    assert "🎯 Plus de 1,5 buts" in text and "💎" not in text and "valeur +" not in text and "pas une valeur" in text and "aucun book de référence" in text, text
    t2 = str(fmt.pronos([sh]))
    assert "💎 Plus de 1,5 buts" in t2 and "valeur +" in t2 and "🎯" not in t2 and "probable" not in t2, t2
    # mise : calculée sur la proba de 1xBet sans marge pour une jambe « probable » -> espérance négative -> aucune mise
    assert CB.stake_prob([sh], 0.6) == 0.6 and CB.stake_prob([nos], nos["prob"]) < nos["prob"] and close(CB.stake_prob([nos], nos["prob"]), nos["p_x"], 1e-3)
    assert CB.stake_prob([nos | {"p_x": None, "xp": None}], 0.7) == 0.0
    assert nos["prob"] * nos["xodds"] > 0.95 and nos["p_x"] * nos["xodds"] < 1
    money.set_daily(5000)
    try:
        leg2 = prono(market="btts", name="Yes", point=None, p0=0.7) | {"id": "n"}
        px2 = pair("btts", "Yes", None, 1.45, 2.8)
        assert xprice.price(leg2, px2, {k: [901, 1, 0] for k in px2}) == "value" and CB.probable(leg2)
        for sharp in (False, True):
            legs = [nos | {"odds": 1.45}, leg2 | {"odds": leg2["xodds"]}] if not sharp else [sh | {"odds": 1.45}, sh | {"id": "z", "odds": 1.45}]
            total = math.prod(c["odds"] for c in legs)
            prob = math.prod(c["prob"] for c in legs)
            st, _, _, _ = app.day_stakes(None, [], [(5, (legs, total, prob))])
            if sharp:
                assert prob * total > 1 and st[("fair", 5)] > 0, (prob, total, st)       # jambes confirmées par un book de référence : mise comme avant
            else:
                assert st[("fair", 5)] == 50, st                         # une jambe sans avis sharp est « fill » : combiné SANS valeur, hors Kelly, mise fixe = un pas d'arrondi (audit du 14/10 ; avant : aucune mise)
                assert money.stake_words(0, CB.stake_prob(legs, prob), total) == money.NO_EDGE
                assert "Kelly ne conseille pas" in (app._stake_words(app._budget_stake(CB.stake_prob(legs, prob), total), CB.stake_prob(legs, prob), total))
    finally:
        store.save("money.json", {})
    # interrupteur : ENSEMBLE_PROBABLE=0 -> sans sharp rien n'est validé
    C.ENSEMBLE_PROBABLE = False
    try:
        off = dict(base)
        assert xprice.price(off, px, refs) == "low" and off["xwhy"] == "no_sharp" and not CB.viable(off) and "xkind" not in off
    finally:
        C.ENSEMBLE_PROBABLE = True
    print("ok  pronostic « probable » : 🎯, sans « valeur », sans mise ; avec sharp : 💎 et mise comme avant ; interrupteur")


def test_line_choice_by_ev_at_1xbet_price():
    """Deux lignes « probables » : celle dont l'espérance au prix 1xBet est la moins mauvaise gagne (valeur x proba préférait la proba la plus BASSE quand l'espérance est négative)."""
    C.ENSEMBLE_ENABLED = True
    alts = [["Over", 1.5, 0.80, None], ["Over", 2.5, 0.62, None], ["Under", 3.5, 0.78, None]]
    c = prono(point=1.5, alts=alts, p0=0.80)
    px = {}
    px |= pair("totals", "Over", 1.5, 1.30, 3.7)            # marge 5 %
    px |= pair("totals", "Over", 2.5, 1.60, 2.25)           # marge 7 %
    px |= pair("totals", "Under", 3.5, 1.28, 3.9)           # marge 3,7 %
    got = xprice.choose(c, px)
    assert got is not None
    v1 = [E.value_of("totals", 0.80, E.x_fair(px, "totals", "Over", 1.5), None, 1.30), E.value_of("totals", 0.62, E.x_fair(px, "totals", "Over", 2.5), None, 1.60),
          E.value_of("totals", 0.78, E.x_fair(px, "totals", "Under", 3.5), None, 1.28)]
    ranks = [xprice.line_rank(v) for v in v1 if v["ok"]]
    assert ranks and max(ranks) == xprice.line_rank(next(v for v in v1 if v["ok"] and (v["p"], v["ev"]) == (got[2], got[6])))
    evs = {(n, p): v["ev"] for (n, p, *_), v in zip(alts, v1) if v["ok"]}
    best = max(evs, key=evs.get)
    assert (got[0], got[1]) == best, (got, evs)
    # valeur (avec sharp) : toujours devant les probables
    ok_value = {"kind": "value", "p": 0.62, "ev": 0.05}
    ok_prob = {"kind": "probable", "p": 0.80, "ev": -0.01}
    assert xprice.line_rank(ok_value) > xprice.line_rank(ok_prob)
    assert xprice.line_rank({"kind": "probable", "p": 0.7, "ev": -0.02}) > xprice.line_rank({"kind": "probable", "p": 0.7, "ev": -0.07})
    assert xprice.line_rank({"kind": "value", "p": 0.7, "ev": 0.04}) > xprice.line_rank({"kind": "value", "p": 0.6, "ev": 0.04})      # à valeur égale, la plus probable
    print("ok  ligne choisie à l'espérance au prix 1xBet (la moins négative des probables, les valeurs d'abord)")


# ---------- 2. ancrage marché ----------

def test_implied_goals_from_closing_odds():
    got = errs = 0
    for m in MS[:300]:
        g = S.implied_goals(m)
        assert g is not None, m["odds"]
        lh, la = synth.ATT[m["home"]] * synth.DEF[m["away"]] / 1.2, synth.ATT[m["away"]] * synth.DEF[m["home"]] / 1.2
        errs += abs(g[0] - lh) / lh + abs(g[1] - la) / la
        got += 2
        assert g[2] == "ps"
    assert errs / got < 0.04, errs / got                      # buts attendus retrouvés à 2-4 % près (marge et bruit des cotes fictives)
    # Pinnacle absent : moyenne des books ; 1N2 seul (CSV « new » : pas de plus/moins) ou cotes absurdes : pas d'ancrage, jamais de valeur inventée
    m = MS[10]
    avg_only = {"odds": {"1x2": {"close": {"avg": m["odds"]["1x2"]["close"]["avg"]}}, "ou25": {"close": {"avg": m["odds"]["ou25"]["close"]["avg"]}}},
                "date": "x1", "home": "H", "away": "A", "div": "E0"}
    assert S.implied_goals(avg_only)[2] == "avg"
    assert S.implied_goals({"odds": {"1x2": m["odds"]["1x2"]}, "date": "x2", "home": "H", "away": "A"}) is None
    assert S.implied_goals({"odds": {}, "date": "x3", "home": "H", "away": "A"}) is None
    assert S.implied_goals({"odds": {"1x2": {"close": {"ps": [1.2, 1.2, 1.2]}}, "ou25": {"close": {"ps": [1.2, 1.2]}}}, "date": "x4", "home": "H", "away": "A"}) is None
    print("ok  buts attendus implicites des cotes de clôture (Pinnacle, sinon moyenne) : erreur moyenne", round(errs / got * 100, 1), "%")


def test_market_anchoring_backtest():
    """Backtest walk-forward sur les données fictives (tests/synth.py : cotes de clôture tirées des vraies forces, marge 5 % et bruit) : l'ancrage améliore le log-loss
    du 1N2 et du plus de 2,5 buts et réduit la sous-estimation des favoris."""
    ms = synth_matches(("2223", "2324", "2425", "2526"))
    res = {}
    for w in (0.0, C.STRENGTH_MKT_WEIGHT):
        r = S.backtest(ms, "2024-08-01", div="E0", mkt=w)
        n = pm = pk = act = 0
        for m, p3, po in S.walk(ms, "2024-08-01", None, S.HALF_LIFE, 3, True, ("goals",), "E0", w):
            if not p3:
                continue
            mk3, _ = S.market_probs(m)
            fav = max(range(3), key=lambda i: mk3[i])
            if fav == 1 or mk3[fav] < 0.5:
                continue
            n += 1
            pm += p3[fav]
            pk += mk3[fav]
            act += "HDA".index(m["ftr"]) == fav
        res[w] = (r, pm / n, pk / n, act / n, n)
    (r0, f0, mk0, a0, n0), (r1, f1, mk1, a1, n1) = res[0.0], res[C.STRENGTH_MKT_WEIGHT]
    print(f"  backtest {r0['n']} matchs, 1N2 log-loss {r0['model']['ll_1x2']:.4f} -> {r1['model']['ll_1x2']:.4f} (marché {r1['market']['ll_1x2']:.4f}) ; "
          f"plus de 2,5 {r0['model']['ll_o25']:.4f} -> {r1['model']['ll_o25']:.4f} (marché {r1['market']['ll_o25']:.4f})")
    print(f"  favoris du marché (> 50 %, n = {n1}) : proba du modèle {f0:.3f} -> {f1:.3f}, marché {mk1:.3f}, réel {a1:.3f}")
    assert r1["model"]["ll_1x2"] < r0["model"]["ll_1x2"] - 0.005 and r1["model"]["br_1x2"] < r0["model"]["br_1x2"]
    assert r1["model"]["ll_o25"] < r0["model"]["ll_o25"] - 0.003 and r1["model"]["br_o25"] < r0["model"]["br_o25"]
    assert abs(f1 - a1) < abs(f0 - a0) and f1 > f0 + 0.015, (f0, f1, a1)           # les favoris ne sont plus sous-estimés autant
    assert r1["model"]["ll_1x2"] > r1["market"]["ll_1x2"] - 0.02, "le modèle ne bat pas la clôture de CE match (elle la connaît) : sinon, fuite"
    # le modèle garde la trace de l'ancrage et mesure la dispersion sur les buts RÉELS
    on = date(2025, 5, 1)
    m1 = S.build(ms, on, div="E0", mkt=0.5)
    m0 = S.build(ms, on, div="E0", mkt=0.0)
    g1, g0 = m1["stats"]["goals"], m0["stats"]["goals"]
    assert g1["mkt"] == 0.5 and g1["n_mkt"] > 600 and g0["mkt"] == 0.0 and g0["n_mkt"] == 0 and m1["v"] == S.VERSION == 4
    assert abs(g1["alpha"] - g0["alpha"]) < 0.08 and g1["atot"] < 0.08
    # cache : l'ancrage fait partie de la clé (un cache fait sans ancrage est refait)
    old_w = C.STRENGTH_MKT_WEIGHT
    S.reload()
    try:
        a = S.get("E0", on, loader=lambda d: ms)
        assert a["key"].endswith("|mk0.5")
        C.STRENGTH_MKT_WEIGHT = 0.0
        S.reload()
        assert S.get("E0", on, loader=lambda d: ms)["stats"]["goals"]["n_mkt"] == 0
    finally:
        C.STRENGTH_MKT_WEIGHT = old_w
        S.reload()
    print("ok  ancrage marché : log-loss en baisse, favoris moins sous-estimés, dispersion sur les buts réels, cache versionné")


# ---------- 3. le réglage tourne ----------

def test_tune_runs_and_retention():
    # rétention des CSV « new » (championnats en année civile) : 800 jours (400 avant : « historique trop court » pour toujours)
    assert fd.NEW_KEEP_DAYS >= 540 + 150
    real_now = state.now
    state.now = lambda: datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
    try:
        rows = [{"date": (date(2026, 10, 12) - timedelta(days=d)).isoformat()} for d in (10, 300, 500, 700, 790, 900)]
        assert [r["date"] for r in fd._recent(rows)] == [r["date"] for r in rows[:5]]
        old = fd.NEW_KEEP_DAYS
        fd.NEW_KEEP_DAYS = 400
        try:
            assert len(fd._recent(rows)) == 2
        finally:
            fd.NEW_KEEP_DAYS = old
    finally:
        state.now = real_now
    # tune.run règle réellement une ligue européenne (3 saisons) et une ligue en année civile gardée 800 jours, mais pas une ligue de 400 jours
    ms = synth_matches(("2324", "2425", "2526"))
    cal = [dict(m, div="BRA") for m in ms if m["date"] >= "2024-07-01"]               # ~ 2 ans (780 jours)
    short = [dict(m, div="USA") for m in ms if m["date"] >= "2025-04-20"]
    old = (C.TUNE_HALF_LIVES, C.TUNE_STEP_DAYS, C.TUNE_SECONDS)
    C.TUNE_HALF_LIVES, C.TUNE_STEP_DAYS, C.TUNE_SECONDS = (60, 110, 330), 14, 600
    try:
        res = tune.run(divs=["E0", "BRA", "USA"], loader=lambda d: {"E0": ms, "BRA": cal, "USA": short}[d], seconds=600)
    finally:
        C.TUNE_HALF_LIVES, C.TUNE_STEP_DAYS, C.TUNE_SECONDS = old
    assert set(res["done"]) == {"E0", "BRA"} and "USA" in res["skipped"] and "trop court" in res["skipped"]["USA"], (res["done"], res["skipped"])
    rep = res["result"]["report"]
    assert set(res["result"]["half_life"]) == {"E0", "BRA"}
    # garde-fou anti-surapprentissage conservé : la demi-vie se choisit sur la 1re moitié de la fenêtre de test, le poids des buts se mesure sur la 2e
    for d in ("E0", "BRA"):
        r = rep[d]
        assert r["n_hl"] < r["n"] and r["chosen"] in (60, 110, 330) and r["gw"]["n1"] > 0, r
    print("ok  tune : E0 (3 saisons) et BRA (800 jours) réglées, USA (", (date.fromisoformat(short[-1]["date"]) - date.fromisoformat(short[0]["date"])).days,
          "j) toujours ignorée ; demi-vie sur la 1re moitié, poids sur la 2e")


# ---------- 4. plus de double rapprochement, xG ----------

def test_single_shrink_and_xg():
    assert C.FAIR_SHRINK == 1.0 and fair.shrink(0.83) == 0.83 and fair.shrink(0.3) == 0.3
    old = C.FAIR_SHRINK
    C.FAIR_SHRINK = 0.9
    try:
        assert close(fair.shrink(0.8), 0.77)                         # réglage d'environnement : l'ancien rapprochement reste possible
    finally:
        C.FAIR_SHRINK = old
    # alternatives / best_per_family : probas brutes (la calibration mesurée est le seul rapprochement)
    opts = [("totals", "Over", 1.5, 0.78, "t"), ("totals", "Over", 2.5, 0.52, "t")]
    assert fair.alternatives(opts, {}, "totals") == [["Over", 1.5, 0.78, None]]
    # xG dans le chemin principal : poids selon la source retenue par features (xg_src)
    model = S.build(MS, date(2025, 5, 1), div="E0", mkt=0.5)
    pred = S.predict(model, "Arsenal", "Chelsea", date(2025, 5, 1))
    f = {"deep": pred, "n_home": 12, "n_away": 12}
    assert prematch.xg_weight(f) == 0.0 and fair.with_xg(f, pred)[0] is pred
    fx = f | {"exp_xg_home": 3.0, "exp_xg_away": 0.6, "xg_src": "understat"}
    ft = f | {"exp_xg_home": 3.0, "exp_xg_away": 0.6, "xg_src": "fotmob-classement"}
    assert prematch.xg_weight(fx) == C.GOAL_XG_WEIGHT == 0.3 and prematch.xg_weight(ft) == C.GOAL_XG_WEIGHT_TABLE == 0.15
    d2, lh, la, tag = fair.with_xg(fx, pred)
    assert close(lh, 0.7 * pred["lh"] + 0.3 * 3.0) and close(la, 0.7 * pred["la"] + 0.3 * 0.6) and tag == " + xG" and d2["lh"] == lh and pred["lh"] != lh
    base_opts = {(o[0], o[1], o[2]): o for o in fair.deep_options(f, "Arsenal", "Chelsea")}
    xg_opts = {(o[0], o[1], o[2]): o for o in fair.deep_options(fx, "Arsenal", "Chelsea")}
    tb_opts = {(o[0], o[1], o[2]): o for o in fair.deep_options(ft, "Arsenal", "Chelsea")}
    k = ("h2h", "Arsenal", None)
    assert k in base_opts and xg_opts[k][3] > tb_opts[k][3] > base_opts[k][3] if pred["lh"] < 3.0 else True
    assert xg_opts[("totals", "Over", 2.5)][3] != base_opts[("totals", "Over", 2.5)][3] and "xG" in xg_opts[k][4] and "xG" not in base_opts[k][4]
    assert prematch.goal_means(fx)[2] == "modèle Kelly + xG" and close(prematch.goal_means(fx)[0], lh) and close(prematch.goal_means(ft)[0], 0.85 * pred["lh"] + 0.15 * 3.0)
    print("ok  un seul rapprochement (FAIR_SHRINK = 1), xG mêlés au modèle de force des pronostics (30 % ; 15 % pour le classement FotMob)")


# ---------- 5. loi des buts ----------

def test_goal_distribution():
    lh, la = 1.5, 1.1
    # kappa = 0 : deux Poisson indépendants (comme avant)
    M0 = S.score_matrix(lh, la, 0.0)
    ph, pa = S.pois_pmf(lh, 12), S.pois_pmf(la, 12)
    s = sum(ph[i] * pa[j] for i in range(13) for j in range(13))
    assert close(M0[2][1], ph[2] * pa[1] / s, 1e-12)
    K = 0.12
    M = S.score_matrix(lh, la, 0.0, cap=30, kappa=K)
    assert close(sum(map(sum, M)), 1.0, 1e-9)
    mu = lh + la
    tot = S.goal_pmf(M)
    mean = sum(i * p for i, p in enumerate(tot))
    var = sum((i - mean) ** 2 * p for i, p in enumerate(tot))
    assert close(mean, mu, 2e-3) and close(var, mu + K * mu * mu, 0.02), (mean, var, mu + K * mu * mu)             # variance du total : mu + kappa mu^2
    home = S.goal_pmf(M, 0)
    mh = sum(i * p for i, p in enumerate(home))
    vh = sum((i - mh) ** 2 * p for i, p in enumerate(home))
    assert close(mh, lh, 2e-3) and close(vh, lh + K * lh * lh, 0.02)
    cov = sum(i * j * M[i][j] for i in range(31) for j in range(31)) - mh * sum(j * p for j, p in enumerate(S.goal_pmf(M, 1)))
    assert close(cov, K * lh * la, 0.01), cov                                                                         # covariance kappa lh la
    # totaux : les queues sont plus épaisses (plus de 4,5 plus probable, plus de 2,5 un peu moins sûr)
    P0, P1 = S.goal_pmf(S.score_matrix(lh, la, 0.0, cap=20)), S.goal_pmf(M)
    assert S.p_over(P1, 4.5) > S.p_over(P0, 4.5) + 0.005 and S.p_over(P1, 0.5) < S.p_over(P0, 0.5) + 1e-9
    assert prematch.p_totals(lh, la, 2.5, "Over", 0.0) < prematch.p_totals(lh, la, 2.5, "Over", K) + 0.05 and close(prematch.p_totals(lh, la, 2.5, "Over", 0.0), 1 - sum(S.pois_pmf(mu, 14)[:3]), 1e-9)
    # les 2 marquent : binomiale négative bivariée et rho (repli) ; le repli n'ignore plus rho
    y0 = prematch.p_btts(lh, la, "Yes", 0.0, 0.0)
    assert close(y0, (1 - math.exp(-lh)) * (1 - math.exp(-la)), 1e-5)
    assert abs(prematch.p_btts(lh, la, "Yes", 0.0, K) - y0) > 0.003
    yr = prematch.p_btts(lh, la, "Yes", -0.1, 0.0)
    assert yr != y0 and close(yr + prematch.p_btts(lh, la, "No", -0.1, 0.0), 1.0)
    assert prematch.p_btts(lh, la, "Yes", 0.1, 0.0) < y0 < yr or prematch.p_btts(lh, la, "Yes", 0.1, 0.0) > y0 > yr          # rho déplace la proba dans un sens, -rho dans l'autre
    ph_, pd_, pa_ = prematch.p_1x2(lh, la, rho=-0.08, kappa=0.0)
    assert close(ph_ + pd_ + pa_, 1.0) and pd_ > S.result_probs(S.score_matrix(lh, la, 0.0))[1]
    # repli de fair.py : kappa du modèle de force s'il existe, sinon le défaut ; rho du modèle
    f = {"exp_goals_home": lh, "exp_goals_away": la, "n_home": 12, "n_away": 12}
    got = {(o[0], o[1], o[2]): o[3] for o in fair._simple_options(f, "A", "B")}
    assert close(got[("totals", "Over", 2.5)], prematch.p_totals(lh, la, 2.5, "Over", C.GOALS_KAPPA_DEFAULT))
    assert close(got[("btts", "Yes", None)], prematch.p_btts(lh, la, "Yes", 0.0, C.GOALS_KAPPA_DEFAULT))
    fd_ = f | {"deep": {"kappa": 0.09, "rho": -0.07, "lh": None}}
    got2 = {(o[0], o[1], o[2]): o[3] for o in fair._simple_options(fd_, "A", "B")}
    assert close(got2[("btts", "Yes", None)], prematch.p_btts(lh, la, "Yes", -0.07, 0.09)) and close(got2[("totals", "Over", 2.5)], prematch.p_totals(lh, la, 2.5, "Over", 0.09))
    # estimation : données à intensité commune (gamma, variance 0,15) -> kappa estimé net, jamais sur des Poisson purs
    rng = random.Random(3)
    teams = [f"T{i}" for i in range(20)]
    gm = []
    for se in range(3):
        for k, day in enumerate(synth.rounds(teams)):
            d = date(2022, 8, 6) + timedelta(days=365 * se + 7 * k)
            for h, a in day:
                g = rng.gammavariate(1 / 0.15, 0.15)
                hg, ag = synth.poisson(rng, 1.45 * g), synth.poisson(rng, 1.15 * g)
                gm.append({"date": d.isoformat(), "div": "E0", "home": h, "away": a, "hg": hg, "ag": ag, "ftr": "H" if hg > ag else "A" if hg < ag else "D", "odds": {}})
    mod = S.build(gm, date(2025, 5, 1), div="E0")
    pure = S.build(MS, date(2025, 5, 1), div="E0", mkt=0.0)
    assert 0.07 < mod["stats"]["goals"]["atot"] < 0.3 and pure["stats"]["goals"]["atot"] < 0.05, (mod["stats"]["goals"]["atot"], pure["stats"]["goals"]["atot"])
    pr = S.predict(mod, "T1", "T2", date(2025, 5, 1))
    assert pr["kappa"] == S.kappa_of(mod["stats"]["goals"]) > 0.07 and S.predict(pure, "Arsenal", "Chelsea", date(2025, 5, 1))["kappa"] < 0.05
    # la loi ajustée décrit mieux les totaux observés que le Poisson (log-vraisemblance)
    obs = [m["hg"] + m["ag"] for m in gm if m["date"] >= "2023-06-01"]
    mean = sum(obs) / len(obs)
    nb, po = S.nb_pmf(mean, mod["stats"]["goals"]["atot"], cap=30), S.pois_pmf(mean, 30)
    assert sum(math.log(nb[x]) for x in obs) > sum(math.log(po[x]) for x in obs) + 20
    C.GOALS_KAPPA_SCALE, old = 0.0, C.GOALS_KAPPA_SCALE
    try:
        assert S.predict(mod, "T1", "T2", date(2025, 5, 1))["kappa"] == 0.0           # interrupteur : Poisson pur
    finally:
        C.GOALS_KAPPA_SCALE = old
    print(f"ok  binomiale négative bivariée (variance, covariance, queues), rho dans le repli des 2 marquent, kappa estimé {mod['stats']['goals']['atot']:.3f} sur données à intensité commune")


# ---------- reste des vagues A et C ----------

def test_market_weights_and_referee():
    for m, w in (("team_corners_home", 1.5), ("team_corners_away", 1.5), ("team_yellow_total", 1.6)):
        assert C.MARKET_WEIGHT[m] == w and fair.weight(m) == w
    assert not hasattr(fair, "EXTRA_WEIGHT") and fair.weight("totals") == C.MARKET_WEIGHT["totals"] and fair.weight("inconnu") == 1.0
    # arbitre : liste du jour d'API-Football en cache -> /analyse et /match (même fonction)
    day = "2026-10-10"
    ko = int(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc).timestamp())
    lst = [{"id": 1, "ts": ko, "home": "Arsenal", "away": "Chelsea", "ref": "Michael Oliver", "status": "NS"},
           {"id": 2, "ts": ko, "home": "Everton", "away": "Fulham", "status": "NS"}]
    store.save("fixtures_day.json", {day: lst, "_fetched": {day: 1.0}})
    c = prono(market="totals", point=2.5, home="Arsenal", away="Chelsea", commence="2026-10-10T15:00:00Z") | {"event": "Arsenal - Chelsea", "start": "15:00"}
    assert app.referee_of(c) == "Michael Oliver" and app.referee_of(c | {"home": "Everton", "away": "Fulham"}) is None
    assert app.referee_of(c | {"sport_key": "basketball_nba"}) is None and app.referee_of({}) is None
    assert app.referee_line("Michael Oliver") == "👨‍⚖️ Arbitre désigné : Michael Oliver" and app.referee_line("Michael Oliver", "Arbitre M Oliver : 4.1 cartons") == ""
    assert app.referee_line(None, "x") == ""
    txt = str(app.analyse([], "Arsenal", pronos=[c]))
    assert "Arbitre désigné : Michael Oliver" in txt, txt
    c2 = c | {"home": "Everton", "away": "Fulham", "event": "Everton - Fulham"}
    assert "Arbitre désigné" not in str(app.analyse([], "Everton", pronos=[c2]))
    print("ok  poids de marché des corners d'une équipe / cartons jaunes (config) ; arbitre désigné affiché dans /analyse et /match")


def test_player_settlement_by_id():
    from kelly import players

    class Resp:
        status_code = 200
        headers = {}

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

        def raise_for_status(self):
            pass

    def block(pid, name, minutes, shots, on, fouls):
        return {"player": {"id": pid, "name": name}, "statistics": [{"games": {"minutes": minutes}, "shots": {"total": shots, "on": on}, "fouls": {"committed": fouls}}]}

    def stat_block(tid, corners):
        return {"team": {"id": tid}, "statistics": [{"type": "Corner Kicks", "value": corners}, {"type": "Total Shots", "value": 9}]}

    def fake(url, **kw):
        if url.endswith("/fixtures/statistics"):
            return Resp({"errors": [], "response": [stat_block(10, 5), stat_block(20, 3)]})
        return Resp({"errors": [], "response": [
            {"team": {"id": 10}, "players": [block(301, "Carlos Gomez", 90, 3, 1, 2), block(101, "K. Mbappé", 90, 4, 2, 1), block(500, "A. Benched", None, None, None, None)]},
            {"team": {"id": 20}, "players": [block(302, "Carlos Gomez", 90, 0, 0, 0), block(202, "B. Silva", 80, 1, 0, 3)]}]})
    stats._today = lambda: "2026-10-10"
    os.environ["STATS_API_KEY"] = "test-key"
    stats._calls, stats._halted = 0, False
    stats._neg.clear()
    orig = stats.requests.get
    stats.requests.get = lambda url, **kw: fake(url, **kw)
    try:
        st = stats.fixture_stats(9001)
    finally:
        stats.requests.get = orig
        os.environ.pop("STATS_API_KEY", None)
    pl = st["players"]
    assert set(pl) == {"301", "302", "101", "202"}, pl                                  # clés = identifiants : les deux Carlos Gomez ne s'écrasent plus
    assert pl["301"] == {"name": "Carlos Gomez", "team_id": 10, "shots": 3.0, "shots_on_target": 1.0, "fouls": 2.0} and pl["302"]["team_id"] == 20
    assert players.stat(st, "Carlos Gomez", "shots") is None, "homonymes sans équipe : ambigu, jamais réglé au hasard"
    assert players.stat(st, "Carlos Gomez", "shots", team=10) == 3.0 and players.stat(st, "Carlos Gomez", "shots", team=20) == 0.0
    assert players.stat(st, "Mbappe", "shots_on_target") == 2.0 and players.stat(st, "Benched", "shots") is None
    b = {"market": "player_shots", "name": "Over", "point": 2.5, "who": "Carlos Gomez", "home": "A", "away": "B", "id": "x", "commence": "2026-10-10T15:00:00Z"}
    ev = {"id": "x", "completed": True, "scores": [{"name": "A", "score": "1"}, {"name": "B", "score": "0"}]}
    assert settle.settle_pick(b, ev, st) is None                                           # équipe inconnue + homonymes : non réglé
    assert settle.settle_pick(b | {"team_id": 10}, ev, st) == "win" and settle.settle_pick(b | {"team_id": 20}, ev, st) == "loss"
    assert settle.final_text(b | {"team_id": 10}, ev, st) == "3 tirs" and settle.final_text(b, ev, st) == ""
    mb = b | {"who": "Kylian Mbappe", "point": 3.5}
    assert settle.settle_pick(mb, ev, st) == "win"                                         # nom unique : pas besoin d'équipe
    legacy = {"players": {"K. Mbappé": {"shots": 4.0}, "B. Silva": {"shots": 1.0}}}         # ancien cache {nom: stats} toujours lu
    assert settle.settle_pick(mb, ev, legacy) == "win" and stats.player_stat(st, "Mbappe", "shots") == 4.0
    print("ok  joueurs : stats par identifiant avec équipe, règlement filtré par équipe, homonymes jamais confondus, ancien format lu")


def main():
    test_sharp_books_and_probable()
    test_probable_flow_display_and_stakes()
    test_line_choice_by_ev_at_1xbet_price()
    test_implied_goals_from_closing_odds()
    test_market_anchoring_backtest()
    test_tune_runs_and_retention()
    test_single_shrink_and_xg()
    test_goal_distribution()
    test_market_weights_and_referee()
    test_player_settlement_by_id()
    print("tests marchés de buts : OK")


if __name__ == "__main__":
    main()
