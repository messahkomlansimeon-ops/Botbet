"""Pool de candidats et combinés."""
import itertools

from . import config as C
from .prematch import mispriced


def soft_market(c):
    return c["weight"] > 1.0


def per_event(picks, k=1):
    """Les `k` meilleurs paris de chaque match (un combiné n'en prend qu'un :
    les paris d'un même match sont corrélés)."""
    by_event = {}
    for c in sorted(picks, key=lambda c: c["score"], reverse=True):
        by_event.setdefault(c["id"], [])
        if len(by_event[c["id"]]) < k:
            by_event[c["id"]].append(c)
    return [c for cs in by_event.values() for c in cs]


def make_pool(picks, n=C.POOL_SIZE):
    """2 paris max par match, meilleurs scores, majorité de marchés mal pricés."""
    ranked = per_event(picks, k=2)
    soft = [c for c in ranked if soft_market(c)][:int(n * C.SOFT_SHARE)]
    rest = [c for c in ranked if c not in soft][:n - len(soft)]
    return soft + rest


def make_special_pool(picks, n=C.POOL_SIZE):
    """Pool du combiné spécial : uniquement les marchés mal pricés (corners, cartons, stats joueurs), 2 paris
    max par match, meilleurs scores."""
    return sorted(per_event([c for c in picks if mispriced(c)], k=2), key=lambda c: c["score"], reverse=True)[:n]


def pool_for(title, picks, pool):
    """Pool d'un palier : le spécial a le sien, les autres le pool commun."""
    return make_special_pool(picks) if title == C.SPECIAL else pool


def make_combo(pool, min_odds, max_odds=None, max_legs=C.MAX_LEGS):
    """Combiné (nombre de matchs libre) dans la fourchette de cote, proba max."""
    best = None
    for size in range(2, min(max_legs, len(pool)) + 1):
        for combo in itertools.combinations(pool, size):
            odds = prob = 1.0
            for c in combo:
                odds *= c["odds"]
                prob *= c["prob"]
            if odds < min_odds or (max_odds and odds > max_odds):
                continue
            if len({c["id"] for c in combo}) < size:
                continue
            if best is None or prob > best[2]:
                best = (list(combo), odds, prob)
    return best
