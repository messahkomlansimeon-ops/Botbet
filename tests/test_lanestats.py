"""Mesures par voie (hors ligne) : CLV 1xBet d'une ligne (relevé ≤ 90 min, pas de pari retiré), marges par match, verdict de coupure / de retour (n minimal, intervalle à 95 %),
objectifs (CLV 1xBet ; capital ≥ départ, plus aucun montant en FCFA), lignes du bilan par voie (jambes, jamais d'argent réparti entre voies, combinés par has_value), la voie
sharpgap n'apprend rien, la ligue dans l'archive. Lancer : python -m tests.test_lanestats"""
import math
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-lanestats-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-lanestats-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "ENSEMBLE_FROZEN_MODEL"):
    os.environ.pop(k, None)

from kelly import ensemble, lanestats as L, learn, money, settle, store  # noqa: E402

TODAY = str(date.today())
WED = "2026-10-14"
M1, M2, M3 = "2026-10-12", "2026-10-19", "2026-10-26"                    # trois lundis consécutifs


def decide():
    """La coupure n'est prononcée qu'au 2e lundi consécutif : le 1er ne décide rien."""
    first = L.apply(M1)
    assert first == [], first
    return L.apply(M2)


def row(i, lane="stats", odds=2.0, close=2.0, lead=60, res="win", **kw):
    return {"key": f"k{i}", "id": f"m{i}", "date": TODAY, "market": "alternate_totals_corners", "sport_key": "soccer_epl", "strategy": lane, "result": res, "xodds": odds,
            "xclose": {"odds": close, "lead_min": lead, "susp": False}} | kw


def test_clv():
    assert math.isclose(L.x_clv(row(1, odds=2.2, close=2.0)), math.log(1.1))
    assert L.x_clv(row(1, lead=91)) is None and L.x_clv(row(1, lead=90)) is not None, "au-delà de 90 min ce n'est pas une clôture"
    assert L.x_clv(row(1) | {"xgone": {"lead_min": 40}}) is None, "pari retiré par 1xBet : pas de clôture"
    assert L.x_clv(row(1) | {"xclose": None}) is None and L.x_clv(row(1) | {"xodds": None}) is None
    # marge par match : 7 lignes d'un même match ne comptent pas comme 7 preuves
    same = [row(i, odds=2.2, id="M") for i in range(7)] + [row(100 + i, odds=1.8, id=f"o{i}") for i in range(7)]
    n, m, h = L.stats([L.x_clv(r) for r in same], [r["id"] for r in same])
    assert n == 14 and h is not None and h > L.stats([L.x_clv(r) for r in same])[2]
    assert L.stats([]) == (0, None, None) and L.stats([0.1])[2] is None
    # verdict : n minimal ET intervalle excluant zéro
    assert L.verdict(299, -0.05, 0.01, 300) is None and L.verdict(300, -0.05, 0.01, 300) == "neg"
    assert L.verdict(300, -0.05, 0.06, 300) is None and L.verdict(300, 0.05, 0.01, 300) == "pos" and L.verdict(300, 0.05, None, 300) is None
    print("ok  CLV 1xBet : relevé ≤ 90 min, pari retiré exclu, marge par match, verdict à n minimal avec intervalle")


def test_lines_and_objectives():
    money.set_daily(0)
    store.save(ensemble.LOG_FILE, [row(i, odds=2.2) for i in range(3)] + [row(10, "sharpgap", odds=1.8, market="totals"), row(11, "fill", res="loss")])
    L._memo["key"] = None
    out = L.objective_lines()
    assert out[0].startswith("🎯 CLV 1xBet ") and "(4/300 lignes)" in out[0], out
    assert not any("FCFA" in x and "objectif" in x.lower() for x in out)
    tracked = [dict(row(i), sent=True) for i in range(3)] + [dict(row(10, "sharpgap", res="loss"), sent=True), dict(row(12, None, res="win"), sent=True, strategy=None)]
    lines = L.lane_lines(tracked)
    assert any(x.startswith("sharpgap : 0 gagnée sur 1 (0 %)") for x in lines) and any(x.startswith("stats : 3 gagnées sur 3 (100 %) · CLV 1xBet") for x in lines), lines
    assert any(x.startswith("avant les étiquettes : 1 gagnée sur 1") for x in lines) and not any(x.startswith("fill") for x in lines), lines
    # un combiné : jambes comptées par voie, argent par has_value (jamais réparti)
    store.save("fair_combos.json", [
        {"id": "c1", "key": "c1", "date": TODAY, "tier": "x", "result": "win", "has_value": False, "odds": 4.0, "played": {"stake": 100, "odds": 4.0},
         "legs": [{"key": "z1", "strategy": "fill", "result": "win", "min_odds": 2.0}, {"key": "z2", "strategy": "fill", "result": "win", "min_odds": 2.0}]},
        {"id": "c2", "key": "c2", "date": TODAY, "tier": "x", "result": "loss", "has_value": True, "odds": 3.0, "played": {"stake": 200, "odds": 3.0},
         "legs": [{"key": "z3", "strategy": "sharpgap", "result": "win", "min_odds": 1.7}, {"key": "z4", "strategy": "stats", "result": "loss", "min_odds": 1.8}]}])
    cm = L.combo_money()
    assert cm["sans valeur"] == (300.0, 1, 100.0) and cm["avec valeur"] == (-200.0, 1, 200.0), cm
    lines = L.lane_lines(tracked)
    assert any(x.startswith("fill : 2 gagnées sur 2") for x in lines) and any(x.startswith("sharpgap : 1 gagnée sur 2") for x in lines), lines
    assert any(x.startswith("combinés sans valeur : +300 FCFA sur 1 noté") for x in lines) and any(x.startswith("combinés avec valeur : −200 FCFA sur 1 noté") for x in lines), lines
    print("ok  objectifs sans FCFA, lignes par voie (jambes, CLV), combinés ventilés par has_value")


def test_learning_and_archive():
    seen = []
    real = learn.train_clv
    learn.train_clv = lambda rows, prev, boot: seen.append([r.get("strategy") for r in rows]) or {}
    try:
        learn.update([{"strategy": "sharpgap"}, {"strategy": "stats"}, {"strategy": "fill"}, {"strategy": None}, {}])
    finally:
        learn.train_clv = real
    assert seen[-1] == ["stats", "fill", None, None], seen
    r = row(5, sport_key="soccer_brazil_campeonato")
    back = ensemble.unpack_archive_row(r["key"], TODAY, ensemble._pack(r))
    assert back["sport_key"] == "soccer_brazil_campeonato" and back["strategy"] == "stats"
    old = ensemble.unpack_archive_row("k", TODAY, ensemble._pack(r)[:-4])        # sans sport_key, sval, ref ni pe (colonnes ajoutées à la fin)
    assert "sport_key" not in old, "une ligne archivée avant l'ajout de la colonne reste lisible"
    print("ok  sharpgap n'apprend rien (stats + fill + anciennes oui) ; la ligue est dans l'archive")


def test_bilan_blocks():
    store.save("fair_combos.json", [])
    store.save(ensemble.LOG_FILE, [row(i) for i in range(3)])
    L._memo["key"] = None
    tracked = [dict(row(i), sent=True, prob=0.6, odds=2.0, pick="x", home="A", away="B", name="Over", point=9.5, closed_at=None) for i in range(3)]
    for fn in (settle._bilan_simple,):
        out = str(fn(None, tracked, [r for r in tracked], []))
        assert "🏁 <b>Objectifs</b>" in out and "🎛️ <b>Par voie</b>" in out and "stats : 3 gagnées sur 3" in out and "CLV" not in out, out      # bilan simple : pas de jargon
    assert "CLV 1xBet" in str(settle._bilan_detail({"combos": []}, None, tracked, tracked, []))
    print("ok  le bilan affiche objectifs et voies")


def bad_rows(n, sk, lane="stats", day=TODAY, base=1.0, odds=1.8, close=2.0):
    """n lignes réglées de ligue `sk`, prises moins cher que la dernière cote (CLV négatif), chacune dans son match."""
    return [row(f"{sk}{i}", lane, odds=odds, close=close, id=f"{sk}-{i}", sport_key=sk, date=day, res="win" if i % 2 else "loss") for i in range(n)]


def test_cutoff():
    from kelly import lanes, config as C
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    # 299 lignes d'une ligue jouée au CLV nettement négatif (mais pas parfaitement constant : la marge existe) : pas assez pour couper la voie
    def noisy(n, sk, mean_odds=1.8, **kw):
        rs = bad_rows(n, sk, odds=mean_odds, **kw)
        for i, r in enumerate(rs):
            r["xodds"] = mean_odds * (1.02 if i % 2 else 0.98)
        return rs
    two = lambda n: noisy(n // 2 + n % 2, "soccer_epl") + noisy(n // 2, "soccer_spain_la_liga")           # deux ligues sous 200 lignes : seule la voie entière peut couper
    store.save(ensemble.LOG_FILE, two(299))
    L._memo["key"] = None
    assert L.evaluate()["global"][3] is None and decide() == [] and not lanes.state()["stats"].get("mode") and not lanes.state()["stats"].get("leagues")
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    store.save(ensemble.LOG_FILE, two(300))
    L._memo["key"] = None
    ev = L.evaluate()
    assert ev["global"][0] == 300 and ev["global"][3] == "neg" and all(v[3] is None for v in ev["leagues"].values()), ev
    before = dict(lanes.state())
    assert L.apply(WED) == [] and lanes.state() == before, "un mercredi : rien n'est évalué ni écrit"
    assert L.apply(M1) == [] and lanes.mode("stats", "soccer_epl") == "on", "1er lundi : constaté, pas prononcé"
    assert L.apply(M3) == [] and lanes.mode("stats", "soccer_epl") == "on", "lundis NON consécutifs (14 jours) : la chaîne est cassée"
    done = L.apply(M3 and "2026-11-02")
    assert done and done[0].startswith("voie stats coupée : CLV 1xBet −"), done
    assert lanes.state()["stats"]["mode"] == "ghost" and lanes.mode("stats", "soccer_epl") == "ghost" and not lanes.usable({"market": "alternate_totals_corners", "sport_key": "soccer_epl"})
    assert "deux lundis de suite" in lanes.state()["stats"]["reason"]
    assert L.apply("2026-11-09") == [], "déjà coupée : une seule décision"
    first = L.cut_lines()[0]
    assert first.startswith("🛑 Voie stats coupée depuis le ") and "voies stats on" in first, first
    # retour à la main seulement ; la mesure repart de ce jour (les anciennes lignes ne recoupent pas tout de suite)
    assert "remise à « on »" in lanes.command("stats on") and lanes.mode("stats", "soccer_epl") == "on"
    yesterday_rows = noisy(300, "soccer_epl", day="2000-01-01")
    store.save(ensemble.LOG_FILE, yesterday_rows)
    L._memo["key"] = None
    assert decide() == [] and lanes.mode("stats", "soccer_epl") == "on", "lignes d'avant la remise : ignorées"
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    print("ok  coupure de la voie stats : 300 lignes réglées avec intervalle sous zéro, une seule décision, retour à la main, mesure repartie de la remise")


def test_league_cut_and_return():
    from kelly import lanes
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    def noisy(n, sk, mean_odds, **kw):
        rs = bad_rows(n, sk, odds=mean_odds, **kw)
        for i, r in enumerate(rs):
            r["xodds"] = mean_odds * (1.02 if i % 2 else 0.98)
        return rs
    # une ligue jouée à 200 lignes au CLV négatif est coupée, la voie (200 < 300) ne l'est pas
    store.save(ensemble.LOG_FILE, noisy(200, "soccer_epl", 1.8))
    L._memo["key"] = None
    done = decide()
    assert done == [done[0]] and done[0].startswith("ligue soccer_epl en fantôme : CLV 1xBet −"), done
    st = lanes.state()["stats"]
    assert "mode" not in st and "soccer_epl" in st["leagues"] and lanes.mode("stats", "soccer_epl") == "ghost" and lanes.mode("stats", "soccer_spain_la_liga") == "on"
    assert any("soccer_epl en fantôme depuis le" in x and "voies soccer_epl on" in x for x in L.cut_lines())
    # retour PROPOSÉ (jamais appliqué) : CLV fantôme positif, intervalle au-dessus de zéro, 200 lignes réglées APRÈS la coupure
    store.save(ensemble.LOG_FILE, noisy(200, "soccer_epl", 2.4, day="2026-10-20"))        # lignes fantômes réglées APRÈS la coupure du 19
    L._memo["key"] = None
    assert not [x for x in L.cut_lines() if x.startswith("✅")], "rien de proposé avant la confirmation de deux lundis"
    assert L.apply(M1) == []
    assert not [x for x in L.cut_lines() if x.startswith("✅")], "un seul lundi : pas encore proposé"
    assert L.apply(M2) == []
    prop = [x for x in L.cut_lines() if x.startswith("✅ soccer_epl")]
    assert prop and "Retour proposé, à faire à la main" in prop[0] and "voies soccer_epl on" in prop[0], L.cut_lines()
    assert lanes.mode("stats", "soccer_epl") == "ghost", "proposé, pas appliqué"
    assert L.apply(M3) == [] and lanes.mode("stats", "soccer_epl") == "ghost"
    store.save(ensemble.LOG_FILE, noisy(199, "soccer_epl", 2.4, day="2026-10-20"))
    L._memo["key"] = None
    assert L.apply("2026-11-02") == [] and not lanes.state()["stats"]["back_confirmed"], "la condition ne tient plus : la proposition disparaît"
    assert not [x for x in L.cut_lines() if x.startswith("✅")], "199 lignes : pas de proposition"
    assert "sort des ligues en fantôme" in lanes.command("soccer_epl on") and lanes.mode("stats", "soccer_epl") == "on"
    # ligue hors football-data : en fantôme par défaut, retour par STATS_ON_LEAGUES (config), proposé quand son CLV fantôme est positif
    store.save(ensemble.LOG_FILE, noisy(200, "soccer_brazil_campeonato", 2.4))
    L._memo["key"] = None
    assert decide() == []
    prop = [x for x in L.cut_lines() if "soccer_brazil_campeonato" in x]
    assert prop and "STATS_ON_LEAGUES=soccer_brazil_campeonato" in prop[0], L.cut_lines()
    from kelly import config as C
    C.STATS_ON_LEAGUES = frozenset({"soccer_brazil_campeonato"})
    try:
        assert lanes.mode("stats", "soccer_brazil_campeonato") == "on" and lanes.mode("stats", "soccer_xb_colombia") == "ghost"
    finally:
        C.STATS_ON_LEAGUES = frozenset()
    assert "configuration" in lanes.command("soccer_brazil_campeonato on") and "rien à remettre" in lanes.command("soccer_spain_la_liga on")
    # la voie ne compte pas les lignes déjà en fantôme : 400 lignes très négatives au Brésil ne coupent pas la voie
    store.save(ensemble.LOG_FILE, noisy(400, "soccer_brazil_campeonato", 1.5))
    L._memo["key"] = None
    assert L.evaluate()["global"][0] == 0 and decide() == []
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    print("ok  coupure d'une ligue à 200 lignes, retour proposé (jamais appliqué) à 200 lignes positives, STATS_ON_LEAGUES, lignes fantômes hors du jugement de la voie")


def test_status_and_morning():
    from kelly import app, lanes
    assert "strategy_state.json" in store.PROTECTED and "strategy_state.json" in app.REPAIR_LABELS
    assert "🎛️ Voies" in lanes.command("") and lanes.command("rien").startswith("🎛️ Voies")
    from datetime import datetime, timezone
    store.save(lanes.STATE_FILE, {"stats": {"mode": "ghost", "since": "2026-10-14", "reason": "CLV 1xBet -3.0 % ± 2.0 sur 340 lignes"}})
    lanes.reset()
    msgs, _ = app.morning_report(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc))
    assert msgs and str(msgs[0]).startswith("🛑 Voie stats coupée depuis le 2026-10-14") and "voies stats on" in str(msgs[0]), msgs[:1]       # PREMIER message du matin
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    msgs, _ = app.morning_report(datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc))
    assert not any("Voie stats coupée" in str(m) for m in msgs)
    print("ok  état protégé, commande voies, message de coupure en premier le matin")


def test_review_fixes():
    from kelly import app, lanes
    # commande depuis Telegram : les arguments arrivent en LISTE de mots
    store.save(lanes.STATE_FILE, {"stats": {"mode": "ghost", "since": "2026-10-14", "reason": "x"}})
    lanes.reset()
    out = lanes.command(["stats", "on"])
    assert "remise à « on »" in out and lanes.mode("stats", "soccer_epl") == "on"
    # prix suspendu et relevé après le coup d'envoi : pas une clôture
    assert L.x_clv(row(1) | {"xclose": {"odds": 2.0, "lead_min": 60, "susp": True}}) is None
    assert L.x_clv(row(1, lead=-8)) is None and L.x_clv(row(1, lead=0)) is not None
    # 30 matchs distincts au moins : 400 lignes de 5 matchs ne coupent rien
    rs = [row(i, odds=1.5, close=2.0, id=f"m{i % 5}", sport_key="soccer_epl") for i in range(400)]
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    assert L.evaluate(rs)["global"][3] is None and L.evaluate(rs)["leagues"]["soccer_epl"][3] is None
    # le retour d'une ligue la repart de zéro : son passé et sa période fantôme ne recoupent pas la voie
    store.save(lanes.STATE_FILE, {"stats": {"leagues": {"soccer_epl": {"since": "2000-01-02"}}}})
    lanes.reset()
    old = [row(f"o{i}", odds=1.8, close=2.0, id=f"o{i}", sport_key="soccer_epl", date="2000-01-01") for i in range(300)]
    assert "sort des ligues" in lanes.command(["soccer_epl", "on"]) and lanes.state()["stats"]["returned"]["soccer_epl"] == TODAY
    ev = L.evaluate(old)
    assert ev["global"][0] == 0 and not ev["leagues"], ev
    # document abîmé au niveau d'une ligue : la voie reste coupée et lisible
    store.save(lanes.STATE_FILE, {"stats": {"mode": "ghost", "since": "2026-10-14", "reason": "r", "leagues": {"soccer_epl": "oops"}}})
    lanes.reset()
    assert lanes.mode("stats", "soccer_epl") == "ghost" and L.cut_lines()[0].startswith("🛑 Voie stats coupée")
    # texte à la française
    assert L._pc(-0.031) == "−3,1 %" and L._pc(0.02, False) == "2,0 %"
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    print("ok  correctifs de relecture : commande en liste de mots, relevé suspendu / en jeu, 30 matchs, retour d'une ligue repart de zéro, document abîmé, virgules")


def test_leg_clv():
    """CLV par jambe des combinés : prix 1xBet pris contre dernier prix connu (ligne du journal de même clé), jambes `fill` à part, part de jambes à valeur à CLV négatif par palier,
    couverture dite (une jambe sans relevé proche n'est jamais comptée comme neutre)."""
    legs = [("a1", "sharpgap", 2.2, 2.0), ("a2", "sharpgap", 1.8, 2.0), ("b1", "stats", 2.1, 2.0), ("c1", "fill", 1.5, 1.4), ("c2", "fill", 1.3, 1.5), ("d1", "sharpgap", None, None)]
    store.save(ensemble.LOG_FILE, [row(k, st, odds=o, close=c, key=k, id=k) for k, st, o, c in legs if o])
    L._memo["key"] = None
    taken = {k: o for k, _, o, _ in legs}
    mk = lambda k, st: {"key": k, "strategy": st, "id": k, "xodds": taken.get(k, 2.0), "result": None, "min_odds": 1.5}        # noqa: E731 - le prix de la jambe = celui du journal ici
    store.save("fair_combos.json", [
        {"date": TODAY, "tier": 5, "result": None, "legs": [mk("a1", "sharpgap"), mk("a2", "sharpgap"), mk("c1", "fill")]},
        {"date": TODAY, "tier": 8, "result": None, "legs": [mk("b1", "stats"), mk("c2", "fill"), mk("d1", "sharpgap"), mk("zz", None)]}])
    rows = L.leg_clv_rows()
    assert len(rows) == 7 and sum(1 for _, _, v in rows if v is not None) == 5, [(l["key"], v) for l, _, v in rows]
    out = L.leg_clv_lines()
    assert out[0].startswith("jambes mesurées : 5 / 7"), out
    sg = next(x for x in out if x.startswith("sharpgap : 2 jambes"))
    assert "50 % battent le dernier prix" in sg and "trop peu pour conclure" in sg, sg
    assert any(x.startswith("stats : 1 jambes") for x in out) and any(x.startswith("fill (à part : jambes sans valeur) : 2 jambes") for x in out), out
    assert not any(x.startswith("avant les étiquettes") for x in out), "la jambe sans relevé n'est pas mesurée"
    t5 = next(x for x in out if x.startswith("palier") and "(1/2)" in x)
    assert "50 % des jambes à valeur à CLV négatif" in t5, t5
    paliers = [x for x in out if x.startswith("palier")]
    assert len(paliers) == 2 and any("0 % des jambes à valeur à CLV négatif (0/1)" in x for x in paliers), paliers       # palier 8 : b1 positive ; c2 (fill) ne compte pas
    assert L.leg_clv_lines(days=1, today="2030-01-01") == []

    # le prix de LA JAMBE, pas celui de la première lecture du journal : un combiné bâti plus tard a pris un autre prix
    store.save(ensemble.LOG_FILE, [row("p1", "sharpgap", odds=2.2, close=2.0, key="p1", id="p1")])
    L._memo["key"] = None
    store.save("fair_combos.json", [{"date": TODAY, "tier": 5, "result": None, "legs": [{"key": "p1", "strategy": "sharpgap", "id": "p1", "xodds": 1.8}]}])
    (_, _, v), = L.leg_clv_rows()
    assert math.isclose(v, math.log(1.8 / 2.0)) and v < 0, v                      # prix de la jambe 1,80 contre dernier prix 2,00 (le journal disait 2,20 : +10 %)
    store.save("fair_combos.json", [{"date": TODAY, "tier": 5, "result": None, "legs": [{"key": "p1", "strategy": "sharpgap", "id": "p1"}]}])
    (_, _, v2), = L.leg_clv_rows()
    assert v2 is None, "jambe sans prix pris : jamais mesurée avec le prix de la première lecture du journal"

    # combinés RÉELS de bets.json : ils n'ont que `xlegs` {clé, prix, référence}, la voie vient du journal
    store.save(ensemble.LOG_FILE, [row("r1", "sharpgap", odds=2.2, close=2.0, key="r1", id="r1"), row("r2", "stats", odds=2.0, close=2.0, key="r2", id="r2", xgone={"lead_min": 40}),
                                   row("r3", "fill", odds=2.0, close=2.0, key="r3", id="r3", xclose={"odds": 2.0, "lead_min": 60, "susp": True}),
                                   row("r4", "fill", odds=2.0, close=2.0, key="r4", id="r4", xclose={"odds": 2.0, "lead_min": -5, "susp": False})])
    L._memo["key"] = None
    store.save("fair_combos.json", [])
    store.save("bets.json", {"picks": [], "combos": [{"date": TODAY, "tier": "🛡️ SÛR", "keys": ["r1", "r2", "r3", "r4", "gone"], "result": None,
                                                       "xlegs": [{"key": "r1", "xodds": 1.9}, {"key": "r2", "xodds": 2.0}, {"key": "r3", "xodds": 2.0}, {"key": "r4", "xodds": 2.0},
                                                                 {"key": "gone", "xodds": 2.0}, {"key": "r1b"}]}]})
    got = {l["key"]: v for l, _, v in L.leg_clv_rows()}
    assert math.isclose(got["r1"], math.log(1.9 / 2.0)), got                     # combiné réel mesuré : prix de la jambe (1,90), pas celui du journal (2,20)
    assert got["r2"] is None and got["r3"] is None and got["r4"] is None, "pari retiré, prix suspendu, relevé après le coup d'envoi : pas de clôture"
    assert got["gone"] is None and got["r1b"] is None, "clé sans ligne au journal, ou sans prix pris : jamais mesurée"
    lines = L.leg_clv_lines()
    assert lines[0].startswith("jambes mesurées : 1 / 6 "), lines[0]
    assert any(x.startswith("palier ") and "SÛR" in x and "(1/1)" in x for x in lines), lines          # palier SÛR : 1 jambe à valeur mesurée (r1, 1,90 contre 2,00), à CLV négatif
    store.save("bets.json", {"picks": [], "combos": []})
    store.save("fair_combos.json", [{"date": TODAY, "tier": 5.0, "result": None, "legs": [{"key": "r1", "strategy": "sharpgap", "id": "r1", "xodds": 1.9}]}])
    assert any(x.startswith("palier cote 5") for x in L.leg_clv_lines()), L.leg_clv_lines()
    store.save("fair_combos.json", [{"date": TODAY, "tier": 5.0, "result": None, "legs": [{"key": "r1", "strategy": "sharpgap", "id": "r1"}]}])
    assert L.leg_clv_rows()[0][2] is None, "une jambe sans prix pris n'est pas mesurée avec le prix du journal"
    store.save("fair_combos.json", [])
    print("ok  CLV par jambe : par voie, fill à part, part négative par palier, couverture dite")


def main():
    test_clv()
    test_lines_and_objectives()
    test_learning_and_archive()
    test_bilan_blocks()
    test_cutoff()
    test_league_cut_and_return()
    test_status_and_morning()
    test_review_fixes()
    test_leg_clv()
    print("tests mesures par voie : OK")


if __name__ == "__main__":
    main()
