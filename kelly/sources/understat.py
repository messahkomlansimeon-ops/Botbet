"""Understat : xG par équipe (forme glissante) et par joueur (tirs, xG, xA pour 90 min).

Source principale : GET https://understat.com/getLeagueData/{ligue}/{année} (JSON simple ; en-têtes
X-Requested-With et Referer, comme le fait la page elle-même). Repli : les anciennes pages
https://understat.com/league/{EPL}/{année} embarquaient `var teamsData = JSON.parse('\\x7B...')`
(échappements hexadécimaux / unicode) : regex, décodage, json. Pages match : shotsData, rostersData.
Si rien ne marche : erreur claire, jamais d'exception.
"""
import json
import re
import warnings

from .. import store
from . import http, state

NAME = "understat"
LABEL = "Understat"
BASE = "https://understat.com"
PROFILE_FILE = "src_understat.json"
MIN_MINUTES = 450                      # joueurs gardés dans le profil (bruit sous ce seuil)

# Clés The Odds API -> ligue Understat.
SPORT_TO_LEAGUE = {
    "soccer_epl": "EPL", "soccer_spain_la_liga": "La_liga", "soccer_italy_serie_a": "Serie_A",
    "soccer_germany_bundesliga": "Bundesliga", "soccer_france_ligue_one": "Ligue_1",
    "soccer_russia_premier_league": "RFPL",
}
LEAGUES = tuple(SPORT_TO_LEAGUE.values())

_VAR = re.compile(r"""var\s+(\w+)\s*=\s*JSON\.parse\(\s*(?:'([^'\\]*(?:\\.[^'\\]*)*)'|"([^"\\]*(?:\\.[^"\\]*)*)")\s*\)""",
                  re.S)


def season_year(today=None):
    d = today or state.now().date()
    return d.year if d.month >= 7 else d.year - 1


def decode_js(s):
    """Décode le contenu d'une chaîne JS (\\xNN, \\uNNNN, \\', \\\\...) en texte."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                     # \/ n'est pas un échappement Python
        t = s.encode("latin-1", "backslashreplace").decode("unicode_escape")
    if "Ã" in t or "Â" in t:                                # UTF-8 lu comme latin-1
        try:
            t = t.encode("latin-1").decode("utf-8")
        except (UnicodeError, ValueError):
            pass
    return t


def extract_vars(html):
    """{nom: objet JSON} de toutes les `var xxx = JSON.parse('...')` d'une page ; + problèmes."""
    out, problems = {}, []
    for m in _VAR.finditer(html or ""):
        name, raw = m.group(1), m.group(2) if m.group(2) is not None else m.group(3)
        try:
            out[name] = json.loads(decode_js(raw))
        except (ValueError, UnicodeError):
            problems.append(f"variable {name} illisible")
    return out, problems


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_list(x):
    if isinstance(x, dict):
        return list(x.values())
    return x if isinstance(x, list) else []


def normalize_league(teams, players, dates):
    """teamsData / playersData / datesData (formes diverses) -> ({équipe: historique}, joueurs, matchs)."""
    hist = {}
    for t in _as_list(teams):
        if isinstance(t, dict) and t.get("title") and isinstance(t.get("history"), list):
            hist[t["title"]] = [h for h in t["history"] if isinstance(h, dict)]
    return hist, [p for p in _as_list(players) if isinstance(p, dict)], [d for d in _as_list(dates) if isinstance(d, dict)]


def parse_league(html):
    """Page ligue -> ({"teams": {équipe: historique}, "players": [...], "dates": [...]}, problèmes)."""
    vs, problems = extract_vars(html)
    if "teamsData" not in vs:
        return None, problems + ["structure de la page modifiée : variable teamsData introuvable"]
    teams, players, dates = normalize_league(vs["teamsData"], vs.get("playersData"), vs.get("datesData"))
    return _check_league(teams, players, dates, problems)


def parse_league_json(text):
    """Réponse JSON de /getLeagueData/{ligue}/{année}."""
    try:
        d = json.loads(text)
    except ValueError:
        return None, ["structure de la page modifiée : réponse JSON illisible"]
    if isinstance(d, dict):
        d = {k.replace("Data", ""): v for k, v in d.items()}      # teams / teamsData
    if not isinstance(d, dict) or "teams" not in d:
        return None, ["structure de la page modifiée : clé teams absente de la réponse JSON"]
    teams, players, dates = normalize_league(d["teams"], d.get("players"), d.get("dates"))
    return _check_league(teams, players, dates, [])


def _check_league(teams, players, dates, problems):
    if not teams:
        return None, problems + ["structure de la page modifiée : aucune équipe dans teamsData"]
    sample = next((h for hist in teams.values() for h in hist), None)
    if sample is None:
        return {"teams": teams, "players": players, "dates": dates}, problems + ["aucun match joué cette saison"]
    miss = [k for k in ("xG", "xGA", "h_a", "date") if k not in sample]
    if miss:
        return None, problems + [f"structure de la page modifiée : champs {', '.join(miss)} absents de l'historique"]
    if not players:
        problems.append("playersData absent (pas de stats joueurs)")
    return {"teams": teams, "players": players, "dates": dates}, problems


def parse_match(html):
    """Page match -> ({"shots": {"h": [...], "a": [...]}, "rosters": {...}}, problèmes)."""
    vs, problems = extract_vars(html)
    miss = [k for k in ("shotsData", "rostersData") if k not in vs]
    if miss:
        return None, problems + [f"structure de la page modifiée : {', '.join(miss)} introuvable"]
    return {"shots": vs["shotsData"], "rosters": vs["rostersData"]}, problems


# --- Forme des équipes et des joueurs -------------------------------------------

def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _form(rows):
    out = {"n": len(rows)}
    for key, src in (("xg", "xG"), ("xga", "xGA"), ("npxg", "npxG"), ("npxga", "npxGA"), ("gf", "scored"),
                     ("ga", "missed"), ("deep", "deep"), ("deep_allowed", "deep_allowed")):
        v = _mean([_f(r.get(src)) for r in rows])
        if v is not None:
            out[key] = v
    pp = [_f(r["ppda"]["att"]) / _f(r["ppda"]["def"]) for r in rows
          if isinstance(r.get("ppda"), dict) and _f(r["ppda"].get("att")) is not None and _f(r["ppda"].get("def"))]
    if pp:
        out["ppda"] = round(sum(pp) / len(pp), 2)
    return out


def team_form(history, last_n=6):
    """Moyennes xG / xGA... sur les `last_n` derniers matchs (all) et par lieu (home / away)."""
    rows = sorted(history, key=lambda r: str(r.get("date")))
    return {"all": _form(rows[-last_n:]),
            "home": _form([r for r in rows if r.get("h_a") == "h"][-last_n:]),
            "away": _form([r for r in rows if r.get("h_a") == "a"][-last_n:])}


def player_per90(players, min_minutes=MIN_MINUTES):
    """{équipe: {joueur: stats pour 90 min}} depuis playersData (valeurs en chaînes)."""
    out = {}
    for p in players:
        name, minutes = p.get("player_name"), _f(p.get("time"))
        if not name or not minutes or minutes < min_minutes:
            continue
        team = str(p.get("team_title") or "").split(",")[-1].strip()
        if not team:
            continue
        rec = {"min": int(minutes), "pos": p.get("position")}
        for key, src in (("shots90", "shots"), ("xg90", "xG"), ("xa90", "xA"), ("kp90", "key_passes"),
                         ("goals90", "goals"), ("yc90", "yellow_cards")):
            v = _f(p.get(src))
            if v is not None:
                rec[key] = round(v * 90 / minutes, 2)
        out.setdefault(team, {})[name] = rec
    return out


def build_league(data, last_n=6):
    return {"teams": {t: team_form(h, last_n) for t, h in data["teams"].items()},
            "players": player_per90(data["players"])}


# --- Téléchargement --------------------------------------------------------------

def api_url(league, year):
    return f"{BASE}/getLeagueData/{league}/{year}"


def api_headers(league):
    """En-têtes du point d'accès JSON que la page du site appelle elle-même (XHR normal)."""
    return {"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/league/{league}",
            "Accept": "application/json, text/javascript, */*; q=0.01"}


def fetch_league(league, year):
    """(données brutes | None, erreurs) d'une ligue et d'une année.
    1) /getLeagueData/{ligue}/{année} (JSON, utilisé par le site) ; 2) repli : page HTML + JSON.parse."""
    tag = f"{league} {year}"
    hdr = api_headers(league)
    res = http.fetch(api_url(league, year), NAME, http.TTL_UNDERSTAT, headers=hdr)
    errs = []
    if res.ok:
        data, probs = parse_league_json(res.text)
        if data is not None:
            return data, [f"{tag} : {p}" for p in probs] + ([f"{tag} : {res.error} (données en cache)"] if res.error else [])
        http.invalidate(api_url(league, year), hdr)
        errs += [f"{tag} : {p}" for p in probs]
    else:
        errs.append(f"{tag} : {res.error}")
    if state.is_blocked(NAME):
        return None, errs
    page = f"{BASE}/league/{league}/{year}"
    res = http.fetch(page, NAME, http.TTL_UNDERSTAT)
    if not res.ok:
        return None, errs + [f"{tag} : {res.error}"]
    data, probs = parse_league(res.text)
    if data is None:
        http.invalidate(page)
        return None, errs + [f"{tag} : {p}" for p in probs]
    return data, [f"{tag} : {p}" for p in probs] + ([f"{tag} : {res.error} (données en cache)"] if res.error else [])


def _has_matches(data):
    return bool(data) and any(data["teams"].values())


def fetch_match(match_id):
    """Stats d'un match (tirs, compositions) ; None + erreurs si indisponible."""
    res = http.fetch(f"{BASE}/match/{match_id}", NAME, http.TTL_UNDERSTAT)
    if not res.ok:
        return None, [f"match {match_id} : {res.error}"]
    data, probs = parse_match(res.text)
    return data, [f"match {match_id} : {p}" for p in probs]


def leagues_for(sport_keys=None):
    if sport_keys is None:
        return list(LEAGUES[:5])
    return list(dict.fromkeys(SPORT_TO_LEAGUE[k] for k in sport_keys if k in SPORT_TO_LEAGUE))


def refresh(sport_keys=None, year=None):
    """Télécharge les ligues demandées, enregistre forme des équipes et stats joueurs.
    Ne lève jamais ; renvoie health()."""
    try:
        leagues = leagues_for(sport_keys)
        if not leagues:
            return health()
        year = year or season_year()
        prof = store.load(PROFILE_FILE, {})
        prof = prof if isinstance(prof, dict) else {}
        prof.setdefault("leagues", {})
        errors, rows = [], 0
        for lg in leagues:
            if state.is_blocked(NAME):
                break
            data, errs = None, []
            for y in (year, year - 1):                     # début de saison : repli sur la précédente
                data, e = fetch_league(lg, y)
                errs += e
                if _has_matches(data):
                    break
                data = None
            errors += errs if data is None else [e for e in errs if "aucun match" not in e and "HTTP 404" not in e]
            if data is not None:
                built = build_league(data)
                built["year"] = y
                prof["leagues"][lg] = built
                rows += len(built["teams"]) + sum(len(p) for p in built["players"].values())
        prof["leagues"] = {k: v for k, v in prof["leagues"].items() if k in LEAGUES}
        if rows:
            prof["updated"] = state.now().isoformat(timespec="minutes")
            if state.save_profile(PROFILE_FILE, prof) > state.MAX_PROFILE_BYTES:
                errors.append("profil trop volumineux (réduire les ligues ou MIN_MINUTES)")
        return state.record_run(NAME, rows, errors)
    except Exception as e:
        return state.record_run(NAME, 0, [f"erreur interne ({type(e).__name__}: {e})"])


def health():
    return state.health(NAME)


def profiles():
    p = store.load(PROFILE_FILE, {})
    return p if isinstance(p, dict) else {}
