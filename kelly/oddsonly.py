"""Voie « cotes » : des probabilités déduites des seules cotes 1xBet, sans aucune statistique d'équipe ni modèle de force. FANTÔME : calculée et journalisée (oddsonly_log.json), jamais choisie ;
aucune jambe de combiné, aucune mise, aucun code coupon, aucun apprentissage, aucune relecture 1xBet en plus. Règles pré-déclarées dans oddsonly_ledger.json (AVANT ce code).

Mécanismes (fonctions pures, partie haute du module) :
  1a  1N2 <-> double chance (identité exacte, deux sens)
  1b  totaux d'équipe -> total du match et les 2 marquent (deux Poisson indépendantes)
  1c  lignes successives d'un même marché : une loi binomiale négative ajustée SANS la ligne testée (leave-one-out)
  2   retard sur les autres books (The Odds API) : buts (Pinnacle / sharp) et corners (consensus d'au moins 2 books)
  3   mouvement (observation seulement, jamais une valeur)
Partie basse : collecte (observe), journal, durée de vie des incohérences."""
import math
import sys

from . import config as C, ensemble, lanes, strength

EDGE = C.ODDSONLY_EDGE
MAX_EDGE = C.ODDSONLY_MAX_EDGE
MOVE_MIN = 0.03                                   # mécanisme 3 : variation (log) de la cote 1xBet journalisée au moins
MIN_PAIRS = 4                                     # 1c : paires en .5 minimales pour tester une ligne (3 pour ajuster)
MIN_TEAM_PAIRS = 2                                # 1b : paires en .5 minimales par équipe
MULTS = (0.5, 1.0, 2.0)                           # 1c : dispersion r essayée = multiple x la dispersion de repli de la famille
FALLBACK_R = {"goals": 100.0, "team_goals": 100.0, "corners": 60.0, "cards": 25.0, "yellow": 25.0, "shots": 40.0, "sot": 25.0, "fouls": 100.0}     # FIGÉES, non apprises (ledger)
MECHS = ("1a", "1b", "1c", "2", "3")
GOAL_FAMILIES = frozenset(("goals", "team_goals", "btts", "dc", "h2h"))
LADDER_MARKETS_EXCLUDED = frozenset(("btts", "h2h", "double_chance"))


def mode():
    """« ghost » ou « off » : « on » est refusé dans ce lot (la voie ne joue rien, ledger)."""
    return "off" if C.STRATEGY_ODDSONLY == "off" else "ghost"


# ---------- démarginage ----------

def pair_prob(px, market, point):
    """Proba (sans marge, Shin) du « Over » / « Yes » d'une paire à deux issues d'une MÊME ligne en .5 ; None si une cote manque, si la ligne est entière ou si l'overround sort de la plage."""
    if point is None or abs(point * 2 - round(point * 2)) > 1e-9 or abs(point - round(point)) < 1e-9:
        return None
    a, b = px.get((market, "Over", point)), px.get((market, "Under", point))
    ps = ensemble.devig([a, b])
    return ps[0] if ps else None


# ---------- lois ----------

def _over_nb(mu, r, point):
    """P(X > point) d'une binomiale négative de moyenne `mu` et de taille `r` (variance mu + mu^2 / r) ; r = None : Poisson."""
    k = int(math.floor(point))
    pmf = strength.nb_pmf(mu, alpha=0.0 if r is None else 1.0 / r, cap=max(k + 2, int(mu + 10 * math.sqrt(mu + (0 if r is None else mu * mu / r))) + 6))
    return max(0.0, min(1.0, 1.0 - sum(pmf[: k + 1])))


def _sse(mu, r, pairs):
    kmax = int(math.floor(max(L for L, _ in pairs)))
    pmf = strength.nb_pmf(mu, alpha=0.0 if r is None else 1.0 / r, cap=kmax + 2)       # une seule loi pour toutes les lignes
    cum, tot = [], 0.0
    for v in pmf[: kmax + 1]:
        tot += v
        cum.append(tot)
    return sum((max(0.0, min(1.0, 1.0 - cum[int(math.floor(L))])) - p) ** 2 for L, p in pairs)


def fit_mu(pairs, r):
    """Moyenne mu qui minimise l'écart quadratique entre P(Over) de la loi (taille r ; None = Poisson) et les probabilités démarginées `pairs` [(ligne, p_over)] ; (mu, écart) ou (None, None)."""
    if not pairs:
        return None, None
    top = max(L for L, _ in pairs) * 2.5 + 3
    grid = [0.2 * (top / 0.2) ** (i / 39) for i in range(40)]
    best = min(grid, key=lambda m: _sse(m, r, pairs))
    lo, hi = best / (top / 0.2) ** (1 / 39), min(top, best * (top / 0.2) ** (1 / 39))
    for _ in range(14):                                          # raffinement par sections d'or
        a, b = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
        if _sse(a, r, pairs) < _sse(b, r, pairs):
            hi = b
        else:
            lo = a
    mu = (lo + hi) / 2
    return mu, _sse(mu, r, pairs)


def fit_nb(pairs, family):
    """(mu, r) retenus : au moins 3 paires -> r parmi MULTS x dispersion de repli de la famille, au moindre écart ; moins -> la dispersion de repli seule."""
    base = FALLBACK_R.get(family, 40.0)
    if len(pairs) < 3:
        mu, _ = fit_mu(pairs, base)
        return mu, base
    best = None
    for m in MULTS:
        mu, e = fit_mu(pairs, base * m)
        if mu is not None and (best is None or e < best[2]):
            best = (mu, base * m, e)
    return (best[0], best[1]) if best else (None, None)


# ---------- mécanismes ----------

def _row(mech, market, name, point, odds, p, **extra):
    return {"mech": mech, "market": market, "name": name, "point": point, "family": ensemble.family(market), "xodds": odds, "p_impl": round(p, 5),
            "edge": round(p * odds - 1, 5)} | extra


def mech_1a(px, home, away):
    """1N2 <-> double chance : toutes les sélections évaluables, avec la proba implicite déduite de l'AUTRE marché. []  si l'un des deux marchés n'est pas complet ou sort de l'overround."""
    names = {"1": home, "X": "Draw", "2": away}
    h = [px.get(("h2h", names[k], None)) for k in ("1", "X", "2")]
    dcn = {"1X": f"{home} or Draw", "12": f"{home} or {away}", "X2": f"Draw or {away}"}
    d = [px.get(("double_chance", dcn[k], None)) for k in ("1X", "12", "X2")]
    out = []
    ph = ensemble.devig(h)
    pd = ensemble.devig_mass(d)
    if ph and all(h):
        p1, px_, p2 = ph
        for k, p in (("1X", p1 + px_), ("12", p1 + p2), ("X2", px_ + p2)):
            o = px.get(("double_chance", dcn[k], None))
            if o:
                out.append(_row("1a", "double_chance", dcn[k], None, o, p, sens="1N2->DC"))
    if pd and all(d):
        a, b, c = pd
        for k, p in (("1", a + b - 1), ("X", a + c - 1), ("2", b + c - 1)):
            o = px.get(("h2h", names[k], None))
            if o and 0 < p < 1:
                out.append(_row("1a", "h2h", names[k], None, o, p, sens="DC->1N2"))
    return out


def _team_pairs(px, market):
    pts = sorted({k[2] for k in px if k[0] == market and k[1] == "Over" and k[2] is not None})
    return [(L, p) for L in pts for p in [pair_prob(px, market, L)] if p is not None]


def mech_1b(px):
    """Totaux d'équipe (buts) -> total du match et les 2 marquent, par deux Poisson indépendantes. [] sans au moins MIN_TEAM_PAIRS paires par équipe."""
    ph, pa = _team_pairs(px, "team_totals_home"), _team_pairs(px, "team_totals_away")
    if len(ph) < MIN_TEAM_PAIRS or len(pa) < MIN_TEAM_PAIRS:
        return []
    lh, _ = fit_mu(ph, None)
    la, _ = fit_mu(pa, None)
    if lh is None or la is None:
        return []
    out = []
    for (market, name, point), o in px.items():
        if market == "totals" and name in ("Over", "Under") and point is not None and abs(point - round(point)) > 1e-9 and o:
            over = _over_nb(lh + la, None, point)
            out.append(_row("1b", market, name, point, o, over if name == "Over" else 1 - over, lam=[round(lh, 3), round(la, 3)]))
        elif market == "btts" and name in ("Yes", "No") and o:
            yes = (1 - math.exp(-lh)) * (1 - math.exp(-la))
            out.append(_row("1b", market, name, None, o, yes if name == "Yes" else 1 - yes, lam=[round(lh, 3), round(la, 3)]))
    return out


def mech_1c(px):
    """Lignes successives d'un même marché : p implicite d'une ligne = loi binomiale négative ajustée SANS elle (leave-one-out). Familles de voie seulement."""
    out = []
    for market in sorted({k[0] for k in px} - LADDER_MARKETS_EXCLUDED):
        if not lanes.ticket_ok({"market": market}):
            continue
        fam = ensemble.family(market)
        pairs = _team_pairs(px, market)
        if len(pairs) < MIN_PAIRS:
            continue
        for L, p_obs in pairs:
            mu, r = fit_nb([q for q in pairs if q[0] != L], fam)
            if mu is None:
                continue
            over = _over_nb(mu, r, L)
            for name, p in (("Over", over), ("Under", 1 - over)):
                o = px.get((market, name, L))
                if o:
                    out.append(_row("1c", market, name, L, o, p, mu=round(mu, 3), r=round(r, 1), n_lines=len(pairs) - 1))
    return out


def mech_2(px, picks, now, history=None):
    """Retard sur les autres books : `picks` = paris réels du MÊME match (The Odds API). Buts : proba d'un book de référence (ensemble.sharp_of) ; corners : consensus d'au moins 2 books.
    Âge du relevé des books connu et <= ENSEMBLE_SHARP_MAX_AGE_MIN."""
    from . import sharpgap
    from .sources import xbet
    out = []
    for c in picks or ():
        fam = ensemble.family(c.get("market") or "")
        if not lanes.ticket_ok(c):
            continue
        if fam in GOAL_FAMILIES:
            sh = ensemble.sharp_of(c)
            p = sh[0] if sh else None
        elif fam == "corners" and c.get("market") == "alternate_totals_corners" and (c.get("n_books") or 0) >= 2 and c.get("prob_book") is not None:
            p = c["prob_book"]
        else:
            continue
        age = sharpgap.sharp_age(c, now, history)
        if p is None or age is None or age > C.ENSEMBLE_SHARP_MAX_AGE_MIN:
            continue
        k = xbet.resolve(px, c["market"], c["name"], c.get("point"))
        o = px.get(k) if k else None
        if o:
            out.append(_row("2", k[0], k[1], k[2], o, p, overlap_sharpgap=fam in GOAL_FAMILIES, age_min=round(age)))
    return out


def evaluate(px, home, away, picks=None, now=None, history=None):
    """Toutes les sélections évaluables d'un relevé (mécanismes 1a, 1b, 1c, 2), écart compris, quel que soit son niveau : [dict]. Aucune famille hors voies. Ne lève jamais."""
    out = []
    for fn, args in ((mech_1a, (px, home, away)), (mech_1b, (px,)), (mech_1c, (px,)), (mech_2, (px, picks, now, history))):
        try:
            out += fn(*args)
        except Exception as e:                  # noqa: BLE001 - un mécanisme en échec ne casse ni les autres ni le relevé
            print(f"[cotes] mécanisme {fn.__name__} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return [r for r in out if lanes.ticket_ok({"market": r["market"]})]


def status_of(edge):
    """« value » (EDGE <= écart <= MAX_EDGE), « suspect » (> MAX_EDGE), sinon None."""
    if edge is None:
        return None
    return "suspect" if edge > MAX_EDGE else "value" if edge >= EDGE else None


def movement(px_now, px_prev, ref_now=None, ref_prev=None):
    """Mécanisme 3 (observation) : {(marché, côté, ligne): {"mv": log(cote / cote précédente), "mv_ref": log(p / p précédente) ou None}} pour les sélections dont |mv| >= MOVE_MIN."""
    out = {}
    for k, o in px_now.items():
        old = px_prev.get(k)
        if not o or not old or o <= 1 or old <= 1:
            continue
        mv = math.log(o / old)
        if abs(mv) < MOVE_MIN:
            continue
        a, b = (ref_now or {}).get(k), (ref_prev or {}).get(k)
        out[k] = {"mv": round(mv, 4), "mv_ref": round(math.log(a / b), 4) if a and b else None}
    return out
