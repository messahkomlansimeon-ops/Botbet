"""Combiné SANS valeur (toutes jambes fill) : construit et journalisé en fantôme (ghost_combos.json), NON envoyé (matin réel, matin à vérifier, fenêtre des stats), une ligne le dit au
message du matin ; NOVALUE_SEND le réaffiche (« sans valeur », jamais de mise conseillée). Jambes `live` : étiquette « live » hors voies, dimensionnement d'avant, comptées comme
étiquetées, visibles à part (bilan par voie, CLV par jambe) ; règle d'alerte live (200 jambes, IC sous zéro : on revient vers l'utilisateur, aucune coupure automatique). Hors ligne.
Lancer : python -m tests.test_novalue_send"""
from datetime import date

from tests import test_notes_fcfa as N      # fixe DATA_DIR (dossier temporaire), monde de test, réglages des jeux d'essai
from kelly import app, config as C, ensemble as E, fair, lanes, lanestats, money, statswindow, store, xprice  # noqa: E402

DAY = N.DAY
TODAY = date.today().isoformat()


def priced(n, odds):
    ps = [N.unpriced(i) for i in range(n)]
    xprice.apply(ps, fetch=N.fake_fetch({f"m{i}": {("totals", "Under", 3.5): odds} for i in range(n)}))
    return ps


def setup():
    C.NOVALUE_SEND = False                              # le défaut du bot (les jeux d'essai partagés épinglent True pour exercer leurs messages)
    N.reset()
    money.set_daily(5000)
    store.save(fair.GHOST_COMBOS_FILE, [])
    store.save("fair_combos.json", [])


def test_unsent_rules():
    setup()
    fills, values = priced(4, 1.30), priced(4, 1.55)
    assert [c["strategy"] for c in fills] == ["fill"] * 4 and [c["strategy"] for c in values] == ["sharpgap"] * 4
    assert fair.unsent(fills) and not fair.unsent(values) and not fair.unsent(fills[:2] + values[:2]), "il suffit d'une jambe à valeur pour être envoyé"
    assert not fair.unsent([{"key": "old"}]) and not fair.unsent([]), "voies inconnues (avant les étiquettes) : envoyé comme avant"
    C.NOVALUE_SEND = True
    try:
        assert not fair.unsent(fills), "NOVALUE_SEND : réaffiché"
    finally:
        C.NOVALUE_SEND = False
    C.ENSEMBLE_ENABLED = False
    try:
        assert not fair.unsent(fills), "ensemble éteint : comportement d'avant"
    finally:
        C.ENSEMBLE_ENABLED = True
    print("ok  combiné sans valeur : seulement si TOUTES les jambes sont fill (voies connues) ; NOVALUE_SEND et ensemble éteint = comme avant")


def test_morning_not_sent():
    setup()
    legs = priced(4, 1.30)
    fair_combos = [(5, (legs, 1.3 ** 4, 0.72 ** 4))]
    msgs, sent_picks, sent_combos = app.build_day([], DAY, pronos=legs, fair_combos=fair_combos, odds_time="09:30")
    txt = "\n".join(str(m) for m in msgs)
    assert "VÉRIF" not in txt and "aux cotes 1xBet" not in txt and not sent_combos, txt[:300]
    assert "👻 1 combiné sans valeur suivi en fantôme, non envoyé" in txt, txt[:600]
    g = fair.load_ghost()
    assert len(g) == 1 and g[0]["kind"] == "matin à vérifier" and g[0]["has_value"] is False and all(l["strategy"] == "fill" for l in g[0]["legs"]), g
    assert not [r for r in fair.load_combos() if r.get("date") == DAY], "rien dans fair_combos.json (ni mise, ni coupon, ni bouton)"
    # une relance du même jour ne double pas le fantôme
    app.build_day([], DAY, pronos=legs, fair_combos=fair_combos, odds_time="09:40")
    assert len(fair.load_ghost()) == 1
    # un combiné avec une jambe à valeur part comme avant
    good = priced(4, 1.55)
    msgs2, _, _ = app.build_day([], DAY, pronos=good, fair_combos=[(5, (good, 1.55 ** 4, 0.72 ** 4))], odds_time="09:30")
    t2 = "\n".join(str(m) for m in msgs2)
    assert "1,55" in t2 and "sans valeur" not in t2, t2[:500]                                            # le combiné à valeur part comme avant, avec ses cotes
    assert not fair.load_ghost(), "mêmes paris devenus à valeur : leur ligne fantôme tombe (voir test de relance)"
    # NOVALUE_SEND : réaffiché avec « sans valeur », jamais de mise conseillée
    store.save(fair.GHOST_COMBOS_FILE, [])
    C.NOVALUE_SEND, stake_before, C.NOVALUE_STAKE = True, C.NOVALUE_STAKE, 0.0          # (les jeux d'essai épinglent une mise fixe de 50 : ici, le défaut)
    try:
        msgs3, _, _ = app.build_day([], DAY, pronos=legs, fair_combos=fair_combos, odds_time="09:30")
        t3 = "\n".join(str(m) for m in msgs3)
        assert "1,30" in t3 and "sans valeur : aucune jambe ne bat le marché" in t3 and "pas de mise conseillée" in t3 and not fair.load_ghost(), t3[:600]          # envoyé, avec la mention, rien en fantôme
        assert "FCFA → si ça passe" not in t3, "aucune mise conseillée"
        C.NOVALUE_STAKE = 50.0                                                          # mise fixe optionnelle : montant ET mention
        t4 = "\n".join(str(m) for m in app.build_day([], DAY, pronos=legs, fair_combos=fair_combos, odds_time="09:30")[0])
        assert "50 FCFA · sans valeur" in t4, t4[:700]
    finally:
        C.NOVALUE_SEND, C.NOVALUE_STAKE = False, stake_before
    print("ok  matin : le combiné sans valeur est suivi en fantôme, dit en une ligne, non envoyé ; relance sans doublon ; NOVALUE_SEND le réaffiche sans mise")


def test_stats_window_not_sent():
    setup()
    legs = priced(3, 1.30)
    sent = []
    real = statswindow.quiet.send
    statswindow.quiet.send = lambda *a, **k: sent.append(a)
    try:
        out = statswindow.send_combo(app, {"tier": "s1", "legs": legs, "total": 2.2, "prob": 0.3, "when": "09:30", "keys": sorted(l["key"] for l in legs)}, DAY, N.at(9))
    finally:
        statswindow.quiet.send = real
    assert out is None and not sent and len(fair.load_ghost()) == 1 and fair.load_ghost()[0]["kind"] == "fenêtre stats"
    assert not [r for r in fair.load_combos() if r.get("date") == DAY]
    print("ok  fenêtre des stats : combiné sans valeur suivi en fantôme, ni code coupon ni message")


def test_live_label():
    live_leg = {"key": "lv1|totals||1.5|Over", "id": "lv1", "strategy": "live", "xodds": 1.6, "market": "totals"}
    assert E.has_value([live_leg]) is None and lanes.item_fields([live_leg, live_leg | {"key": "lv2"}]) == {}, "dimensionnement live d'avant : ni fraction de voie ni « sans valeur »"
    assert lanes.item_fields([live_leg, {"strategy": "fill"}]) == {}
    store.save("fair_combos.json", [{"date": TODAY, "tier": "lc1", "result": "win", "legs": [dict(live_leg, result="win", min_odds=1.4), dict(live_leg, key="lv2|totals||2.5|Over", id="lv2", result="loss", min_odds=1.4)]}])
    n, total = lanestats.unlabeled()
    assert (n, total) == (0, 2), "les jambes live comptent comme étiquetées"
    lines = lanestats.lane_lines([], None)
    assert any(x.startswith("live : 1 gagnée sur 2") and "hors voies" in x and "CLV non mesurable (pas de clôture relevée), calibration 0/200" in x for x in lines), lines
    assert lanestats.live_alert() == "", "aucune mesure de clôture live : la règle ne peut pas se déclencher"
    cl = lanestats.leg_clv_lines()
    assert cl[0].startswith("jambes mesurées : 0 / ") and "live" not in cl[0] and "live (à part : hors voies) : 2 jambes, CLV non mesurable (pas de clôture relevée), calibration 0/200" in cl, cl      # visible à part, hors du ratio de couverture
    store.save("fair_combos.json", [])
    # règle d'alerte (calibration, aucune requête) : 200 jambes réglées, réalisé sous l'annoncé avec un IC par match qui exclut zéro -> message, jamais de coupure
    def book(n, wins_every, prob=0.8):
        legs = [dict(live_leg, key=f"g{i}|totals||1.5|Over", id=f"g{i // 2}", prob=prob, result="win" if i % wins_every else "loss") for i in range(n)]
        store.save("fair_combos.json", [{"date": TODAY, "tier": "lc1", "result": None, "legs": legs}])
    book(220, 3)                                         # ~67 % réalisés pour 80 % annoncés
    msg = lanestats.live_alert()
    assert msg.startswith("⚠️ Jambes live : réalisé 66 % contre 80 % annoncé") and "aucune coupure automatique" in msg and "sur 220 jambes" in msg, msg
    assert "calibration 220/200 : annoncé 80 %, réalisé 66 %" in lanestats.live_calib_text()
    book(199, 3)
    assert lanestats.live_alert() == "" and "calibration 199/200" in lanestats.live_calib_text() and "trop peu" in lanestats.live_calib_text(), "199 jambes : pas assez"
    book(220, 5)                                         # 80 % réalisés pour 80 % annoncés : calibré
    assert lanestats.live_alert() == "", lanestats.live_alert()
    book(220, 2, prob=0.4)                               # 50 % réalisés pour 40 % annoncés : sur-performance, pas d'alerte
    assert lanestats.live_alert() == ""
    store.save("fair_combos.json", [])
    print("ok  jambes live : étiquette hors voies, dimensionnement d'avant, comptées comme étiquetées, visibles à part ; alerte à 200 jambes sans coupure")


def test_run_daily_does_not_record_ghost_combos():
    """Régression de relecture : run_daily enregistrait TOUS les combinés à vérifier dans fair_combos.json (mise, coupon, bouton) ; un fantôme n'y entre jamais, un combiné envoyé si."""
    setup()
    fills, values = priced(4, 1.30), priced(4, 1.55)
    for i, c in enumerate(values):
        c["key"] += f"|v{i}"
    plan = [(5, (fills, 1.3 ** 4, 0.72 ** 4)), (8, (values, 1.55 ** 4, 0.72 ** 4))]
    stakes, coupons, gout = {}, {}, set()
    app.build_day([], DAY, pronos=fills + values, fair_combos=plan, fair_stakes=stakes, odds_time="09:30", fair_coupons=coupons, ghost_out=gout)
    assert gout == {"5"}, gout
    n = app.record_fair_morning(plan, DAY, stakes, coupons, "09:30", gout)
    rows = fair.load_combos()
    assert n == 1 and [str(r["tier"]) for r in rows if r["date"] == DAY] == ["8"], rows
    assert len(fair.load_ghost()) == 1 and {l["key"] for l in fills} == set(fair.load_ghost()[0]["keys"])
    print("ok  matin : le combiné sans valeur est en fantôme, jamais enregistré dans fair_combos.json ; le combiné à valeur l'est")


def test_relaunch_gaining_value_is_sent_and_recorded():
    """Revue du 14/10 (grave) : au matin 1 le combiné est fantôme ; à la relance les MÊMES paris ont gagné de la valeur (voie recalculée) : il est ENVOYÉ, enregistré dans fair_combos.json, et
    sa ligne fantôme tombe."""
    setup()
    fills = priced(4, 1.30)
    plan_fill = [(5, (fills, 1.3 ** 4, 0.72 ** 4))]
    gout = set()
    app.build_day([], DAY, pronos=fills, fair_combos=plan_fill, fair_stakes={}, odds_time="09:30", fair_coupons={}, ghost_out=gout)
    assert gout == {"5"} and len(fair.load_ghost()) == 1
    values = priced(4, 1.55)                                                           # mêmes clés, voie recalculée : sharpgap
    assert [c["key"] for c in values] == [c["key"] for c in fills]
    stakes, coupons, gout2 = {}, {}, set()
    msgs, _, _ = app.build_day([], DAY, pronos=values, fair_combos=[(5, (values, 1.55 ** 4, 0.72 ** 4))], fair_stakes=stakes, odds_time="10:30", fair_coupons=coupons, ghost_out=gout2)
    assert not gout2 and "1,55" in "\n".join(str(m) for m in msgs), "envoyé"
    assert app.record_fair_morning([(5, (values, 1.55 ** 4, 0.72 ** 4))], DAY, stakes, coupons, "10:30", gout2) == 1 and not fair.load_ghost(), "enregistré, plus fantôme"
    # relance toute-fill : la ligne 👻 reste dite (le fantôme déjà suivi compte), sans doublon
    setup()
    app.build_day([], DAY, pronos=fills, fair_combos=plan_fill, fair_stakes={}, odds_time="09:30", fair_coupons={}, ghost_out=set())
    msgs, _, _ = app.build_day([], DAY, pronos=fills, fair_combos=plan_fill, fair_stakes={}, odds_time="09:40", fair_coupons={}, ghost_out=set())
    assert "👻 1 combiné sans valeur" in "\n".join(str(m) for m in msgs) and len(fair.load_ghost()) == 1
    print("ok  relance : un combiné fantôme qui gagne de la valeur est envoyé et enregistré ; relance toute-fill : ligne 👻 gardée, pas de doublon")


def test_stats_window_looks_past_a_ghost():
    """Revue du 14/10 : le meilleur candidat de la fenêtre des stats est sans valeur -> suivi en fantôme puis écarté ; le suivant, avec une jambe à valeur, est proposé (pas de famine)."""
    setup()
    fills, values = priced(3, 1.30), priced(3, 1.55)
    for i, c in enumerate(values):
        c["key"] += f"|v{i}"
        c["id"] = f"v{i}"
    calls = []

    def fake_window(pronos, usage=None, accept=None):
        calls.append(1)
        pool = [c for c in fills + values if accept(c)]
        legs = [c for c in pool if c["strategy"] == "fill"][:3] if len(calls) == 1 else pool[:3]
        return (legs, 2.2, 0.3) if len(legs) == 3 else None
    real, realu, rin, rfr = statswindow.CB.fair_stats_window, app.upcoming, statswindow.in_window, statswindow.fresh
    statswindow.CB.fair_stats_window = fake_window
    app.upcoming = lambda pronos, now: pronos
    statswindow.in_window = statswindow.fresh = lambda c, now: True                      # la sélection de la fenêtre n'est pas ce qu'on teste ici
    try:
        plan = statswindow.plan_combo(app, {"day": DAY, "pronos": fills + values}, {"combos": []}, [], N.at(9))
    finally:
        statswindow.CB.fair_stats_window, app.upcoming, statswindow.in_window, statswindow.fresh = real, realu, rin, rfr
    assert plan is not None and any(l["strategy"] != "fill" for l in plan["legs"]), plan
    assert len(fair.load_ghost()) == 1 and fair.load_ghost()[0]["kind"] == "fenêtre stats"
    print("ok  fenêtre des stats : un candidat sans valeur est suivi en fantôme puis écarté, le suivant (avec valeur) est proposé")


def test_ghost_keys_that_gain_value_are_planned():
    setup()
    values = priced(3, 1.55)
    fair.ghost_record(5, (values, 2.2, 0.3), DAY, "fenêtre stats")                       # mêmes jambes suivies en fantôme à un passage précédent (sans valeur à l'époque)
    real, realu, rin, rfr = statswindow.CB.fair_stats_window, app.upcoming, statswindow.in_window, statswindow.fresh
    statswindow.CB.fair_stats_window = lambda pronos, usage=None, accept=None: (values, 2.2, 0.3)
    app.upcoming = lambda pronos, now: pronos
    statswindow.in_window = statswindow.fresh = lambda c, now: True
    try:
        plan = statswindow.plan_combo(app, {"day": DAY, "pronos": values}, {"combos": []}, [], N.at(9))
    finally:
        statswindow.CB.fair_stats_window, app.upcoming, statswindow.in_window, statswindow.fresh = real, realu, rin, rfr
    assert plan is not None and not fair.unsent(plan["legs"]), plan
    fair.ghost_drop(DAY, plan["keys"])
    assert fair.load_ghost() == [], "il part : plus un fantôme"
    print("ok  fenêtre des stats : des jambes d'abord en fantôme qui ont gagné de la valeur sont proposées, leur ligne fantôme tombe")


def test_ghost_does_not_consume_reuse():
    setup()
    from kelly import combos as CB
    fills, values = priced(4, 1.30), priced(4, 1.55)
    tiers = [(2, 2.0, 4.0), (3, 2.0, 4.0)]
    real_fl, CB.fair_legs = CB.fair_legs, lambda ps: [c | {"odds": c["xodds"]} for c in ps]      # la sélection des jambes n'est pas ce qu'on teste
    try:
        _ghost_reuse(CB, fills, values, tiers)
    finally:
        CB.fair_legs = real_fl


def _ghost_reuse(CB, fills, values, tiers):
    u = CB.Usage()
    plan = CB.fair_plan(fills, tiers=tiers, usage=u)
    assert [c for _, c in plan] and all(c and fair.unsent(c[0]) for _, c in plan), plan
    assert not u.by_leg, "combinés sans valeur (fantômes) : aucune réutilisation consommée, ni de quoi priver un combiné réel"
    u = CB.Usage()
    plan = CB.fair_plan(values, tiers=tiers[:1], usage=u)
    assert plan[0][1] and not fair.unsent(plan[0][1][0]) and u.by_leg, "un combiné avec valeur consomme comme avant"
    u = CB.Usage()
    plan = CB.fair_plan(values[:2] + fills[2:], tiers=tiers[:1], usage=u)
    assert plan[0][1] and u.by_leg, "il suffit d'une jambe à valeur pour consommer"
    u = CB.Usage()
    u.add(fills[:1])
    assert u.by_leg, "un pari seul (pari du jour, écart sharp) fill compte toujours : seul un COMBINÉ fantôme ne consomme rien"
    print("ok  un combiné fantôme ne consomme pas la réutilisation des jambes (Usage), un combiné envoyé oui")


def main():
    test_ghost_does_not_consume_reuse()
    test_ghost_keys_that_gain_value_are_planned()
    test_unsent_rules()
    test_morning_not_sent()
    test_stats_window_not_sent()
    test_run_daily_does_not_record_ghost_combos()
    test_relaunch_gaining_value_is_sent_and_recorded()
    test_stats_window_looks_past_a_ghost()
    test_live_label()
    print("tests combinés sans valeur / jambes live : OK")


if __name__ == "__main__":
    main()
