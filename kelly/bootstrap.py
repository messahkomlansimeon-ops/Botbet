"""Amorçage de l'apprentissage par l'historique football-data : entraîner le modèle CLV dès le premier jour.

Chaque match passé (1N2 et plus/moins 2,5 buts) donne des paris candidats : cote prise = meilleure cote
d'OUVERTURE (colonnes Max, sinon Pinnacle PS, sinon Avg), proba de consensus = Avg (sinon PS) sans marge, et
CLV = log(cote d'ouverture / cote de clôture) du même book, comme pour les paris suivis. Variables calculables
sans fuite d'information (avant le match) : valeur contre le consensus, désaccord Avg/Pinnacle, cote, et les
chiffres d'avant-match issus des moyennes de buts glissantes (écart de buts attendus pour le 1N2, écart
modèle-ligne et proba Poisson pour le plus/moins). Le mouvement de cote (steam) est inconnu : 0.

Fichier data/bootstrap.json, petit : au plus BOOT_PER_DAY candidats par jour (les meilleurs scores, comme les
paris fantômes suivis), éclaircis à BOOT_MAX_ROWS lignes. Format : {built, seasons, matches, cols, rows:
[[date, marché (0=1N2, 1=buts), clv, *learn.BOOT_COLS]]}. Reconstruit au plus une fois par semaine.
"""
import math
from collections import deque
from datetime import datetime

from . import config as C, learn, prematch, store
from .sources import footballdata as fd, state

FILE = "bootstrap.json"
FORM_N, MIN_FORM = 10, 5            # matchs de forme (buts pour / contre) et minimum pour croire une équipe
BOOKS = ("max", "ps", "avg")        # cote prise : la meilleure disponible à l'ouverture ET à la clôture
LINE = 2.5                          # ligne de buts des cotes football-data
MARKETS = (("1x2", "h2h", ("home", "draw", "away")), ("ou25", "totals", ("over", "under")))


def seasons(n=None, today=None):
    """Saison en cours + `n` saisons passées (codes football-data « 2627 »)."""
    out = [fd.current_season(today)]
    for _ in range(C.BOOT_SEASONS if n is None else n):
        out.append(fd.previous_season(out[-1]))
    return out


def _lambdas(form, home, away):
    """Buts attendus (domicile, extérieur) d'après la forme glissante, ou None sous MIN_FORM matchs."""
    h, a = form.get(home), form.get(away)
    if not h or not a or len(h) < MIN_FORM or len(a) < MIN_FORM:
        return None
    avg = lambda d, k: sum(x[k] for x in d) / len(d)             # noqa: E731
    return (avg(h, 0) + avg(a, 1)) / 2, (avg(a, 0) + avg(h, 1)) / 2


def match_rows(m, lam):
    """Lignes candidates d'un match (voir l'en-tête) ; `lam` : buts attendus ou None."""
    out = []
    for mk, market, whos in MARKETS:
        o = m.get("odds", {}).get(mk) or {}
        op, cl = o.get("open", {}), o.get("close", {})
        book = next((b for b in BOOKS if b in op and b in cl), None)
        cons = "avg" if "avg" in op else ("ps" if "ps" in op else None)
        if not book or not cons or cons == book:
            continue                                            # pas de consensus distinct de la cote prise
        p = fd.no_vig(op[cons])
        alt = fd.no_vig(op["ps"]) if cons == "avg" and "ps" in op else None
        if not p:
            continue
        w = C.MARKET_WEIGHT[market]
        for i, who in enumerate(whos):
            taken, close = op[book][i], cl[book][i]
            if not C.ODDS_RANGE[0] <= taken <= C.ODDS_RANGE[1]:
                continue
            edge = p[i] * taken - 1
            spread = abs(p[i] - alt[i]) / p[i] if alt else 0.0
            xg = gap = pgap = bump = 0.0
            if lam and market == "h2h":
                xg = prematch.signed_diff(who, *lam)
            elif lam:
                name = "Over" if who == "over" else "Under"
                pgap = prematch.p_totals(*lam, LINE, name) - p[i]
                gap = prematch.side(name) * (sum(lam) - LINE)
                bump = C.MODEL_WEIGHT["goals"] * pgap * taken       # proba mélangée, comme prematch.apply
            edge += bump
            clv = math.log(taken / close)
            if edge > 0.5 or abs(clv) > 0.7:
                continue                                        # cote aberrante (erreur de saisie)
            heur = (edge + 0.5 * spread) * w + (w - 1) * 0.05
            out.append([m["date"], learn.BOOT_MARKETS.index(market), round(clv, 4), round(edge, 4),
                        round(spread, 4), round(math.log(taken), 4), round(heur, 4), round(xg, 3), round(gap, 3),
                        round(pgap, 4)])
    return out


def build(matches, per_day=None, cap=None):
    """Lignes d'amorçage à partir des matchs normalisés de football-data (voir footballdata.parse_csv)."""
    per_day, cap = per_day or C.BOOT_PER_DAY, cap or C.BOOT_MAX_ROWS
    form, by_day = {}, {}
    for m in sorted(matches, key=lambda m: (m["date"], m["home"])):
        for r in match_rows(m, _lambdas(form, m["home"], m["away"])):
            by_day.setdefault(r[0], []).append(r)
        form.setdefault(m["home"], deque(maxlen=FORM_N)).append((m["hg"], m["ag"]))
        form.setdefault(m["away"], deque(maxlen=FORM_N)).append((m["ag"], m["hg"]))
    hi = learn.BOOT_COLS.index("heuristic") + 3
    rows = [r for d in sorted(by_day) for r in sorted(by_day[d], key=lambda r: -r[hi])[:per_day]]
    if len(rows) > cap:                                         # éclaircit régulièrement, ordre des dates conservé
        step = len(rows) / cap
        rows = [rows[int(i * step)] for i in range(cap)]
    return {"built": state.now().isoformat(timespec="minutes"), "seasons": sorted({m["season"] for m in matches if m.get("season")}),
            "matches": len(matches), "cols": ["date", "market", "clv", *learn.BOOT_COLS], "rows": rows}


def _age_days(obj):
    try:
        return (state.now() - datetime.fromisoformat(obj["built"])).total_seconds() / 86400
    except (KeyError, TypeError, ValueError):
        return 1e9


def ensure(sport_keys=None, force=False):
    """data/bootstrap.json à jour : reconstruit s'il manque, a plus de BOOT_REFRESH_DAYS jours ou si la saison a
    changé ; télécharge les saisons passées au besoin (cache disque, jamais deux fois). Ne lève jamais ; un
    échec ou un résultat vide laisse l'ancien fichier. Renvoie le contenu (ou {})."""
    from . import sources
    try:
        cur = store.load(FILE, {})
        cur = cur if isinstance(cur, dict) else {}
        if (not force and cur.get("rows") and _age_days(cur) < C.BOOT_REFRESH_DAYS
                and cur.get("current") == seasons()[0]):
            return cur
        divs = fd.divs_for(sources.soccer_leagues(sport_keys))
        if not divs:
            return cur
        obj = build(fd.load_history(divs, seasons()))
        if not obj["rows"]:
            return cur
        obj["current"] = seasons()[0]
        store.save(FILE, obj)
        return obj
    except Exception:                                           # noqa: BLE001 - l'amorçage est facultatif
        return {}
