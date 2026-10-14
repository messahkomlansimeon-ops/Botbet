"""Objectif du constructeur de combinés (combos._best_combo) : COMBO_OBJECTIVE = prob (défaut, d'avant) | ev ; plafond de jambes de remplissage COMBO_MAX_FILL_SHARE posé DANS la programmation dynamique.
Lancer : python -m tests.test_combo_objective"""
import itertools
import math
import os
import random
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-objective-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-objective-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "COMBO_OBJECTIVE", "COMBO_MAX_FILL_SHARE"):
    os.environ.pop(k, None)

from kelly import combos as CB, config as C  # noqa: E402


def leg(i, p, o, **kw):
    return {"key": f"e{i}|totals||2.5|Over", "id": f"e{i}", "prob": p, "odds": o, "market": "totals", "commence": "2026-10-14T18:00:00Z"} | kw


def keys(combo):
    return sorted(c["key"] for c in combo[0])


def with_objective(mode, fn):
    old = C.COMBO_OBJECTIVE
    C.COMBO_OBJECTIVE = mode
    try:
        return fn()
    finally:
        C.COMBO_OBJECTIVE = old


def test_defaults_and_equal_ev():
    assert C.COMBO_OBJECTIVE == "prob" and C.COMBO_MAX_FILL_SHARE == 1.0
    legs = [leg(i, 0.5, 2.0) for i in range(6)]            # même espérance (0 %) partout
    a = with_objective("prob", lambda: CB._best_combo(legs, 3.5, 4.5))
    b = with_objective("ev", lambda: CB._best_combo(legs, 3.5, 4.5))
    assert a and b and keys(a) == keys(b) and abs(a[1] - b[1]) < 1e-9, (a, b)
    print("ok  mêmes jambes et même cote quand toutes les jambes ont la même espérance")


def test_ev_moves_only_the_odds_inside_the_window():
    """Sur une cote totale FIXE, maximiser P x O revient à maximiser P : « ev » ne change que la case de cote choisie dans la fourchette (preuve par force brute sur des jambes aléatoires)."""
    rnd = random.Random(3)
    legs = [leg(i, rnd.uniform(0.5, 0.9), round(rnd.uniform(1.15, 2.4), 2)) for i in range(12)]
    for lo, hi in ((4.0, 5.0), (6.0, 7.5), (3.0, 3.6)):
        a = with_objective("prob", lambda: CB._best_combo(legs, lo, hi))
        b = with_objective("ev", lambda: CB._best_combo(legs, lo, hi))
        assert a and b and lo <= b[1] <= hi and b[1] * b[2] >= a[1] * a[2] - 1e-9 and a[2] >= b[2] - 1e-9, (lo, hi, a[1:], b[1:])
    print("ok  « ev » : espérance au moins égale à celle de « prob », probabilité au plus égale, cote toujours dans la fourchette")


def test_prefers_positive_ev_leg():
    """Deux paires de jambes à la même probabilité-produit proche ; la fourchette laisse choisir : « ev » prend la paire la mieux payée."""
    safe_low = [leg(1, 0.80, 1.30), leg(2, 0.80, 1.30)]            # P 0,64, cote 1,69, espérance +8 %
    paid = [leg(3, 0.60, 1.95), leg(4, 0.60, 1.95)]                 # P 0,36, cote 3,80, espérance +37 %
    legs = safe_low + paid
    a = with_objective("prob", lambda: CB._best_combo(legs, 1.5, 4.0))
    b = with_objective("ev", lambda: CB._best_combo(legs, 1.5, 4.0))
    assert keys(a) == keys(CB._best_combo(safe_low, 1.5, 4.0)), a
    assert b[1] * b[2] > a[1] * a[2] and b[1] > a[1], (a[1:], b[1:])
    print("ok  « ev » préfère le combiné mieux payé quand la fourchette le permet, « prob » le plus probable")


def test_one_leg_per_event_and_fill_cap():
    legs = [leg(1, 0.7, 1.5, strategy="fill"), leg(2, 0.7, 1.5, strategy="fill"), leg(3, 0.7, 1.5, strategy="fill"), leg(4, 0.7, 1.5, strategy="fill"),
            leg(5, 0.6, 1.5), leg(6, 0.6, 1.5), leg(5, 0.9, 1.5, key="e5|btts||None|Yes", market="btts")]
    free = CB._best_combo(legs, 5.0, 5.2)
    assert sum(1 for c in free[0] if c.get("strategy") == "fill") >= 3, "sans plafond : les jambes de remplissage plus probables dominent"
    C.COMBO_MAX_FILL_SHARE = 0.5
    try:
        capped = CB._best_combo(legs, 5.0, 5.2)
        assert capped and CB.fill_ok(capped[0]), capped
        assert sum(1 for c in capped[0] if CB.is_fill_leg(c)) * 2 <= len(capped[0])
        assert len({c["id"] for c in capped[0]}) == len(capped[0]), "une jambe par match conservée"
        # la voie saison n'a pas de plafond
        sea = [dict(c, lane="saison") for c in legs]
        assert sum(1 for c in CB._best_combo(sea, 5.0, 5.2)[0] if c.get("strategy") == "fill") >= 3
        # impossible sous le plafond : pas de combiné plutôt qu'un mauvais combiné
        only_fill = [leg(i, 0.7, 1.5, strategy="fill") for i in range(6)]
        assert CB._best_combo(only_fill, 5.0, 5.2) is None
        # exact : même optimum que la force brute sous plafond
        rnd = random.Random(5)
        pool = [leg(i, rnd.uniform(0.5, 0.9), round(rnd.uniform(1.2, 2.0), 2), **({"strategy": "fill"} if i % 2 else {})) for i in range(10)]
        got = CB._best_combo(pool, 4.0, 5.0)
        best = max((math.prod(c["prob"] for c in cmb) for k in range(2, 7) for cmb in itertools.combinations(pool, k)
                    if 4.0 <= math.prod(c["odds"] for c in cmb) <= 5.0 and CB.fill_ok(list(cmb))), default=None)
        assert (got is None) == (best is None) and (got is None or math.prod(c["prob"] for c in got[0]) >= best * 0.97), (got and got[2], best)
    finally:
        C.COMBO_MAX_FILL_SHARE = 1.0
    print("ok  plafond de remplissage dans la programmation dynamique : respecté, une jambe par match, saison exemptée, pas de combiné plutôt qu'un mauvais, proche de la force brute")


def test_ev_with_max_legs_and_exact_cap():
    """« ev » respecte « max N » (pas de regroupement 2+ jambes) ; sous plafond, l'optimum exact est pris même si les meilleurs candidats le dépassent."""
    legs = [leg(i, 0.85, 1.25) for i in range(8)] + [leg(20, 0.9, 1.9), leg(21, 0.9, 1.9)]            # jambes à espérance positive : en ajouter augmente l'espérance
    got = with_objective("ev", lambda: CB._best_combo(legs, 1.5, 6.0, max_legs=2))
    assert got and len(got[0]) == 2 and 1.5 <= got[1] <= 6.0, got
    pile = with_objective("ev", lambda: CB._best_combo(legs, 1.5, 6.0, max_legs=2, min_legs=2))
    assert pile and len(pile[0]) == 2
    C.COMBO_MAX_FILL_SHARE = 0.5
    try:
        pool = [leg(i, 0.7 + i / 100, 1.5, strategy="fill") for i in range(4)] + [leg(10 + i, 0.55, 1.5) for i in range(4)]
        exact = max((math.prod(c["prob"] for c in cmb) for k in range(2, 7) for cmb in itertools.combinations(pool, k)
                     if 5.0 <= math.prod(c["odds"] for c in cmb) <= 5.3 and CB.fill_ok(list(cmb))), default=None)
        got = CB._best_combo(pool, 5.0, 5.3)
        assert (got is None) == (exact is None) and (got is None or math.prod(c["prob"] for c in got[0]) >= exact * 0.97)
    finally:
        C.COMBO_MAX_FILL_SHARE = 1.0
    print("ok  « ev » avec max_legs, plafond exact (jamais un combiné légal mais moins bon)")


def main():
    test_defaults_and_equal_ev()
    test_ev_moves_only_the_odds_inside_the_window()
    test_prefers_positive_ev_leg()
    test_one_leg_per_event_and_fill_cap()
    test_ev_with_max_legs_and_exact_cap()
    print("tests objectif des combinés : OK")


if __name__ == "__main__":
    main()
