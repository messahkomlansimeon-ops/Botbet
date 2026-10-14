"""Plafond de réutilisation PAR JAMBE (match, marché, sélection, ligne) sur la journée : 1 ticket sous LEG_SAFE_PROB, LEG_REUSE_MAX dès LEG_SAFE_PROB, LEG_REUSE_VERY_SAFE dès LEG_VERY_SAFE_PROB ;
compté à chaque ticket ENVOYÉ (matin, fenêtre des stats, « combi N », « saison N », live), jamais pour un combiné fantôme ; une jambe au plafond est écartée et la suivante prise.
Lancer : python -m tests.test_leg_reuse"""
from tests import test_season_combos as S      # fixe DATA_DIR, monde de test, jambes `saison` fictives
from tests import test_combi_track as T
from kelly import app, combos as CB, config as C, custom, fair, store  # noqa: E402

DAY = T.DAY


def leg(i, prob, market="totals", **kw):
    return {"key": f"e{i}|{market}||2.5|Over", "id": f"e{i}", "market": market, "name": "Over", "point": 2.5, "prob": prob, "xodds": 1.9, "odds": 1.9, "min_odds": 1.5} | kw


def test_caps():
    assert (C.LEG_REUSE_MAX, C.LEG_REUSE_VERY_SAFE, C.LEG_SAFE_PROB, C.LEG_VERY_SAFE_PROB) == (3, 5, 0.68, 0.80)
    assert [CB.reuse_cap({"prob": p}) for p in (0.5, 0.679, 0.68, 0.79, 0.80, 0.95)] == [1, 1, 3, 3, 5, 5]
    u = CB.Usage()
    risky, safe, very = leg(1, 0.60), leg(2, 0.70), leg(3, 0.85)
    for n in range(1, 6):
        u.add([risky, safe, very])
        assert (u.allowed(risky), u.allowed(safe), u.allowed(very)) == (False, n < 3, n < 5), n
    assert not u.allowed(very) and u.allowed(leg(3, 0.85, market="btts", key="e3|btts||None|Yes")), "une AUTRE jambe du même match reste libre"
    assert u.allowed(leg(9, 0.5)) and CB.leg_id({"key": "e1|totals||2.5|Over|saison"}) == "e1|totals||2.5|Over", "l'étiquette de voie n'est pas une autre jambe"
    kept = CB.Usage()
    kept.add([risky])
    assert kept.filter([risky, safe]) == [safe] and set(kept.excluded) == {risky["key"]}
    print("ok  plafond par jambe : 1 / 3 / 5 selon la proba, une autre jambe du même match reste libre, filtre et jambes écartées dites")


def test_paths():
    S.setup()
    store.save(custom.FILE, {"day": DAY, "n": 0, "items": {}})
    store.save(fair.GHOST_COMBOS_FILE, [{"date": DAY, "kind": "saison", "legs": [{"key": "g|x", "id": "g"}], "keys": ["g|x"]}])
    # un combiné sur mesure ENVOYÉ (même non joué) compte ; un fantôme non
    a, b = leg(1, 0.60), leg(2, 0.85)
    custom.remember(DAY, 5.0, 4.0, 6.0, ([a, b], 3.6, 0.5))
    u = app.day_usage(DAY)
    assert u.by_leg[a["key"]] == 1 and u.by_leg[b["key"]] == 1 and "g|x" not in u.by_leg, u.by_leg
    assert not u.allowed(a) and u.allowed(b)
    # plan du matin (paris réels), combinés à cote juste, « combi N » : la jambe au plafond est écartée, la suivante prise
    pool = [leg(i, 0.60 + i / 100) for i in range(1, 9)]
    full = CB.fair_custom(pool, 3.0, usage=CB.Usage())
    blocked = CB.Usage()
    blocked.add([{"key": c["key"]} for c in full[0]])
    again = CB.fair_custom(pool, 3.0, usage=blocked)
    assert full and (not again or not {c["key"] for c in again[0]} & {c["key"] for c in full[0]}), "jambes de risque déjà prises : écartées"
    # saison N : même règle
    legs = S.make_legs()
    plan0 = CB.season_plan(legs, [3], usage=CB.Usage())[0][1]
    uu = CB.Usage()
    uu.add(plan0[0])
    plan1 = CB.season_plan(legs, [3], usage=uu)[0][1]
    assert plan0 and (not plan1 or not {CB.leg_id(c) for c in plan1[0]} & {CB.leg_id(c) for c in plan0[0] if c["prob"] < C.LEG_SAFE_PROB}), "saison : jambe risquée prise une fois"
    # remplaçant (live / vérification) : un remplaçant au plafond n'est pas repris
    spare = leg(7, 0.60)
    full_u = CB.Usage()
    full_u.add([spare])
    assert not full_u.allowed(spare) and CB.Usage().allowed(spare)
    # fenêtre des stats / combinés du matin : un seul Usage (day_usage), mêmes règles
    assert app._others_usage(fair.load_combos(), None, DAY, {"day": DAY, "tiers": {}}).by_leg.get(a["key"]) == 1
    print("ok  tous les chemins : combiné sur mesure envoyé compté (fantôme non), « combi N » et « saison N » écartent la jambe au plafond, remplaçant filtré, Usage du jour partagé")


def test_builders_filter():
    """Les constructeurs du matin (fair_plan, tirs), de la fenêtre des stats et le remplaçant de la vérification passent tous par Usage.filter : une jambe au plafond n'y entre pas."""
    real = CB.fair_legs
    legs = [leg(i, 0.60 + i / 100, market="team_shots_home", key=f"e{i}|team_shots_home||2.5|Over", xref=[1, 2, 3]) for i in range(1, 9)]
    CB.fair_legs = lambda pronos: list(legs)
    ran = 0
    try:
        for fn in (lambda u: CB.fair_plan(legs, tiers=[(5, 3.0, 6.0)], usage=u)[0][1], lambda u: CB.fair_stats_window(legs, usage=u),
                   lambda u: CB.fair_shots_plan(legs, tiers=[(5, "team_shots_", "tirs", 3.0, 6.0)], usage=u)[0][1]):
            first = fn(CB.Usage())
            if not first:
                continue
            ran += 1
            u = CB.Usage()
            u.add([{"key": c["key"]} for c in first[0] if c["prob"] < C.LEG_SAFE_PROB])
            second = fn(u)
            banned = {c["key"] for c in first[0] if c["prob"] < C.LEG_SAFE_PROB}
            assert not second or not banned & {c["key"] for c in second[0]}, (banned, [c["key"] for c in second[0]])
    finally:
        CB.fair_legs = real
    assert ran >= 2, ran
    print("ok  constructeurs du matin, des tirs et de la fenêtre des stats : la jambe au plafond est écartée")


def test_note():
    S.setup()
    store.save(custom.FILE, {"day": DAY, "n": 0, "items": {}})
    pronos = [T.prono(i, prob=0.60, xodds=1.9) for i in range(6)]
    assert app.reuse_note(DAY, [], pronos) == ""
    custom.remember(DAY, 5.0, 4.0, 6.0, (pronos[:2], 3.6, 0.5))
    note = app.reuse_note(DAY, [], pronos)
    assert "2 jambes écartées : déjà utilisées aujourd'hui" in note, note
    print("ok  le message dit « n jambes écartées : déjà utilisées aujourd'hui »")


def main():
    test_caps()
    test_paths()
    test_builders_filter()
    test_note()
    print("tests plafond de réutilisation par jambe : OK")


if __name__ == "__main__":
    main()
