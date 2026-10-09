"""BetExplorer : cotes de clôture et résultats par ligue (pages HTML, robots.txt autorisé).

Chaque page /football/{pays}/{ligue}/results/ liste les matchs terminés : équipes, score final et les
3 cotes de clôture 1N2 (attributs data-odd). Kelly y tire l'historique des cotes de clôture — utile là où
The Odds API est hors budget (Afrique, petits championnats) et pour mesurer si 1xBet paye mieux que le
marché. Chaque pays du « tour du monde » a son chemin vérifié ; les chemins inconnus sont ignorés.

Fichier data/src_betexplorer.json : {leagues: {clé: {path, matches: [{d, home, away, hg, ag, o1, ox, o2}]}}}
au plus BETEXPLORER_KEEP matchs par ligue. Les matchs reportés ou sans cote sont sautés.
"""
import os
import re
from datetime import date, datetime, timedelta, timezone

from .. import store
from . import http, state

NAME = "betexplorer"
LABEL = "BetExplorer"
BASE = "https://www.betexplorer.com"
PROFILE_FILE = "src_betexplorer.json"
BETEXPLORER_KEEP = 80                 # matchs gardés par ligue (cotes de clôture)

# Clés The Odds API -> chemin /football/{pays}/{ligue} (tous vérifiés HTTP 200 ou présents dans le sitemap).
PATHS = {
    "soccer_epl": "england/premier-league", "soccer_efl_champ": "england/championship",
    "soccer_england_league1": "england/league-one", "soccer_england_league2": "england/league-two",
    "soccer_fa_cup": "england/fa-cup", "soccer_england_efl_cup": "england/efl-cup",
    "soccer_spain_la_liga": "spain/laliga", "soccer_italy_serie_a": "italy/serie-a",
    "soccer_germany_bundesliga": "germany/bundesliga", "soccer_france_ligue_one": "france/ligue-1",
    "soccer_portugal_primeira_liga": "portugal/liga-portugal", "soccer_netherlands_eredivisie": "netherlands/eredivisie",
    "soccer_belgium_first_div": "belgium/jupiler-pro-league", "soccer_turkey_super_league": "turkey/super-lig",
    "soccer_spl": "scotland/premiership", "soccer_greece_super_league": "greece/super-league",
    "soccer_switzerland_superleague": "switzerland/super-league", "soccer_austria_bundesliga": "austria/bundesliga",
    "soccer_poland_ekstraklasa": "poland/ekstraklasa", "soccer_denmark_superliga": "denmark/superliga",
    "soccer_sweden_allsvenskan": "sweden/allsvenskan", "soccer_norway_eliteserien": "norway/eliteserien",
    "soccer_finland_veikkausliiga": "finland/veikkausliiga", "soccer_league_of_ireland": "ireland/premier-division",
    "soccer_brazil_campeonato": "brazil/serie-a-betano", "soccer_brazil_serie_b": "brazil/serie-b",
    "soccer_argentina_primera_division": "argentina/liga-profesional", "soccer_usa_mls": "usa/mls",
    "soccer_mexico_ligamx": "mexico/liga-mx", "soccer_chile_campeonato": "chile/liga-de-primera",
    "soccer_japan_j_league": "japan/j1-league", "soccer_saudi_arabia_pro_league": "saudi-arabia/saudi-professional-league",
    "soccer_china_superleague": "china/super-league", "soccer_australia_aleague": "australia/a-league",
    "soccer_uefa_champs_league": "europe/champions-league", "soccer_uefa_europa_league": "europe/europa-league",
    "soccer_uefa_europa_conference_league": "europe/conference-league",
    "soccer_uefa_nations_league": "europe/uefa-nations-league", "soccer_international_friendlies": "world/friendly-international",
    "soccer_africa_cup_of_nations": "africa/africa-cup-of-nations",
}
# Ligues africaines sans cotes The Odds API : toujours actualisées (tour du monde, paris 1xBet).
EXTRA_PATHS = {
    "africa_ivory_coast_ligue_1": "ivory-coast/ligue-1", "africa_egypt_premier_league": "egypt/premier-league",
    "africa_morocco_botola_pro": "morocco/botola-pro", "africa_algeria_ligue_1": "algeria/ligue-1",
    "africa_senegal_ligue_1": "senegal/ligue-1", "africa_burkina_premier_league": "burkina-faso/premier-league",
    "africa_mali_premiere_division": "mali/premiere-division", "africa_cameroon_elite_one": "cameroon/elite-one",
    "africa_ghana_premier_league": "ghana/premier-league", "africa_nigeria_npfl": "nigeria/npfl",
    "africa_kenya_premier_league": "kenya/premier-league", "africa_rwanda_premier_league": "rwanda/premier-league",
    "africa_ethiopia_premier_league": "ethiopia/premier-league", "africa_libya_premier_league": "libya/premier-league",
    "africa_botswana_premier_league": "botswana/premier-league", "africa_angola_girabola": "angola/girabola",
    "africa_mozambique_mocambola": "mozambique/mocambola", "africa_burundi_primus_league": "burundi/primus-league",
    "africa_niger_super_ligue": "niger/super-ligue", "africa_chad_premiere_division": "chad/premiere-division",
    "africa_caf_champions_league": "africa/caf-champions-league",
}
_LEAGUE_PATHS = {k for k in PATHS} | set(EXTRA_PATHS)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_MATCH = re.compile(r'href="/football/[^"]*/([A-Za-z0-9]{6,10})/"')
_ANCHOR = re.compile(r'<a[^>]*class="in-match"[^>]*>(.*?)</a>', re.S)
_TEAMS = re.compile(r"<span[^>]*>(.*?)</span>\s*-\s*<span[^>]*>(.*?)</span>", re.S)
_SCORE = re.compile(r'>(\d+)\s*:\s*(\d+)<')
_ODD = re.compile(r'data-odd="([0-9.]+)"')


def enabled():
    return (os.environ.get("BETEXPLORER_ENABLED") or "1").strip().lower() not in ("0", "false", "non", "no")


def _txt(x):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x or "")).strip()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(label, today):
    """« Yesterday », « 06.10. », « 07.10. 18:00 », « 06.10.2025 » -> date ISO (selon la date de relevé `today`).
    Sans année, la page ne liste que des matchs passés : une date postérieure à `today` est celle de l'année précédente
    (relevé début janvier, match fin décembre)."""
    t = _txt(label).split(" ")[0].lower()
    d = today.date() if isinstance(today, datetime) else today
    if t == "yesterday":
        return (d.fromordinal(d.toordinal() - 1)).isoformat() if d else None
    if t == "today":
        return d.isoformat() if d else None
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})?", t)
    if m:
        try:
            if m.group(3):
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            if not d:
                return None
            try:
                x = date(d.year, int(m.group(2)), int(m.group(1)))
            except ValueError:                        # 29.02. : l'année du relevé n'est pas bissextile
                return date(d.year - 1, int(m.group(2)), int(m.group(1))).isoformat()
            if x > d + timedelta(days=1):             # date future : l'année précédente
                try:
                    x = date(d.year - 1, int(m.group(2)), int(m.group(1)))
                except ValueError:
                    return None
            return x.isoformat()
        except ValueError:
            return None
    return None


def parse_results(html, today=None):
    """-> [{d, home, away, hg, ag, o1, ox, o2}, ...] ; sans score ou sans cote = sauté."""
    out = []
    for raw in _ROW.finditer(html or ""):
        row = raw.group(1)
        mid, score = _MATCH.search(row), _SCORE.search(row)
        anchor = _ANCHOR.search(row) or re.search(r'<a[^>]*href="/football/[^"]*">(.*?)</a>', row, re.S)
        teams = _TEAMS.search(anchor.group(1)) if anchor else None
        if not (mid and teams and score):
            continue
        odds = [float(o) for o in _ODD.findall(row)]
        if len(odds) < 3:
            continue
        home, away = _txt(teams.group(1)), _txt(teams.group(2))
        if not home or not away:
            continue
        d = None
        dm = re.search(r">(\d{1,2}\.\d{1,2}\.\s*\d{1,2}:\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\.\d{1,2}\.|Yesterday|Today)<", row)
        if dm:
            d = _date(dm.group(1), today)
        out.append({"d": d, "home": home, "away": away, "hg": int(score.group(1)), "ag": int(score.group(2)),
                    "o1": odds[0], "ox": odds[1], "o2": odds[2], "mid": mid.group(1)})
    return out


def _keep(matches):
    """Les BETEXPLORER_KEEP matchs les plus RÉCENTS, du plus ancien au plus récent. La page /results/ liste le plus récent EN PREMIER :
    garder la fin de la liste (matches[-80:]) gardait les plus anciens (Brésil : janvier à avril sur 289 matchs). Sans date, un match passe
    après les datés (le plus ancien par défaut) ; à date égale l'ordre de la page est conservé."""
    newest = sorted(matches, key=lambda m: m.get("d") or "", reverse=True)[:BETEXPLORER_KEEP]
    return newest[::-1]


def results_url(key):
    path = EXTRA_PATHS.get(key) or PATHS.get(key)
    return f"{BASE}/football/{path}/results/" if path else None


def refresh(sport_keys=None):
    """Actualise les ligues mappées (1 page results par ligue, cache 12 h). Ne lève jamais. Comme FotMob, les ligues qui jouent aujourd'hui
    ou demain passent d'abord (state.set_priority), puis la page la plus ancienne : les championnats africains de fin de liste ne sont
    plus condamnés à rester périmés ; une page servie depuis le cache faute de temps ou d'accès est signalée (profil : stale, rapport)."""
    try:
        if not enabled():
            return health()
        keys = [k for k in (list(PATHS) if sport_keys is None else sport_keys)
                if k in PATHS and not k.endswith("_")]
        keys = sorted(set(keys)) + [k for k in sorted(EXTRA_PATHS) if k not in keys]
        keys = state.order(keys, lambda k: http.age(results_url(k)))
        prof = store.load(PROFILE_FILE, {})
        prof = prof if isinstance(prof, dict) else {}
        prof.setdefault("leagues", {})
        errors, rows, stale = [], 0, []
        for key in keys:
            if state.is_blocked(NAME):
                break
            path = EXTRA_PATHS.get(key) or PATHS.get(key)
            if not path:
                continue
            u = results_url(key)
            res = http.fetch(u, NAME, http.TTL_BETEXPLORER)
            if not res.ok:
                errors.append(f"{key} : {res.error}")
                continue
            matches = parse_results(res.text, state.now().date())
            if not matches:
                errors.append(f"{key} : structure de la page modifiée : aucun match avec cotes")
                http.invalidate(u)
                continue
            entry = {"path": path, "matches": _keep(matches)}
            t = http.fetched_at(u)
            if t is not None:
                entry["at"] = datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="minutes")
            if getattr(res, "stale", False):
                entry["stale"] = True
                stale.append(key)
            prof["leagues"][key] = entry
            rows += len(matches)
        prof["leagues"] = {k: v for k, v in prof["leagues"].items() if k in _LEAGUE_PATHS}
        if rows:
            prof["updated"] = state.now().isoformat(timespec="minutes")
            if state.save_profile(PROFILE_FILE, prof) > state.MAX_PROFILE_BYTES:
                errors.append("profil trop volumineux (réduire BETEXPLORER_KEEP)")
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
    return p if isinstance(p, dict) else {}
