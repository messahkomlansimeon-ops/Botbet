"""Cotes 1xBet (1xbet.ci) : le bookmaker où l'utilisateur joue. Kelly y lit le prix EXACT de ses pronostics (buts, double chance,
« les 2 marquent », corners, tirs, tirs cadrés, fautes, touches, hors-jeu) au lieu de dire « joue si la cote est au moins X ».

Accès poli, uniquement par les adresses publiques que le site sert à tout visiteur : robots.txt respecté et cache (http.fetch),
identité KellyBot déclarée, 3 s entre deux requêtes, un budget de temps et de matchs par passage. Aucune astuce anti-blocage :
ni jeton x-hd, ni proxy, ni faux navigateur ; un 403/429 ou une page anti-robot coupe la source 24 h (sources/state.py).

Chemin (sans jeton). PIÈGE : l'API refuse (HTTP 400) une adresse dont les paramètres ne sont pas dans l'ordre alphabétique (url()).
  1. menu() : leftMenuSports?selectedMs=2.1, les championnats de football (id, nom anglais) ;
  2. games(clé Kelly) : games1x2?selectedMs=1.1.<id>,2.1.<id>,10.1.<id>, les matchs à venir du championnat (id, équipes, heure) ;
  3. prices(id, familles) : gameEvents?gameId= ; le marché principal est AU PREMIER NIVEAU (1X2, double chance, buts, les 2
     marquent) — subGamesForMainGame[0] est la 1re mi-temps, pas le match. Les sous-matchs statistiques y sont listés avec leur
     type (2 corners, 12 tirs cadrés, 95 tirs vers le but, 14 fautes, 13 hors-jeu, 55 touches) : un gameEvents de plus par
     famille donne toute l'échelle des lignes (total du match, total de chaque équipe).
Cartons : 1xBet ne compte que les jaunes (« Cartons jaunes »). Kelly modélise donc les jaunes SEULS (marché « team_yellow_total », règlement en jaunes) et
lit leur échelle chez 1xBet ; l'ancien marché jaunes + rouges (alternate_totals_cards, The Odds API) n'a toujours pas de prix (non comparable).
Le type de sous-match des jaunes n'est pas connu d'avance : il est reconnu à son NOM (« Cartons jaunes », « Yellow cards »), ou à son identifiant donné dans
XBET_YELLOW_TYPES (variable d'environnement, ex. « 17,18 »). Corners : le sous-match corners porte aussi le total de CHAQUE équipe (types 11 à 14), lu comme les tirs.
"""
import json
import os
import re
from collections import namedtuple
from contextlib import contextmanager
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

from .. import config as C, stats, store
from . import http, names, state

SOURCE = "xbet"
COUPON_BACKOFF = (2.0, 5.0, 10.0)   # attentes (s) avant chaque nouvel essai quand 1xBet refuse un code (refus intermittents) : 17 s au plus
DOMAIN = "1xbet.ci"
BASE = "https://" + DOMAIN
http.register_source(DOMAIN, SOURCE)    # un POST (SaveCoupon) obéit aux mêmes blocages que les lectures : 403 / 429 / Cloudflare / vérification = source coupée 24 h
http.require_proxy(DOMAIN)              # jamais en direct : sans proxy, aucune requête vers 1xBet (ton IP n'est jamais montrée)
FEED = BASE + "/service-api/main-line-feed/v3/"
COMMON = {"fcountry": 96, "gr": 673, "lng": "fr", "ref": 286}
JSON_H = {"Accept": "application/json"}
TTL_MENU = 6 * http.HOUR
TTL_GAMES = 2 * http.HOUR
TTL_ODDS = 2 * http.HOUR          # les cotes bougent : relues au plus toutes les 2 h

# Championnat Kelly (catalogue) -> nom anglais du championnat chez 1xBet (menu)
LEAGUES = {
    "soccer_epl": "England. Premier League", "soccer_efl_champ": "England. Championship",
    "soccer_england_league1": "England. League One", "soccer_england_league2": "England. League Two",
    "soccer_england_efl_cup": "England. League Cup", "soccer_spain_la_liga": "Spain. La Liga",
    "soccer_spain_segunda_division": "Spain. Segunda Division", "soccer_italy_serie_a": "Italy. Serie A",
    "soccer_italy_serie_b": "Italy. Serie B", "soccer_germany_bundesliga": "Germany. Bundesliga",
    "soccer_germany_bundesliga2": "Germany. 2. Bundesliga", "soccer_germany_liga3": "Germany. 3. Liga",
    "soccer_france_ligue_one": "France. Ligue 1", "soccer_france_ligue_two": "France. Ligue 2",
    "soccer_uefa_champs_league": "UEFA Champions League", "soccer_uefa_europa_league": "UEFA Europa League",
    "soccer_uefa_europa_conference_league": "UEFA Conference League", "soccer_uefa_nations_league": "UEFA Nations League",
    "soccer_portugal_primeira_liga": "Portugal. Primeira Liga", "soccer_netherlands_eredivisie": "Netherlands. Eredivisie",
    "soccer_belgium_first_div": "Belgium. Jupiler League", "soccer_turkey_super_league": "Turkey. SuperLiga",
    "soccer_spl": "Scotland. Premier League", "soccer_greece_super_league": "Greece. SuperLeague",
    "soccer_switzerland_superleague": "Switzerland. SuperLeague", "soccer_austria_bundesliga": "Austria. Bundesliga",
    "soccer_poland_ekstraklasa": "Poland. Ekstraklasa", "soccer_denmark_superliga": "Denmark. Superliga",
    "soccer_sweden_allsvenskan": "Sweden. Allsvenskan", "soccer_sweden_superettan": "Sweden. Superettan",
    "soccer_norway_eliteserien": "Norway. Eliteserien", "soccer_finland_veikkausliiga": "Finland. Veikkausliiga",
    "soccer_league_of_ireland": "Ireland. Premier League", "soccer_brazil_campeonato": "Brazil. Campeonato Brasileiro. Serie A",
    "soccer_brazil_serie_b": "Brazil. Campeonato Brasileiro. Serie B", "soccer_argentina_primera_division": "Argentina. Primera Division",
    "soccer_usa_mls": "USA. MLS", "soccer_mexico_ligamx": "Mexico. Liga MX", "soccer_chile_campeonato": "Chile. Primera Division",
    "soccer_conmebol_copa_libertadores": "Copa Libertadores", "soccer_conmebol_copa_sudamericana": "Copa Sudamericana",
    "soccer_japan_j_league": "Japan. J-League", "soccer_korea_kleague1": "South Korea. K League 1",
    "soccer_saudi_arabia_pro_league": "Saudi Arabia. Saudi Professional League", "soccer_china_superleague": "China. Super League",
    "soccer_australia_aleague": "Australia. A League", "soccer_africa_cup_of_nations": "Africa Cup of Nations",
    "soccer_international_friendlies": "Friendlies. National Teams",
}

LEAGUES.update({k: x for rows in C.XB_LEAGUES.values() for k, _, x, _ in rows})      # championnats hors The Odds API (xworld)

# Familles statistiques : type de sous-match 1xBet -> (marché total, marché équipe 1, marché équipe 2) de Kelly (None : pas chez Kelly)
YELLOW = "yellow"        # pseudo-type du sous-match « Cartons jaunes » (reconnu à son nom, voir sub_type) ; les autres types sont les gameTypeId de 1xBet
YELLOW_TYPES = frozenset(int(x) for x in re.findall(r"\d+", os.environ.get("XBET_YELLOW_TYPES", "")))      # gameTypeId des jaunes, si connu
_YELLOW_NAME = re.compile(r"(?:cartons?|cards?)\s+jaunes?|yellow\s+cards?", re.I)
_NOT_YELLOW = re.compile(r"rouges?|\bred\b|mi-temps|half|1st|2nd|1[eè]re|2[eè]me|\bhc\b", re.I)      # jaunes + rouges, ou une mi-temps : pas le match entier
SUBGAMES = {2: ("alternate_totals_corners", "team_corners_home", "team_corners_away"), YELLOW: ("team_yellow_total", None, None), 12: ("team_sot_total", "team_sot_home", "team_sot_away"),
            95: ("team_shots_total", "team_shots_home", "team_shots_away"),
            14: ("team_fouls_total", "team_fouls_home", "team_fouls_away"),
            55: ("team_throws_total", "team_throws_home", "team_throws_away"), 13: ("team_offsides_total", None, None)}
OFF_LANE_TYPES = frozenset((55, 13))     # touches, hors-jeu : plus aucun ticket (lanes.ticket_ok faux) -> leur sous-match n'est plus relu (une requête de proxy gaspillée par match)
MARKET_SUB = {m: t for t, ms in SUBGAMES.items() if t not in OFF_LANE_TYPES for m in ms if m}
MAIN_MARKETS = ("totals", "btts", "double_chance", "h2h")
# Types de paris 1xBet (cellule {type, cf, parameter}) : 1X2, double chance, plus/moins (total, équipe 1, équipe 2), les 2 marquent
T_1X2 = {1: "home", 2: "draw", 3: "away"}
T_DC = {4: "1X", 5: "12", 6: "X2"}
T_TOTAL = {9: ("total", "Over"), 10: ("total", "Under"), 11: ("home", "Over"), 12: ("home", "Under"),
           13: ("away", "Over"), 14: ("away", "Under")}
T_BTTS = {180: "Yes", 181: "No"}


class XbetError(Exception):
    pass


def url(endpoint, **params):
    """Adresse de l'API, paramètres triés par ordre alphabétique (sinon HTTP 400)."""
    return FEED + endpoint + "?" + urlencode(sorted((COMMON | params).items()))


def _get_json(address, ttl):
    r = http.fetch(address, SOURCE, ttl, headers=JSON_H)
    if not r.ok or r.stale or r.error:         # http.fetch rend l'ANCIEN corps du cache (stale + error) en cas d'échec : jamais des cotes périmées
        raise XbetError(r.error or ("réponse périmée" if r.ok else "réponse vide"))
    try:
        return json.loads(r.content)
    except ValueError:
        http.invalidate(address, JSON_H)
        raise XbetError("réponse illisible (pas du JSON)") from None


def menu():
    """{nom anglais: {"id"}} des championnats de football du moment (une requête, gardée 6 h)."""
    data = _get_json(url("leftMenuSports", selectedMs="2.1"), TTL_MENU)
    out = {}
    for sec in data if isinstance(data, list) else []:
        for sport in sec.get("sports") or []:
            if sport.get("id") != 1:
                continue
            for liga in sport.get("ligas") or []:
                for x in liga.get("subLigas") or [liga]:
                    if x.get("nameEng") and x.get("id") and x["nameEng"] not in out:
                        out[x["nameEng"]] = {"id": x["id"]}
    if not out:                                     # le football a toujours des championnats : la réponse n'a plus la forme connue
        http.invalidate(url("leftMenuSports", selectedMs="2.1"), JSON_H)
        raise XbetError(f"{FORMAT} (menu sans championnat)")
    return out


def _name(o):
    """Nom de l'équipe : l'anglais d'abord (« CSKA Moscow » plutôt que « CSKA Moscou ») : plus proche de FotMob et The Odds API."""
    return (o or {}).get("fullNameEng") or (o or {}).get("fullName") or (o or {}).get("name") or ""


_SPECIAL = re.compile(r"^(home|away|à domicile|à l'extérieur)\b|special", re.I)      # paris spéciaux présentés comme des matchs


def parse_games(data):
    """Matchs d'une réponse games1x2 : [{"gid", "home", "away", "ts"}] (matchs principaux seulement, pas les paris spéciaux)."""
    return [{"gid": g["id"], "home": _name(g.get("opponent1")), "away": _name(g.get("opponent2")), "ts": g["startTs"]}
            for g in (data if isinstance(data, list) else []) if isinstance(g, dict) and g.get("id") and g.get("startTs")
            and g.get("id") == g.get("mainGameId", g["id"]) and _name(g.get("opponent1")) and _name(g.get("opponent2"))
            and not _SPECIAL.search(_name(g.get("opponent1"))) and not _SPECIAL.search(_name(g.get("opponent2")))
            and (g.get("sport") or {}).get("id") in (1, None)]


def games(key, cat=None):
    """Matchs à venir chez 1xBet du championnat Kelly `key` ([] si 1xBet ne le propose pas). `cat` : menu() déjà lu."""
    name = LEAGUES.get(key)
    cat = cat if cat is not None else menu()
    ent = cat.get(name) if name else None
    if not ent:
        return []
    sel = ",".join(f"{a}.1.{ent['id']}" for a in (1, 2, 10))
    return parse_games(_get_json(url("games1x2", cfView=3, count=40, grMode=4, selectedMs=sel), TTL_GAMES))


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _words(name):
    return {w for w in names.norm(name).split() if len(w) >= 4 and w not in stats.GENERIC}


_MARK_WOMEN = {"w", "women", "womens", "woman", "ladies", "feminin", "feminine", "femenino", "femminile", "frauen", "dames", "fem"}
_MARK_RESERVE = {"b", "ii", "iii", "res", "reserve", "reserves", "castilla", "jong"}
_LAST_ONLY = {"w", "b", "ii", "iii"}              # lettres et chiffres romains : un marqueur seulement en fin de nom (« Getafe B », « Arsenal W »)


def marks(name):
    """Marqueurs d'un nom d'équipe : femmes (« Women », « W », « Féminin »), réserve (« B », « II », « Castilla », « Jong », « Next Gen »), jeunes
    (« U19 », « U-21 », « Youth ») -> frozenset. Deux noms qui ne portent pas les mêmes marqueurs ne désignent jamais la même équipe (« Real Madrid » et
    « Real Madrid Castilla » ne sont pas le même club)."""
    toks = names.norm(name).split()
    out = set()
    for i, t in enumerate(toks):
        if t in _LAST_ONLY and i != len(toks) - 1:
            continue
        if t in _MARK_WOMEN:
            out.add("w")
        elif t in _MARK_RESERVE:
            out.add("b")
        elif t == "youth":
            out.add("y")
        elif re.fullmatch(r"u\d{2}", t):
            out.add(t)
        elif t == "u" and i + 1 < len(toks) and re.fullmatch(r"\d{2}", toks[i + 1]):
            out.add("u" + toks[i + 1])
        elif t == "next" and i + 1 < len(toks) and toks[i + 1] == "gen":
            out.add("b")
    return frozenset(out)


def loose(a, b):
    """Deux noms qui partagent un mot distinctif : l'un dans l'autre (« aldosivi » / « caaldosivi ») ou mêmes 5 premières lettres
    (« seinajoki » / « seinajoen ») ; les mots génériques (club, atletico, united...) ne comptent pas. Jamais deux équipes de marqueurs différents
    (marks : « Brazil » / « Brazil U20 », « Real Madrid » / « Real Madrid Castilla »)."""
    if marks(a) != marks(b):
        return False
    return any(x in y or y in x or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5]) for x in _words(a) for y in _words(b))


def find(ev, cands):
    """Match 1xBet d'un événement Kelly (home_team, away_team, commence_time), parmi les matchs du MÊME championnat :
    coup d'envoi à 3 h près et les deux équipes reconnues ; ou même coup d'envoi (10 min) avec une équipe sûre, ou les deux noms qui
    se recoupent (« CAAldosivi » / « Aldosivi Mar del Plata »), à condition qu'un seul match du championnat convienne à cette heure
    (une équipe ne joue jamais deux matchs en même temps). Garde-fous : les marqueurs femmes / réserve / jeunes (marks) doivent être les mêmes de
    chaque côté ; un match rangé à l'envers chez 1xBet (l'extérieur de Kelly en premier) n'est jamais accepté — les cotes « domicile / extérieur »
    seraient inversées — et, quand les deux sens se ressemblent, le bon sens l'emporte."""
    t = _ts(ev["commence_time"])
    best, score, near = None, 0.0, []
    for g in cands:
        dt = abs(g["ts"] - t)
        if dt > 3 * 3600:
            continue
        if marks(ev["home_team"]) != marks(g["home"]) or marks(ev["away_team"]) != marks(g["away"]):
            continue
        sh, sa = stats.team_sim(ev["home_team"], g["home"]), stats.team_sim(ev["away_team"], g["away"])
        rh, ra = stats.team_sim(ev["home_team"], g["away"]), stats.team_sim(ev["away_team"], g["home"])      # le même match, à l'envers
        if min(rh, ra) >= 0.8 and (min(sh, sa) < 0.8 or rh + ra > sh + sa):       # le même match, mais à l'envers (ou l'envers lui ressemble davantage)
            continue
        if min(sh, sa) >= 0.8 and sh + sa > score:
            best, score = g, sh + sa
        elif dt <= 600 and rh + ra <= sh + sa and (max(sh, sa) >= 0.85 or ((sh >= 0.6 or loose(ev["home_team"], g["home"]))
                                                                            and (sa >= 0.6 or loose(ev["away_team"], g["away"])))):
            near.append(g)
    if best:
        return best
    return near[0] if len(near) == 1 else None


def _iter_cells(groups):
    """Toutes les cellules chiffrées (cf) d'un gameEvents -> (cellule, suspendue). Une cellule « blocked » (sous-match listé avant d'être ouvert, marché
    SUSPENDU un moment) a une cote affichée mais n'est pas jouable dans l'appli."""
    for eg in groups if isinstance(groups, list) else []:
        for ev in (eg.get("events") if isinstance(eg, dict) else None) or []:
            for c in ev if isinstance(ev, list) else []:
                if isinstance(c, dict) and c.get("cf"):
                    yield c, bool(c.get("blocked"))


def _cells(groups):
    """Cellules jouables d'un gameEvents : jamais un prix pour Kelly pour une cellule suspendue (pas de pari, pas de jambe de combiné, pas de coupon)."""
    for c, blocked in _iter_cells(groups):
        if not blocked:
            yield c


def _ladder(groups, total, home, away, out, refs=None, gid=0, blocked=None):
    """Plus/moins d'une famille (total du match, de l'équipe 1, de l'équipe 2) -> out[(marché, côté, ligne)] = cote.
    `refs` (si fourni) : même clé -> [GameId 1xBet, Type, Param] (GameId = sous-match statistique, pas le match). `blocked` (si fourni) : ensemble
    des clés SUSPENDUES (cote affichée, pas jouable pour l'instant)."""
    for c, susp in _iter_cells(groups):
        side = T_TOTAL.get(c.get("type"))
        market = side and {"total": total, "home": home, "away": away}[side[0]]
        if market and c.get("parameter") is not None:
            p = float(c["parameter"])
            if susp:
                if blocked is not None:
                    blocked.add((market, side[1], p))
                continue
            out[(market, side[1], p)] = float(c["cf"])
            if refs is not None:
                refs[(market, side[1], p)] = [gid, c.get("type"), p]


def sub_type(s):
    """Famille statistique d'un sous-match 1xBet `s` (dict de subGamesForMainGame) : son gameTypeId s'il figure dans SUBGAMES, YELLOW si c'est le sous-match
    « Cartons jaunes » (identifiant de XBET_YELLOW_TYPES, ou nom qui dit jaunes sans rouges ni mi-temps), sinon None. Jamais une mi-temps (period)."""
    if not isinstance(s, dict) or s.get("period") or not s.get("id"):
        return None
    t = s.get("gameTypeId")
    if isinstance(t, int) and t in SUBGAMES:
        return t
    if isinstance(t, int) and t in YELLOW_TYPES:
        return YELLOW
    name = str(s.get("subGameName") or s.get("name") or "")
    return YELLOW if _YELLOW_NAME.search(name) and not _NOT_YELLOW.search(name) else None


def parse_main(data, home, away, refs=None, gid=0, blocked=None):
    """Marché principal (premier niveau de gameEvents) -> ({(marché Kelly, côté, ligne): cote}, {type de sous-match: id}).
    `refs` (si fourni) : même clé -> [GameId 1xBet, Type, Param] du pari (référence pour un ticket enregistré). `blocked` (si fourni) : ensemble
    des clés SUSPENDUES un moment par 1xBet (absentes du résultat, mais pas retirées de l'offre)."""
    out = {}
    names = {"home": home, "draw": "Draw", "away": away}
    dc = {"1X": f"{home} or Draw", "X2": f"Draw or {away}", "12": f"{home} or {away}"}
    groups = data.get("eventGroups") or []
    for c, susp in _iter_cells(groups):
        t, cf = c.get("type"), float(c["cf"])
        if t in T_1X2:
            key = ("h2h", names[T_1X2[t]], None)
        elif t in T_DC:
            key = ("double_chance", dc[T_DC[t]], None)
        elif t in T_BTTS:
            key = ("btts", T_BTTS[t], None)
        else:
            continue
        if susp:
            if blocked is not None:
                blocked.add(key)
            continue
        out[key] = cf
        if refs is not None:
            refs[key] = [gid, t, 0]
    _ladder(groups, "totals", "team_totals_home", "team_totals_away", out, refs, gid, blocked)      # buts du match, puis de chaque équipe (types 11 à 14)
    subs = {}
    for s in data.get("subGamesForMainGame") or []:
        t = sub_type(s)
        if t is not None:
            subs.setdefault(t, s["id"])
    return out, subs


_ODDS_TTL = [TTL_ODDS]           # C.XBET_FRESH_MIN min pendant live_mode : cotes relues sauf si elles viennent d'être lues


@contextmanager
def live_mode(fresh_min=None):
    """Vérification en direct (combi N, boutons « 🔄 ») : cotes relues même pendant une commande Telegram (hors ligne par ailleurs :
    seul 1xBet est relu), C.XBET_LIVE_DELAY s entre deux requêtes ; celles lues il y a moins de C.XBET_FRESH_MIN min (relevé,
    « Mettre à jour tous les paris » ou combi juste avant) sont reprises du cache : pas de proxy pour rien. Tout est rétabli à la sortie."""
    from .. import cache as kcache
    old = (kcache.OFFLINE, _ODDS_TTL[0], http.DELAY_OVERRIDE.get(DOMAIN))
    fresh = C.XBET_FRESH_MIN if fresh_min is None else fresh_min           # « combi N » : C.XBET_COMBI_FRESH_MIN
    kcache.OFFLINE, _ODDS_TTL[0], http.DELAY_OVERRIDE[DOMAIN] = False, fresh * 60, C.XBET_LIVE_DELAY
    try:
        yield
    finally:
        kcache.OFFLINE, _ODDS_TTL[0] = old[0], old[1]
        if old[2] is None:
            http.DELAY_OVERRIDE.pop(DOMAIN, None)
        else:
            http.DELAY_OVERRIDE[DOMAIN] = old[2]


def _events_url(gid):
    return url("gameEvents", cfView=3, countEvents=250, gameId=gid, grMode=4, marketType=2)


def odds_time(gids):
    """Instant (timestamp) du plus ancien relevé en cache des matchs 1xBet `gids` (GameId de la cote : le sous-match pour des corners
    ou des tirs, sinon le match) ; None si aucun."""
    ts = [hit[0].get("fetched_at") for g in gids if g for hit in [http._read(http._key(_events_url(g), JSON_H))] if hit]
    ts = [t for t in ts if t]
    return min(ts) if ts else None


Cell = namedtuple("Cell", "cf at blocked", defaults=(False,))      # cote, instant du relevé, cellule suspendue ; cf[0], cf[1] comme l'ancien couple


def cached_cell(ref):
    """Cote 1xBet du pari `ref` ([GameId, Type, Param], c["xref"]) lue dans le CACHE du disque, sans aucune requête (alertes : pas de
    données de proxy pour rien) -> Cell(cote, instant du relevé, suspendu) ; cote None = le relevé en cache ne propose plus ce pari (ABSENT) ;
    suspendu vrai = le pari est listé mais 1xBet l'a suspendu un moment (cote affichée, pas jouable : pas « retiré ») ; None si le match n'est pas
    en cache."""
    try:
        gid, typ, param = ref
        hit = http._read(http._key(_events_url(gid), JSON_H))
        if not hit:
            return None
        at = hit[0].get("fetched_at")
        for c, susp in _iter_cells(json.loads(hit[1]).get("eventGroups")):
            if c.get("type") == typ and float(c.get("parameter") or 0) == float(param or 0):
                return Cell(float(c["cf"]), at, susp)
        return Cell(None, at, False)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _events(gid):
    # marketType=2 (« Populaires ») : 1X2, double chance, toute l'échelle des buts, les 2 marquent, sous-matchs typés, et pour un
    # sous-match statistique tout son plus/moins (match et équipes) ; 7 Ko compressés au lieu de 48 Ko pour « tous les marchés »
    data = _get_json(_events_url(gid), _ODDS_TTL[0])
    if not isinstance(data, dict):                  # [] / null : 1xBet n'a rien (plus) pour ce match ; pas gardé en cache
        http.invalidate(_events_url(gid), JSON_H)
        raise XbetError("réponse inattendue (pas un objet JSON)")
    return data


def prices(game, markets, home, away):
    """Cotes 1xBet d'un match pour les marchés Kelly voulus : {(marché, côté, ligne): cote} ; `home` / `away` : noms Kelly (double
    chance). Une requête pour le marché principal, une de plus par famille statistique demandée (sous-match ouvert seulement :
    ils ouvrent la veille ou le jour du match). L'id permanent du match (lien vers sa page) est noté dans game["cid"], la
    référence 1xBet de chaque cote ([GameId, Type, Param], pour un ticket enregistré) dans game["refs"]."""
    data = _events(game["gid"])
    game["cid"] = data.get("constId")
    blocked = set()
    out, subs = parse_main(data, home, away, refs := {}, game["gid"], blocked)
    for t in sorted({MARKET_SUB[m] for m in markets if m in MARKET_SUB}, key=str):
        if t in subs:
            try:
                _ladder(_events(subs[t]).get("eventGroups"), *SUBGAMES[t], out, refs, subs[t], blocked)
            except XbetError as e:                  # un sous-match (corners, tirs...) en erreur ne fait pas perdre le marché principal déjà lu
                game.setdefault("errors", []).append(f"{home} - {away} (sous-match {SUBGAMES[t][0]}) : {e}")
    game["refs"] = refs
    game["blocked"] = {k for k in blocked if k not in out}      # suspendus un moment : ni prix, ni « retirés » (alertes)
    return out


CLOSED = "pas un objet JSON"       # message de _events quand 1xBet répond [] / null : le sous-match n'existe plus


def read_closing(gids, deadline=None):
    """Relevé de CLÔTURE (kelly/closeread.py) : relit à l'instant le(s) sous-match(s) 1xBet `gids` (GameId de la cote, `xref[0]`) et laisse la réponse dans le cache disque, d'où
    ensemble.mark_x_close la lira (`cached_cell`). Une session à part (proxy, plafond du mois, compteur de données) ; PAS fetch_day : aucun observateur (voie « cotes ») ne voit
    ces lectures partielles (sous-match seul, sans marché principal). Ne lève jamais. -> ({gid: instant du relevé, ou None si 1xBet n'a plus ce sous-match}, (octets, requêtes), raison
    de l'abandon ou None). Un sous-match en PANNE (réseau, délai, proxy) est absent du résultat : il sera relu au passage suivant."""
    out, why = {}, None
    ses = _Session()
    if ses.why:
        return out, (0, 0), ses.why
    try:
        with live_mode(0):                          # TTL 0 : toujours relu, jamais repris du cache
            for g in gids:
                if (deadline and time.monotonic() > deadline) or ses.full():
                    why = "délai dépassé" if not ses.full() else "plafond du mois atteint" if not state.is_blocked(SOURCE) else "1xBet refuse l'accès"
                    break
                try:
                    ses.call(_events, g)
                    hit = http._read(http._key(_events_url(g), JSON_H))
                    out[g] = hit[0].get("fetched_at") if hit else None
                except Exception as e:              # noqa: BLE001 - un sous-match en erreur ne fait pas perdre les autres
                    msg = http.scrub(e)
                    if CLOSED in msg:               # 1xBet n'a plus ce sous-match (fermé) : réponse, pas panne -> noté sans relevé
                        out[g] = None
                    print(f"[1xbet] relevé de clôture {g} : {msg}", file=sys.stderr)       # toute autre erreur (réseau, délai, proxy) : ABSENT du résultat, le passage suivant réessaie
    finally:
        spent = _spent(ses.start)
        ses.close(f"clôture de {len(out)} sous-match(s)")
    return out, spent, why


def match_url(const_id):
    """Page publique du match chez 1xBet (l'id du championnat et les noms sont complétés par le site) ; None sans id."""
    return f"{BASE}/fr/line/football/1/{const_id}" if const_id else None


def book_prob(px, market, name, point):
    """Proba implicite de 1xBet sans sa marge pour un pari à deux issues (plus/moins, oui/non) : None si l'autre issue manque."""
    other = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}.get(name)
    a, b = px.get((market, name, point)), px.get((market, other, point)) if other else None
    return round((1 / a) / (1 / a + 1 / b), 4) if a and b else None


def _canon(market, name):
    """Nom d'une issue sans casse, ni ordre pour une double chance (« Nul ou … » : « Draw or Arsenal » = « Arsenal or Draw »)."""
    n = str(name).strip().casefold()
    if market == "double_chance":
        return frozenset(p.strip() for p in n.split(" or "))
    return n


def resolve(px, market, name, point):
    """Clé du pari Kelly (marché, `name`, ligne) telle que 1xBet l'a dans `px`, ou None : le nom exact d'abord, puis, pour le 1X2 et la
    double chance (The Odds API écrit « Draw or Arsenal » là où parse_main écrit « Arsenal or Draw »), sans casse ni ordre."""
    pt = None if point is None else float(point)
    if (market, name, pt) in px:
        return (market, name, pt)
    if market in ("h2h", "double_chance") and isinstance(name, str):
        want = _canon(market, name)
        return next((k for k in px if k[0] == market and k[2] == pt and isinstance(k[1], str) and _canon(market, k[1]) == want), None)
    return None


def same_bet(a, b):
    """Deux paris Kelly (dicts : marché, côté, ligne) sont-ils le même pari chez 1xBet ? Sans casse, ni ordre des équipes en double chance."""
    pa, pb = (None if x.get("point") is None else float(x["point"]) for x in (a, b))
    return a["market"] == b["market"] and pa == pb and _canon(a["market"], a["name"]) == _canon(b["market"], b["name"])


def lookup(px, market, name, point):
    """Cote 1xBet d'un pari Kelly (None si 1xBet ne le propose pas). Voir resolve pour les noms de 1X2 et de double chance."""
    k = resolve(px, market, name, point)
    return px.get(k) if k else None


USAGE = "xbet_usage.json"       # données 1xBet du mois passées par le proxy : {"month", "bytes", "requests", "days": {jour: octets}}


def proxy():
    """Proxy de 1xBet : XBET_PROXY de l'environnement, sinon la ligne XBET_PROXY= de C.XBET_PROXY_FILE (fichier de scrap_stat). None
    si introuvable. Jamais affiché (il contient le mot de passe)."""
    if os.environ.get("XBET_PROXY"):
        return os.environ["XBET_PROXY"].strip()
    try:
        with open(C.XBET_PROXY_FILE, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == "XBET_PROXY" and v.strip():
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


FORMAT = "format du site 1xBet changé ?"          # réponse lisible mais vide là où il y a toujours des données
EMPTY_SHARE, EMPTY_MIN = 0.8, 5                   # cotes vides pour 80 % des matchs lus (5 au moins) : le format a sans doute changé
HARD_FAIL = ("ProxyError", "ConnectTimeout")                    # le proxy lui-même ne répond pas : une autre session Webshare peut aider
SOFT_FAIL = ("ReadTimeout", "SSLError", "ConnectionError")     # peut aussi venir de 1xBet (coupure de son côté) : au plus UNE autre session par relevé
PROXY_FAIL = HARD_FAIL + SOFT_FAIL
MAX_SWITCH = 5                  # sessions Webshare essayées au plus par relevé quand le proxy lui-même est en panne
MAX_SESSION_SPAN = 50           # numéros de session Webshare utilisés au plus à partir de celui de XBET_PROXY (au-delà : on repart du numéro de base)
BREAKER = 3                     # matchs de suite en échec (proxy / réseau / réponse inutilisable) avant d'interrompre le relevé


def with_session(url, n):
    """Même proxy Webshare, session collante n° `n` (« user-GB-20:mdp@... » -> « user-GB-21:mdp@... ») ; None si pas de numéro."""
    m = re.match(r"^(.*-[A-Z]{2}-)(\d+)(:.*)$", url or "")
    return f"{m.group(1)}{n}{m.group(3)}" if m else None


def session_no(url):
    m = re.match(r"^.*-[A-Z]{2}-(\d+):", url or "")
    return int(m.group(1)) if m else None


def usage(today=None):
    """Données 1xBet du mois en cours (dict USAGE remis à zéro au changement de mois)."""
    today = str(today or state.now().date())
    u = store.load(USAGE, {})
    return u if isinstance(u, dict) and u.get("month") == today[:7] else {"month": today[:7], "bytes": 0, "requests": 0, "days": {}}


def _spent(start):
    w = http.WIRE.get(DOMAIN, {"bytes": 0, "requests": 0})
    return w["bytes"] - start["bytes"], w["requests"] - start["requests"]


def _save_usage(u, start, session=None):
    """Ajoute les données de la session (depuis `start`) au compteur du mois. Le compteur est RELU ici : un autre relevé a pu écrire pendant celui-ci (la fenêtre des stats
    relit 1xBet sans le verrou des données, voir statswindow), sans quoi le plus lent des deux effacerait la consommation de l'autre. `session` : numéro de session Webshare
    qui a vraiment répondu (à retenir) ; sinon celui déjà enregistré reste."""
    b, n = _spent(start)
    day = str(state.now().date())

    def add(doc):                                      # atomique (une seule transaction) : le suivi, le matin et la fenêtre des stats peuvent compter en même temps
        cur = doc if isinstance(doc, dict) and doc.get("month") == day[:7] else {"month": day[:7], "bytes": 0, "requests": 0, "days": {}}
        cur["bytes"], cur["requests"] = int(cur["bytes"] + b), cur["requests"] + n
        cur["days"][day] = int(cur["days"].get(day, 0) + b)
        cur["days"] = dict(sorted(cur["days"].items())[-40:])
        if session is not None:
            cur["session"] = session
        return cur, cur
    cur = store.update(USAGE, add, {})
    u.clear()
    u.update(cur)
    return b, n


def usage_line(u=None):
    u = u or usage()
    return f"1xBet via proxy : {u['bytes'] / 1e6:.1f} Mo ce mois ({u['requests']} requêtes), plafond {C.XBET_MONTH_MB:.0f} Mo"


class _Session:
    """Un relevé 1xBet : proxy (proxy() ; introuvable = rien, sauf C.XBET_DIRECT ; dernière session Webshare qui marchait), compteur
    de données du mois et plafond C.XBET_MONTH_MB. `call(fn, *a)` : proxy en panne (502 du fournisseur, délai) -> session Webshare
    suivante, MAX_SWITCH fois au plus (une seule fois pour une coupure qui peut venir de 1xBet lui-même : SOFT_FAIL) ; jamais pour un refus de
    1xBet (403/429 : source coupée 24 h par http.fetch). Le numéro de session ne sort jamais de [base, base + MAX_SESSION_SPAN[ et n'est retenu
    (data/xbet_usage.json) que si la nouvelle session a VRAIMENT répondu."""

    def __init__(self):
        self.u, self.cap, self.switches, self.soft, self.why = usage(), C.XBET_MONTH_MB * 1e6, 0, 0, None
        self.switched = None                         # numéro de session Webshare adopté pendant ce relevé (None : celle enregistrée reste)
        self.url = proxy()
        self.base, self.trying = session_no(self.url), None
        if state.is_blocked(SOURCE):
            self.why = state.health(SOURCE)["last_error"] or "source bloquée"
        elif not self.url and not C.XBET_DIRECT:
            self.why = f"proxy introuvable (XBET_PROXY ou {C.XBET_PROXY_FILE}) : aucun relevé, jamais en direct"
        elif self.url and not http.proxy_alive(self.url):
            self.why = f"le proxy ne répond pas : aucun relevé (nouvel essai dans {http.PROXY_DOWN_S // 60} min), jamais en direct"
        elif self.u["bytes"] >= self.cap:
            self.why = f"plafond mensuel atteint ({usage_line(self.u)}) : relevé sauté"
        saved = self.u.get("session")
        if (self.url and self.base is not None and isinstance(saved, int) and self.base <= saved < self.base + MAX_SESSION_SPAN
                and with_session(self.url, saved)):
            self.url = with_session(self.url, saved)
        if self.url:
            http.PROXIES[DOMAIN] = self.url
        else:
            http.PROXIES.pop(DOMAIN, None)
        self.start = dict(http.WIRE.get(DOMAIN, {"bytes": 0, "requests": 0}))

    def full(self):
        """Plafond du mois atteint (avec ce relevé) ou source bloquée en cours de route."""
        return self.u["bytes"] + _spent(self.start)[0] >= self.cap or state.is_blocked(SOURCE)

    def _next(self):
        """URL du proxy avec le numéro de session suivant (revient au numéro de base après MAX_SESSION_SPAN) ; None sans numéro."""
        cur = http.PROXIES.get(DOMAIN)
        n = (session_no(cur) or 0) + 1
        if self.base is not None and n >= self.base + MAX_SESSION_SPAN:
            n = self.base
        return with_session(cur, n)

    def call(self, fn, *a, **kw):
        while True:
            try:
                out = fn(*a, **kw)
                if self.trying is not None:         # la session essayée a répondu : elle devient celle des prochains relevés
                    self.u["session"], self.trying = self.trying, None
                    self.switched = self.u["session"]
                return out
            except XbetError as e:
                msg = str(e)
                hard = any(k in msg for k in HARD_FAIL)
                nxt = self._next()
                if (not nxt or self.switches >= MAX_SWITCH or not (hard or any(k in msg for k in SOFT_FAIL))
                        or (not hard and self.soft >= 1)):
                    raise
                self.switches += 1
                self.soft += 0 if hard else 1
                http.PROXIES[DOMAIN] = nxt
                self.trying = session_no(nxt)
                print(f"[1xbet] proxy en panne ({http.scrub(e)}) : essai de la session Webshare n° {self.trying}", file=sys.stderr)

    def close(self, what):
        b, n = _save_usage(self.u, self.start, self.switched)
        print(f"[1xbet] {what} : {n} requête(s), {b / 1e3:.0f} Ko via {'le proxy' if self.url else 'la connexion directe'} ; "
              f"{usage_line()}", file=sys.stderr)


def list_leagues(keys, deadline=None):
    """Matchs à venir chez 1xBet des championnats Kelly `keys` (C.XB_KEYS : hors The Odds API) : {clé: [{"gid", "home", "away",
    "ts"}]}. Un menu + une liste (~10 Ko) par championnat proposé ce jour-là. Ne lève pas."""
    out, errors, ses = {}, [], _Session()
    if ses.why:
        state.record_run(SOURCE, 0, [ses.why])
        return out
    try:
        cat = ses.call(menu)
    except XbetError as e:
        ses.close("liste des championnats")
        state.record_run(SOURCE, 0, [f"menu : {e}"])
        return out
    for key in keys:
        if (deadline and time.monotonic() > deadline) or ses.full():
            break
        if LEAGUES.get(key) not in cat:
            continue
        try:
            out[key] = ses.call(games, key, cat)
        except XbetError as e:
            errors.append(f"{key} : {e}")
    ses.close(f"liste des matchs de {len(out)} championnat(s)")
    state.record_run(SOURCE, sum(len(v) for v in out.values()), errors)
    return out


def prioritise_stats(events, wanted):
    """`events` avec d'abord les matchs dont on veut des sous-matchs statistiques (tirs, tirs cadrés, fautes, touches, hors-jeu : un gameEvents de plus par
    famille, dans `wanted`), puis les autres, chacun dans l'ordre reçu. Si le délai du relevé tombe, ce sont eux qui valent le plus par requête : ils
    nourrissent les combinés de stats. Sans `wanted`, l'ordre ne change pas. Appelé par xprice.apply pour le relevé du matin seulement (les relevés en direct
    gardent l'ordre de l'appelant : combinés et paris montrés d'abord)."""
    events = list(events)
    if not wanted:
        return events
    stat = lambda e: any(m in MARKET_SUB and MARKET_SUB[m] != 2 for m in wanted.get(e[1].get("id"), ()))      # noqa: E731 - type 2 = corners : tous les matchs
    return sorted(events, key=lambda e: not stat(e))                                                          # tri stable


OBSERVERS = []                                  # fonctions (cotes_lues, événements) appelées après chaque fetch_day


def fetch_day(events, deadline=None, max_matches=None, wanted=None, live=False, fresh_min=None, record=True):
    """Cotes 1xBet des matchs Kelly `events` ([(clé, événement)] dans l'ordre de priorité) : {id Kelly: {"gid", "px"}}. Un
    événement qui porte "xgid" (id 1xBet déjà connu : relevé du matin, championnat vu par 1xBet) est lu directement, sans menu ni
    liste du championnat. `wanted` : {id Kelly: marchés à couvrir} (sous-matchs statistiques demandés seulement pour ceux-là).
    Voir _Session (proxy, plafond) ; s'arrête au délai `deadline` (time.monotonic) ou après `max_matches` matchs. `live` : cotes
    relues à l'instant (live_mode). Ne lève pas : erreurs dans state (rapport des sources)."""
    if live:
        with live_mode(fresh_min):
            return fetch_day(events, deadline, max_matches, wanted, record=record)
    out, errors, leagues, cat, menu_down = {}, [], {}, None, False
    max_matches = C.XBET_MAX_MATCHES if max_matches is None else max_matches
    events = list(events)
    ses = _Session()
    if ses.why:
        if record:
            state.record_run(SOURCE, 0, [ses.why])
            _note_run(0, len(events), (0, ses.why.split(" : ")[0][:80]), [])
        return out
    fails, cut = 0, None
    try:                                            # la session est TOUJOURS fermée : les données du proxy sont comptées même si un match fait lever
        for i, (key, ev) in enumerate(events):
            if len(out) >= max_matches:
                break
            if (deadline and time.monotonic() > deadline) or ses.full():
                cut = (i, "délai dépassé" if not ses.full() else "plafond du mois atteint" if not state.is_blocked(SOURCE) else "1xBet refuse l'accès")
                break
            if ev.get("xgid"):
                g = {"gid": ev["xgid"], "home": ev["home_team"], "away": ev["away_team"]}
            else:
                if key not in LEAGUES or (cat is None and menu_down):
                    continue
                if key not in leagues:
                    try:
                        cat = cat if cat is not None else ses.call(menu)
                        leagues[key] = ses.call(games, key, cat)
                    except XbetError as e:
                        errors.append(f"{key} : {e}")
                        leagues[key] = []
                        menu_down = cat is None         # menu illisible : inutile d'insister, mais les matchs à id 1xBet connu restent lus
                        if menu_down:
                            continue
                g = find(ev, leagues[key])
                if not g:
                    continue
            try:
                px = ses.call(prices, g, (wanted or {}).get(ev["id"], ()), ev["home_team"], ev["away_team"])
                out[ev["id"]] = {"gid": g["gid"], "px": px, "refs": g.get("refs"), "cid": g.get("cid"),
                                 "home": g["home"], "away": g["away"], "blocked": g.get("blocked") or set()}
                errors += g.get("errors") or []
                fails = 0
            except Exception as e:                  # noqa: BLE001 - un match en échec (réseau, réponse inutilisable) ne fait perdre ni les autres ni le compte des données
                errors.append(f"{ev['home_team']} - {ev['away_team']} : {e}")
                fails += 1
                if fails >= BREAKER:                # proxy ou réseau en panne : inutile d'attendre le délai de chaque match restant
                    errors.append(f"relevé interrompu : {BREAKER} matchs de suite en échec (proxy ou réseau)")
                    cut = (i + 1, "proxy ou réseau en panne")
                    break
    finally:
        ses.close(f"cotes de {len(out)} match(s)")
        empty = sum(1 for v in out.values() if not v["px"])
        if len(out) >= EMPTY_MIN and empty >= EMPTY_SHARE * len(out):
            errors.insert(0, f"{FORMAT} (cotes vides pour {empty}/{len(out)} matchs)")
        if record:                                  # un relevé annexe (voie `saison`) ne remplace pas le compte rendu du relevé principal
            state.record_run(SOURCE, len(out), errors)
            _note_run(len(out), len(events), cut, errors)
    for fn in OBSERVERS:                            # observateurs (voie « cotes » : kelly/oddsonly.py) : lecture seule du résultat, aucune requête, ne lèvent jamais
        try:
            fn(out, events)
        except Exception as e:                      # noqa: BLE001
            print(f"[1xbet] observateur ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return out


def _note_run(read, want, cut, errors):
    """Bilan du relevé pour le status et le message du matin (sources_state.json, source « xbet ») : « run » = le dernier (matchs lus, prévus) ;
    « cut » = la dernière coupure (délai, plafond, panne, relevé sauté) ou le dernier format suspect, GARDÉE même si un relevé suivant passe (le
    relevé des paris réels, souvent en cache, ne cache plus celui des pronostics). Ne lève jamais."""
    try:
        at, fmt_bad = state._iso(state.now()), any(FORMAT in e for e in errors)
        kw = {"run": {"at": at, "read": read, "want": want}}
        if cut or fmt_bad:
            kw["cut"] = {"at": at, "read": read, "left": max(0, want - cut[0]) if cut else 0, "why": cut[1] if cut else None, "format": fmt_bad}
        state._put(SOURCE, **kw)
    except Exception as e:                          # noqa: BLE001
        print(f"[1xbet] bilan du relevé non noté : {type(e).__name__}: {e}", file=sys.stderr)


def save_coupon(legs):
    """Enregistre un combiné chez 1xBet (ticket sans mise, Summ 0, sans connexion) et retourne son code (str) ou None.
    Ne lève jamais (un échec de coupon ne bloque jamais l'envoi du combiné).
    `legs` : liste de pronostics avec 'xref' ([GameId, Type, Param]) et 'xodds' (cote/Coef), ou événements bruts. Un pari LIVE (c["live"],
    kelly/live.py) part avec Kind 1 et le ticket avec Live vrai (le site fait pareil : clés « Kind » par pari, « Live » du ticket) ; avec Kind 3
    (avant-match) 1xBet rendait un code, mais le relisait « évènements terminés » et l'appli le refusait (le 10/10 : BR3HG, XF4HG, Q9LHG)."""
    if not legs:
        return None
    events = []
    live = any(isinstance(c, dict) and c.get("live") for c in legs)
    try:                                            # une référence mal formée (xref / xodds illisibles) ne lève jamais : pas de code
        for c in legs:
            if isinstance(c, dict) and "GameId" in c:
                events.append(c)
            elif isinstance(c, dict) and c.get("xref") and c.get("xodds"):
                gid, t, p = c["xref"]
                events.append({
                    "GameId": gid,
                    "Type": int(t),
                    "Coef": float(c["xodds"]),
                    "Param": 0.0 if p is None else float(p),
                    "PV": None,
                    "PlayerId": 0,
                    "Kind": 1 if c.get("live") else 3,       # 1 : match en cours (live), 3 : avant-match (ligne)
                    "InstrumentId": 0,
                    "Seconds": 0,
                    "Price": 0,
                    "Expired": 0,
                    "PlayersDuel": None,
                })
            else:
                return None
    except Exception as e:                          # noqa: BLE001 - un échec de coupon ne bloque jamais l'envoi du combiné
        _coupon_fail(f"pari illisible ({http.err(e)})", legs)
        return None
    if not events:
        return None
    body = {
        "Events": events,
        "Summ": 0,
        "Vid": 1,
        "Kind": 1 if live else 3,
        "partner": C.XBET_COUPON_PARTNER,      # 1 : lisible dans l'appli mobile ET sur le site 1xbet.ci (voir config)
        "Live": live,
        "notWait": True,
        "CheckCf": 1,
    }
    ses = _Session()
    if ses.why:
        return None

    def post():
        r = http.post(BASE + "/service-api/LiveBet/Open/SaveCoupon", body)
        # proxy en panne AVANT l'envoi (jamais un délai de lecture : le ticket a pu être créé) : session suivante via _Session.call
        if r and not r.ok and r.error and any(k in r.error for k in ("ProxyError", "ConnectTimeout")):
            raise XbetError(r.error)
        return r
    def attempt():
        """Un enregistrement -> (code ou None, refus passager de 1xBet : on peut réessayer). Raison d'un échec écrite au journal (avec le code HTTP)."""
        try:
            r = ses.call(post)
            status = getattr(r, "status", None)
            if r is not None and getattr(r, "blocked", False):
                _coupon_fail(f"HTTP {status or '?'} {r.error or 'accès refusé'} : 1xBet coupé 24 h", legs)
                return None, False                  # 403 / 429 / Cloudflare / vérification : la source est coupée (state.block), jamais d'insistance
            if not r or not r.ok or not r.content:
                _coupon_fail(f"HTTP {status or '?'} {getattr(r, 'error', '') or 'réponse vide'}", legs)
                return None, False                  # coupure ou erreur réseau : le ticket a pu être créé, jamais renvoyé
            try:
                data = json.loads(r.content)
            except ValueError:
                _coupon_fail(f"HTTP {status or '?'} : réponse illisible (pas du JSON)", legs)
                return None, False
            if not isinstance(data, dict):
                _coupon_fail(f"HTTP {status or '?'} : réponse inattendue ({type(data).__name__})", legs)
                return None, False
            if not data.get("Success"):
                _coupon_fail(json.dumps({k: data.get(k) for k in ("Error", "ErrorCode", "Value") if data.get(k) not in (None, "")},
                                        ensure_ascii=False), legs)
                attempt.why = _coupon_why(data)
                return None, data.get("ErrorCode") in COUPON_TRANSIENT      # seul un refus connu comme passager vaut un nouvel essai
            val = data.get("Value")
            if isinstance(val, str) and val.strip():
                return val.strip(), False
            if isinstance(val, dict) and val.get("Guid"):
                return str(val["Guid"]).strip(), False
            _coupon_fail("réponse sans code : " + str(val)[:200], legs)
            return None, False                      # Success vrai sans code : le ticket a pu être créé, jamais renvoyé
        except Exception as e:                      # noqa: BLE001 - un échec de coupon ne bloque jamais l'envoi du combiné
            _coupon_fail(http.err(e), legs)
        return None, False
    # refus intermittents de 1xBet (même ticket accepté quelques minutes plus tard) : nouveaux essais à attente croissante
    # (COUPON_BACKOFF), seulement après un « non » explicite ET connu comme passager (COUPON_TRANSIENT : ErrorCode 161627) : aucun ticket n'a été
    # créé. Les autres refus (Success faux) sont définitifs, et un Success vrai n'est JAMAIS rejoué (le ticket peut exister).
    try:
        attempt.why, _notice.sent = "", False
        code, again = attempt()
        for i, wait in enumerate(COUPON_BACKOFF, 2):
            if code or not again:
                break
            _notice(f"⏳ 1xBet a refusé le code coupon" + (f" (« {attempt.why} »)" if attempt.why else "")
                    + f" : nouvel essai dans {wait:g} s ({i}/{len(COUPON_BACKOFF) + 1})…")
            attempt.why = ""
            time.sleep(wait)
            code, again = attempt()
        if code and _notice.sent:
            _notice("✅ 1xBet a accepté le code coupon.")
        return code
    finally:
        ses.close("enregistrement du coupon")


COUPON_ERRORS = {161627: "panne passagère chez 1xBet"}     # ErrorCode de SaveCoupon -> raison en français (161627 : « Сбой вызова, попробуйте позже. »)


COUPON_TRANSIENT = frozenset(COUPON_ERRORS)       # ErrorCode seuls réessayés (2, 5 puis 10 s) ; tout autre refus explicite est définitif


def _coupon_why(data):
    """Raison d'un refus de SaveCoupon, en français pour l'utilisateur : traduction connue de l'ErrorCode, sinon le texte de 1xBet s'il
    est en alphabet latin, sinon « erreur chez 1xBet (code N) » (1xBet répond parfois en russe)."""
    code, text = data.get("ErrorCode"), str(data.get("Error") or "").strip()
    if code in COUPON_ERRORS:
        return COUPON_ERRORS[code]
    if text and not re.search(r"[\u0400-\u04FF]", text):
        return text[:120]
    return f"erreur chez 1xBet (code {code})" if code else "erreur chez 1xBet"


_NOTICE = None          # fonction(texte) pendant une commande Telegram : l'utilisateur qui attend est prévenu d'un refus de 1xBet


@contextmanager
def coupon_notice(send):
    """Pendant ce bloc (une commande ou un bouton Telegram), un code coupon refusé par 1xBet puis réessayé est annoncé à
    l'utilisateur avec `send(texte)` ; jamais pendant les relevés automatiques (personne n'attend la réponse)."""
    global _NOTICE
    old, _NOTICE = _NOTICE, send
    try:
        yield
    finally:
        _NOTICE = old


def _notice(text):
    """Prévient l'utilisateur qui attend (coupon_notice) ; rien hors d'une commande ; ne lève jamais."""
    if _NOTICE is None:
        return
    try:
        _NOTICE(text)
        _notice.sent = True
    except Exception as e:                          # noqa: BLE001 - un message d'attente ne bloque jamais le code coupon
        print(f"[1xbet] message d'attente non envoyé : {http.err(e)}", file=sys.stderr)


_notice.sent = False


def _coupon_fail(why, legs):
    """Journal d'un code coupon refusé par 1xBet : la raison donnée (sans identifiants du proxy) et les paris envoyés (match, type,
    ligne, cote), pour comprendre « pas de code » sans recréer de ticket. Ne lève jamais (références mal formées comprises)."""
    why = http.scrub(re.sub(r"://[^@\s]*@", "://***@", str(why)))[:400]
    try:
        refs = ", ".join(f"{(c.get('xref') or ['?'])[0]}/{(c.get('xref') or [0, '?'])[1]}/{(c.get('xref') or [0, 0, '?'])[2]}@{c.get('xodds')}"
                         for c in legs if isinstance(c, dict))
    except Exception:                               # noqa: BLE001 - xref qui n'est pas une liste de trois éléments
        refs = "références illisibles"
    print(f"[1xbet] coupon refusé : {why} — paris : {refs[:600]}", file=sys.stderr)
