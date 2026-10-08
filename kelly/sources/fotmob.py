"""FotMob : xG, classements et résultats par ligue.

Pages HTML de www.fotmob.com autorisées par robots.txt (interdiction /api/* : jamais utilisée).
Chaque page de ligue embarque ses données dans `__NEXT_DATA__` : classement général, classement xG
(xG, xG concédés, xPoints) et les matchs de la saison (noms, heure UTC, score final). Kelly en tire :
- l'xG attendu par équipe (repli d'Understat : les 5 grandes ligues + Angleterre bas de table, MLS,
  Argentine, Brésil, Chine, Corée, Japon, Australie, Ligue 1 algérienne, Premier League égyptienne) ;
- les derniers résultats de chaque équipe (forme, face-à-face du jour).

Fichier data/src_fotmob.json : {leagues: {clé sport: {teams: {nom: {n, gf, ga, xg, xga, xpts}},
matches: [{d, home, away, hg, ag}]}}} ; au plus FOTMOB_KEEP matchs par ligue.
IDs vérifiés un par un sur /leagues/{id} (champs `country` + `name`), le 8 octobre 2026.
"""
import json
import os
import re

from .. import store
from . import http, state

NAME = "fotmob"
LABEL = "FotMob"
BASE = "https://www.fotmob.com"
PROFILE_FILE = "src_fotmob.json"
FOTMOB_KEEP = 30                      # derniers matchs gardés par ligue (forme + face-à-face)
PREFIX = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Clés The Odds API -> id FotMob (tous vérifiés : pays + nom lus sur la page de la ligue).
SPORT_TO_LEAGUE = {
    "soccer_epl": 47, "soccer_efl_champ": 48, "soccer_england_league1": 108, "soccer_england_league2": 109,
    "soccer_spain_la_liga": 87, "soccer_italy_serie_a": 55, "soccer_germany_bundesliga": 54, "soccer_france_ligue_one": 53,
    "soccer_netherlands_eredivisie": 57, "soccer_belgium_first_div": 40, "soccer_turkey_super_league": 71,
    "soccer_greece_super_league": 135, "soccer_austria_bundesliga": 38, "soccer_poland_ekstraklasa": 196,
    "soccer_denmark_superliga": 46, "soccer_sweden_allsvenskan": 67, "soccer_norway_eliteserien": 59,
    "soccer_finland_veikkausliiga": 51, "soccer_league_of_ireland": 126, "soccer_usa_mls": 130,
    "soccer_argentina_primera_division": 112, "soccer_brazil_campeonato": 268, "soccer_brazil_serie_b": 8814,
    "soccer_mexico_ligamx": 230, "soccer_chile_campeonato": 273, "soccer_conmebol_copa_libertadores": 45,
    "soccer_conmebol_copa_sudamericana": 299, "soccer_china_superleague": 120,
    "soccer_korea_kleague1": 9080, "soccer_australia_aleague": 113, "soccer_japan_j_league": 223,
    "soccer_uefa_champs_league": 42, "soccer_uefa_europa_league": 73,
    "soccer_uefa_europa_conference_league": 10216, "soccer_international_friendlies": 114,
    # Préfixes de qualifications (clé The Odds API finissant par « _ ») -> (id, sous-clé).
    "soccer_fifa_world_cup_qualifiers_europe": 10195,
    "soccer_fifa_world_cup_qualifiers_south_america": 10199,
    "soccer_fifa_world_cup_qualifiers_afc": 10197, "soccer_fifa_world_cup_qualifiers_concacaf": 10198,
}
# Ligues hors catalogue The Odds API, toujours actualisées : le tour du monde africain et les qualifications
# CAF n'ont pas de cotes The Odds API mais FotMob donne leurs classements et résultats.
EXTRA_LEAGUES = {
    "soccer_caf_champions_league": 526, "soccer_african_football_league": 10508,
    "soccer_african_nations_championship": 9428, "soccer_can_qualification": 10608,
    "soccer_world_cup_qualifiers_caf": 10196,
    "africa_egypt_premier_league": 519, "africa_morocco_botola_pro": 530,     # stats de match (fmmatch) : touches, fautes... (Égypte)
}
LEAGUES = {k for k in SPORT_TO_LEAGUE if not k.endswith("_")} | set(EXTRA_LEAGUES)
_QUALIF = (("africa", 10196), ("afc", 10197), ("concacaf", 10198), ("south_america", 10199), ("europe", 10195))


def enabled():
    return (os.environ.get("FOTMOB_ENABLED") or "1").strip().lower() not in ("0", "false", "non", "no")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _lines(v):
    """Toutes les lignes de classement d'une sous-table FotMob : soit une liste de lignes, soit des
    groupes de conférences (chaque groupe avec rows), soit un dict de groupes."""
    if isinstance(v, dict):
        return [r for r in (v.get("rows") or []) if isinstance(r, dict)]
    if isinstance(v, list):
        out = []
        for g in v:
            if not isinstance(g, dict):
                continue
            if isinstance(g.get("rows"), list):
                out += [r for r in g["rows"] if isinstance(r, dict)]
            elif g.get("name"):
                out.append(g)
        return out
    return []


def parse_league(html):
    """-> ({"teams": {nom: {...}}, "matches": [...]}, problèmes) depuis le `__NEXT_DATA__` d'une page de ligue."""
    m = PREFIX.search(html or "")
    if not m:
        return None, ["structure de la page modifiée : __NEXT_DATA__ introuvable"]
    try:
        pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    except ValueError:
        return None, ["structure de la page modifiée : JSON illisible"]
    tabs = pp.get("table")
    if not isinstance(tabs, list) or not tabs or not isinstance(tabs[0], dict):
        return None, ["structure de la page modifiée : classement introuvable"]
    data = tabs[0].get("data") or {}
    lines, xgv = [], []
    for sub in [data.get("table")] + list(data.get("tables") or []):
        # Une ligue FotMob : soit data.table unique, soit data.tables = conférences / groupes.
        # Deux formats de sous-table selon la structure du site : sous-clé table (ancien format,
        # all + xg) ou directement le dict de lignes (nouveau format, all + xg).
        if not isinstance(sub, dict):
            continue
        nested = sub.get("table")
        table = nested if isinstance(nested, dict) and (nested.get("all") or nested.get("xg")) else sub
        lines += _lines(table.get("all"))
        xg = table.get("xg")
        if xg:
            xgv += _lines(xg)
    if not lines:
        return None, ["aucun classement (coupe sans classement)"]
    teams = {}
    for r in lines:
        name = r.get("name")
        gf, ga = _f(str(r.get("scoresStr") or "").split("-")[0]), _f(str(r.get("scoresStr") or "").split("-")[-1])
        if not name:
            continue
        teams[name] = {"n": _f(r.get("played")), "gf": gf, "ga": ga,
                       "w": _f(r.get("wins")), "pts": _f(r.get("pts"))}
    for r in xgv:
        name, rec = r.get("name"), teams.get(r.get("name"))
        xg, xga = _f(r.get("xg")), _f(r.get("xgConceded"))
        # Lignes sans xG (FotMob ne sert pas de xG pour toutes les ligues, ex. Argentine) : ne rien
        # écrire, pour ne pas écraser le xG d'une sous-table qui en a (conférences, groupes).
        if name and rec is not None and xg is not None:
            rec["xg"], rec["xga"] = xg, xga
            rec["xpts"] = _f(r.get("xPoints"))
    matches, seen = [], set()
    for m in (pp.get("fixtures") or {}).get("allMatches") or []:
        st = m.get("status") or {}
        if not st.get("finished") or st.get("cancelled"):
            continue
        home, away = (m.get("home") or {}).get("name"), (m.get("away") or {}).get("name")
        if not home or not away or (home, away) in seen:
            continue
        hg, ag = _f(str(st.get("scoreStr") or "").replace(" ", "").split("-")[0]), \
            _f(str(st.get("scoreStr") or "").replace(" ", "").split("-")[-1])
        if hg is None or ag is None:
            continue
        seen.add((home, away))
        matches.append({"d": str(st.get("utcTime") or "")[:10], "home": home, "away": away, "hg": hg, "ag": ag})
    return {"teams": teams, "matches": matches}, []


def build(data):
    """Profil compact d'une ligue : équipes (classement + xG) et FOTMOB_KEEP derniers matchs."""
    return {"teams": data["teams"], "matches": data["matches"][-FOTMOB_KEEP:]}


def leagues_for(sport_keys=None):
    """Clés à actualiser : intersection avec SPORT_TO_LEAGUE (préfixes développés), + EXTRA_LEAGUES."""
    keys = []
    for k in (list(SPORT_TO_LEAGUE) if sport_keys is None else sport_keys):
        if k in SPORT_TO_LEAGUE and not k.endswith("_"):
            keys.append(k)
        elif k.endswith("_"):
            base = k.rstrip("_")
            keys += [f"{base}_{q}" for q, _ in _QUALIF if f"{base}_{q}" in SPORT_TO_LEAGUE]
    keys = sorted(set(keys))
    return keys + [k for k in sorted(EXTRA_LEAGUES) if k not in keys]


def refresh(sport_keys=None):
    """Actualise les ligues mappées (1 page HTML par ligue, cache 12 h). Ne lève jamais."""
    try:
        if not enabled():
            return health()
        keys = leagues_for(sport_keys)
        prof = store.load(PROFILE_FILE, {})
        prof = prof if isinstance(prof, dict) else {}
        prof.setdefault("leagues", {})
        errors, rows = [], 0
        for key in keys:
            if state.is_blocked(NAME):
                break
            lgid = EXTRA_LEAGUES.get(key) or SPORT_TO_LEAGUE.get(key)
            if not lgid:
                continue
            u = f"{BASE}/leagues/{lgid}"
            res = http.fetch(u, NAME, http.TTL_FOTMOB)
            if not res.ok:
                errors.append(f"{key} : {res.error}")
                continue
            data, probs = parse_league(res.text)
            if data is None:
                errors += [f"{key} : {p}" for p in probs]
                http.invalidate(u)
                continue
            built = build(data) | {"id": lgid}               # id : une ligue dont l'id a changé (ex. 123 -> 48) n'est plus servie
            prof["leagues"][key] = built
            rows += len(built["teams"]) + len(built["matches"])
            if probs:
                errors.append(f"{key} : {probs[0]}")
        prof["leagues"] = {k: v for k, v in prof["leagues"].items() if k in LEAGUES}
        if rows:
            prof["updated"] = state.now().isoformat(timespec="minutes")
            if state.save_profile(PROFILE_FILE, prof) > state.MAX_PROFILE_BYTES:
                errors.append("profil trop volumineux (réduire FOTMOB_KEEP)")
        return state.record_run(NAME, rows, errors)
    except Exception as e:
        return state.record_run(NAME, 0, [f"erreur interne ({type(e).__name__}: {e})"])


def health():
    return state.health(NAME)


def profiles():
    p = store.load(PROFILE_FILE, {})
    if not isinstance(p, dict):
        return {}
    cur = EXTRA_LEAGUES | SPORT_TO_LEAGUE
    lg = {k: v for k, v in (p.get("leagues") or {}).items() if not isinstance(v, dict) or v.get("id") in (None, cur.get(k))}
    return p | {"leagues": lg} if "leagues" in p else p
