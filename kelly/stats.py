"""Statistiques réelles (API-Football v3, api-sports.io). Optionnel : sans clé
STATS_API_KEY, tout renvoie None et le bot fonctionne avec les cotes seules.

Sert à deux choses :
1. régler les paris corners / cartons / stats joueurs (résultats réels) ;
2. estimer les lignes corners/cartons avant le match (moyennes récentes des
   deux équipes) pour repérer les lignes mal placées par les bookmakers : en repli
   seulement, les sources gratuites (kelly/sources, voir enrich) passent d'abord.

Statistiques de toute équipe du monde (tirs, tirs cadrés, corners, fautes, cartons) pour les profils de kelly/apistats.py :
fixture_stats les garde dans .cache/apistats/stats_cache.json (un match connu n'est jamais redemandé).

Plan gratuit : 100 appels/jour, et SEULEMENT hier, aujourd'hui et demain pour /fixtures?date= ; les paramètres « last » et « ids »
sont refusés. get() ne les envoie donc jamais (voir refused) : un historique ne se rattrape pas, il s'accumule jour après jour.
Garde-fous : plafond par exécution
(STATS_BUDGET), plafond par jour UTC (STATS_DAILY_CAP, défaut 90, compteur dans
data/stats_quota.json), en-tête x-ratelimit-requests-remaining, et arrêt net
dès que l'API renvoie une erreur (HTTP 200 avec `errors` non vide).

Fréquence : le forfait gratuit limite à ~10 requêtes par MINUTE (au-delà : HTTP 429, ou HTTP 200 avec `errors.rateLimit`). get() espace donc
deux appels d'au moins STATS_MIN_GAP secondes (6,2 s) ; un 429 malgré cela n'est pas un arrêt définitif : il est journalisé (corps compris),
get() attend STATS_RATE_WAIT secondes (60 s) puis réessaie UNE fois, dans la limite du délai du passage (set_deadline : apistats.fill y met
APISTATS_SECONDS) ; un second 429 arrête les appels pour ce passage seulement (le suivant repart).
"""
import difflib
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from . import apileagues, cache, catalogue, config as C, store

BASE = "https://v3.football.api-sports.io"
BUDGET = int(os.environ.get("STATS_BUDGET", 90))     # appels max par exécution (le plafond du jour reste STATS_DAILY_CAP)
MIN_MATCHES = C.APISTATS_MIN_N                       # sous ce seuil : pas de moyenne
DONE = ("FT", "AET", "PEN")                          # matchs terminés
MIN_GAP = C.STATS_MIN_GAP                            # secondes entre deux appels (forfait gratuit : 10 requêtes par minute)
RATE_WAIT = C.STATS_RATE_WAIT                        # attente avant l'unique nouvel essai après un HTTP 429
_clock, _sleep = time.monotonic, time.sleep          # remplaçables (tests : horloge simulée)
_calls = 0
_halted = False                                      # erreur API : plus aucun appel pour cette exécution (le passage suivant repart)
_last_call = None                                    # instant (_clock) du dernier appel parti
_deadline = None                                     # fin (_clock) du budget de temps du passage : get() n'attend jamais au-delà (set_deadline)
_deferred = False                                    # get() a renoncé à un appel faute de temps (ni erreur de l'API, ni arrêt)
RATE_ERROR = re.compile(r"rate ?limit|too many requests|per minute", re.I)       # limite de fréquence annoncée dans `errors` (HTTP 200)
_neg = {}                                            # (chemin, paramètres) -> (instant, résultat) : erreurs et réponses vides, brièvement
_refused = set()                                     # demandes écartées par refused() déjà signalées pendant cette exécution
FORBIDDEN_PARAMS = ("last", "ids")                   # refusés par le forfait gratuit d'API-Football : jamais envoyés


def enabled():
    return bool(os.environ.get("STATS_API_KEY"))


def halted():
    """Vrai si API-Football a renvoyé une erreur pendant cette exécution (plus aucun appel) : elle ne règle plus rien, les replis prennent le relais."""
    return _halted


def _warn(msg):
    print(f"stats : {msg}", file=sys.stderr)


def _halt(msg):
    global _halted
    if not _halted:
        _warn(f"{msg} Arrêt des appels API-Football pour cette exécution.")
    _halted = True


def set_deadline(instant=None):
    """Fin (instant _clock) du budget de temps du passage en cours, ou None : get() n'attend plus (écart entre deux appels, nouvel essai
    après un 429) au-delà ; il renvoie alors None sans rien arrêter (voir deferred)."""
    global _deadline, _deferred
    _deadline, _deferred = instant, False


def deferred():
    """Vrai si get() a renoncé à un appel faute de temps depuis set_deadline : le passage s'arrête (délai), l'API n'est pas en cause."""
    return _deferred


def _wait(seconds):
    """Dort `seconds` ; False, sans dormir, si cela dépasserait la fin du passage (set_deadline)."""
    global _deferred
    if seconds <= 0:
        return True
    if _deadline is not None and _clock() + seconds > _deadline:
        _deferred = True
        return False
    _sleep(seconds)
    return True


def _pace():
    """Respecte l'écart minimal MIN_GAP depuis le dernier appel. False si le temps du passage ne le permet pas."""
    return MIN_GAP <= 0 or _last_call is None or _wait(MIN_GAP - (_clock() - _last_call))


def _json(r):
    try:
        return r.json()
    except (ValueError, TypeError, AttributeError):
        return None


def _rate_limited(r, data):
    """Limite de fréquence : HTTP 429, ou HTTP 200 dont `errors` annonce la limite par minute (« rateLimit »)."""
    if r.status_code == 429:
        return True
    errors = data.get("errors") if isinstance(data, dict) else None
    return bool(errors) and bool(RATE_ERROR.search(str(errors)))


def _body(r, data):
    """Début du corps de la réponse pour le journal (texte brut sinon JSON)."""
    text = getattr(r, "text", None)
    if not isinstance(text, str) or not text:
        text = json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""
    return text.strip().replace("\n", " ")[:200]


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


PLAN_ERROR = re.compile(r"plan|season|saison|access|subscription|abonnement", re.I)


def refused(path, params):
    """Raison (texte) pour laquelle le forfait gratuit refuserait cette demande, sinon None. Une demande refusée n'est JAMAIS envoyée
    (elle coûterait une requête du quota et, pire, une erreur de plan arrêterait tous les appels de la journée) :
    - les paramètres « last » et « ids » ;
    - /fixtures?date= hors de la fenêtre hier..demain (C.STATS_DATE_WINDOW jours autour d'aujourd'hui)."""
    bad = [k for k in FORBIDDEN_PARAMS if k in params]
    if bad:
        return f"paramètre {bad[0]} non inclus dans le forfait gratuit"
    if path == "/fixtures" and "date" in params:
        try:
            gap = (datetime.fromisoformat(str(params["date"])[:10]).date() - datetime.fromisoformat(_today()).date()).days
        except ValueError:
            return "date illisible"
        if abs(gap) > C.STATS_DATE_WINDOW:
            return f"date {str(params['date'])[:10]} hors de la fenêtre du forfait gratuit (hier, aujourd'hui, demain)"
    return None


def get(path, **params):
    """Appel API-Football. Renvoie `response` (liste, éventuellement vide) ou
    None (désactivé, demande refusée d'avance par refused(), quota atteint, erreur, réponse de type inattendu, temps du passage épuisé).
    Une erreur API stoppe tout pour cette exécution ; une limite de fréquence (429) attend puis réessaie une fois (voir l'en-tête du
    module). L'erreur est gardée dans data/stats_quota.json (clé « err ») : --check et le journal du matin la relisent. Ne lève jamais."""
    global _calls, _last_call
    if not enabled() or _halted or _calls >= BUDGET or cache.OFFLINE:
        return None
    why = refused(path, params)
    if why:
        kind = (path, "date" if "date" in params else "paramètre")
        if kind not in _refused:                       # signalé une fois par exécution et par sorte de demande
            _refused.add(kind)
            _warn(f"{path} non demandé : {why}.")
        return None
    nk = (path, tuple(sorted(params.items())))
    prev = _neg.get(nk)
    if prev and time.monotonic() - prev[0] < C.STATS_NEG_MINUTES * 60:     # même échec ou même réponse vide : pas de nouvelle requête
        cache.hit("api-football", 1)
        return prev[1]
    q = _quota()
    if q["n"] >= _cap() or q.get("remaining") == 0:
        _halt(f"plafond quotidien atteint ({q['n']}/{_cap()} appels aujourd'hui).")
        return None
    for attempt in (0, 1):                         # 2e tour : l'unique nouvel essai après une limite de fréquence
        if not _pace():
            return None                            # plus le temps d'attendre l'écart minimal : le passage s'arrête (deferred)
        _calls += 1
        q["n"] += 1
        cache.miss("api-football", 1)
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=30,
                             headers={"x-apisports-key": os.environ["STATS_API_KEY"]})
        except requests.RequestException as e:
            _last_call = _clock()
            store.save("stats_quota.json", q)
            _warn(f"réseau indisponible ({type(e).__name__}).")
            _neg[nk] = (time.monotonic(), None)
            return None
        _last_call = _clock()
        h = getattr(r, "headers", None) or {}
        try:                                   # quota réel annoncé par l'API (journalier)
            rem, lim = int(h["x-ratelimit-requests-remaining"]), h.get("x-ratelimit-requests-limit")
            q["remaining"] = rem
            if lim is not None:
                q["n"] = max(q["n"], int(lim) - rem)
        except (KeyError, TypeError, ValueError):
            q.pop("remaining", None)
        store.save("stats_quota.json", q)
        data = _json(r)
        if r.status_code in (401, 403):
            _halt(f"HTTP {r.status_code} (clé invalide ou accès refusé).")
            return None
        if _rate_limited(r, data):
            _warn(f"limite de fréquence sur {path} (HTTP {r.status_code}) : {_body(r, data)}")
            if attempt == 0 and _wait(RATE_WAIT):
                _warn(f"attente de {RATE_WAIT:g} s puis un seul nouvel essai.")
                continue
            if not _deferred:
                _halt("limite de fréquence (10 requêtes par minute) toujours atteinte après l'attente.")
            return None
        break
    if r.status_code >= 400:
        _warn(f"HTTP {r.status_code} sur {path}.")
        _neg[nk] = (time.monotonic(), None)
        return None
    if not isinstance(data, dict):                 # JSON absent, liste, chaîne... (proxy, WAF, panne) : une erreur, jamais une exception
        _warn(f"réponse illisible ou de type inattendu sur {path} ({type(data).__name__ if data is not None else 'pas du JSON'}).")
        _neg[nk] = (time.monotonic(), None)
        return None
    errors = data.get("errors")                 # liste ou dict, vide si tout va bien
    if errors:
        plan = bool(PLAN_ERROR.search(str(errors)))
        if isinstance(errors, dict):
            text = "; ".join(f"{k}: {v}" for k, v in errors.items())
        elif isinstance(errors, (list, tuple)):
            text = "; ".join(map(str, errors))
        else:
            text = str(errors)
        q["err"] = {"path": path, "msg": text[:200], "plan": plan}
        store.save("stats_quota.json", q)
        _halt(f"erreur API sur {path} ({errors}) : clé, quota ou saison non incluse dans le plan.")
        return None
    if q.get("remaining") == 0:
        _halt("quota quotidien épuisé.")
    out = data.get("response") or []
    if not isinstance(out, list):                  # les éléments sont vérifiés par ceux qui les lisent (_day_fixtures, fixture_stats)
        _warn(f"réponse de type inattendu sur {path} (response : {type(out).__name__}).")
        _neg[nk] = (time.monotonic(), None)
        return None
    if not out:
        _neg[nk] = (time.monotonic(), out)         # réponse vide : pas redemandée pendant quelques minutes
    return out


# --- Noms d'équipes -------------------------------------------------------

_STOP = {"fc", "afc", "cf", "sc", "ac", "as", "de", "the", "club", "ssc", "fk", "sk", "cd", "ud",
         "rc", "calcio", "sv", "vfb", "vfl", "tsg", "1", "cp", "ca", "of", "and", "e", "y", "1899", "bk", "if", "ik",
         "bsc", "kv", "ksv", "krc", "kaa", "rsc", "kvc", "kfco"}      # préfixes de club (« BSC Young Boys », « Cercle Brugge KSV »)
# Écarts fréquents Odds API / API-Football (clés et valeurs déjà normalisées).
_ALIAS = {
    "inter milan": "inter", "internazionale": "inter", "internazionale milano": "inter",
    "bayern munich": "bayern munchen", "bayern": "bayern munchen",
    "paris saint germain": "psg", "paris sg": "psg", "paris st germain": "psg",
    "man united": "manchester united", "man utd": "manchester united",
    "man city": "manchester city", "wolves": "wolverhampton wanderers",
    "tottenham hotspur": "tottenham", "spurs": "tottenham",
    "brighton and hove albion": "brighton", "brighton hove albion": "brighton",
    "nottm forest": "nottingham forest", "athletic club": "athletic bilbao", "atletico de madrid": "atletico madrid",
    "sporting lisbon": "sporting", "sporting lisboa": "sporting",
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
    # Brésil : The Odds API et API-Football ne nomment pas les clubs pareil
    "atletico mg": "atletico mineiro", "athletico pr": "athletico paranaense", "atletico pr": "athletico paranaense",
    "atletico paranaense": "athletico paranaense",
    # abréviations de football-data (sans apostrophe : « Nott'm Forest » -> « nottm forest », « M'gladbach » -> « mgladbach »)
    "sp lisbon": "sporting", "qpr": "queens park rangers", "nott m forest": "nottingham forest", "mgladbach": "monchengladbach",
    "m gladbach": "monchengladbach", "sp gijon": "sporting gijon", "la coruna": "deportivo la coruna", "hertha": "hertha berlin",
    "for sittard": "fortuna sittard", "nijmegen": "nec nijmegen", "hibs": "hibernian",
    # Écarts constatés entre The Odds API, football-data, FotMob et API-Football (audit du 9 octobre 2026), tous le MÊME club :
    "stade lavallois": "laval", "al qadisiyah": "al qadsiah",
    "shanghai sipg": "shanghai port",                                   # rebaptisé Shanghai Port (2021) ; certains books gardent l'ancien nom
    "shenzhen xinpengcheng": "shenzhen peng city", "henan songshan longmen": "henan",
    "chongqing tongliang long": "chongqing tonglianglong",
    # JAMAIS d'alias entre Qingdao Hainiu, Qingdao West Coast et Qingdao Jonoon : trois clubs différents (tests/test_sources_fix.py).
}
# Sigles d'États brésiliens en fin de nom (« Nautico PE », « Atletico MG ») : retirés pour comparer, jamais pour confondre deux
# États (« Atletico MG » n'est pas « Atletico GO »).
_STATES = {"pe", "mg", "sp", "rj", "rs", "pr", "ba", "ce", "go", "pb", "rn", "df", "mt", "ms"}
# Mots trop courants pour porter seuls un rapprochement : « Atlético » n'est ni « Atlético Madrid » ni « Atlético-GO », « Independiente »
# n'est pas « Independiente Rivadavia ». Un nom dont les seuls mots communs avec l'autre sont dans cette liste ne lui ressemble pas.
GENERIC = {"atletico", "athletic", "athletico", "independiente", "inter", "sporting", "sport", "sports", "club", "america", "nacional",
           "united", "city", "town", "county", "real", "royal", "deportivo", "deportes", "deportiva", "union", "racing", "dinamo",
           "olympic", "rovers", "wanderers", "albion", "rangers", "juniors", "central", "universidad", "academico", "al", "el", "la",
           "los", "las", "st", "santa", "sao", "new", "north", "south", "east", "west", "athletics",
           "olympique", "internacional", "internazionale", "alianza", "libertad"}
_TOKEN_ALIAS = {"utd": "united", "dynamo": "dinamo", "jrs": "juniors", "rvs": "rovers",
                "sint": "st", "saint": "st", "sankt": "st", "san": "st"}      # « Sint Truiden » = « St Truiden », « Saint-Etienne » = « St Etienne »
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
                           "ß": "ss", "ı": "i", "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ħ": "h", "Ħ": "H"})   # lettres sans décomposition NFKD
# Marqueurs d'équipe féminine, de jeunes ou de réserve : « Jong Ajax » n'est pas « Ajax », « Real Madrid Castilla » pas « Real Madrid »,
# « Newcastle U-21 » pas « Newcastle ». Deux noms dont les marqueurs diffèrent ne se ressemblent pas (team_sim = 0).
_MARK = re.compile(r"^(w|women|fem|ii|iii|b|u\d\d|youth|reserves|res|jong|castilla|amateure|amateurs|primavera|next|gen)$")
_U_AGE = re.compile(r"\bu[ -]?(\d\d)\b")      # « U-21 », « U 21 », « U21 » -> « u21 »


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s).translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and ")).split())


def _state(name):
    """Sigle d'État brésilien en fin de nom (« Nautico PE » -> « pe »), sinon None."""
    toks = _norm(name).split()
    return toks[-1] if len(toks) >= 2 and toks[-1] in _STATES else None


def _initials(toks):
    """« Gimnasia L P » et « Gimnasia LP » sont le même nom : les initiales isolées qui se suivent sont collées."""
    out, run = [], []
    for t in toks + [""]:
        if len(t) == 1 and t.isalpha() and t not in _STOP and not _MARK.match(t):
            run.append(t)
            continue
        out += ["".join(run)] if len(run) > 1 else run
        run = []
        if t:
            out.append(t)
    return out


def _team_key(name):
    """(nom canonique, marqueurs femmes/jeunes/réserve) d'une équipe."""
    n = _norm(str(name).replace("'", "").replace("\u2019", "").replace("\u2018", "").replace("`", ""))     # « Newell's » = « Newells »
    n = _U_AGE.sub(r"u\1", n)
    n = _ALIAS.get(n, n)
    toks = _initials(n.split())
    if len(toks) >= 2 and toks[-1] in _STATES:
        toks = toks[:-1]
    marks = frozenset(t for t in toks if _MARK.match(t))
    kept = " ".join(_TOKEN_ALIAS.get(t, t) for t in toks if t not in _STOP and t not in marks)
    return _ALIAS.get(kept, kept), marks


def _fold_initials(na, nb):
    """« estudiantes la plata » ~ « estudiantes lp » : une suite de mots d'un nom dont les initiales forment un sigle de l'autre est
    remplacée par ce sigle (« L.P. » = « La Plata »). -> (na, nb) repliés. Le sigle doit être un mot ENTIER de l'autre nom, et le reste des deux
    noms doit partager au moins un mot distinctif (non générique) : « Atalanta BC » n'est ni « Bristol City » ni « Barracas Central »
    (leurs initiales font « bc », mais rien d'autre ne les rapproche) ; un sigle ne l'emporte donc jamais sur un mot distinctif commun."""
    def fold(x, y):                       # y dont les suites de mots épelant un sigle de x sont remplacées par ce sigle
        xs, ys = x.split(), y.split()
        for t in xs:
            if 2 <= len(t) <= 4 and t not in ys:
                for i in range(len(ys) - len(t) + 1):
                    run = ys[i:i + len(t)]
                    if "".join(w[0] for w in run) == t and all(len(w) > 1 for w in run):
                        anchor = (set(w for w in xs if w != t) & set(ys[:i] + ys[i + len(t):])) - GENERIC
                        if anchor:
                            ys = ys[:i] + [t] + ys[i + len(t):]
                        break
        return " ".join(ys)
    return fold(nb, na), fold(na, nb)


def distinct_overlap(a, b):
    """Nombre de mots non génériques communs aux noms canoniques de `a` et `b` (départage les égalités de best_team)."""
    (na, _), (nb, _) = _team_key(a), _team_key(b)
    na, nb = _fold_initials(na, nb)
    return len((set(na.split()) & set(nb.split())) - GENERIC)


def _same_word(x, y):
    """Deux mots différents désignent-ils le même ? Abréviation de 3-4 lettres d'un mot bien plus long (« ind » / « independiente », mais
    pas « como » / « comoros ») ou variante d'orthographe d'un long mot. Jamais pour un mot court : « gremio » / « remo », « nasr » /
    « masry », « uts » / « fus » sont des équipes différentes ; ni pour des mots longs seulement voisins (« america » / « almeria »)."""
    s, l = sorted((x, y), key=len)
    if 3 <= len(s) <= 4 and len(l) - len(s) >= 4 and l.startswith(s):
        return True
    if len(s) >= 6 and len(l) - len(s) <= 3 and l.startswith(s):       # « Hamburg » / « Hamburger », « Halmstad » / « Halmstads »
        return True
    return len(s) >= 5 and difflib.SequenceMatcher(None, x, y).ratio() >= 0.9


def team_sim(a, b):
    """Similarité 0..1 entre deux noms d'équipes (0 si l'un est féminin/jeunes et pas l'autre)."""
    (na, ma), (nb, mb) = _team_key(a), _team_key(b)
    if ma != mb or not na or not nb:
        return 0.0
    sa, sb = _state(a), _state(b)
    if sa and sb and sa != sb:
        return 0.0
    na, nb = _fold_initials(na, nb)
    ta, tb = set(na.split()), set(nb.split())
    if na == nb:
        return 1.0 if sa == sb else (0.88 if ta - GENERIC else 0.0)      # « Atletico GO » n'est pas « Atletico » tout court
    if ta <= tb or tb <= ta:                    # « Tottenham » ~ « Tottenham Hotspur », mais pas « Inter » ~ « Inter Miami »
        if (ta if len(ta) <= len(tb) else tb) - GENERIC:
            return 0.88
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    da, db = sorted(ta - tb), sorted(tb - ta)       # les mots qui distinguent chaque nom doivent se retrouver de l'autre côté
    for x in list(da):
        y = next((y for y in db if _same_word(x, y)), None)
        if y is not None:
            da.remove(x)
            db.remove(y)
    return max(ratio, 0.9) if not da and not db else min(ratio, 0.7)


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


def clean_referee(name):
    """Nom de l'arbitre d'API-Football (« Michael Oliver, England ») sans le pays : « Michael Oliver » ; None s'il est absent ou vide."""
    if not isinstance(name, str):
        return None
    name = " ".join(name.split(",")[0].split())
    return name or None


def _entry(f):
    """Match du jour allégé : ids, statut et buts (temps réglementaire si fourni : les paris se
    règlent sur 90 minutes, pas sur la prolongation). « lg » : ligue, seulement pour l'Afrique. « lk » / « ln » : clé du catalogue
    et nom de la ligue quand elle vaut des statistiques pour les profils (apileagues.classify, « africa » pour l'Afrique).
    « ref » : arbitre désigné (fixture.referee, sans le pays), seulement quand l'API le donne (clé absente = inconnu)."""
    ft = (f.get("score") or {}).get("fulltime") or {}
    goals = f.get("goals") or {}
    pick = lambda k: ft.get(k) if ft.get(k) is not None else goals.get(k)
    e = {"id": f["fixture"]["id"], "ts": f["fixture"].get("timestamp"),
         "status": (f["fixture"].get("status") or {}).get("short"),
         "home": f["teams"]["home"]["name"], "home_id": f["teams"]["home"]["id"],
         "away": f["teams"]["away"]["name"], "away_id": f["teams"]["away"]["id"],
         "gh": pick("home"), "ga": pick("away")}
    ref = clean_referee(f["fixture"].get("referee"))
    if ref:
        e["ref"] = ref
    if not (isinstance(e["home"], str) and e["home"] and isinstance(e["away"], str) and e["away"]):
        raise ValueError("équipe sans nom")
    lg = african_league(f)
    if lg:
        e["lg"], e["lk"] = lg, "africa"
    else:
        lgi = f.get("league") or {}
        cl = apileagues.classify(lgi.get("country"), lgi.get("name"))
        if cl:
            e["lk"], e["ln"] = cl[0], cl[1]
    return e


def _stale(day, lst, fetched=None):
    """Liste en cache d'un jour PASSÉ qui contient un match non terminé (ou sans buts, ancien format) :
    le cache du matin (statut NS) bloquerait le règlement. Aussi : liste du jour `day` téléchargée AVANT son début (préchargement
    de la veille, africa_matches) alors que `day` a commencé : horaires et statuts d'hier, à rafraîchir. `fetched` : heure de
    téléchargement (time.time()), None si inconnue (ancien cache : on ne devine pas)."""
    if str(day) < _today() and any(f.get("status") in PENDING or "gh" not in f for f in lst):
        return True
    if fetched and str(day) == _today():           # jours passés : la règle précédente suffit (un match non terminé)
        try:
            return fetched < datetime.fromisoformat(str(day)[:10]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return False
    return False


def _fx_load():
    """(listes par jour, heures de téléchargement par jour) de data/fixtures_day.json."""
    raw = store.load("fixtures_day.json", {})
    raw = raw if isinstance(raw, dict) else {}
    meta = raw.pop("_fetched", None)
    return raw, meta if isinstance(meta, dict) else {}


def _day_fixtures(day):
    """Tous les matchs du jour (UTC), en un seul appel par jour, mis en cache.
    - jour passé dont tous les matchs sont terminés : pour toujours, zéro requête ;
    - aujourd'hui : relevé du matin, relu tel quel (le règlement n'a besoin que des matchs finis, donc de la veille), sauf s'il a été
      téléchargé avant le début du jour (préchargement de la veille) : alors rafraîchi, comme un jour passé ;
    - jour passé avec un match non terminé : re-téléchargé au plus une fois par exécution, une fois par
      C.FIXTURES_RETRY_HOURS, et seulement pendant C.FIXTURES_STALE_DAYS jours (un match reporté ne se re-télécharge pas
      indéfiniment)."""
    cache_, meta = _fx_load()
    old = cache_.get(str(day))
    if old is not None:
        if not _stale(day, old, meta.get(str(day))):
            cache.hit("api-football", 1)
            return old
        age_days = (datetime.fromisoformat(_today()).date() - datetime.fromisoformat(str(day)).date()).days
        since = time.time() - meta.get(str(day), 0)
        if str(day) in _refreshed or age_days > C.FIXTURES_STALE_DAYS or since < C.FIXTURES_RETRY_HOURS * 3600:
            cache.hit("api-football", 1)
            return old
    _refreshed.add(str(day))
    raw = get("/fixtures", date=str(day), timezone="UTC")
    if not raw:                                 # erreur ou rien : on garde l'ancien cache
        return old or []
    lst, bad = [], 0
    for f in raw:
        try:
            lst.append(_entry(f))
        except (KeyError, TypeError, AttributeError, ValueError):       # match sans équipes, sans nom, d'un type inattendu : sauté
            bad += 1
    if bad:
        _warn(f"{bad} match(s) illisible(s) dans la liste du {day} (sur {len(raw)}).")
    if not lst:                                 # rien d'exploitable : comme une erreur, l'ancien cache est gardé
        return old or []
    keep = str((datetime.fromisoformat(_today()) - timedelta(days=10)).date())
    cache_ = {d: v for d, v in cache_.items() if d >= keep}
    cache_[str(day)] = lst
    meta = {d: t for d, t in meta.items() if d in cache_} | {str(day): time.time()}
    store.save("fixtures_day.json", cache_ | {"_fetched": meta})
    return lst


def africa_matches(day):
    """Matchs africains de la « journée Kelly » (du jour `day` jusqu'au lendemain C.DAY_END_HOUR h UTC), entrées de _day_fixtures
    avec « lg », triés par heure. Une requête par date (celle du jour sert aussi au règlement ; celle du lendemain n'est demandée
    que s'il est encore dans la fenêtre du forfait gratuit), mises en cache ; [] sans clé API-Football."""
    if not enabled():
        return []
    d = datetime.fromisoformat(str(day)[:10])
    end = (d + timedelta(days=1, hours=C.DAY_END_HOUR)).replace(tzinfo=timezone.utc).timestamp()
    nxt = [f for f in _day_fixtures(str((d + timedelta(days=1)).date())) if f.get("lg") and (f.get("ts") or 0) < end]
    both = {f["id"]: f for f in [f for f in _day_fixtures(str(day)[:10]) if f.get("lg")] + nxt}       # un match n'est listé qu'une fois
    return sorted(both.values(), key=lambda f: f.get("ts") or 0)


def left_today():
    """Requêtes API-Football que Kelly peut encore faire aujourd'hui (plafond STATS_DAILY_CAP, appels déjà faits déduits)."""
    if not enabled() or _halted:
        return 0
    q = _quota()
    return 0 if q.get("remaining") == 0 else max(0, _cap() - q["n"])


def _best_fixture(lst, home, away, kts=None):
    """Match de la liste `lst` (entrées de _entry) qui est `home` - `away` : noms approchés (seuil de confiance), horaire à ±3 h de `kts` (timestamp) si connu ;
    le plus ressemblant, à ressemblance égale le plus proche de l'horaire. None si aucun."""
    best, best_key = None, None
    for f in lst:
        sh, sa = team_sim(f["home"], home), team_sim(f["away"], away)
        if sh < 0.8 or sa < 0.8 or sh + sa < 1.75:         # seuil de confiance
            continue
        gap = abs(f["ts"] - kts) if kts is not None and f.get("ts") else 0
        if kts is not None and gap > 3 * 3600:
            continue
        key = (round(sh + sa, 2), -gap)
        if best_key is None or key > best_key:
            best, best_key = f, key
    return best


_ref_memo = {"sig": None, "raw": {}, "found": {}}


def referee_for(ev):
    """Arbitre désigné d'un match (événement Kelly : home_team, away_team, commence_time), lu dans la liste du jour d'API-Football DÉJÀ en cache
    (_day_fixtures le télécharge : le relevé du matin et le règlement) : AUCUNE requête, jamais d'appel réseau. None si la liste manque, si le match n'y est pas, ou si l'API n'a
    pas encore désigné l'arbitre (fixture.referee vide). Même appariement que find_fixture (noms approchés, ±3 h)."""
    try:
        home, away, ko = ev["home_team"], ev["away_team"], ev["commence_time"]
        sig = store.version("fixtures_day.json")
        if _ref_memo["sig"] != sig or sig is None:
            _ref_memo.update(sig=sig, raw=_fx_load()[0], found={})
        k = (home, away, str(ko))
        if k not in _ref_memo["found"]:
            lst = _ref_memo["raw"].get(str(ko)[:10]) or []
            f = _best_fixture([x for x in lst if isinstance(x, dict) and x.get("home") and x.get("away")], home, away, _ts(ko))
            _ref_memo["found"][k] = (f or {}).get("ref")
        return _ref_memo["found"][k]
    except Exception:                                # noqa: BLE001 - l'arbitre est facultatif : jamais bloquant
        return None


def find_fixture(day, home, away, kickoff=None):
    """Fixture API-Football d'un match (noms approchés, horaire ±3 h si connu).
    `kickoff` (ISO) facultatif ; sinon un `day` plus long que AAAA-MM-JJ sert d'horaire."""
    if kickoff is None and len(str(day)) > 10:
        kickoff = day
    # La liste d'API-Football est rangée par date UTC du coup d'envoi : un match de la « journée Kelly » joué après minuit UTC
    # (Amériques, 00:30) est dans la liste du lendemain, pas dans celle du jour d'analyse.
    day = str(kickoff)[:10] if kickoff else str(day)[:10]
    known = store.load("fixtures.json", {})
    k = f"{day}|{home}|{away}"
    if k in known:
        return known[k]
    kts = _ts(kickoff) if kickoff else None
    best = _best_fixture(_day_fixtures(day), home, away, kts)
    if not best:
        return None
    known[k] = {"id": best["id"], "home_id": best["home_id"], "away_id": best["away_id"],
                "status": best["status"]}
    cut = str((datetime.fromisoformat(_today()) - timedelta(days=14)).date())
    store.save("fixtures.json", {key: v for key, v in known.items() if key[:10] >= cut})     # le fichier ne grossit pas
    return known[k]


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


def _numn(v):
    """Comme _num, mais None si la valeur est absente (null : l'API ne suit pas cette statistique pour ce match)."""
    try:
        return float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _xg_value(v):
    """Buts attendus (« expected_goals ») d'une équipe pour un match : nombre ou chaîne « 1.62 », arrondi à 2 décimales ; None si l'API
    ne le donne pas (null, absent, illisible, négatif ou absurde) : un xG inconnu n'est jamais compté comme 0."""
    x = _numn(v)
    return round(x, 2) if x is not None and 0 <= x < 20 else None


STATS_KEEP_DAYS = 45                                 # statistiques brutes : les matchs plus vieux sont oubliés (profils déjà calculés)


def _team_values(t):
    """Totaux d'une équipe pour un match d'après la réponse /fixtures/statistics : corners, cartons (jaunes + rouges), tirs,
    tirs cadrés, fautes (0 si absent, compatibilité du règlement) + « nul » : statistiques que l'API a laissées à null (jamais
    comptées comme 0 dans les profils) + « xg » (buts attendus, seulement s'il est donné : les entrées de cache anciennes n'en ont pas). Un null de tirs cadrés avec des tirs connus, ou de jaunes, rouges et corners avec d'autres
    statistiques de l'équipe, est un vrai zéro."""
    st = {s["type"]: _numn(s.get("value")) for s in t.get("statistics") or [] if s.get("type")}
    shots, y, r, c = st.get("Total Shots"), st.get("Yellow Cards"), st.get("Red Cards"), st.get("Corner Kicks")
    # API-Football envoie null pour un compteur à zéro : dès que l'équipe a d'autres statistiques (tirs, fautes, possession...), un null de
    # jaunes, rouges ou corners est un vrai zéro. Sans aucune autre statistique, la donnée manque (nul) : jamais comptée comme 0.
    if any(v is not None for k, v in st.items() if k not in ("Yellow Cards", "Red Cards", "Corner Kicks")):
        y, r, c = (0.0 if v is None else v for v in (y, r, c))
    vals = {"corners": c, "shots": shots, "fouls": st.get("Fouls"),
            "sot": st.get("Shots on Goal") if st.get("Shots on Goal") is not None else (0.0 if shots is not None else None),
            "cards": None if y is None and r is None else (y or 0.0) + (r or 0.0), "yel": y if y is not None else (0.0 if r is not None else None)}
    out = {k: (v if v is not None else 0.0) for k, v in vals.items()}
    out["nul"] = [k for k, v in vals.items() if v is None]
    xg = _xg_value(st.get("expected_goals"))
    if xg is not None:                              # xG : seulement quand l'API le donne (clé absente = inconnu, jamais 0) ; ne sert à aucun règlement
        out["xg"] = xg
    return out


def _entry_from(teams):
    """Entrée du cache d'un match terminé à partir des équipes de /fixtures/statistics ; None si l'API n'a pas de statistiques
    (sans aucune ligne « Corner Kicks », la donnée manque : ne pas régler sur 0)."""
    if len(teams) < 2 or not any(s.get("type") == "Corner Kicks" for t in teams for s in t.get("statistics") or []):
        return None
    out = {"corners": 0, "cards": 0, "shots": 0, "sot": 0, "fouls": 0, "teams": {}, "players": {}, "players_done": False,
           "d": _today()}
    for t in teams:
        tv = _team_values(t)
        out["teams"][str(t["team"]["id"])] = tv
        for k in ("corners", "cards", "shots", "sot", "fouls"):
            out[k] += tv[k]
    return out


def _stats_path():
    """Fichier des statistiques brutes des matchs : .cache/apistats (persisté par actions/cache, jamais commité : il grossit de
    ~60 matchs par jour) ; dans le dossier de données quand DATA_DIR est réglé (tests, --sample) ; STATS_CACHE_DIR le remplace."""
    d = os.environ.get("STATS_CACHE_DIR")
    if not d and not os.environ.get("DATA_DIR"):
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "apistats")
    return os.path.join(d, "stats_cache.json") if d else store.path("stats_cache.json")


def _stats_cache():
    try:
        with open(_stats_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stats_cache(cache_):
    """Écrit le cache des statistiques en oubliant les matchs vus il y a plus de STATS_KEEP_DAYS jours (le fichier ne grossit pas)."""
    cut = str((datetime.fromisoformat(_today()) - timedelta(days=STATS_KEEP_DAYS)).date())
    for k, v in cache_.items():
        if isinstance(v, dict) and not v.get("d") and not v.get("missing"):
            v["d"] = _today()                       # ancien format : daté à la première réécriture
    keep = {k: v for k, v in cache_.items() if not isinstance(v, dict) or (v.get("d") or v.get("missing") or "9") >= cut}
    path, tmp = _stats_path(), f"{_stats_path()}.{os.getpid()}.tmp"
    try:                                            # écriture atomique : fichier temporaire puis os.replace (un arrêt en cours d'écriture ne tronque jamais le cache)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(keep, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:                            # le cache est facultatif : sans lui, on redemande
        _warn(f"cache des statistiques non écrit ({type(e).__name__}).")
        try:
            os.remove(tmp)
        except OSError:
            pass


def stats_known(fixture_id):
    """Vrai si le cache du disque contient déjà les statistiques (ou leur absence récente) de ce match : zéro requête."""
    cur = _stats_cache().get(str(fixture_id))
    if not cur:
        return False
    if cur.get("missing"):
        return _today() < str((datetime.fromisoformat(cur["missing"]) + timedelta(days=C.STATS_NO_DATA_DAYS)).date())
    return True


def cached_stats(fixture_id):
    """Statistiques d'un match DÉJÀ téléchargées (cache disque), ou None : jamais une requête de plus (règlement du journal de l'ensemble, qui ne doit pas
    entamer le quota du jour)."""
    cur = _stats_cache().get(str(fixture_id))
    return cur if isinstance(cur, dict) and not cur.get("missing") else None


def fixture_stats(fixture_id, players=True):
    """Totaux du match : corners, cartons, tirs, tirs cadrés, fautes, par équipe et au total, et stats par joueur. Mis en cache
    (pour la durée STATS_KEEP_DAYS) : un match déjà demandé ne l'est jamais deux fois.
    players=False évite l'appel /fixtures/players (moyennes d'équipe seulement)."""
    cache_ = _stats_cache()
    cur = cache_.get(str(fixture_id))
    if cur and cur.get("missing"):                  # l'API n'a pas de statistiques pour ce match : pas avant quelques jours
        if _today() < str((datetime.fromisoformat(cur["missing"]) + timedelta(days=C.STATS_NO_DATA_DAYS)).date()):
            cache.hit("api-football", 1)
            return None
        cur = None
    if cur and (not players or cur.get("players_done")):
        cache.hit("api-football", 1 + (1 if players else 0))
        return cur
    if cur:
        out = cur
    else:
        teams = get("/fixtures/statistics", fixture=fixture_id)
        if teams is None:                           # erreur ou quota : on réessaiera
            return None
        try:
            out = _entry_from(teams)
        except (KeyError, TypeError, AttributeError, ValueError):       # réponse d'un type inattendu : une erreur, pas « pas de statistiques »
            _warn(f"statistiques illisibles pour le match {fixture_id} (réessayé plus tard).")
            return None
        if out is None:
            cache_[str(fixture_id)] = {"missing": _today()}
            _save_stats_cache(cache_)
            return None
    if players:
        resp = get("/fixtures/players", fixture=fixture_id)
        if resp is not None:
            try:
                got = {}
                for team in resp:
                    tid = (team.get("team") or {}).get("id")
                    for p in team.get("players") or []:
                        s = (p.get("statistics") or [{}])[0]
                        if not (s.get("games") or {}).get("minutes"):      # remplaçant jamais entré (minutes null ou 0) : pas de stats, pas un « 0 tir »
                            continue
                        pid = p["player"].get("id")
                        # clé = identifiant du joueur (deux homonymes, un par équipe, ne s'écrasent plus) ; nom et équipe gardés pour kelly/players.py ;
                        # sans identifiant (réponse incomplète) : le nom, comme l'ancien format {nom: stats} que players.py lit toujours
                        got[str(pid) if pid is not None else p["player"]["name"]] = {
                            "name": p["player"]["name"], **({"team_id": tid} if tid is not None else {}),
                            "shots": _num((s.get("shots") or {}).get("total")),
                            "shots_on_target": _num((s.get("shots") or {}).get("on")),
                            "fouls": _num((s.get("fouls") or {}).get("committed")),
                        }
                out["players"].update(got)
                out["players_done"] = True
            except (KeyError, TypeError, AttributeError, IndexError, ValueError):
                _warn(f"stats joueurs illisibles pour le match {fixture_id} (réessayées plus tard).")
    cache_[str(fixture_id)] = out
    _save_stats_cache(cache_)
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
    for pkey, st in stats["players"].items():
        pname = st.get("name") or pkey if isinstance(st, dict) else pkey          # format par identifiant {id: {"name", "team_id", ...}} ou ancien {nom: stats}
        pi, ps = _player_key(pname)
        if not ps or not (qs <= ps or ps <= qs):
            continue
        if qi and pi and qi != pi:
            continue
        hits[pkey] = st
    if len(hits) != 1:
        return None
    return next(iter(hits.values())).get(field)


# --- Moyennes d'équipe ----------------------------------------------------

def team_averages(team_id, day=None):
    """Moyennes corners/cartons pour et contre d'une équipe, lues dans l'historique que kelly/apistats.py accumule jour après
    jour : AUCUNE requête (le forfait gratuit refuse « last »). None si moins de MIN_MATCHES matchs avec statistiques."""
    from . import apistats
    return apistats.averages(team_id)


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
                ref = referee_for({"home_team": c["home"], "away_team": c["away"], "commence_time": c.get("commence") or ""})      # arbitre désigné (liste du jour en cache)
                feats[c["id"]] = sources.match_features(c["home"], c["away"], c["sport_key"], on=c.get("commence"), **({"referee": ref} if ref else {}))
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
