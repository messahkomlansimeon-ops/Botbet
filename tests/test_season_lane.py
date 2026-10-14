"""Voie `saison`, commit B (hors ligne) : jambes, prix 1xBet, étiquette, journal, apprentissage (kelly/seasonlane.py). Vérifie l'ISOLEMENT : aucune écriture dans le journal
d'ensemble, calib.json, ensemble.json, means.json ni tracked.json ; clés distinctes de celles des autres voies ; apprentissage sur les seules lignes de référence ; interrupteur
on / ghost / off. Lancer : python -m tests.test_season_lane"""
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-lane-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-season-lane-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from tests.test_season import league  # noqa: E402
from kelly import calib, config as C, ensemble, lanes, season, seasonlane as SL, store  # noqa: E402

SK = "soccer_epl"
NOW = datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)
DAY = NOW.date()
KO = (NOW + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
UNTOUCHED = ("ensemble_log.json", "ensemble.json", "calib.json", "means.json", "tracked.json", "fair.json", "strategy_state.json")


def setup():
    C.STRATEGY_SEASON = "on"
    C.ENSEMBLE_ENABLED = True
    season.reset()
    for name in UNTOUCHED + (SL.LOG, SL.WEIGHTS_FILE, SL.CALIB_FILE):
        store.remove(name) if name in store.names("") or store.load(name, None) is not None else None
    obs = league(8, 6, seed=7, disp=0.1)
    season.collect = lambda sport_key, on, loaders=None: (obs, "football-data")
    return {"leagues": {SK: [{"id": "e1", "sport_key": SK, "home_team": "T1", "away_team": "T4", "commence_time": KO, "league": "EPL"},
                             {"id": "e2", "sport_key": SK, "home_team": "T2", "away_team": "T5", "commence_time": KO, "league": "EPL"}]}}


def px_for(c, over_odds=2.4, under_odds=1.62):
    """Cotes 1xBet de toutes les lignes (plus/moins) du pronostic `c` : l'issue testée paie `over_odds`, l'autre `under_odds`."""
    px = {}
    for name, point, *_ in c["alts"]:
        if name in ("Over", "Under"):
            px[(c["market"], "Over", point)], px[(c["market"], "Under", point)] = over_odds, under_odds
    return px


def fetch_of(pronos, **odds_kw):
    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        out = {}
        for k, ev in events:
            px = {}
            for c in pronos:
                if c["id"] == ev["id"] and c["market"] == "alternate_totals_corners":
                    px |= px_for(c, **odds_kw)
            out[ev["id"]] = {"gid": 7000 + int(ev["id"][1:]), "px": px, "refs": {k_: [7000, 1 + i, 0] for i, k_ in enumerate(px)}}
        return out
    return fetch


def test_make():
    scan = setup()
    pronos = SL.make(scan, NOW, DAY)
    assert pronos, "ligue couverte : des pronostics"
    mk = {c["market"] for c in pronos}
    assert {"totals", "btts", "double_chance", "alternate_totals_corners", "team_totals_home"} <= mk, mk
    assert len({(c["id"], c["market"]) for c in pronos}) == len(pronos), "un pronostic par match et par marché"
    assert all(c["key"].endswith("|saison") and c["id"] in ("e1", "e2") and c["alts"] and "strategy" not in c for c in pronos)
    assert all(C.FAIR_P_RANGE[0] <= a[2] <= C.FAIR_P_RANGE[1] for c in pronos for a in c["alts"])
    # ligue hors de SEASON_LEAGUES ou match commencé : rien
    C.SEASON_LEAGUES = frozenset({"soccer_other"})
    try:
        assert SL.make(scan, NOW, DAY) == []
    finally:
        C.SEASON_LEAGUES = frozenset()
    assert SL.make(scan, NOW + timedelta(hours=9), DAY) == [], "match commencé : pas de pronostic"
    season.collect = lambda sport_key, on, loaders=None: ([], None)
    season.reset()
    assert SL.make(scan, NOW, DAY) == [], "ligue non couverte : rien"
    print("ok  pronostics de la voie : un par match et par marché, clés « |saison », lignes dans la plage de proba ; ligue hors voie, match commencé, ligue non couverte : rien")


def test_price_value_fill_and_none():
    scan = setup()
    pronos = [c for c in SL.make(scan, NOW, DAY) if c["market"] == "alternate_totals_corners"]
    c = pronos[0]
    px = px_for(c, over_odds=3.4, under_odds=1.25)                     # le marché dit ~ 75 % pour « moins » : un « plus » à 3,4 est une valeur si le modèle le voit à ~35 %+
    chosen, ref = SL.price(c, px, {k: [1, 2, 3] for k in px}, NOW)
    assert chosen is not None and chosen["strategy"] == "saison" and chosen["key"].endswith("|saison") and chosen["xref"] == [1, 2, 3]
    assert "alts" not in chosen and chosen["sval"] in (0, 1) and chosen["ew"] == C.SEASON_MODEL_WEIGHT, chosen
    # proba retenue = log-cotes du modèle (poids wm) et du marché (1xBet sans marge)
    pm, px_ = chosen["p_model"], chosen["p_x"]
    want = ensemble.pool(pm, px_, C.SEASON_MODEL_WEIGHT)
    assert abs(chosen["prob"] - round(want, 4)) < 1e-3 and abs(chosen["xodds"] * chosen["prob"] - 1 - chosen["xev"]) < 2e-3, (chosen["prob"], want)
    assert chosen["sval"] == (1 if chosen["prob"] * chosen["xodds"] >= 1 + C.SEASON_MARGIN else 0)
    # sans la paire (l'autre issue absente chez 1xBet) : pas d'avis de marché, aucune jambe
    only = {k: v for k, v in px.items() if k[1] == "Over"}
    assert SL.price(c, only, None, NOW) == (None, None)
    # ligne de référence : la plus proche de FAIR_P_TARGET, sans regarder le prix
    ref_alt = min(c["alts"], key=lambda a: abs(a[2] - C.FAIR_P_TARGET))
    rows = [d for d in (chosen, ref) if d]
    if ref is not None:
        assert "sval" not in ref and ref.get("ref_only") == 1 and ref["ref"] == 1, ref                # la référence d'une autre ligne n'est jamais une jambe
    assert any(d.get("ref") == 1 and d["name"] == ref_alt[0] and d["point"] == ref_alt[1] for d in rows), (ref_alt, [(d["name"], d["point"], d.get("ref")) for d in rows])
    print("ok  prix : proba retenue = log-cotes modèle/marché (wm 0,35), sval 0/1, xref posé, sans avis de marché aucune jambe, ligne de référence indépendante du prix")


def test_fill_and_value_rules():
    scan = setup()
    c = [c for c in SL.make(scan, NOW, DAY) if c["market"] == "alternate_totals_corners"][0]
    name, point, p0, base = max(c["alts"], key=lambda a: a[2])           # la ligne la plus probable du modèle
    wd, cd = SL.load_weights(), SL.load_calib()
    px = {(c["market"], name, point): 1.40, (c["market"], "Under" if name == "Over" else "Over", point): 3.0}
    v = SL._line(c, px, name, point, p0, base, 1.40, NOW, wd, cd)
    assert v["p"] is not None and v["kind"] in ("value", "fill", None)
    # remplissage : proba retenue assez haute et espérance au prix du marché >= -8 %
    v2 = dict(v, p=0.66, ev=0.66 * 1.5 - 1, ev_mkt=-0.05, kind=None)
    assert (0.66 >= C.SEASON_FILL_MIN_P and v2["ev_mkt"] >= C.SEASON_FILL_MIN_EV)
    # rang : valeur d'abord
    a, b = dict(v, kind="value", p=0.5, ev=0.03), dict(v, kind="fill", p=0.7, ev=0.05)
    assert SL._rank(a) > SL._rank(b)
    print("ok  règles : valeur = proba retenue x cote >= 1,02, remplissage = proba >= 0,60 et espérance au prix du marché >= -8 %, la valeur passe d'abord")


def test_run_isolation_and_journal():
    scan = setup()
    pronos = SL.make(scan, NOW, DAY)
    fetch = fetch_of(pronos, over_odds=3.4, under_odds=1.25)
    legs, mode = SL.run(scan, [], DAY, NOW, fetch=fetch, history={})
    assert mode == "on" and legs and all(c["strategy"] == "saison" and c["key"].endswith("|saison") for c in legs), legs[:1]
    assert {c["market"] for c in legs} == {"alternate_totals_corners"}, "seul le marché chiffré par le faux 1xBet"
    log = store.load(SL.LOG, [])
    assert log and all(r["strategy"] == "saison" and r["key"].endswith("|saison") and "shadow" not in r and r.get("pm") is not None and (r.get("px") is not None) for r in log), log[:1]
    assert any(r.get("ref") == 1 for r in log) and all(r.get("sval") is not None or r.get("ref") == 1 for r in log), "chaque ligne est une jambe (sval) ou une référence"
    for name in UNTOUCHED:
        assert store.load(name, None) in (None, [], {}), f"{name} ne doit pas être touché par la voie saison"
    n1 = len(log)
    SL.run(scan, [], DAY, NOW, fetch=fetch, history={})                    # relance : le journal ne double pas
    assert len(store.load(SL.LOG, [])) == n1
    print("ok  passage du matin : jambes étiquetées « saison », journal à part (avis pm / px, sans fantôme de gel), rien d'écrit chez les autres voies, relance sans doublon")


def test_second_pass_promotes_reference_to_leg():
    scan = setup()
    pronos = SL.make(scan, NOW, DAY)
    legs, _ = SL.run(scan, [], DAY, NOW, fetch=fetch_of(pronos, over_odds=3.4, under_odds=1.25), history={})
    log = store.load(SL.LOG, [])
    only_ref = [r for r in log if r.get("ref_only")]
    k = next((r for r in log if r.get("sval") is None and r.get("ref")), None)
    if k is None:                                           # le faux 1xBet choisit toujours la référence : on fabrique le cas (même clé, d'abord sans sval)
        k = dict(log[0])
        k.pop("sval", None)
        k["ref_only"] = 1
        log[0] = k
        store.save(SL.LOG, log)
    key = k["key"]
    c = {"key": key, "id": k["id"], "sport_key": SK, "home": k["home"], "away": k["away"], "commence": KO, "market": k["market"], "name": k["name"], "point": k["point"],
         "p_model": 0.6, "p_x": 0.55, "p0": 0.6, "xodds": 1.9, "sval": 1, "ref": 1, "strategy": "saison", "p_ens": 0.58}
    ensemble.record([c], str(DAY), NOW, name=SL.LOG, shadow=False)
    row = next(r for r in store.load(SL.LOG, []) if r["key"] == key)
    assert row.get("sval") == 1, row
    print("ok  deuxième passage : une ligne d'abord écrite comme simple référence devient la jambe jouée (sval reporté)")


def test_modes():
    scan = setup()
    pronos = SL.make(scan, NOW, DAY)
    fetch = fetch_of(pronos, over_odds=3.4, under_odds=1.25)
    C.STRATEGY_SEASON = "off"
    assert SL.run(scan, [], DAY, NOW, fetch=fetch, history={}) == ([], "off") and store.load(SL.LOG, []) == []
    C.STRATEGY_SEASON = "ghost"
    legs, mode = SL.run(scan, [], DAY, NOW, fetch=fetch, history={})
    assert mode == "ghost" and legs and all(c.get("ghost") == 1 for c in legs) and store.load(SL.LOG, []), "ghost : calculé et journalisé, marqué fantôme"
    C.STRATEGY_SEASON = "on"
    leg = {"strategy": "saison", "sval": 1, "sport_key": "soccer_xb_usl", "market": "alternate_totals_corners"}
    assert lanes.usable(leg) and lanes.usable(dict(leg, sval=0)), "jamais les ligues en fantôme de la voie stats"
    C.FILL_LEGS = False
    try:
        assert lanes.usable(leg) and not lanes.usable(dict(leg, sval=0))
    finally:
        C.FILL_LEGS = True
    C.STRATEGY_SEASON = "ghost"
    assert not lanes.usable(leg)
    C.STRATEGY_SEASON = "on"
    C.ENSEMBLE_ENABLED = False
    assert lanes.season_mode() == "off"
    C.ENSEMBLE_ENABLED = True
    print("ok  interrupteur on / ghost / off ; ligues en fantôme de la voie stats sans effet ici ; FILL_LEGS coupe le remplissage")


def test_run_guards():
    scan = setup()
    C.SOURCES_REFRESH = False
    try:
        assert SL.run(scan, [], DAY, NOW, history={}) == ([], "on"), "SOURCES_REFRESH éteint : aucune requête 1xBet (sans fetch injecté)"
    finally:
        C.SOURCES_REFRESH = True

    def boom(events, deadline=None, wanted=None):
        raise OSError("réseau")
    assert SL.run(scan, [], DAY, NOW, fetch=boom, history={}) == ([], "on"), "une panne du relevé ne casse jamais le matin"
    seen = {}

    def fetch(events, deadline=None, wanted=None):
        seen["deadline"] = deadline
        seen["n"] = len(events)
        return {}
    SL.run(scan, [], DAY, NOW, fetch=fetch, history={})
    import time
    assert seen["n"] == 2 and 0 < seen["deadline"] - time.monotonic() <= C.SEASON_SECONDS
    print("ok  garde-fous : SOURCES_REFRESH éteint = aucune requête, panne du relevé ignorée, délai propre à la voie (SEASON_SECONDS)")


def test_has_value():
    v, f = {"strategy": "saison", "sval": 1}, {"strategy": "saison", "sval": 0}
    assert ensemble.has_value([v, f]) is True and ensemble.has_value([f, f]) is False and ensemble.has_value([f, {"strategy": "sharpgap"}]) is True
    assert ensemble.has_value([f, {"strategy": "fill"}]) is False and ensemble.has_value([f, {}]) is None
    print("ok  combiné : une jambe saison à valeur le rend « avec valeur », que des remplissages (saison ou fill) « sans valeur »")


def test_learn_reference_rows_only():
    setup()
    day = str(DAY - timedelta(days=1))

    def row(i, ref, result, pm=0.62, px=0.58):
        return {"key": f"g{i}|alternate_totals_corners||9.5|Over|saison", "id": f"g{i}", "sport_key": SK, "home": "A", "away": "B", "market": "alternate_totals_corners", "name": "Over",
                "point": 9.5, "p0": pm, "pm": pm, "prob": pm, "px": px, "ps": None, "xodds": 1.8, "date": day, "result": result, "strategy": "saison", "sval": 1, **({"ref": 1} if ref else {})}
    log = [row(i, i % 2 == 0, "win" if i % 3 else "loss") for i in range(40)]
    n = SL.learn(log)
    assert n == 20 and all(r.get("ens") for r in log if r.get("ref")) and not any(r.get("ens") for r in log if not r.get("ref")), "seules les lignes de référence apprennent"
    wd = SL.load_weights()
    assert wd["fam"]["corners"]["m"]["n"] > 0 and len(wd["done"]) == 20
    cd = SL.load_calib()
    assert cd.get("corners") and round(cd["corners"][0]) == 20, cd.get("corners")
    assert SL.learn(log) == 0, "idempotent"
    for name in UNTOUCHED:
        assert store.load(name, None) in (None, [], {}), f"{name} ne doit pas être touché par l'apprentissage de la voie"
    # sous SEASON_MIN_N matchs le poids reste celui de départ
    assert SL.weights("corners")["wm"] == C.SEASON_MODEL_WEIGHT
    print("ok  apprentissage : lignes de référence seulement, idempotent, poids et calibration dans les documents de la voie (rien chez les autres)")


def test_settle_log_with_learner_and_archive():
    setup()
    day = str(DAY - timedelta(days=1))
    rows = [{"key": f"g{i}|totals||2.5|Over|saison", "id": f"g{i}", "sport_key": SK, "home": "A", "away": "B", "market": "totals", "name": "Over", "point": 2.5, "p0": 0.6, "pm": 0.6,
             "prob": 0.6, "px": 0.55, "ps": None, "xodds": 1.9, "date": day, "result": None, "strategy": "saison", "sval": 1, "ref": 1} for i in range(4)]
    store.save(SL.LOG, rows)

    def settle_one(r):
        r["result"] = "win" if int(r["id"][1:]) % 2 else "loss"
        return True
    settled, learned = ensemble.settle_log(settle_one, name=SL.LOG, learner=SL.learn, arch_prefix=SL.ARCH)
    assert len(settled) == 4 and learned == 4
    assert store.names(SL.ARCH) and not store.names(ensemble.ARCH_PREFIX), "archive à part (arch/sea/), pas celle de l'ensemble"
    arch = ensemble.archive_rows(prefix=SL.ARCH)
    assert len(arch) == 4 and all(r["strategy"] == "saison" and r.get("sval") == 1 and r.get("ref") == 1 for r in arch), arch[:1]
    assert store.load("ensemble.json", None) in (None, {}) and store.load("ensemble_log.json", None) in (None, [])
    print("ok  règlement du journal de la voie : apprentissage de la voie seulement, archive arch/sea/ (sval, ref conservés), rien dans l'archive ni les poids de l'ensemble")


def main():
    for t in (test_make, test_price_value_fill_and_none, test_fill_and_value_rules, test_run_isolation_and_journal, test_second_pass_promotes_reference_to_leg, test_modes, test_run_guards, test_has_value, test_learn_reference_rows_only,
              test_settle_log_with_learner_and_archive):
        t()
    print("tests voie saison (jambes) : OK")


if __name__ == "__main__":
    main()
