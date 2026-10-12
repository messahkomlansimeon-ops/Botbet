"""Modèle de force des équipes (Dixon-Coles pondéré dans le temps) et marchés qui en découlent.

Données : les CSV football-data DÉJÀ en cache disque (aucune requête réseau). Pour chaque ligue, on ajuste par mises à
jour itératives (Maher / IPF, pur Python) un modèle de Poisson multiplicatif
    buts domicile = m x H x Att(dom) x Def(ext)      buts extérieur = m x Att(ext) x Def(dom)
pondéré par l'ancienneté (demi-vie HALF_LIFE jours) et rétréci vers la moyenne de la ligue (loi a priori Gamma dont
la force est estimée sur les données par la méthode des moments : peu de matchs = proche de la moyenne). Les équipes
promues partent d'un a priori plus faible. La correction de Dixon-Coles (rho, scores 0-0, 1-0, 0-1, 1-1) est estimée
ensuite par maximum de vraisemblance. Même décomposition attaque / défense pour les tirs, tirs cadrés, corners, fautes
et cartons là où les colonnes existent ; surdispersion (binomiale négative) estimée sur les données.

Facteurs de contexte (petits, bornés, expliqués) : forme des 5 derniers matchs, domicile/extérieur propre à l'équipe,
face-à-face, repos, enjeu (classement calculé depuis les résultats de la saison) ; arbitre pour cartons, cartons jaunes et fautes.

Cache : data/strength_<DIV>.json, un fichier par ligue, recalculé au plus une fois par jour (ou quand les profils
football-data sont actualisés). Sans données brutes, le dernier cache de moins de STALE_DAYS jours sert, marqué périmé.

API : get(div) -> modèle | None ; predict(modèle, dom, ext, on, referee) -> prévision | None ; grid(prev) -> grille de
marchés ; lines(prev, dom, ext) -> lignes de marché à plat ; analysis_text(...) ; backtest(...).
"""
import json
import math
import os
import sys
import time
from datetime import date, timedelta

from . import store

VERSION = 2                      # 2 : + cartons jaunes seuls (stat « yellow ») ; un cache de la version 1 est refait au premier appel
HALF_LIFE = 110                  # jours : un match vieux de 110 jours pèse moitié moins
STATS = ("goals", "shots", "sot", "corners", "fouls", "cards", "yellow")
UNIT = {"goals": "buts", "shots": "tirs", "sot": "tirs cadrés", "corners": "corners", "fouls": "fautes", "cards": "cartons", "yellow": "cartons jaunes"}
COLS = {"shots": ("hs", "as"), "sot": ("hst", "ast"), "corners": ("hc", "ac"), "fouls": ("hf", "af"), "yellow": ("hy", "ay")}      # yellow : jaunes SEULS (1xBet ne compte que les jaunes), cards = jaunes + rouges
MIN_MATCHES = 60                 # matchs minimum dans la ligue pour ajuster un modèle
MIN_ROWS = 50                    # lignes minimum pour une statistique
STALE_DAYS = 14                  # cache périmé encore utilisable si les données brutes manquent
NEFF_FULL = 8.0                  # matchs pondérés à partir desquels une équipe est « bien connue »
# a priori des équipes promues (attaque, défense) : elles marquent moins et encaissent plus
PROMO = {"goals": (0.87, 1.13), "shots": (0.93, 1.07), "sot": (0.93, 1.07), "corners": (0.94, 1.06),
         "fouls": (1.0, 1.0), "cards": (1.0, 1.0), "yellow": (1.0, 1.0)}
K_RANGE = (3.0, 25.0)            # force de l'a priori, en matchs : bornes de l'estimation sur les données

FORM_N, FORM_W, FORM_K = 5, (1.0, 0.85, 0.7, 0.55, 0.4), 0.15
# bornes des ajustements de contexte (relatifs aux buts attendus)
BOUND = {"form": 0.05, "split": 0.04, "h2h": 0.03, "rest": 0.03, "stakes": 0.03, "total": 0.10}
REF_K = 10                       # arbitre : matchs d'a priori (rétrécissement vers la moyenne de la ligue)
REF_MIN_N = 3
REF_BOUND = {"cards": (0.80, 1.25), "fouls": (0.90, 1.10)}
H2H_KEEP, H2H_DAYS = 3, 1100

CALENDAR = {"BRA", "USA", "JPN", "CHN", "NOR", "SWE", "FIN", "IRL"}           # saison sur l'année civile
STAKES_DIVS = {"E0", "E1", "E2", "E3", "SP1", "SP2", "I1", "I2", "D1", "D2", "F1", "F2", "N1", "P1", "T1",
               "BRA", "NOR", "SWE", "JPN", "CHN", "FIN", "IRL"}               # championnat à tableau unique
TIER2 = {"E1", "E2", "E3", "SP2", "I2", "D2", "F2"}
RELEGATED = {"E0": 3, "SP1": 3, "I1": 3, "D1": 2, "F1": 2, "N1": 2, "P1": 2, "E1": 3, "E2": 4, "E3": 2, "SP2": 4,
             "I2": 3, "D2": 2, "F2": 2}

FITS = 0                         # nombre d'ajustements faits par ce processus (tests : le cache évite de recalculer)
_memo = {}                       # div -> (jour, modèle | None)
_memo_stamp = {}                 # div -> empreinte des CSV (_stamp) au moment où le modèle de _memo a été fait
_names = {}


def reload():
    """Oublie les modèles et les réglages mesurés en mémoire (après une actualisation des sources)."""
    _memo.clear(), _memo_stamp.clear(), _names.clear(), _stamp_memo.clear(), _tuned.clear()


# ---------- réglages mesurés (data/tuning.json, écrit par kelly/tune.py) ----------

TUNING_FILE = "tuning.json"
HL_RANGE = (20, 1000)            # demi-vie acceptée dans tuning.json (jours) ; hors bornes = ignorée
GW_RANGE = (0.0, 0.8)            # poids du modèle (buts) accepté dans tuning.json
_tuned = {}                      # chemin -> (signature du fichier, contenu)


def tuning():
    """Contenu de data/tuning.json ({} si absent ou illisible), gardé en mémoire et relu dès que le fichier change
    (le bot qui tourne en continu voit donc un réglage fraîchement mesuré sans redémarrer). Ne lève jamais."""
    p = store.path(TUNING_FILE)
    sig = store.version(TUNING_FILE)
    if sig is None:
        _tuned.pop(p, None)
        return {}
    hit = _tuned.get(p)
    if hit and hit[0] == sig:
        return hit[1]
    data = store.load(TUNING_FILE, {})
    data = data if isinstance(data, dict) else {}
    _tuned[p] = (sig, data)
    return data


def _tuned_num(section, div, lo, hi):
    v = (tuning().get(section) or {}).get(div)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
        return None
    return v


def hl_for(div):
    """Demi-vie (jours) à utiliser pour la ligue `div` : valeur mesurée (data/tuning.json) sinon HALF_LIFE."""
    v = _tuned_num("half_life", div, *HL_RANGE) if div else None
    return HALF_LIFE if v is None else v


def goal_weight(div):
    """Poids mesuré du modèle de buts face aux books pour la ligue `div` (0 à 0,8), ou None si non mesuré : l'appelant
    garde alors config.MODEL_WEIGHT["goals"]."""
    return _tuned_num("goal_weight", div, *GW_RANGE) if div else None


def today():
    from .sources import state
    return state.now().date()


def _r(x, n=3):
    return round(x, n)


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


# ---------- lois de probabilité ----------

def pois_pmf(mean, cap=14):
    """[P(X=k) pour k = 0..cap] d'une loi de Poisson (récurrence stable)."""
    mean = max(mean, 1e-9)
    out = [math.exp(-mean)]
    for k in range(1, cap + 1):
        out.append(out[-1] * mean / k)
    return out


def nb_pmf(mean, alpha=0.0, cap=None):
    """[P(X=k)] d'une binomiale négative de moyenne `mean` et variance mean + alpha x mean^2 (alpha = 0 : Poisson)."""
    mean = max(mean, 1e-9)
    if cap is None:
        sd = math.sqrt(mean + alpha * mean * mean)
        cap = int(mean + 9 * sd) + 6
    if alpha < 1e-6:
        return pois_pmf(mean, cap)
    r = 1.0 / alpha
    p0 = (r / (r + mean)) ** r
    out = [p0]
    q = mean / (r + mean)
    for k in range(cap):
        out.append(out[-1] * (k + r) / (k + 1) * q)
    return out


def p_over(pmf, point):
    """P(X > point) : pour une ligne entière (9), exactement `point` perd le pari « plus de »."""
    k = int(math.floor(point))
    return max(0.0, 1.0 - sum(pmf[:k + 1]))


def tau(x, y, lh, la, rho):
    """Correction de Dixon-Coles des scores faibles."""
    if x == 0 and y == 0:
        return 1 - lh * la * rho
    if x == 0 and y == 1:
        return 1 + lh * rho
    if x == 1 and y == 0:
        return 1 + la * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def score_matrix(lh, la, rho=0.0, cap=12):
    """M[i][j] = P(domicile i, extérieur j) : deux Poisson indépendants + correction de Dixon-Coles, somme = 1."""
    ph, pa = pois_pmf(lh, cap), pois_pmf(la, cap)
    m = [[ph[i] * pa[j] for j in range(cap + 1)] for i in range(cap + 1)]
    for (i, j) in ((0, 0), (0, 1), (1, 0), (1, 1)):
        m[i][j] *= max(0.0, tau(i, j, lh, la, rho))
    s = sum(map(sum, m))
    return [[x / s for x in row] for row in m]


def result_probs(m):
    """(P domicile, P nul, P extérieur) d'une matrice de scores."""
    ph = sum(m[i][j] for i in range(len(m)) for j in range(i))
    pd = sum(m[i][i] for i in range(len(m)))
    return ph, pd, 1 - ph - pd


def goal_pmf(m, side=None):
    """Loi du total de buts (side=None), du domicile (0) ou de l'extérieur (1)."""
    n = len(m)
    if side == 0:
        return [sum(m[i]) for i in range(n)]
    if side == 1:
        return [sum(m[i][j] for i in range(n)) for j in range(n)]
    out = [0.0] * (2 * n - 1)
    for i in range(n):
        for j in range(n):
            out[i + j] += m[i][j]
    return out


# ---------- ajustement ----------

def _pair(m, stat):
    """(domicile, extérieur) d'une statistique pour un match, ou None. Cartons = jaunes + rouges."""
    if stat == "goals":
        return m["hg"], m["ag"]
    if stat == "cards":
        if m.get("hy") is None or m.get("ay") is None:
            return None
        return m["hy"] + (m.get("hr") or 0), m["ay"] + (m.get("ar") or 0)
    h, a = COLS[stat]
    xh, xa = m.get(h), m.get(a)
    return None if xh is None or xa is None else (xh, xa)


def _norm(x, w):
    """Divise x par sa moyenne pondérée par w ; renvoie cette moyenne."""
    s = sum(a * b for a, b in zip(x, w)) / (sum(w) or 1.0)
    if s > 0:
        for i in range(len(x)):
            x[i] /= s
    return s or 1.0


def _ipf(rows, n, wt, m, H, A, D, SA, SD, ka, kd, A0, D0, iters=80, tol=1e-6):
    """Mises à jour de Maher : chaque force = (marqué + a priori) / (attendu + a priori), puis H et m. Renvoie
    (m, H, A, D, expositions) ; `ka`, `kd` : force de l'a priori en événements (rétrécie par E2/E : poids décroissants)."""
    EA = ED = EA2 = ED2 = None
    for _ in range(iters):
        EA, EA2 = [0.0] * n, [0.0] * n
        for hi, ai, xh, xa, w in rows:
            e = w * m * H * D[ai]
            EA[hi] += e
            EA2[hi] += e * w
            e = w * m * D[hi]
            EA[ai] += e
            EA2[ai] += e * w
        newA = []
        for i in range(n):
            kk = ka * (EA2[i] / EA[i] if EA[i] > 0 else 1.0)
            newA.append((SA[i] + kk * A0[i]) / (EA[i] + kk) if EA[i] + kk > 0 else 1.0)
        m *= _norm(newA, wt)
        ED, ED2 = [0.0] * n, [0.0] * n
        for hi, ai, xh, xa, w in rows:
            e = w * m * newA[ai]
            ED[hi] += e
            ED2[hi] += e * w
            e = w * m * H * newA[hi]
            ED[ai] += e
            ED2[ai] += e * w
        newD = []
        for i in range(n):
            kk = kd * (ED2[i] / ED[i] if ED[i] > 0 else 1.0)
            newD.append((SD[i] + kk * D0[i]) / (ED[i] + kk) if ED[i] + kk > 0 else 1.0)
        m *= _norm(newD, wt)
        num = den = 0.0
        for hi, ai, xh, xa, w in rows:
            num += w * xh
            den += w * m * newA[hi] * newD[ai]
        H = num / den if den > 0 else H
        num = den = 0.0
        for hi, ai, xh, xa, w in rows:
            num += w * (xh + xa)
            den += w * (H * newA[hi] * newD[ai] + newA[ai] * newD[hi])
        m = num / den if den > 0 else m
        delta = max(max(abs(a - b) for a, b in zip(newA, A)), max(abs(a - b) for a, b in zip(newD, D)))
        A, D = newA, newD
        if delta < tol:
            break
    return m, H, A, D, (EA, EA2, ED, ED2)


def _eb_k(S, E, E2, mu):
    """Force de l'a priori (en événements) par la méthode des moments : variance observée des forces moins la variance
    d'échantillonnage = variance réelle entre équipes ; k = 1 / variance, borné à K_RANGE matchs."""
    vals = []
    med = sorted(E)[len(E) // 2]
    for s, e, e2 in zip(S, E, E2):
        if e < 0.5 * mu or e < 0.5 * med:
            continue                                       # équipe très peu observée : n'informe pas sur la variance entre équipes
        a = s / e
        vals.append((a - 1) ** 2 - max(a, 0.3) * e2 / (e * e))
    t2 = sum(vals) / len(vals) if vals else 0.0
    lo, hi = 1.0 / (K_RANGE[1] * mu), 1.0 / (K_RANGE[0] * mu)
    return 1.0 / _clip(t2, lo, hi)


def fit_stat(rows, n, promo=None, stat="goals", iters=80):
    """Ajuste une statistique. rows = [(i_dom, i_ext, x_dom, x_ext, poids)] ; promo = indices des équipes promues.
    -> {m, h, A, D, W (matchs pondérés par équipe), k (a priori att, déf), alpha, atot, [rho]}."""
    tw = sum(r[4] for r in rows)
    m = max(sum(w * (xh + xa) for _, _, xh, xa, w in rows) / (2 * tw), 1e-6)
    SA, SD, wt = [0.0] * n, [0.0] * n, [0.0] * n
    for hi, ai, xh, xa, w in rows:
        SA[hi] += w * xh
        SA[ai] += w * xa
        SD[hi] += w * xa
        SD[ai] += w * xh
        wt[hi] += w
        wt[ai] += w
    pa, pd = PROMO.get(stat, (1.0, 1.0))
    A0 = [pa if i in (promo or ()) else 1.0 for i in range(n)]
    D0 = [pd if i in (promo or ()) else 1.0 for i in range(n)]
    A, D = list(A0), list(D0)
    mu = m * 1.1
    weak = 0.3 * mu
    m, H, A, D, ex = _ipf(rows, n, wt, m, 1.15, A, D, SA, SD, weak, weak, A0, D0, iters)
    mu = m * (1 + H) / 2
    ka, kd = _eb_k(SA, ex[0], ex[1], mu), _eb_k(SD, ex[2], ex[3], mu)
    m, H, A, D, ex = _ipf(rows, n, wt, m, H, A, D, SA, SD, ka, kd, A0, D0, iters)
    num = den = nt = dt = 0.0
    for hi, ai, xh, xa, w in rows:
        lh, la = m * H * A[hi] * D[ai], m * A[ai] * D[hi]
        num += w * ((xh - lh) ** 2 - lh + (xa - la) ** 2 - la)
        den += w * (lh * lh + la * la)
        big = lh + la
        nt += w * ((xh + xa - big) ** 2 - big)
        dt += w * big * big
    adj = 2 * len(rows) / max(2 * len(rows) - 2 * n - 3, 1)               # degrés de liberté perdus par l'ajustement
    out = {"m": m, "h": H, "A": A, "D": D, "W": wt, "k": (ka / mu, kd / mu),
           "alpha": _clip(adj * num / den if den > 0 else 0.0, 0.0, 0.6),
           "atot": _clip(adj * nt / dt if dt > 0 else 0.0, 0.0, 0.6)}
    return out


def fit_rho(rows, fit):
    """rho de Dixon-Coles par maximum de vraisemblance (recherche sur [-0.25, 0.15])."""
    m, H, A, D = fit["m"], fit["h"], fit["A"], fit["D"]
    low = [(m * H * A[hi] * D[ai], m * A[ai] * D[hi], xh, xa, w) for hi, ai, xh, xa, w in rows if xh <= 1 and xa <= 1]
    best, best_ll = 0.0, -1e18
    for r100 in range(-25, 16):
        rho, ll = r100 / 100, 0.0
        for lh, la, xh, xa, w in low:
            t = tau(xh, xa, lh, la, rho)
            if t <= 1e-9:
                ll = -1e18
                break
            ll += w * math.log(t)
        if ll > best_ll:
            best, best_ll = rho, ll
    return best


def _ord(s, memo={}):
    v = memo.get(s)
    if v is None:
        v = memo[s] = date.fromisoformat(s).toordinal()
    return v


def build(matches, on, hl=HALF_LIFE, stats=STATS, div=None):
    """Modèle complet d'une ligue à la date `on` (date), à partir des matchs normalisés (footballdata.parse_csv) joués
    AVANT `on`. None si moins de MIN_MATCHES matchs. Dict JSON : {v, div, on, n, last, stats:{stat:{...,t:{équipe:[att,
    déf, matchs pondérés, matchs]}}}, ctx:{form, table, refs, lg, h2h}}."""
    global FITS
    on_s = on.isoformat()
    ms = sorted((m for m in matches if m["date"] < on_s and m.get("hg") is not None), key=lambda m: m["date"])
    if len(ms) < MIN_MATCHES:
        return None
    FITS += 1
    teams = sorted({m["home"] for m in ms} | {m["away"] for m in ms})
    idx = {t: i for i, t in enumerate(teams)}
    n, on_o = len(teams), on.toordinal()
    first = {}
    for m in ms:
        first.setdefault(m["home"], m["date"])
        first.setdefault(m["away"], m["date"])
    start, span = _ord(ms[0]["date"]), on_o - _ord(ms[0]["date"])
    promo = {idx[t] for t, d in first.items() if span >= 250 and _ord(d) - start > 200 and on_o - _ord(d) <= 300}
    ln2 = math.log(2) / hl
    wts = [math.exp(-ln2 * (on_o - _ord(m["date"]))) for m in ms]
    model = {"v": VERSION, "div": div, "on": on_s, "n": len(ms), "last": ms[-1]["date"], "hl": hl, "stats": {}}
    for stat in stats:
        rows, cnt, seen = [], [0] * n, 0
        for m, w in zip(ms, wts):
            if w < 0.004:
                continue                                   # trop ancien pour compter
            seen += 1
            p = _pair(m, stat)
            if p is None:
                continue
            rows.append((idx[m["home"]], idx[m["away"]], p[0], p[1], w))
            cnt[idx[m["home"]]] += 1
            cnt[idx[m["away"]]] += 1
        if len(rows) < MIN_ROWS:
            continue
        try:
            f = fit_stat(rows, n, promo, stat)
        except (ArithmeticError, ValueError):               # donnée dégénérée (colonne vide, tout à zéro) : statistique ignorée
            if stat == "goals":
                raise
            continue
        d = {"m": _r(f["m"], 4), "h": _r(f["h"], 4), "alpha": _r(f["alpha"], 4), "atot": _r(f["atot"], 4),
             "cov": _r(len(rows) / seen, 3), "rows": len(rows), "k": [_r(x, 1) for x in f["k"]],
             "t": {teams[i]: [_r(f["A"][i], 4), _r(f["D"][i], 4), _r(f["W"][i], 1), cnt[i]] for i in range(n) if cnt[i]}}
        if stat == "goals":
            d["rho"] = _r(fit_rho(rows, f), 3)
            d["promo"] = sorted(teams[i] for i in promo)
        model["stats"][stat] = d
        if stat == "goals":
            model["ctx"] = _ctx_tables(ms, wts, model, on, div)
    if "goals" not in model["stats"]:
        return None
    return model


# ---------- contexte : forme, face-à-face, classement, arbitres ----------

def _sig(model, h, a):
    g = model["stats"]["goals"]
    m, H = g["m"], g["h"]
    th, ta = g["t"][h], g["t"][a]
    return m * H * th[0] * ta[1], m * ta[0] * th[1]


def standings(matches):
    """Classement d'une liste de matchs {home, away, hg, ag} : [{team, p, pts, gf, ga, gd}] trié par points, différence
    de buts, buts marqués (le départage officiel varie selon les ligues : approximation)."""
    t = {}
    for m in matches:
        for team, gf, ga in ((m["home"], m["hg"], m["ag"]), (m["away"], m["ag"], m["hg"])):
            r = t.setdefault(team, {"team": team, "p": 0, "pts": 0, "gf": 0, "ga": 0})
            r["p"] += 1
            r["gf"] += gf
            r["ga"] += ga
            r["pts"] += 3 if gf > ga else (1 if gf == ga else 0)
    out = sorted(t.values(), key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"], r["team"]))
    for r in out:
        r["gd"] = r["gf"] - r["ga"]
    return out


def season_start(on, div):
    """Début (date) de la saison en cours : 1er juillet, ou 1er janvier pour les championnats en année civile."""
    if div in CALENDAR:
        return date(on.year, 1, 1)
    return date(on.year if on.month >= 7 else on.year - 1, 7, 1)


def zones(div, n):
    """(nombre de relégués, places du haut du tableau : Europe ou promotion) d'un championnat de n équipes."""
    rel = RELEGATED.get(div) or max(2, round(n * 0.15))
    top = {20: 6, 18: 5, 16: 4}.get(n, max(2, round(n * 0.3)))
    return rel, top


def stakes(rows, team, rel, top, total=None, tier2=False):
    """Enjeu d'une équipe d'après le classement `rows` (standings) : dict {pos, pts, played, left, n, kind, adj, text}.
    kind : titre | titre_acquis | maintien | condamne | haut | rien | milieu | debut. `adj` : ajustement relatif des buts
    attendus, borné à ±BOUND["stakes"], d'autant plus fort que la saison est avancée."""
    n = len(rows)
    i = next((k for k, r in enumerate(rows) if r["team"] == team), None)
    if i is None:
        return None
    r = rows[i]
    total = total or 2 * (n - 1)
    left = max(0, total - r["p"])
    room = 3 * left                                         # points encore à prendre
    frac = r["p"] / total if total else 0
    out = {"pos": i + 1, "pts": r["pts"], "played": r["p"], "left": left, "n": n, "kind": "milieu", "adj": 0.0, "text": ""}
    if n < 8 or frac < 0.5:
        out["kind"] = "debut"
        return out
    pts_at = lambda pos: rows[min(max(pos, 1), n) - 1]["pts"]               # noqa: E731
    urgency = _clip((frac - 0.5) / 0.35, 0.0, 1.0)
    b = BOUND["stakes"]
    me = f"{team} {i + 1}e ({r['pts']} pts en {r['p']} matchs)"

    def done(kind, adj, text):
        out.update(kind=kind, adj=_clip(adj * urgency, -b, b), text=f"{me} : {text}")
        return out

    gap1 = pts_at(1) - r["pts"]
    if i == 0:
        lead = r["pts"] - pts_at(2)
        if lead > room:
            return done("titre_acquis", -0.02, "titre quasi acquis")
        if lead <= 0.5 * room:
            return done("titre", 0.02, f"course au titre (+{lead} pt{'s' if lead > 1 else ''} sur le 2e)")
    elif i < 3 and gap1 <= 0.35 * room:
        return done("titre", 0.02, f"course au titre (à {gap1} pt{'s' if gap1 > 1 else ''} du leader)")
    safe = n - rel
    if i + 1 > safe:
        gap = pts_at(safe) - r["pts"]
        if gap > room:
            return done("condamne", -0.02, f"maintien improbable (à {gap} pts de la zone sûre)")
        if gap <= 0.7 * room:
            return done("maintien", 0.02, f"lutte pour le maintien (à {gap} pt{'s' if gap > 1 else ''} de la zone sûre)")
    elif left <= 14:                                   # à l'abri, mais pas encore sûr de l'être
        gap = r["pts"] - pts_at(safe + 1)
        if gap <= 0.3 * room:
            return done("maintien", 0.01, f"doit encore assurer son maintien (+{gap} pt{'s' if gap > 1 else ''})")
    if i + 1 > top:
        gap = pts_at(top) - r["pts"]
        fight = gap <= 0.5 * room
    else:
        gap = r["pts"] - pts_at(top + 1)
        fight = gap <= 0.25 * room
    if fight and left <= 14:
        return done("haut", 0.01, "course à la promotion" if tier2 else "course aux places européennes")
    if left <= 8:
        return done("rien", -0.03, "plus grand-chose à jouer")
    return done("milieu", 0.0, "milieu de tableau")


def _ctx_tables(ms, wts, model, on, div):
    """Tables de contexte du modèle (JSON compact) : forme, classement, arbitres, moyennes de ligue, face-à-face."""
    g = model["stats"]["goals"]
    T = g["t"]
    per, hs, es = {}, {}, {}
    c_split = 8.0
    for mt, w in zip(ms, wts):
        h, a = mt["home"], mt["away"]
        if h not in T or a not in T:
            continue
        lh, la = _sig(model, h, a)
        per.setdefault(h, []).append((mt["date"], mt["hg"], mt["ag"], lh, la))
        per.setdefault(a, []).append((mt["date"], mt["ag"], mt["hg"], la, lh))
        s = hs.setdefault(h, [0.0, 0.0, 0.0, 0.0])
        s[0] += w * mt["hg"]
        s[1] += w * lh
        s = es.setdefault(a, [0.0, 0.0, 0.0, 0.0])
        s[0] += w * mt["ag"]
        s[1] += w * la
    form = {}
    for t, recs in per.items():
        last = recs[-FORM_N:][::-1]                            # le plus récent d'abord
        sw = [FORM_W[k] for k in range(len(last))]
        gf = sum(w * r[1] for w, r in zip(sw, last))
        ef = sum(w * r[3] for w, r in zip(sw, last))
        ga = sum(w * r[2] for w, r in zip(sw, last))
        ea = sum(w * r[4] for w, r in zip(sw, last))
        sh, se = hs.get(t, [0, 0, 0, 0]), es.get(t, [0, 0, 0, 0])
        split = math.sqrt(((sh[0] + c_split) / (sh[1] + c_split)) / ((se[0] + c_split) / (se[1] + c_split)))
        form[t] = {"s": "".join("V" if r[1] > r[2] else ("N" if r[1] == r[2] else "D") for r in recs[-FORM_N:]),
                   "rf": _r((gf - ef) / ef if ef else 0.0), "rd": _r((ga - ea) / ea if ea else 0.0),
                   "gf": _r(sum(r[1] for r in recs[-FORM_N:]) / max(len(last), 1), 2),
                   "ef": _r(sum(r[3] for r in recs[-FORM_N:]) / max(len(last), 1), 2),
                   "sp": _r(_clip(split, 1 - BOUND["split"], 1 + BOUND["split"]), 4),
                   "ld": [r[0] for r in recs[-4:]]}
    ctx = {"form": form}
    if div in STAKES_DIVS:
        s0 = season_start(on, div).isoformat()
        cur = [x for x in ms if x["date"] >= s0]
        rows = standings(cur)
        if len(rows) >= 8:
            ctx["table"] = {"n": len(rows), "rows": [[r["team"], r["p"], r["pts"], r["gf"], r["ga"]] for r in rows]}
    # arbitres et moyennes de ligue sur les 2 dernières saisons
    refs, tot_c, tot_f, n_c, n_f, goals = {}, 0.0, 0.0, 0, 0, 0.0
    for mt in ms:
        c = _pair(mt, "cards")
        f = None if mt.get("hf") is None or mt.get("af") is None else mt["hf"] + mt["af"]
        goals += mt["hg"] + mt["ag"]
        if c is not None:
            tot_c += c[0] + c[1]
            n_c += 1
        if f is not None:
            tot_f += f
            n_f += 1
        if mt.get("referee"):
            r = refs.setdefault(mt["referee"], [0, 0.0, 0, 0.0, 0])
            if c is not None:
                r[0] += 1
                r[1] += c[0] + c[1]
            if f is not None:
                r[2] += 1
                r[3] += f
    ctx["lg"] = {"cards": _r(tot_c / n_c, 3) if n_c else None, "fouls": _r(tot_f / n_f, 3) if n_f else None,
                 "goals": _r(goals / len(ms), 3)}
    ctx["refs"] = {k: [v[0], _r(v[1] / v[0], 2) if v[0] else None, v[2], _r(v[3] / v[2], 2) if v[2] else None]
                   for k, v in refs.items() if v[0] >= REF_MIN_N or v[2] >= REF_MIN_N}
    # face-à-face : « AAMMJJ:D:h-a » par couple « i|j » d'indices d'équipes triées (D = la première jouait à domicile)
    cut = (on - timedelta(days=H2H_DAYS)).isoformat()
    h2h, order = {}, sorted({x["home"] for x in ms} | {x["away"] for x in ms})
    pos = {t: i for i, t in enumerate(order)}
    for mt in reversed(ms):
        if mt["date"] < cut:
            break
        a, b = sorted((mt["home"], mt["away"]))
        lst = h2h.setdefault(f"{pos[a]}|{pos[b]}", [])
        if len(lst) < H2H_KEEP:
            lst.append(f"{mt['date'][2:4]}{mt['date'][5:7]}{mt['date'][8:10]}:{'1' if mt['home'] == a else '0'}:{mt['hg']}-{mt['ag']}")
    ctx["h2h"], ctx["teams"] = h2h, order
    return ctx


# ---------- cache ----------

def _file(div):
    return f"strength_{div}.json"


def _save(div, model):
    """Écrit le cache JSON compact de la ligue ; un disque en lecture seule ne doit pas faire perdre le modèle."""
    try:
        store.save(_file(div), model, compact=True)
    except OSError as e:
        print(f"[strength] cache {div} non écrit : {e}", file=sys.stderr)


def load_matches(div):
    """Matchs de la ligue déjà téléchargés (cache disque de footballdata, AUCUNE requête réseau) : 3 saisons."""
    from .sources import footballdata as fd
    cur = fd.current_season()
    p1 = fd.previous_season(cur)
    return fd.cached_history([div], [cur, p1, fd.previous_season(p1)])


_stamp_memo = []                 # [(signature du document, date d'actualisation, instant de la dernière interrogation)]
STAMP_RECHECK_SECONDS = 1.0      # le document des profils football-data n'est pas reinterrogé plus d'une fois par seconde
_stamp_clock = time.monotonic


def _stamp():
    """Date d'actualisation des profils football-data (change quand refresh_all a téléchargé de nouveaux CSV), relue dès que leur document
    change dans la base : un bot qui tourne en continu ne garde plus l'empreinte de son démarrage."""
    try:
        now = _stamp_clock()
        if _stamp_memo and now - _stamp_memo[0][2] < STAMP_RECHECK_SECONDS:
            return _stamp_memo[0][1]
        from .sources import footballdata as fd
        sig = store.version(fd.PROFILE_FILE)
        if not _stamp_memo or _stamp_memo[0][0] != sig:
            _stamp_memo[:] = [(sig, str(fd.profiles().get("updated") or ""), now)]
        else:
            _stamp_memo[0] = _stamp_memo[0][:2] + (now,)
    except Exception:                                    # noqa: BLE001
        return ""
    return _stamp_memo[0][1]


def get(div, on=None, loader=None):
    """Modèle de la ligue `div` (code football-data) : mémoire > cache du jour (data/strength_<DIV>.json) > ajustement
    sur les CSV en cache. Sans données brutes : dernier cache de moins de STALE_DAYS jours (model["stale"] = jours).
    None si rien d'exploitable. Ne lève jamais."""
    try:
        on = on or today()
        hit = _memo.get(div)
        if hit and hit[0] == on.isoformat() and _memo_stamp.get(div) == _stamp():     # CSV actualisés depuis (autre processus) : modèle refait
            return hit[1]
        hl = hl_for(div)
        key = f"{on.isoformat()}|{_stamp()}" + ("" if hl == HALF_LIFE else f"|hl{hl}")        # demi-vie mesurée : cache du jour refait une fois
        disk = store.load(_file(div), None)
        disk = disk if isinstance(disk, dict) and disk.get("v") == VERSION else None
        if disk and disk.get("key") == key:
            _memo[div] = (key.split("|")[0], disk)
            _memo_stamp[div] = _stamp()
            return disk
        model = build((loader or load_matches)(div), on, hl, div=div)
        if model:
            model["key"] = key
            _save(div, model)
        elif disk and 0 <= (on - date.fromisoformat(disk["on"])).days <= STALE_DAYS:
            model = dict(disk, stale=(on - date.fromisoformat(disk["on"])).days)
        _memo[div] = (key.split("|")[0], model)
        _memo_stamp[div] = _stamp()
        return model
    except Exception as e:                               # noqa: BLE001 - jamais bloquant
        print(f"[strength] {div} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ---------- prévision d'un match ----------

def find_team(model, name):
    from .sources import names
    ts = model["stats"]["goals"]["t"]
    key = (model.get("div"), model.get("on"), model.get("hl"), len(ts), name)     # demi-vie : même date, autres équipes retenues
    if key not in _names:
        _names[key] = names.best_team(name, ts)
    return _names[key]


def _expect(model, stat, h, a):
    """Valeur attendue (domicile, extérieur) d'une statistique pour un adversaire et un contexte neutres."""
    s = model["stats"][stat]
    th, ta = s["t"].get(h), s["t"].get(a)
    if not th or not ta:
        return None
    return s["m"] * s["h"] * th[0] * ta[1], s["m"] * ta[0] * th[1]


def _quality(model, stat, h, a):
    t = model["stats"]["goals"]["t"]
    q = math.sqrt(min(1.0, t[h][2] / NEFF_FULL) * min(1.0, t[a][2] / NEFF_FULL))
    q *= model["stats"][stat]["cov"] * min(1.0, model["n"] / 150)
    if model.get("stale"):
        q *= max(0.5, 1 - model["stale"] / 30)
    return _clip(q, 0.0, 1.0)


def _rest_adj(days, played14):
    if days is None or days > 21:
        return 0.0
    adj = -0.03 if days <= 3 else (-0.015 if days == 4 else 0.0)
    if played14 >= 4:
        adj -= 0.01
    return _clip(adj, -BOUND["rest"], 0.0)


def referee_factor(model, referee, kind):
    """(facteur borné, n matchs, moyenne de l'arbitre, moyenne de la ligue, nom) pour « cards » ou « fouls » ; None si
    l'arbitre est inconnu ou a moins de REF_MIN_N matchs. Rétréci vers 1 : (n x moy + REF_K x ligue) / (n + REF_K)."""
    ctx = model.get("ctx") or {}
    refs = ctx.get("refs") or {}
    if not referee or not refs:
        return None
    from .sources import features
    name = referee if referee in refs else features.match_referee(referee, refs)      # « Michael Oliver, England » -> « M Oliver » ; jamais une devinette
    if not name:
        return None
    n, avg = (refs[name][0], refs[name][1]) if kind == "cards" else (refs[name][2], refs[name][3])
    lg = (ctx.get("lg") or {}).get(kind)
    if not n or n < REF_MIN_N or avg is None or not lg:
        return None
    est = (n * avg + REF_K * lg) / (n + REF_K)
    lo, hi = REF_BOUND[kind]
    return _clip(est / lg, lo, hi), n, avg, lg, name


def _h2h(model, h, a):
    """(ajustement, rencontres) : rencontres récentes, écart de buts réel moins attendu, très petit poids. Les
    rencontres sont [(date, h jouait à domicile, buts domicile, buts extérieur)], la plus récente d'abord."""
    a1, b1 = sorted((h, a))
    c = model.get("ctx") or {}
    order = c.get("teams") or []
    items = c.get("h2h", {}).get(f"{order.index(a1)}|{order.index(b1)}") if a1 in order and b1 in order else None
    items = items or []
    meets, resid = [], []
    g = model["stats"]["goals"]
    for s in items:
        d, first_home, sc = s.split(":")
        gh, ga_ = (int(x) for x in sc.split("-"))
        home_is_h = (first_home == "1") == (h == a1)               # `h` jouait à domicile ce jour-là
        meets.append((f"20{d[:2]}-{d[2:4]}-{d[4:]}", home_is_h, gh, ga_))
        gd_h = (gh - ga_) if home_is_h else (ga_ - gh)               # écart de buts de h
        th, ta = g["t"][h], g["t"][a]
        mm, HH = g["m"], g["h"]
        exp_h = mm * (HH * th[0] * ta[1] if home_is_h else th[0] * ta[1])
        exp_a = mm * (ta[0] * th[1] if home_is_h else HH * ta[0] * th[1])
        resid.append(gd_h - (exp_h - exp_a))
    if not resid:
        return 0.0, meets
    n = len(resid)
    mean = sum(resid) / n * n / (n + 2)
    return _clip(0.02 * mean, -BOUND["h2h"], BOUND["h2h"]), meets


def _with_newcomer(model, name):
    """Copie du modèle avec une équipe jamais vue (promue, pas encore jouée) à l'a priori des promus et 0 match connu :
    qualité nulle, donc le modèle pèse moitié moins et le pronostic sans cote est écarté."""
    out = dict(model)
    out["stats"] = {}
    for stat, s in model["stats"].items():
        pa, pd = PROMO.get(stat, (1.0, 1.0))
        out["stats"][stat] = dict(s, t=dict(s["t"], **{name: [pa, pd, 0.0, 0]}))
    out["stats"]["goals"]["promo"] = list(model["stats"]["goals"].get("promo", [])) + [name]
    return out


def predict(model, home, away, on=None, referee=None, ctx=True):
    """Prévision d'un match. dict : {home, away, lh, la, rho, q, qs, stats:{stat:{h, a, al, at}}, notes, tm, base, ...}
    ou None si une équipe est inconnue. `ctx=False` : force pure, sans facteurs de contexte."""
    if not model or not model.get("stats", {}).get("goals"):
        return None
    h, a = find_team(model, home), find_team(model, away)
    unknown = None
    if bool(h) != bool(a) and len(model["stats"]["goals"]["t"]) >= 14:
        unknown = home if not h else away                        # équipe absente des données : promue (a priori prudent)
        model = _with_newcomer(model, unknown)
        h, a = (unknown, a) if not h else (h, unknown)
    if not h or not a or h == a:
        return None
    on = on or today()
    g = model["stats"]["goals"]
    lh0, la0 = _expect(model, "goals", h, a)
    c = model.get("ctx") or {}
    form = c.get("form") or {}
    adj_h, adj_a, notes = [], [], []                                    # (code, ajustement) ; notes : (code, texte, adj_h, adj_a)
    info = {"form": {}, "rest": {}, "h2h": [], "stakes": {}, "referee": None}
    if ctx:
        fh, fa = form.get(h), form.get(a)
        if fh and fa:
            info["form"] = {"home": fh["s"], "away": fa["s"]}
            B = BOUND["form"]
            ah = _clip(0.5 * FORM_K * (fh["rf"] + fa["rd"]), -B, B)
            aa = _clip(0.5 * FORM_K * (fa["rf"] + fh["rd"]), -B, B)
            adj_h.append(("form", ah)), adj_a.append(("form", aa))
            for t, d, rec in ((h, ah, fh), (a, aa, fa)):
                if abs(d) >= 0.01:
                    notes.append(("form", f"forme de {t} {rec['s']} ({rec['gf']:.1f} but/match pour {rec['ef']:.1f} attendus)",
                                  d if t == h else 0.0, d if t == a else 0.0))
            sh, sa = fh["sp"] - 1, 1 / fa["sp"] - 1
            adj_h.append(("split", sh)), adj_a.append(("split", sa))
            if abs(sh) >= 0.015:
                notes.append(("split", f"{h} {'plus fort' if sh > 0 else 'moins fort'} à domicile que sa force globale", sh, 0.0))
            if abs(sa) >= 0.015:
                notes.append(("split", f"{a} {'plus fort' if sa > 0 else 'moins fort'} à l'extérieur que sa force globale", 0.0, sa))
            for t, rec, key in ((h, fh, "home"), (a, fa, "away")):
                last = rec["ld"][-1] if rec.get("ld") else None
                if last:
                    days = (on - date.fromisoformat(last)).days
                    p14 = sum(1 for d in rec["ld"] if 0 <= (on - date.fromisoformat(d)).days <= 14)
                    ad = _rest_adj(days, p14)
                    info["rest"][key] = days
                    (adj_h if key == "home" else adj_a).append(("rest", ad))
                    if ad <= -0.01:
                        notes.append(("rest", f"{t} a joué il y a {days} j (championnat)", ad if key == "home" else 0.0,
                                      ad if key == "away" else 0.0))
        e, meets = _h2h(model, h, a)
        info["h2h"] = meets
        if abs(e) > 0:
            adj_h.append(("h2h", e)), adj_a.append(("h2h", -e))
            if abs(e) >= 0.01:
                notes.append(("h2h", f"face-à-face récent en faveur de {h if e > 0 else a}", e, -e))
        tb = c.get("table")
        if tb:
            rows = [{"team": r[0], "p": r[1], "pts": r[2], "gf": r[3], "ga": r[4]} for r in tb["rows"]]
            rel, top = zones(model.get("div"), tb["n"])
            for t, key, lst in ((h, "home", adj_h), (a, "away", adj_a)):
                sk = stakes(rows, t, rel, top, tier2=model.get("div") in TIER2)
                if sk:
                    info["stakes"][key] = sk
                    lst.append(("stakes", sk["adj"]))
                    other = adj_a if key == "home" else adj_h
                    other.append(("stakes", -0.5 * sk["adj"]))
                    if abs(sk["adj"]) >= 0.005:
                        notes.append(("stakes", sk["text"], sk["adj"] if key == "home" else -0.5 * sk["adj"],
                                      sk["adj"] if key == "away" else -0.5 * sk["adj"]))

    def mult(lst):
        f = 1.0
        for _, x in lst:
            f *= 1 + x
        return _clip(f, 1 - BOUND["total"], 1 + BOUND["total"])

    fh_, fa_ = mult(adj_h), mult(adj_a)
    lh, la = lh0 * fh_, la0 * fa_
    stats = {"goals": {"h": lh, "a": la, "al": 0.0, "at": 0.0}}
    tm, base, qs = {}, {}, {}
    for stat, s in model["stats"].items():
        e = _expect(model, stat, h, a)
        if not e:
            continue
        m, HH = s["m"], s["h"]
        t1, t2 = s["t"][h], s["t"][a]
        tm[stat] = {"hf": m * HH * t1[0], "hc": m * t1[1], "af": m * t2[0], "ac": m * HH * t2[1], "lh": m * HH, "la": m,
                    "A": [t1[0], t2[0]], "D": [t1[1], t2[1]]}
        base[stat] = (m * HH, m)
        qs[stat] = _quality(model, stat, h, a)
        if stat != "goals":
            stats[stat] = {"h": e[0], "a": e[1], "al": s["alpha"], "at": s["atot"]}
    stats["goals"]["rho"] = g.get("rho", 0.0)
    for kind in ("cards", "fouls", "yellow"):
        if kind in stats:
            rk = "fouls" if kind == "fouls" else "cards"           # les jaunes suivent l'arbitre comme les cartons (les rouges sont rares)
            rf = referee_factor(model, referee, rk)
            if rf:
                stats[kind]["h"] *= rf[0]
                stats[kind]["a"] *= rf[0]
                stats[kind]["ref"] = rf[0]
                if kind == "cards":
                    info["referee"] = {"name": rf[4], "n": rf[1], "cards": rf[2], "lg": rf[3], "factor": rf[0]}
                elif kind == "fouls":
                    info["referee_fouls"] = {"name": rf[4], "n": rf[1], "avg": rf[2], "lg": rf[3], "factor": rf[0]}
    pred = {"div": model.get("div"), "home": h, "away": a, "lh": lh, "la": la, "lh0": lh0, "la0": la0, "rho": g.get("rho", 0.0),
            "fh": fh_, "fa": fa_, "q": qs.get("goals", 0.0), "qs": qs, "stats": stats, "notes": notes, "info": info, "tm": tm,
            "base": {"lh": base["goals"][0], "la": base["goals"][1],
                     "stats": {k: {"h": v[0], "a": v[1]} for k, v in base.items()}},
            "n": [g["t"][h][3], g["t"][a][3]], "neff": [g["t"][h][2], g["t"][a][2]], "model_n": model["n"],
            "last": model["last"], "stale": model.get("stale", 0), "promo": [h in g.get("promo", []), a in g.get("promo", [])],
            "unavailable_referee": bool(referee) and not info["referee"], "newcomer": unknown}
    pred["why"] = explain(pred)
    return pred


# ---------- grille de marchés ----------

def _lines_near(mean, alpha, lo=1, step_sd=1.35):
    """Demi-lignes de mean - step_sd x sd à mean + step_sd x sd, 9 au plus, centrées sur floor(mean) + 0,5 (pas de coupure par le
    bas : à forte dispersion les lignes au-dessus de la moyenne, donc les Under, doivent exister aussi) ; jamais sous 0,5 ni sous `lo`."""
    sd = math.sqrt(mean + alpha * mean * mean)
    c = math.floor(mean) + 0.5
    a = max(lo - 0.5, 0.5, math.floor(mean - step_sd * sd) + 0.5, c - 4)
    b = min(math.floor(mean + step_sd * sd) + 0.5, c + 4)
    pts, x = [], a
    while x <= b + 1e-9:
        pts.append(round(x, 1))
        x += 1
    return pts or [round(max(c, 0.5), 1)]


def _both(pmf, pts):
    return {str(pt): {"over": p_over(pmf, pt), "under": 1 - p_over(pmf, pt)} for pt in pts}


def grid(pred, base=False, like=None):
    """Grille complète de marchés d'un match (probabilités, pas de cotes) :
    xg [dom, ext], scores (5 plus probables), 1x2 {H,D,A}, dc {1X,X2,12}, totals {ligne: {over, under}} (0.5 à 4.5),
    btts {yes, no}, team_totals {home, away: {0.5, 1.5, 2.5}}, puis pour chaque statistique présente (corners, cards,
    shots, sot, fouls) {mean, h, a, total: {ligne: {over, under}}, home: {...}, away: {...}}.
    `base=True` : même grille pour un match moyen de la ligue (référence naïve) ; `like` : grille dont on reprend les
    lignes (pour comparer ligne à ligne avec la référence)."""
    if base:
        b = pred["base"]
        lh, la = b["lh"], b["la"]
        st = {k: {"h": v["h"], "a": v["a"], "al": pred["stats"][k]["al"], "at": pred["stats"][k]["at"]} for k, v in b["stats"].items()}
    else:
        lh, la, st = pred["lh"], pred["la"], pred["stats"]
    M = score_matrix(lh, la, pred.get("rho", 0.0))
    ph, pd, pa = result_probs(M)
    tot = goal_pmf(M)
    out = {"xg": [lh, la], "1x2": {"H": ph, "D": pd, "A": pa}, "dc": {"1X": ph + pd, "X2": pd + pa, "12": ph + pa},
           "totals": _both(tot, (0.5, 1.5, 2.5, 3.5, 4.5)),
           "btts": {"yes": 1 - sum(M[0]) - sum(M[i][0] for i in range(len(M))) + M[0][0]}}
    out["btts"]["no"] = 1 - out["btts"]["yes"]
    out["team_totals"] = {"home": _both(goal_pmf(M, 0), (0.5, 1.5, 2.5)), "away": _both(goal_pmf(M, 1), (0.5, 1.5, 2.5))}
    flat = sorted(((M[i][j], f"{i}-{j}") for i in range(len(M)) for j in range(len(M))), reverse=True)[:5]
    out["scores"] = [(s, p) for p, s in flat]
    for stat in ("corners", "cards", "yellow", "shots", "sot", "fouls"):
        s = st.get(stat)
        if not s:
            continue
        mean = s["h"] + s["a"]
        pm = nb_pmf(mean, s["at"])
        if like and stat in like:
            pts_t, pts_h, pts_a = ([float(x) for x in like[stat][k]] for k in ("total", "home", "away"))
        else:
            pts_t, pts_h, pts_a = _lines_near(mean, s["at"], 1), _lines_near(s["h"], s["al"], 0), _lines_near(s["a"], s["al"], 0)
        d = {"mean": mean, "h": s["h"], "a": s["a"], "total": _both(pm, pts_t)}
        d["home"], d["away"] = _both(nb_pmf(s["h"], s["al"]), pts_h), _both(nb_pmf(s["a"], s["al"]), pts_a)
        out[stat] = d
    return out


def p_goals_total(pred, point, name):
    """Proba du côté choisi (Over/Under) d'un total de buts, ligne quelconque (matrice de Dixon-Coles)."""
    return _side(name, p_over(goal_pmf(score_matrix(pred["lh"], pred["la"], pred.get("rho", 0.0))), point))


def p_btts(pred, name):
    g = grid(pred)
    return g["btts"]["yes" if name == "Yes" else "no"]


def p_stat_total(pred, stat, point, name):
    """Proba du côté choisi du total d'une statistique (corners, cards, shots, sot, fouls), binomiale négative."""
    s = pred["stats"][stat]
    return _side(name, p_over(nb_pmf(s["h"] + s["a"], s["at"]), point))


def _side(name, po):
    return po if name in ("Over", "Yes") else 1 - po


def lines(pred, home, away):
    """Toutes les lignes de marché à plat : [{market, name, point, p, base, family, label}] ; `market`/`name`/`point` suivent
    les clés de The Odds API quand le marché existe (totals, btts, double_chance, alternate_totals_corners/cards, h2h) et
    `team_*` / `shots` / `sot` / `fouls` sinon. `base` : proba du même pari pour un match moyen de la ligue."""
    g = grid(pred)
    b = grid(pred, base=True, like=g)
    out = []

    qs = pred.get("qs") or {}

    def add(market, name, point, p, p0, family, label, stat="goals", side=None):
        out.append({"market": market, "name": name, "point": point, "p": p, "base": p0, "family": family, "label": label,
                    "stat": stat, "q": qs.get(stat, pred["q"]), "side": side})

    for who, key, nm in ((home, "H", home), ("Nul", "D", "Draw"), (away, "A", away)):
        add("h2h", nm, None, g["1x2"][key], b["1x2"][key], "1N2", f"Victoire {who}" if key != "D" else "Match nul")
    for key, nm, label in (("1X", f"{home} or Draw", f"Double chance : {home} ou Nul"),
                           ("X2", f"Draw or {away}", f"Double chance : Nul ou {away}"),
                           ("12", f"{home} or {away}", f"Double chance : {home} ou {away}")):
        add("double_chance", nm, None, g["dc"][key], b["dc"][key], "double chance", label)
    for pt, v in g["totals"].items():
        for nm, side, word in (("Over", "over", "Plus"), ("Under", "under", "Moins")):
            add("totals", nm, float(pt), v[side], b["totals"][pt][side], "buts", f"{word} de {pt} buts")
    for nm, k, word in (("Yes", "yes", "Oui"), ("No", "no", "Non")):
        add("btts", nm, None, g["btts"][k], b["btts"][k], "les 2 marquent", f"Les 2 équipes marquent : {word}")
    for side, team in (("home", home), ("away", away)):
        for pt, v in g["team_totals"][side].items():
            for nm, k, word in (("Over", "over", "plus"), ("Under", "under", "moins")):
                add("team_totals", nm, float(pt), v[k], b["team_totals"][side][pt][k], "buts équipe", f"{team} : {word} de {pt} buts",
                    side=side)
    for stat, market in (("corners", "alternate_totals_corners"), ("cards", "alternate_totals_cards"), ("yellow", "team_yellow_total"), ("shots", "team_shots_total"),
                         ("sot", "team_sot_total"), ("fouls", "team_fouls_total")):
        if stat not in g:
            continue
        gb = b[stat]
        unit = UNIT[stat]
        for pt, v in g[stat]["total"].items():
            for nm, k, word in (("Over", "over", "Plus"), ("Under", "under", "Moins")):
                add(market, nm, float(pt), v[k], gb["total"][pt][k],
                    unit, f"{word} de {pt} {unit}", stat)
        for side, team in (("home", home), ("away", away)):
            for pt, v in g[stat][side].items():
                for nm, k, word in (("Over", "over", "plus"), ("Under", "under", "moins")):
                    add(f"team_{stat}", nm, float(pt), v[k], gb[side][pt][k], f"{unit} équipe",
                        f"{team} : {word} de {pt} {unit}", stat, side)
    return out


def market_grid(home, away, sport_key, referee=None, on=None, model=None):
    """Point d'entrée « tout-en-un » : {pred, grid, lines} d'un match (noms The Odds API ou football-data), ou None si la
    ligue n'a pas assez de matchs en cache ou si une équipe est inconnue. `grid` : voir grid() ; `lines` : voir lines()."""
    from .sources import footballdata as fd
    div = fd.SPORT_TO_DIV.get(sport_key)
    model = model or (get(div, on) if div else None)
    pred = predict(model, home, away, on, referee) if model else None
    if not pred:
        return None
    return {"pred": pred, "grid": grid(pred), "lines": lines(pred, pred["home"], pred["away"])}


# ---------- explications ----------

def _pct(x):
    return f"{(x - 1) * 100:+.0f}%"


def _team_phrase(pred, stat, unit):
    """Deux moteurs d'une statistique d'équipe : ce que produit le domicile / concède l'extérieur, et l'inverse, le plus
    marqué d'abord (écart à la moyenne de la ligue)."""
    t = pred["tm"].get(stat)
    if not t:
        return ""
    h, a = pred["home"], pred["away"]
    home = (f"{h} {t['hf']:.1f} {unit}/match à domicile (ligue {t['lh']:.1f}), {a} en concède {t['ac']:.1f}",
            abs(math.log(max(t["hf"] / t["lh"], 1e-3))) + abs(math.log(max(t["ac"] / t["lh"], 1e-3))))
    away = (f"{a} {t['af']:.1f} {unit}/match dehors (ligue {t['la']:.1f}), {h} en concède {t['hc']:.1f}",
            abs(math.log(max(t["af"] / t["la"], 1e-3))) + abs(math.log(max(t["hc"] / t["la"], 1e-3))))
    return " ; ".join(x[0] for x in sorted((home, away), key=lambda x: -x[1]))


def shorten(text, n=170):
    """Coupe un « pourquoi » (moteurs séparés par « ; ») au dernier moteur entier qui tient dans n caractères (au moins un)."""
    parts = text.split(" ; ")
    out = parts[0]
    for x in parts[1:]:
        if len(out) + 3 + len(x) > n:
            break
        out += " ; " + x
    return out


def explain(pred):
    """Pourquoi du modèle, par famille : {goals, corners, cards, shots, sot, fouls} -> phrase courte (2 à 3 moteurs)."""
    h, a = pred["home"], pred["away"]
    out = {}
    cand = []
    t = pred["tm"].get("goals")
    if t:
        for ratio, team, att in ((t["A"][0], h, True), (t["D"][1], a, False), (t["A"][1], a, True), (t["D"][0], h, False)):
            txt = (f"attaque de {team} {_pct(ratio)} vs la ligue" if att else
                   f"défense de {team} {'perméable' if ratio > 1 else 'solide'} ({_pct(ratio)} de buts encaissés)")
            cand.append((abs(math.log(max(ratio, 1e-3))), txt))
    top = sorted(cand, reverse=True)[:1]
    notes = sorted(pred["notes"], key=lambda n: -max(abs(n[2]), abs(n[3])))[:2]
    out["goals"] = " ; ".join(([x[1] for x in top] + [n[1] for n in notes])[:3])
    for stat, unit in (("corners", "corners"), ("shots", "tirs"), ("sot", "tirs cadrés")):
        if stat in pred["tm"]:
            out[stat] = _team_phrase(pred, stat, unit)
    for stat, unit in (("cards", "cartons"), ("yellow", "cartons jaunes"), ("fouls", "fautes")):
        tt = pred["tm"].get(stat)
        if not tt:
            continue
        s = f"{h} {tt['hf']:.1f} {unit}/match à domicile (ligue {tt['lh']:.1f}), {a} {tt['af']:.1f} dehors (ligue {tt['la']:.1f})"
        r = pred["info"].get("referee")
        if stat in ("cards", "yellow") and r:
            s += f" ; arbitre {r['name']} {r['cards']:.1f} cartons/match (ligue {r['lg']:.1f})"
        rfo = pred["info"].get("referee_fouls")
        if stat == "fouls" and rfo:
            s += f" ; arbitre {rfo['name']} {rfo['avg']:.1f} fautes/match (ligue {rfo['lg']:.1f})"
        out[stat] = s
    return out


# ---------- /analyse ----------

def top_lines(pred, home, away, n=5, per_family=2, lo=0.55, hi=0.90):
    """Les `n` meilleures lignes du modèle : proba entre lo et hi, classées par avance sur un match moyen de la ligue
    (proba - référence) x (0,5 + 0,5 x qualité des données de la statistique), au plus `per_family` par famille (buts, corners, cartons... équipes comprises)."""
    cand = [dict(l, lift=l["p"] - l["base"], score=(l["p"] - l["base"]) * (0.5 + 0.5 * l["q"]))
            for l in lines(pred, home, away) if lo <= l["p"] <= hi and l["market"] != "h2h" and l["p"] - l["base"] >= 0.03]
    cand.sort(key=lambda l: -l["score"])
    out, seen = [], {}
    for l in cand:
        grp = l["family"].replace(" équipe", "")                 # buts et buts d'une équipe comptent ensemble
        if seen.get(grp, 0) >= per_family:
            continue
        seen[grp] = seen.get(grp, 0) + 1
        out.append(l)
        if len(out) == n:
            break
    return out


def _dm(s):
    return f"{s[8:10]}/{s[5:7]}/{s[2:4]}"


def analysis_text(home, away, sport_key, referee=None, on=None, model=None):
    """Texte de /analyse : score probable, buts attendus, 1N2, forme, face-à-face, repos, classement et enjeu, arbitre,
    corners / cartons / tirs et les 5 meilleures lignes (cote juste, confiance). Texte brut en français. Ne lève jamais.
    `model` : modèle déjà construit (tests) ; sinon cache du jour de la ligue (get)."""
    try:
        return _analysis_text(home, away, sport_key, referee, on, model)
    except Exception as e:                                       # noqa: BLE001
        return f"🔬 {home} - {away}\nAnalyse indisponible ({type(e).__name__})."


def _analysis_text(home, away, sport_key, referee, on, model):
    from . import catalogue
    from .sources import footballdata as fd
    div = fd.SPORT_TO_DIV.get(sport_key)
    league = catalogue.name(sport_key) if sport_key else ""
    head = f"🔬 {home} - {away}" + (f" [{league}]" if league else "")
    model = model or (get(div, on) if div else None)
    pred = predict(model, home, away, on, referee) if model else None
    if not pred:
        return (head + "\nPas assez de données pour une analyse détaillée de ce match (équipe inconnue, ligue absente de "
                "football-data ou historique trop court).")
    h, a = pred["home"], pred["away"]
    g = grid(pred)
    info = pred["info"]
    lh, la = g["xg"]
    q = pred["q"]
    L = [head, f"Modèle de force pondéré dans le temps (demi-vie {model.get('hl', HALF_LIFE)} j), {pred['model_n']} matchs de la "
               f"ligue, dernier le {_dm(pred['last'])} ; qualité des données {q * 100:.0f}%"
         + (f" (cache vieux de {pred['stale']} j)" if pred["stale"] else "") + "."]
    if any(pred["promo"]):
        L.append("Équipe(s) promue(s) : " + ", ".join(t for t, p in zip((h, a), pred["promo"]) if p) + " (a priori plus prudent).")
    sc = ", ".join(f"{s} ({p * 100:.0f}%)" for s, p in g["scores"][:3])
    L.append(f"⚽ Buts attendus : {h} {lh:.2f} - {la:.2f} {a} (total {lh + la:.2f}). Scores les plus probables : {sc}.")
    x = g["1x2"]
    L.append("1N2 : " + " · ".join(f"{n} {x[k] * 100:.0f}% (cote juste {1 / x[k]:.2f})" for n, k in ((h, "H"), ("Nul", "D"), (a, "A"))))
    t25, bt = g["totals"]["2.5"], g["btts"]
    L.append(f"Plus de 2.5 buts {t25['over'] * 100:.0f}% (cote juste {1 / t25['over']:.2f}) · les 2 marquent {bt['yes'] * 100:.0f}% "
             f"(cote juste {1 / bt['yes']:.2f}) · double chance {h} ou Nul {g['dc']['1X'] * 100:.0f}%, Nul ou {a} {g['dc']['X2'] * 100:.0f}%.")
    if info.get("form"):
        L.append(f"Forme (5 derniers, V/N/D) : {h} {info['form']['home']} · {a} {info['form']['away']}")
    ftbl = {"home": h, "away": a}
    if info.get("h2h"):
        ms = []
        for d, home_is_h, gh, ga in info["h2h"][:4]:
            ms.append(f"{_dm(d)} {h if home_is_h else a} {gh}-{ga} {a if home_is_h else h}")
        L.append("Face-à-face récent : " + " ; ".join(ms))
    rest = info.get("rest") or {}
    if rest and min(rest.values()) <= 21:
        L.append("Repos (matchs de championnat seulement) : " + " · ".join(f"{ftbl[k]} {v} j" for k, v in rest.items() if v <= 21))
    for key in ("home", "away"):
        sk = (info.get("stakes") or {}).get(key)
        if sk:
            L.append("Classement : " + (sk["text"] or f"{ftbl[key]} {sk['pos']}e ({sk['pts']} pts en {sk['played']} matchs)"))
    if pred["notes"]:
        L.append("Facteurs de contexte appliqués (bornés à ±" + f"{BOUND['total'] * 100:.0f}% au total) : "
                 + " ; ".join(f"{n[1]} : {max(n[2], n[3], key=abs) * 100:+.1f}% de buts attendus" for n in pred["notes"]))
    r = info.get("referee")
    if r:
        L.append(f"Arbitre {r['name']} : {r['cards']:.1f} cartons/match sur {r['n']} matchs (ligue {r['lg']:.1f}), "
                 f"effet retenu {_pct(r['factor'])} sur les cartons.")
        rfo = info.get("referee_fouls")
        if rfo:
            L[-1] += f" Fautes : {rfo['avg']:.1f}/match (ligue {rfo['lg']:.1f}), effet retenu {_pct(rfo['factor'])}."
    elif referee:
        L.append(f"Arbitre {referee} : trop peu de matchs connus pour en tenir compte.")

    def mid(stat, unit, lab):
        if stat not in g:
            return
        d = g[stat]
        pts = sorted(d["total"], key=lambda p: abs(float(p) - d["mean"]))[:2]
        txt = " · ".join(f"plus de {p} : {d['total'][p]['over'] * 100:.0f}% (cote juste {1 / max(d['total'][p]['over'], 1e-6):.2f})"
                         for p in sorted(pts, key=float))
        L.append(f"{lab} : {d['mean']:.1f} attendus ({h} {d['h']:.1f} · {a} {d['a']:.1f}) ; {txt}.")

    mid("corners", "corners", "Corners")
    mid("cards", "cartons", "Cartons (jaunes + rouges)")
    mid("yellow", "cartons jaunes", "Cartons jaunes seuls (le pari de 1xBet)")
    mid("shots", "tirs", "Tirs")
    mid("sot", "tirs cadrés", "Tirs cadrés")
    mid("fouls", "fautes", "Fautes")
    best = top_lines(pred, h, a)
    if best:
        L.append("Meilleures lignes du modèle (proba, cote juste, avance sur un match moyen de la ligue, confiance = qualité des données) :")
        for i, l in enumerate(best, 1):
            L.append(f"{i}. {l['label']} : {l['p'] * 100:.0f}% · cote juste {1 / l['p']:.2f} · {l['lift'] * 100:+.0f} pts "
                     f"(référence {l['base'] * 100:.0f}%) · confiance {l['q'] * 100:.0f}%")
        L.append(f"Matchs pondérés par l'ancienneté : {pred['neff'][0]:.0f} ({h}) et {pred['neff'][1]:.0f} ({a}). "
                 "Compare toujours à la cote réelle : joue seulement si la cote dépasse la cote juste.")
    return "\n".join(L)


# ---------- backtest ----------

def market_probs(m):
    """Probabilités sans marge de la cote de clôture : ([H, D, A] ou None, P(plus de 2,5) ou None)."""
    from .sources import footballdata as fd
    o = m.get("odds") or {}
    p3 = po = None
    for book in ("avg", "ps", "b365"):
        x = ((o.get("1x2") or {}).get("close") or {}).get(book)
        if x and p3 is None:
            p3 = fd.no_vig(x)
        y = ((o.get("ou25") or {}).get("close") or {}).get(book)
        if y and po is None:
            v = fd.no_vig(y)
            po = v[0] if v else None
    return p3, po


def _ll(p, k):
    return -math.log(max(p[k], 1e-9))


def _br(p, k):
    return sum((p[i] - (1.0 if i == k else 0.0)) ** 2 for i in range(len(p)))


def walk(matches, test_from, test_to=None, hl=HALF_LIFE, step_days=3, ctx=True, stats=("goals",), div=None):
    """Évaluation pas à pas (walk-forward) : avant chaque lot de matchs (tous les `step_days` jours) le modèle est
    réajusté sur le passé seulement, puis prédit le lot. Produit (match, p1x2 [H, D, A], P(plus de 2,5)) pour chaque
    match de [test_from, test_to[ ; (match, None, None) quand le modèle ne peut pas le prédire. Générateur : l'appelant
    peut s'arrêter (budget de temps) entre deux matchs."""
    tf = str(test_from)
    te = str(test_to) if test_to else "9999"
    batch_start, model = None, None
    for m in sorted((m for m in matches if tf <= m["date"] < te), key=lambda m: m["date"]):
        d = date.fromisoformat(m["date"])
        if batch_start is None or d >= batch_start + timedelta(days=step_days):
            batch_start = d
            model = build(matches, d, hl, stats, div)
        pred = predict(model, m["home"], m["away"], d, m.get("referee"), ctx) if model else None
        if not pred:
            yield m, None, None
            continue
        gr = grid(pred)
        yield m, [gr["1x2"]["H"], gr["1x2"]["D"], gr["1x2"]["A"]], gr["totals"]["2.5"]["over"]


def backtest(matches, test_from, test_to=None, hl=HALF_LIFE, step_days=3, ctx=True, stats=("goals",), div=None):
    """Évaluation pas à pas (walk-forward, voir walk). Compare au modèle naïf (fréquences de la ligue avant
    `test_from`) et aux cotes de clôture sans marge, sur les mêmes matchs. -> dict {n, n_mkt, model, market, naive, ...} :
    log-loss et Brier du 1N2 (somme des 3 carrés) et du plus de 2,5 buts, plus la calibration du plus de 2,5 buts."""
    tf = str(test_from)
    past = [m for m in matches if m["date"] < tf]
    if not past or not any(tf <= m["date"] < (str(test_to) if test_to else "9999") for m in matches):
        return None
    cnt = [sum(1 for m in past if m["ftr"] == k) for k in "HDA"]
    naive3 = [c / len(past) for c in cnt]
    naive_o = sum(1 for m in past if m["hg"] + m["ag"] > 2.5) / len(past)
    acc = {k: {"ll3": 0.0, "br3": 0.0, "llo": 0.0, "bro": 0.0, "n3": 0, "no": 0} for k in ("model", "market", "naive", "model_m")}
    calib = [[0.0, 0.0, 0] for _ in range(5)]
    n = skipped = 0
    for m, p3, po in walk(matches, test_from, test_to, hl, step_days, ctx, stats, div):
        if p3 is None:
            skipped += 1
            continue
        n += 1
        k3 = "HDA".index(m["ftr"])
        over = 1 if m["hg"] + m["ag"] > 2.5 else 0
        mk3, mko = market_probs(m)
        rows = [("model", p3, po), ("naive", naive3, naive_o)]
        if mk3 and mko is not None:
            rows += [("market", mk3, mko), ("model_m", p3, po)]
        for key, a3, ao in rows:
            r = acc[key]
            r["ll3"] += _ll(a3, k3)
            r["br3"] += _br(a3, k3)
            r["llo"] += -math.log(max(ao if over else 1 - ao, 1e-9))
            r["bro"] += (ao - over) ** 2
            r["n3"] += 1
        b = min(4, int(po * 5))
        calib[b][0] += po
        calib[b][1] += over
        calib[b][2] += 1
    if not n:
        return None

    def fin(r):
        k = r["n3"] or 1
        return {"ll_1x2": r["ll3"] / k, "br_1x2": r["br3"] / k, "ll_o25": r["llo"] / k, "br_o25": r["bro"] / k, "n": r["n3"]}

    return {"n": n, "skipped": skipped, "n_mkt": acc["market"]["n3"], "hl": hl, "ctx": ctx, "stats": list(stats),
            "model": fin(acc["model"]), "naive": fin(acc["naive"]), "market": fin(acc["market"]),
            "model_on_market_matches": fin(acc["model_m"]),
            "calib_o25": [(c[0] / c[2], c[1] / c[2], c[2]) for c in calib if c[2]]}


def format_backtest(res):
    """Résumé lisible d'un backtest."""
    if not res:
        return "backtest : pas assez de données"
    f = lambda d: f"1N2 log-loss {d['ll_1x2']:.4f} Brier {d['br_1x2']:.4f} | +2,5 buts log-loss {d['ll_o25']:.4f} Brier {d['br_o25']:.4f}"   # noqa: E731
    return "\n".join([f"backtest sur {res['n']} matchs (cotes de clôture sur {res['n_mkt']}), demi-vie {res['hl']} j, "
                      f"contexte {'oui' if res['ctx'] else 'non'}",
                      "  modèle   : " + f(res["model"]), "  naïf     : " + f(res["naive"]),
                      "  marché   : " + f(res["market"]) + "  (sans marge, clôture)",
                      "  modèle sur les mêmes matchs que le marché : " + f(res["model_on_market_matches"])])


if __name__ == "__main__":                       # python -m kelly.strength E0 : backtest sur la saison en cours (CSV en cache)
    d = sys.argv[1] if len(sys.argv) > 1 else "E0"
    ms = load_matches(d)
    print(format_backtest(backtest(ms, season_start(today(), d), div=d)))
