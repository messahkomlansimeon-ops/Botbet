"""Estimations d'avant-match à partir des stats gratuites (kelly/sources) : corners, cartons, tirs des
joueurs (par 90 min), buts (xG). Pour chaque pari, le modèle (Poisson) donne une proba du côté choisi,
mélangée à celle des books (poids dans config.MODEL_WEIGHT) ; l'écart modèle/ligne est gardé dans
c["pm"] et alimente l'apprentissage (learn.features).

c["pm"] = {kind, src, exp?, gap?, pgap?, xgd?} :
  kind  corners | cards | shots | sot | fouls | goals | btts | h2h     src  sources | api
  exp   valeur attendue (par match)   gap   exp - ligne, du côté du pari (+ = le modèle aime le pari)
  pgap  proba modèle - proba des books    xgd  buts attendus (domicile - extérieur), du côté du pari
Cartons = jaunes + rouges (convention d'API-Football et de football-data) ; à vérifier selon le book.
"""
import math

from . import config as C
from .stats import poisson_over

CORNERS, CARDS = "alternate_totals_corners", "alternate_totals_cards"
PLAYER = {"player_shots": ("shots90", "shots", "tirs"), "player_shots_on_target": ("sot90", "sot", "tirs cadrés"),
          "player_fouls": ("fouls90", "fouls", "fautes")}
BOUNDS = {"corners": (3.0, 20.0), "cards": (0.5, 10.0), "goals": (0.3, 6.0)}   # au-delà : donnée suspecte, ignorée


def mispriced(c):
    """Marchés mal pricés (corners, cartons, stats joueurs) : cible du combiné spécial."""
    return c["market"] in (CORNERS, CARDS) or c["market"].startswith("player_")


def over_p(name, p_over):
    """Proba du côté choisi à partir de P(plus de la ligne)."""
    return p_over if name in ("Over", "Yes") else 1 - p_over


def side(name):
    return 1.0 if name in ("Over", "Yes") else -1.0


def goal_means(f):
    """(buts attendus domicile, extérieur, étiquette) : xG Understat si connus, sinon moyennes de buts
    football-data ; None sinon."""
    if f.get("exp_xg_home") is not None and f.get("exp_xg_away") is not None:
        return f["exp_xg_home"], f["exp_xg_away"], "xG"
    if f.get("exp_goals_home") is not None and f.get("exp_goals_away") is not None:
        return f["exp_goals_home"], f["exp_goals_away"], "moyennes de buts"
    return None


def p_totals(lh, la, point, name):
    """Proba du côté choisi d'un total de buts (Poisson sur la somme des buts attendus)."""
    return over_p(name, poisson_over(lh + la, point))


def p_btts(lh, la, name):
    """Proba du côté choisi des 2 équipes marquent (buts indépendants)."""
    yes = (1 - math.exp(-lh)) * (1 - math.exp(-la))
    return yes if name == "Yes" else 1 - yes


def p_1x2(lh, la, cap=10):
    """(P domicile, P nul, P extérieur) pour deux Poisson indépendants de moyennes lh et la."""
    pm = lambda m, k: math.exp(-m) * m ** k / math.factorial(k)             # noqa: E731
    ph = pd = pa = 0.0
    for i in range(cap + 1):
        for j in range(cap + 1):
            p = pm(lh, i) * pm(la, j)
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
    t = ph + pd + pa
    return ph / t, pd / t, pa / t


def signed_diff(who, lh, la):
    """Buts attendus (domicile - extérieur) du côté du pari : who = home | away | draw (nul : -|écart|)."""
    d = lh - la
    return -abs(d) if who == "draw" else (d if who == "home" else -d)


def player_stats(c, sources):
    """Stats par 90 min du joueur d'un pari, cherché dans l'équipe à domicile puis à l'extérieur
    (None si inconnu ou ambigu : même nom dans les deux équipes)."""
    found = []
    for team in (c["home"], c["away"]):
        p = sources.player_features(c.get("who") or "", team, c["sport_key"])
        if p:
            found.append(p)
    return found[0] if len(found) == 1 else None


def assess(c, f, sources):
    """Évaluation d'un pari à partir des chiffres d'avant-match `f` (sources.match_features) :
    dict {kind, exp?, p?, w?, tag?, flag?, xgd?} ou None si les données manquent."""
    m, point = c["market"], c.get("point")
    if m in (CORNERS, CARDS):
        kind = "corners" if m == CORNERS else "cards"
        exp = f.get("exp_" + kind)
        if exp is None or point is None or not BOUNDS[kind][0] <= exp <= BOUNDS[kind][1]:
            return None
        unit = "corners" if kind == "corners" else "cartons"
        return {"kind": kind, "exp": exp, "p": over_p(c["name"], poisson_over(exp, point)), "w": C.MODEL_WEIGHT[kind],
                "tag": f"stats réelles : {exp:.1f} {unit} attendus vs ligne {point}", "flag": C.PM_FLAG}
    if m in PLAYER and point is not None and c.get("who"):
        col, kind, unit = PLAYER[m]
        p = player_stats(c, sources)
        if not p:
            return None
        per90, est = p.get(col), False
        if per90 is None and kind == "sot" and p.get("shots90") is not None:      # FBref absent : tirs cadrés estimés
            per90, est = p["shots90"] * C.SOT_RATIO, True
        if per90 is None:
            return None
        exp = per90 * C.PLAYER_MINUTES / 90
        tag = f"stats réelles : {per90:.1f} {unit}/90{' (estimé)' if est else ''} vs ligne {point}"
        return {"kind": kind, "exp": exp, "p": over_p(c["name"], poisson_over(exp, point)),
                "w": C.MODEL_WEIGHT["player"] / (2 if est else 1), "tag": tag, "flag": C.PM_FLAG}
    gm = goal_means(f) if m in ("totals", "btts", "h2h") else None
    if not gm or not all(BOUNDS["goals"][0] <= x <= BOUNDS["goals"][1] for x in gm[:2]):
        return None
    lh, la, label = gm
    if m == "totals" and point is not None:
        exp = lh + la
        return {"kind": "goals", "exp": exp, "p": p_totals(lh, la, point, c["name"]), "w": C.MODEL_WEIGHT["goals"],
                "tag": f"{label} : {exp:.1f} buts attendus vs ligne {point}", "flag": C.PM_FLAG * 1.25}
    if m == "btts":
        return {"kind": "btts", "p": p_btts(lh, la, c["name"]), "w": C.MODEL_WEIGHT["goals"],
                "tag": f"{label} : {lh:.1f}-{la:.1f} buts attendus (les 2 marquent)", "flag": C.PM_FLAG * 1.25}
    if m == "h2h":
        who = "draw" if c["name"] == "Draw" else ("home" if c["name"] == c["home"] else "away")
        return {"kind": "h2h", "xgd": signed_diff(who, lh, la)}
    return None


def apply(c, a, src="sources"):
    """Applique l'évaluation `a` à un pari : mélange de proba (jamais pour h2h), score, « pourquoi », c["pm"]."""
    pm = {"kind": a["kind"], "src": src}
    if a.get("exp") is not None:
        pm["exp"] = round(a["exp"], 2)
        if c.get("point") is not None:
            pm["gap"] = round(side(c["name"]) * (a["exp"] - c["point"]), 2)
    if a.get("p") is not None:
        old, w = c["prob"], a["w"]
        c["prob"] = (1 - w) * old + w * a["p"]
        c["edge"] = c["prob"] * c["odds"] - 1
        delta = (c["edge"] - (old * c["odds"] - 1)) * c["weight"]
        c["score"] += delta
        if "heuristic" in c:
            c["heuristic"] += delta
        pm["pgap"] = round(a["p"] - old, 3)
        if abs(a["p"] - old) > a["flag"]:
            c["why"] = ", ".join(filter(None, [c["why"], a["tag"]]))
    if a.get("xgd") is not None:
        pm["xgd"] = round(a["xgd"], 2)
    c["pm"] = pm
    return c


def api_assess(c, exp):
    """Repli API-Football : moyennes corners/cartons des deux équipes (stats.expected_totals), ou None."""
    kind = "corners" if c["market"] == CORNERS else "cards"
    if not exp or exp.get(kind) is None or c.get("point") is None:
        return None
    unit = "corners" if kind == "corners" else "cartons"
    return {"kind": kind, "exp": exp[kind], "p": over_p(c["name"], poisson_over(exp[kind], c["point"])),
            "w": C.MODEL_WEIGHT[kind], "tag": f"stats réelles : {exp[kind]:.1f} {unit} attendus vs ligne {c['point']}",
            "flag": C.PM_FLAG}
