"""Données « légères » des équipes que Kelly ne connaît pas, préparées À L'AVANCE pour le bouton 🔴 Live (kelly/live.py).

Au moment d'un appui, lire l'historique de 40 équipes prendrait des minutes : les cotes live auraient changé. Ce module le fait au passage horaire
(app.fill_match_stats), via 365scores et le proxy (s365._via_proxy, son plafond du mois) :
  1. la liste des matchs du jour (/games/allscores/, ~180 Ko, relue toutes les C.LIVE_PREP_LIST_H heures) : tous les championnats, avec le numéro
     365scores de chaque équipe ;
  2. pour les matchs commencés depuis moins de 2 h ou qui commencent dans les C.LIVE_PREP_AHEAD_H prochaines heures (les plus proches d'abord), hors jeunes /
     réserves / régional (excluded, C.LIVE_SMALL=0), les derniers résultats de chaque équipe pas encore préparée (/games/results/?competitors=…,
     ~4 Ko, 1,5 s d'écart comme le rattrapage 365scores), au plus C.LIVE_PREP_TEAMS équipes par passage et C.LIVE_PREP_SECONDS secondes ; une
     équipe n'est pas relue avant C.LIVE_PREP_KEEP_DAYS jours, et son historique s'ACCUMULE en base (C.LIVE_PREP_HISTORY matchs, jamais effacé).
pred_for(home, away) en tire, sans aucune requête, des buts attendus : attaque et défense de chaque équipe sur ses C.LIVE_PREP_GAMES derniers
matchs, ramenées vers la moyenne (comme un a priori de 4 matchs), avantage du terrain ; qualité au plus 0,5 (« données légères »).
Ne lève jamais."""
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as C, store
from .sources import http, names

FILE = "live_teams.json"        # {"lists": {jour: {"at", "games": [...]}}, "teams": {id: {name, comp, at, res: [[jour, pour, contre, domicile]]}}} : historique gardé EN BASE
AVG = 1.35                      # buts par équipe et par match quand on ne sait rien
PRIOR = 4.0                     # matchs « moyens » ajoutés aux vrais : peu de matchs = estimation tirée vers la moyenne
HOME = 1.10                     # avantage du terrain sur les buts attendus
SMALL = re.compile(r"\bU-?(1[4-9]|2[0-3])\b|youth|junior|juvenil|primavera|reserve|\bres\.|\bII\b|\bIII\b|\bB\b(?!\w)|amateur|regional|regionalliga|oblast|"
                   r"state league|county|\bdistrict|academy", re.I)


def _now():
    return datetime.now(timezone.utc)


CUP = re.compile(r"\bcup\b|coupe|\bcopa\b|pokal|coppa|ta[cç]a|beker|friendl|amical|super ?cup|trophy|shield", re.I)


def cup(*texts):
    """Coupe ou amical : deux équipes de divisions différentes, que des buts marqués « chacune dans son championnat » ne comparent pas."""
    return any(CUP.search(str(t or "")) for t in texts)


def excluded(*texts):
    """Jeunes (U19, U21...), réserves (II, B), amateurs et ligues régionales : exclus du live tant que C.LIVE_SMALL est éteint."""
    return not C.LIVE_SMALL and any(SMALL.search(str(t or "")) for t in texts)


def load():
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("lists", {})
    d.setdefault("teams", {})
    return d


def _day_list(s365, d, day, now):
    """Matchs 365scores du jour `day` (« JJ/MM/AAAA » pour la requête), relus au plus toutes les C.LIVE_PREP_LIST_H heures."""
    key = day.strftime("%Y-%m-%d")
    cur = d["lists"].get(key)
    if cur and now - datetime.fromisoformat(cur["at"]) < timedelta(hours=C.LIVE_PREP_LIST_H):
        return cur["games"]
    res = http.fetch(s365._url("/games/allscores/", sports=1, startDate=day.strftime("%d/%m/%Y"), endDate=day.strftime("%d/%m/%Y"),
                               showOdds="false"), s365.NAME, int(C.LIVE_PREP_LIST_H * 3600))
    data = s365._json(res) if res.ok else None
    if not isinstance(data, dict):
        return (cur or {}).get("games") or []
    comps = {c.get("id"): c.get("name") for c in data.get("competitions") or []}
    games = []
    for g in data.get("games") or []:
        h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
        if h.get("id") and a.get("id") and g.get("startTime"):
            games.append({"id": g.get("id"), "comp": comps.get(g.get("competitionId")) or "", "start": g["startTime"],
                          "home": {"id": h["id"], "name": h.get("name") or ""}, "away": {"id": a["id"], "name": a.get("name") or ""}})
    d["lists"][key] = {"at": now.isoformat(timespec="minutes"), "games": games}
    d["lists"] = dict(sorted(d["lists"].items())[-2:])
    return games


def _results(s365, cid):
    """Derniers matchs terminés de l'équipe 365scores `cid` -> [[pour, contre, 1 si à domicile]] (les plus récents d'abord) ; None si illisible."""
    res = http.fetch(s365._url("/games/results/", competitors=cid), s365.NAME, 12 * 3600)
    data = s365._json(res) if res.ok else None
    if not isinstance(data, dict):
        return None
    out = []
    for g in sorted(data.get("games") or [], key=lambda g: g.get("startTime") or "", reverse=True):
        h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
        hs, as_ = h.get("score"), a.get("score")
        if hs is None or as_ is None or hs < 0 or as_ < 0:
            continue
        home = h.get("id") == cid
        out.append([str(g.get("startTime") or "")[:10], int(hs if home else as_), int(as_ if home else hs), int(home)])
        if len(out) >= C.LIVE_PREP_HISTORY:
            break
    return out


def merge(old, new):
    """Historique d'une équipe : les matchs déjà en base + ceux qui viennent d'être lus, sans doublon, les plus récents d'abord, C.LIVE_PREP_HISTORY
    au plus. Les anciennes lignes [pour, contre, domicile] (sans date) sont gardées derrière."""
    seen, out = set(), []
    for r in sorted(new or [], key=lambda r: r[0], reverse=True) + [r for r in old or [] if len(r) == 4] + [r for r in old or [] if len(r) != 4]:
        k = tuple(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    dated = sorted([r for r in out if len(r) == 4], key=lambda r: r[0], reverse=True)
    return (dated + [r for r in out if len(r) != 4])[:C.LIVE_PREP_HISTORY]


def prepare(now=None, max_teams=None, seconds=None):
    """Passage horaire : prépare les équipes des matchs qui commencent bientôt. -> {"teams": préparées, "skipped": raison ou None}. Ne lève jamais."""
    from .sources import s365
    now = now or _now()
    max_teams = C.LIVE_PREP_TEAMS if max_teams is None else max_teams
    deadline = time.monotonic() + (C.LIVE_PREP_SECONDS if seconds is None else seconds)
    rep = {"teams": 0, "skipped": None}
    if not C.LIVE_PREP or max_teams <= 0:
        rep["skipped"] = "éteint (LIVE_PREP=0)"
        return rep
    try:
        with s365._via_proxy() as why:
            if why:
                rep["skipped"] = why
                return rep
            d = load()
            days = [now.date()] + ([now.date() + timedelta(days=1)] if now.hour >= 20 else [])
            games = [g for day in days for g in _day_list(s365, d, datetime(day.year, day.month, day.day), now)]
            lo, hi = now - timedelta(hours=2), now + timedelta(hours=C.LIVE_PREP_AHEAD_H)     # un match à la 88e a commencé il y a ~1 h 45
            fresh = now - timedelta(days=C.LIVE_PREP_KEEP_DAYS)
            todo = []
            for g in sorted(games, key=lambda g: g["start"]):
                try:
                    ko = datetime.fromisoformat(g["start"])
                except ValueError:
                    continue
                if not (lo <= ko <= hi) or excluded(g["comp"], g["home"]["name"], g["away"]["name"]):
                    continue
                for t in (g["home"], g["away"]):
                    cur = d["teams"].get(str(t["id"]))
                    if (not cur or datetime.fromisoformat(cur["at"]) < fresh) and t["id"] not in [x[0] for x in todo]:
                        todo.append((t["id"], t["name"], g["comp"]))
            http.DELAY_REDUCED[s365.HOST] = C.S365_BACKFILL_DELAY        # comme le rattrapage : 1,5 s (le Crawl-delay du robots.txt reste prioritaire)
            try:
                for cid, name, comp in todo:
                    if rep["teams"] >= max_teams or time.monotonic() > deadline:
                        break
                    res = _results(s365, cid)
                    if res is None:
                        continue
                    old = (d["teams"].get(str(cid)) or {}).get("res")
                    d["teams"][str(cid)] = {"name": name, "comp": comp, "at": now.isoformat(timespec="minutes"), "res": merge(old, res)}
                    rep["teams"] += 1
                    if rep["teams"] % 25 == 0:      # sauvegarde en route : un passage coupé ne perd pas ce qui a été lu
                        store.save(FILE, d, compact=True)
            finally:
                http.DELAY_REDUCED.pop(s365.HOST, None)
            store.save(FILE, d, compact=True)
            rep["waiting"] = max(0, len(todo) - rep["teams"])
    except Exception as e:                          # noqa: BLE001 - facultatif : le live garde les autres sources
        rep["skipped"] = f"{type(e).__name__}: {e}"
    return rep


def report_line(rep):
    if rep.get("skipped"):
        return f"équipes du live : rien préparé ({rep['skipped']})"
    return f"équipes du live : {rep['teams']} préparée(s) (365scores), {rep.get('waiting', 0)} en attente"


def _team(name, teams):
    """L'équipe préparée la plus proche du nom 1xBet `name` (names.best_team) -> sa fiche ; None si inconnue ou ambiguë."""
    by = {}
    for t in teams.values():
        by.setdefault(t["name"], t)
    best = names.best_team(name, list(by), min_sim=0.85)
    return by.get(best) if best else None


def _game(home, away, start, d):
    """Le match 365scores du jour qui correspond au match 1xBet `home` – `away` commencé à `start` (timestamp) : coup d'envoi à C.LIVE_MATCH_MIN
    minutes près, et une équipe qui correspond bien (≥ 0,85) avec l'autre au moins plausible (≥ 0,4), ou les deux à ≥ 0,6 ; seul candidat (sinon
    None). Les noms diffèrent souvent d'un site à l'autre (« CSC 1599 Selimbar » chez 1xBet, « Viitorul Selimbar » chez 365scores) : le match
    entier se reconnaît bien mieux qu'une équipe seule."""
    if not start:
        return None
    ko = datetime.fromtimestamp(start, tz=timezone.utc)
    found = []
    for lst in d.get("lists", {}).values():
        for g in lst.get("games") or []:
            try:
                if abs((datetime.fromisoformat(g["start"]) - ko).total_seconds()) > C.LIVE_MATCH_MIN * 60:
                    continue
            except ValueError:
                continue
            sh, sa = names.team_sim(home, g["home"]["name"]), names.team_sim(away, g["away"]["name"])
            if (max(sh, sa) >= 0.85 and min(sh, sa) >= 0.4) or min(sh, sa) >= 0.6:
                found.append((sh + sa, g))
    found.sort(key=lambda x: -x[0])
    if not found or (len(found) > 1 and found[1][0] >= found[0][0] - 0.05):
        return None
    return found[0][1]


def _rates(res):
    """(attaque, défense) d'une équipe : ses buts marqués et encaissés par match, ramenés vers AVG (PRIOR matchs moyens), en multiples de AVG."""
    n = len(res)
    gf, ga = sum(r[-3] for r in res), sum(r[-2] for r in res)          # [jour, pour, contre, domicile] (ou ancien [pour, contre, domicile])
    return (gf + PRIOR * AVG) / (n + PRIOR) / AVG, (ga + PRIOR * AVG) / (n + PRIOR) / AVG


def pred_for(home, away, data=None, start=None):
    """Buts attendus « légers » du match `home` – `away` (noms 1xBet, coup d'envoi `start` en timestamp) à partir des équipes préparées : {lh, la, q,
    n, src} ; None si l'une des deux n'est pas préparée ou a moins de C.VIABLE_MIN_N matchs. Le match est d'abord retrouvé dans la liste 365scores
    du jour (_game : équipes par leur numéro), sinon chaque équipe par son nom. Aucune requête."""
    d = data if data is not None else load()
    g = _game(home, away, start, d)
    if g:
        th, ta = d["teams"].get(str(g["home"]["id"])), d["teams"].get(str(g["away"]["id"]))
    else:
        th, ta = _team(home, d["teams"]), _team(away, d["teams"])
    rh, ra = (th or {}).get("res", [])[:C.LIVE_PREP_GAMES], (ta or {}).get("res", [])[:C.LIVE_PREP_GAMES]
    if not th or not ta or min(len(rh), len(ra)) < C.VIABLE_MIN_N:
        return None
    (ah, dh), (aa, da) = _rates(rh), _rates(ra)
    n = [len(rh), len(ra)]
    return {"lh": AVG * ah * da * HOME, "la": AVG * aa * dh / HOME, "q": round(0.5 * min(1.0, min(n) / C.LIVE_PREP_GAMES), 3), "n": n,
            "src": "365scores"}


def safe_prepare(**kw):
    """prepare() pour le passage horaire : imprime la ligne de rapport, ne lève jamais."""
    try:
        rep = prepare(**kw)
        print("[live] " + report_line(rep), file=sys.stderr)
        return rep
    except Exception as e:                          # noqa: BLE001
        print(f"[live] préparation ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return None
