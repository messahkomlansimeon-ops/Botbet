"""Estimations d'avant-match à partir des stats gratuites (kelly/sources) : corners, cartons, tirs des
joueurs (par 90 min), buts (xG). Pour chaque pari, le modèle (Poisson) donne une proba du côté choisi,
mélangée à celle des books (poids dans config.MODEL_WEIGHT) ; l'écart modèle/ligne est gardé dans
c["pm"] et alimente l'apprentissage (learn.features).

c["pm"] = {kind, src, exp?, gap?, pgap?, xgd?} :
  kind  corners | cards | shots | sot | fouls | goals | btts | h2h     src  sources | api
  exp   valeur attendue (par match)   gap   exp - ligne, du côté du pari (+ = le modèle aime le pari)
  pgap  proba modèle - proba des books    xgd  buts attendus (domicile - extérieur), du côté du pari
Cartons = jaunes + rouges (convention d'API-Football et de football-data) ; à vérifier selon le book.

Modèle approfondi (kelly/strength.py) : quand f["deep"] existe, la proba vient de la matrice de scores de Dixon-Coles
(buts, 2 équipes marquent) ou d'une binomiale négative (corners, cartons), le poids du modèle est celui de config
multiplié par (0,5 + 0,5 x qualité des données) et le « pourquoi » cite les 2-3 moteurs (c["pm"]["q"] = qualité).
Le poids des buts de ce modèle est celui mesuré sur l'historique pour la ligue du pari (data/tuning.json, voir kelly/tune.py)
quand il existe ; sinon config.MODEL_WEIGHT["goals"]. Corners et cartons gardent le poids de config (pas de cotes historiques).
"""
import math

from . import config as C, strength
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


XG_SOURCES_TABLE = ("fotmob-classement",)          # xG de saison du classement FotMob : plus grossiers (config.GOAL_XG_WEIGHT_TABLE)


def xg_weight(f):
    """Poids des xG (f["exp_xg_home"] / f["exp_xg_away"]) dans les buts attendus du modèle de force : 0 s'ils manquent. La source (f["xg_src"]) est celle que
    sources.features a retenue dans l'ordre Understat > FotMob matchs > API-Football matchs > FotMob classement ; les xG du classement pèsent moins."""
    if f.get("exp_xg_home") is None or f.get("exp_xg_away") is None:
        return 0.0
    return C.GOAL_XG_WEIGHT_TABLE if f.get("xg_src") in XG_SOURCES_TABLE else C.GOAL_XG_WEIGHT


def goal_means(f):
    """(buts attendus domicile, extérieur, étiquette) : xG Understat si connus, sinon moyennes de buts
    football-data ; None sinon. Avec le modèle approfondi (f["deep"]), ses buts attendus, mêlés aux xG s'ils existent (même poids que les pronostics sans cote :
    xg_weight, 30 % ; 15 % pour les xG de saison du classement FotMob)."""
    d = f.get("deep")
    if d and d.get("lh") is not None:
        w = xg_weight(f)
        if w:
            return (1 - w) * d["lh"] + w * f["exp_xg_home"], (1 - w) * d["la"] + w * f["exp_xg_away"], "modèle Kelly + xG"
        return d["lh"], d["la"], "modèle Kelly"
    if f.get("exp_xg_home") is not None and f.get("exp_xg_away") is not None:
        return f["exp_xg_home"], f["exp_xg_away"], "xG"
    if f.get("exp_goals_home") is not None and f.get("exp_goals_away") is not None:
        return f["exp_goals_home"], f["exp_goals_away"], "moyennes de buts"
    return None


def goal_kappa(f=None):
    """Surdispersion des buts d'un match : le kappa du modèle de force (f["deep"]["kappa"], mesuré sur la ligue) sinon C.GOALS_KAPPA_DEFAULT (repli sans modèle)."""
    d = (f or {}).get("deep") or {}
    return d["kappa"] if d.get("kappa") is not None else C.GOALS_KAPPA_DEFAULT


def goal_rho(f=None):
    """Correction de Dixon-Coles du modèle de force (f["deep"]["rho"]) ; 0 sans modèle (repli : buts indépendants)."""
    return ((f or {}).get("deep") or {}).get("rho") or 0.0


def p_totals(lh, la, point, name, kappa=None):
    """Proba du côté choisi d'un total de buts : binomiale négative de moyenne lh + la et de surdispersion `kappa` (C.GOALS_KAPPA_DEFAULT par défaut ; 0 =
    Poisson), comme la loi bivariée de strength.score_matrix."""
    k = C.GOALS_KAPPA_DEFAULT if kappa is None else kappa
    return over_p(name, strength.p_over(strength.nb_pmf(lh + la, k), point))


def p_btts(lh, la, name, rho=0.0, kappa=None):
    """Proba du côté choisi des 2 équipes marquent : matrice de scores bivariée (strength.score_matrix : surdispersion `kappa`, correction de Dixon-Coles `rho` des
    scores 0-0, 1-0, 0-1 et 1-1) ; l'ancien repli supposait les deux scores indépendants et sans surdispersion."""
    k = C.GOALS_KAPPA_DEFAULT if kappa is None else kappa
    m = strength.score_matrix(lh, la, rho, kappa=k)
    yes = 1 - sum(m[0]) - sum(row[0] for row in m) + m[0][0]
    return yes if name == "Yes" else 1 - yes


def p_1x2(lh, la, cap=10, rho=0.0, kappa=None):
    """(P domicile, P nul, P extérieur) : matrice de scores de strength.score_matrix (surdispersion `kappa`, C.GOALS_KAPPA_DEFAULT par défaut ; Dixon-Coles `rho`)."""
    k = C.GOALS_KAPPA_DEFAULT if kappa is None else kappa
    return strength.result_probs(strength.score_matrix(lh, la, rho, cap=cap, kappa=k))


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


def qscale(q):
    """Poids du modèle selon la qualité des données (matchs pondérés des deux équipes, complétude des colonnes de la ligue)."""
    return 0.5 + 0.5 * max(0.0, min(1.0, q))


def aps_scale(f, key):
    """Poids du modèle réduit quand le chiffre `key` (exp_corners...) vient d'un profil API-Football court (f["aps_q"])."""
    return qscale(f.get("aps_q", 1.0)) if key in (f.get("aps_keys") or ()) else 1.0


def deep_p(d, lh, la, market, point, name):
    """Proba du côté choisi pour un total de buts ou « les 2 marquent » avec la matrice de Dixon-Coles du modèle `d`."""
    m = strength.score_matrix(lh, la, d.get("rho", 0.0), kappa=d.get("kappa", 0.0))
    if market == "btts":
        yes = 1 - sum(m[0]) - sum(row[0] for row in m) + m[0][0]
        return yes if name == "Yes" else 1 - yes
    return over_p(name, strength.p_over(strength.goal_pmf(m), point))


def goal_weight(c):
    """Poids du modèle de buts pour ce pari : celui mesuré sur l'historique pour sa ligue (data/tuning.json, kelly/tune.py),
    sinon config.MODEL_WEIGHT["goals"]."""
    from . import ensemble
    if ensemble.model_silent(c.get("market") or "totals"):       # poids figé à 0 (audit du 14/10, extension par analogie non testée : voir le ledger) : ni 20 % ni réglage par championnat
        return 0.0
    from .sources import footballdata as fd
    w = strength.goal_weight(fd.SPORT_TO_DIV.get(c.get("sport_key")))
    return C.MODEL_WEIGHT["goals"] if w is None else w


def btts_weight(c):
    """Poids du modèle de buts pour « les 2 marquent » : celui de config (MODEL_WEIGHT["goals"]), JAMAIS le poids mesuré par tune.py, qui est celui
    du plus/moins de 2,5 buts (football-data n'a aucune cote historique des 2 marquent : le poids du modèle sur ce marché n'est pas mesuré). Figé à 0 comme goal_weight."""
    from . import ensemble
    return 0.0 if ensemble.model_silent("btts") else C.MODEL_WEIGHT["goals"]


def deep_assess(c, f, d):
    """Évaluation par le modèle approfondi (f["deep"]) pour corners, cartons, buts, 2 équipes marquent, 1N2 ; None sinon."""
    m, point = c["market"], c.get("point")
    why = d.get("why") or {}
    if m in (CORNERS, CARDS):
        kind = "corners" if m == CORNERS else "cards"
        s = (d.get("stats") or {}).get(kind)
        if not s or point is None:
            return None
        exp = s["h"] + s["a"]
        if not BOUNDS[kind][0] <= exp <= BOUNDS[kind][1]:
            return None
        q = d["qs"].get(kind, 0.0)
        unit = "corners" if kind == "corners" else "cartons"
        tag = strength.shorten(f"modèle Kelly : {exp:.1f} {unit} attendus vs ligne {point}" + (f" ; {why[kind]}" if why.get(kind) else ""), 200)
        return {"kind": kind, "exp": exp, "p": strength.p_stat_total(d, kind, point, c["name"]),
                "w": C.MODEL_WEIGHT[kind] * qscale(q), "tag": tag, "flag": C.PM_FLAG, "q": q}
    gm = goal_means(f) if m in ("totals", "btts", "h2h") else None
    if not gm or not all(BOUNDS["goals"][0] <= x <= BOUNDS["goals"][1] for x in gm[:2]):
        return None
    lh, la, label = gm
    q = d["qs"].get("goals", 0.0)
    if m == "totals" and point is not None:
        exp = lh + la
        tag = strength.shorten(f"{label} : {exp:.1f} buts attendus vs ligne {point}" + (f" ; {why['goals']}" if why.get("goals") else ""), 200)
        return {"kind": "goals", "exp": exp, "p": deep_p(d, lh, la, m, point, c["name"]), "w": goal_weight(c) * qscale(q),
                "tag": tag, "flag": C.PM_FLAG * 1.25, "q": q}
    if m == "btts":
        tag = strength.shorten(f"{label} : {lh:.1f}-{la:.1f} buts attendus (les 2 marquent)" + (f" ; {why['goals']}" if why.get("goals") else ""), 200)
        return {"kind": "btts", "p": deep_p(d, lh, la, m, None, c["name"]), "w": btts_weight(c) * qscale(q),
                "tag": tag, "flag": C.PM_FLAG * 1.25, "q": q}
    if m == "h2h":
        who = "draw" if c["name"] == "Draw" else ("home" if c["name"] == c["home"] else "away")
        return {"kind": "h2h", "xgd": signed_diff(who, lh, la), "q": q}
    return None


def assess(c, f, sources):
    """Évaluation d'un pari à partir des chiffres d'avant-match `f` (sources.match_features) :
    dict {kind, exp?, p?, w?, tag?, flag?, xgd?, q?} ou None si les données manquent."""
    m, point = c["market"], c.get("point")
    if f.get("deep") and not m.startswith("player_"):
        a = deep_assess(c, f, f["deep"])
        if a is not None:
            return a
    if m in (CORNERS, CARDS):
        kind = "corners" if m == CORNERS else "cards"
        exp = f.get("exp_" + kind)
        if exp is None or point is None or not BOUNDS[kind][0] <= exp <= BOUNDS[kind][1]:
            return None
        unit = "corners" if kind == "corners" else "cartons"
        return {"kind": kind, "exp": exp, "p": over_p(c["name"], poisson_over(exp, point)),
                "w": C.MODEL_WEIGHT[kind] * aps_scale(f, "exp_" + kind),
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
        return {"kind": "goals", "exp": exp, "p": p_totals(lh, la, point, c["name"], goal_kappa(f)),
                "w": goal_weight(c) * aps_scale(f, "exp_goals_home"),
                "tag": f"{label} : {exp:.1f} buts attendus vs ligne {point}", "flag": C.PM_FLAG * 1.25}
    if m == "btts":
        return {"kind": "btts", "p": p_btts(lh, la, c["name"], goal_rho(f), goal_kappa(f)), "w": btts_weight(c) * aps_scale(f, "exp_goals_home"),
                "tag": f"{label} : {lh:.1f}-{la:.1f} buts attendus (les 2 marquent)", "flag": C.PM_FLAG * 1.25}
    if m == "h2h":
        who = "draw" if c["name"] == "Draw" else ("home" if c["name"] == c["home"] else "away")
        return {"kind": "h2h", "xgd": signed_diff(who, lh, la) * aps_scale(f, "exp_goals_home")}    # profil court : avantage atténué
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
        from . import ensemble
        if ensemble.model_silent(c.get("market") or ""):
            pm["silent"] = 1                                # poids figé à 0 : l'avis du modèle reste noté (fantôme) mais n'est plus un argument du pari
        elif abs(a["p"] - old) > a["flag"]:
            c["why"] = ", ".join(filter(None, [c["why"], a["tag"]]))
    if a.get("xgd") is not None:
        pm["xgd"] = round(a["xgd"], 2)
    if a.get("q") is not None:
        pm["q"] = round(a["q"], 2)
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
