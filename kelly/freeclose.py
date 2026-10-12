"""Cote finale gratuite des paris suivis que le relevé de clôture n'a pas couverts : les paris fantômes (forfait gratuit : seuls les
paris envoyés sont relevés) et les paris envoyés dont le relevé a manqué. Aucun crédit The Odds API.

Sources, dans cet ordre :
- football-data (CSV déjà en cache) : cotes de clôture Pinnacle, sinon moyenne du marché ; 1N2 et plus/moins 2,5 buts ;
- BetExplorer (pages de résultats déjà lues le matin) : cotes de clôture 1N2, petits championnats et Afrique compris.

Mesure : mouvement du marché en probabilité sans marge, clv = log(proba de clôture / proba des books à la prise). La marge est retirée par
la MÊME méthode des deux côtés, celle de Shin (odds.shin_probs) : la proba à la prise (prob_book, consensus des books) l'est, et la cote de
clôture aussi (avant, football-data / BetExplorer étaient dé-margés proportionnellement : un biais de méthode, pas un mouvement du marché,
qui pesait surtout sur les outsiders et les petits favoris). C'est la cote
finale « log(cote prise / cote de clôture) » des paris relevés, si l'écart entre cote typique et cote juste ne bouge pas entre les
deux moments. Les lignes ainsi clôturées sont marquées (closed_at = « free:<source> », close_src) et pèsent C.FREE_CLOSE_WEIGHT dans
l'apprentissage (learn.train_clv) : un pari vraiment relevé reste la meilleure mesure.
"""
import math
from datetime import datetime, timedelta

from . import config as C, sharpgap, stats
from .sources import betexplorer, footballdata as fd

GOAL_LINE = 2.5                       # seule ligne de buts des cotes de clôture de football-data
BOOKS_FOR = {"pinnacle": ("ps",), "consensus": ("avg",)}      # source de la proba à la prise -> books de clôture football-data comparables


def devig(prices):
    """Probas sans marge d'un marché de cotes décimales, méthode de Shin comme prob_book (odds.shin_probs) ; None si une cote manque ou est
    invalide (≤ 1), comme fd.no_vig. Repli : normalisation proportionnelle si Shin échoue."""
    if not prices or any(not isinstance(o, (int, float)) or isinstance(o, bool) or o != o or o <= 1 for o in prices):
        return None
    from . import odds                    # import tardif : freeclose est chargé par app avant que tout odds soit utile
    try:
        return odds.shin_probs(list(prices))
    except Exception:                    # noqa: BLE001 - ne jamais arrêter le remplissage des suivantes
        return fd.no_vig(list(prices))


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _index(r):
    """Position de l'issue du pari dans les cotes de clôture : (marché football-data, index) ou None si le marché n'y figure pas."""
    if r["market"] == "h2h":
        return "1x2", 0 if r["name"] == r["home"] else (1 if r["name"] == "Draw" else 2)
    if r["market"] == "totals" and r.get("point") == GOAL_LINE and r["name"] in ("Over", "Under"):
        return "ou25", 0 if r["name"] == "Over" else 1
    return None


def comparable(r, src):
    """La source de clôture `src` (« football-data/ps », « football-data/avg », « betexplorer ») est-elle de la même famille que la proba prise (r["prob_src"]) ?"""
    take = r.get("prob_src")
    if take == "pinnacle":
        return src == "football-data/ps"
    if take == "consensus":
        return src in ("football-data/avg", "betexplorer")
    return False


def _fd_close(r, fdata):
    """Proba de clôture sans marge d'après football-data (le book comparable à la proba prise : Pinnacle ou la moyenne), ou None."""
    idx = _index(r)
    m = fdata.match(r) if idx and fd.SPORT_TO_DIV.get(r["sport_key"]) else None
    close = ((m or {}).get("odds", {}).get(idx[0]) or {}).get("close", {}) if idx else {}
    for book in BOOKS_FOR.get(r.get("prob_src"), ()):
        p = devig(close[book]) if close.get(book) else None
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
        except (KeyError, TypeError, ValueError, AttributeError):     # date absente/invalide : ligne sautée, jamais d'arrêt
            continue
        if gap <= 1 and stats.team_sim(m["home"], r["home"]) >= 0.8 and stats.team_sim(m["away"], r["away"]) >= 0.8:
            p = devig([m.get("o1"), m.get("ox"), m.get("o2")])
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
        try:
            got = _fd_close(r, fdata) or _bx_close(r, (leagues.get(r["sport_key"]) or {}).get("matches") or [])
        except Exception:                 # une ligne bancale ne doit jamais arrêter le remplissage des suivantes
            continue
        if not got:
            continue
        p_close, src = got
        if not comparable(r, src):          # Pinnacle contre moyenne (ou source inconnue) : un marché immobile n'aurait pas un CLV de 0
            continue
        r.update(close_prob=round(p_close, 4), close_src=src, closed_at=f"free:{src.split('/')[0]}",
                 clv=round(math.log(p_close / p_take), 4), w=C.FREE_CLOSE_WEIGHT)
        if r.get("sharpgap") and r.get("xodds") and src == "football-data/ps":       # pari « 1xBet en retard » : clôture de Pinnacle, gratuite
            r["gap_edge_close"], r["gap_close_src"] = sharpgap.close_edge(r["xodds"], p_close), "pinnacle"
        n += 1
    return n
