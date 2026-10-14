"""Voies (sharpgap / stats / fill) : interrupteurs on / ghost / off, ligues de la voie stats en fantôme (par défaut toutes celles sans corners / cartons / tirs / fautes chez football-data),
état de la coupure automatique qui PRIME, jambes « fill » coupées par FILL_LEGS, fraction de Kelly par voie (relative : sharpgap inchangé, stats = 60 %), combiné SANS valeur à mise fixe
(hors Kelly, hors de la somme des parts, prise sur le reste du jour, même plafond de ticket). Une voie « ghost » ou « off » n'est jamais sélectionnée (viable, leg_reason, écarts sharp,
fenêtre des stats) mais reste calculée et journalisée ; « off » n'est même pas lue. Hors ligne. Lancer : python -m tests.test_lanes"""
import contextlib
import copy
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-lanes-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-lanes-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "ENSEMBLE_FROZEN_MODEL", "STRATEGY_SHARPGAP", "STRATEGY_STATS", "FILL_LEGS", "STATS_GHOST_LEAGUES", "STATS_GHOST_OUTSIDE_FD",
          "NOVALUE_STAKE"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, combos as CB, config as C, lanes, money, statswindow, status, store, xalerts, xprice  # noqa: E402

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


@contextlib.contextmanager
def conf(**kw):
    old = {k: getattr(C, k) for k in kw}
    for k, v in kw.items():
        setattr(C, k, v)
    lanes.reset()
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(C, k, v)
        lanes.reset()


def prono(market="totals", name="Over", point=2.5, lane="sharpgap", sport="soccer_epl", **kw):
    return {"key": f"m|{market}||{point}|{name}", "id": "m", "sport_key": sport, "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z", "market": market, "name": name,
            "point": point, "prob": 0.72, "p0": 0.72, "p_ens": 0.72, "min_odds": 1.40, "xodds": 1.55, "xev": 0.1, "conf": 0.6, "n_min": 8, "score": 0.2, "strategy": lane,
            "pick": "x", "xkind": "value"} | kw


def test_modes():
    assert (C.STRATEGY_SHARPGAP, C.STRATEGY_STATS, C.FILL_LEGS, C.STATS_GHOST_OUTSIDE_FD, C.NOVALUE_STAKE) == ("on", "on", True, True, 0)
    assert C.KELLY_FRACTION_SHARPGAP == 0.25 == C.KELLY_FRACTION and C.KELLY_FRACTION_STATS == 0.15
    os.environ["STRATEGY_STATS"] = "bizarre"
    assert C._mode("STRATEGY_STATS", "on") == "on", "une valeur inconnue ne coupe jamais une voie"
    os.environ["STRATEGY_STATS"] = "GHOST"
    assert C._mode("STRATEGY_STATS", "on") == "ghost"
    os.environ.pop("STRATEGY_STATS")
    lanes.reset()
    assert lanes.mode("sharpgap") == "on" and lanes.mode("stats", "soccer_epl") == "on"
    assert lanes.mode("stats", "soccer_brazil_campeonato") == "ghost" and lanes.mode("stats", "soccer_xb_colombia") == "ghost", "ligues hors football-data : fantôme par défaut"
    assert lanes.mode("stats", "soccer_epl") == "on" and lanes.mode("stats", "soccer_germany_bundesliga2") == "on"
    with conf(STATS_GHOST_OUTSIDE_FD=False):
        assert lanes.mode("stats", "soccer_brazil_campeonato") == "on"
    with conf(STATS_GHOST_LEAGUES=frozenset({"soccer_epl"})):
        assert lanes.mode("stats", "soccer_epl") == "ghost"
    with conf(STRATEGY_STATS="off"):
        assert lanes.mode("stats", "soccer_epl") == "off"
    # l'état de la coupure automatique PRIME sur « on » ; il ne repart qu'à la main
    store.save(lanes.STATE_FILE, {"stats": {"mode": "ghost", "since": "2026-10-14"}})
    lanes.reset()
    assert lanes.mode("stats", "soccer_epl") == "ghost" and lanes.mode("sharpgap") == "on"
    store.save(lanes.STATE_FILE, {"stats": {"mode": "on", "leagues": {"soccer_spain_la_liga": {"n": 200}}}})
    lanes.reset()
    assert lanes.mode("stats", "soccer_spain_la_liga") == "ghost" and lanes.mode("stats", "soccer_epl") == "on"
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    print("ok  modes : défauts, valeur inconnue sans effet, ligues hors football-data en fantôme, état de coupure prioritaire")


def test_usable_and_gates():
    corners = prono("alternate_totals_corners", "Under", 9.5, lane="stats")
    goals = prono()
    fill = prono(lane="fill", xkind="probable")
    assert lanes.usable(corners) and lanes.usable(goals) and lanes.usable(fill) and lanes.usable({"market": "totals"}), "tout est « on » par défaut, y compris sans étiquette"
    assert not lanes.usable(prono("alternate_totals_corners", "Under", 9.5, lane="stats", sport="soccer_brazil_campeonato")), "ligue hors football-data : fantôme"
    assert CB.viable(goals) and CB.leg_reason(goals | {"edge": 0.12, "n_books": 10}) == "value"
    with conf(STRATEGY_SHARPGAP="ghost"):
        assert not lanes.usable(goals) and not lanes.usable(fill), "les lignes des cinq familles, y compris les favoris sans valeur, suivent la voie sharpgap"
        assert not CB.viable(goals) and CB.leg_reason(goals | {"edge": 0.12, "n_books": 10}) is None and lanes.usable(corners)
    with conf(STRATEGY_STATS="off"):
        assert not lanes.usable(corners) and lanes.usable(goals) and not CB.viable(corners)
    with conf(FILL_LEGS=False):
        assert not lanes.usable(fill) and lanes.usable(goals) and not CB.viable(fill) and CB.leg_reason(fill | {"edge": 0.0, "n_books": 10}) is None
        assert lanes.usable(prono("team_throws_total", "Over", 30.5, lane="fill")) is False, "une touche n'a de place que comme jambe « fill »"
    with conf(ENSEMBLE_ENABLED=False, STRATEGY_SHARPGAP="off", FILL_LEGS=False):
        assert lanes.usable(goals) and lanes.usable(fill), "ensemble éteint : le comportement d'avant, aucune voie"
    print("ok  sélection : une voie « ghost » / « off », une ligue de stats en fantôme et les jambes « fill » (FILL_LEGS) ne sont jamais viables ni jambes ; ensemble éteint = comme avant")


def test_stakes():
    items = [{"kind": "combo", "tier": "🛡️ SÛR", "prob": 0.40, "odds": 3.0}, {"kind": "combo", "tier": "⚖️ COSTAUD", "prob": 0.2, "odds": 7.0}]
    base = money.allocate(items, 5000)[0]
    assert base[0] > 0 and base[1] > 0
    assert money.allocate([i | {"frac": 0.25} for i in items], 5000)[0] == base, "sharpgap (0,25) : mises inchangées"
    stats = money.allocate([i | {"frac": 0.15} for i in items], 5000)[0]
    for it in items:                                                            # exactement 60 % de la part (0,15 / 0,25), à l'arrondi du pas près
        assert abs(money._raw(it | {"frac": 0.15}) - 0.6 * money._raw(it)) < 1e-12
    assert stats == [int(5000 * money._raw(i | {"frac": 0.15}) / 50) * 50 for i in items] and all(s < b for s, b in zip(stats, base)), (base, stats)
    assert money.allocate([items[0] | {"frac": 0}], 5000)[0] == [0], "fraction nulle : aucune mise (pas « fraction absente »)"
    # jour SUR-SOUSCRIT : réduire une mise stats ne gonfle pas les mises sharpgap (la somme des parts est celle d'avant la fraction)
    big = [{"kind": "combo", "tier": "🛡️ SÛR", "prob": 0.9, "odds": 3.0}] * 3
    full = money.allocate(big + [{"kind": "combo", "tier": "⚖️ COSTAUD", "prob": 0.6, "odds": 5.0}], 5000)[0]
    mixed = money.allocate(big + [{"kind": "combo", "tier": "⚖️ COSTAUD", "prob": 0.6, "odds": 5.0, "frac": 0.15}], 5000)[0]
    assert sum(money._raw(i, False) for i in big + [{"kind": "combo", "tier": "⚖️ COSTAUD", "prob": 0.6, "odds": 5.0}]) > 1, "le jour est sur-souscrit"
    assert mixed[:3] == full[:3] and mixed[3] < full[3], (full, mixed)
    # fields de voie
    sg, st, fl = {"strategy": "sharpgap"}, {"strategy": "stats"}, {"strategy": "fill"}
    assert lanes.item_fields([sg, fl]) == {"frac": 0.25} and lanes.item_fields([st, fl]) == {"frac": 0.15}
    assert lanes.item_fields([sg, st]) == {"frac": 0.15}, "le plus prudent des combinés à valeur"
    assert lanes.item_fields([fl, fl]) == {"novalue": True} and lanes.item_fields([fl], single=True) == {} and lanes.item_fields([st], single=True) == {"frac": 0.15}
    assert lanes.item_fields([sg, {}]) == {} and lanes.item_fields([]) == {}, "voie inconnue (ligne d'avant les étiquettes, live) : comportement d'avant"
    # combiné sans valeur : mise fixe = un pas d'arrondi (50 pour 5 000), hors somme des parts, hors Kelly même à espérance négative
    nv = {"kind": "fair", "tier": 6.0, "prob": 0.10, "odds": 6.0, "novalue": True}
    assert money.allocate([nv], 5000)[0] == [50], "espérance négative mais mise fixe"
    both = money.allocate(items + [nv], 5000)[0]
    assert both[:2] == base and both[2] == 50, "n'enlève rien aux autres (hors de la somme des parts)"
    assert money.allocate([nv], 5000, detail=True)[2] == 4950
    with conf(NOVALUE_STAKE=100.0):
        assert money.allocate([nv], 5000)[0] == [100]
    big = [{"kind": "combo", "tier": "🛡️ SÛR", "prob": 0.9, "odds": 3.0}] * 3 + [nv]
    out = money.allocate(big, 5000)[0]
    assert sum(out) <= 5000 and out[3] <= 50, out
    assert money.allocate([nv], 20)[0] == [0.5], "petit budget : un pas d'arrondi (0,5)"
    with conf(NOVALUE_STAKE=30.0):
        assert money.allocate([nv], 5000)[0] == [50], "jamais sous un pas d'arrondi : la mise minimale"
    cap = money.ticket_cap
    money.ticket_cap = lambda: 120                                     # mode capital : jamais plus de TICKET_SHARE du capital sur un ticket
    try:
        with conf(NOVALUE_STAKE=500.0):
            assert money.allocate([nv], 5000)[0] == [100], "plafonné par le ticket (120), arrondi au pas (100)"
    finally:
        money.ticket_cap = cap
    full_day = [{"kind": "combo", "tier": "🛡️ SÛR", "prob": 0.99, "odds": 3.0}] * 4
    out = money.allocate(full_day + [nv], 5000)[0]
    assert sum(out) <= 5000 and out[4] in (0, 50), out                          # le budget n'est jamais dépassé, la mise fixe se contente du reste
    assert money.allocate([nv], 0)[0] == [0], "sans budget : rien"
    # row_item : lignes de suivi
    row = {"legs": [fl | {"key": "a"}, fl | {"key": "b"}], "min_odds": 6.0, "prob": 0.1}
    assert money.row_item(row)["novalue"] is True and money.row_item(row | {"legs": [sg | {"key": "a"}, fl | {"key": "b"}]}).get("frac") == 0.25
    assert money.row_item({"keys": ["a"], "tier": "🛡️", "prob": 0.4, "odds": 3.0, "has_value": False})["novalue"] is True
    assert "novalue" not in money.row_item({"keys": ["a"], "tier": "🛡️", "prob": 0.4, "odds": 3.0})
    print("ok  mises : fraction de la voie relative à 0,25 (sharpgap inchangé, stats 60 %), le plus prudent d'un combiné, combiné sans valeur = mise fixe hors Kelly et hors somme des parts")


def test_selection_points():
    pk = [prono()]
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("sharpgap.find ne doit pas être appelé"))   # noqa: E731
    real = app.sharpgap.find
    try:
        app.sharpgap.find = boom
        with conf(STRATEGY_SHARPGAP="ghost"):
            assert app.morning_gaps(pk, [], "2026-10-14", "08:00") == [], "voie ghost : rien d'annoncé, rien de numéroté (reserve) ni de suivi"
        with conf(STRATEGY_SHARPGAP="off"):
            assert app.morning_gaps(pk, [], "2026-10-14", "08:00") == []
            assert xalerts.gap_items({"day": "2026-10-14", "picks": pk}, NOW, None) == []
    finally:
        app.sharpgap.find = real
    # xprice.apply : voie « off » = rien de lu pour ses lignes ; « ghost » est lue comme « on »
    seen = []
    fetch = lambda events, **k: seen.append(len(events)) or {}                                          # noqa: E731
    cor = prono("alternate_totals_corners", "Under", 9.5, lane="stats") | {"home": "A", "away": "B", "xgid": 1}
    with conf(STRATEGY_STATS="off"):
        out = xprice.apply([copy.deepcopy(cor)], fetch=fetch)
        assert not seen and out["matches"] == 0, "voie stats « off » : aucun relevé"
    with conf(STRATEGY_STATS="ghost"):
        xprice.apply([copy.deepcopy(cor)], fetch=fetch)
        assert seen, "voie stats « ghost » : lue (la référence 1xBet est nécessaire)"
    # fenêtre des stats : « off » ne relit plus ; « ghost » relit (journalisé) mais ne peut rien annoncer
    doc = {"reads": {}, "announced": [], "combos": [], "requests": 0, "bytes": 0, "passes": 0}
    near = cor | {"commence": (NOW.replace(hour=11)).isoformat(), "xgid": 5, "xat": None, "xodds": None, "market": "team_shots_home", "name": "Over", "point": 5.5}
    near.pop("xodds")
    with conf(STRATEGY_STATS="off"):
        assert statswindow.select([near], NOW, doc) == []
    with conf(STRATEGY_STATS="ghost"):
        assert statswindow.select([near], NOW, doc) != [], "ghost : relevé et journal comme « on »"
        assert statswindow.announce_list({"pronos": [near | {"xodds": 1.9, "xat": "07:55"}], "announced": []}, {"announced": [], "combos": []}, NOW) == [], "mais rien d'annoncé"
    # status
    assert any("sharpgap : on · stats : on" in l for l in lanes.lines()) and any("Stats en fantôme" in l for l in lanes.lines())
    assert "🎛️ Voies" in str(status.report(NOW))
    print("ok  points de sélection : écarts sharp (matin et alertes), relevé 1xBet, fenêtre des stats ; ghost = calculé et journalisé jamais sélectionné ; off = rien de lu ; status affiche les voies")


def test_bypasses_and_robustness():
    from kelly import live
    import kelly.combos as combos_mod
    # état abîmé : jamais un plantage de la sélection
    for bad in ({"stats": "ghost"}, {"stats": ["x"]}, {"stats": {"mode": "ghost", "leagues": ["soccer_epl"]}}, ["pas un dict"], "texte"):
        store.save(lanes.STATE_FILE, bad)
        lanes.reset()
        assert lanes.mode("stats", "soccer_epl") in ("on", "ghost") and lanes.usable(prono())
    store.save(lanes.STATE_FILE, {"stats": {"mode": "ghost", "leagues": ["soccer_epl"]}})
    lanes.reset()
    assert lanes.mode("stats", "soccer_epl") == "ghost", "le mode lisible reste appliqué, les ligues mal formées sont ignorées"
    store.save(lanes.STATE_FILE, {})
    lanes.reset()
    # combinés « audacieux » : une ligne de stats d'une ligue en fantôme n'y entre pas
    bra = prono("alternate_totals_corners", "Under", 9.5, lane="stats", sport="soccer_brazil_campeonato")
    epl = prono("alternate_totals_corners", "Under", 9.5, lane="stats", sport="soccer_epl") | {"key": "e|c"}
    mine, real = app._bold_pools([], [bra, epl], "", min_p=0.5, min_ev=-1)
    keys = {c["key"] for c in mine}
    assert epl["key"] in keys and bra["key"] not in keys, keys
    with conf(STATS_GHOST_OUTSIDE_FD=False):
        assert bra["key"] in {c["key"] for c in app._bold_pools([], [bra, epl], "", min_p=0.5, min_ev=-1)[0]}
    # combiné live : les jambes d'une voie « ghost » / « off » ne sont pas proposées
    seen = []
    legs_ = [{"id": "l1", "key": "l1", "market": "totals", "sport_key": "soccer_epl", "odds": 1.5, "prob": 0.7},
             {"id": "l2", "key": "l2", "market": "alternate_totals_corners", "sport_key": "soccer_epl", "odds": 1.5, "prob": 0.7}]
    real_fetch, real_legs, real_best, real_rec = live.fetch, live.legs, combos_mod.best_combo, live.record
    live.fetch, live.legs, live.record = lambda get=None: ({}, None), (lambda raw, now=None, model_for=None: (legs_, {}, {"live": 2, "window": 2, "known": 2})), lambda c, now: seen.append(("rec", len(c)))
    combos_mod.best_combo = lambda cands, *a, **k: seen.append(("best", [c["id"] for c in cands])) or None
    try:
        with conf(STRATEGY_SHARPGAP="ghost"):
            live.combo(10, "2026-10-14")
    finally:
        live.fetch, live.legs, live.record, combos_mod.best_combo = real_fetch, real_legs, real_rec, real_best
    assert ("rec", 2) in seen and ("best", ["l2"]) in seen, "journalisé en entier, mais la jambe de la voie sharpgap en ghost n'est pas proposée"
    # écarts sharp avec l'ensemble ÉTEINT : comme avant (l'interrupteur de voie ne joue pas)
    real = app.sharpgap.find
    try:
        app.sharpgap.find = lambda *a, **k: ["écart"]
        with conf(ENSEMBLE_ENABLED=False, STRATEGY_SHARPGAP="ghost"):
            assert app.morning_gaps([prono()], [], "2026-10-14", "08:00") == ["écart"]
        with conf(STRATEGY_SHARPGAP="ghost"):
            assert app.morning_gaps([prono()], [], "2026-10-14", "08:00") == []
        with conf():
            assert app.morning_gaps([prono(), bra], [], "2026-10-14", "08:00") == ["écart"]
            assert app.morning_gaps([bra], [], "2026-10-14", "08:00") == [], "un écart sur un marché de stats d'une ligue en fantôme n'est pas annoncé"
    finally:
        app.sharpgap.find = real
    print("ok  contournements fermés : combinés audacieux, live, écarts sur marché de stats en fantôme ; état abîmé sans plantage ; ensemble éteint = comme avant")


def test_day_stakes_end_to_end():
    money.set_daily(5000)
    sg = prono() | {"strategy": "sharpgap"}
    fl = prono(lane="fill", xkind="probable") | {"key": "f1"}
    fl2 = fl | {"key": "f2"}
    top = None
    built = [("🛡️ SÛR", ([sg, sg | {"key": "k2"}], 3.0, 0.40))]
    plain = app.day_stakes(top, built, [])[0]
    with_nv = app.day_stakes(top, built, [(6.0, ([fl, fl2], 6.0, 0.10))])
    assert with_nv[0]["🛡️ SÛR"] == plain["🛡️ SÛR"] > 0, "le combiné sans valeur ne retire rien aux autres"
    assert with_nv[0][("fair", 6.0)] == 50 and with_nv[1] == plain["🛡️ SÛR"] + 50, "mise fixe d'un pas d'arrondi, comptée dans le misé"
    stats = prono("alternate_totals_corners", "Under", 9.5, lane="stats")
    stake_stats = app.day_stakes(None, [("🛡️ SÛR", ([stats, stats | {"key": "k3"}], 3.0, 0.40))], [])[0]["🛡️ SÛR"]
    assert stake_stats < plain["🛡️ SÛR"], "un combiné de la voie stats mise 60 % de la part"
    print("ok  day_stakes de bout en bout : sharpgap inchangé, combiné sans valeur = un pas d'arrondi sans rien retirer aux autres, voie stats = 60 %")


def test_ghost_lines_count_in_trial():
    """Décision du 14/10 : l'essai des tirs « plus de » compte les lignes passées en fantôme dans n et dans le taux réalisé ; seul le gain aux cotes 1xBet les exclut."""
    from kelly import calib, fair
    store.save(fair.FILE, [])
    store.save(calib.FILE, {})
    bra = [prono("team_shots_home", "Over", 5.5 + i, lane="stats", sport="soccer_brazil_campeonato") | {"id": f"b{i}", "key": f"b{i}|team_shots_home||{5.5 + i}|Over"} for i in range(3)]
    epl = prono("team_shots_home", "Over", 5.5, lane="stats", sport="soccer_epl") | {"id": "e1", "key": "e1|team_shots_home||5.5|Over"}
    assert not lanes.usable(bra[0]) and lanes.usable(epl)
    n = fair.record(bra + [epl], "2026-10-14", shown=[])
    rows = {r["id"]: r for r in fair.load()}
    assert n == 4 and all(rows[f"b{i}"].get("ghost") == 1 and rows[f"b{i}"].get("x") == 1 for i in range(3)), rows
    assert "ghost" not in rows["e1"], "une ligne jouable n'est pas marquée"
    for r in rows.values():
        r["result"] = "win" if r["id"] in ("b0", "b1", "e1") else "loss"
    calib.add(list(rows.values()), None)
    cell = calib.load()["_cell"]["|".join(calib.TRIAL)]
    assert cell[0] == 4.0 and cell[1] == 3.0, cell                                    # n = 4 (3 fantômes + 1 jouable), 3 gagnés
    nr = calib.load().get("_new_rows") or []
    assert nr == [["2026-10-14", "e1", "shots", 1.55, 1]], nr                          # seule la ligne jouable entre dans le gain aux cotes 1xBet
    print("ok  essai des tirs : les lignes en fantôme sont suivies, réglées et comptées dans n / taux réalisé ; leur gain 1xBet est exclu")


def main():
    test_modes()
    test_usable_and_gates()
    test_stakes()
    test_selection_points()
    test_bypasses_and_robustness()
    test_day_stakes_end_to_end()
    test_ghost_lines_count_in_trial()
    print("tests voies : OK")


if __name__ == "__main__":
    main()
