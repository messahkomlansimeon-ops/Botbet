"""Suivi de tes tickets PENDANT les matchs et opportunités live (`bot.py --watch`, kelly-watch.timer toutes les 10 minutes).

Tickets : tous les paris notés joués du jour et de la veille (kelly/exposure.py : combinés du matin, pari du jour, combis sur mesure et 🔴 live). Pour
chaque pari, son match est retrouvé dans la liste 365scores du jour (kelly/livedata.py, relue au plus toutes les 6 h), puis seuls tes matchs commencés
sont relus (/games/?games=…, ~1 Ko par match : score, minute, statut), puis :
  - ⚽ un but dans un match d'un pari de buts : « Arsenal 1-0 Leeds (63e) · moins de 2,5 buts : encore 1 but de marge » ;
  - ✅ / ❌ un pari décidé, parfois avant la fin (plus de 2,5 buts gagné au 3e but, moins de 12,5 tirs perdu au 13e) ; stats en direct par
    /game/stats/ (~2 Ko) seulement pour les paris de stats ;
  - match fini (sorti de la liste « en cours » 1 h 55 après le coup d'envoi) : score final par les derniers résultats de l'équipe (~4 Ko) ;
  - 🏆 ticket gagné (avec le gain) ou ❌ ticket perdu, une seule fois ; sinon « 📊 3/8 gagnés, 5 en cours ».
Un seul message par passage, seulement s'il y a du nouveau ; aucune requête s'il n'y a ni match de tes tickets en cours ou à venir, ni opportunité à
chercher. Le règlement officiel (le lendemain matin) ne change pas : ce suivi n'est qu'une information.

Opportunités live : entre C.LIVE_OPP_HOURS, au plus C.LIVE_OPP_DAY par jour et une toutes les C.LIVE_OPP_GAP_H heures : un contrôle léger (365scores :
assez de matchs en cours dans la fenêtre C.LIVE_MIN_MINUTE-C.LIVE_MAX_MINUTE, 30e-80e), puis la liste live de 1xBet (kelly/live.py) ; si un bon combiné live est possible, « 🔴 Opportunité
live » avec des boutons de cote. Heures calmes (kelly/quiet.py) : les messages de la nuit sont gardés pour le réveil. Ne lève jamais."""
import math
import sys
from datetime import datetime, timedelta, timezone

from . import config as C, exposure, fmt, livedata, quiet, store, telegram
from .sources import http, names

FILE = "ticket_watch.json"      # {"legs": {"<ticket>|<pari>": {"state", "score", "gid", "val"}}, "tickets": {ticket: "won"|"lost"}, "opp": {"day", "n", "at"}}
FINISH_MIN = 115                # un match sorti de la liste « en cours » est fini s'il a commencé il y a au moins N minutes
GIVE_UP_H = 8                   # au-delà, plus de recherche de résultat (le règlement du matin s'en charge)
STATS = {"team_shots": "shots", "team_sot": "sot", "team_fouls": "fouls", "team_throws": "throws", "team_offsides": "offsides"}
WORD = {"shots": "tirs", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu", "corners": "corners", "cards": "cartons",
        "yellow": "cartons jaunes"}
ONE = {"shots": "tir", "sot": "tir cadré", "fouls": "faute", "throws": "touche", "offsides": "hors-jeu", "corners": "corner", "cards": "carton",
       "yellow": "carton jaune"}
GOAL_MARKETS = ("totals", "btts", "h2h", "double_chance", "team_totals_home", "team_totals_away")
# statusText 365scores d'un match du groupe 4 qui n'est PAS fini : suspendu (incident, météo), reporté, abandonné, annulé... (le 10/10 : Rayo – Bilbao
# « Suspended » à la 25e, pris pour terminé : paris tranchés et 2 tickets déclarés perdus à tort)
PAUSED = ("suspend", "susp", "postpon", "abandon", "cancel", "interrupt", "delay", "award")


def _now():
    return datetime.now(timezone.utc)


def _ko(c):
    try:
        return datetime.fromisoformat(str(c.get("commence")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load():
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) else {}
    for k, v in (("legs", {}), ("tickets", {}), ("opp", {})):
        d.setdefault(k, v)
    return d


# ---------- marché d'un pari -> ce qu'il faut regarder ----------

def need(leg):
    """(statistique, côté) du pari : ("goals", None), ("corners", "total"), ("shots", "home")... ; (None, None) si Kelly ne sait pas le suivre en direct."""
    m = str(leg.get("market") or "")
    if m in GOAL_MARKETS:
        return "goals", None
    if m == "alternate_totals_corners":
        return "corners", "total"
    if m in ("team_corners_home", "team_corners_away"):          # corners d'UNE équipe (1xBet)
        return "corners", m.rsplit("_", 1)[1]
    if m == "team_yellow_total":                                  # cartons jaunes seuls du match (1xBet)
        return "yellow", "total"
    if m == "alternate_totals_cards":
        return "cards", "total"
    for pre, key in STATS.items():
        if m.startswith(pre + "_"):
            return key, m.rsplit("_", 1)[1]
    return None, None


def state(leg, h, a, stats, finished):
    """État du pari avec le score `h`-`a` (et les stats {clé: (domicile, extérieur)}) : ("win" | "loss" | "live", détail) ; (None, "") si illisible."""
    m, name, point = leg.get("market"), leg.get("name"), leg.get("point")
    key, side = need(leg)
    if key is None:
        return None, ""
    if m == "btts":
        both = h > 0 and a > 0
        if name == "Yes":
            return ("win" if both else "loss" if finished else "live"), f"{h}-{a}"
        return ("loss" if both else "win" if finished else "live"), f"{h}-{a}"
    if m in ("h2h", "double_chance"):
        if not finished:
            return "live", f"{h}-{a}"
        res = leg["home"] if h > a else leg["away"] if a > h else "Draw"
        ok = name == res if m == "h2h" else res in str(name).split(" or ")
        return ("win" if ok else "loss"), f"{h}-{a}"
    if key == "goals":
        val, unit = (h if m == "team_totals_home" else a if m == "team_totals_away" else h + a), "buts"
    else:
        hv, av = stats.get(key) or (None, None)
        if hv is None:
            return None, ""
        val = hv + av if side == "total" else hv if side == "home" else av
        unit = WORD.get(key, key)
    if point is None:
        return None, ""
    detail = f"{h}-{a}" if key == "goals" else f"{val} {ONE.get(key, unit) if val == 1 else unit}"
    if name == "Over":
        return ("win" if val > point else "loss" if finished else "live"), detail
    return ("loss" if val > point else "win" if finished else "live"), detail


def margin(leg, h, a):
    """Où en est un pari de buts en cours, après un but : « encore 1 but de marge » / « il manque 2 buts » (total du match ou d'une équipe),
    « pour toi pour l'instant » / « pas pour toi pour l'instant » (1N2, double chance), « il manque un but de Leeds » (les 2 marquent) ; '' sinon."""
    m = leg.get("market")
    if m in ("h2h", "double_chance"):
        res = leg.get("home") if h > a else leg.get("away") if a > h else "Draw"
        ok = leg.get("name") == res if m == "h2h" else res in str(leg.get("name")).split(" or ")
        return "pour toi pour l'instant" if ok else "pas pour toi pour l'instant"
    if m == "btts":
        if leg.get("name") == "Yes":
            miss = [t for t, g in ((leg.get("home"), h), (leg.get("away"), a)) if g == 0]
            return "il faut un but de chaque équipe" if len(miss) == 2 else f"il manque un but de {miss[0]}" if miss else ""
        return "encore bon tant qu'une équipe reste à 0"
    if m not in ("totals", "team_totals_home", "team_totals_away") or leg.get("point") is None:
        return ""
    total = h if m == "team_totals_home" else a if m == "team_totals_away" else h + a
    p = leg["point"]
    if leg.get("name") == "Under":
        k = math.floor(p - total)
        return f"encore {k} but{'s' if k > 1 else ''} de marge" if k >= 1 else "plus aucun but permis"
    k = math.floor(p - total) + 1
    return f"il manque {k} but{'s' if k > 1 else ''}" if k >= 1 else ""


# ---------- 365scores ----------

def _day_games(s365, now, net=True):
    """Matchs 365scores d'hier et d'aujourd'hui (listes du jour de kelly/livedata.py, relues au plus toutes les 6 h) -> {id: {id, start, home, away}}."""
    ld = livedata.load()
    if net and s365 is not None:
        for day in (now.date() - timedelta(days=1), now.date()):
            livedata._day_list(s365, ld, datetime(day.year, day.month, day.day), now)
        livedata.save_lists(ld)                           # seulement si une liste a été relue, et sans toucher aux équipes préparées
    out = {}
    for lst in ld.get("lists", {}).values():
        for g in lst.get("games") or []:
            out[g["id"]] = {"id": g["id"], "start": g["start"], "home": g["home"], "away": g["away"]}
    return out


def _live(s365, ids, now):
    """Score, minute et statut des matchs `ids` (requête ciblée /games/?games=…, ~1 Ko par match) -> {id: {"live", "ended", "score", "minute"}}."""
    if not ids:
        return {}
    fmt_d = lambda d: d.strftime("%d/%m/%Y")              # noqa: E731
    res = http.fetch(s365._url("/games/", games=",".join(map(str, sorted(ids))), startDate=fmt_d(now - timedelta(days=1)), endDate=fmt_d(now)),
                     s365.NAME, 120)
    return {g.get("id"): _status(g) for g in (((s365.read_json(res) or {}).get("games") or []) if res.ok else [])}


def _status(g):
    """Statut d'un match 365scores -> {"live", "paused", "ended", "score", "minute"}. Groupe 3 = en cours ; groupe 4 = terminé, SAUF si le texte dit
    suspendu / reporté / abandonné (PAUSED) : le match n'est pas fini, son score est celui du moment et rien n'est tranché."""
    h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
    hs, as_ = h.get("score"), a.get("score")
    ok = hs is not None and as_ is not None and hs >= 0 and as_ >= 0
    txt = str(g.get("statusText") or g.get("shortStatusText") or "").lower()
    paused = g.get("statusGroup") == 4 and any(w in txt for w in PAUSED)
    return {"live": g.get("statusGroup") == 3, "paused": paused, "ended": g.get("statusGroup") == 4 and ok and not paused,
            "score": (int(hs), int(as_)) if ok else None, "minute": int(g.get("gameTime") or 0)}


def find(leg, games):
    """Le match 365scores du pari `leg` : coup d'envoi à C.LIVE_MATCH_MIN minutes près et noms plausibles (comme livedata._game) ; None sinon."""
    ko = _ko(leg)
    if not ko:
        return None
    found = []
    for g in games.values():
        try:
            if abs((datetime.fromisoformat(g["start"]) - ko).total_seconds()) > C.LIVE_MATCH_MIN * 60:
                continue
        except (TypeError, ValueError):
            continue
        sh, sa = names.team_sim(leg.get("home") or "", g["home"]["name"] or ""), names.team_sim(leg.get("away") or "", g["away"]["name"] or "")
        if (max(sh, sa) >= 0.85 and min(sh, sa) >= 0.4) or min(sh, sa) >= 0.6:
            found.append((sh + sa, g))
    found.sort(key=lambda x: -x[0])
    if not found or (len(found) > 1 and found[1][0] >= found[0][0] - 0.05):
        return None
    return found[0][1]


def _stats(s365, g):
    """Stats du match (en direct ou fini) -> {clé: (domicile, extérieur)} ; {} si illisible."""
    res = http.fetch(s365._url("/game/stats/", games=g["id"]), s365.NAME, 240)
    data = s365.read_json(res)
    out = {}
    for st in (data or {}).get("statistics") or []:
        key = s365.STAT_NAMES.get(str(st.get("name") or "").lower())
        if not key:
            continue
        try:
            v = int(float(str(st.get("value") or "0").replace("%", "")))
        except ValueError:
            continue
        home = st.get("competitorId") == g["home"]["id"]
        for k in (("cards", "yellow") if key == "yc" else ("cards",) if key == "rc" else (key,)):     # cartons = jaunes + rouges ; jaunes seuls à part
            h, a = out.get(k, (0, 0))
            out[k] = (h + v, a) if home else (h, a + v)
    return out


def _final(s365, g):
    """Score final d'un match terminé, par les derniers résultats de l'équipe à domicile -> (h, a) ou None s'il n'y est pas encore."""
    res = http.fetch(s365._url("/games/results/", competitors=g["home"]["id"]), s365.NAME, 600)
    data = s365.read_json(res)
    for x in (data or {}).get("games") or []:
        if x.get("id") == g["id"]:
            hs, as_ = (x.get("homeCompetitor") or {}).get("score"), (x.get("awayCompetitor") or {}).get("score")
            if hs is not None and as_ is not None and hs >= 0 and as_ >= 0:
                return int(hs), int(as_)
    return None


# ---------- le passage ----------

def _open_legs(ts, d, now):
    """Paris des tickets encore à suivre (pas décidés, match commencé ou commençant dans l'heure, pas trop vieux). Ticket gagné : plus rien à suivre.
    Ticket déjà perdu : ses autres matchs restent suivis (C.WATCH_AFTER_LOSS) ; avec des tickets de 12 paris, un seul pari raté coupait sinon la
    moitié de tes matchs (dimanche 11/10 : 43 matchs sur 86 jamais suivis)."""
    out = []
    for t in ts:
        verdict = d["tickets"].get(t["what"])
        if verdict == "won" or (verdict == "lost" and not C.WATCH_AFTER_LOSS):
            continue
        for leg in t["legs"]:
            k = f"{t['what']}|{leg['key']}"
            ko = _ko(leg)
            if d["legs"].get(k, {}).get("state") in ("win", "loss") or not ko or need(leg)[0] is None:
                continue
            if ko <= now + timedelta(hours=1) and now - ko <= timedelta(hours=GIVE_UP_H):
                out.append((t, leg, k))
    return out


def _ticket_line(t, d):
    """Pied d'un ticket : 🏆 gagné (gain), ❌ perdu, ou « 📊 3/8 gagnés, 5 en cours »."""
    st = [d["legs"].get(f"{t['what']}|{l['key']}", {}).get("state") for l in t["legs"]]
    won = sum(s == "win" for s in st)
    if "loss" in st:
        return "lost", "❌ Ticket perdu."
    if won == len(st):
        gain = (t.get("odds") or 0) * (t.get("stake") or 0)
        from . import money
        return "won", f"🏆 Ticket GAGNÉ ! {fmt.odds_txt(t.get('odds') or 0)} × {money.money(t.get('stake') or 0)} = {money.money(round(gain))}"
    return None, f"📊 {won}/{len(st)} gagnés, {len(st) - won} en cours"


def follow(now=None, games=None, live_for=None, stats_for=None, final_for=None):
    """Suivi des tickets : -> le message (Html) ou None s'il n'y a rien de neuf. `games` ({id: match}), `live_for(ids)`, `stats_for(g)`, `final_for(g)` :
    injectables (tests) ; sinon 365scores via le proxy."""
    from .sources import s365
    now = now or _now()
    d = _load()
    ts = [t for day in (now.date(), now.date() - timedelta(days=1)) for t in exposure.tickets(str(day))]
    todo = _open_legs(ts, d, now)
    if not todo:
        return None
    alive = {t["what"] for t in ts if d["tickets"].get(t["what"]) not in ("won", "lost")}         # tickets encore en jeu avant ce passage
    alive_legs = {leg["key"] for t in ts if t["what"] in alive for leg in t["legs"]}
    # message : chaque pari décidé ou but UNE fois (même dans 7 tickets), puis une ligne par ticket touché (« 🎫 Combi … · 📊 1/7 gagnés, 6 en cours »)
    events, touched = {}, {}                          # ligne -> None (ordre, sans doublon) ; ticket -> t
    with (_NoProxy() if games is not None else s365._via_proxy(settle=True)) as why:     # tes tickets (~1 Ko par match) passent avant la lecture de fond
        if why:
            print(f"[suivi] tickets non suivis : {why}", file=sys.stderr)
            return None
        games = games if games is not None else _day_games(s365, now)
        found = [(t, leg, k, find(leg, games)) for t, leg, k in todo]
        found = [x for x in found if x[3] and _ko(x[1]) <= now]                 # matchs commencés
        if not found:
            store.save(FILE, d)
            return None
        status = (live_for or (lambda ids: _live(s365, ids, now)))({g["id"] for _, _, _, g in found})
        cache_stats, cache_final = {}, {}
        for t, leg, k, g in found:
            ko, prev, st_g = _ko(leg), d["legs"].get(k, {}), status.get(g["id"]) or {}
            paused = st_g.get("paused")
            finished = st_g.get("ended") or (not st_g.get("live") and not paused and now - ko >= timedelta(minutes=FINISH_MIN))
            score = st_g.get("score") if (st_g.get("live") or st_g.get("ended") or paused) else None
            if finished and not score:
                if g["id"] not in cache_final:
                    cache_final[g["id"]] = (final_for or (lambda x: _final(s365, x)))(g)
                score = cache_final[g["id"]]
            if not score:
                continue
            stats = {}
            if need(leg)[0] not in (None, "goals"):
                if g["id"] not in cache_stats:
                    cache_stats[g["id"]] = (stats_for or (lambda x: _stats(s365, x)))(g)
                stats = cache_stats[g["id"]]
            st, detail = state(leg, *score, stats, finished)
            if st is None:
                continue
            sc = f"{score[0]}-{score[1]}"
            line = None
            match = f"{leg['home']} – {leg['away']}"
            if st in ("win", "loss"):
                line = f"{'✅' if st == 'win' else '❌'} {match} : {leg.get('pick')} ({detail}{'' if detail == sc else ', ' + sc})"
            elif leg.get("market") in GOAL_MARKETS and prev.get("score") not in (None, sc):        # un but : tous les paris de buts (pas seulement plus / moins)
                line = f"⚽ {leg['home']} {sc} {leg['away']} ({st_g.get('minute')}e{', suspendu' if paused else ''}) · {leg.get('pick')} : {margin(leg, *score)}".rstrip(" :")
            d["legs"][k] = {"state": st, "score": sc, "gid": g["id"], "at": now.isoformat(timespec="minutes")}
            if line:
                if leg["key"] not in alive_legs:          # pari seulement dans des tickets déjà perdus : dit, mais sans compter
                    line += " · ticket déjà perdu"
                events[line] = None
                if t["what"] in alive:                    # pied de ticket : seulement les tickets encore en jeu (« ❌ Ticket perdu » une seule fois)
                    touched[t["what"]] = t
    feet = []
    for what, t in touched.items():
        verdict, foot = _ticket_line(t, d)
        if verdict:
            d["tickets"][what] = verdict
        feet.append(f"🎫 {fmt.b(fmt.esc(t['name']))} · {fmt.esc(foot)}")
    store.save(FILE, d)
    return telegram.Html("\n".join(fmt.esc(x) for x in events) + ("\n\n" + "\n".join(feet) if feet else "")) if events else None


class _NoProxy:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def opportunity(now=None, current=None, get=None):
    """🔴 Opportunité live : -> le message ou None (hors des heures, plafond du jour, trop tôt après la précédente, pas assez de matchs, rien de bon)."""
    from . import combos as CB, live
    now = now or _now()
    if not C.LIVE_OPP or quiet.is_quiet(now):
        return None
    lo, hi = (int(x) for x in str(C.LIVE_OPP_HOURS).split("-"))
    if not lo <= now.hour < hi:
        return None
    d = _load()
    opp = d["opp"] if d["opp"].get("day") == str(now.date()) else {"day": str(now.date()), "n": 0, "at": None}
    if opp["n"] >= C.LIVE_OPP_DAY or (opp["at"] and now - datetime.fromisoformat(opp["at"]) < timedelta(hours=C.LIVE_OPP_GAP_H)):
        return None
    if current is None:                              # contrôle léger d'abord (365scores, ~29 Ko) : assez de matchs dans la fenêtre ?
        from .sources import s365
        with s365._via_proxy() as why:
            if why:
                return None
            res = http.fetch(s365._url("/games/current/", sports=1), s365.NAME, 240)
            current = ((s365.read_json(res) or {}).get("games") or []) if res.ok else []
    window = [g for g in current if g.get("statusGroup") == 3 and C.LIVE_MIN_MINUTE <= int(g.get("gameTime") or 0) <= C.LIVE_MAX_MINUTE]
    if len(window) < C.LIVE_OPP_GATE:
        return None
    raw, why = live.fetch(get)
    if why:
        return None
    cands, seen, count = live.legs(raw, now=now)
    try:
        live.record(cands, now)                     # seulement les paris retenus (un côté par marché) : jamais les paris seulement évalués
    except Exception:                               # noqa: BLE001
        pass
    matches = {c["id"] for c in cands}
    top = min(CB.reach(cands, C.LIVE_COMBO_MAX_LEGS), C.LIVE_COMBO_MAX_ODDS)       # combiné live : 4 paris et cote 20 au plus
    if len(matches) < C.LIVE_OPP_MATCHES or top < 3:
        return None
    opp.update(n=opp["n"] + 1, at=now.isoformat(timespec="minutes"))
    d["opp"] = opp
    store.save(FILE, d)
    targets = [t for t in (3, 5, 10, 20) if t <= top] or [3]
    msg = telegram.Html(fmt.b("🔴 Opportunité live") + "\n" + fmt.esc(f"{len(matches)} matchs en cours avec un bon pari (cote possible jusqu'à "
                                                                      f"{fmt.trim(top, 1)}). Touche une cote :"))
    return telegram.with_buttons(msg, telegram.inline([[(f"cote {t}", f"live|{t}") for t in targets]]))


def tick(now=None):
    """Le passage de 10 minutes : alertes gardées la nuit (quiet.flush), suivi des tickets, opportunité live. Ne lève jamais."""
    now = now or _now()
    for name, fn in (("alertes de la nuit", lambda: quiet.flush(now)), ("tickets", lambda: follow(now)), ("opportunité live", lambda: opportunity(now))):
        try:
            msg = fn()
            if msg and not isinstance(msg, int):
                quiet.send(msg, now)
                print(f"[suivi] {name} : message envoyé", file=sys.stderr)
        except Exception as e:                      # noqa: BLE001 - une partie en panne n'empêche pas les autres
            print(f"[suivi] {name} ignoré : {http.err(e)}", file=sys.stderr)
