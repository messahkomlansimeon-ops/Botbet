"""Championnats hors The Odds API (C.XB_LEAGUES : Roumanie, Colombie, J2, Égypte...) ajoutés au relevé du matin grâce à 1xBet.

The Odds API ne liste que ~60 championnats ; 1xBet en propose des centaines. Pour ceux de C.XB_LEAGUES, Kelly a aussi des données
FotMob (classement, puis stats de match accumulées par fmmatch : buts, xG, tirs, corners, fautes...) : la liste des matchs du jour
Kelly vient de 1xBet (xbet.list_leagues, via le proxy, ~10 Ko par championnat), chaque match porte son id 1xBet ("xgid") pour que
ses cotes soient lues directement (xprice), et il passe ensuite par le même chemin que les autres : pronostics (fair.make, avec les
stats disponibles, sinon rien), cotes 1xBet réelles, règlement gratuit (API-Football). Un championnat sans données suffisantes ne
produit simplement aucun pronostic.
"""
import sys
from datetime import datetime, timezone

from . import catalogue, config as C, odds
from .sources import xbet


def events(games, key, day, after=None):
    """Matchs 1xBet d'un championnat -> événements au format du relevé gratuit (odds.scan_today), dans le jour Kelly `day`."""
    out = []
    for g in games:
        ko = datetime.fromtimestamp(g["ts"], tz=timezone.utc)
        if odds.in_day(ko, day, after):
            out.append({"id": f"xb{g['gid']}", "sport_key": key, "sport_title": catalogue.name(key), "home_team": g["home"],
                        "away_team": g["away"], "commence_time": ko.strftime("%Y-%m-%dT%H:%M:%SZ"), "xgid": g["gid"]})
    return out


def extend_scan(scan, day, after=None, lister=None):
    """Ajoute à `scan` (odds.scan_today) les matchs du jour Kelly des championnats C.XB_LEAGUES proposés par 1xBet. Ne lève jamais.
    `lister` : injectable pour les tests (xbet.list_leagues). -> nombre de matchs ajoutés."""
    if not (C.XWORLD_ENABLED and C.XBET_ENABLED and C.SOURCES_REFRESH) or scan is None:
        return 0
    try:
        found = (lister or xbet.list_leagues)(C.XB_KEYS)
    except Exception as e:                          # noqa: BLE001 - une source en panne ne casse pas le matin
        print(f"[xworld] championnats 1xBet ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        return 0
    n = 0
    for key, games in found.items():
        evs = events(games, key, day, after)
        if evs and key not in scan.setdefault("leagues", {}):
            scan["leagues"][key] = evs
            n += len(evs)
    print(f"[xworld] {n} match(s) ajouté(s) depuis 1xBet dans {sum(1 for k in found if k in scan.get('leagues', {}))} championnat(s) "
          "hors The Odds API.", file=sys.stderr)
    return n
