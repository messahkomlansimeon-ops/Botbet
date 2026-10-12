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
from datetime import datetime, timedelta, timezone

from .. import config as C, store
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
    "soccer_conmebol_copa_sudamericana": 299, "soccer_portugal_primeira_liga": 61, "soccer_spl": 64,
    "soccer_germany_bundesliga2": 146, "soccer_spain_segunda_division": 140, "soccer_italy_serie_b": 86,
    "soccer_france_ligue_two": 110, "soccer_switzerland_superleague": 69, "soccer_china_superleague": 120,
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
SPORT_TO_LEAGUE["soccer_sweden_superettan"] = 168                       # vérifié : SWE, Superettan
if C.XWORLD_ENABLED:                                                    # XWORLD_ENABLED=0 : ni pages de ligue ni stats pour les championnats 1xBet
    EXTRA_LEAGUES.update({k: f for rows in C.XB_LEAGUES.values() for k, _, _, f in rows})     # championnats 1xBet (kelly/xworld.py)
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


STAGE_MIN_N = 3                       # matchs joués minimum d'une équipe dans la phase en cours ; sinon sa phase précédente (plus fournie) sert de repli


def _stage(round_name):
    """Phase d'un tour FotMob : « Clausura - Round 11 » -> « Clausura », « Relegation Round - Round 1 » -> « Relegation Round ».
    Un tour numéroté (« 4 », 4) n'en nomme aucune : None."""
    if not isinstance(round_name, str) or " - " not in round_name:
        return None
    return round_name.rsplit(" - ", 1)[0].strip() or None


def _stage_dates(matches):
    """{phase: date (AAAA-MM-JJ) du dernier match joué} d'après les tours de la page ; sert à reconnaître la phase en cours."""
    out = {}
    for m in matches:
        st = _stage(m.get("roundName") or m.get("round"))
        d = str((m.get("status") or {}).get("utcTime") or "")[:10]
        if st and d and (m.get("status") or {}).get("finished"):
            out[st] = max(out.get(st, ""), d)
    return out


def _sub_score(name, stage_dates):
    """Date du dernier match joué de la phase que nomme la sous-table `name` (« Clausura Group A » <- phase « Clausura »), « » si inconnue."""
    name = str(name or "")
    best = ""
    for stage, d in stage_dates.items():
        if name == stage or name.startswith(stage + " ") or stage.startswith(name + " "):
            best = max(best, d)
    return best


def _sub_teams(sub):
    """{nom: {n, gf, ga, w, pts[, xg, xga, xpts]}} d'UNE sous-table de classement (xG de la même sous-table) ; {} si elle est vide."""
    nested = sub.get("table")
    table = nested if isinstance(nested, dict) and (nested.get("all") or nested.get("xg")) else sub
    teams = {}
    for r in _lines(table.get("all")):
        name = r.get("name")
        if not name:
            continue
        gf, ga = _f(str(r.get("scoresStr") or "").split("-")[0]), _f(str(r.get("scoresStr") or "").split("-")[-1])
        teams[name] = {"n": _f(r.get("played")), "gf": gf, "ga": ga, "w": _f(r.get("wins")), "pts": _f(r.get("pts"))}
    for r in _lines(table.get("xg")):
        rec, xg = teams.get(r.get("name")), _f(r.get("xg"))
        # Lignes sans xG (FotMob ne sert pas de xG pour toutes les ligues, ex. Argentine) : ne rien écrire.
        if rec is not None and xg is not None:
            rec["xg"], rec["xga"], rec["xpts"] = xg, _f(r.get("xgConceded")), _f(r.get("xPoints"))
    return teams


def parse_league(html):
    """-> ({"teams": {nom: {...}}, "matches": [...], "stages": [...]}, problèmes) depuis le `__NEXT_DATA__` d'une page de ligue.

    Une page peut porter plusieurs sous-tables (data.tables) : conférences ou groupes (équipes différentes dans chacune), mais aussi des
    PHASES successives où la même équipe figure plusieurs fois (Apertura / Clausura de Colombie, du Pérou, du Paraguay, du Venezuela,
    d'Argentine ; phase régulière puis poules de titre / de relégation de l'Équateur et de la Finlande). FotMob les liste la plus récente
    en premier ; plutôt que de parier sur cet ordre, on garde pour chaque équipe la ligne de la sous-table dont la phase a le match joué le plus
    récent (les tours « Clausura - Round 11 » nomment la phase), sinon la première listée. Une équipe qui n'a joué que STAGE_MIN_N - 1 matchs
    ou moins dans la phase en cours garde sa phase précédente (un xG par match ne se calcule pas sur 2 matchs). Les totaux (matchs joués, buts,
    xG) d'une équipe viennent tous de la MÊME sous-table. `stages` : noms des sous-tables retenues quand la page en a plusieurs."""
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
    all_matches = [x for x in (pp.get("fixtures") or {}).get("allMatches") or [] if isinstance(x, dict)]
    stage_dates = _stage_dates(all_matches)
    subs = []
    # Une ligue FotMob : soit data.table unique, soit data.tables = conférences / groupes / phases.
    for i, sub in enumerate([data.get("table")] + list(data.get("tables") or [])):
        if not isinstance(sub, dict):
            continue
        teams = _sub_teams(sub)
        if teams:
            subs.append((_sub_score(sub.get("leagueName") or data.get("leagueName"), stage_dates), i, str(sub.get("leagueName") or ""), teams))
    if not subs:
        return None, ["aucun classement (coupe sans classement)"]
    subs.sort(key=lambda t: t[0], reverse=True)                      # phase la plus récente d'abord ; à égalité (ou sans date) l'ordre de la page (tri stable)
    teams, chosen = {}, []
    for name in dict.fromkeys(n for _, _, _, tm in subs for n in tm):
        cands = [(sub_name, tm[name]) for _, _, sub_name, tm in subs if name in tm]
        sub_name, rec = next(((sn, r) for sn, r in cands if (r.get("n") or 0) >= STAGE_MIN_N), cands[0])
        teams[name] = rec
        chosen.append(sub_name)
    matches, seen = [], set()
    for x in all_matches:
        st = x.get("status") or {}
        if not st.get("finished") or st.get("cancelled") or st.get("awarded"):
            continue
        if str((st.get("reason") or {}).get("short") or "FT").upper() not in ("FT", "FIN"):     # après prolongation : score de 120 min, pas celui des 90
            continue
        home, away = (x.get("home") or {}).get("name"), (x.get("away") or {}).get("name")
        if not home or not away:
            continue
        hg, ag = _f(str(st.get("scoreStr") or "").replace(" ", "").split("-")[0]), \
            _f(str(st.get("scoreStr") or "").replace(" ", "").split("-")[-1])
        if hg is None or ag is None:
            continue
        d = str(st.get("utcTime") or "")[:10]
        key = str(x["id"]) if x.get("id") else (home, away, d)      # un même duel rejoué à une autre date est un autre match (Celtic - Rangers 3-4 fois)
        if key in seen:
            continue
        seen.add(key)
        matches.append({"d": d, "home": home, "away": away, "hg": hg, "ag": ag, "_t": str(st.get("utcTime") or "")})
    matches.sort(key=lambda r: r["_t"])                              # chronologique : FOTMOB_KEEP garde les PLUS RÉCENTS
    for r in matches:
        del r["_t"]
    out = {"teams": teams, "matches": matches}
    if len(subs) > 1:
        out["stages"] = [n for n in dict.fromkeys(chosen) if n]
    return out, []


def build(data):
    """Profil compact d'une ligue : équipes (classement + xG) et FOTMOB_KEEP derniers matchs (les plus récents ; les phases d'un classement
    à plusieurs phases : `stages`)."""
    return {"teams": data["teams"], "matches": data["matches"][-FOTMOB_KEEP:]} | ({"stages": data["stages"]} if data.get("stages") else {})


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


def league_url(key):
    lgid = EXTRA_LEAGUES.get(key) or SPORT_TO_LEAGUE.get(key)
    return f"{BASE}/leagues/{lgid}" if lgid else None


def playing_soon(key, today=None, days=1):
    """Vrai si la page de ligue gardée en cache (aucun téléchargement) annonce un match non joué entre aujourd'hui et dans `days` jours :
    la ligue est à actualiser avant les autres, c'est elle qui sert aux pronostics du jour."""
    u = league_url(key)
    res = http.cached(u) if u else None
    m = PREFIX.search(res.text) if res and res.ok else None
    if not m:
        return False
    today = today or state.now().date()
    lo, hi = today.isoformat(), (today + timedelta(days=days)).isoformat()
    try:
        matches = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("fixtures", {}).get("allMatches") or []
    except (ValueError, AttributeError):
        return False
    return any(isinstance(x, dict) and not (x.get("status") or {}).get("finished") and not (x.get("status") or {}).get("cancelled")
               and lo <= str((x.get("status") or {}).get("utcTime") or "")[:10] <= hi for x in matches)


def refresh(sport_keys=None):
    """Actualise les ligues mappées (1 page HTML par ligue, cache 12 h). Ne lève jamais. Le délai du passage (http.set_deadline) ne suffit
    pas à toutes les ligues à froid : celles qui jouent aujourd'hui ou demain passent d'abord (state.set_priority), puis la page la plus
    ancienne ; une page non rafraîchie faute de temps ou d'accès reste servie depuis le cache mais est SIGNALÉE (profil : stale, rapport)."""
    try:
        if not enabled():
            return health()
        keys = [k for k in leagues_for(sport_keys) if league_url(k)]
        keys = state.order(keys, lambda k: http.age(league_url(k)))
        prof = store.load(PROFILE_FILE, {})
        prof = prof if isinstance(prof, dict) else {}
        prof.setdefault("leagues", {})
        errors, rows, stale = [], 0, []
        for key in keys:
            if state.is_blocked(NAME):
                break
            lgid = EXTRA_LEAGUES.get(key) or SPORT_TO_LEAGUE.get(key)
            u = league_url(key)
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
            t = http.fetched_at(u)
            if t is not None:
                built["at"] = datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="minutes")
            if getattr(res, "stale", False):
                built["stale"] = True
                stale.append(key)
            prof["leagues"][key] = built
            rows += len(built["teams"]) + len(built["matches"])
            if probs:
                errors.append(f"{key} : {probs[0]}")
        prof["leagues"] = {k: v for k, v in prof["leagues"].items() if k in LEAGUES}
        if rows:
            prof["updated"] = state.now().isoformat(timespec="minutes")
            if state.save_profile(PROFILE_FILE, prof) > state.MAX_PROFILE_BYTES:
                errors.append("profil trop volumineux (réduire FOTMOB_KEEP)")
        if stale:
            errors.insert(0, f"{len(stale)} ligue(s) servie(s) depuis un cache périmé (téléchargement reporté ou refusé) : "
                             + ", ".join(stale[:4]) + ("…" if len(stale) > 4 else ""))
        return state.record_run(NAME, rows, errors, stale=len(stale))
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
