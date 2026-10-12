"""Données « légères » des équipes que Kelly ne connaît pas, préparées À L'AVANCE pour le bouton 🔴 Live (kelly/live.py).

Au moment d'un appui, lire l'historique de 40 équipes prendrait des minutes : les cotes live auraient changé. Ce module le fait au passage horaire
(app.fill_match_stats), via 365scores et le proxy (s365._via_proxy, son plafond du mois) :
  1. la liste des matchs du jour (/games/allscores/, ~180 Ko, relue toutes les C.LIVE_PREP_LIST_H heures) : tous les championnats, avec le numéro
     365scores de chaque équipe ;
  2. pour les matchs commencés depuis moins de 2 h ou qui commencent dans les C.LIVE_PREP_AHEAD_H prochaines heures (les plus proches d'abord), hors jeunes /
     réserves / régional (excluded, C.LIVE_SMALL=0), les derniers résultats de chaque équipe pas encore préparée (/games/results/?competitors=…,
     ~4 Ko, 1,5 s d'écart comme le rattrapage 365scores), au plus C.LIVE_PREP_TEAMS équipes par passage et C.LIVE_PREP_SECONDS secondes ; une
     équipe n'est pas relue avant C.LIVE_PREP_KEEP_DAYS jours, et son historique s'ACCUMULE en base (C.LIVE_PREP_HISTORY matchs) tant qu'elle est relue ; oubliée après PURGE_DAYS jours sans relecture.
Bornes : une équipe non relue depuis PURGE_DAYS jours est oubliée (le fichier ne grossit plus sans fin) ; le fichier n'est
réécrit que si quelque chose a changé (SAVE_EVERY équipes au plus entre deux sauvegardes) ; une réponse illisible est retirée du cache ; une équipe dont la
lecture échoue est mise de côté (FAIL_MAX échecs espacés de FAIL_GAP_H heures), le passage s'arrête après MAX_ERRORS erreurs d'affilée, et ses requêtes sont
comptées contre le plafond du jour (S365_DAILY + DAILY_EXTRA) et celui du mois (S365_MONTH_MB moins la réserve du règlement, relu pendant le passage).
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
# Réglages de ce module (config.py n'est pas touché ; chacun a sa variable d'environnement)
PURGE_DAYS = http.env("LIVE_PREP_PURGE_DAYS", 60)       # équipe non relue depuis N jours : oubliée
SAVE_EVERY = http.env("LIVE_PREP_SAVE_EVERY", 50)       # sauvegarde en route toutes les N équipes lues (et une seule fois à la fin, si quelque chose a changé)
MAX_ERRORS = http.env("LIVE_PREP_MAX_ERRORS", 3)        # erreurs de lecture d'affilée avant d'arrêter le passage
FAIL_MAX = http.env("LIVE_PREP_FAIL_MAX", 3)            # échecs d'une même équipe avant de la laisser de côté 24 h
FAIL_GAP_H = http.env("LIVE_PREP_FAIL_GAP_H", 2.0)      # écart minimal entre deux essais d'une équipe en échec (heures)
DAILY_EXTRA = http.env("LIVE_PREP_DAILY", 600)          # requêtes du jour permises au live EN PLUS de S365_DAILY (remplissage + live comptés ensemble)
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
    for k in ("lists", "teams", "fail"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    if not isinstance(d.get("req"), dict):
        d["req"] = {"day": "", "n": 0}
    return d


def _when(iso):
    """Instant (UTC) d'une date ISO, naïve ou non ; None si illisible."""
    try:
        t = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def purge(d, now):
    """Oublie les équipes non préparées depuis PURGE_DAYS jours (ou sans date lisible), les échecs de plus de 3 jours. -> nombre d'équipes oubliées."""
    cut = now - timedelta(days=PURGE_DAYS)
    keep = {k: v for k, v in d["teams"].items() if isinstance(v, dict) and (_when(v.get("at")) or cut) > cut}
    gone = len(d["teams"]) - len(keep)
    d["teams"] = keep
    d["fail"] = {k: v for k, v in d["fail"].items() if isinstance(v, list) and len(v) == 2 and (_when(v[1]) or cut) > now - timedelta(days=3)}
    return gone


def save_lists(d):
    """Écrit les seules listes du jour (watch.py, sans verrou) : le document est RELU avant d'écrire, pour ne jamais effacer les équipes qu'une préparation
    en cours (autre processus) vient d'ajouter. N'écrit rien si les listes n'ont pas changé."""
    cur = load()
    if cur["lists"] == d["lists"]:
        return False
    cur["lists"] = d["lists"]
    store.save(FILE, cur, compact=True)
    return True


def _day_list(s365, d, day, now):
    """Matchs 365scores du jour `day` (« JJ/MM/AAAA » pour la requête), relus au plus toutes les C.LIVE_PREP_LIST_H heures."""
    key = day.strftime("%Y-%m-%d")
    cur = d["lists"].get(key)
    at = _when(cur.get("at")) if cur else None
    if cur and at and now - at < timedelta(hours=C.LIVE_PREP_LIST_H):
        return cur["games"]
    url = s365._url("/games/allscores/", sports=1, startDate=day.strftime("%d/%m/%Y"), endDate=day.strftime("%d/%m/%Y"), showOdds="false")
    res = http.fetch(url, s365.NAME, int(C.LIVE_PREP_LIST_H * 3600))
    data = s365.read_json(res, url)
    if not isinstance(data, dict) or not isinstance(data.get("games"), list):
        if res.ok and not getattr(res, "stale", False):
            http.invalidate(url)                      # 200 sans la liste attendue : pas gardé 6 h
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
    url = s365._url("/games/results/", competitors=cid)
    res = http.fetch(url, s365.NAME, 12 * 3600)
    data = s365.read_json(res, url)             # un 200 qui n'est pas du JSON est retiré du cache (sinon resservi 12 h)
    if not isinstance(data, dict) or not isinstance(data.get("games"), list):
        if res.ok and not getattr(res, "stale", False):
            http.invalidate(url)
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
    """Passage horaire : prépare les équipes des matchs qui commencent bientôt. -> {"teams": préparées, "skipped": raison ou None}. Ne lève jamais.
    Sauvegarde : toutes les SAVE_EVERY équipes lues et une fois à la fin, SEULEMENT si quelque chose a changé (équipe lue, liste du jour relue, équipe
    oubliée). Arrêt anticipé (rep["stopped"]) : MAX_ERRORS erreurs de lecture d'affilée (disjoncteur), plafond de requêtes du jour (S365_DAILY +
    DAILY_EXTRA, remplissage 365scores compris) ou du mois (s365.month_left, relu à chaque équipe)."""
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
            wire0 = http.WIRE.get(s365.HOST, {"requests": 0})["requests"]      # avant la liste du jour : ses requêtes comptent aussi
            dirty = purge(d, now) > 0
            before = {k: v.get("at") for k, v in d["lists"].items()}
            days = [now.date()] + ([now.date() + timedelta(days=1)] if now.hour >= 20 else [])
            games = [g for day in days for g in _day_list(s365, d, datetime(day.year, day.month, day.day), now)]
            dirty = dirty or before != {k: v.get("at") for k, v in d["lists"].items()}
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
                    at = _when(cur.get("at")) if cur else None
                    if (not cur or not at or at < fresh) and t["id"] not in [x[0] for x in todo] and not _resting(d, t["id"], now):
                        todo.append((t["id"], t["name"], g["comp"]))
            day = str(now.date())
            live0 = d["req"]["n"] if d["req"].get("day") == day else 0       # requêtes du live déjà comptées aujourd'hui
            used0 = live0 + _s365_used(s365)
            cap = C.S365_DAILY + DAILY_EXTRA
            made = lambda: http.WIRE.get(s365.HOST, {"requests": 0})["requests"] - wire0      # noqa: E731 - requêtes de CE passage (listes du jour comprises)
            errs = 0

            def stamp():
                d["req"] = {"day": day, "n": live0 + made()}
                return d
            http.DELAY_REDUCED[s365.HOST] = C.S365_BACKFILL_DELAY        # comme le rattrapage : 1,5 s (le Crawl-delay du robots.txt reste prioritaire)
            try:
                for cid, name, comp in todo:
                    if rep["teams"] >= max_teams or time.monotonic() > deadline:
                        break
                    left = s365.month_left()
                    if left is not None and left <= 0:
                        rep["stopped"] = "plafond mensuel 365scores atteint"
                        break
                    if used0 + made() >= cap:
                        rep["stopped"] = f"plafond du jour atteint ({used0 + made()} requêtes 365scores)"
                        break
                    res = _results(s365, cid)
                    if res is None:
                        _fail(d, cid, now)
                        dirty = True
                        errs += 1
                        if errs >= MAX_ERRORS:          # disjoncteur : proxy ou 365scores en panne, inutile d'insister sur les équipes restantes
                            rep["stopped"] = f"{errs} erreurs de suite (365scores ou proxy)"
                            break
                        continue
                    errs = 0
                    d["fail"].pop(str(cid), None)
                    old = (d["teams"].get(str(cid)) or {}).get("res")
                    d["teams"][str(cid)] = {"name": name, "comp": comp, "at": now.isoformat(timespec="minutes"), "res": merge(old, res)}
                    rep["teams"] += 1
                    dirty = True
                    if rep["teams"] % SAVE_EVERY == 0:      # sauvegarde en route : un passage coupé ne perd pas ce qui a été lu
                        store.save(FILE, stamp(), compact=True)
            finally:
                http.DELAY_REDUCED.pop(s365.HOST, None)
            if dirty or made():
                store.save(FILE, stamp(), compact=True)
            rep["waiting"] = max(0, len(todo) - rep["teams"])
    except Exception as e:                          # noqa: BLE001 - facultatif : le live garde les autres sources
        rep["skipped"] = http.err(e)
    return rep


def _s365_used(s365):
    """Requêtes du jour déjà comptées par le remplissage 365scores (s365_matches.json) : elles comptent avec celles du live contre le plafond du jour."""
    try:
        return s365._used_today(s365.load())
    except Exception:                               # noqa: BLE001 - un compteur illisible ne bloque pas le live
        return 0


def _fail(d, cid, now):
    """Mémorise l'échec de lecture de l'équipe `cid` : [nombre d'échecs, instant] (voir _resting)."""
    n = (d["fail"].get(str(cid)) or [0, ""])[0] + 1
    d["fail"][str(cid)] = [n, now.isoformat(timespec="minutes")]


def _resting(d, cid, now):
    """L'équipe `cid` est-elle mise de côté après un échec récent ? Réessayée après FAIL_GAP_H heures ; après FAIL_MAX échecs, après 24 h."""
    f = d["fail"].get(str(cid))
    t = _when(f[1]) if isinstance(f, list) and len(f) == 2 else None
    if not t:
        return False
    return now - t < timedelta(hours=24 if f[0] >= FAIL_MAX else FAIL_GAP_H)


def report_line(rep):
    if rep.get("skipped"):
        return f"équipes du live : rien préparé ({rep['skipped']})"
    return f"équipes du live : {rep['teams']} préparée(s) (365scores), {rep.get('waiting', 0)} en attente"


def _index(teams):
    """Index des équipes préparées : {nom normalisé: [(id, fiche)]} et {mot / début de mot: {noms normalisés}}, pour retrouver une équipe sans comparer son nom
    aux 10 000 autres (une recherche par similarité coûtait ~1 s)."""
    by, tok = {}, {}
    for tid, t in teams.items():
        nm = names.norm(t.get("name")) if isinstance(t, dict) else ""
        if not nm:
            continue
        by.setdefault(nm, []).append((tid, t))
    for nm in by:
        for w in nm.split():
            if len(w) >= 3:
                tok.setdefault(w, set()).add(nm)
                tok.setdefault("~" + w[:4], set()).add(nm)
    return {"by": by, "tok": tok}


_MEMO = {"ver": None, "data": None, "index": None}


def _cached():
    """(document, index) du fichier, relus seulement quand le document change dans la base (store.version) : pred_for est appelé pour chaque match live."""
    ver = store.version(FILE)
    if ver is None or _MEMO["ver"] != ver or _MEMO["data"] is None:
        data = load()
        _MEMO.update(ver=ver, data=data, index=_index(data["teams"]))
    return _MEMO["data"], _MEMO["index"]


def _comp_words(text):
    return {w for w in names.norm(text).split() if len(w) >= 4 and w not in ("league", "liga", "division", "first", "second", "cup")}


def _pick(group, league):
    """Une fiche parmi des équipes de même nom (id, fiche) : l'unique, sinon celle dont le championnat ressemble à `league` (mots communs, et une seule), sinon
    None (homonymes de pays différents : jamais mélangés, on ne devine pas)."""
    if len(group) == 1:
        return group[0][1]
    want = _comp_words(league)
    if not want:
        return None
    scored = sorted(((len(want & _comp_words(t.get("comp"))), i) for i, (_, t) in enumerate(group)), reverse=True)
    if scored[0][0] > 0 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return group[scored[0][1]][1]
    return None


def _team(name, teams, league=None, index=None):
    """L'équipe préparée la plus proche du nom 1xBet `name` (names.best_team) -> sa fiche ; None si inconnue ou ambiguë. Le nom exact (normalisé) d'abord,
    sinon la similarité sur les seuls noms qui partagent un mot (ou un début de mot) avec `name`. Deux équipes de même nom (pays différents) : départagées
    par le championnat `league` si possible, sinon refusées."""
    idx = index or _index(teams)
    by, tok = idx["by"], idx["tok"]
    exact = by.get(names.norm(name))
    if exact:
        return _pick(exact, league)
    cands = set()
    for w in names.norm(name).split():
        if len(w) >= 3:
            cands |= tok.get(w, set()) | tok.get("~" + w[:4], set())
    shown = {(by[nm][0][1].get("name") or nm): nm for nm in cands}
    best = names.best_team(name, list(shown), min_sim=0.85) if shown else None
    return _pick(by[shown[best]], league) if best else None


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


def pred_for(home, away, data=None, start=None, league=None):
    """Buts attendus « légers » du match `home` – `away` (noms 1xBet, coup d'envoi `start` en timestamp) à partir des équipes préparées : {lh, la, q,
    n, src} ; None si l'une des deux n'est pas préparée ou a moins de C.VIABLE_MIN_N matchs. Le match est d'abord retrouvé dans la liste 365scores
    du jour (_game : équipes par leur numéro), sinon chaque équipe par son nom (_team : index par nom, homonymes de pays différents refusés ou
    départagés par `league`, le championnat 1xBet). Aucune requête."""
    d, idx = (data, None) if data is not None else _cached()
    g = _game(home, away, start, d)
    if g:
        th, ta = d["teams"].get(str(g["home"]["id"])), d["teams"].get(str(g["away"]["id"]))
    else:
        idx = idx or _index(d["teams"])
        th, ta = _team(home, d["teams"], league, idx), _team(away, d["teams"], league, idx)
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
