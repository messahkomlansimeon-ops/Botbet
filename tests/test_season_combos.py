"""Voie `saison`, commit C (hors ligne) : combinés par palier de cote (3, 10, 30, 100), envoi du matin dans une section à part, mise (part de la limite du jour, fraction propre à la
voie, plafonds), réutilisation des matchs avec le reste de la journée, combiné sans valeur en fantôme, relance sans doublon, aucune mise à jour automatique par les passages horaires,
commandes « saison » / « saison N ». Lancer : python -m tests.test_season_combos"""
from datetime import timedelta

from tests import test_notes_fcfa as N      # fixe DATA_DIR (dossier temporaire), monde de test, réglages des jeux d'essai
from tests import test_season_lane as T     # jambes `saison` fictives (ligue de 8 équipes, faux 1xBet)
from kelly import app, combos as CB, config as C, ensemble as E, fair, lanes, money, season, seasonlane as SL, store, played  # noqa: E402

DAY, NOW = T.DAY, T.NOW


def make_legs(n=12, over_odds=2.4, under_odds=1.62):
    scan = T.setup()
    evs = [{"id": f"e{i}", "sport_key": T.SK, "home_team": f"T{i % 8}", "away_team": f"T{(i + 3) % 8}", "commence_time": T.KO, "league": "EPL"} for i in range(1, n + 1)]
    scan = {"leagues": {T.SK: evs}}
    pr = SL.make(scan, NOW, DAY)
    legs, _ = SL.run(scan, [], DAY, NOW, fetch=T.fetch_of(pr, over_odds=over_odds, under_odds=under_odds), history={})
    return legs


def setup():
    N.reset()
    C.NOVALUE_SEND = False
    C.SEASON_SEND_FILL = True
    C.STRATEGY_SEASON = "on"
    store.save(fair.GHOST_COMBOS_FILE, [])
    store.save("fair_combos.json", [])
    money.set_daily(5000)
    return make_legs()


def test_plan_tiers_and_ranges():
    legs = setup()
    plan = CB.season_plan(legs)
    assert [t for t, _ in plan] == ["ss3", "ss10", "ss30", "ss100"]
    for t, combo in plan:
        if combo:
            target = CB.season_no(t)
            assert target * (1 - C.SEASON_SPREAD) - 0.01 <= combo[1] <= target * (1 + C.SEASON_SPREAD) + 0.01, (t, combo[1])
            ids = [c["id"] for c in combo[0]]
            assert len(ids) == len(set(ids)) >= 2, "un pari par match"
            assert all(c["strategy"] == "saison" for c in combo[0])
    got = [t for t, c in plan if c]
    assert "ss3" in got and "ss10" in got, got
    print("ok  paliers 3 / 10 / 30 / 100 : cote totale dans la fourchette de ±20 %, un pari par match, jambes étiquetées « saison »")


def test_value_first_then_fill():
    legs = setup()
    value, fill = [c | {"sval": 1} for c in legs[:6]], [dict(c, sval=0, key=c["key"] + "f", id="f" + c["id"]) for c in legs[6:12]]
    only_value = CB.season_plan(value, [3])[0][1]
    assert only_value and all(c["sval"] == 1 for c in only_value[0])
    # la fourchette n'est atteinte qu'avec du remplissage : valeur d'abord, remplissage ensuite
    few = value[:2]
    mixed = CB.season_plan(few + fill, [10])[0][1]
    assert mixed and 8 <= mixed[1] <= 12 and len(mixed[0]) > len(few), mixed and mixed[1]            # deux jambes à valeur ne font pas 10 : le remplissage complète
    C.FILL_LEGS = False
    try:
        assert CB.season_plan(few + fill, [10])[0][1] is None, "FILL_LEGS éteint : jamais de remplissage"
    finally:
        C.FILL_LEGS = True
    print("ok  jambes de valeur d'abord, puis le remplissage seulement si la fourchette l'exige (et si FILL_LEGS)")


def test_usage_rules():
    legs = setup()
    u = CB.Usage()
    plan = CB.season_plan(legs, [3, 3], usage=u)
    assert plan[0][1] and u.by_event, "un combiné avec valeur consomme la réutilisation"
    used = {c["id"]: v for c, v in ((c, u.by_event[c["id"]]) for c in plan[0][1][0])}
    assert all(v["n"] == 1 for v in used.values())
    fills = [dict(c, sval=0, strategy="fill") for c in legs]
    u2 = CB.Usage()
    p2 = CB.season_plan(fills, [3], usage=u2)
    assert p2[0][1] and not E.unsent(p2[0][1][0]) and u2.by_event, "un combiné de remplissage de la voie saison est ENVOYÉ : il consomme la réutilisation"
    C.SEASON_SEND_FILL = False
    try:
        u3 = CB.Usage()
        p3 = CB.season_plan(fills, [3], usage=u3)
        assert p3[0][1] and E.unsent(p3[0][1][0]) and not u3.by_event, "SEASON_SEND_FILL éteint : fantôme, ne consomme rien"
    finally:
        C.SEASON_SEND_FILL = True
    print("ok  réutilisation des matchs : un combiné à valeur ou de remplissage envoyé consomme (Usage de la journée), le fantôme (SEASON_SEND_FILL éteint) non")


def test_item_fields_and_cap():
    legs = setup()
    v, f = legs[0] | {"sval": 1}, legs[1] | {"sval": 0, "strategy": "fill"}
    assert lanes.item_fields([v, f]) == {"frac": C.KELLY_FRACTION_SEASON}
    assert lanes.item_fields([f, f]) == {"frac": C.KELLY_FRACTION_SEASON, "flat": True} and lanes.item_fields([f], single=True) == {}, "remplissage seul envoyé : part de la voie, confiance plancher"
    C.SEASON_SEND_FILL = False
    try:
        assert lanes.item_fields([f, f]) == {"novalue": True}
    finally:
        C.SEASON_SEND_FILL = True
    fl = money.allocate([{"kind": "fair", "tier": 3.0, "prob": 0.3, "odds": 3.0, "p_mkt": 0.3} | lanes.item_fields([f, f])], budget=5000)[0][0]
    assert fl > 0, "même sans espérance positive, le combiné de remplissage de la voie reçoit sa mise plancher"
    lot = money.allocate([{"kind": "fair", "tier": 100.0, "prob": 0.02, "odds": 100.0, "p_mkt": 0.02} | lanes.item_fields([f, f])], budget=50000)[0][0]
    assert lot <= money.LOTTERY_STAKE, "au-delà de la cote 50 : le plafond loterie existant"
    C.SEASON_TICKET_MAX = 300
    try:
        assert lanes.item_fields([v, f]) == {"frac": C.KELLY_FRACTION_SEASON, "cap": 300}
        st = money.allocate([{"kind": "fair", "tier": 10.0, "prob": 0.2, "odds": 10.0, "p_mkt": 0.2} | lanes.item_fields([v, f])], budget=5000)[0][0]
        assert 0 < st <= 300, st
    finally:
        C.SEASON_TICKET_MAX = 0
    base = money.allocate([{"kind": "fair", "tier": 10.0, "prob": 0.2, "odds": 10.0, "p_mkt": 0.2}], budget=5000)[0][0]
    season_st = money.allocate([{"kind": "fair", "tier": 10.0, "prob": 0.2, "odds": 10.0, "p_mkt": 0.2} | lanes.item_fields([v, f])], budget=5000)[0][0]
    assert 0 < season_st < base and abs(season_st / base - C.KELLY_FRACTION_SEASON / C.KELLY_FRACTION_REF) < 0.15, (season_st, base)
    print("ok  mise : fraction propre à la voie (0,15 / 0,25 de la part), plafond SEASON_TICKET_MAX, remplissage seul = sans valeur")


def test_morning_section_and_record():
    legs = setup()
    plan = [(t, c) for t, c in CB.season_plan(legs) if c]
    msgs, sent_picks, sent_combos = app.build_day([], DAY, pronos=[], fair_combos=plan, odds_time="09:30")
    txt = "\n".join(str(m) for m in msgs)
    assert "SAISON EN COURS · cote 3" in txt and "SAISON EN COURS · cote 10" in txt, txt[:500]
    assert "Mise conseillée" in txt or "FCFA" in txt
    stakes, fcoupons = {}, {}
    msgs2, *_ = app.build_day([], DAY, pronos=[], fair_combos=plan, fair_stakes=stakes, fair_coupons=fcoupons, odds_time="09:30")
    assert set(stakes) >= {t for t, _ in plan}, stakes
    app.record_fair_morning(plan, DAY, stakes, fcoupons, "09:30")
    rows = [r for r in fair.load_combos() if r["date"] == DAY.isoformat() or r["date"] == str(DAY)]
    assert {r["tier"] for r in rows} == {t for t, _ in plan} and all(r.get("has_value") is True for r in rows), rows
    assert app._recorded_fair(str(DAY)) == {} and set(app._recorded_season(str(DAY))) == {t for t, _ in plan}, "les paliers saison ne se mêlent pas aux paliers à vérifier"
    # relance : mêmes combinés renvoyés tels quels, aucune ligne en double
    again = app._resend_fair(legs, app._recorded_season(str(DAY)), CB.season_plan(legs))
    app.record_fair_morning(again, DAY, stakes, fcoupons, "09:41")
    assert len([r for r in fair.load_combos() if CB.season_no(r["tier"]) is not None]) == len(plan)
    assert [c[0][0]["key"] for _, c in again if c] == [c[0][0]["key"] for _, c in plan], "mêmes paris à la relance"
    print("ok  matin : section « saison en cours » (un message par palier), mises prises sur le budget du jour, suivi sous « ss<N> », relance sans doublon")


def test_fill_only_is_ghost():
    legs = setup()
    C.SEASON_SEND_FILL = False
    fills = [dict(c, sval=0, strategy="fill") for c in legs]
    plan = [(t, c) for t, c in CB.season_plan(fills, [3, 10]) if c]
    assert plan
    ghost_out = set()
    msgs, _, sent = app.build_day([], DAY, pronos=[], fair_combos=plan, odds_time="09:30", ghost_out=ghost_out)
    txt = "\n".join(str(m) for m in msgs)
    assert "SAISON EN COURS" not in txt and "👻" in txt, txt[:400]
    assert ghost_out == {t for t, _ in plan} and len(fair.load_ghost()) == len(plan) and all(g["kind"] == "saison" for g in fair.load_ghost())
    app.record_fair_morning(plan, DAY, {}, {}, "09:30", ghost_out)
    assert not [r for r in fair.load_combos() if CB.season_no(r["tier"]) is not None], "rien dans fair_combos.json"
    C.SEASON_SEND_FILL = True
    print("ok  SEASON_SEND_FILL éteint : le combiné de remplissage seul est suivi en fantôme (« saison »), jamais envoyé ni enregistré comme ticket")


def test_fill_only_is_sent():
    legs = setup()
    fills = [dict(c, sval=0, strategy="fill") for c in legs]
    assert all(c["lane"] == "saison" and c["strategy"] == "fill" for c in fills)
    plan = [(t, c) for t, c in CB.season_plan(fills, [3, 10]) if c]
    assert plan and all(E.has_value(c[0]) is False and not E.unsent(c[0]) for _, c in plan)
    # tri par proba retenue décroissante : le combiné à cote 3 est fait des favoris les plus sûrs
    best = sorted((c for c in fills), key=lambda c: -c["prob"])[:2]
    assert {l["id"] for l in plan[0][1][0]} <= {c["id"] for c in sorted(fills, key=lambda c: -c["prob"])[:6]}, "jambes parmi les plus probables"
    stakes, fc, ghost = {}, {}, set()
    msgs, _, sent = app.build_day([], DAY, pronos=[], fair_combos=plan, fair_stakes=stakes, fair_coupons=fc, odds_time="09:30", ghost_out=ghost)
    txt = "\n".join(str(m) for m in msgs)
    assert "SAISON EN COURS · cote 3" in txt and "sans valeur" in txt and not ghost and "👻" not in txt, txt[:600]
    assert all(stakes[t] > 0 for t, _ in plan), stakes                      # mise = part de la voie, confiance plancher
    app.record_fair_morning(plan, DAY, stakes, fc, "09:30")
    rows = [r for r in fair.load_combos() if CB.season_no(r["tier"]) is not None]
    assert len(rows) == len(plan) and all(r.get("has_value") is False and all(l["strategy"] == "fill" and l["lane"] == "saison" for l in r["legs"]) for r in rows), rows
    # textes : « sans valeur » toujours dit pour un combiné ENVOYÉ, jamais « suivi en fantôme » ni « Kelly ne conseille pas »
    assert money.stake_words(200, 0.1, 3.0, " (sur ton budget du jour)", flat=True) == "200 FCFA · sans valeur (sur ton budget du jour)"
    assert money.stake_words(0, 0.1, 3.0, flat=True) == money.NOFILL_TXT and "fantôme" not in money.NOFILL_TXT
    assert money.stake_words(None, 0.1, 3.0, flat=True).startswith("sans valeur") and "Kelly ne conseille" not in money.stake_words(None, 0.1, 3.0, flat=True)
    assert money.stake_words(0, 0.1, 3.0, novalue=True) == money.NOVALUE_TXT, "un vrai fantôme garde son texte"
    assert app._stake_words(0, 0.1, 3.0, plan[0][1][0]) == money.NOFILL_TXT
    # l'argent de la voie n'entre pas dans « avec / sans valeur » des autres voies
    row = {"date": str(DAY), "tier": "ss3", "min_odds": 3.0, "prob": 0.3, "result": "win", "has_value": False, "legs": plan[0][1][0],
           "played": {"odds": 3.0, "stake": 100, "at": "x"}, "stake": 100, "cur": "FCFA"}
    store.save("fair_combos.json", [row])
    from kelly import lanestats
    assert lanestats.combo_money() == {}, lanestats.combo_money()
    slim = fair._keep([dict(row, date="2020-01-01")], "2021-01-01")
    assert slim[0].get("slim") and all(l.get("lane") == "saison" and l.get("strategy") == "fill" for l in slim[0]["legs"]), "les jambes allégées gardent leur voie"
    # tout-fill des AUTRES voies : toujours fantôme
    other = [{"key": f"o{i}|totals||2.5|Over", "id": f"o{i}", "strategy": "fill", "market": "totals", "prob": 0.7, "xodds": 1.4} for i in range(3)]
    assert fair.unsent(other), "sharpgap / stats : tout-fill = fantôme"
    print("ok  combiné de remplissage de la voie saison : ENVOYÉ « sans valeur », jambes `fill` (lane saison), mise plancher de la voie, suivi ; les autres voies gardent « tout-fill = fantôme »")


def test_no_automatic_changes_and_no_leak():
    legs = setup()
    plan = [(t, c) for t, c in CB.season_plan(legs) if c]
    stakes, fc = {}, {}
    app.build_day([], DAY, pronos=[], fair_combos=plan, fair_stakes=stakes, fair_coupons=fc, odds_time="09:30")
    app.record_fair_morning(plan, DAY, stakes, fc, "09:30")
    rows = [r for r in fair.load_combos() if CB.season_no(r["tier"]) is not None]
    for r in rows:
        r["legs"] = [dict(l, result="win") for l in r["legs"]]
        r["result"] = "win"
    from kelly import settle
    assert settle._combo_legs(rows) == [], "la voie ne nourrit pas la prudence par jambe des autres combinés"
    assert app._tier_range("ss10") == (10 * (1 - C.SEASON_SPREAD), 10 * (1 + C.SEASON_SPREAD), None)
    with N.Fakes() as fk:
        n = app._update_combos(str(DAY), [], NOW, {l["id"] for r in rows for l in r["legs"]})
        assert n == 0 and not fk.sent, (n, fk.sent)            # sans pronostic de remplacement, un combiné du matin serait déclaré mort : pas une ligne « ss<N> »
    assert all(not r.get("dead") and r.get("result") is None for r in fair.load_combos() if CB.season_no(r["tier"]) is not None)
    print("ok  passages horaires : une ligne « ss<N> » n'est jamais remplacée tout seul ; ses jambes n'apprennent pas la prudence des autres combinés")


def test_commands():
    legs = setup()
    today = {"day": str(DAY), "season": legs, "picks": [], "pronos": [], "xbet_at": "09:30"}
    st = app.season_command([], today, now=NOW)
    assert "Saison en cours" in st and f"{len(legs)} (" in st and "saison 10" in st, st
    store.save("today.json", today)
    mute = lambda events, **kw: {}      # noqa: E731 - 1xBet muet : la reconstruction à la demande (tests/test_season_rebuild.py) retombe sur les jambes du matin
    reply = app.season_command(["10"], today, now=NOW, fetch=mute)
    txt = str(reply)
    assert "SAISON EN COURS · cote 10" in txt and "1xBet n'a rien rendu" in txt and "🔎 0 match lu" in txt, txt[:500]
    labels = [b["text"] for row in reply.markup["inline_keyboard"] for b in row]
    assert any("Je le joue" in t for t in labels) and not any("Un autre" in t for t in labels), labels      # « 🔀 Un autre » sortirait de la voie
    from kelly import custom
    items = custom._load(str(DAY))["items"]
    assert items and list(items.values())[-1]["src"] == "saison" and all(l.get("lane") == "saison" for l in list(items.values())[-1]["legs"]), items
    bad = app.season_command(["abc"], today, now=NOW)
    assert "Exemple" in bad
    C.STRATEGY_SEASON = "off"
    try:
        assert "éteinte" in app.season_command(["10"], today, now=NOW, fetch=mute)
    finally:
        C.STRATEGY_SEASON = "on"
    none = app.season_command(["10000"], today, now=NOW, fetch=mute)
    assert "Aucun combiné" in str(none) and "plus haute cote" in str(none), none
    assert app.chat.route("saison 10")[0] == "/saison 10"
    print("ok  commandes : « saison » (état), « saison N » (combiné à la cote N, bouton « Je le joue »), cote impossible expliquée, voie éteinte dite")


def test_sval_persisted_and_stake_isolation():
    legs = setup()
    plan = [(t, c) for t, c in CB.season_plan(legs) if c]
    stakes, fc = {}, {}
    app.build_day([], DAY, pronos=[], fair_combos=plan, fair_stakes=stakes, fair_coupons=fc, odds_time="09:30")
    app.record_fair_morning(plan, DAY, stakes, fc, "09:30")
    rows = [r for r in fair.load_combos() if CB.season_no(r["tier"]) is not None]
    assert rows and all(l.get("sval") in (0, 1) for r in rows for l in r["legs"]), "sval est gardé dans le suivi"
    assert all(lanes.item_fields(r["legs"]).get("frac") == C.KELLY_FRACTION_SEASON for r in rows), "relu plus tard, un combiné à valeur reste « avec valeur »"
    # étanchéité : les mises des autres voies sont exactement celles d'avant (la voie est allouée à part, sur le reste du budget)
    top = {"prob": 0.7, "odds": 1.6, "key": "k"}
    alone = app.day_stakes(top, [], [])[0]
    with_season = app.day_stakes(top, [], plan)[0]
    assert alone["pari"] == with_season["pari"] > 0, (alone, with_season)
    assert sum(v for k, v in with_season.items() if isinstance(k, tuple)) <= money.daily() - with_season["pari"]
    step = money.round_step(money.daily())
    assert all(abs(v / step - round(v / step)) < 1e-9 for v in with_season.values()), "mises alignées sur le pas du jour entier"
    print("ok  suivi : `sval` conservé (« avec valeur » relu plus tard) ; mises des autres voies identiques avec ou sans la voie saison (allocation à part sur le reste du budget)")


def test_live_recheck_helpers():
    legs = setup()
    store.save("today.json", {"day": str(DAY), "picks": [], "pronos": [], "season": legs})
    ids = {legs[0]["id"], legs[1]["id"]}
    got_px = {}

    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None):
        assert live is True
        out = {}
        for k, ev in events:
            c = next(c for c in legs if c["id"] == ev["id"])
            px = {(c["market"], c["name"], c["point"]): c["xodds"] * 1.10, (c["market"], "Over" if c["name"] == "Under" else "Under", c["point"]): 2.5}
            out[ev["id"]] = {"gid": 1, "px": px, "refs": {k_: [1, 2, 3] for k_ in px}}
        return out
    pool, got, hhmm = SL.live_check(ids, NOW, fetch=fetch)
    assert got == ids and len(pool) == 2 and all(abs(p["xodds"] / next(c for c in legs if c["key"] == p["key"])["xodds"] - 1.10) < 1e-9 for p in pool), pool
    assert all(p["sval"] in (0, 1, None) and p["xref"] == [1, 2, 3] for p in pool)
    # un pari qui n'existe plus : retiré du résultat ; le combiné est refait par season_patch avec des remplaçants
    base_combo = CB.season_plan(legs, [3])[0][1]
    assert base_combo
    res = CB.season_patch(base_combo[0], pool=[dict(c, sval=1) for c in legs if c["id"] != base_combo[0][0]["id"]], lo=2.4, hi=3.6, fresh_ids={c["id"] for c in legs})
    assert res and base_combo[0][0]["key"] in {l["key"] for l in res[3]} and len(res[0]) >= 2 and 2.4 <= res[1] <= 3.6 * 1.3, res[1]
    # rien relu : la jambe reste telle quelle
    assert CB.season_patch(base_combo[0], [], 2.4, 3.6, fresh_ids=set())[0] == base_combo[0]
    # un ticket noté : ses paris sont vérifiés sur le pool de la voie (jamais celui des autres voies)
    row = {"played": {"odds": 2.9, "stake": 100}, "coupon": "ABC"}
    with N.Fakes():
        txt = str(app._check_ticket(row, base_combo[0], str(DAY), "ss3", NOW, check=lambda i, n, fresh_min=None: (pool, ids, "10:00")))
    assert "toujours chez 1xBet" in txt or "n'est plus proposé" in txt
    # bouton « 🎟️ Vérifier ce combiné » sous un combiné saison NON noté : relu sur la voie, remplacé par des jambes de la voie seulement
    plan = [(t, c) for t, c in CB.season_plan(legs) if c]
    app.build_day([], DAY, pronos=[], fair_combos=plan, odds_time="09:30")
    app.record_fair_morning(plan, DAY, {}, {}, "09:30")
    tier = plan[0][0]
    row_legs = plan[0][1][0]
    gone_id = row_legs[0]["id"]
    seen = {}

    def chk(ids, now, fresh_min=None):
        seen["ids"] = set(ids)
        return [dict(c, sval=1) for c in legs if c["id"] in ids and c["id"] != gone_id], set(ids), "10:30"
    with N.Fakes():
        out = str(app.refresh_combo(tier, str(DAY), now=NOW, check=chk))
    assert "remis aux cotes 1xBet de 10:30" in out and "SAISON" in out.upper() or "COMBINÉ" in out, out[:300]
    new_row = next(r for r in fair.load_combos() if r["tier"] == tier and r["date"] == str(DAY))
    assert all(l.get("strategy") == "saison" for l in new_row["legs"]) and gone_id not in {l["id"] for l in new_row["legs"]}, [l["id"] for l in new_row["legs"]]
    print("ok  relecture en direct : jambes remises au prix, jambe disparue remplacée par une jambe de la voie, rien relu = rien changé, ticket noté vérifié sur la voie")


def main():
    for t in (test_plan_tiers_and_ranges, test_value_first_then_fill, test_usage_rules, test_item_fields_and_cap, test_morning_section_and_record, test_fill_only_is_ghost, test_fill_only_is_sent,
              test_no_automatic_changes_and_no_leak, test_sval_persisted_and_stake_isolation, test_live_recheck_helpers, test_commands):
        t()
    print("tests voie saison (combinés) : OK")


if __name__ == "__main__":
    main()
