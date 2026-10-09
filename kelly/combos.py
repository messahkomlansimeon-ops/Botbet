"""Jambes de qualité, réutilisation et combinés à cote fixe.

Un combiné est défini par sa seule fourchette de cote : le nombre de matchs n'est jamais une contrainte (4, 7 ou 20, peu
importe). Parmi toutes les jambes de qualité du jour, `best_combo` cherche la combinaison la plus probable dont la cote
totale tombe dans la fourchette (programmation dynamique sur le log des cotes, une jambe au plus par match).

Plancher de qualité (config.LEG_*) : une jambe sans raison n'entre dans aucun combiné.
  valeur : proba x cote typique - 1 >= LEG_MIN_EDGE x (1 + FEW_BOOKS / nb de books)
  stats  : le modèle d'avant-match (c["pm"]) voit au moins LEG_PM_GAP de proba de plus que les books
  sûr    : proba >= LEG_SAFE_PROB
Réutilisation d'un match dans plusieurs combinés du jour : jambe sûre jusqu'à LEG_REUSE_MAX combinés, jambe risquée une fois.
Espérance (proba x cote) : la marge du bookmaker se multiplie à chaque jambe. Un combiné sous C.COMBO_MIN_EV est rebâti avec les
seules jambes de valeur positive (espérance du produit >= 1) ; s'il n'y en a pas assez, le palier saute.
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


def ev(combo):
    """Espérance d'un combiné (jambes, cote, proba) : ce que rend 1 u misée en moyenne (1 = à l'équilibre)."""
    return combo[1] * combo[2]


def best_with_ev(legs, min_odds, max_odds=None):
    """best_combo, puis, si son espérance est sous C.COMBO_MIN_EV, le même avec les seules jambes de valeur positive.
    -> (combiné | None, rejeté pour l'espérance ?)."""
    combo = best_combo(legs, min_odds, max_odds)
    if not combo or ev(combo) >= C.COMBO_MIN_EV:
        return combo, False
    combo = best_combo([c for c in legs if c.get("edge", 0) >= 0], min_odds, max_odds)
    return (combo, False) if combo and ev(combo) >= C.COMBO_MIN_EV else (None, True)


def make_combo(pool, min_odds, max_odds=None, max_legs=None):
    """Alias de best_combo (ancien nom)."""
    return best_combo(pool, min_odds, max_odds, max_legs)


# ---------- la journée ----------

def plan(picks, tiers=None, usage=None, rejected=None):
    """Combinés du jour, palier par palier (dans l'ordre de config.COMBOS) : [(titre, combiné | None)]. Un palier sans
    assez de jambes de qualité, ou d'espérance négative (best_with_ev), n'est pas bâti (None) ; `rejected` reçoit les titres
    sautés pour l'espérance. `usage` est complété : les paris d'un combiné sont réutilisables selon les règles de
    réutilisation (sûres jusqu'à LEG_REUSE_MAX combinés, risquées une seule fois)."""
    usage = usage if usage is not None else Usage()
    out = []
    for title, lo, hi in (tiers if tiers is not None else C.COMBOS):
        legs = usage.filter(pool_for(title, picks))
        combo, neg = best_with_ev(legs, lo, hi)
        if neg and rejected is not None:
            rejected.append(title)
        if combo:
            usage.add(combo[0])
        out.append((title, combo))
    return out


def custom(picks, target, usage=None):
    """/combi <cote> : combiné à la cote visée (± C.CUSTOM_TOLERANCE) sur les jambes de qualité encore disponibles ; celui
    d'espérance positive s'il existe, sinon le plus probable (demandé explicitement : l'appelant prévient)."""
    legs = (usage or Usage()).filter(make_pool(picks))
    lo, hi = target * (1 - C.CUSTOM_TOLERANCE), target * (1 + C.CUSTOM_TOLERANCE)
    combo, _ = best_with_ev(legs, lo, hi)
    return combo or best_combo(legs, lo, hi)


def ev_line(rejected):
    """« Pas de Méchant/Monstre : espérance négative, la marge du bookmaker mangerait le gain. »"""
    from . import fmt
    names = "/".join(fmt.tier_word(t) for t in rejected)
    return f"Pas de {names} : espérance négative aujourd'hui (la marge du bookmaker, multipliée à chaque pari, mangerait le gain)."


def skip_line(missing, n_events, anything=True):
    """Une seule ligne quand des paliers ne sont pas bâtis : « Journée trop pauvre : pas de Méchant/Monstre aujourd'hui
    (7 matchs avec cotes). »"""
    from . import fmt
    names = "/".join(fmt.tier_word(t) for t in missing)
    what = f"pas de {names}" if anything else "ni pari ni combiné de qualité"
    return f"Journée trop pauvre : {what} aujourd'hui ({n_events} matchs avec cotes)."


# ---------- combinés à cote juste (pronostics sans cote de bookmaker) ----------
# Jambes = pronostics de kelly/fair.py : proba Kelly et « cote minimale » (cote juste x 1,05) à exiger chez 1xBet. Même
# optimiseur que les combinés réels, appliqué aux cotes minimales : le combiné a une « cote mini » (produit des cotes
# minimales) et une proba. Jamais mélangé aux combinés à cotes réelles (autres jambes, autres messages, autre suivi) et
# jamais de mise calculée sur la bankroll : seulement une mise indicative.

def fair_eligible(c):
    """Pronostic assez solide pour un combiné : proba Kelly >= FAIR_COMBO_MIN_P, qualité des données >= FAIR_COMBO_MIN_Q."""
    return (c.get("prob", 0) >= C.FAIR_COMBO_MIN_P and c.get("conf", C.FAIR_COMBO_MIN_Q) >= C.FAIR_COMBO_MIN_Q
            and c.get("min_odds", 0) > 1.0)


def fair_legs(pronos):
    """Jambes d'un combiné à cote juste : les pronostics solides, avec la cote minimale comme « cote » de l'optimiseur."""
    return [c | {"odds": c["min_odds"]} for c in pronos if fair_eligible(c)]


def fair_stake(min_total):
    """Mise indicative (unités, pas la bankroll) : plus la cote mini est haute, plus elle est petite."""
    return next(s for limit, s in C.FAIR_COMBO_STAKE if min_total < limit)


def fair_plan(pronos, tiers=None, usage=None):
    """Combinés à cote juste du jour, un par palier de C.FAIR_COMBO_TIERS (5, 12, 25 de cote mini) quand la matière le permet :
    [(palier, (jambes, cote mini, proba) | None)]. Les matchs déjà pris sont réutilisables selon les règles des combinés réels."""
    usage = usage if usage is not None else Usage()
    legs = fair_legs(pronos)
    out = []
    for target, lo, hi in (tiers if tiers is not None else C.FAIR_COMBO_TIERS):
        combo = best_combo(usage.filter(legs), lo, hi)
        if combo:
            usage.add(combo[0])
        out.append((target, combo))
    return out


def fair_shots_plan(pronos, tiers=None):
    """Combinés « spécial tirs » à vérifier chez 1xBet (C.FAIR_SHOTS_TIERS) : l'un sur les tirs cadrés, l'autre sur les tirs vers le
    but, un pari par match, mêmes jambes solides que les autres combinés à cote juste : [(palier, combiné | None)]."""
    out = []
    for target, prefix, _, lo, hi in (tiers if tiers is not None else C.FAIR_SHOTS_TIERS):
        legs = [c for c in fair_legs(pronos) if c["market"].startswith(prefix)]
        out.append((target, best_combo(legs, lo, hi)))
    return out


def shots_title(target):
    """Titre d'un combiné « spécial tirs » (« 🎯 COMBINÉ TIRS CADRÉS »), None pour les autres combinés à cote juste."""
    return next((t for k, _, t, _, _ in C.FAIR_SHOTS_TIERS if k == target), None)


def fair_custom(pronos, target, usage=None):
    """/combi <cote> sans cotes réelles : combiné à cote juste dont la cote mini est au moins la cote visée (jusqu'à
    + C.CUSTOM_TOLERANCE) : on ne promet jamais moins que demandé, puisque c'est un minimum."""
    legs = (usage or Usage()).filter(fair_legs(pronos))
    return best_combo(legs, target, target * (1 + C.CUSTOM_TOLERANCE))


def fair_shortfall(pronos, target):
    """Ce qui manque pour un combiné à cote juste de cote `target` : {matches, legs, best_total, need} (matchs avec un
    pronostic solide, meilleure cote mini atteignable avec tous, nombre approximatif de matchs nécessaires)."""
    legs = fair_legs(pronos)
    best = {}
    for c in legs:
        if c["id"] not in best or c["prob"] > best[c["id"]]["prob"]:
            best[c["id"]] = c
    total = math.prod(c["odds"] for c in best.values()) if best else 1.0
    mean = math.exp(sum(math.log(c["odds"]) for c in best.values()) / len(best)) if best else 1.5
    need = math.ceil(math.log(target) / math.log(max(mean, 1.05)))
    return {"matches": len(best), "legs": len(legs), "best_total": total, "need": need}
