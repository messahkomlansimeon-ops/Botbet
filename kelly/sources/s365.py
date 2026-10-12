"""Statistiques de match 365scores : corners, tirs, tirs cadrés, fautes, touches, hors-jeu et cartons de chaque équipe, pour les championnats
où Kelly n'a pas (assez) de matchs ailleurs : championnats vus par 1xBet (config.XB_LEAGUES), Argentine, Mexique, MLS, USL, Pérou, Uruguay...

API web publique du site (https://webws.365scores.com/web, celle que lit www.365scores.com), sondée le 9 octobre 2026 :
- robots.txt : webws.365scores.com/robots.txt répond 404 (aucune règle : tout est permis, RFC 9309) ; celui de www.365scores.com (200) ne
  parle que des pages du site (`*date=*`, `*/1*`...), jamais de ce nom d'hôte. http.fetch relit et respecte le robots.txt de webws à chaque
  passage (cache 24 h) : s'il se met à interdire /web/, Kelly s'arrête.
- une requête simple (requests, identité KellyBot déclarée, aucun en-tête de navigateur, aucune empreinte TLS imitée) suffit : HTTP 200 JSON.
  Politesse : >= 3 s entre deux requêtes (http.DEFAULT_DELAY), blocage de 24 h sur 403 / 429 / page de vérification (state.block), jamais de
  proxy, d'imitation de navigateur ni de rotation d'adresse.
- GET /games/results/?competitions={id} : environ les 50 DERNIERS matchs terminés du championnat (les dates startDate / endDate sont ignorées
  avec `competitions=`) + `paging.previousPage` (/web/games/?...&aftergame={id}&direction=-1) pour remonter plus loin : une page de
  15 à 85 matchs, 10 à 90 Ko.
- GET /game/stats/?games={id} : UNE requête par match (plusieurs ids : seul le premier est servi), ~7 à 20 Ko : statistics[] =
  {name, competitorId, value} par équipe. Noms lus en anglais (langId=1) : « Total Shots », « Shots On Target », « Corners », « Fouls »,
  « Throw-Ins », « Offsides », « Yellow Cards », « Red Cards ». Les championnats « légers » (Uruguay, Ukraine, Pologne 2...) n'ont pas les fautes ;
  l'Écosse 2 n'a ni touches ni hors-jeu ; certains matchs d'un même championnat n'ont pas les touches : un profil ne sert une moyenne qu'à
  partir de APISTATS_MIN_N matchs qui portent la statistique.
- `hasStats` (liste de résultats) vaut False pour des matchs entiers (Iran 100 %, Danemark 2 : 38 %, Costa Rica : 19 %) : jamais demandés
  (leurs « statistiques » sont des zéros). L'heure de début renvoyée par /game/stats/ porte un décalage horaire faux : la date d'un match est
  celle de la LISTE (timezoneName=UTC).
- les identifiants de championnat se heurtent entre sports (38 = LNB Élite, basket) : COMPETITIONS ne contient que des championnats vérifiés
  de football (sportId 1, `python -m kelly.sources.s365 verify`), et chaque match lu doit porter sportId 1 et la bonne competitionId.

Fichier data/s365_matches.json (compact, même schéma que fm_matches.json) :
  teams    {id 365: {nm, ln: nom long, sk: [clés de ligue], d: mise à jour, m: [[jour AAMMJJ, domicile 1/0, puis (pour, contre) pour chaque stat de
           KEYS]]}} : S365_KEEP_MATCHES derniers matchs par équipe ; None quand le match n'a pas la stat.
  seen     {id du match: jour} : jamais relu (match sans statistiques exploitables compris).
  pending  {id du match: {k: clé de ligue, d: jour, h / a: id des équipes, hn / an: noms, hl / al: noms longs}} : matchs listés dont les
           statistiques restent à lire (budget de requêtes par passage).
  lists    {clé de ligue: {at: dernière liste, new / old: plus récent / plus ancien match vu, prev: page plus ancienne à lire (None : fin),
           n / ns / nt: matchs, matchs avec statistiques, équipes de la dernière liste}}.
  recent   {id du match: {d, sk, h, a, s: {stat: [domicile, extérieur]}}} : matchs des S365_RECENT_DAYS derniers jours, pour le règlement des
           paris sur touches, hors-jeu, fautes, tirs... (match_stats, au format de fmmatch.match_stats).
  req      {day, n} : requêtes du jour (plafond S365_DAILY).
Les profils servis (profiles()) ont le format de football-data (n, throws_for, throws_against...) et sources.features._s365 les consomme.

Cadence : s365.fill() est appelée par le passage horaire (une poignée de requêtes à chaque fois : S365_GAMES_PER_RUN statistiques et
S365_LISTS_PER_RUN listes, S365_SECONDS au plus) ; le remplissage des S365_WINDOW_DAYS derniers jours se fait ainsi tout seul en quelques jours,
ou d'un coup avec `python -m kelly.sources.s365 backfill`.
"""
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

from .. import cache as kcache, config as C, store
from . import fmmatch, footballdata, http, names, state

NAME = "s365"
LABEL = "365scores"
FILE = "s365_matches.json"
HOST = "webws.365scores.com"
BASE = f"https://{HOST}/web"
PARAMS = (("appTypeId", "5"), ("langId", "1"), ("timezoneName", "UTC"), ("userCountryId", "21"))
SPORT_FOOTBALL = 1
HOUR = 3600
KEYS = ("goals", "shots", "sot", "corners", "fouls", "cards", "throws", "offsides", "yellow")      # mêmes noms que fmmatch.KEYS (sans xG) ; yellow (jaunes seuls) à la fin
STAT_NAMES = {"total shots": "shots", "shots on target": "sot", "corners": "corners", "fouls": "fouls", "throw-ins": "throws",
              "offsides": "offsides", "yellow cards": "yc", "red cards": "rc"}
NEVER_ZERO = ("throws", "shots", "fouls")      # 0-0 sur ces stats = non suivie pour ce match, pas un vrai zéro
COUNT_KEYS = ("shots", "sot", "corners", "fouls", "throws", "offsides")
MIN_STATS = 2                                  # statistiques de jeu au moins (hors cartons) pour qu'un match serve : cartons et corners seuls = rien de suivi
R_DAY, R_HOME, R_STATS = fmmatch.R_DAY, fmmatch.R_HOME, fmmatch.R_STATS
MAX_ERRORS = 3                                 # erreurs d'affilée avant d'arrêter le passage
MAX_PENDING = 1500
RETRY_FRESH = 3                                # essais d'un match d'hier sans statistiques encore publiées
# Réglages de ce module (config.py n'est pas touché ; chacun a sa variable d'environnement)
GAME_FAIL_MAX = http.env("S365_GAME_FAILS", 3)            # échecs de lecture d'un même match avant de le mettre de côté (marqué « vu » : plus jamais demandé)
GAME_FAIL_GAP_H = http.env("S365_GAME_FAIL_GAP_H", 6.0)   # ... et écart minimal entre deux essais d'un match en échec (heures)
SEEN_DAYS = http.env("S365_SEEN_DAYS", 60)                # « vus » gardés : plus que la fenêtre de remplissage (S365_WINDOW_DAYS + 14), jamais redemandés dans l'intervalle
MAX_SEEN = http.env("S365_MAX_SEEN", 20000)               # plafond de la liste des « vus » (les plus anciens d'abord)
SETTLE_RESERVE = http.env("S365_SETTLE_RESERVE", 0.15)    # part du plafond mensuel (S365_MONTH_MB) gardée pour le règlement des paris : le remplissage et le live s'arrêtent avant

# Clé de ligue Kelly -> id de championnat 365scores. Tous vérifiés le 9 octobre 2026 dans /search/ : sportId 1 (football), nom lu.
COMPETITIONS = {
    # Amériques (catalogue The Odds API)
    "soccer_argentina_primera_division": 72,      # Liga Profesional
    "soccer_brazil_campeonato": 113, "soccer_brazil_serie_b": 116, "soccer_usa_mls": 104, "soccer_mexico_ligamx": 141,
    "soccer_chile_campeonato": 135,               # First Division
    # Asie / Océanie
    "soccer_japan_j_league": 147, "soccer_korea_kleague1": 618, "soccer_saudi_arabia_pro_league": 649, "soccer_china_superleague": 150,
    "soccer_australia_aleague": 151,
    # Europe sans statistiques football-data (CSV « new » : buts seulement)
    "soccer_denmark_superliga": 119, "soccer_norway_eliteserien": 131, "soccer_sweden_allsvenskan": 122, "soccer_poland_ekstraklasa": 153,
    "soccer_austria_bundesliga": 111, "soccer_switzerland_superleague": 95, "soccer_league_of_ireland": 68,
    "soccer_finland_veikkausliiga": 126, "soccer_germany_liga3": 34,
    # Championnats vus par 1xBet (config.XB_LEAGUES), Europe
    "soccer_xb_romania": 136,                     # Liga 1
    "soccer_xb_croatia": 158,                     # HNL
    "soccer_xb_serbia": 628,                      # Super Liga
    "soccer_xb_czech": 117,                       # Chance Liga
    "soccer_xb_russia": 89,                       # Premier Liga
    "soccer_xb_ukraine": 129,                     # Premier League
    "soccer_xb_hungary": 137,                     # OTP Bank Liga
    "soccer_xb_bulgaria": 163,                    # Parva Liga
    "soccer_xb_israel": 42,                       # Premier League
    "soccer_xb_cyprus": 160,                      # Cyta Championship
    "soccer_xb_slovakia": 145,                    # Niké liga
    "soccer_xb_slovenia": 170,                    # PrvaLiga
    "soccer_xb_scotland_2": 62,                   # Scottish Championship
    "soccer_xb_netherlands_2": 58,                # Eerste Divisie
    "soccer_xb_portugal_2": 74,                   # Liga Portugal 2
    "soccer_xb_turkey_2": 81,                     # 1. Lig
    "soccer_xb_belgium_2": 6142,                  # Challenger Pro League
    "soccer_xb_austria_2": 112,                   # 2. Liga
    "soccer_xb_switzerland_2": 96,                # Challenge League
    "soccer_xb_denmark_2": 120,                   # 1st Division
    "soccer_xb_norway_2": 132,                    # OBOS-ligaen
    "soccer_xb_poland_2": 154,                    # I Liga
    "soccer_xb_northern_ireland": 5511,           # Premiership
    # Amériques
    "soccer_xb_colombia": 620,                    # Liga BetPlay
    "soccer_xb_peru": 583,                        # Liga 1
    "soccer_xb_ecuador": 5062,                    # Liga Pro
    "soccer_xb_uruguay": 617,                     # Uruguayan Championship
    "soccer_xb_paraguay": 621,                    # Copa de Primera
    "soccer_xb_bolivia": 5055,                    # División Profesional
    "soccer_xb_venezuela": 634,                   # Liga FUTVE
    "soccer_xb_costa_rica": 5056,                 # liga FPD
    "soccer_xb_guatemala": 6211,                  # Liga Nacional
    "soccer_xb_usl": 5663,                        # USL Championship
    "soccer_xb_argentina_2": 419,                 # Primera Nacional
    "soccer_xb_mexico_2": 5514,                   # Liga de Expansión MX
    "soccer_xb_brazil_c": 5518,                   # Serie C
    "soccer_xb_canada": 5620,                     # Canadian Premier League
    # Asie
    "soccer_xb_japan_2": 148,                     # J2 League
    "soccer_xb_korea_2": 5557,                    # K League 2
    "soccer_xb_qatar": 408,                       # Doha Bank Stars League
    "soccer_xb_iran": 561,                        # Persian Gulf Pro League (hasStats False sur tous ses matchs : listé rarement, jamais lu)
    "soccer_xb_indonesia": 5431,                  # Super League
    "soccer_xb_china_2": 5625,                    # League One
    # Afrique
    "africa_egypt_premier_league": 552, "africa_morocco_botola_pro": 557, "soccer_xb_algeria": 560,
}
FD_FULL = frozenset(footballdata.SPORT_TO_DIV) - frozenset(footballdata.SPORT_TO_NEW)      # statistiques complètes déjà chez football-data


# ---------- réglages ----------

def enabled():
    return bool(C.S365_ENABLED)


def in_hours(now=None):
    """Vrai si l'heure UTC est permise par S365_HOURS (« 0-6,22-23 », fenêtre qui passe minuit permise : « 22-5 ») ; vide = toujours."""
    spec = (C.S365_HOURS or "").strip()
    if not spec:
        return True
    h = (now or state.now()).hour
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        try:
            lo, hi = int(a), int(b) if b.strip() else int(a)
        except ValueError:
            continue
        if (lo <= h <= hi) if lo <= hi else (h >= lo or h <= hi):
            return True
    return False


def scope():
    """Clés de ligue suivies, dans l'ordre du catalogue : S365_LEAGUES (« all » = toutes celles de COMPETITIONS), sinon celles dont
    football-data n'a pas déjà les statistiques complètes."""
    asked = [k.lower() for k in C.S365_LEAGUES]
    if "all" in asked:
        keys = list(COMPETITIONS)
    elif asked:
        keys = [k for k in C.S365_LEAGUES if k in COMPETITIONS]
    else:
        keys = [k for k in COMPETITIONS if k not in FD_FULL]
    rank = {k: i for i, k in enumerate(C.LEAGUE_KEYS + C.XB_KEYS)}
    return sorted(dict.fromkeys(keys), key=lambda k: (rank.get(k, len(rank)), k))


# ---------- adresses ----------

def _url(path, **q):
    return f"{BASE}{path}?" + urlencode(list(PARAMS) + list(q.items()), safe="/")


def list_url(key):
    cid = COMPETITIONS.get(key)
    return _url("/games/results/", competitions=cid) if cid else None


def stats_url(gid):
    return _url("/game/stats/", games=int(gid))


def prev_url(path, cid):
    """Adresse absolue de la page plus ancienne annoncée par une liste (`paging.previousPage`), ou None si elle ne reste pas dans
    /web/games/ du même championnat en remontant le temps : un chemin lu dans une réponse n'est jamais suivi sans ce contrôle."""
    if not isinstance(path, str) or not path.startswith("/web/games/"):
        return None
    q = parse_qs(urlparse(path).query)
    if q.get("competitions") != [str(cid)] or q.get("direction") != ["-1"]:
        return None
    return f"https://{HOST}{path}"


# ---------- lecture des réponses ----------

def _json(res):
    try:
        d = json.loads(res.content)
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def read_json(res, url=None):
    """Objet JSON d'une réponse de http.fetch ; sinon None ET l'adresse (`url`, sinon res.url) est retirée du cache : une réponse 200 qui n'est pas du
    JSON (page d'erreur, de vérification) ne doit pas être resservie pendant toute la durée du cache (12 h pour les listes du live)."""
    data = _json(res) if res is not None and res.ok else None
    if data is None and res is not None and res.ok and not getattr(res, "stale", False):
        url = url or getattr(res, "url", None)
        if url:
            http.invalidate(url)
    return data


def _num(v):
    """Entier >= 0 d'un compte (« 15 » ; jamais « 53% » ni « 3/8 (38%) »), sinon None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return int(v) if v == int(v) and v >= 0 else None
        except (ValueError, OverflowError):          # nan, inf
            return None
    s = str(v or "").strip()
    return int(s) if s.isdigit() else None


def _utc_day(iso):
    """Jour UTC (AAAA-MM-JJ) d'une heure ISO avec décalage, sinon None."""
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date().isoformat()


def parse_results(payload, key):
    """Réponse de /games/results/ (première page ou page plus ancienne) du championnat `key` -> (matchs terminés, adresse de la page
    plus ancienne ou None). Un match est gardé s'il est du football (sportId 1), du bon championnat, terminé NORMALEMENT (« Ended » : ni
    reporté, ni suspendu, ni attribué, ni prolongation) avec un score et deux équipes distinctes. Chaque match : {id, d: jour UTC, hid, hn,
    hl, aid, an, al, st: hasStats}."""
    payload = payload if isinstance(payload, dict) else {}
    cid = COMPETITIONS.get(key)
    games = []
    for g in payload.get("games") if isinstance(payload.get("games"), list) else []:
        if not isinstance(g, dict) or g.get("sportId") != SPORT_FOOTBALL or g.get("competitionId") != cid or not g.get("id"):
            continue
        if g.get("statusGroup") != 4 or str(g.get("statusText") or "").strip().lower() != "ended":
            continue
        h, a = g.get("homeCompetitor"), g.get("awayCompetitor")
        if not isinstance(h, dict) or not isinstance(a, dict) or not h.get("id") or not a.get("id") or h["id"] == a["id"]:
            continue
        day = _utc_day(g.get("startTime"))
        if not day or _num(h.get("score")) is None or _num(a.get("score")) is None or not h.get("name") or not a.get("name"):
            continue
        games.append({"id": str(g["id"]), "d": day, "hid": str(h["id"]), "hn": str(h["name"]), "hl": str(h.get("longName") or ""),
                      "aid": str(a["id"]), "an": str(a["name"]), "al": str(a.get("longName") or ""), "st": g.get("hasStats") is True})
    paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
    return games, prev_url(paging.get("previousPage"), cid)


def parse_stats(payload, day=None):
    """Réponse de /game/stats/ d'un match terminé -> {id, d, hid, aid, s: {stat: [domicile, extérieur]}} ; None si le match n'est pas
    terminé, si ses équipes ne sont pas celles des statistiques, ou s'il n'a pas au moins MIN_STATS statistiques de jeu (cartons et
    corners seuls : zéros d'un match non suivi). `day` : jour UTC donné par la liste (le décalage horaire de /game/stats/ est faux).
    cards = jaunes + rouges (comme fmmatch quand il n'a pas la liste des cartons)."""
    payload = payload if isinstance(payload, dict) else {}
    games = payload.get("games")
    g = games[0] if isinstance(games, list) and games and isinstance(games[0], dict) else None
    if not g or g.get("sportId") != SPORT_FOOTBALL or str(g.get("statusText") or "").strip().lower() != "ended":
        return None
    h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
    hid, aid = h.get("id"), a.get("id")
    hg, ag = _num(h.get("score")), _num(a.get("score"))
    if not hid or not aid or hid == aid or hg is None or ag is None:
        return None
    raw = {}
    for s in payload.get("statistics") if isinstance(payload.get("statistics"), list) else []:
        if not isinstance(s, dict):
            continue
        k, cid, v = STAT_NAMES.get(str(s.get("name") or "").strip().lower()), s.get("competitorId"), _num(s.get("value"))
        if k and cid in (hid, aid) and v is not None:
            raw.setdefault(k, {}).setdefault(cid, v)
    st = {"goals": [hg, ag]}
    for k in COUNT_KEYS:
        pair = raw.get(k)
        if pair and hid in pair and aid in pair:
            p = [pair[hid], pair[aid]]
            if not (k in NEVER_ZERO and p == [0, 0]):
                st[k] = p
    yc, rc = raw.get("yc") or {}, raw.get("rc") or {}
    if hid in yc and aid in yc:
        st["cards"] = [yc[hid] + rc.get(hid, 0), yc[aid] + rc.get(aid, 0)]
        st["yellow"] = [yc[hid], yc[aid]]                          # jaunes seuls : le pari de 1xBet
    if sum(1 for k in COUNT_KEYS if k in st) < MIN_STATS:
        return None
    if not any(sum(st[k]) > 0 for k in COUNT_KEYS if k in st):       # que des zéros (match non suivi) : aucune moyenne ni règlement « moins de corners » ne s'en sert
        return None
    return {"id": str(g.get("id") or ""), "d": day or str(g.get("startTime") or "")[:10], "hid": str(hid), "aid": str(aid), "s": st}


def verify_catalogue(payload):
    """Contrôle COMPETITIONS contre le catalogue /search/ de 365scores -> liste de problèmes (vide : tout est bon) : id absent, sport autre que
    le football (38 = basket), ou id partagé par deux clés de ligue."""
    comps = payload.get("competitions") if isinstance(payload, dict) else None
    comps = {c.get("id"): c for c in comps if isinstance(c, dict)} if isinstance(comps, list) else {}
    out, seen = [], {}
    for key, cid in COMPETITIONS.items():
        c = comps.get(cid)
        if c is None:
            out.append(f"{key} : championnat {cid} absent du catalogue")
        elif c.get("sportId") != SPORT_FOOTBALL:
            out.append(f"{key} : championnat {cid} « {c.get('name')} » n'est pas du football (sportId {c.get('sportId')})")
        if cid in seen:
            out.append(f"{key} : id {cid} déjà pris par {seen[cid]}")
        seen.setdefault(cid, key)
    return out


# ---------- stockage ----------

_d2i, _i2d = fmmatch._d2i, fmmatch._i2d


def _today():
    return state.now().date()


def load():
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) else {}
    for k in ("teams", "seen", "pending", "lists", "recent"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    if not isinstance(d.get("req"), dict):
        d["req"] = {"day": "", "n": 0}
    return d


def save(d):
    """Écrit le fichier en oubliant les équipes non mises à jour depuis S365_KEEP_DAYS jours, les matchs « vus » trop vieux pour revenir (SEEN_DAYS ;
    la garantie « un match lu n'est jamais redemandé » vaut sur toute la fenêtre de remplissage, S365_WINDOW_DAYS), les
    matchs en attente plus vieux que la fenêtre (ou en trop : les plus anciens d'abord) et les listes d'un championnat qui n'est plus suivi."""
    today = _today()
    cut = str(today - timedelta(days=C.S365_KEEP_DAYS))
    d["teams"] = {k: v for k, v in d["teams"].items() if v.get("d", "0") >= cut and v.get("m")}
    floor = _d2i(str(today - timedelta(days=max(SEEN_DAYS, C.S365_WINDOW_DAYS + 14))))
    seen = {k: v for k, v in d["seen"].items() if v >= floor}
    if len(seen) > MAX_SEEN:                                   # un match plus vieux que la fenêtre n'est de toute façon plus demandé
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SEEN])
    d["seen"] = seen
    wcut = str(today - timedelta(days=C.S365_WINDOW_DAYS))
    pend = {k: v for k, v in d["pending"].items() if v.get("d", "0") >= wcut and k not in d["seen"]}
    if len(pend) > MAX_PENDING:
        pend = dict(sorted(pend.items(), key=lambda kv: kv[1]["d"], reverse=True)[:MAX_PENDING])
    d["pending"] = pend
    d["lists"] = {k: v for k, v in d["lists"].items() if k in COMPETITIONS}
    d["lfail"] = {k: v for k, v in (d.get("lfail") or {}).items() if k in COMPETITIONS and state.clock() - v < 2 * GAME_FAIL_GAP_H * HOUR}
    rcut = str(today - timedelta(days=C.S365_RECENT_DAYS))
    d["recent"] = {k: v for k, v in d["recent"].items() if v.get("d", "0") >= rcut}
    d["v"] = 1
    store.save(FILE, d, compact=True)


def n_matches(t):
    return len((t or {}).get("m") or [])


def record(d, m, key):
    """Ajoute le match lu `m` ({id, d, hid, hn, hl, aid, an, al, s}) aux deux équipes. -> True s'il est neuf."""
    if not m or not m.get("id") or m["id"] in d["seen"]:
        return False
    day = _d2i(m["d"] or str(_today()))
    d["seen"][m["id"]] = day
    d["pending"].pop(m["id"], None)
    d["recent"][m["id"]] = {"d": m["d"], "sk": key, "h": m["hn"], "a": m["an"], "s": m["s"]}
    for tid, name, long_, home in ((m["hid"], m["hn"], m.get("hl"), 1), (m["aid"], m["an"], m.get("al"), 0)):
        row = [day, home]
        for k in KEYS:
            pair = m["s"].get(k)
            row += (pair if home else pair[::-1]) if pair else [None, None]
        t = d["teams"].setdefault(tid, {"m": []})
        t.update(nm=name, d=str(_today()), sk=list(dict.fromkeys([x for x in t.get("sk", []) if x != key] + [key]))[-4:])
        if long_ and long_ != name:
            t["ln"] = long_
        t["m"] = sorted(t["m"] + [row], key=lambda r: r[R_DAY])[-C.S365_KEEP_MATCHES:]
    return True


def note_list(d, key, games, prev, mode, nt, now):
    """Range une liste lue (mode « first » : première page, « prev » : page plus ancienne) : état du championnat et matchs à lire."""
    L = d["lists"].setdefault(key, {})
    d.get("lfail", {}).pop(key, None)
    days = sorted(g["d"] for g in games)
    if mode == "first":
        L["at"] = now
        L["n"], L["ns"], L["nt"] = len(games), sum(1 for g in games if g["st"]), nt
        if "prev" not in L:                                    # début de la chaîne de pages plus anciennes
            L["prev"] = prev if days else None
    else:                                                      # page plus ancienne : la chaîne avance, ou s'arrête sur une page vide
        L["prev"] = prev if days else None
    if days:
        L["new"] = max(L.get("new") or "", days[-1])
        L["old"] = min(L.get("old") or days[0], days[0])
    for g in games:
        if g["st"] and g["id"] not in d["seen"]:
            p = {"k": key, "d": g["d"], "h": g["hid"], "hn": g["hn"], "a": g["aid"], "an": g["an"]}
            if g["hl"] and g["hl"] != g["hn"]:
                p["hl"] = g["hl"]
            if g["al"] and g["al"] != g["an"]:
                p["al"] = g["al"]
            d["pending"].setdefault(g["id"], p)


def _apply(d, op, today):
    """Rejoue une opération du passage sur le document `d` -> nombre de matchs neufs enregistrés."""
    if op[0] == "list":
        note_list(d, *op[1:])
        return 0
    if op[0] == "lfail":                                  # liste de résultats en échec : réessayée après GAME_FAIL_GAP_H heures (jamais abandonnée : le championnat en a besoin)
        d.setdefault("lfail", {})[op[1]] = op[2]
        return 0
    if op[0] == "fail":                                   # lecture en échec : compteur durable ; GAME_FAIL_MAX échecs espacés = mis de côté (« vu »)
        _, gid, clock = op
        p = d["pending"].get(gid)
        if p is not None:
            p["f"], p["ft"] = p.get("f", 0) + 1, clock
            if p["f"] >= GAME_FAIL_MAX:
                d["seen"][gid] = _d2i(p["d"])
                d["pending"].pop(gid, None)
        return 0
    _, gid, rec = op
    p = d["pending"].get(gid)
    if rec is not None:
        return int(record(d, rec, rec["k"]))
    # match sans statistiques exploitables : relu quelques fois s'il date d'hier (les chiffres se publient parfois avec retard), sinon oublié
    if p and (today - date.fromisoformat(p["d"])).days <= 1 and p.get("r", 0) + 1 < RETRY_FRESH:
        p["r"] = p.get("r", 0) + 1
    else:
        d["seen"][gid] = _d2i(p["d"]) if p else _d2i(str(today))
        d["pending"].pop(gid, None)
    return 0


# ---------- profils ----------

def _views(rows):
    from . import footballdata as fd
    cell = lambda r, j: r[j] if j < len(r) else None          # noqa: E731 - ligne plus courte : colonne manquante = None
    recs = [{k: (cell(r, R_STATS + 2 * i), cell(r, R_STATS + 2 * i + 1)) for i, k in enumerate(KEYS)} for r in rows]
    n = C.APISTATS_LAST

    def view(rs):
        return fd._avg_view(rs, keys=KEYS, min_count=min(C.APISTATS_MIN_N, len(rs)))

    return {"all": view(recs[-n:]), "home": view([x for x, r in zip(recs, rows) if r[R_HOME]][-n:]),
            "away": view([x for x, r in zip(recs, rows) if not r[R_HOME]][-n:])}


def profiles():
    """{"leagues": {clé de ligue: {"teams": {nom: {id, sk, all/home/away: {n, throws_for...}}}}}, "updated"} : équipes avec au moins
    APISTATS_MIN_N matchs, rangées dans leur dernier championnat (deux clubs de même nom dans deux pays ne se mélangent pas). Un nom long
    (« Universitario de Deportes ») sert d'alias vers le MÊME profil, sans jamais prendre le nom court d'une autre équipe."""
    d = load()
    mine, updated = [], ""
    for tid, t in d["teams"].items():
        if n_matches(t) < C.APISTATS_MIN_N or not t.get("sk") or t["sk"][-1] not in COMPETITIONS or not t.get("nm"):
            continue
        p = {"id": tid, "nm": t["nm"], "sk": t["sk"], "last": _i2d(t["m"][-1][R_DAY]), "d": t.get("d")} | _views(t["m"])
        mine.append((t["sk"][-1], p, t))
        updated = max(updated, t.get("d", ""))
    leagues = {}
    for field in ("nm", "ln"):                             # d'abord tous les noms courts, puis les noms longs qui ne prennent la place de personne
        for key, p, t in mine:
            if t.get(field):
                leagues.setdefault(key, {"teams": {}})["teams"].setdefault(t[field], p)
    return {"leagues": leagues, "updated": updated}


def summary(d=None):
    d = d or load()
    ns = [n_matches(t) for t in d["teams"].values()]
    return {"tracked": len(ns), "usable": sum(1 for n in ns if n >= C.APISTATS_MIN_N), "good": sum(1 for n in ns if n >= C.APISTATS_GOOD_N),
            "total": len(d["seen"]), "pending": len(d["pending"])}


# ---------- choix des listes et des matchs à lire ----------

def _covered(d):
    """Ligues que FotMob (fm_matches.json) couvre déjà : au moins 90 % de leurs équipes ont APISTATS_GOOD_N matchs avec tirs ET corners ;
    365scores n'y apporterait rien (FotMob passe avant lui dans features) : listées rarement, aucun match lu."""
    try:
        fm = fmmatch.load()["teams"]
    except Exception:                                    # noqa: BLE001 - un indice, jamais bloquant
        return frozenset()
    j_sh, j_co = R_STATS + 2 * fmmatch.KEYS.index("shots"), R_STATS + 2 * fmmatch.KEYS.index("corners")
    good = {}
    for t in fm.values():
        ok = sum(1 for r in t.get("m") or [] if len(r) > j_co and r[j_sh] is not None and r[j_co] is not None)
        if ok >= C.APISTATS_GOOD_N:
            for k in t.get("sk") or ():
                good[k] = good.get(k, 0) + 1
    return frozenset(k for k, v in good.items() if (d["lists"].get(k) or {}).get("nt", 99) >= 6
                     and v >= 0.9 * (d["lists"].get(k) or {}).get("nt", 99))


def _no_stats(L):
    """Championnat dont la dernière liste (10 matchs terminés au moins) n'a AUCUN match avec statistiques (Iran)."""
    return L.get("n", 0) >= 10 and not L.get("ns")


def _interval(L, covered):
    base = C.S365_LIST_HOURS * HOUR
    if covered or _no_stats(L):                           # couvert par FotMob, ou sans statistiques (Iran) : on ne revient pas tous les jours
        return 7 * 24 * HOUR
    new = L.get("new")
    if new and (_today() - date.fromisoformat(new)).days > 14:       # saison finie / pause : une liste tous les trois jours suffit
        return 3 * base
    return base


def due_lists(d, keys, covered=(), failed=(), clock=None):
    """Listes à lire, dans l'ordre : [(clé, mode)]. D'abord les championnats jamais listés (catalogue), puis les pages plus anciennes qui
    manquent à la fenêtre de remplissage S365_WINDOW_DAYS, puis les listes à rafraîchir (la plus ancienne d'abord)."""
    clock = state.clock() if clock is None else clock
    cutoff = str(_today() - timedelta(days=C.S365_WINDOW_DAYS))
    out = []
    for i, key in enumerate(keys):
        if key in failed:
            continue
        L = d["lists"].get(key)
        ft = (d.get("lfail") or {}).get(key)
        if ft is not None and clock - ft < GAME_FAIL_GAP_H * HOUR:        # liste en échec il y a peu : on passe à la suivante
            continue
        if L is None:
            out.append(((0, 0, i), key, "first"))
        elif L.get("prev") and key not in covered and not _no_stats(L) and (L.get("old") or "") > cutoff:
            out.append(((1, 0, i), key, "prev"))
        elif clock - L.get("at", 0) >= _interval(L, key in covered):
            out.append(((2, -(clock - L.get("at", 0)), i), key, "first"))
    return [(k, m) for _, k, m in sorted(out)]


def targets(d, keys, covered=(), today=None, tried=(), clock=None):
    """Matchs en attente à lire, du plus utile au moins utile. Un match est lu si l'une de ses équipes a moins de S365_TARGET_N matchs ou s'il
    date de moins de S365_FRESH_DAYS jours. Ordre : équipes sans profil utilisable (moins de APISTATS_MIN_N matchs), puis moins de
    APISTATS_GOOD_N, puis le reste ; à égalité le plus récent. Les championnats alternent (jamais un seul qui épuise le budget).
    Un match déjà en échec n'est redemandé qu'après GAME_FAIL_GAP_H heures et passe après les autres : un match qui ne répond pas ne bloque plus la file.
    -> [id du match]."""
    today = today or _today()
    clock = state.clock() if clock is None else clock
    cutoff = str(today - timedelta(days=C.S365_WINDOW_DAYS))
    fresh = str(today - timedelta(days=C.S365_FRESH_DAYS))
    ks = set(keys)
    groups = {}
    for gid, p in d["pending"].items():
        if gid in d["seen"] or gid in tried or p["k"] not in ks or p["k"] in covered or p["d"] < cutoff:
            continue
        if p.get("ft") and clock - p["ft"] < GAME_FAIL_GAP_H * HOUR:
            continue
        low = min(n_matches(d["teams"].get(p["h"])), n_matches(d["teams"].get(p["a"])))
        if p["d"] < fresh and low >= C.S365_TARGET_N:
            continue
        bucket = 0 if low < C.APISTATS_MIN_N else (1 if low < C.APISTATS_GOOD_N else 2)
        groups.setdefault(p["k"], []).append((((p.get("f") or 0) > 0, bucket, -_d2i(p["d"]), low), gid))
    order = sorted(groups, key=lambda k: min(groups[k])[0])
    for k in order:
        groups[k].sort()
    out = []
    for i in range(max((len(v) for v in groups.values()), default=0)):
        out += [groups[k][i][1] for k in order if i < len(groups[k])]
    return out


# ---------- remplissage (passages horaires) ----------

def _locked():
    return fmmatch._locked()


def _wire():
    return http.WIRE.get(HOST, {}).get("requests", 0)


def _used_today(d):
    r = d["req"]
    return r.get("n", 0) if r.get("day") == str(_today()) else 0


def _fill(max_games=None, max_lists=None, seconds=None, backfill=False, max_requests=None, leagues=None):
    """Lit au plus `max_lists` listes de résultats et `max_games` statistiques de match (S365_LISTS_PER_RUN, S365_GAMES_PER_RUN) en `seconds`
    secondes (S365_SECONDS), dans le plafond de requêtes du jour (S365_DAILY). `backfill` : remplissage manuel (S365_BACKFILL_REQUESTS
    requêtes au plus, sans plafond du jour ni limite d'heures, S365_BACKFILL_SECONDS). `leagues` : restreint aux clés données. Ne lève
    jamais. -> {added, games, lists, requests, left, errors, tracked, usable, good, ...} ; `off` si désactivé ou hors des heures permises,
    `blocked` si 365scores est bloqué (aucune requête dans ce cas).
    Trois temps comme fmmatch.fill : choisir (document lu), TÉLÉCHARGER (3 s entre deux requêtes, SANS le verrou des données), puis
    rejouer les opérations sur le document rechargé et l'écrire (verrou repris quelques millisecondes)."""
    rep = {"added": 0, "games": 0, "lists": 0, "requests": 0, "left": 0, "errors": []}
    if not enabled():
        return rep | {"off": True}
    if not backfill and not (C.S365_GAMES_PER_RUN or C.S365_LISTS_PER_RUN):
        return rep | {"off": True}
    if not backfill and not in_hours():
        return rep | {"off": True, "why": "hors des heures permises (S365_HOURS)"}
    if kcache.OFFLINE:
        return rep | {"off": True, "why": "hors ligne"}
    if state.is_blocked(NAME):
        return rep | {"blocked": True, "errors": [state.health(NAME)["last_error"] or "source bloquée"]}
    if backfill:
        max_games = max_requests = C.S365_BACKFILL_REQUESTS if max_requests is None else max_requests
        max_lists = max_games if max_lists is None else max_lists
        seconds = C.S365_BACKFILL_SECONDS if seconds is None else seconds
    else:
        max_games = C.S365_GAMES_PER_RUN if max_games is None else max_games
        max_lists = C.S365_LISTS_PER_RUN if max_lists is None else max_lists
        seconds = C.S365_SECONDS if seconds is None else seconds
    deadline = time.monotonic() + seconds
    keys = [k for k in scope() if not leagues or k in leagues]
    ops, failed, tried, errors, failed_games, failed_lists = [], set(), set(), rep["errors"], [], []
    wire0, used0, today, stop = _wire(), 0, _today(), []
    try:
        with _locked():
            work = load()
        covered = _covered(work)
        used0 = _used_today(work)

        def room():
            """Vrai s'il reste du temps et des requêtes (passage, jour, mois) et que rien n'a arrêté le passage."""
            made = _wire() - wire0
            if stop or time.monotonic() > deadline or state.is_blocked(NAME):
                return False
            if month_left() is not None and month_left() <= 0:
                stop.append("plafond mensuel atteint")
                errors.append("plafond mensuel atteint : passage arrêté")
                return False
            if backfill:
                return max_requests is None or made < max_requests
            return used0 + made < C.S365_DAILY

        streak = [0]                                      # erreurs D'AFFILÉE : un 504 passager de temps en temps n'arrête pas un long rattrapage
        good = [0]                                        # lectures réussies du passage : preuve que le réseau marche (un échec « systémique » ne compte alors que pour son match)
        last_fail = [("", None)]                          # (erreur, code HTTP) de la dernière lecture en échec

        def get(url, ttl):
            res = http.fetch(url, NAME, ttl)
            if res.blocked:
                stop.append(res.error or "bloqué")
            elif (res.error or "").startswith("robots.txt"):
                stop.append(res.error)
            if res.ok and not res.stale:
                streak[0] = 0
                good[0] += 1
                return res
            errors.append(res.error or "réponse vide")
            last_fail[0] = (res.error, res.status)
            streak[0] += 1
            if streak[0] >= MAX_ERRORS:
                stop.append("trop d'erreurs")
            return None

        # 1. listes de résultats (premières pages, pages plus anciennes, rafraîchissements)
        while rep["lists"] < max_lists and room():
            due = due_lists(work, keys, covered, failed)
            if not due:
                break
            key, mode = due[0]
            url = list_url(key) if mode == "first" else work["lists"][key]["prev"]
            if not isinstance(url, str) or not url.startswith(f"https://{HOST}/web/"):      # adresse gardée par note_list, déjà contrôlée par prev_url
                work["lists"][key]["prev"] = None
                continue
            rep["lists"] += 1
            res = get(url, 6 * HOUR)
            payload = read_json(res) if res else None
            unreadable = bool(res) and (payload is None or not isinstance(payload.get("games"), list))
            if unreadable:
                errors.append(f"{key} : réponse illisible")
                http.invalidate(url)
                res = None
            if res is None:
                failed.add(key)
                failed_lists.append((key, "specific" if unreadable else http.failure_kind(*last_fail[0])))
                continue
            games, prev = parse_results(payload, key)
            teams = {t for g in games for t in (g["hid"], g["aid"])}
            op = ("list", key, games, prev, mode, len(teams), state.clock())
            ops.append(op)
            _apply(work, op, today)
        # 2. statistiques de match
        while rep["games"] < max_games and room():
            todo = targets(work, keys, covered, today, tried)
            if not todo:
                break
            gid = todo[0]
            tried.add(gid)
            p = work["pending"][gid]
            url = stats_url(gid)
            rep["games"] += 1
            res = get(url, http.FOREVER)
            if res is None:                               # HTTP en erreur, réponse vide : le match garde son compteur d'échecs (sinon il resterait en tête de file)
                failed_games.append((gid, http.failure_kind(*last_fail[0])))
                continue
            payload = _json(res)
            http.invalidate(url)                          # 7 à 20 Ko par match : le document garde le résultat, pas la réponse brute
            if payload is None or not isinstance(payload.get("statistics"), list):
                errors.append(f"match {gid} : réponse illisible")
                failed_games.append((gid, "specific"))
                continue
            got = parse_stats(payload, p["d"])
            rec = None
            if got and got["id"] == gid and got["hid"] == p["h"] and got["aid"] == p["a"]:
                rec = got | {"k": p["k"], "hn": p["hn"], "an": p["an"], "hl": p.get("hl"), "al": p.get("al")}
            op = ("game", gid, rec)
            ops.append(op)
            _apply(work, op, today)
        for key, kind in failed_lists:
            if kind == "specific" or (kind == "systemic" and good[0] > 0):
                ops.append(("lfail", key, state.clock()))
        for gid, kind in failed_games:                    # échec propre au match, ou panne générale seulement si le reste du passage a marché
            if kind == "specific" or (kind == "systemic" and good[0] > 0):
                ops.append(("fail", gid, state.clock()))
        rep["left"] = len(targets(work, keys, covered, today, tried))
    except store.LockTimeout:
        errors.append("verrou des données occupé : passage reporté")
    except Exception as e:                                # noqa: BLE001 - facultatif
        errors.append(f"erreur interne ({http.err(e)})")
    rep["requests"] = _wire() - wire0
    d = None
    if ops or rep["requests"]:                            # rien de lu, aucune requête : rien à écrire (et pas d'attente du verrou pour rien)
        try:
            with _locked():
                d = load()                                # rechargé : un autre processus a pu écrire entre-temps
                for op in ops:
                    rep["added"] += _apply(d, op, today)
                day = str(today)
                d["req"] = {"day": day, "n": _used_today(d) + rep["requests"]}
                save(d)
                state.record_run(NAME, len(d["seen"]), errors[:3])
        except store.LockTimeout:
            errors.append(f"verrou des données occupé : {len(ops)} lecture(s) perdue(s), refaites au prochain passage")
        except Exception as e:                            # noqa: BLE001
            errors.append(f"erreur interne ({http.err(e)})")
    if stop and stop[0] not in ("trop d'erreurs", "plafond mensuel atteint"):
        rep["blocked"] = state.is_blocked(NAME)
    return rep | summary(d or load())


USAGE = "s365_usage.json"      # données 365scores du mois passées par le proxy : {"month", "bytes", "requests", "days": {jour: octets}}


def usage(today=None):
    """Données 365scores du mois en cours passées par le proxy (remises à zéro au changement de mois)."""
    today = str(today or state.now().date())
    u = store.load(USAGE, {})
    return u if isinstance(u, dict) and u.get("month") == today[:7] else {"month": today[:7], "bytes": 0, "requests": 0, "days": {}}


def usage_line(u=None):
    u = u or usage()
    return f"365scores via proxy : {u['bytes'] / 1e6:.1f} Mo ce mois ({u['requests']} requêtes), plafond {C.S365_MONTH_MB:.0f} Mo"


_BUDGET = {}      # pendant _via_proxy : {"limit": octets du mois permis à ce bloc, "base": octets déjà comptés, "wire0": WIRE au départ}


def month_left():
    """Octets du mois encore permis au bloc _via_proxy en cours (plafond S365_MONTH_MB, moins la réserve du règlement hors règlement), compteur
    RELU pendant le passage ; None hors _via_proxy (aucun plafond à appliquer). <= 0 : arrêter."""
    if not _BUDGET:
        return None
    return _BUDGET["limit"] - _BUDGET["base"] - (http.WIRE.get(HOST, {"bytes": 0})["bytes"] - _BUDGET["wire0"])


@contextmanager
def _via_proxy(settle=False):
    """Les requêtes 365scores de ce bloc passent par le proxy Webshare de 1xBet (même fichier XBET_PROXY, même session collante que le dernier
    relevé 1xBet qui a répondu), dans le plafond C.S365_MONTH_MB, compté dans data/s365_usage.json. Jamais de changement de session pour
    contourner un refus : un 403/429 coupe la source 24 h (http.fetch). Donne None si tout est prêt, sinon la raison de ne rien lire (proxy
    introuvable : jamais de connexion directe, sauf S365_PROXY=0 ; plafond du mois atteint). Le mot de passe du proxy n'est jamais affiché."""
    if not C.S365_PROXY:
        yield None
        return
    from . import xbet
    url = xbet.proxy()
    saved, base = xbet.usage().get("session"), xbet.session_no(url)
    if url and isinstance(saved, int) and base is not None and base <= saved < base + xbet.MAX_SESSION_SPAN and xbet.with_session(url, saved):
        url = xbet.with_session(url, saved)
    if not url:
        yield "proxy introuvable (XBET_PROXY) : 365scores n'est pas lu en direct (S365_PROXY=0 pour la connexion directe)"
        return
    limit = C.S365_MONTH_MB * 1e6 * (1.0 if settle else 1.0 - SETTLE_RESERVE)      # le règlement des paris peut aller jusqu'au bout ; le reste garde sa réserve
    used = usage()["bytes"]
    if used >= limit:
        yield f"plafond mensuel atteint ({usage_line()}{'' if settle else ', dont ' + format(SETTLE_RESERVE, '.0%') + ' gardés pour le règlement'}) : rien lu"
        return
    start = dict(http.WIRE.get(HOST, {"bytes": 0, "requests": 0}))
    http.PROXIES[HOST] = url
    prev = dict(_BUDGET)
    _BUDGET.update(limit=limit, base=used, wire0=start["bytes"])
    try:
        yield None
    finally:
        _BUDGET.clear()
        _BUDGET.update(prev)
        http.PROXIES.pop(HOST, None)
        http._sessions.pop(HOST, None)
        w = http.WIRE.get(HOST, {"bytes": 0, "requests": 0})
        b, n = w["bytes"] - start["bytes"], w["requests"] - start["requests"]
        if n:
            day = str(state.now().date())
            cur = usage(day)                              # relu : un autre processus a pu compter entre-temps
            cur["bytes"], cur["requests"] = int(cur["bytes"] + b), cur["requests"] + n
            cur["days"][day] = int(cur["days"].get(day, 0) + b)
            cur["days"] = dict(sorted(cur["days"].items())[-40:])
            store.save(USAGE, cur)


def fill(*a, **kw):
    """_fill par le proxy (_via_proxy) : rien n'est lu si le proxy manque ou si le plafond du mois est atteint. Ne lève jamais."""
    if not enabled() or kcache.OFFLINE or state.is_blocked(NAME):
        return _fill(*a, **kw)                            # aucune requête possible : _fill le dit lui-même
    with _via_proxy() as why:
        if why:
            return {"added": 0, "games": 0, "lists": 0, "requests": 0, "left": 0, "errors": [why], "off": True, "why": why}
        if not kw.get("backfill"):
            return _fill(*a, **kw)
        # rattrapage ponctuel : écart réduit (S365_BACKFILL_DELAY) qui cède au Crawl-delay du robots.txt ; le passage horaire garde 3 s
        http.DELAY_REDUCED[HOST] = C.S365_BACKFILL_DELAY
        try:
            return _fill(*a, **kw)
        finally:
            http.DELAY_REDUCED.pop(HOST, None)


def match_stats(b):
    """_match_stats (règlement d'un pari) ; ses rares lectures passent par le proxy comme le remplissage, sinon seulement ce qui est déjà lu."""
    if not C.S365_PROXY or not enabled() or kcache.OFFLINE or state.is_blocked(NAME):
        return _match_stats(b)
    with _via_proxy(settle=True) as why:
        return _match_stats(b, net=not why)


def fill_safe(**kw):
    """fill() pour le passage horaire : imprime la ligne de rapport sur stderr, ne lève jamais."""
    try:
        rep = fill(**kw)
        print("[s365] " + report_line(rep) + (f" · {usage_line()}" if C.S365_PROXY else ""), file=sys.stderr)
        return rep
    except Exception as e:                                # noqa: BLE001 - facultatif
        print(f"[s365] ignoré : {http.err(e)}", file=sys.stderr)
        return None


def report_line(rep):
    if rep.get("off"):
        return f"365scores (stats de match) : désactivé{' — ' + rep['why'] if rep.get('why') else ''}"
    if rep.get("blocked"):
        return f"365scores (stats de match) : bloqué ({'; '.join(rep['errors'][:1]) or 'accès refusé'})"
    line = (f"365scores (stats de match : corners, tirs, fautes, touches...) : {rep['added']} match(s) ajouté(s) en {rep['requests']} requête(s) "
            f"({rep['lists']} liste(s), {rep['games']} match(s) lus), {rep.get('left', 0)} en attente · {rep.get('tracked', 0)} équipes suivies, "
            f"{rep.get('usable', 0)} utilisables, {rep.get('good', 0)} bonnes")
    return line + (f" · erreurs : {'; '.join(rep['errors'][:2])}" if rep["errors"] else "")


def health():
    return state.health(NAME)


# ---------- règlement ----------

_settle = {"n": 0}


def _gap(day, b):
    try:
        return abs((date.fromisoformat(str(day)[:10]) - date.fromisoformat(str(b["commence"])[:10])).days)
    except (ValueError, TypeError, KeyError):
        return 99


def _is(home, away, day, key, b):
    """Le match (équipes `home` / `away`, jour `day`, ligue `key`) est-il celui du pari `b` ? Même ligue (si le pari la donne), à un jour près,
    les deux équipes reconnues (names.team_sim >= 0,8 : « Atlas » n'est pas « Guadalajara », une équipe de jeunes n'est pas la première)."""
    if b.get("sport_key") and key != b["sport_key"]:
        return False
    return (_gap(day, b) <= 1 and names.team_sim(home or "", b.get("home") or "") >= 0.8
            and names.team_sim(away or "", b.get("away") or "") >= 0.8)


def _match_stats(b, net=True):
    """Totaux du match d'un pari (touches, hors-jeu, fautes, corners, tirs, tirs cadrés, cartons), au format de fmmatch.match_stats
    (« side » : par équipe) ; None si 365scores ne l'a pas. Lit d'abord les matchs déjà accumulés (S365_RECENT_DAYS jours) ; sinon, si le match est
    listé (file « pending » du passage horaire), lit ses statistiques maintenant : au plus S365_SETTLE_REQUESTS requêtes par exécution, jamais
    hors ligne, jamais source bloquée. Ne lève jamais pour une réponse illisible."""
    d = load()
    r = next((r for r in d["recent"].values() if _is(r.get("h"), r.get("a"), r.get("d"), r.get("sk"), b)), None)
    if (r is None and net and enabled() and not kcache.OFFLINE and not state.is_blocked(NAME) and _settle["n"] < C.S365_SETTLE_REQUESTS
            and (month_left() is None or month_left() > 0)):
        for gid, p in d["pending"].items():
            if not _is(p.get("hn"), p.get("an"), p.get("d"), p.get("k"), b):
                continue
            _settle["n"] += 1
            url = stats_url(gid)
            res = http.fetch(url, NAME, http.FOREVER)
            http.invalidate(url)
            payload = read_json(res) if res.ok and not res.stale else None
            got = parse_stats(payload, p["d"]) if payload else None
            if got and got["id"] == gid and got["hid"] == p["h"] and got["aid"] == p["a"]:
                record(d, got | {"k": p["k"], "hn": p["hn"], "an": p["an"], "hl": p.get("hl"), "al": p.get("al")}, p["k"])
                save(d)
                r = d["recent"].get(gid)
            break
    if not r:
        return None
    s = r["s"]
    ks = [k for k in s if k != "goals"]
    out = {k: s[k][0] + s[k][1] for k in ks}
    out["side"] = {side: {k: s[k][i] for k in ks} for i, side in ((0, "home"), (1, "away"))}
    out["players"] = {}
    return out


# ---------- ligne de commande ----------

def _cli(argv):
    os.environ[fmmatch.OWN_LOCK_ENV] = "1"               # télécharger SANS le verrou des données, ne le prendre que pour fusionner
    cmd = argv[0] if argv else "status"
    opts = dict(a.lstrip("-").split("=", 1) for a in argv[1:] if "=" in a)
    if cmd in ("fill", "backfill"):
        kw = {"backfill": cmd == "backfill"}
        if opts.get("requests"):
            kw["max_requests"] = int(opts["requests"])
        if opts.get("seconds"):
            kw["seconds"] = int(opts["seconds"])
        if opts.get("leagues"):
            kw["leagues"] = opts["leagues"].split(",")
        rep = fill(**kw)
        print(report_line(rep))
        return 0 if not rep["errors"] else 1
    if cmd == "verify":                                  # une requête (catalogue complet, ~16 Mo décompressé) : à lancer rarement
        res = http.fetch(_url("/search/"), NAME, http.FOREVER)
        http.invalidate(_url("/search/"))
        payload = _json(res) if res.ok else None
        if payload is None:
            print("catalogue illisible :", res.error)
            return 1
        probs = verify_catalogue(payload)
        print("\n".join(probs) if probs else f"{len(COMPETITIONS)} championnats vérifiés")
        return 1 if probs else 0
    if cmd == "status":
        s = summary()
        print(f"{s['tracked']} équipes, {s['usable']} utilisables, {s['good']} bonnes, {s['total']} matchs lus, {s['pending']} en attente")
        return 0
    print("usage : python -m kelly.sources.s365 [status|fill|backfill|verify] [--requests=N] [--seconds=N] [--leagues=clé1,clé2]")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
