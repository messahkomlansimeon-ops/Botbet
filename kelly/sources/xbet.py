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
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

from .. import config as C, stats, store
from . import http, names, state

SOURCE = "xbet"
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
    if not r.ok:
        raise XbetError(r.error or "réponse vide")
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


def _ladder(groups, total, home, away, out):
    """Plus/moins d'une famille (total du match, de l'équipe 1, de l'équipe 2) -> out[(marché, côté, ligne)] = cote."""
    for c in _cells(groups):
        side = T_TOTAL.get(c.get("type"))
        market = side and {"total": total, "home": home, "away": away}[side[0]]
        if market and c.get("parameter") is not None:
            out[(market, side[1], float(c["parameter"]))] = float(c["cf"])


def parse_main(data, home, away):
    """Marché principal (premier niveau de gameEvents) -> ({(marché Kelly, côté, ligne): cote}, {type de sous-match: id})."""
    out = {}
    names = {"home": home, "draw": "Draw", "away": away}
    dc = {"1X": f"{home} or Draw", "X2": f"Draw or {away}", "12": f"{home} or {away}"}
    groups = data.get("eventGroups") or []
    for c in _cells(groups):
        t, cf = c.get("type"), float(c["cf"])
        if t in T_1X2:
            out[("h2h", names[T_1X2[t]], None)] = cf
        elif t in T_DC:
            out[("double_chance", dc[T_DC[t]], None)] = cf
        elif t in T_BTTS:
            out[("btts", T_BTTS[t], None)] = cf
    _ladder(groups, "totals", None, None, out)
    subs = {}
    for s in data.get("subGamesForMainGame") or []:
        if s.get("gameTypeId") in SUBGAMES and not s.get("period") and s.get("id"):
            subs.setdefault(s["gameTypeId"], s["id"])
    return out, subs


def _events(gid):
    # marketType=2 (« Populaires ») : 1X2, double chance, toute l'échelle des buts, les 2 marquent, sous-matchs typés, et pour un
    # sous-match statistique tout son plus/moins (match et équipes) ; 7 Ko compressés au lieu de 48 Ko pour « tous les marchés »
    return _get_json(url("gameEvents", cfView=3, countEvents=250, gameId=gid, grMode=4, marketType=2), TTL_ODDS)


def prices(game, markets, home, away):
    """Cotes 1xBet d'un match pour les marchés Kelly voulus : {(marché, côté, ligne): cote} ; `home` / `away` : noms Kelly (double
    chance). Une requête pour le marché principal, une de plus par famille statistique demandée (sous-match ouvert seulement :
    ils ouvrent la veille ou le jour du match)."""
    data = _events(game["gid"])
    out, subs = parse_main(data, home, away)
    for t in sorted({MARKET_SUB[m] for m in markets if m in MARKET_SUB}):
        if t in subs:
            _ladder((_events(subs[t]) or {}).get("eventGroups"), *SUBGAMES[t], out)
    return out


def book_prob(px, market, name, point):
    """Proba implicite de 1xBet sans sa marge pour un pari à deux issues (plus/moins, oui/non) : None si l'autre issue manque."""
    other = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}.get(name)
    a, b = px.get((market, name, point)), px.get((market, other, point)) if other else None
    return round((1 / a) / (1 / a + 1 / b), 4) if a and b else None


def lookup(px, market, name, point):
    """Cote 1xBet d'un pari Kelly (None si 1xBet ne le propose pas)."""
    return px.get((market, name, None if point is None else float(point)))


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

    def call(self, fn, *a):
        while True:
            try:
                return fn(*a)
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


def fetch_day(events, deadline=None, max_matches=None, wanted=None):
    """Cotes 1xBet des matchs Kelly `events` ([(clé, événement)] dans l'ordre de priorité) : {id Kelly: {"gid", "px"}}. Un
    événement qui porte "xgid" (id 1xBet déjà connu : relevé du matin, championnat vu par 1xBet) est lu directement, sans menu ni
    liste du championnat. `wanted` : {id Kelly: marchés à couvrir} (sous-matchs statistiques demandés seulement pour ceux-là).
    Voir _Session (proxy, plafond) ; s'arrête au délai `deadline` (time.monotonic) ou après `max_matches` matchs. Ne lève pas :
    erreurs dans state (rapport des sources)."""
    out, errors, leagues, cat = {}, [], {}, None
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
            if key not in LEAGUES:
                continue
            if key not in leagues:
                try:
                    cat = cat if cat is not None else ses.call(menu)
                    leagues[key] = ses.call(games, key, cat)
                except XbetError as e:
                    errors.append(f"{key} : {e}")
                    leagues[key] = []
                    if cat is None:                 # menu illisible : inutile d'insister
                        break
            g = find(ev, leagues[key])
            if not g:
                continue
        try:
            out[ev["id"]] = {"gid": g["gid"], "px": ses.call(prices, g, (wanted or {}).get(ev["id"], ()), ev["home_team"],
                                                             ev["away_team"])} | {"home": g["home"], "away": g["away"]}
        except XbetError as e:
            errors.append(f"{ev['home_team']} - {ev['away_team']} : {e}")
    ses.close(f"cotes de {len(out)} match(s)")
    state.record_run(SOURCE, len(out), errors)
    return out
