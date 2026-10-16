"""Voie `saison`, paliers ADAPTATIFS (SEASON_TIERS=auto, hors ligne, pool fictif) : jour pauvre = un seul combiné, jour riche = plusieurs, aucune jambe partagée (envoyée ou en fantôme), espérance jamais sous le
plancher, cote <= plafond, une jambe par match, arrêt à la première jambe qui ne passe pas, cotes fixes toujours possibles (SEASON_TIERS=3,10 et « saison N »), relance sans doublon, mise plafonnée dès la
cote 25, bilan par tranche de cote. Lancer : python -m tests.test_season_auto"""
import math
import random

from tests import test_notes_fcfa as N      # fixe DATA_DIR (dossier temporaire), monde de test
from kelly import app, combos as CB, config as C, fair, lanestats, money, store  # noqa: E402


def leg(i, p, odds, sval=True, match=None):
    m = match if match is not None else i
    return {"id": f"m{m}", "key": f"m{m}|mk{i}|saison", "lane": "saison", "strategy": "saison", "sval": sval, "prob": p, "odds": odds, "xodds": odds, "xref": f"r{i}",
            "commence": f"2026-10-15T{10 + i % 10:02d}:00:00Z", "market": "totals", "sel": "over"}


def pool(n, base_p=0.60, base_odds=1.75, fill=0):
    """n jambes à valeur (p x cote ~ 1,05 +) et `fill` jambes de remplissage (p x cote ~ 0,95)."""
    legs = [leg(i, base_p + 0.002 * i, base_odds + 0.01 * i) for i in range(n)]
    return legs + [leg(n + j, 0.70, 1.36, sval=False) for j in range(fill)]


def setup():
    N.reset()
    C.SEASON_AUTO, C.SEASON_AUTO_MIN_EV, C.SEASON_AUTO_MAX_ODDS, C.SEASON_AUTO_MAX_COMBOS = True, -0.15, 150.0, 4
    C.SEASON_SEND_FILL = True
    store.save("fair_combos.json", [])
    store.save(fair.GHOST_COMBOS_FILE, [])


def ev_of(legs):
    return math.prod(c["prob"] * c["odds"] for c in legs) - 1


def check_invariants(plan):
    seen = set()
    for t, (legs, odds, prob) in plan:
        assert CB.season_auto(t) and CB.season_no(t) is not None, t
        assert len(legs) >= 2 and len({c["id"] for c in legs}) == len(legs), "au moins 2 jambes, une par match"
        assert not (seen & {c["id"] for c in legs}), "aucune jambe partagée entre deux combinés"
        seen |= {c["id"] for c in legs}
        assert ev_of(legs) >= C.SEASON_AUTO_MIN_EV - 1e-9 and odds <= C.SEASON_AUTO_MAX_ODDS + 1e-9, (t, ev_of(legs), odds)
        assert abs(odds - math.prod(c["odds"] for c in legs)) < 1e-9 and 0 < prob < 1
    assert len({t for t, _ in plan}) == len(plan), "paliers tous différents"


def test_poor_and_rich_days():
    setup()
    C.SEASON_AUTO_MAX_ODDS = 6.0                     # plafond serré : la taille d'un combiné est bornée par la cote
    poor = CB.season_plan(pool(3))
    check_invariants(poor)
    assert len(poor) == 1 and 2 <= len(poor[0][1][0]) <= 3, [(t, len(c[0])) for t, c in poor]
    rich = CB.season_plan(pool(30))
    check_invariants(rich)
    assert len(rich) == 4, [t for t, _ in rich]
    C.SEASON_AUTO_MAX_ODDS = 150.0
    print("ok  jour pauvre : un seul combiné ; jour riche : quatre ; aucune jambe partagée, espérance >= plancher, cote <= plafond, paliers distincts")


def test_stop_rules():
    setup()
    # la 1re jambe qui viole la cote maximale ouvre le combiné suivant (jamais sautée)
    C.SEASON_AUTO_MAX_ODDS = 4.0
    legs = [leg(0, 0.7, 1.9), leg(1, 0.7, 1.9), leg(2, 0.7, 1.9), leg(3, 0.7, 1.9)]
    plan = CB.season_plan(legs)
    check_invariants(plan)
    assert [len(c[0]) for _, c in plan] == [2, 2], plan                  # 1,9 x 1,9 = 3,61 ; une 3e jambe = 6,9 > 4
    # moins de 2 jambes utilisables : rien
    assert CB.season_plan([leg(0, 0.7, 1.9)]) == []
    # une seule jambe par match : la meilleure des deux est prise
    two = CB.season_plan([leg(0, 0.7, 1.9, match=0), leg(1, 0.8, 1.8, match=0), leg(2, 0.7, 1.9)])
    assert two and len({c["id"] for c in two[0][1][0]}) == 2
    best = [c for c in two[0][1][0] if c["id"] == "m0"][0]
    assert best["prob"] * best["odds"] == max(0.7 * 1.9, 0.8 * 1.8), best
    # espérance sous le plancher : le combiné s'arrête avant la jambe qui le fait passer dessous
    C.SEASON_AUTO_MAX_ODDS = 150.0
    C.SEASON_AUTO_MIN_EV = -0.05
    mixed = CB.season_plan([leg(0, 0.6, 1.9), leg(1, 0.6, 1.9), leg(2, 0.6, 1.9, sval=False), leg(3, 0.5, 1.7, sval=False)])
    check_invariants(mixed)
    assert all(ev_of(c[0]) >= -0.05 - 1e-9 for _, c in mixed)
    C.SEASON_AUTO_MIN_EV = -0.15
    # plafond de combinés
    C.SEASON_AUTO_MAX_COMBOS = 2
    C.SEASON_AUTO_MAX_ODDS = 4.0
    assert len(CB.season_plan([leg(i, 0.7, 1.9) for i in range(10)])) == 2
    print("ok  arrêt à la première jambe qui ne passe pas, moins de 2 jambes = rien, une jambe par match, plancher d'espérance, plafond de combinés")


def test_min_probability_default():
    """Décision du 15/10 : SEASON_AUTO_MIN_P = 0,25 par défaut ; aucun combiné auto ne descend sous 25 % de chances (produit des p des jambes)."""
    assert C.SEASON_AUTO_MIN_P == 0.25, "défaut du code (pas un réglage de .env)"
    random.seed(7)
    for nv, nf in ((3, 0), (8, 17), (25, 55), (40, 30)):
        setup()
        legs = []
        for i in range(nv):
            p = random.uniform(0.50, 0.78)
            legs.append(leg(i, p, round(random.uniform(1.02, 1.10) / p, 2)))
        for j in range(nf):
            p = random.uniform(0.60, 0.80)
            legs.append(leg(nv + j, p, max(round(random.uniform(0.92, 0.99) / p, 2), 1.15), sval=False))
        plan = CB.season_plan(legs)
        check_invariants(plan)
        for _, (cl, odds, _) in plan:
            assert math.prod(c["prob"] for c in cl) >= 0.25 - 1e-9, (nv, nf, odds, [round(c["prob"], 2) for c in cl])
    # éteint : le premier combiné absorbe les jambes à valeur (comportement littéral de la spécification)
    setup()
    legs = [leg(i, 0.7, 1.7) for i in range(12)]
    C.SEASON_AUTO_MIN_P = 0.0
    try:
        free = CB.season_plan(legs)
    finally:
        C.SEASON_AUTO_MIN_P = 0.25
    kept = CB.season_plan(legs)
    assert len(free[0][1][0]) > 4 and len(kept) == 4 and all(len(c[0]) <= 3 for _, c in kept), ([len(c[0]) for _, c in free], [len(c[0]) for _, c in kept])
    print("ok  SEASON_AUTO_MIN_P = 0,25 par défaut : aucun combiné auto sous 25 % de chances (jours pauvres et riches fictifs), 0 l'éteint")


def test_on_demand_big_ticket_capped():
    setup()
    money.set_daily(50000)
    legs = [leg(i, 0.6, 1.9) for i in range(5)]
    big = app._budget_stake(0.12, 25.0, legs=legs)
    assert 0 < big <= money.LOTTERY_STAKE, big
    assert app._budget_stake(0.4, 4.0, legs=legs) > money.LOTTERY_STAKE
    print("ok  « saison 30 » / « saison 100 » à la demande : mise plafonnée à 100 FCFA dès la cote 25")


def test_fill_legs_switch_and_row_item():
    setup()
    C.SEASON_AUTO_MAX_ODDS = 5.0
    C.FILL_LEGS = False
    try:
        plan = CB.season_plan(pool(2, fill=8))
    finally:
        C.FILL_LEGS = True
    assert all(c.get("sval") for _, cb in plan for c in cb[0]), "FILL_LEGS éteint : aucune jambe de remplissage"
    row = {"date": "2026-10-15", "tier": "ssa40.2", "min_odds": 40.2, "prob": 0.02, "legs": pool(2)}
    assert money.row_item(row).get("lottery") is True and not money.row_item(dict(row, tier="ss40", min_odds=40.2)).get("lottery")
    assert not money.row_item(dict(row, min_odds=4.2)).get("lottery")
    print("ok  FILL_LEGS éteint respecté ; le plafond loterie est rejoué pour une ligne de suivi « ssa » dès la cote 25")


def test_fill_after_value_and_ghost_no_share():
    setup()
    C.SEASON_AUTO_MAX_ODDS = 5.0
    C.SEASON_SEND_FILL = False                          # remplissage seul = fantôme (ensemble.unsent) : ses jambes restent à lui, jamais reprises par un autre combiné
    plan = CB.season_plan(pool(4, fill=6))
    check_invariants(plan)
    firsts = [c["sval"] for c in plan[0][1][0]]
    assert all(firsts), "valeur d'abord"
    C.SEASON_SEND_FILL = True
    print("ok  valeur d'abord, remplissage ensuite ; les jambes d'un combiné fantôme ne sont pas reprises")


def test_fixed_tiers_still_work():
    setup()
    legs = pool(30)
    plan = CB.season_plan(legs, [3, 10])
    assert [t for t, _ in plan] == ["ss3", "ss10"] and not any(CB.season_auto(t) for t, _ in plan)
    C.SEASON_AUTO = False
    try:
        assert [t for t, _ in CB.season_plan(legs)] == [f"ss{x}" for x in C.SEASON_TIERS]
    finally:
        C.SEASON_AUTO = True
    assert CB.season_no("ssa4.2") == 4.2 and CB.season_no("ss10") == 10 and CB.season_no("ssa") is None and not CB.season_auto("ss10") and CB.season_auto("ssa4.2")
    print("ok  cotes fixes toujours possibles (SEASON_TIERS=3,10 / saison N), palier « ssa4.2 » reconnu comme voie saison")


def test_resend_no_duplicates():
    setup()
    legs = pool(12)
    plan = CB.season_plan(legs)
    rows = [{"date": "2026-10-15", "tier": t, "keys": [c["key"] for c in cb[0]], "legs": [{k: c.get(k) for k in ("xodds", "xref", "odds", "min_odds")} for c in cb[0]],
             "min_odds": cb[1], "prob": cb[2]} for t, cb in plan]
    store.save("fair_combos.json", rows)
    again = app._recorded_season("2026-10-15")
    assert set(again) == {t for t, _ in plan}
    C.STRATEGY_SEASON = "on"
    out = app.season_plan_all(legs, "2026-10-15", CB.Usage())
    assert [t for t, _ in out] == [t for t, _ in plan] and all(c for _, c in out), "relance : mêmes combinés, mêmes paliers, rien de nouveau"
    print("ok  relance du matin : les combinés du jour sont renvoyés tels quels, sans nouveaux paliers")


def test_lottery_stake_and_buckets():
    setup()
    money.set_daily(20000)
    items = [{"kind": "fair", "tier": 40.0, "prob": 0.2, "odds": 40.0, "frac": 0.15, "lottery": True},
             {"kind": "fair", "tier": 4.0, "prob": 0.6, "odds": 4.0, "frac": 0.15}]
    st = money.allocate(items, budget=20000)[0]
    assert 0 < st[0] <= money.LOTTERY_STAKE and st[1] > money.LOTTERY_STAKE, st
    assert [CB.odds_bucket(x) for x in (2, 4.99, 5, 14.9, 15, 49, 50, 300)] == ["< 5", "< 5", "5-15", "5-15", "15-50", "15-50", "> 50", "> 50"] and CB.odds_bucket("x") is None
    print("ok  mise plafonnée comme un ticket loterie dès la cote 25 ; tranches de cote < 5 / 5-15 / 15-50 / > 50")


def main():
    test_poor_and_rich_days()
    test_stop_rules()
    test_min_probability_default()
    test_on_demand_big_ticket_capped()
    test_fill_legs_switch_and_row_item()
    test_fill_after_value_and_ghost_no_share()
    test_fixed_tiers_still_work()
    test_resend_no_duplicates()
    test_lottery_stake_and_buckets()
    print("tests saison auto : OK")


if __name__ == "__main__":
    main()
