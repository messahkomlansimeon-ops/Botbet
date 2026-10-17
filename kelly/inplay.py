"""Chances en direct d'un pari pendant son match (suivi des tickets, kelly/watch.py) : à partir de la chance d'avant-match du pari (c["prob"]), du score,
de la minute, des cartons rouges et des stats du moment. Fonctions pures, aucune requête.

  - buts (plus / moins) et buts d'une équipe : la moyenne de buts du match qui redonne la chance d'avant-match (solve_mean), réduite au temps restant
    (live.remaining : 45 % des buts en 1re mi-temps, 55 % en 2e) et aux cartons rouges (live.RED_SELF / RED_OTHER), loi de Poisson ;
  - 1N2, double chance, les 2 marquent : le modèle live de Kelly (live.probs, buts attendus d'avant-match de live.model), s'il connaît le match ;
  - corners, cartons, tirs, fautes… : la moyenne d'avant-match (solve_mean, binomiale négative) corrigée par ce que le match montre (rythme observé,
    l'avant-match pesant un match entier), réduite au temps restant ; et le rythme du match en clair : « 6 corners à la 55e, rythme 9,8 sur 90 min ».
À la minute 0, sans but ni stat, la chance redonnée est celle d'avant-match : seul le match la fait bouger."""
from . import live, strength

# Effet du score et des cartons rouges sur ce qu'il reste à venir d'une statistique (live_ledger.json, pré-déclaré le 17/10 AVANT le code ; paramètres FIXES et BORNÉS, jamais appris) :
# l'équipe MENÉE attaque davantage (ses corners, tirs et tirs cadrés restants x1,15 menée d'un but, x1,25 de deux buts ou plus), celle qui MÈNE se replie (x0,90 / x0,85) ; égalité : 1 ; un carton rouge
# réduit à dix : ses corners, tirs et tirs cadrés x0,80, ses fautes x1,15 (par carton). Aucun effet sur les cartons, les touches et le hors-jeu. Les buts n'en ont pas ici (live.RED_SELF / RED_OTHER).
SCORE_MULT = {1: (1.15, 0.90), 2: (1.25, 0.85)}      # écart de buts (plafonné à 2) -> (équipe menée, équipe qui mène)
SCORE_STATS = ("corners", "shots", "sot")
RED_MULT = {"corners": 0.80, "shots": 0.80, "sot": 0.80, "fouls": 1.15}
MULT_BOUNDS = (0.5, 1.5)                             # garde-fou : le facteur d'une équipe ne sort jamais de cette plage
ALPHA_STATS = 0.10               # dispersion (binomiale négative) d'une stat de match quand le modèle ne la donne pas
UNITS = {"corners": ("corner", "corners"), "cards": ("carton", "cartons"), "yellow": ("carton jaune", "cartons jaunes"), "shots": ("tir", "tirs"),
         "sot": ("tir cadré", "tirs cadrés"), "fouls": ("faute", "fautes"), "throws": ("touche", "touches"), "offsides": ("hors-jeu", "hors-jeu")}


def _over(mean, alpha, need):
    """P(X > need), X de moyenne `mean` (Poisson si alpha = 0, sinon binomiale négative)."""
    if need < 0:
        return 1.0
    if mean <= 0:
        return 0.0
    return strength.p_over(strength.nb_pmf(mean, alpha), need)


def solve_mean(p0, name, point, alpha=0.0):
    """Moyenne du match entier pour laquelle P(plus de `point`) vaut `p0` (`name` « Over »), ou P(moins de) vaut `p0` (« Under ») : bissection."""
    target = p0 if name == "Over" else 1.0 - p0
    lo, hi = 0.01, max(4.0, point * 4 + 10)
    for _ in range(60):
        mid = (lo + hi) / 2
        if _over(mid, alpha, point) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def stat_remaining(minute, period):
    """Part d'une stat (corners, cartons, tirs…) encore à venir : moitié par mi-temps, temps additionnel compris."""
    if period == 1:
        return 0.5 * max(0.0, live.HALF_1_LEN - minute) / live.HALF_1_LEN + 0.5
    return 0.5 * max(0.0, 45.0 + live.HALF_2_LEN - max(minute, 45.0)) / live.HALF_2_LEN


def pace(key, val, minute):
    """« 6 corners à la 55e, rythme 9,8 sur 90 min » ; '' avant la 15e (trop tôt pour un rythme)."""
    if minute < 15 or val is None:
        return ""
    one, many = UNITS.get(key, (key, key))
    rhythm = f"{val * 90 / minute:.1f}".replace(".", ",")
    return f"{val} {one if val == 1 else many} à la {minute}e, rythme {rhythm} sur 90 min"


def team_mults(key, score, reds=None):
    """(facteur domicile, facteur extérieur) appliqué à ce qui reste à venir de la statistique `key` : effet du score (SCORE_STATS) et des cartons rouges (RED_MULT), bornés à MULT_BOUNDS. (1.0, 1.0) sans effet."""
    h, a = score
    rh, ra = reds or (0, 0)
    mh = ma = 1.0
    if key in SCORE_STATS and h != a:
        trail, lead = SCORE_MULT[min(2, abs(h - a))]
        mh, ma = (lead, trail) if h > a else (trail, lead)
    f = RED_MULT.get(key)
    if f:
        mh, ma = mh * f ** rh, ma * f ** ra
    lo, hi = MULT_BOUNDS
    return max(lo, min(hi, mh)), max(lo, min(hi, ma))


def shares(key, pred):
    """Part (domicile, extérieur) de la statistique `key` d'un match : celle des moyennes d'avant-match du modèle (pred["stats"][key]["h" / "a"]) si elles sont connues, sinon 50 / 50 (hypothèse déclarée au ledger)."""
    st = ((pred or {}).get("stats") or {}).get(key) or {}
    h, a = st.get("h"), st.get("a")
    if isinstance(h, (int, float)) and isinstance(a, (int, float)) and h > 0 and a > 0:
        return h / (h + a), a / (h + a)
    return 0.5, 0.5


def side_mult(key, side, score, reds, pred):
    """Facteur de ce qui reste à venir pour le côté du pari (« total » : moyenne des deux équipes pondérée par leurs parts)."""
    mh, ma = team_mults(key, score, reds)
    if side == "home":
        return mh
    if side == "away":
        return ma
    sh, sa = shares(key, pred)
    return sh * mh + sa * ma


def chance(leg, key, side, score, minute, period, reds=None, stats=None, pred=None, effect=False):
    """Chance (0 à 1) que le pari `leg` gagne d'ici la fin du match, et le rythme du match pour une stat -> (p, rythme) ; (None, '') si Kelly ne sait
    pas l'estimer (pas de chance d'avant-match, 1N2 sans modèle, stat inconnue). `key`, `side` : watch.need(leg). `effect` : applique l'effet du score et des cartons rouges aux statistiques
    (team_mults) ; faux par défaut, le suivi des matchs le règle sur C.LIVE_SCORE_EFFECT."""
    p0 = leg.get("prob")
    m, name, point = leg.get("market"), leg.get("name"), leg.get("point")
    h, a = score
    rh, ra = reds or (0, 0)
    if key == "goals":
        if m in ("h2h", "double_chance", "btts"):
            if not pred or not pred.get("lh") or not pred.get("la"):
                return None, ""
            probs = live.probs({"minute": minute, "period": period, "score": (h, a), "reds": (rh, ra), "home": leg["home"], "away": leg["away"],
                                "odds": {}}, pred)
            p = probs.get((m, name, None))
            return (None if p is None else max(0.0, min(1.0, p))), ""
        if not p0 or point is None or name not in ("Over", "Under"):
            return None, ""
        full = solve_mean(p0, name, point)
        rem = live.remaining(minute, period)
        if m == "team_totals_home":
            cur, f = h, (live.RED_SELF ** rh) * (live.RED_OTHER ** ra)
        elif m == "team_totals_away":
            cur, f = a, (live.RED_SELF ** ra) * (live.RED_OTHER ** rh)
        else:
            cur, f = h + a, ((live.RED_SELF + live.RED_OTHER) / 2) ** (rh + ra)
        over = _over(full * rem * f, 0.0, point - cur)
        return (over if name == "Over" else 1.0 - over), ""
    if not p0 or point is None or name not in ("Over", "Under") or not stats:
        return None, ""
    hv, av = stats.get(key) or (None, None)
    if hv is None:
        return None, ""
    val = hv + av if side == "total" else hv if side == "home" else av
    alpha = ALPHA_STATS
    st = ((pred or {}).get("stats") or {}).get(key)
    if st:
        alpha = st.get("at" if side == "total" else "al") or alpha
    full = solve_mean(p0, name, point, alpha)
    played = max(0.0, min(90.0, float(minute)))
    rate = (full + val) / (1.0 + played / 90.0)      # l'avant-match compte pour un match entier, le match du jour pour ce qui est joué
    mult = side_mult(key, side, score, reds, pred) if effect else 1.0       # effet=False (défaut) : exactement le calcul d'avant (x 1.0 ne change aucun bit)
    over = _over(rate * stat_remaining(minute, period) * mult, alpha, point - val)
    return (over if name == "Over" else 1.0 - over), pace(key, val, minute)


def pct(p):
    return f"{round(p * 100)} %"
