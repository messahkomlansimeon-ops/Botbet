"""Jambes de qualité, réutilisation et combinés à cote fixe.

Un combiné est défini par sa seule fourchette de cote : le nombre de matchs n'est jamais une contrainte (4, 7 ou 20, peu
importe). Parmi toutes les jambes de qualité du jour, `best_combo` cherche la combinaison la plus probable dont la cote
totale tombe dans la fourchette (programmation dynamique sur le log des cotes, une jambe au plus par match).

Plancher de qualité (config.LEG_*) : une jambe sans raison n'entre dans aucun combiné.
  valeur : proba x cote typique - 1 >= LEG_MIN_EDGE x (1 + FEW_BOOKS / nb de books)
  stats  : le modèle d'avant-match (c["pm"]) voit au moins LEG_PM_GAP de proba de plus que les books. Jamais sur une famille où l'ensemble
           décide (config.ENSEMBLE_ACTIVE_FAMILIES : buts, 1N2, double chance, les 2 marquent, buts d'une équipe), sauf si l'ensemble valide
           lui aussi le pari (ensemble.value) : les paris où le modèle s'écarte du marché y perdent 9 à 16 % ; jamais non plus sur un pari de
           buts / 1N2 d'une ligue où le modèle mesuré ne vaut pas le marché (prematch.goal_weight = 0)
  sûr    : proba >= LEG_SAFE_PROB
Réutilisation d'un match dans plusieurs combinés du jour : jambe sûre jusqu'à LEG_REUSE_MAX combinés, jambe risquée une fois.
Espérance (proba x cote) : la marge du bookmaker se multiplie à chaque jambe. Un combiné sous C.COMBO_MIN_EV est rebâti avec les
seules jambes de valeur positive (espérance du produit >= 1) ; s'il n'y en a pas assez, le palier saute.
"""
import math

from . import config as C, ensemble, odds as O
from .prematch import CARDS, CORNERS, goal_weight, mispriced

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


GOAL_PM_KINDS = ("goals", "btts", "h2h")            # c["pm"]["kind"] issus du modèle de buts (celui que tune.py mesure face au marché)


def stats_signal(c):
    """Le modèle d'avant-match soutient le pari : écart de proba positif, ou avantage de buts attendus au 1N2."""
    pm = c.get("pm") or {}
    if (pm.get("pgap") or 0) >= C.LEG_PM_GAP:
        return True
    return pm.get("kind") == "h2h" and c.get("name") != "Draw" and (pm.get("xgd") or 0) >= C.LEG_XGD


def stats_allowed(c):
    """La raison « stats » est-elle recevable pour `c` ? Faux pour un pari de buts / 1N2 dont le modèle de buts est mesuré sans valeur dans sa ligue
    (poids 0 : tune.py), et pour un pari d'une famille que l'ensemble décide, sauf si l'ensemble le valide aussi à la cote 1xBet (ensemble.value :
    proba retenue x cote >= 1 + marge, sans veto) : c'est le désaccord modèle-marché qui perd, pas le signal."""
    pm = c.get("pm") or {}
    if pm.get("kind") in GOAL_PM_KINDS and goal_weight(c) <= 0:
        return False
    if ensemble.is_active(c["market"]):
        if not c.get("xodds"):
            return False
        try:
            v = ensemble.value(c)
            return bool(v["ok"]) and v.get("kind") != "probable"       # « probable » (sans avis sharp) n'est pas une raison « stats »
        except Exception:                           # noqa: BLE001 - avis incomplets : l'ensemble ne valide pas
            return False
    return True


def leg_reason(c):
    """« value » | « stats » | « safe » : la raison d'être de la jambe ; None si elle n'en a aucune (ou si un garde-fou la
    refuse : cote trop mal payée, modèle d'avant-match contre)."""
    from . import lanes
    if not lanes.usable(c):                         # voie en « ghost » / « off », ligue de stats en fantôme, jambe « fill » coupée (FILL_LEGS) : jamais sélectionnée
        return None
    # Garde-fou de marge : une jambe sûre a une petite cote, où la marge du bookmaker pèse plus lourd en pourcentage
    # (1,35 au lieu de 1,44 juste = -6 %) ; elle a donc droit à un plancher plus bas que les autres.
    edge = c.get("edge")
    if edge is None:                                # pas de valeur calculée : pas de jambe (jamais un 0 inventé)
        return None
    floor = C.LEG_SAFE_EDGE_FLOOR if is_safe(c) else C.LEG_EDGE_FLOOR
    if edge < floor or ((c.get("pm") or {}).get("pgap", 0) <= -C.LEG_PM_GAP and not ensemble.model_silent(c["market"])):       # famille figée : le modèle ne refuse rien
        return None
    if edge >= C.LEG_MIN_EDGE * O.few_books_factor(c.get("n_books", 10)):
        return "value"
    if stats_signal(c) and stats_allowed(c):
        return "stats"
    if is_safe(c):
        return "safe"
    return None


STAT_FAMILIES = frozenset(("corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides", "player"))


def cross_league_stat(c):
    """Pari de stats (corners, cartons, tirs, fautes, touches, hors-jeu, joueurs) d'un match de coupe d'Europe, d'Amérique, de coupe nationale ou entre sélections
    (C.CROSS_LEAGUE_PREFIXES) : les moyennes de Kelly y viennent de championnats différents, sans correction de niveau (audit du 13/10). Jamais proposé."""
    from .calib import family
    return family(c.get("market") or "") in STAT_FAMILIES and str(c.get("sport_key") or "").startswith(C.CROSS_LEAGUE_PREFIXES)


def x_playable(c, real):
    """Pari réel jouable au prix que 1xBet paie (xprice.price_picks) : dès que 1xBet a chiffré au moins un pari du jour (`real`), seuls
    ceux qui ont leur cote 1xBet (c["xodds"]) servent : jamais la cote typique des autres books. Sans aucune cote 1xBet (relevé en
    panne) : comme avant, sauf un pari déjà déclaré absent de 1xBet (xnone, xmiss)."""
    return bool(c.get("xodds")) or not (real or c.get("xnone") or c.get("xmiss"))


def quality_legs(picks, special=False):
    """Les jambes qui ont une raison d'être (pour le palier spécial : seulement corners, cartons et stats joueurs), aux cotes 1xBet
    quand 1xBet a chiffré les paris du jour (x_playable) : leur valeur est alors calculée à la cote 1xBet."""
    real = any(c.get("xodds") for c in picks)
    return [c for c in picks if (not special or mispriced(c)) and x_playable(c, real) and not cross_league_stat(c) and leg_reason(c)]


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


def leg_discount(data=None):
    """Prudence par jambe d'un combiné : réussite / proba annoncée des jambes de combinés réglées (calib.json « _combo », vieilli), rapprochée de
    C.COMBO_LEG_START (0,95) tant qu'elles sont peu nombreuses (n / (n + C.COMBO_LEG_PRIOR)), bornée à C.COMBO_LEG_K. L'optimiseur choisit les jambes
    que Kelly juge les meilleures, donc celles qu'elle surestime le plus souvent : la correction par marché (calib) ne voit pas cet écart-là. Audit du
    13/10 : sur 189 jambes réglées (pronostics de Kelly), 4,6 % de réussite en moins qu'annoncé, d'où 5 % au départ (1 % avant). Ne vaut que pour les
    jambes aux probas de Kelly (leg_factor)."""
    from . import calib
    lo, hi = C.COMBO_LEG_K
    acc = (data if data is not None else calib.load()).get("_combo")
    d = C.COMBO_LEG_START
    if acc and acc[0] > 0 and acc[2] > 0:
        n = acc[0]
        d = d + (acc[1] / acc[2] - d) * n / (n + C.COMBO_LEG_PRIOR)
    return max(lo, min(hi, d))


def leg_factor(c, d):
    """Prudence d'une jambe : `d` (leg_discount, 5 % au départ) pour une proba de Kelly (modèle, ou mélange avec 1xBet : pronostics, combinés sur
    mesure), C.COMBO_LEG_K[1] (1 %) pour un pari réel dont la proba est celle des books de référence (c["prob_book"] : Pinnacle sans marge, rien à
    corriger de l'excès de confiance de Kelly)."""
    return C.COMBO_LEG_K[1] if "prob_book" in c else d


def combo_prob(legs, d=None):
    """Proba d'un combiné : produit des probas des jambes x leg_factor par jambe (5 % de prudence par jambe de Kelly au départ, 1 % pour un pari
    réel aux probas des books de référence : les erreurs se multiplient)."""
    d = leg_discount() if d is None else d
    return math.prod(c["prob"] * leg_factor(c, d) for c in legs)


def leg_market_p(c):
    """Chance d'une jambe selon 1xBet, sans sa marge : c["p_x"] (xprice, cotes de toutes les issues), sinon c["xp"], sinon 1 / (cote 1xBet x (1 +
    C.X_MARGIN_GUESS)) ; None sans cote 1xBet (jambe à cote mini, cote d'un autre bookmaker)."""
    p = c.get("p_x") if c.get("p_x") is not None else c.get("xp")
    try:
        if p is not None and 0 < float(p) <= 1:
            return float(p)
        x = float(c.get("xodds") or 0)
    except (TypeError, ValueError):
        return None
    return 1 / (x * (1 + C.X_MARGIN_GUESS)) if x > 1 else None


def market_prob(legs):
    """Chance d'un combiné selon 1xBet : produit des chances de ses jambes sans la marge (leg_market_p) ; None si une jambe n'en a pas. C'est l'avis du
    marché : audit du 13/10, les jambes jouées passaient 65 % du temps pour 71 % annoncés par Kelly et 61 % par 1xBet, et l'écart se multiplie avec le
    nombre de jambes (ticket de 8 jambes du 10/10 : 4,2 % selon Kelly, 0,6 % selon 1xBet)."""
    ps = [leg_market_p(c) for c in legs]
    return math.prod(ps) if ps and all(p is not None for p in ps) else None


def _trial_leg(c):
    """Jambe reprise PAR l'essai (tirs « plus de » qui ne passeraient pas la règle normale) : seules celles-là sont limitées par combiné."""
    from . import calib
    return calib.trial_ok(c) and calib.viable_factor(c["market"], sport_key=c.get("sport_key"), side=calib.side(c), p=c.get("p0"),
                                                     shown=c.get("prob")) < C.VIABLE_MIN_K


def best_combo(legs, min_odds, max_odds=None, max_legs=None, step=None, min_legs=None):
    """_best_combo, avec au plus C.SHOTS_TRIAL_MAX_LEGS jambes de l'essai (tirs vers le but « plus de ») : au-delà, seules les plus sûres restent et le
    combiné est refait."""
    res = _best_combo(legs, min_odds, max_odds, max_legs, step, min_legs)
    if not res or not C.SHOTS_TRIAL:
        return res
    trial = sorted((c for c in res[0] if _trial_leg(c)), key=lambda c: -c["prob"])
    if len(trial) <= C.SHOTS_TRIAL_MAX_LEGS:
        return res
    keep = {id(c) for c in trial[:C.SHOTS_TRIAL_MAX_LEGS]}
    return _best_combo([c for c in legs if not _trial_leg(c) or id(c) in keep], min_odds, max_odds, max_legs, step, min_legs)


def _best_combo(legs, min_odds, max_odds=None, max_legs=None, step=None, min_legs=None):
    """Combiné de cote totale dans [min_odds, max_odds] qui maximise la probabilité (combo_prob : produit des probas, prudence par jambe), au plus une
    jambe par match, au moins 2 jambes ; `max_legs` (C.MAX_LEGS par défaut, une borne de sécurité ; ou le nombre de paris choisi : « combi 20 max 4 »)
    et `min_legs` (« combi 20 pile 4 » : min = max) bornent le nombre de jambes. -> (jambes triées par heure, cote, proba) ou None. Optimisation exacte à
    la discrétisation près (pas `step` en log-cote), puis vérification de la vraie cote : jamais de combiné hors fourchette."""
    max_legs = max_legs or C.MAX_LEGS
    min_legs = max(2, min_legs or 2)
    if min_legs > max_legs:
        return None
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
    d = leg_discount()
    lps = [math.log(c["prob"]) + math.log(leg_factor(c, d)) - EPS for c in legs]      # prudence par jambe : à cote égale, moins de jambes (le nombre imposé reste respecté)
    by_event = {}
    for j, c in enumerate(legs):
        by_event.setdefault(c["id"], []).append(j)
    items = list(by_event.values())
    lo_b = max(0, int(lo_l / step) - slack)

    def finish(dp, choices, kdim, ks):
        cands = sorted(((dp[k][b], b, k) for k in ks for b in range(lo_b, bmax + 1) if dp[k][b] != NEG), reverse=True)
        for _, b, k in cands[:60]:
            idx = _rebuild(choices, kdim, k, b, weights)
            if not min_legs <= len(idx) <= max_legs:
                continue
            odds = math.prod(legs[j]["odds"] for j in idx)
            if min_odds <= odds <= max_odds:
                return [legs[j] for j in idx], odds, combo_prob([legs[j] for j in idx], d)
        return None

    dp, choices = _dp(items, weights, lps, bmax, 3, True)
    best = finish(dp, choices, 3, (2,)) if min_legs == 2 else None       # « pile N » : le nombre de jambes est compté dans l'état tout de suite
    if best is None:
        # Les meilleurs combinés dépassent peut-être la borne de sécurité `max_legs` : on la compte alors dans l'état.
        kdim = max_legs + 1
        dp, choices = _dp(items, weights, lps, bmax, kdim, False)
        best = finish(dp, choices, kdim, range(min_legs, kdim))
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
        if combo and not ensemble.unsent(combo[0]):   # un combiné SANS valeur (fantôme, jamais envoyé) ne consomme aucune réutilisation : il ne prive pas un combiné réel de ses jambes
            usage.add(combo[0])
        out.append((title, combo))
    return out


# ---------- types de paris d'un combiné sur mesure (« combi 20 tirs fautes », cases à cocher après la cote) ----------
# (code, libellé, préfixes de marché) : un type = des marchés qui commencent par l'un de ses préfixes ; plusieurs types = l'union. Les préfixes
# passent tels quels aux filtres existants (patch_combo, _live_fair : « prefix ») : « Vérifier ce combiné » ne remplace un pari que par un du même type.
KINDS = (("v", "Victoire", ("h2h",)), ("dc", "Double chance", ("double_chance",)), ("b", "Buts", ("totals", "team_totals")),
         ("btts", "Les 2 marquent", ("btts",)), ("co", "Corners", (CORNERS, "team_corners_")), ("ca", "Cartons", (CARDS, "team_yellow_")),
         ("t", "Tirs vers le but", ("team_shots_",)), ("tc", "Tirs cadrés", ("team_sot_",)), ("f", "Fautes", ("team_fouls_",)),
         ("to", "Touches", ("team_throws_",)), ("hj", "Hors-jeu", ("team_offsides_",)), ("j", "Joueurs", ("player_",)))
KIND_CODES = tuple(k for k, _, _ in KINDS)


def kinds_prefix(codes):
    """Préfixes de marché des types `codes` (tuple, pour str.startswith) ; None = tous les paris."""
    codes = set(codes or ())
    return tuple(p for k, _, ps in KINDS if k in codes for p in ps) or None


def kinds_label(codes):
    """« tirs vers le but, fautes » (ordre de KINDS) ; '' = tous les paris."""
    codes = set(codes or ())
    return ", ".join(label.lower() for k, label, _ in KINDS if k in codes)


def kind_of(market):
    """Code du type d'un marché (« team_fouls_total » -> « f ») ; None hors des types (handicap...)."""
    return next((k for k, _, ps in KINDS if market.startswith(ps)), None)


def _keep(legs, prefix):
    return legs if not prefix else [c for c in legs if c["market"].startswith(prefix)]


def custom(picks, target, usage=None, prefix=None, max_legs=None, min_legs=None):
    """/combi <cote> : combiné à la cote visée (± C.CUSTOM_TOLERANCE) sur les jambes de qualité encore disponibles ; celui
    d'espérance positive s'il existe, sinon le plus probable (demandé explicitement : l'appelant prévient). `prefix` : types de paris
    choisis (kinds_prefix), None = tous."""
    legs = _keep((usage or Usage()).filter(make_pool(picks)), prefix)
    lo, hi = target * (1 - C.CUSTOM_TOLERANCE), target * (1 + C.CUSTOM_TOLERANCE)
    if max_legs or min_legs:                        # nombre de paris choisi : l'optimiseur le respecte (espérance dite par l'appelant)
        return best_combo(legs, lo, hi, max_legs=max_legs, min_legs=min_legs)
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

def probable(c):
    """Pronostic de buts / 1N2 « probable » sans avis sharp (c["xkind"], xprice) : jamais « valeur », aucune mise tant que 1xBet sans marge ne lui donne pas d'espérance positive."""
    return c.get("xkind") == "probable"


def stake_prob(legs, prob):
    """Proba servant à calculer la MISE d'un combiné de `legs` dont la proba est `prob` : celle de Kelly, sauf pour une jambe « probable » (sans avis sharp), où elle est
    remplacée par la proba de 1xBet sans marge (c["p_x"]) : Kelly ne mise que s'il reste une espérance au prix du marché (cote x proba 1xBet - 1 > 0), en pratique
    presque jamais puisque 1xBet garde sa marge. Proba 1xBet inconnue pour une telle jambe : 0 (aucune mise). Les jambes sûres de valeur sont inchangées."""
    for c in legs:
        if probable(c):
            px = c.get("p_x") if c.get("p_x") is not None else c.get("xp")
            if not c.get("prob"):
                return 0.0
            prob *= min(1.0, (px or 0.0) / c["prob"])
    return prob


def viable(c):
    """Pronostic sans cote assez solide pour être montré ou joué (C.VIABLE_*) : assez de chances après correction de fiabilité, assez
    de matchs de données pour les deux équipes, données de qualité suffisante, match plus favorable au pari qu'un match moyen du
    championnat (c["base"], modèle approfondi), et marché ou championnat où Kelly ne s'est pas montrée trop sûre d'elle.
    Famille où l'ensemble décide (ensemble.is_active : buts, 1N2...) : c["prob"] est la proba RETENUE p_ens, donc le seuil C.VIABLE_MIN_P s'applique à elle ;
    le veto (c["xveto"]) et une cote 1xBet sous la cote mini relevée (c["min_odds"], (1 + marge) / p_ens) écartent le pari comme avant. La référence c["base"] est
    une proba du MODÈLE (un match moyen vu par Kelly) : elle se compare à la proba du modèle (c["p_model"]), pas à p_ens (voir ensemble.model_prob)."""
    from . import ensemble, lanes
    if not lanes.usable(c):                         # voie en « ghost » / « off », ligue de stats en fantôme, jambe « fill » coupée : calculée et journalisée, jamais sélectionnée
        return False
    if ensemble.model_silent(c["market"]) and c.get("p_ens") is None:       # famille figée : sans proba retenue du MARCHÉ (c["p_ens"]), le modèle ne décide pas seul
        return False
    if c.get("prob", 0) < C.VIABLE_MIN_P or not data_ok(c):
        return False
    if c.get("base") is not None and not ensemble.model_silent(c["market"]) and ensemble.model_prob(c) < c["base"]:       # famille figée : le modèle n'écarte rien
        return False
    if c.get("xodds") and c["xodds"] < c.get("min_odds", 0):              # 1xBet paie moins que la cote mini de Kelly : pas joué
        return False
    return not c.get("xnone")                                             # match trouvé chez 1xBet, mais ce pari n'y existe pas (pas encore ouvert)


def data_ok(c):
    """Kelly connaît assez les équipes du pronostic `c` : pas un pari de stats d'une coupe (cross_league_stat), données de qualité suffisante
    (C.VIABLE_MIN_Q), assez de matchs récents pour les deux équipes (C.VIABLE_MIN_N), et marché ou championnat où elle ne s'est pas montrée trop sûre
    d'elle (calib, C.VIABLE_MIN_K). Partie « données »
    de viable, reprise telle quelle par le combiné audacieux (app._bold_pools), qui assouplit la probabilité mais jamais ces contrôles."""
    from . import calib
    if cross_league_stat(c):                                              # stats d'une coupe : moyennes de championnats différents
        return False
    if c.get("conf", 1.0) < C.VIABLE_MIN_Q or (c.get("n_min") is not None and c["n_min"] < C.VIABLE_MIN_N):
        return False
    # sur les seuls vrais résultats (le rejeu corrige les probas, il n'écarte jamais un marché à lui seul) ; corners, cartons, tirs... : sur ceux de la
    # nouvelle règle de choix des lignes dès qu'il y en a assez (calib.viable_factor), et sur leur gain réel à la cote 1xBet (calib.new_rule_ok)
    return (calib.viable_factor(c["market"], sport_key=c.get("sport_key"), side=calib.side(c), p=c.get("p0"), shown=c.get("prob")) >= C.VIABLE_MIN_K
            or calib.trial_ok(c)) and calib.new_rule_ok(calib.family(c["market"]))      # essai : tirs vers le but « plus de » (C.SHOTS_TRIAL)


def best_per_match(pronos):
    """Le meilleur pronostic viable de chaque match, un seul par match, dans l'ordre du classement. Dès que le relevé 1xBet du jour a
    donné des cotes (kelly/xprice.py), seulement les pronostics jouables à leur cote 1xBet (c["xodds"]) : rien d'inventé ; sans
    aucune cote 1xBet (relevé en panne), les pronostics « à vérifier » (cote mini)."""
    real = any(c.get("xodds") for c in pronos)
    best = {}
    # à la cote 1xBet : le pari du match qui rapporte le plus (valeur x proba, comme xprice.choose) ; sinon le mieux classé
    for c in sorted(pronos, key=lambda c: -((c.get("xev") or 0.0) * c["prob"] if real and c.get("xodds") else c.get("score", 0))):       # xev absent : ligne écartée plus loin (viable)
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


def _fair_best(legs, lo, hi, max_legs=None, min_legs=None):
    """Combiné à cote juste : d'abord avec les seules jambes aux cotes 1xBet relevées par Kelly (rien d'inventé), sinon avec toutes. `max_legs` /
    `min_legs` : nombre de paris choisi (voir best_combo)."""
    real = [c for c in legs if c.get("xodds")]
    kw = {"max_legs": max_legs, "min_legs": min_legs}
    return best_combo(real, lo, hi, **kw) or (best_combo(legs, lo, hi, **kw) if len(real) < len(legs) else None)     # mêmes jambes : inutile de refaire


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
    Le résultat a toujours AU MOINS 2 jambes (comme `best_combo`) : un survivant isolé, ou un remplaçant seul quand tout est tombé, est
    complété sinon le combiné est abandonné.
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
        return keep, total, combo_prob(keep), [], [], []
    added = []
    if not keep or total < lo or len(keep) < 2:             # un combiné a toujours au moins 2 jambes : un survivant isolé est complété (ou abandonné)
        used = {c["id"] for c in keep}
        spares = [c for c in fresh.values() if c["id"] not in used and (usage is None or usage.allowed(c))]
        rlo, rhi = max(lo / total, 1.01), hi / total
        if rhi < rlo:
            return None
        one = max((c for c in spares if rlo <= c["odds"] <= rhi), key=lambda c: c["prob"], default=None)   # un seul remplaçant suffit (s'il reste au moins une jambe) ?
        extra = ([one], one["odds"], one["prob"]) if (one and keep) else best_combo(spares, rlo, rhi)    # tout est tombé : best_combo, au moins 2 jambes
        if not extra:
            return None
        added = extra[0]
        keep += added
        total = math.prod(c["odds"] for c in keep)
    return keep, total, combo_prob(keep), removed, added, changed


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
        if combo and not ensemble.unsent(combo[0]):   # sans valeur = fantôme : aucune réutilisation consommée
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
        if combo and not ensemble.unsent(combo[0]):   # sans valeur = fantôme : aucune réutilisation consommée
            usage.add(combo[0])
        out.append((target, combo))
    return out


def shots_title(target):
    """Titre d'un combiné « spécial tirs » (« 🎯 COMBINÉ TIRS CADRÉS »), None pour les autres combinés à cote juste. Un combiné de la fenêtre des stats
    (window_tier : « ts5w1 ») porte celui du palier « stats » du matin."""
    return (next((t for k, _, t, _, _ in C.FAIR_SHOTS_TIERS if k == target), None) or (C.STATS_WINDOW_TITLE if window_no(target) is not None else None)
            or (f"{C.SEASON_TITLE} · cote {season_no(target):g}" if season_no(target) is not None else None))


def season_tier(target):
    """Palier d'un combiné de la voie `saison` pour la cote visée `target` : « ss3 », « ss10 », « ss2,5 » -> « ss2.5 »."""
    t = float(target)
    return f"{C.SEASON_TIER_PREFIX}{int(t) if t.is_integer() else t:g}"


def season_no(tier):
    """Cote visée d'un palier de la voie `saison` (« ss10 » -> 10.0) ; None pour tout autre palier."""
    t = str(tier)
    if not t.startswith(C.SEASON_TIER_PREFIX):
        return None
    try:
        v = float(t[len(C.SEASON_TIER_PREFIX):])
    except ValueError:
        return None
    return v if v > 1.0 else None


def season_plan(legs, tiers=None, usage=None):
    """Combinés de la voie `saison`, un par palier de C.SEASON_TIERS, cote totale dans [cote x (1 - C.SEASON_SPREAD), cote x (1 + C.SEASON_SPREAD)] : [(palier « ss10 », (jambes,
    cote, proba) | None)]. Les jambes de VALEUR d'abord (sval), puis, si la fourchette n'est pas atteinte et si C.FILL_LEGS, avec le remplissage. Un seul pari par match, la proba du
    combiné maximale (_best_combo : l'essai des tirs de best_combo ne concerne pas cette voie). `usage` : celui de toute la journée (mêmes règles de réutilisation que les autres tickets) ;
    un combiné SANS valeur (que du remplissage) est construit mais ne consomme aucune réutilisation (il reste en fantôme, voir ensemble.unsent)."""
    usage = usage if usage is not None else Usage()
    pool = [c | {"odds": c.get("xodds") or c.get("odds")} for c in legs if c.get("xodds")]
    out = []
    for target in (tiers if tiers is not None else C.SEASON_TIERS):
        lo, hi = target * (1 - C.SEASON_SPREAD), target * (1 + C.SEASON_SPREAD)
        free = usage.filter(pool)
        combo = _best_combo([c for c in free if c.get("sval")], lo, hi)
        if combo is None and C.FILL_LEGS:
            combo = _best_combo(free, lo, hi)
        if combo and not ensemble.unsent(combo[0]):
            usage.add(combo[0])
        out.append((season_tier(target), combo))
    return out


def season_patch(legs, pool, lo, hi, fresh_ids=None, usage=None):
    """Combiné `saison` `legs` remis aux cotes du moment : `pool` = jambes `saison` relues (seasonlane.live_check : remises au prix, `sval` recalculé). Une jambe dont le match a été relu reste si CETTE
    ligne est encore proposée et vaut encore la valeur ou le remplissage (sval) ; sinon elle sort. Une jambe dont le match n'a PAS été relu est reprise telle quelle. Cote totale >= `lo` : on
    garde les survivants ; sinon ils sont complétés par le meilleur combiné de remplaçants (jambes de la voie encore valables, `usage` : règles de réutilisation) pour revenir dans
    [lo, hi]. Toujours au moins 2 jambes. -> (jambes, cote, proba, retirées, ajoutées, []) ou None si impossible. Jamais une jambe d'une autre voie."""
    from . import lanes
    by_key = {c.get("key"): c for c in pool}
    keep, removed = [], []
    for l in legs:
        if fresh_ids is not None and l["id"] not in fresh_ids:
            keep.append(l)
            continue
        c = by_key.get(l["key"])
        if c and c.get("xodds") and c.get("sval") is not None and lanes.usable(c):
            keep.append(c)
        else:
            removed.append(l)
    total = math.prod(c.get("odds") or c["xodds"] for c in keep) if keep else 1.0
    if fresh_ids is not None and not fresh_ids:
        return keep, total, combo_prob(keep), [], [], []
    added = []
    if not keep or total < lo or len(keep) < 2:
        used = {c["id"] for c in keep}
        spares = [c for c in pool if c["id"] not in used and c.get("xodds") and c.get("sval") is not None and lanes.usable(c) and (usage is None or usage.allowed(c))]
        best = {}
        for c in spares:                                    # une jambe par match : la plus probable
            if c["id"] not in best or c["prob"] > best[c["id"]]["prob"]:
                best[c["id"]] = c
        spares = [c | {"odds": c["xodds"]} for c in best.values()]
        rlo, rhi = max(lo / total, 1.01), hi / total
        if rhi < rlo:
            return None
        one = max((c for c in spares if rlo <= c["odds"] <= rhi), key=lambda c: c["prob"], default=None)
        extra = ([one], one["odds"], one["prob"]) if (one and keep) else _best_combo(spares, rlo, rhi)
        if not extra:
            return None
        added = extra[0]
        keep = keep + added
        total = math.prod(c.get("odds") or c["xodds"] for c in keep)
    return keep, total, combo_prob(keep), removed, added, []


def window_tier(n):
    """Palier du n-ième « 🧮 COMBINÉ STATS » de la journée envoyé par la fenêtre des stats (kelly/statswindow.py) : « ts5w1 », « ts5w2 »..."""
    return f"{C.STATS_WINDOW_TIER}{n}"


def window_no(tier):
    """Numéro d'un palier de la fenêtre des stats (« ts5w2 » -> 2) ; None pour tout autre palier (« ts5 », « tc5 », 5...)."""
    t = str(tier)
    return int(t[len(C.STATS_WINDOW_TIER):]) if t.startswith(C.STATS_WINDOW_TIER) and t[len(C.STATS_WINDOW_TIER):].isdigit() else None


def fair_stats_window(pronos, usage=None, accept=None):
    """Combiné « stats » de la fenêtre (cote C.STATS_WINDOW_COMBO) : un pari par match, parmi les jambes solides aux cotes 1xBet (fair_legs) des marchés stats, avec leur
    référence de pari (de quoi enregistrer un code coupon) ; `accept(jambe)` en filtre encore (match dans la fenêtre, cote fraîche). `usage` : les combinés déjà bâtis
    aujourd'hui (mêmes règles de réutilisation). -> (jambes, cote, proba) ou None."""
    usage = usage if usage is not None else Usage()
    lo, hi = C.STATS_WINDOW_COMBO
    legs = [c for c in usage.filter(fair_legs(pronos)) if c["market"].startswith(C.STATS_WINDOW_PREFIX) and c.get("xodds") and c.get("xref")
            and (accept is None or accept(c))]
    return best_combo(legs, lo, hi)


def reach(legs, max_legs=None):
    """Plus haute cote atteignable avec au plus `max_legs` paris (un par match, la meilleure cote de chacun, les plus hautes d'abord) ; 1 sans 2 matchs."""
    best = {}
    for c in legs:
        best[c["id"]] = max(best.get(c["id"], 1.0), c["odds"])
    top = sorted(best.values(), reverse=True)[:max_legs or None]
    return math.prod(top) if len(top) >= 2 else 1.0


def kinds_available(picks, pronos):
    """Types de paris jouables aujourd'hui pour un combiné sur mesure -> {code: nombre de matchs} (jambes de qualité des paris réels et
    pronostics solides, ceux aux cotes 1xBet dès que 1xBet en a donné), pour les cases à cocher. Les paris réels ne comptent que si
    app.custom_combo peut les proposer (1xBet les a chiffrés, ou 1xBet n'a rien chiffré du tout)."""
    xp, xk = any(c.get("xodds") for c in pronos), any(c.get("xodds") for c in picks)
    ids = {}
    for c in (make_pool(picks) if picks and (xk or not xp) else []) + fair_legs(pronos):
        k = kind_of(c["market"])
        if k:
            ids.setdefault(k, set()).add(c["id"])
    return {k: len(v) for k, v in ids.items()}


def fair_custom(pronos, target, usage=None, prefix=None, max_legs=None, min_legs=None):
    """/combi <cote> sans cotes réelles : combiné à cote juste dont la cote mini est au moins la cote visée (jusqu'à
    + C.CUSTOM_TOLERANCE) : on ne promet jamais moins que demandé, puisque c'est un minimum. `prefix` : types de paris choisis (filtrés
    APRÈS fair_legs, qui regarde TOUS les pronostics pour savoir si 1xBet a donné des cotes aujourd'hui)."""
    legs = _keep((usage or Usage()).filter(fair_legs(pronos)), prefix)
    return _fair_best(legs, target, target * (1 + C.CUSTOM_TOLERANCE), max_legs, min_legs)


def fair_shortfall(pronos, target, prefix=None):
    """Ce qui manque pour un combiné à cote juste de cote `target` : {matches, legs, best_total, need} (matchs avec un
    pronostic solide, meilleure cote mini atteignable avec tous, nombre approximatif de matchs nécessaires). `prefix` : voir fair_custom."""
    legs = _keep(fair_legs(pronos), prefix)
    best = {}
    for c in legs:
        if c["id"] not in best or c["prob"] > best[c["id"]]["prob"]:
            best[c["id"]] = c
    total = math.prod(c["odds"] for c in best.values()) if best else 1.0
    mean = math.exp(sum(math.log(c["odds"]) for c in best.values()) / len(best)) if best else 1.5
    need = math.ceil(math.log(target) / math.log(max(mean, 1.05)))
    return {"matches": len(best), "legs": len(legs), "best_total": total, "need": need}
