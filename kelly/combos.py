"""Jambes de qualité, réutilisation et combinés à cote fixe.

Un combiné est défini par sa seule fourchette de cote : le nombre de matchs n'est jamais une contrainte (4, 7 ou 20, peu
importe). Parmi toutes les jambes de qualité du jour, `best_combo` cherche la combinaison la plus probable dont la cote
totale tombe dans la fourchette (programmation dynamique sur le log des cotes, une jambe au plus par match).

Plancher de qualité (config.LEG_*) : une jambe sans raison n'entre dans aucun combiné.
  valeur : proba x cote typique - 1 >= LEG_MIN_EDGE x (1 + FEW_BOOKS / nb de books)
  stats  : le modèle d'avant-match (c["pm"]) voit au moins LEG_PM_GAP de proba de plus que les books
  sûr    : proba >= LEG_SAFE_PROB
Réutilisation d'un match dans plusieurs combinés du jour : jambe sûre jusqu'à LEG_REUSE_MAX combinés, jambe risquée une fois.
"""
import math

from . import config as C, odds as O
from .prematch import mispriced

NEG = float("-inf")
EPS = 1e-4                  # à probabilité égale, on préfère moins de jambes


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


# ---------- plancher de qualité ----------

def is_safe(c):
    return c["prob"] >= C.LEG_SAFE_PROB


def stats_signal(c):
    """Le modèle d'avant-match soutient le pari : écart de proba positif, ou avantage de buts attendus au 1N2."""
    pm = c.get("pm") or {}
    if (pm.get("pgap") or 0) >= C.LEG_PM_GAP:
        return True
    return pm.get("kind") == "h2h" and c.get("name") != "Draw" and (pm.get("xgd") or 0) >= C.LEG_XGD


def leg_reason(c):
    """« value » | « stats » | « safe » : la raison d'être de la jambe ; None si elle n'en a aucune (ou si un garde-fou la
    refuse : cote trop mal payée, modèle d'avant-match contre)."""
    # Garde-fou de marge : une jambe sûre a une petite cote, où la marge du bookmaker pèse plus lourd en pourcentage
    # (1,35 au lieu de 1,44 juste = -6 %) ; elle a donc droit à un plancher plus bas que les autres.
    floor = C.LEG_SAFE_EDGE_FLOOR if is_safe(c) else C.LEG_EDGE_FLOOR
    if c.get("edge", 0) < floor or (c.get("pm") or {}).get("pgap", 0) <= -C.LEG_PM_GAP:
        return None
    if c["edge"] >= C.LEG_MIN_EDGE * O.few_books_factor(c.get("n_books", 10)):
        return "value"
    if stats_signal(c):
        return "stats"
    if is_safe(c):
        return "safe"
    return None


def quality_legs(picks, special=False):
    """Les jambes qui ont une raison d'être (pour le palier spécial : seulement corners, cartons et stats joueurs)."""
    return [c for c in picks if (not special or mispriced(c)) and leg_reason(c)]


def make_pool(picks):
    """Toutes les jambes de qualité du jour (pas de taille de pool : le nombre de candidats n'est pas une contrainte)."""
    return quality_legs(picks)


def make_special_pool(picks):
    return quality_legs(picks, special=True)


def pool_for(title, picks, pool=None):
    """Jambes d'un palier : le spécial a les siennes (marchés mal pricés), les autres toutes les jambes de qualité."""
    return make_special_pool(picks) if title == C.SPECIAL else (pool if pool is not None else make_pool(picks))


# ---------- réutilisation d'un match dans plusieurs combinés ----------

class Usage:
    """Combinés déjà bâtis aujourd'hui, par match : combien de fois il est pris et s'il l'a été avec une jambe risquée."""

    def __init__(self):
        self.by_event = {}                          # id du match -> {"n": nombre de combinés, "risky": bool}

    def allowed(self, c):
        u = self.by_event.get(c["id"])
        return u is None or (is_safe(c) and not u["risky"] and u["n"] < C.LEG_REUSE_MAX)

    def filter(self, legs):
        return [c for c in legs if self.allowed(c)]

    def add(self, legs):
        for c in legs:
            u = self.by_event.setdefault(c["id"], {"n": 0, "risky": False})
            u["n"] += 1
            u["risky"] = u["risky"] or not is_safe(c)

    @classmethod
    def from_sent(cls, combos, picks):
        """Usage reconstitué depuis les combinés déjà envoyés (clés des paris dans bets.json). Une jambe introuvable dans
        `picks` est comptée risquée : dans le doute, on ne la réutilise pas."""
        by_key, u = {c["key"]: c for c in picks}, cls()
        for combo in combos:
            for k in combo.get("keys", []):
                c = by_key.get(k)
                u.add([c if c else {"id": k.split("|")[0], "prob": 0.0}])
        return u


# ---------- optimiseur à cote fixe ----------

def _dp(items, weights, lps, bmax, kdim, saturate):
    """Programmation dynamique « sac à dos par groupes » : items = [[indices des jambes du match]]. dp[k][b] = meilleure
    somme de log-probas avec k jambes (k >= kdim - 1 regroupé si `saturate`) et un log-cote total de b pas. Renvoie (dp,
    choix par match) pour reconstruire."""
    dp = [[NEG] * (bmax + 1) for _ in range(kdim)]
    dp[0][0] = 0.0
    top = [0] + [-1] * (kdim - 1)                   # plus grand pas atteint par k (-1 : rien)
    choices = []
    for group in items:
        new = [row[:] for row in dp]
        ch = [[-1] * (bmax + 1) for _ in range(kdim)]
        ntop = top[:]
        for j in group:
            w, lp = weights[j], lps[j]
            for k in range(kdim):
                if top[k] < 0:
                    continue
                nk = min(k + 1, kdim - 1) if saturate else k + 1
                if nk >= kdim:
                    continue
                src, dst, cdst = dp[k], new[nk], ch[nk]
                hi = min(top[k], bmax - w)
                for b in range(hi + 1):
                    v = src[b]
                    if v != NEG and v + lp > dst[b + w]:
                        dst[b + w] = v + lp
                        cdst[b + w] = j * kdim + k
                ntop[nk] = max(ntop[nk], min(top[k] + w, bmax))
        dp, top = new, ntop
        choices.append(ch)
    return dp, choices


def _rebuild(choices, kdim, k, b, weights):
    out = []
    for ch in reversed(choices):
        c = ch[k][b]
        if c >= 0:
            j, k = divmod(c, kdim)
            out.append(j)
            b -= weights[j]
    return out


def best_combo(legs, min_odds, max_odds=None, max_legs=None, step=None):
    """Combiné de cote totale dans [min_odds, max_odds] qui maximise la probabilité (produit des probas), au plus une
    jambe par match, au moins 2 jambes ; `max_legs` (C.MAX_LEGS) n'est qu'une borne de sécurité. -> (jambes triées par
    heure, cote, proba) ou None. Optimisation exacte à la discrétisation près (pas `step` en log-cote), puis vérification de
    la vraie cote : jamais de combiné hors fourchette."""
    max_legs = max_legs or C.MAX_LEGS
    max_odds = max_odds or min_odds * 2
    legs = [c for c in legs if c["odds"] > 1.0 and 0 < c["prob"] <= 1]
    if len(legs) < 2:
        return None
    step = step or (C.DP_BIN if len(legs) > 40 else C.DP_BIN / 4)
    lo_l, hi_l = math.log(min_odds), math.log(max_odds)
    slack = max(2, int(0.02 / step))                 # marge de discrétisation, vérifiée ensuite sur les vraies cotes
    bmax = int(hi_l / step) + slack
    weights = [max(1, round(math.log(c["odds"]) / step)) for c in legs]
    lps = [math.log(c["prob"]) - EPS for c in legs]
    by_event = {}
    for j, c in enumerate(legs):
        by_event.setdefault(c["id"], []).append(j)
    items = list(by_event.values())
    lo_b = max(0, int(lo_l / step) - slack)

    def finish(dp, choices, kdim, ks):
        cands = sorted(((dp[k][b], b, k) for k in ks for b in range(lo_b, bmax + 1) if dp[k][b] != NEG), reverse=True)
        for _, b, k in cands[:60]:
            idx = _rebuild(choices, kdim, k, b, weights)
            if not 2 <= len(idx) <= max_legs:
                continue
            odds = math.prod(legs[j]["odds"] for j in idx)
            if min_odds <= odds <= max_odds:
                return [legs[j] for j in idx], odds, math.prod(legs[j]["prob"] for j in idx)
        return None

    dp, choices = _dp(items, weights, lps, bmax, 3, True)
    best = finish(dp, choices, 3, (2,))
    if best is None:
        # Les meilleurs combinés dépassent peut-être la borne de sécurité `max_legs` : on la compte alors dans l'état.
        kdim = max_legs + 1
        dp, choices = _dp(items, weights, lps, bmax, kdim, False)
        best = finish(dp, choices, kdim, range(2, kdim))
    if best is None:
        return None
    best[0].sort(key=lambda c: c.get("commence", ""))
    return best


def make_combo(pool, min_odds, max_odds=None, max_legs=None):
    """Alias de best_combo (ancien nom)."""
    return best_combo(pool, min_odds, max_odds, max_legs)


# ---------- la journée ----------

def plan(picks, tiers=None, usage=None):
    """Combinés du jour, palier par palier (dans l'ordre de config.COMBOS) : [(titre, combiné | None)]. Un palier sans
    assez de jambes de qualité n'est pas bâti (None). `usage` est complété : les paris d'un combiné sont réutilisables selon
    les règles de réutilisation (sûres jusqu'à LEG_REUSE_MAX combinés, risquées une seule fois)."""
    usage = usage if usage is not None else Usage()
    out = []
    for title, lo, hi in (tiers if tiers is not None else C.COMBOS):
        legs = usage.filter(pool_for(title, picks))
        combo = best_combo(legs, lo, hi)
        if combo:
            usage.add(combo[0])
        out.append((title, combo))
    return out


def custom(picks, target, usage=None):
    """/combi <cote> : combiné à la cote visée (± C.CUSTOM_TOLERANCE) sur les jambes de qualité encore disponibles."""
    legs = (usage or Usage()).filter(make_pool(picks))
    return best_combo(legs, target * (1 - C.CUSTOM_TOLERANCE), target * (1 + C.CUSTOM_TOLERANCE))


def skip_line(missing, n_events, anything=True):
    """Une seule ligne quand des paliers ne sont pas bâtis : « Journée trop pauvre : pas de Méchant/Monstre aujourd'hui
    (7 matchs avec cotes). »"""
    from . import fmt
    names = "/".join(fmt.tier_word(t) for t in missing)
    what = f"pas de {names}" if anything else "ni pari ni combiné de qualité"
    return f"Journée trop pauvre : {what} aujourd'hui ({n_events} matchs avec cotes)."
