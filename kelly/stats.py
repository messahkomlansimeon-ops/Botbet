"""Statistiques réelles (API-Football v3, api-sports.io). Optionnel : sans clé
STATS_API_KEY, tout renvoie None et le bot fonctionne avec les cotes seules.

Sert à deux choses :
1. régler les paris corners / cartons / stats joueurs (résultats réels) ;
2. estimer les lignes corners/cartons avant le match (moyennes récentes des
   deux équipes) pour repérer les lignes mal placées par les bookmakers : en repli
   seulement, les sources gratuites (kelly/sources, voir enrich) passent d'abord.

Plan gratuit : 100 appels/jour. Garde-fous : plafond par exécution
(STATS_BUDGET), plafond par jour UTC (STATS_DAILY_CAP, défaut 90, compteur dans
data/stats_quota.json), en-tête x-ratelimit-requests-remaining, et arrêt net
dès que l'API renvoie une erreur (HTTP 200 avec `errors` non vide).
"""
import difflib
import math
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from . import catalogue, store

BASE = "https://v3.football.api-sports.io"
BUDGET = int(os.environ.get("STATS_BUDGET", 60))     # appels max par exécution
LAST_N = 6                                           # matchs récents utilisés
MIN_MATCHES = 3                                      # sous ce seuil : pas de moyenne
DONE = ("FT", "AET", "PEN")                          # matchs terminés
_calls = 0
_halted = False                                      # erreur API : plus aucun appel


def enabled():
    return bool(os.environ.get("STATS_API_KEY"))


def _warn(msg):
    print(f"stats : {msg}", file=sys.stderr)


def _halt(msg):
    global _halted
    if not _halted:
        _warn(f"{msg} Arrêt des appels API-Football pour cette exécution.")
    _halted = True


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _cap():
    try:
        return int(os.environ.get("STATS_DAILY_CAP", 90))
    except ValueError:
        return 90


def _quota():
    q = store.load("stats_quota.json", {})
    if not isinstance(q, dict) or q.get("day") != _today():
        q = {"day": _today(), "n": 0}
    return q


def get(path, **params):
    """Appel API-Football. Renvoie `response` (liste, éventuellement vide) ou
    None (désactivé, quota atteint, erreur). Une erreur API stoppe tout."""
    global _calls
    if not enabled() or _halted or _calls >= BUDGET:
        return None
    q = _quota()
    if q["n"] >= _cap() or q.get("remaining") == 0:
        _halt(f"plafond quotidien atteint ({q['n']}/{_cap()} appels aujourd'hui).")
        return None
    _calls += 1
    q["n"] += 1
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=30,
                         headers={"x-apisports-key": os.environ["STATS_API_KEY"]})
    except requests.RequestException as e:
        store.save("stats_quota.json", q)
        _warn(f"réseau indisponible ({type(e).__name__}).")
        return None
    h = getattr(r, "headers", None) or {}
    try:                                   # quota réel annoncé par l'API (journalier)
        rem, lim = int(h["x-ratelimit-requests-remaining"]), h.get("x-ratelimit-requests-limit")
        q["remaining"] = rem
        if lim is not None:
            q["n"] = max(q["n"], int(lim) - rem)
    except (KeyError, TypeError, ValueError):
        q.pop("remaining", None)
    store.save("stats_quota.json", q)
    if r.status_code in (401, 403, 429):
        _halt(f"HTTP {r.status_code} (clé invalide, accès refusé ou limite atteinte).")
        return None
    if r.status_code >= 400:
        _warn(f"HTTP {r.status_code} sur {path}.")
        return None
    try:
        data = r.json()
    except ValueError:
        _warn(f"réponse illisible sur {path}.")
        return None
    errors = data.get("errors")                 # liste ou dict, vide si tout va bien
    if errors:
        _halt(f"erreur API sur {path} ({errors}) : clé, quota ou saison non incluse dans le plan.")
        return None
    if q.get("remaining") == 0:
        _halt("quota quotidien épuisé.")
    return data.get("response") or []


# --- Noms d'équipes -------------------------------------------------------

_STOP = {"fc", "afc", "cf", "sc", "ac", "as", "de", "the", "club", "ssc", "fk", "sk", "cd", "ud",
         "rc", "calcio", "sv", "vfb", "vfl", "tsg", "1", "cp", "ca", "of", "and", "e", "y", "1899"}
# Écarts fréquents Odds API / API-Football (clés et valeurs déjà normalisées).
_ALIAS = {
    "inter milan": "inter", "internazionale": "inter", "internazionale milano": "inter",
    "bayern munich": "bayern munchen", "bayern": "bayern munchen",
    "paris saint germain": "psg", "paris sg": "psg", "paris st germain": "psg",
    "man united": "manchester united", "man utd": "manchester united",
    "man city": "manchester city", "wolves": "wolverhampton wanderers",
    "tottenham hotspur": "tottenham", "spurs": "tottenham",
    "brighton and hove albion": "brighton", "brighton hove albion": "brighton",
    "nottm forest": "nottingham forest", "athletic bilbao": "athletic", "athletic club": "athletic",
    "atletico madrid": "atletico", "sporting lisbon": "sporting", "sporting lisboa": "sporting",
    "rb leipzig": "leipzig", "rasenballsport leipzig": "leipzig", "bayer leverkusen": "leverkusen",
    "bayer 04 leverkusen": "leverkusen", "borussia monchengladbach": "monchengladbach",
    "borussia m gladbach": "monchengladbach", "gladbach": "monchengladbach",
    "borussia dortmund": "dortmund", "eintracht frankfurt": "frankfurt",
    "hellas verona": "verona", "west ham united": "west ham", "newcastle united": "newcastle",
    "leeds united": "leeds", "leicester city": "leicester", "norwich city": "norwich",
    "olympique marseille": "marseille", "olympique lyonnais": "lyon", "olympique lyon": "lyon",
    "stade rennais": "rennes", "ac milan": "milan", "as roma": "roma", "napoli": "napoli",
    "celta vigo": "celta", "real betis": "betis", "red star belgrade": "crvena zvezda",
    "psv eindhoven": "psv", "sl benfica": "benfica", "fc porto": "porto",
}
_MARK = re.compile(r"^(w|women|fem|ii|iii|b|u\d\d|youth|reserves|res)$")


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and ")).split())


def _team_key(name):
    """(nom canonique, marqueurs femmes/jeunes/réserve) d'une équipe."""
    n = _norm(name)
    n = _ALIAS.get(n, n)
    toks = n.split()
    marks = frozenset(t for t in toks if _MARK.match(t))
    kept = " ".join(t for t in toks if t not in _STOP and t not in marks)
    return _ALIAS.get(kept, kept), marks


def team_sim(a, b):
    """Similarité 0..1 entre deux noms d'équipes (0 si l'un est féminin/jeunes et pas l'autre)."""
    (na, ma), (nb, mb) = _team_key(a), _team_key(b)
    if ma != mb or not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if ta <= tb or tb <= ta:                    # « Tottenham » ~ « Tottenham Hotspur »
        return 0.88
    return difflib.SequenceMatcher(None, na, nb).ratio()


def similar(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _ts(kickoff):
    """Timestamp UTC d'un horaire ISO / datetime (None si illisible)."""
    try:
        if isinstance(kickoff, datetime):
            d = kickoff
        else:
            d = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


PENDING = ("TBD", "NS", "1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE")   # pas (encore) terminés
_refreshed = set()                                   # jours re-téléchargés pendant cette exécution


# Championnats africains (The Odds API n'en couvre presque aucun) : repérés dans la liste du jour d'API-Football.
AFRICAN = {"algeria", "egypt", "morocco", "tunisia", "south africa", "nigeria", "ghana", "ivory coast", "senegal",
           "cameroon", "kenya", "tanzania", "uganda", "zambia", "zimbabwe", "mali", "burkina faso", "dr congo", "congo dr",
           "congo", "ethiopia", "angola", "libya", "sudan", "benin", "togo", "guinea", "gabon", "rwanda", "mozambique",
           "botswana", "namibia", "malawi", "madagascar", "mauritania", "gambia", "sierra leone", "liberia", "niger",
           "burundi", "cape verde", "equatorial guinea", "djibouti", "eswatini", "lesotho", "somalia"}
_NOT_SENIOR = re.compile(r"women|femin|\bu\d\d\b|youth|reserve|junior", re.I)


def african_league(f):
    """« Ligue (Pays) » d'un match africain masculin senior (championnat national ou compétition CAF), sinon None."""
    lg = f.get("league") or {}
    name, country = lg.get("name") or "", _norm(lg.get("country") or "")
    if not name or _NOT_SENIOR.search(name):
        return None
    if country in AFRICAN:
        return f"{name} ({lg.get('country')})"
    if country == "world" and re.search(r"\bcaf\b|africa", name, re.I):
        return name
    return None


def _entry(f):
    """Match du jour allégé : ids, statut et buts (temps réglementaire si fourni : les paris se
    règlent sur 90 minutes, pas sur la prolongation). « lg » : ligue, seulement pour l'Afrique."""
    ft = (f.get("score") or {}).get("fulltime") or {}
    goals = f.get("goals") or {}
    pick = lambda k: ft.get(k) if ft.get(k) is not None else goals.get(k)
    e = {"id": f["fixture"]["id"], "ts": f["fixture"].get("timestamp"),
         "status": (f["fixture"].get("status") or {}).get("short"),
         "home": f["teams"]["home"]["name"], "home_id": f["teams"]["home"]["id"],
         "away": f["teams"]["away"]["name"], "away_id": f["teams"]["away"]["id"],
         "gh": pick("home"), "ga": pick("away")}
    lg = african_league(f)
    if lg:
        e["lg"] = lg
    return e


def _stale(day, lst):
    """Liste en cache d'un jour PASSÉ qui contient un match non terminé (ou sans buts, ancien format) :
    le cache du matin (statut NS) bloquerait le règlement."""
    return str(day) < _today() and any(f.get("status") in PENDING or "gh" not in f for f in lst)


def _day_fixtures(day):
    """Tous les matchs du jour (UTC), en un seul appel par jour, mis en cache. Un jour passé dont le
    cache contient un match non terminé est re-téléchargé (1 requête, au plus une fois par exécution)."""
    cache = store.load("fixtures_day.json", {})
    old = cache.get(str(day))
    if old is not None and not (_stale(day, old) and str(day) not in _refreshed):
        return old
    _refreshed.add(str(day))
    raw = get("/fixtures", date=str(day), timezone="UTC")
    if not raw:                                 # erreur ou rien : on garde l'ancien cache
        return old or []
    lst = [_entry(f) for f in raw]
    keep = str((datetime.now(timezone.utc) - timedelta(days=10)).date())
    cache = {d: v for d, v in cache.items() if d >= keep}
    cache[str(day)] = lst
    store.save("fixtures_day.json", cache)
    return lst


def africa_matches(day):
    """Matchs africains du jour (entrées de _day_fixtures avec « lg »), triés par heure. Une seule requête pour tous
    les matchs du monde ce jour-là, la même que celle du règlement (mise en cache) ; [] sans clé API-Football."""
    if not enabled():
        return []
    return sorted((f for f in _day_fixtures(str(day)) if f.get("lg")), key=lambda f: f.get("ts") or 0)


def left_today():
    """Requêtes API-Football que Kelly peut encore faire aujourd'hui (plafond STATS_DAILY_CAP, appels déjà faits déduits)."""
    if not enabled() or _halted:
        return 0
    q = _quota()
    return 0 if q.get("remaining") == 0 else max(0, _cap() - q["n"])


def find_fixture(day, home, away, kickoff=None):
    """Fixture API-Football d'un match (noms approchés, horaire ±3 h si connu).
    `kickoff` (ISO) facultatif ; sinon un `day` plus long que AAAA-MM-JJ sert d'horaire."""
    if kickoff is None and len(str(day)) > 10:
        kickoff = day
    day = str(day)[:10]
    cache = store.load("fixtures.json", {})
    k = f"{day}|{home}|{away}"
    if k in cache:
        return cache[k]
    kts = _ts(kickoff) if kickoff else None
    best, best_key = None, None
    for f in _day_fixtures(day):
        sh, sa = team_sim(f["home"], home), team_sim(f["away"], away)
        if sh < 0.8 or sa < 0.8 or sh + sa < 1.75:         # seuil de confiance
            continue
        gap = abs(f["ts"] - kts) if kts is not None and f.get("ts") else 0
        if kts is not None and gap > 3 * 3600:
            continue
        key = (round(sh + sa, 2), -gap)
        if best_key is None or key > best_key:
            best, best_key = f, key
    if not best:
        return None
    cache[k] = {"id": best["id"], "home_id": best["home_id"], "away_id": best["away_id"],
                "status": best["status"]}
    store.save("fixtures.json", cache)
    return cache[k]


def fixture_result(day, home, away, kickoff=None):
    """Résultat d'un match (règlement gratuit des paris foot) : {id, status, home, away, finished}
    avec les buts (None tant que non terminé) ; None si le match n'est pas trouvé."""
    fx = find_fixture(day, home, away, kickoff)
    if not fx:
        return None
    f = next((f for f in _day_fixtures(str(day)[:10]) if f["id"] == fx["id"]), None)
    if not f:
        return None
    done = f["status"] in DONE and f.get("gh") is not None and f.get("ga") is not None
    return {"id": f["id"], "status": f["status"], "finished": done,
            "home": f["gh"] if done else None, "away": f["ga"] if done else None}


# --- Statistiques d'un match ----------------------------------------------

def _num(v):
    """Nombre d'une valeur API (None, « 45% », « 3 » ...). 0.0 si absent."""
    try:
        return float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def fixture_stats(fixture_id, players=True):
    """Totaux du match : corners, cartons, et stats par joueur. Mis en cache.
    players=False évite l'appel /fixtures/players (moyennes d'équipe seulement)."""
    cache = store.load("stats_cache.json", {})
    cur = cache.get(str(fixture_id))
    if cur and (not players or cur.get("players_done")):
        return cur
    if cur:
        out = cur
    else:
        teams = get("/fixtures/statistics", fixture=fixture_id)
        if not teams or len(teams) < 2:
            return None
        # Sans aucune ligne « Corner Kicks », la donnée manque : ne pas régler sur 0.
        if not any(s.get("type") == "Corner Kicks" for t in teams for s in t.get("statistics") or []):
            return None
        out = {"corners": 0, "cards": 0, "shots": 0, "fouls": 0, "teams": {}, "players": {},
               "players_done": False}
        for t in teams:
            st = {s["type"]: _num(s.get("value")) for s in t.get("statistics") or []}
            tv = {"corners": st.get("Corner Kicks", 0), "cards": st.get("Yellow Cards", 0) + st.get("Red Cards", 0),
                  "shots": st.get("Total Shots", 0), "fouls": st.get("Fouls", 0)}
            out["teams"][str(t["team"]["id"])] = tv
            for k2 in ("corners", "cards", "shots", "fouls"):
                out[k2] += tv[k2]
    if players:
        resp = get("/fixtures/players", fixture=fixture_id)
        if resp is not None:
            for team in resp:
                for p in team.get("players") or []:
                    s = (p.get("statistics") or [{}])[0]
                    out["players"][p["player"]["name"]] = {
                        "shots": _num((s.get("shots") or {}).get("total")),
                        "shots_on_target": _num((s.get("shots") or {}).get("on")),
                        "fouls": _num((s.get("fouls") or {}).get("committed")),
                    }
            out["players_done"] = True
    cache[str(fixture_id)] = out
    store.save("stats_cache.json", cache)
    return out


_SUFFIX = {"jr", "junior", "sr", "filho"}


def _player_key(name):
    """(initiale ou None, ensemble des mots du nom de famille)."""
    toks = [t for t in _norm(name).split() if t not in _SUFFIX]
    if not toks:
        return None, frozenset()
    if len(toks) == 1:
        return None, frozenset(toks)
    return toks[0][0], frozenset(toks[1:])


def player_stat(stats, name, field):
    """Stat d'un joueur par nom approché (« Kylian Mbappe », « K. Mbappé »).
    Nom de famille (sans accents) + initiale ; None si absent ou ambigu."""
    if not stats or not stats.get("players") or not name:
        return None
    qi, qs = _player_key(name)
    if not qs:
        return None
    hits = {}
    for pname, st in stats["players"].items():
        pi, ps = _player_key(pname)
        if not ps or not (qs <= ps or ps <= qs):
            continue
        if qi and pi and qi != pi:
            continue
        hits[pname] = st
    if len(hits) != 1:
        return None
    return next(iter(hits.values())).get(field)


# --- Moyennes d'équipe ----------------------------------------------------

def team_averages(team_id, day):
    """Moyennes corners/cartons pour et contre sur les derniers matchs.
    None si moins de MIN_MATCHES matchs avec stats (rien n'est mis en cache)."""
    cache = store.load("team_avg.json", {})
    k = f"{team_id}|{day}"
    if k in cache:
        return cache[k]
    # `last` seul (status filtré côté client : combinaison non garantie par l'API).
    fixtures = get("/fixtures", team=team_id, last=LAST_N + 4) or []
    fixtures = sorted((f for f in fixtures if f["fixture"]["status"]["short"] in DONE),
                      key=lambda f: f["fixture"].get("timestamp") or 0, reverse=True)[:LAST_N]
    tot = {"corners_for": 0, "corners_against": 0, "cards_for": 0, "cards_against": 0, "n": 0}
    for f in fixtures:
        st = fixture_stats(f["fixture"]["id"], players=False)
        if not st or str(team_id) not in st["teams"]:
            continue
        mine = st["teams"][str(team_id)]
        other = next((v for tid, v in st["teams"].items() if tid != str(team_id)), None)
        if not other:
            continue
        tot["corners_for"] += mine["corners"]; tot["corners_against"] += other["corners"]
        tot["cards_for"] += mine["cards"]; tot["cards_against"] += other["cards"]
        tot["n"] += 1
    if tot["n"] < MIN_MATCHES:
        return None
    avg = {k2: v / tot["n"] for k2, v in tot.items() if k2 != "n"}
    avg["n"] = tot["n"]
    cache[k] = avg
    store.save("team_avg.json", cache)
    return avg


def poisson_over(mean, point):
    """P(X > point) pour X ~ Poisson(mean)."""
    if mean <= 0:
        return 0.0 if point >= 0 else 1.0
    k_max = int(math.floor(point))
    cdf = sum(math.exp(-mean) * mean ** k / math.factorial(k) for k in range(k_max + 1))
    return 1 - cdf


def expected_totals(day, home, away, kickoff=None):
    """Corners et cartons attendus pour un match (None si indisponible)."""
    fx = find_fixture(day, home, away, kickoff)
    if not fx:
        return None
    h, a = team_averages(fx["home_id"], day), team_averages(fx["away_id"], day)
    if not h or not a:
        return None
    return {
        "corners": (h["corners_for"] + a["corners_against"]) / 2 + (a["corners_for"] + h["corners_against"]) / 2,
        "cards": (h["cards_for"] + a["cards_against"]) / 2 + (a["cards_for"] + h["cards_against"]) / 2,
        "n": min(h["n"], a["n"]),
    }


def enrich(picks, day, max_events=8):
    """Estimations d'avant-match, mélangées à la proba des books (60 % books, 40 % modèle ; buts : 80/20).
    1. Sources gratuites d'abord (kelly/sources : football-data, Understat, FBref) : corners, cartons, tirs et
       tirs cadrés des joueurs (par 90 min), buts attendus (totals, les 2 marquent) ; aucune requête.
    2. Repli API-Football pour les corners/cartons seulement si les sources n'ont rien (`max_events` matchs :
       économise le quota).
    Ajoute un signal lisible (« pourquoi ») et c["pm"] (voir kelly/prematch.py). Donnée absente = pari inchangé."""
    from . import prematch, sources            # import tardif : sources importe stats
    feats, api = {}, {}
    for c in picks:
        if c.get("group") != "soccer" or "market" not in c or catalogue.region(c.get("sport_key", "")) == "monde":
            continue                            # les sélections ne sont pas dans les profils de clubs
        a = None
        try:
            if c["id"] not in feats:
                feats[c["id"]] = sources.match_features(c["home"], c["away"], c["sport_key"])
            a = prematch.assess(c, feats[c["id"]], sources)
        except Exception as e:                  # noqa: BLE001 - une source cassée ne doit jamais bloquer
            _warn(f"sources ignorées pour {c.get('event', '?')} : {type(e).__name__}")
        src = "sources"
        if a is None and enabled() and c["market"] in (prematch.CORNERS, prematch.CARDS):
            if c["id"] not in api and len(api) < max_events:
                api[c["id"]] = expected_totals(day, c["home"], c["away"], c.get("commence"))
            a, src = prematch.api_assess(c, api.get(c["id"])), "api"
        if a is not None:
            prematch.apply(c, a, src)
    return picks
