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


def x_playable(c, real):
    """Pari réel jouable au prix que 1xBet paie (xprice.price_picks) : dès que 1xBet a chiffré au moins un pari du jour (`real`), seuls
    ceux qui ont leur cote 1xBet (c["xodds"]) servent : jamais la cote typique des autres books. Sans aucune cote 1xBet (relevé en
    panne) : comme avant, sauf un pari déjà déclaré absent de 1xBet (xnone, xmiss)."""
    return bool(c.get("xodds")) or not (real or c.get("xnone") or c.get("xmiss"))


def quality_legs(picks, special=False):
    """Les jambes qui ont une raison d'être (pour le palier spécial : seulement corners, cartons et stats joueurs), aux cotes 1xBet
    quand 1xBet a chiffré les paris du jour (x_playable) : leur valeur est alors calculée à la cote 1xBet."""
    real = any(c.get("xodds") for c in picks)
    return [c for c in picks if (not special or mispriced(c)) and x_playable(c, real) and leg_reason(c)]


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
    if not 0 < min_odds <= max_odds or max_odds <= 1.0:      # un combiné d'au moins 2 jambes (cote > 1 chacune) ne tient jamais sous 1 : pas de calcul
        return None
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

def viable(c):
    """Pronostic sans cote assez solide pour être montré ou joué (C.VIABLE_*) : assez de chances après correction de fiabilité, assez
    de matchs de données pour les deux équipes, données de qualité suffisante, match plus favorable au pari qu'un match moyen du
    championnat (c["base"], modèle approfondi), et marché ou championnat où Kelly ne s'est pas montrée trop sûre d'elle."""
    from . import calib
    if c.get("prob", 0) < C.VIABLE_MIN_P or c.get("conf", 1.0) < C.VIABLE_MIN_Q:
        return False
    if c.get("n_min") is not None and c["n_min"] < C.VIABLE_MIN_N:
        return False
    if c.get("base") is not None and c["prob"] < c["base"]:
        return False
    if c.get("xodds") and c["xodds"] < c.get("min_odds", 0):              # 1xBet paie moins que la cote mini de Kelly : pas joué
        return False
    if c.get("xnone"):                                                    # match trouvé chez 1xBet, mais ce pari n'y existe pas
        return False
    return calib.factor(c["market"], sport_key=c.get("sport_key")) >= C.VIABLE_MIN_K


def best_per_match(pronos):
    """Le meilleur pronostic viable de chaque match, un seul par match, dans l'ordre du classement. Dès que le relevé 1xBet du jour a
    donné des cotes (kelly/xprice.py), seulement les pronostics jouables à leur cote 1xBet (c["xodds"]) : rien d'inventé ; sans
    aucune cote 1xBet (relevé en panne), les pronostics « à vérifier » (cote mini)."""
    real = any(c.get("xodds") for c in pronos)
    best = {}
    # à la cote 1xBet : le pari du match qui rapporte le plus (valeur x proba, comme xprice.choose) ; sinon le mieux classé
    for c in sorted(pronos, key=lambda c: -(c["xev"] * c["prob"] if real and c.get("xodds") else c.get("score", 0))):
        if c["id"] not in best and viable(c) and (c.get("xodds") or not real):
            best[c["id"]] = c
    return list(best.values())


def fair_eligible(c):
    """Pronostic assez solide pour un combiné : proba Kelly >= FAIR_COMBO_MIN_P, qualité des données >= FAIR_COMBO_MIN_Q."""
    return (c.get("prob", 0) >= C.FAIR_COMBO_MIN_P and c.get("conf", C.FAIR_COMBO_MIN_Q) >= C.FAIR_COMBO_MIN_Q
            and c.get("min_odds", 0) > 1.0)


def fair_legs(pronos):
    """Jambes d'un combiné à cote juste : les pronostics solides. Dès que le relevé 1xBet du jour a donné des cotes, SEULEMENT ceux
    qui ont leur cote 1xBet (c["xodds"], jamais sous la cote mini) : Kelly n'invente pas de cote quand 1xBet en donne. Sans aucune
    cote 1xBet (relevé en panne) : la cote minimale à exiger, « à vérifier »."""
    real = any(c.get("xodds") for c in pronos)
    return [c | {"odds": c.get("xodds") or c["min_odds"]} for c in pronos
            if fair_eligible(c) and viable(c) and (c.get("xodds") or not real)]


def _fair_best(legs, lo, hi):
    """Combiné à cote juste : d'abord avec les seules jambes aux cotes 1xBet relevées par Kelly (rien d'inventé), sinon avec toutes."""
    real = [c for c in legs if c.get("xodds")]
    return best_combo(real, lo, hi) or (best_combo(legs, lo, hi) if len(real) < len(legs) else None)     # mêmes jambes : inutile de refaire


def _carried(l, by_key):
    """Jambe d'un combiné dont le match n'a pas été relu : telle quelle (cote et référence 1xBet enregistrées), avec la clé « odds »
    attendue par l'optimiseur (« xref » repris du pronostic du jour si la jambe enregistrée n'en a pas et que sa cote est la même)."""
    o = by_key.get(l.get("key")) or {}
    ref = l.get("xref") or (o.get("xref") if o.get("xodds") == l.get("xodds") else None)
    return l | {"odds": l.get("odds") or l.get("xodds") or l.get("min_odds")} | ({"xref": ref} if ref else {})


def patch_combo(legs, pool, lo, hi, fresh_ids=None, prefix=None, usage=None):
    """Combiné `legs` remis aux cotes du moment : `pool` = pronostics à jour (xprice.live_check). Une jambe dont le match a été relu
    reste si ce match a encore un pari viable du même marché à sa cote 1xBet (la ligne a pu changer : « changées ») ; sinon elle sort.
    Une jambe dont le match n'a PAS été relu (`fresh_ids` : matchs relus à l'instant seulement) n'est jamais retirée : reprise telle
    quelle. Cote totale encore >= `lo` : on garde les survivants (même au-dessus de `hi` : les cotes ont monté) ; sinon ils sont
    complétés par le meilleur combiné des remplaçants (`prefix` : marchés permis, combinés spécial tirs) pour revenir dans [lo, hi],
    sans jamais dépasser `hi`. `usage` (Usage des AUTRES combinés du jour) : un remplaçant déjà pris ailleurs n'est repris que si les règles de
    réutilisation le permettent (jambe sûre, jamais une jambe risquée prise deux fois) ; les survivants ne sont pas concernés.
    -> (jambes, cote, proba, retirées, ajoutées, changées [(ancienne, nouvelle)]) ou None si impossible."""
    fresh = {}
    for c in fair_legs(pool):
        if c.get("xodds") and (fresh_ids is None or c["id"] in fresh_ids) and (not prefix or c["market"].startswith(prefix)):
            k = (c["id"], c["market"])
            if k not in fresh or c["prob"] > fresh[k]["prob"]:
                fresh[k] = c
    by_key = {c.get("key"): c for c in pool}
    keep, removed, changed = [], [], []
    for l in legs:
        if fresh_ids is not None and l["id"] not in fresh_ids:
            keep.append(_carried(l, by_key))
            continue
        c = fresh.get((l["id"], l["market"]))
        if not c:
            removed.append(l)
            continue
        keep.append(c)
        if (l.get("name"), l.get("point")) != (c["name"], c["point"]):
            changed.append((l, c))
    total = math.prod(c["odds"] for c in keep) if keep else 1.0
    if fresh_ids is not None and not fresh_ids:                 # rien relu : rien à juger
        return keep, total, math.prod(c["prob"] for c in keep), [], [], []
    added = []
    if not keep or total < lo:
        used = {c["id"] for c in keep}
        spares = [c for c in fresh.values() if c["id"] not in used and (usage is None or usage.allowed(c))]
        rlo, rhi = max(lo / total, 1.01), hi / total
        if rhi < rlo:
            return None
        one = max((c for c in spares if rlo <= c["odds"] <= rhi), key=lambda c: c["prob"], default=None)   # un seul remplaçant suffit ?
        extra = ([one], one["odds"], one["prob"]) if one else best_combo(spares, rlo, rhi)               # sinon plusieurs (2 au moins)
        if not extra:
            return None
        added = extra[0]
        keep += added
        total = math.prod(c["odds"] for c in keep)
    return keep, total, math.prod(c["prob"] for c in keep), removed, added, changed


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
        combo = _fair_best(usage.filter(legs), lo, hi)
        if combo:
            usage.add(combo[0])
        out.append((target, combo))
    return out


def fair_shots_plan(pronos, tiers=None, usage=None):
    """Combinés « spécial tirs » à vérifier chez 1xBet (C.FAIR_SHOTS_TIERS) : l'un sur les tirs cadrés, l'autre sur les tirs vers le
    but, un pari par match, mêmes jambes solides que les autres combinés à cote juste : [(palier, combiné | None)]. `usage` : celui de fair_plan
    (les mêmes règles de réutilisation d'un match dans tous les combinés du matin : une jambe risquée n'est dans qu'un seul), complété ici."""
    usage = usage if usage is not None else Usage()
    out = []
    for target, prefix, _, lo, hi in (tiers if tiers is not None else C.FAIR_SHOTS_TIERS):
        legs = [c for c in usage.filter(fair_legs(pronos)) if c["market"].startswith(prefix)]
        combo = _fair_best(legs, lo, hi)
        if combo:
            usage.add(combo[0])
        out.append((target, combo))
    return out


def shots_title(target):
    """Titre d'un combiné « spécial tirs » (« 🎯 COMBINÉ TIRS CADRÉS »), None pour les autres combinés à cote juste."""
    return next((t for k, _, t, _, _ in C.FAIR_SHOTS_TIERS if k == target), None)


def fair_custom(pronos, target, usage=None):
    """/combi <cote> sans cotes réelles : combiné à cote juste dont la cote mini est au moins la cote visée (jusqu'à
    + C.CUSTOM_TOLERANCE) : on ne promet jamais moins que demandé, puisque c'est un minimum."""
    legs = (usage or Usage()).filter(fair_legs(pronos))
    return _fair_best(legs, target, target * (1 + C.CUSTOM_TOLERANCE))


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
