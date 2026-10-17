"""Statistiques des COUPES dans la voie `stats` (kelly/cupstats.py), hors ligne : jambes de stats calculées avec les profils de championnat de chaque équipe, cup = true, FANTÔME par défaut
(STATS_CUPS_MODE), journal et règlement À PART, rien d'appris, ligne « stats (coupes) » du bilan, règle de retour. Lancer : python -m tests.test_cup_stats"""
import os
import tempfile
from datetime import timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-cup-stats-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-cup-stats-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from tests.test_season import league  # noqa: E402
from tests.test_season_cup import rename  # noqa: E402
from tests import test_season_lane as T  # noqa: E402
from kelly import app, calib, combos as CB, config as C, cupstats, ensemble, fair, lanes, prematch, lanestats as L, season, seasonlane as SL, settle, store, xprice  # noqa: E402

CUP = "soccer_uefa_champs_league"
NOW = T.NOW
UNTOUCHED = ("ensemble_log.json", "ensemble.json", "calib.json", "means.json", "fair.json", "tracked.json", "strategy_state.json")


def world():
    T.setup()
    C.STATS_CUPS_MODE = "ghost"
    obs = {"soccer_x": rename(league(10, 5, seed=1, mean_h=1.9, mean_a=1.5, corners=(6.5, 5.5), disp=0.1), "Xe"),
           "soccer_y": rename(league(10, 5, seed=2, mean_h=1.2, mean_a=0.9, corners=(4.5, 3.5), disp=0.1), "Yo")}
    season.collect = lambda sk, on, loaders=None, docs=None: (obs.get(sk, []), "football-data" if sk in obs else None)
    SL.known_leagues = lambda _filtered=True: ["soccer_x", "soccer_y"]
    season.reset()
    for name in UNTOUCHED + (cupstats.LOG,):
        store.remove(name)
    started = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, h, a, t=T.KO: {"id": f"c{i}", "sport_key": CUP, "home_team": h, "away_team": a, "commence_time": t, "league": "LDC"}      # noqa: E731
    return {"leagues": {CUP: [ev(1, "XeT1", "YoT3"), ev(2, "YoT2", "XeT5"), ev(3, "XeT2", "XeT6", started)], "soccer_x": [{"id": "l1", "sport_key": "soccer_x", "home_team": "XeT1", "away_team": "XeT4",
                                                                                                                       "commence_time": T.KO}]}}


def test_make():
    scan = world()
    pronos = cupstats.make(scan, NOW)
    assert pronos and all(c["cup"] is True and c["id"] in ("c1", "c2") and c["market"] in cupstats.stat_markets() and c["alts"] for c in pronos), pronos[:1]
    assert not any(c["market"] in ("totals", "btts", "h2h", "double_chance") or c["market"].startswith("team_totals") for c in pronos), "stats seulement : buts / 1N2 inchangés"
    assert {c["id"] for c in pronos} == {"c1", "c2"}, "match commencé et championnat : jamais"
    assert all(c["src"] == "coupe" and "strategy" not in c for c in pronos)
    assert all(isinstance(c["mu"], float) and c["mu"] > 0 for c in pronos), "le nombre attendu (mu) est enregistré comme pour les autres lignes : le règlement écrira le nombre réel (val)"
    pred = season.cup_predict(SL.cup_models(NOW.date()), "XeT1", "YoT3")
    c1 = next(c for c in pronos if c["id"] == "c1" and c["market"] in (prematch.CORNERS, prematch.CARDS))
    st = pred["stats"][SL._stat_of(c1["market"])]
    assert abs(c1["mu"] - round(st["h"] + st["a"], 3)) < 1e-9, "total du match = domicile + extérieur"
    row = dict(pronos[0], date="2026-10-15", result="win", final="9 corners", val=9.0, key="k1")
    back = ensemble.unpack_archive_row("k1", "2026-10-15", ensemble._pack(row))
    assert (back["mu"], back["val"], back["final"]) == (pronos[0]["mu"], 9.0, "9 corners"), "l'archive garde mu, val et le score final"
    assert ensemble.unpack_archive_row("k2", "2026-10-15", ensemble._pack(row)[:22]).get("mu") is None, "une ancienne ligne d'archive reste lisible"
    C.STATS_CUPS_MODE = "off"
    try:
        assert cupstats.make(scan, NOW) == []
    finally:
        C.STATS_CUPS_MODE = "ghost"
    print("ok  jambes de stats de coupe : profils de championnat, cup = true, stats seulement (buts inchangés), match commencé écarté, voie « off » : rien")
    return pronos


def test_lanes_and_combos(pronos):
    c = dict(pronos[0], strategy="stats")
    assert not lanes.usable(c) and lanes.priced(c), "ghost : prix lus, jamais choisie"
    C.STATS_CUPS_MODE = "on"
    try:
        assert lanes.usable(c) and not CB.cross_league_stat(c), "on : jouable, plus écartée comme stat de coupe"
        C.STRATEGY_STATS = "ghost"
        assert not lanes.usable(c), "voie stats en fantôme : les coupes aussi"
        C.STRATEGY_STATS = "on"
        assert lanes.usable(dict(c, strategy="fill"))
        C.FILL_LEGS = False
        assert not lanes.usable(dict(c, strategy="fill")) and lanes.usable(c), "FILL_LEGS coupe le remplissage, pas la valeur"
    finally:
        C.STATS_CUPS_MODE, C.STRATEGY_STATS, C.FILL_LEGS = "ghost", "on", True
    C.STATS_CUPS_MODE = "off"
    try:
        assert not lanes.priced(c)
    finally:
        C.STATS_CUPS_MODE = "ghost"
    plain = {k: v for k, v in c.items() if k != "cup"}
    assert CB.cross_league_stat(plain), "un pari de stats de coupe SANS drapeau (pari réel d'un autre book) reste écarté"
    print("ok  interrupteur : ghost = prix lus et jamais choisie, on = jouable (voie stats « on »), off = rien lu ; un pari réel de stats de coupe reste écarté")


def test_pricing_and_journal(pronos):
    px_of = lambda c: {k: v for name, pt, *_ in c["alts"] for k, v in (((c["market"], "Over", pt), 2.1), ((c["market"], "Under", pt), 1.75))}      # noqa: E731

    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        out = {}
        for k, ev in events:
            px = {}
            for c in pronos:
                if c["id"] == ev["id"]:
                    px |= px_of(c)
            out[ev["id"]] = {"gid": 1, "px": px, "refs": {k_: [1, 2, 3] for k_ in px}}
        return out
    res = xprice.apply(pronos, fetch=fetch)
    priced = [c for c in pronos if c.get("xodds")]
    assert priced and all(c["strategy"] in ("stats", "fill") and c["cup"] for c in priced), res
    # le routage du journal : les lignes de coupe vont à cup_log.json, jamais à ensemble_log.json
    other = {"key": "l1|totals||2.5|Over", "id": "l1", "sport_key": "soccer_x", "home": "A", "away": "B", "commence": T.KO, "market": "totals", "name": "Over", "point": 2.5, "prob": 0.6,
             "p_model": 0.6, "p_x": 0.55, "xodds": 1.9, "strategy": "fill"}
    cup_rows, rest = cupstats.split(priced + [other])
    assert len(cup_rows) == len(priced) and rest == [other]
    n = app.log_opinions(priced + [other], T.DAY, NOW)
    cl = store.load(cupstats.LOG, [])
    assert cl and all(r.get("cup") and r["id"] in ("c1", "c2") for r in cl), cl[:1]
    el = store.load(ensemble.LOG_FILE, [])
    assert [r["key"] for r in el] == ["l1|totals||2.5|Over"] and not any(r.get("cup") for r in el), "journal d'ensemble : seulement l'autre ligne"
    for name in ("fair.json", "ensemble.json", "calib.json", "means.json"):
        assert store.load(name, None) in (None, [], {}), name
    print("ok  prix 1xBet et journal : étiquette stats / fill avec cup, lignes de coupe dans cup_log.json, rien dans le journal d'ensemble ni fair.json")
    return cl


def test_settle_no_learning_and_report(cl):
    day = str(T.DAY - timedelta(days=1))
    base = dict(cl[0])
    rows = [dict(base, key=f"c{i}|alternate_totals_corners||9.5|Over", id=f"c{i}", date=day, result=None, strategy="stats", pm=0.6, xodds=2.0) for i in range(8)]
    store.save(cupstats.LOG, rows)

    def settle_one(r):
        r["result"] = "win" if int(r["id"][1:]) % 3 else "loss"
        r["xclose"] = {"odds": 1.8 if int(r["id"][1:]) % 2 else 2.2, "lead_min": 60, "susp": False}
        return True
    got, learned = ensemble.settle_log(settle_one, name=cupstats.LOG, learner=lambda log: 0, arch_prefix=cupstats.ARCH)
    assert len(got) == 8 and learned == 0 and store.names(cupstats.ARCH) and not store.names(ensemble.ARCH_PREFIX)
    for name in ("ensemble.json", "calib.json", "means.json", "fair.json"):
        assert store.load(name, None) in (None, [], {}), f"{name} : rien d'appris"
    L._memo["key"] = None
    C.STATS_CUPS_BACK_N = 200
    line = L.cup_line()
    assert line.startswith("stats (coupes, ghost) : 5 gagnées sur 8") and "CLV 1xBet" in line and "sur 8/200 lignes" in line and "règle de retour" not in line, line
    C.STATS_CUPS_BACK_N = 4
    try:
        rows2 = store.load(cupstats.LOG, [])
        for r in rows2:
            r["xclose"] = {"odds": 1.8, "lead_min": 60, "susp": False}
        store.save(cupstats.LOG, rows2)
        L._memo["key"] = None
        assert "✅ règle de retour atteinte : à la main, STATS_CUPS_MODE=on" in L.cup_line(), L.cup_line()
        C.STATS_CUPS_MODE = "on"
        assert "règle de retour" not in L.cup_line()
    finally:
        C.STATS_CUPS_BACK_N, C.STATS_CUPS_MODE = 200, "ghost"
    # étanchéité : les objectifs, la coupure et les lignes des autres voies ne voient pas les lignes de coupe
    assert not any("coupes" in x for x in L.objective_lines()) and L.cut_lines() == [] and L.lane_rows("stats") == []
    assert any(x.startswith("stats (coupes, ghost)") for x in L.lane_lines([]))
    print("ok  règlement à part sans apprentissage ; ligne « stats (coupes) » (réalisé, CLV ± intervalle, n/200) ; règle de retour dite à ≥ 0 sur 200 lignes ; objectifs et coupure ne les voient pas")


def test_other_guards():
    # un pari réel de coupe : le pronostic de stats de coupe ne lui donne JAMAIS un « avis du modèle »
    scan = world()
    pronos = cupstats.make(scan, NOW)
    pick = {"key": "c1|alternate_totals_corners|", "id": "c1", "market": "alternate_totals_corners", "name": "Over", "point": pronos[0]["alts"][0][1], "home": "XeT1", "away": "YoT3"}
    pick["key"] = f'c1|alternate_totals_corners||{pick["point"]}|Over'
    ensemble.attach(pronos, [pick])
    assert "p_model" not in pick, pick
    # la prudence par jambe des autres combinés ne voit pas les jambes de coupe
    legs = [{"key": "a", "result": "win", "prob": 0.7, "cup": True}, {"key": "b", "result": "win", "prob": 0.7}]
    assert [l["key"] for l in settle._combo_legs([{"date": "2026-10-14", "tier": 5, "legs": legs}])] == ["b"]
    print("ok  gardes : pas d'avis du modèle sur un pari réel de coupe, pas de prudence par jambe apprise des jambes de coupe")


def test_no_duplicate_cup_stats_elsewhere():
    """fair.make donnait déjà (moyennes football-data sans correction de niveau) des stats aux matchs soccer_uefa_* : ces lignes, sans drapeau cup, auraient la MÊME clé que celles de cupstats et
    entreraient dans le journal d'ensemble, fair.json et l'apprentissage. Désormais elles ne sont plus faites tant que les stats de coupe ne sont pas « off »."""
    scan = world()
    real = fair._options
    fair._options = lambda f, h, a: ([("alternate_totals_corners", "Over", 9.5, 0.6, "x"), ("team_shots_total", "Over", 24.5, 0.6, "x"), ("totals", "Over", 2.5, 0.62, "x")], {})
    real_mn = fair.odds.match_numbers
    fair.odds.match_numbers = lambda h, a, k, **kw: {"n_home": 20, "n_away": 20}
    try:
        both = {"leagues": {CUP: scan["leagues"][CUP][:1], "soccer_x": scan["leagues"]["soccer_x"]}}
        out = fair.make(both, (), None, all_families=True)
        by_league = {(c["sport_key"], c["market"]) for c in out}
        assert (CUP, "totals") in by_league and (CUP, "alternate_totals_corners") not in by_league and (CUP, "team_shots_total") not in by_league, by_league
        assert ("soccer_x", "alternate_totals_corners") in by_league, "un championnat garde ses stats"
        C.STATS_CUPS_MODE = "off"
        assert (CUP, "alternate_totals_corners") in {(c["sport_key"], c["market"]) for c in fair.make(both, (), None, all_families=True)}, "voie « off » : comme avant"
    finally:
        fair._options, fair.odds.match_numbers = real, real_mn
        C.STATS_CUPS_MODE = "ghost"
    # fair.record ne garde jamais une ligne de coupe (calibration) ; keep_pronos garde les lignes de coupe pour la relecture
    pronos = cupstats.make(scan, NOW)
    store.remove("fair.json")
    assert fair.record(pronos, T.DAY, shown=pronos) == 0 and store.load("fair.json", None) in (None, [], {})
    kept = app.keep_pronos(pronos)
    assert {c["key"] for c in pronos} <= {c["key"] for c in kept}, "lignes de coupe relues à 15:00 et par la fenêtre des stats"
    c = dict(pronos[0], xodds=1.5, min_odds=1.9, prob=0.7, strategy="stats")
    assert not lanes.usable(c), "ghost : pas de « probable mais mal payé » affiché"
    ev_src = open(os.path.join(os.path.dirname(app.__file__), "app.py"), encoding="utf-8").read()
    assert "new = new + cupstats.make(scan, now)" in ev_src, "le relevé du soir refait aussi les stats de coupe"
    print("ok  pas de doublon : fair.make ne fait plus les stats d'un match de coupe (clés identiques, fuite d'apprentissage), fair.record ignore la coupe, keep_pronos la garde")


def main():
    real = (season.collect, SL.known_leagues)
    try:
        pronos = test_make()
        test_lanes_and_combos(pronos)
        cl = test_pricing_and_journal(pronos)
        test_settle_no_learning_and_report(cl)
        test_other_guards()
        test_no_duplicate_cup_stats_elsewhere()
    finally:
        season.collect, SL.known_leagues = real
        C.STATS_CUPS_MODE = "ghost"
    print("tests stats de coupe : OK")


if __name__ == "__main__":
    main()
