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
Cartons : 1xBet ne compte que les jaunes (« Cartons jaunes ») alors que Kelly compte jaunes + rouges : pas de prix (non comparable).
"""
import json
import os
import re
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
SUBGAMES = {2: ("alternate_totals_corners", None, None), 12: ("team_sot_total", "team_sot_home", "team_sot_away"),
            95: ("team_shots_total", "team_shots_home", "team_shots_away"),
            14: ("team_fouls_total", "team_fouls_home", "team_fouls_away"),
            55: ("team_throws_total", "team_throws_home", "team_throws_away"), 13: ("team_offsides_total", None, None)}
MARKET_SUB = {m: t for t, ms in SUBGAMES.items() for m in ms if m}
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


def loose(a, b):
    """Deux noms qui partagent un mot distinctif : l'un dans l'autre (« aldosivi » / « caaldosivi ») ou mêmes 5 premières lettres
    (« seinajoki » / « seinajoen ») ; les mots génériques (club, atletico, united...) ne comptent pas."""
    return any(x in y or y in x or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5]) for x in _words(a) for y in _words(b))


def find(ev, cands):
    """Match 1xBet d'un événement Kelly (home_team, away_team, commence_time), parmi les matchs du MÊME championnat :
    coup d'envoi à 3 h près et les deux équipes reconnues ; ou même coup d'envoi (10 min) avec une équipe sûre, ou les deux noms qui
    se recoupent (« CAAldosivi » / « Aldosivi Mar del Plata »), à condition qu'un seul match du championnat convienne à cette heure
    (une équipe ne joue jamais deux matchs en même temps)."""
    t = _ts(ev["commence_time"])
    best, score, near = None, 0.0, []
    for g in cands:
        dt = abs(g["ts"] - t)
        if dt > 3 * 3600:
            continue
        sh, sa = stats.team_sim(ev["home_team"], g["home"]), stats.team_sim(ev["away_team"], g["away"])
        if min(sh, sa) >= 0.8 and sh + sa > score:
            best, score = g, sh + sa
        elif dt <= 600 and (max(sh, sa) >= 0.85 or ((sh >= 0.6 or loose(ev["home_team"], g["home"]))
                                                    and (sa >= 0.6 or loose(ev["away_team"], g["away"])))):
            near.append(g)
    if best:
        return best
    return near[0] if len(near) == 1 else None


def _cells(groups):
    for eg in groups or []:
        for ev in eg.get("events") or []:
            for c in ev or []:
                if isinstance(c, dict) and c.get("cf"):
                    yield c


def _ladder(groups, total, home, away, out, refs=None, gid=0):
    """Plus/moins d'une famille (total du match, de l'équipe 1, de l'équipe 2) -> out[(marché, côté, ligne)] = cote.
    `refs` (si fourni) : même clé -> [GameId 1xBet, Type, Param] (GameId = sous-match statistique, pas le match)."""
    for c in _cells(groups):
        side = T_TOTAL.get(c.get("type"))
        market = side and {"total": total, "home": home, "away": away}[side[0]]
        if market and c.get("parameter") is not None:
            p = float(c["parameter"])
            out[(market, side[1], p)] = float(c["cf"])
            if refs is not None:
                refs[(market, side[1], p)] = [gid, c.get("type"), p]


def parse_main(data, home, away, refs=None, gid=0):
    """Marché principal (premier niveau de gameEvents) -> ({(marché Kelly, côté, ligne): cote}, {type de sous-match: id}).
    `refs` (si fourni) : même clé -> [GameId 1xBet, Type, Param] du pari (référence pour un ticket enregistré)."""
    out = {}
    names = {"home": home, "draw": "Draw", "away": away}
    dc = {"1X": f"{home} or Draw", "X2": f"Draw or {away}", "12": f"{home} or {away}"}
    groups = data.get("eventGroups") or []
    for c in _cells(groups):
        t, cf = c.get("type"), float(c["cf"])
        if t in T_1X2:
            key = ("h2h", names[T_1X2[t]], None)
        elif t in T_DC:
            key = ("double_chance", dc[T_DC[t]], None)
        elif t in T_BTTS:
            key = ("btts", T_BTTS[t], None)
        else:
            continue
        out[key] = cf
        if refs is not None:
            refs[key] = [gid, t, 0]
    _ladder(groups, "totals", None, None, out, refs, gid)
    subs = {}
    for s in data.get("subGamesForMainGame") or []:
        if s.get("gameTypeId") in SUBGAMES and not s.get("period") and s.get("id"):
            subs.setdefault(s["gameTypeId"], s["id"])
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


def cached_cell(ref):
    """Cote 1xBet du pari `ref` ([GameId, Type, Param], c["xref"]) lue dans le CACHE du disque, sans aucune requête (alertes : pas de
    données de proxy pour rien) -> (cote, instant du relevé) ; cote None = le relevé en cache ne propose plus ce pari ; None si le match
    n'est pas en cache."""
    try:
        gid, typ, param = ref
        hit = http._read(http._key(_events_url(gid), JSON_H))
        if not hit:
            return None
        for c in _cells(json.loads(hit[1]).get("eventGroups")):
            if c.get("type") == typ and float(c.get("parameter") or 0) == float(param or 0):
                return float(c["cf"]), hit[0].get("fetched_at")
        return None, hit[0].get("fetched_at")
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _events(gid):
    # marketType=2 (« Populaires ») : 1X2, double chance, toute l'échelle des buts, les 2 marquent, sous-matchs typés, et pour un
    # sous-match statistique tout son plus/moins (match et équipes) ; 7 Ko compressés au lieu de 48 Ko pour « tous les marchés »
    return _get_json(_events_url(gid), _ODDS_TTL[0])


def prices(game, markets, home, away):
    """Cotes 1xBet d'un match pour les marchés Kelly voulus : {(marché, côté, ligne): cote} ; `home` / `away` : noms Kelly (double
    chance). Une requête pour le marché principal, une de plus par famille statistique demandée (sous-match ouvert seulement :
    ils ouvrent la veille ou le jour du match). L'id permanent du match (lien vers sa page) est noté dans game["cid"], la
    référence 1xBet de chaque cote ([GameId, Type, Param], pour un ticket enregistré) dans game["refs"]."""
    data = _events(game["gid"])
    game["cid"] = data.get("constId")
    out, subs = parse_main(data, home, away, refs := {}, game["gid"])
    for t in sorted({MARKET_SUB[m] for m in markets if m in MARKET_SUB}):
        if t in subs:
            _ladder((_events(subs[t]) or {}).get("eventGroups"), *SUBGAMES[t], out, refs, subs[t])
    game["refs"] = refs
    return out


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


PROXY_FAIL = ("ProxyError", "ConnectTimeout", "ReadTimeout", "SSLError", "ConnectionError")
MAX_SWITCH = 5                  # sessions Webshare essayées au plus par relevé quand le proxy lui-même est en panne


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


def _save_usage(u, start):
    b, n = _spent(start)
    day = str(state.now().date())
    u["bytes"], u["requests"] = int(u["bytes"] + b), u["requests"] + n
    u["days"][day] = int(u["days"].get(day, 0) + b)
    u["days"] = dict(sorted(u["days"].items())[-40:])
    store.save(USAGE, u)
    return b, n


def usage_line(u=None):
    u = u or usage()
    return f"1xBet via proxy : {u['bytes'] / 1e6:.1f} Mo ce mois ({u['requests']} requêtes), plafond {C.XBET_MONTH_MB:.0f} Mo"


class _Session:
    """Un relevé 1xBet : proxy (proxy() ; introuvable = rien, sauf C.XBET_DIRECT ; dernière session Webshare qui marchait), compteur
    de données du mois et plafond C.XBET_MONTH_MB. `call(fn, *a)` : proxy en panne (502 du fournisseur, délai) -> session Webshare
    suivante, MAX_SWITCH fois au plus ; jamais pour un refus de 1xBet (403/429 : source coupée 24 h par http.fetch)."""

    def __init__(self):
        self.u, self.cap, self.switches, self.why = usage(), C.XBET_MONTH_MB * 1e6, 0, None
        self.url = proxy()
        if state.is_blocked(SOURCE):
            self.why = state.health(SOURCE)["last_error"] or "source bloquée"
        elif not self.url and not C.XBET_DIRECT:
            self.why = f"proxy introuvable (XBET_PROXY ou {C.XBET_PROXY_FILE}) : aucun relevé, jamais en direct"
        elif self.u["bytes"] >= self.cap:
            self.why = f"plafond mensuel atteint ({usage_line(self.u)}) : relevé sauté"
        if self.url and self.u.get("session") and with_session(self.url, self.u["session"]):
            self.url = with_session(self.url, self.u["session"])
        if self.url:
            http.PROXIES[DOMAIN] = self.url
        else:
            http.PROXIES.pop(DOMAIN, None)
        self.start = dict(http.WIRE.get(DOMAIN, {"bytes": 0, "requests": 0}))

    def full(self):
        """Plafond du mois atteint (avec ce relevé) ou source bloquée en cours de route."""
        return self.u["bytes"] + _spent(self.start)[0] >= self.cap or state.is_blocked(SOURCE)

    def call(self, fn, *a, **kw):
        while True:
            try:
                return fn(*a, **kw)
            except XbetError as e:
                nxt = with_session(http.PROXIES.get(DOMAIN), (session_no(http.PROXIES.get(DOMAIN)) or 0) + 1)
                if not nxt or self.switches >= MAX_SWITCH or not any(k in str(e) for k in PROXY_FAIL):
                    raise
                self.switches += 1
                http.PROXIES[DOMAIN] = nxt
                self.u["session"] = session_no(nxt)
                print(f"[1xbet] proxy en panne ({e}) : session Webshare n° {self.u['session']}", file=sys.stderr)

    def close(self, what):
        b, n = _save_usage(self.u, self.start)
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


def fetch_day(events, deadline=None, max_matches=None, wanted=None, live=False, fresh_min=None):
    """Cotes 1xBet des matchs Kelly `events` ([(clé, événement)] dans l'ordre de priorité) : {id Kelly: {"gid", "px"}}. Un
    événement qui porte "xgid" (id 1xBet déjà connu : relevé du matin, championnat vu par 1xBet) est lu directement, sans menu ni
    liste du championnat. `wanted` : {id Kelly: marchés à couvrir} (sous-matchs statistiques demandés seulement pour ceux-là).
    Voir _Session (proxy, plafond) ; s'arrête au délai `deadline` (time.monotonic) ou après `max_matches` matchs. `live` : cotes
    relues à l'instant (live_mode). Ne lève pas : erreurs dans state (rapport des sources)."""
    if live:
        with live_mode(fresh_min):
            return fetch_day(events, deadline, max_matches, wanted)
    out, errors, leagues, cat, menu_down = {}, [], {}, None, False
    max_matches = C.XBET_MAX_MATCHES if max_matches is None else max_matches
    ses = _Session()
    if ses.why:
        state.record_run(SOURCE, 0, [ses.why])
        return out
    for key, ev in events:
        if len(out) >= max_matches or (deadline and time.monotonic() > deadline) or ses.full():
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
                             "home": g["home"], "away": g["away"]}
        except XbetError as e:
            errors.append(f"{ev['home_team']} - {ev['away_team']} : {e}")
    ses.close(f"cotes de {len(out)} match(s)")
    state.record_run(SOURCE, len(out), errors)
    return out


def save_coupon(legs):
    """Enregistre un combiné chez 1xBet (ticket sans mise, Summ 0, sans connexion) et retourne son code (str) ou None.
    Ne lève jamais (un échec de coupon ne bloque jamais l'envoi du combiné).
    `legs` : liste de pronostics avec 'xref' ([GameId, Type, Param]) et 'xodds' (cote/Coef), ou événements bruts."""
    if not legs:
        return None
    events = []
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
                "Kind": 3,
                "InstrumentId": 0,
                "Seconds": 0,
                "Price": 0,
                "Expired": 0,
                "PlayersDuel": None,
            })
        else:
            return None
    if not events:
        return None
    body = {
        "Events": events,
        "Summ": 0,
        "Vid": 1,
        "Kind": 3,
        "partner": C.XBET_COUPON_PARTNER,      # 1 : lisible dans l'appli mobile ET sur le site 1xbet.ci (voir config)
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
        """Un enregistrement -> (code ou None, refus explicite de 1xBet : on peut réessayer). Raison d'un échec écrite au journal."""
        try:
            r = ses.call(post)
            if not r or not r.ok or not r.content:
                _coupon_fail(f"HTTP {getattr(r, 'status', '?')} {getattr(r, 'error', '') or 'réponse vide'}", legs)
                return None, False                  # coupure ou erreur réseau : le ticket a pu être créé, jamais renvoyé
            data = json.loads(r.content)
            if not data.get("Success"):
                _coupon_fail(json.dumps({k: data.get(k) for k in ("Error", "ErrorCode", "Value") if data.get(k) not in (None, "")},
                                        ensure_ascii=False), legs)
                attempt.why = _coupon_why(data)
                return None, True
            val = data.get("Value")
            if isinstance(val, str) and val.strip():
                return val.strip(), False
            if isinstance(val, dict) and val.get("Guid"):
                return str(val["Guid"]).strip(), False
            _coupon_fail("réponse sans code : " + str(val)[:200], legs)
            return None, True
        except Exception as e:                      # noqa: BLE001 - un échec de coupon ne bloque jamais l'envoi du combiné
            _coupon_fail(f"{type(e).__name__}: {e}", legs)
        return None, False
    # refus intermittents de 1xBet (même ticket accepté quelques minutes plus tard) : nouveaux essais à attente croissante
    # (COUPON_BACKOFF), seulement après un « non » explicite de 1xBet (Success false, réponse sans code) : aucun ticket n'a été créé
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
        print(f"[1xbet] message d'attente non envoyé : {type(e).__name__}: {e}", file=sys.stderr)


_notice.sent = False


def _coupon_fail(why, legs):
    """Journal d'un code coupon refusé par 1xBet : la raison donnée (sans identifiants du proxy) et les paris envoyés (match, type,
    ligne, cote), pour comprendre « pas de code » sans recréer de ticket."""
    why = re.sub(r"://[^@\s]*@", "://***@", str(why))[:400]
    refs = ", ".join(f"{(c.get('xref') or ['?'])[0]}/{(c.get('xref') or [0, '?'])[1]}/{(c.get('xref') or [0, 0, '?'])[2]}@{c.get('xodds')}"
                     for c in legs if isinstance(c, dict))
    print(f"[1xbet] coupon refusé : {why} — paris : {refs[:600]}", file=sys.stderr)
