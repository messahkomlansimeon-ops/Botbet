"""Cote finale gratuite des paris suivis que le relevé de clôture n'a pas couverts : les paris fantômes (forfait gratuit : seuls les
paris envoyés sont relevés) et les paris envoyés dont le relevé a manqué. Aucun crédit The Odds API.

Sources, dans cet ordre :
- football-data (CSV déjà en cache) : cotes de clôture Pinnacle, sinon moyenne du marché ; 1N2 et plus/moins 2,5 buts ;
- BetExplorer (pages de résultats déjà lues le matin) : cotes de clôture 1N2, petits championnats et Afrique compris.

Mesure : mouvement du marché en probabilité sans marge, clv = log(proba de clôture / proba des books à la prise). C'est la cote
finale « log(cote prise / cote de clôture) » des paris relevés, si l'écart entre cote typique et cote juste ne bouge pas entre les
deux moments. Les lignes ainsi clôturées sont marquées (closed_at = « free:<source> », close_src) et pèsent C.FREE_CLOSE_WEIGHT dans
l'apprentissage (learn.train_clv) : un pari vraiment relevé reste la meilleure mesure.
"""
import math
from datetime import datetime, timedelta

from . import config as C, stats
from .sources import betexplorer, footballdata as fd

GOAL_LINE = 2.5                       # seule ligne de buts des cotes de clôture de football-data


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _index(r):
    """Position de l'issue du pari dans les cotes de clôture : (marché football-data, index) ou None si le marché n'y figure pas."""
    if r["market"] == "h2h":
        return "1x2", 0 if r["name"] == r["home"] else (1 if r["name"] == "Draw" else 2)
    if r["market"] == "totals" and r.get("point") == GOAL_LINE and r["name"] in ("Over", "Under"):
        return "ou25", 0 if r["name"] == "Over" else 1
    return None


def _fd_close(r, fdata):
    """Proba de clôture sans marge d'après football-data (Pinnacle, sinon moyenne), ou None."""
    idx = _index(r)
    m = fdata.match(r) if idx and fd.SPORT_TO_DIV.get(r["sport_key"]) else None
    close = ((m or {}).get("odds", {}).get(idx[0]) or {}).get("close", {}) if idx else {}
    for book in ("ps", "avg"):
        p = fd.no_vig(close[book]) if close.get(book) else None
        if p:
            return p[idx[1]], f"football-data/{book}"
    return None


def _bx_close(r, matches):
    """Proba de clôture 1N2 sans marge d'après BetExplorer, ou None."""
    if r["market"] != "h2h":
        return None
    day = _ts(r["commence"]).date()
    for m in matches:
        try:
            gap = abs((datetime.fromisoformat(m["d"]).date() - day).days)
        except (KeyError, ValueError):
            continue
        if gap <= 1 and stats.team_sim(m["home"], r["home"]) >= 0.8 and stats.team_sim(m["away"], r["away"]) >= 0.8:
            p = fd.no_vig([m.get("o1"), m.get("ox"), m.get("o2")])
            if p:
                return p[_index(r)[1]], "betexplorer"
    return None


def fill(rows, now, fdata=None, bx=None):
    """Complète la cote finale des lignes de `rows` (tracked.json) restées sans clôture, matchs terminés depuis 3 h à
    C.FREE_CLOSE_DAYS jours. `fdata` : settle.FootballData (CSV en cache), `bx` : profils BetExplorer (injectables pour les tests).
    -> nombre de lignes complétées."""
    if fdata is None:
        from .settle import FootballData
        fdata = FootballData()
    leagues = (bx if bx is not None else betexplorer.profiles()).get("leagues") or {}
    n = 0
    for r in rows:
        if r.get("clv") is not None or r.get("closed_at") not in (None, "missed") or r.get("group") != "soccer" or not _index(r):
            continue
        ko = _ts(r["commence"])
        if not now - timedelta(days=C.FREE_CLOSE_DAYS) < ko < now - timedelta(hours=3):
            continue
        p_take = r.get("prob_book") or (r["prob"] if r["market"] == "h2h" else None)    # anciennes lignes : 1N2 seulement (proba non mélangée)
        if not p_take:
            continue
        got = _fd_close(r, fdata) or _bx_close(r, (leagues.get(r["sport_key"]) or {}).get("matches") or [])
        if not got:
            continue
        p_close, src = got
        r.update(close_prob=round(p_close, 4), close_src=src, closed_at=f"free:{src.split('/')[0]}",
                 clv=round(math.log(p_close / p_take), 4), w=C.FREE_CLOSE_WEIGHT)
        n += 1
    return n
