"""Suivi de tes tickets PENDANT les matchs et opportunités live (`bot.py --watch`, kelly-watch.timer toutes les 10 minutes).

Tickets : tous les paris notés joués du jour et de la veille (kelly/exposure.py : combinés du matin, pari du jour, combis sur mesure et 🔴 live). Pour
chaque pari, son match est retrouvé dans la liste 365scores du jour (kelly/livedata.py, relue au plus toutes les 6 h), puis seuls tes matchs commencés
sont relus (/games/?games=…, ~1 Ko par match : score, minute, statut), puis :
  - ⚽ un but dans un match d'un pari de buts : « Arsenal 1-0 Leeds (63e) · moins de 2,5 buts : encore 1 but de marge » ;
  - ✅ / ❌ un pari décidé, parfois avant la fin (plus de 2,5 buts gagné au 3e but, moins de 12,5 tirs perdu au 13e) ; stats en direct par
    /game/stats/ (~2 Ko) seulement pour les paris de stats ;
  - match fini (sorti de la liste « en cours » 1 h 55 après le coup d'envoi) : score final par les derniers résultats de l'équipe (~4 Ko) ;
  - 🏆 ticket gagné (avec le gain) ou ❌ ticket perdu, une seule fois ; sinon « 📊 3/8 gagnés, 5 en cours ».
  - 🟢 coup d'envoi de chaque match de tes tickets, avec ses paris et leurs coupons (C.WATCH_KICKOFF) ; un match reste suivi jusqu'à la fin, même
    quand ses paris sont décidés (décision provisoire, revue jusqu'au coup de sifflet final) ;
  - par coupon, un message À PART (en réponse au message du coupon quand Kelly le connaît, avec son bouton « 🎫 ») : ⏰ « Dans 5 min » avant son 1er
    match (matchs à la même heure groupés ; le runner déclenche le passage à l'heure voulue, data/watch_next.json), 🔥 « plus qu'un match », 🎉 coupon
    gagné (paris, scores, gain, capital et objectif du mois) ; sous le message groupé, un bouton « 🎫 » par coupon concerné : sa fiche à jour ;
  - 📌 tableau du jour (board, C.WATCH_BOARD) : UN message épinglé, modifié sur place à chaque passage où quelque chose change, avec tous tes coupons ;
  - 365scores muet (proxy en panne, 365scores en panne ou bloqué) : SECOURS FotMob (C.WATCH_FM_BACKUP) : une page de championnat par passage pour tes
    matchs en cours, lue en direct (FotMob n'a pas besoin du proxy) : scores, mi-temps, fins de match et paris de buts ; ni buteur, ni corners, ni
    tirs (au règlement du matin). L'avertissement dit la vraie cause (« le proxy ne répond pas », « 365scores en panne (erreur 504) ») et le secours.
Un seul message par passage, seulement s'il y a du nouveau ; aucune requête s'il n'y a ni match de tes tickets en cours ou à venir, ni opportunité à
chercher. Le règlement officiel (le lendemain matin) ne change pas : ce suivi n'est qu'une information.

Opportunités live : entre C.LIVE_OPP_HOURS, au plus C.LIVE_OPP_DAY par jour et une toutes les C.LIVE_OPP_GAP_H heures : un contrôle léger (365scores :
assez de matchs en cours dans la fenêtre C.LIVE_MIN_MINUTE-C.LIVE_MAX_MINUTE, 30e-80e), puis la liste live de 1xBet (kelly/live.py) ; si un bon combiné live est possible, « 🔴 Opportunité
live » avec des boutons de cote. Heures calmes (kelly/quiet.py) : les messages de la nuit sont gardés pour le réveil. Ne lève jamais."""
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as C, exposure, fmt, inplay, livedata, quiet, store, telegram
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


class WatchStateError(Exception):
    """Le document du suivi (ticket_watch.json) n'a pas pu être lu : le passage ne fait RIEN (sinon il croirait tout neuf et renverrait tous les
    ✅ / ❌ / 🏆 déjà annoncés)."""


V = 2                           # version du document : verdicts des tickets par identifiant daté (tid), santé, lignes à envoyer
KEEP_DAYS = 3                   # paris, tickets et drapeaux oubliés au-delà
DEFAULTS = (("legs", {}), ("tickets", {}), ("games", {}), ("flags", {}), ("out", []), ("health", {}), ("budget", {}), ("recap", {}), ("opp", {}),
            ("board", {}))


def _load():
    """Document du suivi, version 2 ; lève WatchStateError si la base ne peut pas le lire. Migration : les anciens verdicts rangés par NOM de ticket
    (« vm1 », « pari » : le combi m1 d'aujourd'hui héritait de celui d'hier) sont oubliés ; rien n'est ré-annoncé."""
    try:
        d = store.load(FILE, {}, strict=True)
    except OSError as e:
        raise WatchStateError(str(e)) from None
    d = d if isinstance(d, dict) else {}
    for k, v in DEFAULTS:
        if not isinstance(d.get(k), type(v)):
            d[k] = type(v)()
    if d.get("v") != V:
        d["tickets"] = {k: v for k, v in d["tickets"].items() if "|" in k}
        d["v"] = V
    return d


def _purge(d, now):
    """Oublie ce qui a plus de KEEP_DAYS jours (paris, tickets, drapeaux), les matchs de plus de 2 jours et les lignes jamais envoyées trop vieilles."""
    cut = (now - timedelta(days=KEEP_DAYS)).isoformat()
    d["legs"] = {k: v for k, v in d["legs"].items() if str(v.get("at") or "9") >= cut}
    day_cut = str((now - timedelta(days=KEEP_DAYS)).date())
    d["tickets"] = {k: v for k, v in d["tickets"].items() if k[:10] >= day_cut}
    d["flags"] = {k: v for k, v in d["flags"].items() if k[:10] >= day_cut}
    gcut = (now - timedelta(days=2)).isoformat()
    d["games"] = {k: v for k, v in d["games"].items() if str((v or {}).get("at") or "9") >= gcut}
    ocut = (now - timedelta(hours=C.OUTBOX_MAX_HOURS)).isoformat()
    d["out"] = [o for o in d["out"] if str(o.get("at") or "9") >= ocut]


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


_WHY = {"live": ""}             # cause du dernier échec de _live, en mots simples (http.cause), pour l'avertissement


def _live(s365, ids, now):
    """Score, minute et statut des matchs `ids` (requête ciblée /games/?games=…, ~1 Ko par match) -> {id: {"live", "ended", "score", "minute"}} ;
    None si 365scores n'a pas répondu (ou seulement par une vieille copie) : la cause va dans _WHY["live"]."""
    if not ids:
        return {}
    fmt_d = lambda d: d.strftime("%d/%m/%Y")              # noqa: E731
    res = http.fetch(s365._url("/games/", games=",".join(map(str, sorted(ids))), startDate=fmt_d(now - timedelta(days=1)), endDate=fmt_d(now)),
                     s365.NAME, 120)
    if not _fresh(res):                                   # erreur, source bloquée, ou vieille copie : AUCUNE donnée (jamais un score périmé pris pour le direct)
        _WHY["live"] = http.cause(getattr(res, "error", None)) if res is not None else "365scores ne répond pas"
        return None
    return {g.get("id"): _status(g) for g in ((s365.read_json(res) or {}).get("games") or [])}


def _fresh(res):
    """Réponse utilisable comme donnée du moment : reçue, ni périmée (copie de secours après une erreur), ni d'une source bloquée."""
    return bool(res is not None and res.ok and not res.stale and not res.blocked)


ET_WORDS = ("extra", "penalt", "aet", "a.e.t", "after pen", "e.t.")


def _status(g):
    """Statut d'un match 365scores -> {"live", "paused", "ended", "score", "minute", "period", "ht", "et", "red", "text"}. Groupe 3 = en cours ; groupe 4 =
    terminé, SAUF si le texte dit suspendu / reporté / abandonné (PAUSED) : le match n'est pas fini, son score est celui du moment et rien n'est tranché.
    `ht` : mi-temps (« Halftime » / HT) ; `et` : prolongation ou tirs au but (les paris se règlent sur le score à 90 min) ; `red` : cartons rouges
    (domicile, extérieur) si 365scores les donne, sinon None (inconnu, pas zéro)."""
    h, a = g.get("homeCompetitor") or {}, g.get("awayCompetitor") or {}
    hs, as_ = h.get("score"), a.get("score")
    ok = hs is not None and as_ is not None and hs >= 0 and as_ >= 0
    txt = str(g.get("statusText") or g.get("shortStatusText") or "").lower()
    short = str(g.get("shortStatusText") or "").lower()
    paused = g.get("statusGroup") == 4 and any(w in txt for w in PAUSED)
    minute = int(g.get("gameTime") or 0)
    ht = txt == "halftime" or short == "ht"
    et = any(w in txt for w in ET_WORDS) or short in ("aet", "a.e.t.", "pen", "pen.", "et", "et ht") or minute >= 106
    red = None
    if "redCards" in h or "redCards" in a:
        red = (int(h.get("redCards") or 0), int(a.get("redCards") or 0))
    period = 1 if ("1st" in txt or ht or (minute <= 45 and "2nd" not in txt)) else 2
    return {"live": g.get("statusGroup") == 3, "paused": paused, "ended": g.get("statusGroup") == 4 and ok and not paused,
            "score": (int(hs), int(as_)) if ok else None, "minute": minute, "period": period, "ht": ht, "et": bool(et), "red": red,
            "text": g.get("statusText") or ""}


# ---------- fiche détaillée d'un match (buteur, penalty, carton rouge, VAR, score à 90 min) : aux moments clés seulement ----------

EV_KINDS = {"goal": "goal", "red card": "red", "second yellow": "red", "goal disallowed": "var", "missed penalty": "pen_miss", "penalty missed": "pen_miss"}


def parse_detail(payload):
    """Fiche /game/?gameId=… -> {"events": [{"id", "kind" (goal / red / var / pen_miss), "sub" (« Penalty », « Own Goal »…), "side", "min", "player"}],
    "s90": (h, a) score à la fin des 90 minutes ou None, "ht": score à la mi-temps ou None, "text"} ; None si illisible."""
    g = (payload or {}).get("game") if isinstance(payload, dict) else None
    if not isinstance(g, dict):
        return None
    names = {m.get("id"): m.get("shortName") or m.get("name") for m in g.get("members") or [] if isinstance(m, dict)}
    hid = (g.get("homeCompetitor") or {}).get("id")
    evs = []
    for e in g.get("events") or []:
        et = e.get("eventType") or {}
        kind = EV_KINDS.get(str(et.get("name") or "").strip().lower())
        if not kind:
            continue
        evs.append({"id": f"{et.get('id')}|{e.get('gameTime')}|{e.get('addedTime')}|{e.get('playerId')}", "kind": kind, "sub": et.get("subTypeName") or "",
                    "side": "home" if e.get("competitorId") == hid else "away", "min": e.get("gameTimeDisplay") or f"{int(e.get('gameTime') or 0)}'",
                    "player": names.get(e.get("playerId"))})
    stages = {s.get("id"): s for s in g.get("stages") or [] if isinstance(s, dict)}

    def sc(st):
        h, a = (st or {}).get("homeCompetitorScore"), (st or {}).get("awayCompetitorScore")
        return (int(h), int(a)) if h is not None and a is not None and h >= 0 and a >= 0 else None
    return {"events": evs, "s90": sc(stages.get(9)), "ht": sc(stages.get(7)), "text": g.get("statusText") or ""}


def _detail(s365, g):
    """Fiche détaillée du match `g` (~16 Ko) -> parse_detail, ou None (pas de réponse fraîche)."""
    res = http.fetch(s365._url("/game/", gameId=g["id"]), s365.NAME, 120)
    return parse_detail(s365.read_json(res)) if _fresh(res) else None


def _budget_ok(d, now, settle=False):
    """Une fiche détaillée de plus est-elle permise ? Plafond du jour C.WATCH_DETAIL_DAY (sauf `settle` : score à 90 min d'une prolongation, qui règle
    tes paris) et données du mois (s365.month_left)."""
    from .sources import s365
    b = d["budget"]
    if b.get("day") != str(now.date()):
        d["budget"] = b = {"day": str(now.date()), "detail": 0}
    left = s365.month_left()
    if left is not None and left <= 0:
        return False
    return settle or int(b.get("detail") or 0) < C.WATCH_DETAIL_DAY


# ---------- secours FotMob : 365scores muet ----------

def _fm_league(leg):
    """Identifiant FotMob du championnat du pari (fotmob.SPORT_TO_LEAGUE / EXTRA_LEAGUES), ou None."""
    from .sources import fotmob
    sk = leg.get("sport_key") or ""
    return fotmob.EXTRA_LEAGUES.get(sk) or fotmob.SPORT_TO_LEAGUE.get(sk)


FM_PAUSED = ("PP", "AB", "ABD", "SUSP", "INT", "CANC", "POSTP")


def _fm_status(st):
    """Statut d'un match lu sur une page de championnat FotMob -> même forme que _status (365scores) ; cartons rouges inconnus (None)."""
    started, finished = bool(st.get("started")), bool(st.get("finished"))
    reason = str((st.get("reason") or {}).get("short") or "").strip().upper()
    live_t = str((st.get("liveTime") or {}).get("short") or "").strip()
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", str(st.get("scoreStr") or ""))
    score = (int(m.group(1)), int(m.group(2))) if m else None
    mm = re.match(r"(\d+)", live_t)
    minute = int(mm.group(1)) if mm else 0
    ht = reason == "HT" or live_t.upper() == "HT"
    paused = bool(st.get("cancelled")) or reason in FM_PAUSED
    live = started and not finished and not paused
    ended = finished and score is not None and not paused and not st.get("awarded")
    return {"live": live, "paused": paused and started, "ended": ended, "score": score if (live or ended or (paused and started)) else None,
            "minute": minute or (45 if ht else 0), "period": 1 if (ht or 0 < minute <= 45) else 2, "ht": ht,
            "et": reason in ("AET", "PEN", "AP", "ET") or minute >= 106, "red": None, "text": reason or live_t}


def _fm_games(todo, now):
    """Matchs de tes paris lus sur les pages de championnat FotMob (en direct, sans proxy), au plus C.WATCH_FM_LEAGUES pages (les championnats qui ont
    le plus de tes matchs commencés d'abord), chacune relue au plus toutes les C.WATCH_FM_TTL_S s -> ({id « fm… » : match au format de _day_games,
    statut dans « fm »}, cause d'échec en mots simples ou '')."""
    from .sources import fmmatch, fotmob
    by_lg = {}
    for _, leg, _ in todo:
        lg = _fm_league(leg)
        if lg and _ko(leg) and _ko(leg) <= now:
            by_lg[lg] = by_lg.get(lg, 0) + 1
    if not by_lg:
        return {}, "championnats absents de FotMob"
    games, errs = {}, []
    for lg in sorted(by_lg, key=lambda x: -by_lg[x])[:C.WATCH_FM_LEAGUES]:
        res = http.fetch(f"{fotmob.BASE}/leagues/{lg}", fotmob.NAME, C.WATCH_FM_TTL_S)
        if not _fresh(res):
            errs.append(http.cause(getattr(res, "error", None), "FotMob"))
            continue
        for m in (((fmmatch._next_data(res.text) or {}).get("fixtures") or {}).get("allMatches") or []):
            st, h, a = m.get("status") or {}, m.get("home") or {}, m.get("away") or {}
            if not m.get("id") or not st.get("utcTime"):
                continue
            gid = f"fm{m['id']}"
            games[gid] = {"id": gid, "start": str(st["utcTime"]).replace("Z", "+00:00"), "home": {"id": h.get("id"), "name": h.get("name") or ""},
                          "away": {"id": a.get("id"), "name": a.get("name") or ""}, "fm": _fm_status(st)}
    return games, ("" if games else (errs[0] if errs else "FotMob ne répond pas"))


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
    data = s365.read_json(res) if _fresh(res) else None
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
        for k in (("cards", "yellow") if key == "yc" else ("cards", "red") if key == "rc" else (key,)):     # cartons = jaunes + rouges ; jaunes, rouges à part
            h, a = out.get(k, (0, 0))
            out[k] = (h + v, a) if home else (h, a + v)
    return out


def _final(s365, g):
    """Score final d'un match terminé, par les derniers résultats de l'équipe à domicile, sinon de l'équipe à l'extérieur -> (h, a) ou None s'il n'y
    est pas encore."""
    for side in ("home", "away"):
        res = http.fetch(s365._url("/games/results/", competitors=g[side]["id"]), s365.NAME, 600)
        data = s365.read_json(res) if _fresh(res) else None
        for x in (data or {}).get("games") or []:
            if x.get("id") == g["id"]:
                hs, as_ = (x.get("homeCompetitor") or {}).get("score"), (x.get("awayCompetitor") or {}).get("score")
                if hs is not None and as_ is not None and hs >= 0 and as_ >= 0:
                    return int(hs), int(as_)
    return None


# ---------- le passage ----------

def _verdict(d, t):
    """Verdict déjà annoncé du ticket `t` (« won » / « lost » / None), rangé par son identifiant daté."""
    return d["tickets"].get(t.get("tid") or t["what"])


def _open_legs(ts, d, now):
    """Paris des tickets encore à suivre (pas décidés, ou décidés en cours de match : provisoires jusqu'à la fin), match commencé ou commençant dans
    l'heure, pas trop vieux. Ticket gagné : seuls ses paris provisoires restent revus (but annulé). Ticket déjà perdu : ses autres matchs restent suivis (C.WATCH_AFTER_LOSS) ; avec des
    tickets de 12 paris, un seul pari raté coupait sinon la moitié de tes matchs (dimanche 11/10 : 43 matchs sur 86 jamais suivis)."""
    out = []
    for t in ts:
        verdict = _verdict(d, t)
        closed = verdict == "won" or (verdict == "lost" and not C.WATCH_AFTER_LOSS)     # tranché : seuls ses paris provisoires restent revus
        for leg in t["legs"]:
            k = f"{t['what']}|{leg['key']}"
            ko = _ko(leg)
            cur = d["legs"].get(k, {})
            if closed and not cur.get("prov"):
                continue
            if (cur.get("state") in ("win", "loss") and not cur.get("prov")) or not ko or need(leg)[0] is None:
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


WON_LEGS_MAX = 12               # coupon gagné : paris listés au plus (au-delà : « … et N autres »)


def _won_text(t, d):
    """🎉 Coupon gagné (HTML) : le coupon, chaque pari avec son score, la mise x la cote = ce qui est encaissé (et le bénéfice) ; en mode capital, le
    capital après le règlement du lendemain et l'avancée de l'objectif du mois ; après un gros gain (5 % du capital ou plus), un conseil de discipline.
    Chiffres du suivi : le règlement officiel du matin fait foi."""
    from . import money
    stake, odds = float(t.get("stake") or 0), float(t.get("odds") or 0)
    pay = round(stake * odds)
    profit = pay - stake
    lines = ["🎉🏆 " + fmt.b("COUPON GAGNÉ !") + " 🏆🎉",
             f"🎫 {fmt.b(fmt.esc(t['name']))}" + (fmt.esc(f" · 🎟️ {t['coupon']}") if t.get("coupon") else "")]
    for leg in t["legs"][:WON_LEGS_MAX]:
        cur = d["legs"].get(f"{t['what']}|{leg['key']}", {})
        lines.append(fmt.esc(f"✅ {leg.get('home')} – {leg.get('away')} · {leg.get('pick')}" + (f" ({cur['score']})" if cur.get("score") else "")))
    if len(t["legs"]) > WON_LEGS_MAX:
        lines.append(fmt.esc(f"✅ … et {len(t['legs']) - WON_LEGS_MAX} autres"))
    lines.append(fmt.esc(f"💰 {money.money(round(stake))} × {fmt.odds_txt(odds)} = ") + fmt.b(fmt.esc(money.money(pay)))
                 + fmt.esc(f" encaissés ({money.signed(round(profit))})"))
    st = money.capital_state()
    if st:
        line = f"💼 Capital après le règlement de demain matin : {money.money(round(st['capital'] + profit))}"
        if st.get("goal"):
            line += f" · objectif du mois {money._bar(((st.get('month') or 0) + profit) / st['goal'])}"
        lines.append(fmt.esc(line))
        if st["capital"] > 0 and profit >= 0.05 * st["capital"]:
            lines.append(fmt.i("💡 Beau gain : mets-en une partie de côté (la moitié par exemple). Ta limite du jour reste à 3 % du capital."))
    return "\n".join(lines)


def _out(d, now, kind, text, **extra):
    """Ajoute une ligne ou un bloc (HTML déjà échappé) au message du passage. `kind` : warn (panne / rétabli), ev (un match : bloc de lignes), danger,
    last (dernier pari), foot (pied de ticket), nf (non suivi), recap (bilan de la soirée). `extra` : « ht » (résumé court d'une mi-temps, pour le mode
    compact). Les lignes restent dans le document jusqu'à leur envoi réussi (ou leur mise en attente par Telegram) : jamais perdues si le passage
    est coupé."""
    if not any(o["text"] == text and o["k"] == kind for o in d["out"]):
        d["out"].append({"at": now.isoformat(timespec="minutes"), "k": kind, "text": text, **extra})


ORDER = ("warn", "ev", "danger", "foot", "nf", "recap")
SEPARATE = ("pre", "last", "won")   # un message À PART chacun (⏰ avant le 1er match, 🔥 plus qu'un match, 🎉 coupon gagné), sous le bouton de son coupon et
                                    # en réponse au message du coupon quand Kelly le connaît (toucher la citation y ramène)
HT_COMPACT = 12                 # plus de mi-temps que ça dans un même message : une seule ligne pour toutes (sans le détail des paris)
BUTTONS_MAX = 6                 # boutons « 🎫 » de coupons au plus sous le message groupé


def _hid(tid):
    """Identifiant court d'un ticket pour le bouton « 🎫 » (callback « tk|<id> », 64 octets au plus)."""
    return hashlib.sha1(str(tid).encode()).hexdigest()[:10]


def _tk(t):
    """[id court, libellé du bouton (code coupon, sinon le ticket), id du message du coupon] : le bouton « 🎫 » d'un ticket et le message auquel répondre."""
    return [_hid(t.get("tid") or t["what"]), t.get("coupon") or t["what"], t.get("msg")]


def _buttons(tks):
    """Boutons « 🎫 AB12 » (3 par ligne, BUTTONS_MAX au plus, sans doublon) : un appui donne la fiche du coupon (ticket_card) ; None sans ticket."""
    seen, btns = set(), []
    for h, lab, _ in tks or []:
        if h not in seen and len(btns) < BUTTONS_MAX:
            seen.add(h)
            btns.append((f"🎫 {lab}", f"tk|{h}"))
    return telegram.inline([btns[i:i + 3] for i in range(0, len(btns), 3)]) if btns else None


def compose(out):
    """Messages du passage depuis les lignes à envoyer -> le premier (Html), les suivants dans son attribut `more` (telegram.send les envoie à la suite) ;
    None si rien. Le message groupé : avertissement / rétabli, matchs, paris en danger, pieds de tickets, paris non suivis, bilan, avec les boutons
    « 🎫 » des coupons concernés ; plus de HT_COMPACT mi-temps à la fois : « ⏸️ Mi-temps : A 1-0 B · C 0-0 D … » en une ligne. Puis un message à part
    par ligne SEPARATE (⏰, 🔥, 🎉), avec le bouton de son coupon et en réponse à son message."""
    main_out = [o for o in out if o["k"] not in SEPARATE]
    hts = [o for o in main_out if o.get("ht")]
    compact = len(hts) > HT_COMPACT
    blocks = []
    for kind in ORDER:
        lines = [o["text"] for o in main_out if o["k"] == kind and not (compact and o.get("ht"))]
        if kind == "ev" and compact:
            lines.insert(0, "⏸️ " + fmt.b("Mi-temps") + " : " + " · ".join(o["ht"] for o in hts))
        if lines:
            blocks.append(("\n\n" if kind == "ev" else "\n").join(lines))
    msgs = []
    if blocks:
        main = telegram.Html("\n\n".join(blocks))
        main.markup = _buttons([tk for o in main_out for tk in (o.get("tk") or [])])
        msgs.append(main)
    for o in out:
        if o["k"] in SEPARATE:
            m = telegram.Html(o["text"])
            tks = o.get("tk") or []
            m.markup = _buttons(tks)
            m.reply_to = tks[0][2] if len(tks) == 1 and tks[0][2] else None
            msgs.append(m)
    if not msgs:
        return None
    msgs[0].more = msgs[1:]
    return msgs[0]


def all_text(msg):
    """Tout le texte d'un passage (message groupé puis messages à part) : pour les tests et les journaux."""
    return "\n\n".join(str(m) for m in [msg, *getattr(msg, "more", ())]) if msg else ""


def _dur(td):
    m = int(td.total_seconds() // 60)
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


def _health(d, now, ok, why="", open_n=0, hard=False, backup=False):
    """Santé du suivi (d["health"]) : jamais de panne silencieuse. Panne DURE (365scores bloqué, proxy absent, plafond du mois) : avertie tout de suite ;
    panne douce (pas de réponse, délai dépassé, erreur) : après C.WATCH_FAIL_PASSES passages de suite. Seulement s'il y a des matchs de tes tickets à
    suivre, et une seule fois par panne ; au premier passage réussi ensuite : « ✅ rétabli ». `backup` : 365scores muet mais le secours FotMob suit tes
    matchs (scores seulement) : l'avertissement le dit."""
    h = d["health"]
    at = now.isoformat(timespec="minutes")
    if ok:
        if h.get("warned"):
            try:
                since = _dur(now - datetime.fromisoformat(h["since"])) if h.get("since") else ""
            except ValueError:
                since = ""
            if h.get("backup"):
                _out(d, now, "warn", "✅ " + fmt.b("365scores rétabli") + fmt.esc(f" (coupé {since})" if since else "") + fmt.esc(" : suivi complet de nouveau."))
            else:
                _out(d, now, "warn", "✅ " + fmt.b("Suivi des matchs rétabli") + fmt.esc(f" (coupé {since})" if since else "") + fmt.esc(" : rattrapage ci-dessous."))
        d["health"] = {"at": at, "ok": True, "why": "", "since": None, "fails": 0, "warned": False, "open": open_n}
        return
    h.update(at=at, ok=False, why=why, open=open_n, fails=int(h.get("fails") or 0) + 1, since=h.get("since") or at)
    if backup and h.get("warned") and not h.get("backup"):         # panne déjà dite (sans secours), le secours FotMob reprend : dit une fois
        _out(d, now, "warn", "🛟 " + fmt.b("Secours FotMob") + fmt.esc(" : je suis de nouveau tes matchs (scores, mi-temps, fins de match)."))
    if open_n > 0 and not h.get("warned") and (hard or h["fails"] >= C.WATCH_FAIL_PASSES):
        if backup:
            _out(d, now, "warn", "⚠️ " + fmt.b("365scores en panne") + fmt.esc(f" : {why}. Je suis tes matchs par FotMob : scores, mi-temps et fins de "
                                                                                   "match. Corners, cartons, tirs et fautes : au règlement du matin."))
        else:
            n = f"{open_n} match{'s' if open_n > 1 else ''} de tes tickets ne {'sont' if open_n > 1 else 'est'} plus suivi{'s' if open_n > 1 else ''}"
            _out(d, now, "warn", "⚠️ " + fmt.b("Suivi des matchs en panne") + fmt.esc(f" : {why}. {n} : regarde-les toi-même. Je te préviens dès que ça repart."))
        h["warned"] = True
    if backup:
        h["backup"] = True
    print(f"[suivi] en panne : {why}", file=sys.stderr)


def _blocked_why():
    """Raison d'une panne dure de 365scores (source bloquée après un refus, avec l'heure de fin), ou None."""
    from .sources import state as src_state
    hl = src_state.health("s365")
    if not hl.get("blocked_until"):
        return None
    until = str(hl["blocked_until"])[:16].replace("T", " à ")
    return f"365scores refuse les lectures ({hl.get('last_error') or 'accès refusé'}), bloqué jusqu'au {until[8:10]}/{until[5:7]} {until[11:]} UTC"


def _unfollowed(ts, d, now, games):
    """Paris de tes tickets que Kelly ne peut PAS suivre en direct, dits une fois (d["legs"][k]["nf"]) : marché non suivi, heure inconnue, match
    introuvable sur 365scores 30 min après le coup d'envoi. -> [(texte du match, raison)]."""
    out, seen = [], set()
    for t in ts:
        if _verdict(d, t) == "won":
            continue
        for leg in t["legs"]:
            k = f"{t['what']}|{leg['key']}"
            cur = d["legs"].get(k, {})
            if cur.get("nf") or cur.get("state") in ("win", "loss") or leg["key"] in seen:
                continue
            ko = _ko(leg)
            if need(leg)[0] is None and ko and ko <= now:
                why = "marché non suivi en direct"
            elif not ko:
                why = "heure du match inconnue"
            elif games is not None and now - ko >= timedelta(minutes=30) and now - ko <= timedelta(hours=GIVE_UP_H) and not find(leg, games):
                why = "match introuvable sur 365scores"
            else:
                continue
            seen.add(leg["key"])
            for t2 in ts:                               # le même pari dans plusieurs tickets : dit une fois, marqué partout
                for l2 in t2["legs"]:
                    if l2["key"] == leg["key"]:
                        d["legs"].setdefault(f"{t2['what']}|{l2['key']}", {}).update(nf=why, at=now.isoformat(timespec="minutes"))
            out.append((f"{leg.get('home')} – {leg.get('away')}" + (f" ({leg.get('pick')})" if leg.get("pick") else ""), why))
    return out


def _lower(prev, cur):
    """Le score a-t-il BAISSÉ depuis le passage précédent (but annulé, VAR) ?"""
    try:
        ph, pa = (int(x) for x in str(prev).split("-"))
    except ValueError:
        return False
    return cur[0] < ph or cur[1] < pa


def _follow(d, now, games=None, live_for=None, stats_for=None, final_for=None, detail_for=None, fm_for=None):
    """Le travail d'un passage, sur le document `d` (lignes à envoyer ajoutées à d["out"], document sauvé après chaque match)."""
    from .sources import s365
    ts = [t for day in (now.date(), now.date() - timedelta(days=1)) for t in exposure.tickets(str(day))]
    _prekick(d, now, ts)                                 # ⏰ 5 min avant le 1er match de chaque coupon (aucune requête)
    todo = _open_legs(ts, d, now)
    open_n = len({leg.get("id") or leg["key"] for _, leg, _ in todo})
    if not todo:
        for m, why in _unfollowed(ts, d, now, games):
            _out(d, now, "nf", "👁️ " + fmt.esc(f"Pas suivi en direct (verdict au règlement du matin) : {m} : {why}"))
        d["health"].update(at=now.isoformat(timespec="minutes"), open=0)
        return
    alive = {t.get("tid") or t["what"] for t in ts if _verdict(d, t) not in ("won", "lost")}            # tickets encore en jeu avant ce passage
    alive_legs = {leg["key"] for t in ts if (t.get("tid") or t["what"]) in alive for leg in t["legs"]}
    tix = {}                                             # pari -> [(étiquette du ticket, encore en jeu ?)] : de quel coupon vient chaque pari
    for t in sorted(ts, key=lambda t: (t.get("tid") or t["what"]) not in alive):
        for leg in t["legs"]:
            lab = (_label(t), (t.get("tid") or t["what"]) in alive)
            if lab not in tix.setdefault(leg["key"], []):
                tix[leg["key"]].append(lab)
            if _tk(t) not in tix.setdefault(("tk", leg["key"]), []):      # boutons « 🎫 » des coupons de ce pari
                tix[("tk", leg["key"])].append(_tk(t))
    if games is None:
        why = _blocked_why()
        if why:
            _backup(d, now, ts, todo, open_n, why, alive, alive_legs, tix, fm_for, hard=True)
            return
    touched, late = {}, False
    with (_NoProxy() if games is not None else s365._via_proxy(settle=True)) as why:     # tes tickets (~1 Ko par match) passent avant la lecture de fond
        if why:
            _backup(d, now, ts, todo, open_n, why, alive, alive_legs, tix, fm_for, hard=True)
            return
        games = games if games is not None else _day_games(s365, now)
        for m, why_nf in _unfollowed(ts, d, now, games):
            _out(d, now, "nf", "👁️ " + fmt.esc(f"Pas suivi en direct (verdict au règlement du matin) : {m} : {why_nf}"))
        found = [(t, leg, k, find(leg, games)) for t, leg, k in todo]
        found = [x for x in found if x[3] and _ko(x[1]) <= now]                 # matchs commencés
        if not found:
            _health(d, now, True, open_n=open_n)
            return
        status = (live_for or (lambda ids: _live(s365, ids, now)))({g["id"] for _, _, _, g in found})
        if status is None:
            _backup(d, now, ts, todo, open_n, _WHY["live"] or "365scores ne répond pas", alive, alive_legs, tix, fm_for)
            return
        late = _run(d, now, found, status, stats_for, final_for, detail_for, s365, alive, alive_legs, tix, touched)
    _after(d, now, ts, alive, touched, open_n, late=late)


def _backup(d, now, ts, todo, open_n, why, alive, alive_legs, tix, fm_for=None, hard=False):
    """365scores muet (`why`) : tes matchs commencés lus sur FotMob (_fm_games : scores, mi-temps, fins de match ; ni buteur, ni stats, ni carton rouge).
    L'avertissement dit la cause et le secours (une fois) ; secours impossible (C.WATCH_FM_BACKUP=0, championnat absent de FotMob, FotMob muet) : la
    panne classique, avec la raison."""
    if not C.WATCH_FM_BACKUP:
        _health(d, now, False, why, open_n, hard=hard)
        return
    games, fm_why = (fm_for or (lambda: _fm_games(todo, now)))()
    found = [(t, leg, k, find(leg, games)) for t, leg, k in todo]
    found = [x for x in found if x[3] and _ko(x[1]) <= now]
    if not found:
        _health(d, now, False, why + (f" ; secours FotMob impossible : {fm_why}" if fm_why else ""), open_n, hard=hard)
        return
    touched = {}
    late = _run(d, now, found, {x[3]["id"]: x[3]["fm"] for x in found}, lambda g: {}, lambda g: None, lambda g: None, None, alive, alive_legs, tix,
                touched)
    _after(d, now, ts, alive, touched, open_n, late=late, backup=why, hard=hard)


def _run(d, now, found, status, stats_for, final_for, detail_for, s365, alive, alive_legs, tix, touched):
    """Les matchs commencés `found` ([(ticket, pari, clé, match)]) au statut `status` ({id du match: statut}) : coups d'envoi, puis un bloc par match
    (_one_game), document sauvé après CHAQUE match. -> vrai si le délai du passage a coupé la liste (le reste au passage suivant)."""
    by_game = {}
    for x in found:
        by_game.setdefault(x[3]["id"], []).append(x)
    def game_tk(gid):
        out = []
        for _, leg, _, _ in by_game[gid]:
            out += [tk for tk in tix.get(("tk", leg["key"]), []) if tk not in out]
        return out
    for text, gids in _kickoffs(d, now, by_game, status, tix):
        _out(d, now, "ev", fmt.esc(text), tk=[tk for gid in gids for tk in game_tk(gid)])
    order = sorted(by_game, key=lambda gid: (not (status.get(gid) or {}).get("live"), min(_ko(x[1]) for x in by_game[gid])))
    cache_stats, cache_final, events = {}, {}, {}
    for gid in order:
        dl = http.get_deadline()
        if dl is not None and http.monotonic() > dl:
            return True
        try:
            _one_game(d, now, by_game[gid], status.get(gid) or {}, cache_stats, cache_final, stats_for, final_for, s365, events, touched, alive,
                      alive_legs, detail_for, tix)
        except Exception as e:                      # noqa: BLE001 - un match en erreur n'empêche pas les autres
            print(f"[suivi] match {gid} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        for text, extra in events.items():
            _out(d, now, "ev", fmt.esc(text), tk=game_tk(gid), **{k: fmt.esc(v) for k, v in (extra or {}).items()})
        events.clear()
        store.save(FILE, d)                         # après CHAQUE match : un passage coupé ne perd ni ne répète rien
    return False


def _after(d, now, ts, alive, touched, open_n, late=False, backup=None, hard=False):
    """Fin d'un passage : derniers paris, pieds des tickets touchés, santé (secours FotMob en cours : `backup` = cause de la panne de 365scores)."""
    _last_legs(d, now, ts, alive)
    for tid, t in touched.items():
        verdict, foot = _ticket_line(t, d)
        old = d["tickets"].get(tid)
        if verdict == "won" and old != "won":            # 🎉 coupon gagné : un message à part, festif
            d["tickets"][tid] = verdict
            _out(d, now, "won", _won_text(t, d), tk=[_tk(t)])
            continue
        if verdict == "won":                             # déjà fêté : rien de plus
            continue
        if old and verdict != old:                       # verdict revu (but annulé, décision provisoire corrigée)
            foot = ("⚠️ Ticket corrigé : de nouveau en jeu. " if not verdict else "⚠️ Ticket corrigé. ") + foot
        if verdict:
            d["tickets"][tid] = verdict
        else:
            d["tickets"].pop(tid, None)
        _out(d, now, "foot", f"🎫 {fmt.b(fmt.esc(t['name']))}" + (fmt.esc(f" · 🎟️ {t['coupon']}") if t.get("coupon") else "") + f" · {fmt.esc(foot)}",
             tk=[_tk(t)])
    if backup:
        _health(d, now, False, backup, open_n, hard=hard, backup=True)
    elif late:
        _health(d, now, False, f"passage trop long (plus de {C.WATCH_DEADLINE_S} s), le reste au prochain passage", open_n)
    else:
        _health(d, now, True, open_n=open_n)


def _gs(d, gid, legs):
    """État du match `gid` dans d["games"]. Le même match suivi avant sous un autre identifiant (365scores <-> secours FotMob, d'après le « gid » de ses
    paris) : son état le suit (score déjà vu, mi-temps et fin déjà dites), rien n'est annoncé deux fois."""
    gid = str(gid)
    if gid not in d["games"]:
        for x in legs:
            g0 = d["legs"].get(x[2], {}).get("gid")
            old = "" if g0 is None else str(g0)
            if old and old != gid and old in d["games"]:
                d["games"][gid] = d["games"].pop(old)
                break
    return d["games"].setdefault(gid, {})


KICKOFF_LATE = 15               # vu pour la première fois après la 15e (passages toutes les 10 min ; panne, redémarrage) : pas de « coup d'envoi »


def _kickoffs(d, now, by_game, status, tix):
    """🟢 Coup d'envoi des matchs de tes tickets vus en cours pour la première fois : un bloc par match (le match, puis ses paris et leurs coupons) ;
    plus de HT_COMPACT à la fois : une seule ligne. Un match découvert après la KICKOFF_LATE e minute, à la mi-temps ou fini n'est pas annoncé (marqué
    vu), ni un match déjà annoncé par « ⏰ Dans 5 min ». -> [(texte, [ids des matchs])]."""
    out = []
    for gid, xs in by_game.items():
        st_g = status.get(gid) or {}
        gs = _gs(d, gid, xs)
        if gs.get("ko") or not (st_g.get("live") or st_g.get("ended")):
            continue
        gs["ko"] = 1
        if not C.WATCH_KICKOFF or st_g.get("ended") or st_g.get("ht") or int(st_g.get("minute") or 0) > KICKOFF_LATE or gs.get("score"):
            continue
        if all(d["legs"].get(x[2], {}).get("pre") for x in xs):         # déjà annoncé 5 min avant (⏰) : pas deux fois
            continue
        block, seen = [f"🟢 Coup d'envoi · {xs[0][1]['home']} – {xs[0][1]['away']}"], set()
        for _, leg, _, _ in xs:
            if leg["key"] not in seen:
                seen.add(leg["key"])
                block.append(f"   {fmt.market_icon(leg)} {leg.get('pick')}" + _from(leg["key"], tix))
        out.append(("\n".join(block), [gid]))
    if len(out) > HT_COMPACT:
        return [("🟢 Coup d'envoi : " + " · ".join(b.split("\n")[0].removeprefix("🟢 Coup d'envoi · ") for b, _ in out), [g for _, gs_ in out for g in gs_])]
    return out


PREKICK_MIN = 5                 # ⏰ annonce d'un coupon : 5 min avant son 1er match (le runner déclenche un passage à cette heure, voir plan_next)
PREKICK_EARLY = 8               # ... envoyée par le 1er passage tombé dans les 8 dernières minutes avant le match (jamais après le coup d'envoi)
NEXT_FILE = "watch_next.json"   # data/watch_next.json : {"at": instant du prochain passage voulu} (lu par termux_runner.py, hors de la base)


def _wave(t, now):
    """(heure du 1er match du coupon `t` pas encore commencé, [ses paris à cette heure], [heures des matchs suivants]) ; None sans match à venir."""
    kos = sorted({k for k in (_ko(l) for l in t["legs"]) if k and k > now})
    if not kos:
        return None
    return kos[0], [l for l in t["legs"] if _ko(l) == kos[0]], [k for l in t["legs"] for k in [_ko(l)] if k and k > kos[0]]


def _prekick(d, now, ts):
    """⏰ « Dans 5 min » (message à part par coupon, groupé : tous les matchs du coupon qui commencent à la même heure) : avant le 1er match d'un coupon
    encore en jeu, une fois par coupon (d["flags"]["<tid>|pre"]), avec les matchs suivants et ce que rapporte le coupon s'il passe. Ces matchs n'ont
    pas de « 🟢 coup d'envoi » en plus. Aucune requête : tout vient de tes tickets."""
    if not C.WATCH_PREKICK:
        return
    from . import money
    for t in ts:
        tid = t.get("tid") or t["what"]
        if _verdict(d, t) in ("won", "lost") or d["flags"].get(f"{tid}|pre"):
            continue
        if any(_ko(l) and _ko(l) <= now for l in t["legs"]):            # déjà commencé : trop tard pour « avant le 1er match »
            continue
        w = _wave(t, now)
        if not w or w[0] - now > timedelta(minutes=PREKICK_EARLY):
            continue
        first, wave, later = w
        d["flags"][f"{tid}|pre"] = first.isoformat(timespec="minutes")
        mins = max(1, round((first - now).total_seconds() / 60))
        head = "⏰ " + fmt.b(f"Dans {mins} min") + fmt.esc(f" · 🎫 {t['name']}" + (f" · 🎟️ {t['coupon']}" if t.get("coupon") else ""))
        lines = [head, fmt.esc(f"{len(wave)} match{'s' if len(wave) > 1 else ''} à {first:%Hh%M} :")]
        for l in wave:
            lines.append(fmt.esc(f"   {fmt.market_icon(l)} {l.get('home')} – {l.get('away')} · {l.get('pick')}"))
            d["legs"].setdefault(f"{t['what']}|{l['key']}", {})["pre"] = 1
        pay = money.money(round((t.get("odds") or 0) * (t.get("stake") or 0)))
        tail = (f"Puis {len(later)} match{'s' if len(later) > 1 else ''} (le dernier à {max(later):%Hh%M}) · " if later else "") + "si tout passe : "
        lines.append(fmt.esc(tail) + fmt.b(fmt.esc(pay)))
        _out(d, now, "pre", "\n".join(lines), tk=[_tk(t)])


def plan_next(now=None, ts=None, d=None):
    """Heure du prochain passage voulu : 5 min avant le 1er match du prochain coupon pas encore annoncé (⏰), écrite dans data/watch_next.json pour le
    runner (qui sinon passe toutes les 10 min). Appelée à la fin de chaque passage et quand un pari est noté. -> l'instant (epoch) ou None. Ne lève jamais."""
    try:
        now = now or _now()
        d = d if d is not None else _load()
        ts = ts if ts is not None else [t for day in (now.date(), now.date() - timedelta(days=1)) for t in exposure.tickets(str(day))]
        at = None
        for t in ts:
            tid = t.get("tid") or t["what"]
            if d["flags"].get(f"{tid}|pre") or _verdict(d, t) in ("won", "lost") or any(_ko(l) and _ko(l) <= now for l in t["legs"]):
                continue
            w = _wave(t, now)
            if w:
                x = (w[0] - timedelta(minutes=PREKICK_MIN)).timestamp()
                at = x if at is None else min(at, x)
        tmp = store.path(NEXT_FILE + ".tmp")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": at}, f)
        os.replace(tmp, store.path(NEXT_FILE))
        return at
    except Exception as e:                          # noqa: BLE001 - sans lui, le passage ordinaire de 10 min envoie l'annonce un peu plus tôt
        print(f"[suivi] prochain passage non planifié : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def ticket_card(h, now=None):
    """Fiche d'un coupon (bouton « 🎫 » d'une alerte) : son nom, son code (en <code> : un appui le copie), cote, mise, ce qu'il rapporte s'il passe, puis
    chaque pari : ✅ gagné / ❌ perdu (avec le score), ⏳ en cours (score et minute du dernier passage), 🕘 à venir (heure) ; et le compte. Html."""
    from . import money
    now = now or _now()
    t = next((t for day in (now.date(), now.date() - timedelta(days=1), now.date() - timedelta(days=2)) for t in exposure.tickets(str(day))
              if _hid(t.get("tid") or t["what"]) == h), None)
    if t is None:
        return "Ce coupon n'est plus suivi (il date de plus de 2 jours)."
    try:
        d = _load()
    except WatchStateError:
        d = {"legs": {}, "games": {}}
    pay = money.money(round((t.get("odds") or 0) * (t.get("stake") or 0)))
    lines = [f"🎫 {fmt.b(fmt.esc(t['name']))}" + (f" · 🎟️ {fmt.code(t['coupon'])}" if t.get("coupon") else ""),
             fmt.esc(f"cote {fmt.odds_txt(t.get('odds') or 0)} · mise {money.money(round(t.get('stake') or 0))} · si tout passe : {pay}"), ""]
    n = {"win": 0, "loss": 0, "live": 0, "soon": 0}
    for leg in sorted(t["legs"], key=lambda l: (_ko(l) or now).isoformat()):
        state, line = _leg_line(t, leg, d, now)
        n[state] += 1
        lines.append(fmt.esc(line))
    lines += ["", fmt.esc("📊 " + _counts(n) + (" · coupon perdu" if n["loss"] else ""))]
    return telegram.Html("\n".join(lines))


def _leg_line(t, leg, d, now):
    """(état, ligne) d'un pari de coupon : win « ✅ A – B 2-1 · pari », loss « ❌ … », live « ⏳ A – B 1-0 (63e) · pari », soon « 🕘 A – B 21h00 · pari »."""
    cur = d["legs"].get(f"{t['what']}|{leg['key']}", {})
    ko = _ko(leg)
    gs = (d.get("games") or {}).get(str(cur.get("gid")), {}) if cur.get("gid") is not None else {}
    match = f"{leg.get('home')} – {leg.get('away')}"
    if cur.get("state") in ("win", "loss"):
        state, line = cur["state"], f"{'✅' if cur['state'] == 'win' else '❌'} {match} {cur.get('score') or ''} · {leg.get('pick')}"
    elif ko and ko > now:
        state, line = "soon", f"🕘 {match} {ko:%Hh%M} · {leg.get('pick')}"
    else:
        sc = gs.get("score") or cur.get("score")
        state = "live"
        line = f"⏳ {match}" + (f" {sc}" + (f" ({gs['min']}e)" if gs.get("min") and not gs.get("ft") else "") if sc else "") + f" · {leg.get('pick')}"
    return state, " ".join(line.split())


BOARD_MAX = 3900                # caractères au plus du tableau (un message Telegram : 4 096) ; au-delà, il se resserre
BOARD_SOON_MIN = 60             # le tableau du jour naît quand un match d'un coupon commence dans l'heure (ou est en cours)


def _board_tickets(d, now):
    """Coupons du tableau du jour : ceux d'aujourd'hui, et ceux d'hier encore en jeu (matchs d'après minuit)."""
    today = [t for t in exposure.tickets(str(now.date()))]
    return today + [t for t in exposure.tickets(str(now.date() - timedelta(days=1))) if _verdict(d, t) is None]


def _board_text(ts, d, now, detail=2):
    """(Html du tableau, empreinte du contenu sans l'heure de mise à jour). `detail` : 2 = chaque pari de chaque coupon en jeu, 1 = seulement les paris
    en cours, 0 = une ligne par coupon (tableau trop long)."""
    from . import money
    order = {None: 0, "won": 1, "lost": 2}
    body, stake_in, pay_in, won = [], 0.0, 0.0, 0.0
    for t in sorted(ts, key=lambda t: (order.get(_verdict(d, t), 3), min((_ko(l) or now) for l in t["legs"]).isoformat())):
        verdict = _verdict(d, t)
        stake, pay = float(t.get("stake") or 0), round(float(t.get("stake") or 0) * float(t.get("odds") or 0))
        head = f"🎫 {fmt.b(fmt.esc(t['name']))}" + (fmt.esc(f" · 🎟️ {t['coupon']}") if t.get("coupon") else "")
        rows = sorted(((leg, *_leg_line(t, leg, d, now)) for leg in t["legs"]), key=lambda x: (_ko(x[0]) or now).isoformat())
        if verdict == "won":
            won += pay - stake
            body.append(head + fmt.esc(f" · 🏆 gagné {money.signed(round(pay - stake))}"))
        elif verdict == "lost":
            lost = [line for _, st, line in rows if st == "loss"]
            body.append(head + fmt.esc(" · ❌ perdu" + (f" ({lost[0].removeprefix('❌ ')})" if lost and detail else "")))
        else:
            stake_in, pay_in = stake_in + stake, pay_in + pay
            n = {"win": 0, "loss": 0, "live": 0, "soon": 0}
            for _, st, _ in rows:
                n[st] += 1
            body.append(head + fmt.esc(f" · {_counts(n)} · si tout passe {money.money(pay)}"))
            body += [fmt.esc("   " + line) for _, st, line in rows if detail >= 2 or (detail == 1 and st == "live")]
    foot = []
    if stake_in:
        foot.append(fmt.esc(f"💰 En jeu : mise {money.money(round(stake_in))} · gains possibles {money.money(round(pay_in))}"))
    if won:
        foot.append(fmt.esc(f"🏆 Gagné aujourd'hui : {money.signed(round(won))}"))
    content = "\n".join(body + ([""] + foot if foot else []))
    head = "📌 " + fmt.b("Tableau du jour") + fmt.esc(f" · {fmt.fr_day(now.date())} · mis à jour {now:%Hh%M} UTC")
    text = telegram.Html(head + "\n\n" + content)
    if len(text) > BOARD_MAX and detail > 0:
        return _board_text(ts, d, now, detail - 1)
    return text, hashlib.sha1(content.encode()).hexdigest()[:16]


def board(now=None):
    """📌 Tableau du jour : UN message, épinglé sans notification, qui montre tous tes coupons du jour (chaque pari : ✅ ❌ ⏳ score et minute, 🕘 heure),
    ce qui est en jeu et ce qui est gagné ; il naît quand un match d'un coupon commence dans l'heure, puis il est MODIFIÉ (jamais renvoyé) à chaque
    passage où quelque chose change. Le tableau de la veille est désépinglé. Message effacé : un nouveau est envoyé. d["board"] = {day, msg, hash,
    pinned}. Ne lève jamais."""
    if not C.WATCH_BOARD:
        return
    try:
        now = now or _now()
        d = _load()
        ts = _board_tickets(d, now)
        b, day = d.get("board") or {}, str(now.date())
        if not ts:
            return
        active = any(k and now - timedelta(hours=12) <= k <= now + timedelta(minutes=BOARD_SOON_MIN) for t in ts for k in (_ko(l) for l in t["legs"]))
        if b.get("day") != day and not active:
            return
        text, h = _board_text(ts, d, now)
        if b.get("day") == day and b.get("hash") == h:
            return
        if b.get("day") == day and b.get("msg") and telegram.edit_message(b["msg"], text):
            b["hash"] = h
        else:
            if b.get("msg") and b.get("pinned") and b.get("day") != day:
                telegram.unpin(b["msg"])
            mid = telegram.send(text, queue=False)
            if type(mid) is not int:                     # pas envoyé (ou hors ligne) : au prochain passage
                return
            b = {"day": day, "msg": mid, "hash": h, "pinned": telegram.pin(mid)}
        d["board"] = b
        store.save(FILE, d)
    except Exception as e:                          # noqa: BLE001 - le tableau ne bloque jamais le suivi
        print(f"[suivi] tableau du jour ignoré : {type(e).__name__}: {e}", file=sys.stderr)


def _counts(n):
    """« 2 gagnés, 1 perdu, 1 en cours, 3 à venir »."""
    return ", ".join([f"{n['win']} gagné{'s' if n['win'] > 1 else ''}"] + ([f"{n['loss']} perdu{'s' if n['loss'] > 1 else ''}"] if n["loss"] else [])
                     + ([f"{n['live']} en cours"] if n["live"] else []) + ([f"{n['soon']} à venir"] if n["soon"] else []))


def _score_t(txt):
    try:
        h, a = (int(x) for x in str(txt).split("-"))
        return h, a
    except ValueError:
        return None


def _leg_now(leg, h, a, stats):
    """Où en est un pari encore en jeu (lignes de mi-temps et de but) : marge de buts, « pour toi pour l'instant », ou la stat du moment."""
    key, side = need(leg)
    if key == "goals":
        return margin(leg, h, a)
    hv, av = stats.get(key) or (None, None)
    if hv is None:
        return ""
    val = hv + av if side == "total" else hv if side == "home" else av
    return f"{val} {ONE.get(key, key) if val == 1 else WORD.get(key, key)}"


FROM_MAX = 3                    # coupons cités au plus sur la ligne d'un pari (les autres : « +N »)


def _label(t):
    """Repère d'un ticket : son code coupon 1xBet (« 🎟️ 4G2MJ », le seul sûr : trois « Combi sur mesure (cote 1000) » le même jour), sinon son nom."""
    return f"🎟️ {t['coupon']}" if t.get("coupon") else f"🎫 {t['name']}"


def _from(key, tix, alive_only=False):
    """« · 🎟️ 4G2MJ, 🎟️ LJVKJ (perdu) +2 » : les tickets d'où vient le pari `key` (encore en jeu d'abord) ; '' sans ticket."""
    labs = [(lab, ok) for lab, ok in (tix or {}).get(key, []) if ok or not alive_only]
    if not labs:
        return ""
    shown = [lab + ("" if ok else " (perdu)") for lab, ok in labs[:FROM_MAX]]
    return " · " + ", ".join(shown) + (f" +{len(labs) - FROM_MAX}" if len(labs) > FROM_MAX else "")


DANGER_DROP = 0.15              # « pari en danger » : la chance a baissé d'au moins 15 points depuis l'avant-match


def _pred(gs, leg):
    """Buts attendus d'avant-match du match (live.model : modèle de force, profils, équipes préparées), gardés dans gs["pred"] ; {} si inconnus."""
    if "pred" not in gs:
        gs["pred"] = {}
        try:
            from . import live
            got = live.model({"home": leg["home"], "away": leg["away"], "league": leg.get("league") or "", "start": None}, leg.get("sport_key"))
            pr = (got or {}).get("pred") or {}
            if pr.get("lh") and pr.get("la"):
                gs["pred"] = {"lh": round(float(pr["lh"]), 3), "la": round(float(pr["la"]), 3),
                              "stats": {k: {"al": v.get("al"), "at": v.get("at")} for k, v in (pr.get("stats") or {}).items() if isinstance(v, dict)}}
        except Exception as e:                      # noqa: BLE001 - sans modèle, pas de chance pour le 1N2 : rien d'autre ne change
            print(f"[suivi] modèle du match {leg.get('home')} – {leg.get('away')} indisponible : {type(e).__name__}", file=sys.stderr)
    return gs["pred"]


def _chance(leg, gs, score, st_g, stats):
    """(chance du pari maintenant, rythme) ou (None, '') — kelly/inplay.py ; C.WATCH_CHANCES=0 : jamais."""
    if not C.WATCH_CHANCES:
        return None, ""
    key, side = need(leg)
    if key is None:
        return None, ""
    pred = _pred(gs, leg) if (leg.get("market") in ("h2h", "double_chance", "btts") or key != "goals") else None
    try:
        return inplay.chance(leg, key, side, score, int(st_g.get("minute") or 0), int(st_g.get("period") or (1 if (st_g.get("minute") or 0) <= 45 else 2)),
                             tuple(st_g.get("red") or (0, 0)), stats, pred)
    except Exception as e:                          # noqa: BLE001
        print(f"[suivi] chance de {leg.get('pick')} non calculée : {type(e).__name__}: {e}", file=sys.stderr)
        return None, ""


def _ch_txt(p, leg):
    """« · 61 % (55 % avant le match) » ; '' sans chance."""
    if p is None:
        return ""
    p0 = leg.get("prob")
    return f" · {inplay.pct(p)}" + (f" ({inplay.pct(p0)} avant le match)" if p0 else "")


def _one_game(d, now, legs, st_g, cache_stats, cache_final, stats_for, final_for, s365, events, touched, alive, alive_legs, detail_for=None,
              tix=None):
    """Les paris d'UN match : score, stats si besoin, fiche détaillée aux moments clés (but d'un match d'un ticket en jeu, prolongation), état de chaque
    pari ; UN bloc de lignes pour ce match dans `events` (titre + lignes des paris), tickets touchés dans `touched`."""
    g = legs[0][3]
    gs = _gs(d, g["id"], legs)
    gs["at"] = now.isoformat(timespec="minutes")
    gs["min"] = int(st_g.get("minute") or 0) if st_g.get("live") else gs.get("min")      # minute du dernier passage (fiche du coupon)
    paused = st_g.get("paused")
    ko = min(_ko(x[1]) for x in legs)
    finished = st_g.get("ended") or (not st_g.get("live") and not paused and now - ko >= timedelta(minutes=FINISH_MIN))
    score = st_g.get("score") if (st_g.get("live") or st_g.get("ended") or paused) else None
    if finished and not score:
        if g["id"] not in cache_final:
            cache_final[g["id"]] = (final_for or (lambda x: _final(s365, x)))(g)
        score = cache_final[g["id"]]
    if not score:
        return
    sc = f"{score[0]}-{score[1]}"
    home, away = legs[0][1]["home"], legs[0][1]["away"]
    match = f"{home} – {away}"
    minute = st_g.get("minute") or 0
    prev = _score_t(gs.get("score"))
    up = prev is not None and score[0] + score[1] > prev[0] + prev[1]
    lower = prev is not None and (score[0] < prev[0] or score[1] < prev[1])
    et = bool(st_g.get("et") or gs.get("et"))
    in_play = any(leg["key"] in alive_legs for _, leg, _, _ in legs)
    # fiche détaillée : sur un but d'un match d'un ticket en jeu (plafond du jour), ou pour le score à 90 min d'une prolongation (hors plafond)
    det = None
    want_et = et and not gs.get("s90")
    if (up and in_play and C.WATCH_SUMMARIES and _budget_ok(d, now)) or (want_et and _budget_ok(d, now, settle=True)):
        det = (detail_for or (lambda x: _detail(s365, x)))(g)
        d["budget"]["detail"] = int(d["budget"].get("detail") or 0) + 1
    seen = set(gs.get("ev") or [])
    new_ev = [e for e in (det or {}).get("events", []) if e["id"] not in seen]
    if det:
        gs["ev"] = sorted(seen | {e["id"] for e in det["events"]})
        if det.get("s90"):
            gs["s90"] = f"{det['s90'][0]}-{det['s90'][1]}"
    if et:
        gs["et"] = 1
    s90 = _score_t(gs.get("s90"))
    block = []                                           # lignes du match, dans l'ordre
    name = {"home": home, "away": away}

    def who(e):
        return (e.get("player") or name[e["side"]]) + (" (penalty)" if "penal" in e["sub"].lower() else " (contre son camp)" if "own" in e["sub"].lower() else "")
    # prolongation : dite une fois ; les paris de buts se règlent sur le score à 90 min
    if et and not gs.get("et_said") and C.WATCH_SUMMARIES:
        gs["et_said"] = 1
        block.append(f"⏱️ Prolongation · {match} : " + (f"{gs['s90']} après 90 min. Tes paris de buts se règlent sur ce score (temps réglementaire)."
                                                        if s90 else "tes paris de buts se règlent sur le score à 90 min."))
    # but annulé
    if lower:
        block.append(f"↩️ But annulé : {home} {sc} {away} ({minute}e)")
    elif any(e["kind"] == "var" for e in new_ev):
        e = next(e for e in new_ev if e["kind"] == "var")
        block.append(f"↩️ But refusé par la VAR ({e['min']}) · {home} {sc} {away}")
    # carton rouge (365scores en direct, sinon la fiche, sinon la stat « Red Cards » déjà lue)
    stats_g = cache_stats.get(g["id"]) or {}
    red_now = st_g.get("red") or stats_g.get("red")
    red_prev = tuple(gs["red"]) if isinstance(gs.get("red"), list) else None
    if red_prev is None and prev is not None and red_now:
        red_prev = (0, 0)                                # 365scores ne donne les rouges que s'il y en a : un match déjà vu sans rouge en avait 0
    reds = [e for e in new_ev if e["kind"] == "red"]
    if reds and C.WATCH_SUMMARIES:
        for e in reds:
            block.append(f"🟥 Carton rouge pour {name[e['side']]} ({e['min']}) · {home} {sc} {away}")
    elif red_now and red_prev and sum(red_now) > sum(red_prev) and C.WATCH_SUMMARIES:
        side = home if red_now[0] > red_prev[0] else away
        block.append(f"🟥 Carton rouge pour {side} ({minute}e) · {home} {sc} {away}")
    if red_now:
        gs["red"] = list(red_now)
    for e in new_ev:
        if e["kind"] == "pen_miss" and C.WATCH_SUMMARIES:
            block.append(f"🚫 Penalty manqué par {e.get('player') or name[e['side']]} ({e['min']}) · {home} {sc} {away}")
    goals = [e for e in new_ev if e["kind"] == "goal"]
    head_goal = None
    if up and not (et and s90 and score[0] + score[1] > s90[0] + s90[1] and prev[0] + prev[1] >= s90[0] + s90[1]):
        head_goal = f"⚽ {home} {sc} {away} ({minute}e{', suspendu' if paused else ''})" + (
            " — " + ", ".join(f"{who(e)} {e['min']}" for e in goals[-max(1, score[0] + score[1] - prev[0] - prev[1]):]) if goals else "")
    elif up:
        head_goal = f"⚽ {home} {sc} {away} (prolongation, {minute}e) · sans effet sur tes paris de buts"
    if head_goal:
        block.append(head_goal)
    ht_lines, ft_lines, live_lines, dec_lines, said = [], [], [], [], set()
    lastof = {v for f, v in d["flags"].items() if f.endswith("|last") and isinstance(v, str)}       # paris qui sont le dernier match d'un coupon en jeu
    for t, leg, k, _ in legs:
        prevl = d["legs"].get(k, {})
        stats = {}
        if need(leg)[0] not in (None, "goals"):
            if g["id"] not in cache_stats:
                cache_stats[g["id"]] = (stats_for or (lambda x: _stats(s365, x)))(g)
            stats = cache_stats[g["id"]]
        goal_leg = leg.get("market") in GOAL_MARKETS
        if goal_leg and et:
            if not s90:
                continue                                  # prolongation sans le score à 90 min : rien n'est tranché sur un but de prolongation
            st, detail = state(leg, *s90, stats, True)
        else:
            st, detail = state(leg, *score, stats, finished)
        if st is None:
            continue
        tag = _from(leg["key"], tix)                     # « · 🎟️ 4G2MJ, LJVKJ (perdu) » : de quels coupons vient ce pari
        if leg["key"] in lastof and leg["key"] in alive_legs:
            tag += " · 🔥 dernier match du coupon"
        line = None
        first = leg["key"] not in said
        said.add(leg["key"])
        decided_now = goal_leg and et and s90
        if prevl.get("prov") and prevl.get("state") in ("win", "loss"):
            if st != prevl["state"]:
                line = (f"⚠️ Corrigé : {match} : {leg.get('pick')} : finalement {'gagné' if st == 'win' else 'perdu'} ({detail}){tag}" if st in ("win", "loss")
                        else f"⚠️ Corrigé : {match} : {leg.get('pick')} : de nouveau en jeu ({sc}){tag}")
        elif st in ("win", "loss") and prevl.get("state") not in ("win", "loss"):
            shown = f"{detail} à 90 min" if decided_now else (detail if detail == sc else f"{detail}, {sc}")
            line = f"{'✅' if st == 'win' else '❌'} {match} : {leg.get('pick')} ({shown}){tag}"
        elif st == "live" and first:
            now_txt = _leg_now(leg, *score, stats)
            p, rhythm = _chance(leg, gs, score, st_g, stats) if not et else (None, "")
            if p is not None:
                for t2, l2, k2, _ in legs:
                    if l2["key"] == leg["key"]:
                        d["legs"].setdefault(k2, {})["p"] = round(p, 3)
            now_txt = rhythm or now_txt
            if head_goal and goal_leg:
                live_lines.append(f"   {leg.get('pick')}" + (f" : {now_txt}" if now_txt else "") + _ch_txt(p, leg) + tag)
            if st_g.get("ht") and not gs.get("ht"):
                ht_lines.append(f"   {fmt.market_icon(leg)} {leg.get('pick')}" + (f" : {now_txt}" if now_txt else "") + _ch_txt(p, leg) + tag)
            p0 = leg.get("prob")
            if (p is not None and p0 and leg["key"] in alive_legs and p < C.WATCH_DANGER_P and p0 - p >= DANGER_DROP and int(st_g.get("minute") or 0) >= 20
                    and not prevl.get("danger")):
                _out(d, now, "danger", fmt.esc(f"⚠️ Pari en danger · {match} ({st_g.get('minute')}e, {sc}) : {leg.get('pick')} · {inplay.pct(p)} "
                                               f"({inplay.pct(p0)} avant le match)" + _from(leg["key"], tix, alive_only=True)),
                     tk=list((tix or {}).get(("tk", leg["key"]), [])))
                for t2, l2, k2, _ in legs:
                    if l2["key"] == leg["key"]:
                        d["legs"].setdefault(k2, {})["danger"] = 1
                prevl = d["legs"].get(k, {})
        rec = {"state": st, "score": sc, "gid": g["id"], "at": now.isoformat(timespec="minutes")}
        if st in ("win", "loss") and not finished and not decided_now:
            rec["prov"] = 1                              # décidé en cours de match : revu jusqu'à la fin (but annulé)
        cur = d["legs"].get(k, {})
        d["legs"][k] = {**{x: cur[x] for x in ("nf", "danger", "p", "pre") if x in cur and (x != "p" or st == "live")}, **rec}
        if line and first:
            (ft_lines if finished else dec_lines).append(line)
            tid = t.get("tid") or t["what"]
            if tid in alive or line.startswith("⚠️ Corrigé"):     # pied : tickets en jeu, ou ticket déjà tranché dont un pari est corrigé
                touched[tid] = t
        elif line:
            tid = t.get("tid") or t["what"]
            if tid in alive or line.startswith("⚠️ Corrigé"):
                touched[tid] = t
    if st_g.get("ht") and not gs.get("ht") and C.WATCH_SUMMARIES:
        gs["ht"] = 1
        events[f"⏸️ Mi-temps · {home} {sc} {away}" + ("\n" + "\n".join(ht_lines) if ht_lines else "")] = {"ht": f"{home} {sc} {away}"}
    block += live_lines + dec_lines                       # titre, paris encore en jeu, puis paris décidés
    if finished and C.WATCH_SUMMARIES and not gs.get("ft"):
        gs["ft"] = 1
        shown = f"{gs['s90']} à 90 min, {sc} au final" if et and s90 and gs["s90"] != sc else sc
        block.append(f"🏁 Terminé · {home} {shown} {away}")
    block += ft_lines
    gs["score"] = sc
    if block:
        events["\n".join(block)] = {}


def _last_legs(d, now, ts, alive):
    """« 🔥 PLUS QU'UN MATCH ! » (message à part) : un ticket encore en jeu (au moins 2 paris) dont tous les paris sont gagnés sauf UN encore en jeu, avec
    ce que rapporte le coupon s'il passe ; une fois par ticket (d["flags"]["<tid>|last"]). Ensuite la ligne de ce pari dit « 🔥 dernier match du coupon »."""
    from . import money
    for t in ts:
        tid = t.get("tid") or t["what"]
        if tid not in alive or len(t["legs"]) < 2 or d["flags"].get(f"{tid}|last"):
            continue
        st = [(leg, d["legs"].get(f"{t['what']}|{leg['key']}", {})) for leg in t["legs"]]
        rest = [(leg, cur) for leg, cur in st if cur.get("state") not in ("win", "loss")]
        if len(rest) != 1 or any(cur.get("state") == "loss" for _, cur in st):
            continue
        leg, cur = rest[0]
        ko = _ko(leg)
        gain = money.money(round((t.get("odds") or 0) * (t.get("stake") or 0)))
        where = f"⏳ {leg.get('home')} – {leg.get('away')} · {leg.get('pick')}"
        if need(leg)[0] is None:
            where += " (pas suivi en direct)"
        elif ko and ko > now:
            where += f" · coup d'envoi {ko:%Hh%M} UTC"
        else:
            where += (f" ({cur['score']})" if cur.get("score") else "") + (f" · {inplay.pct(cur['p'])} de chances" if cur.get("p") is not None else "")
        txt = "\n".join(["🔥 " + fmt.b("PLUS QU'UN MATCH !") + fmt.esc(f" · 🎫 {t['name']}" + (f" · 🎟️ {t['coupon']}" if t.get("coupon") else "")),
                         fmt.esc(f"✅ {len(t['legs']) - 1}/{len(t['legs'])} déjà gagnés"), fmt.esc(where),
                         fmt.esc("💰 Si ça passe : ") + fmt.b(fmt.esc(gain))])
        _out(d, now, "last", txt, tk=[_tk(t)])
        d["flags"][f"{tid}|last"] = leg["key"]


def _recap_day(d, now):
    """Jour dont le bilan de la soirée est dû, ou None : le jour même dès C.WATCH_RECAP_HOUR UTC quand plus aucun match de ses tickets n'est en cours ;
    la veille entre minuit et 3 h dans le même cas (matchs des Amériques), et à 3 h au plus tard."""
    if not C.WATCH_RECAP:
        return None
    for day, forced in ((now.date(), False), (now.date() - timedelta(days=1), now.hour >= 3)):
        if str(day) == (d["recap"] or {}).get("day"):
            continue
        if day == now.date() and now.hour < C.WATCH_RECAP_HOUR:
            continue
        if day != now.date() and now.hour >= C.WATCH_RECAP_HOUR:
            continue
        ts = exposure.tickets(str(day))
        if not ts:
            continue
        running = any(_ko(leg) and _ko(leg) <= now < _ko(leg) + timedelta(minutes=FINISH_MIN + 10)
                      and d["legs"].get(f"{t['what']}|{leg['key']}", {}).get("state") not in ("win", "loss") for t in ts for leg in t["legs"])
        if forced or not running:
            return day, ts
    return None


def _recap(d, now):
    """🌙 Bilan de la soirée, une fois par jour (d["recap"]) : tickets gagnés / perdus / en jeu, argent des tickets tranchés, tickets perdus d'un seul pari,
    paris gagnés. Chiffres du suivi en direct (le règlement officiel suit le lendemain matin)."""
    from . import money
    got = _recap_day(d, now)
    if not got:
        return
    day, ts = got
    won = lost = 0
    staked = cashed = 0.0
    near, legs_won, legs_done = [], set(), set()
    for t in ts:
        states = [(leg, d["legs"].get(f"{t['what']}|{leg['key']}", {})) for leg in t["legs"]]
        verdict = _verdict(d, t) or _ticket_line(t, d)[0]
        for leg, cur in states:
            if cur.get("state") in ("win", "loss"):
                legs_done.add(leg["key"])
                if cur["state"] == "win":
                    legs_won.add(leg["key"])
        if verdict == "won":
            won += 1
            staked += t.get("stake") or 0
            cashed += (t.get("odds") or 0) * (t.get("stake") or 0)
        elif verdict == "lost":
            lost += 1
            staked += t.get("stake") or 0
            losses = [(leg, cur) for leg, cur in states if cur.get("state") == "loss"]
            if len(losses) == 1 and all(cur.get("state") == "win" for leg, cur in states if (leg, cur) != losses[0]) and len(states) > 1:
                leg, cur = losses[0]
                near.append(f"   • {t['name']} ({len(states) - 1}/{len(states)}) : {leg.get('home')} – {leg.get('away')}, {leg.get('pick')} ({cur.get('score')})")
    open_n = len(ts) - won - lost
    lines = [f"🌙 {fmt.b('Bilan de la soirée')} · {fmt.DAYS[day.weekday()]} {day:%d/%m}",
             fmt.esc(f"🎫 {len(ts)} ticket{'s' if len(ts) > 1 else ''} · 🏆 {won} gagné{'s' if won > 1 else ''} · ❌ {lost} perdu{'s' if lost > 1 else ''}"
                     + (f" · ⏳ {open_n} encore en jeu" if open_n else ""))]
    if won or lost:
        lines.append(fmt.esc(f"💰 Misé {money.money(round(staked))} · encaissé {money.money(round(cashed))} · {money.signed(round(cashed - staked))}"))
    if near:
        lines += [fmt.esc("😬 Perdus d'un seul pari :")] + [fmt.esc(x) for x in near[:5]] + ([fmt.esc(f"   … et {len(near) - 5} autres")] if len(near) > 5 else [])
    if legs_done:
        lines.append(fmt.esc(f"✅ {len(legs_won)} paris gagnés sur {len(legs_done)} ({round(100 * len(legs_won) / len(legs_done))} %)"))
    lines.append(fmt.i(fmt.esc("Chiffres du suivi en direct ; règlement officiel demain matin.")))
    _out(d, now, "recap", "\n".join(lines))
    d["recap"] = {"day": str(day)}


def follow(now=None, games=None, live_for=None, stats_for=None, final_for=None, keep=False, detail_for=None, fm_for=None):
    """Suivi des tickets : -> le message (Html) ou None s'il n'y a rien de neuf. `games` ({id: match}), `live_for(ids)`, `stats_for(g)`, `final_for(g)`,
    `detail_for(g)` (fiche détaillée, parse_detail), `fm_for()` (secours FotMob : ({id: match avec « fm »}, cause)) :
    injectables (tests) ; sinon 365scores via le proxy. `keep` : les lignes restent dans le document jusqu'à leur envoi (tick les retire ensuite) ;
    sinon elles sont rendues et retirées tout de suite. Lève WatchStateError si le document ne peut pas être lu."""
    now = now or _now()
    d = _load()
    _purge(d, now)
    _follow(d, now, games, live_for, stats_for, final_for, detail_for, fm_for)
    plan_next(now, d=d)                              # ⏰ le prochain « dans 5 min » : le runner déclenche un passage à cette heure
    try:
        _recap(d, now)
    except Exception as e:                          # noqa: BLE001 - le bilan ne bloque jamais le suivi
        print(f"[suivi] bilan de la soirée ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    msg = compose(d["out"])
    if not keep:
        d["out"] = []
    store.save(FILE, d)
    return msg


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


def _lock():
    """Verrou NON bloquant data/.watch.lock (fichier ouvert à fermer) ; None si un autre passage du suivi tourne déjà."""
    import fcntl
    try:
        os.makedirs(store.DATA_DIR, exist_ok=True)
        f = open(store.path(".watch.lock"), "a")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def _tickets(now):
    """Suivi des tickets puis envoi du message : ses lignes ne quittent le document qu'une fois envoyées ou mises en attente par Telegram ; après
    SEND_TRIES échecs elles sont abandonnées (note sur stderr). Une erreur imprévue compte comme une panne (santé, avertissement)."""
    try:
        msg = follow(now, keep=True)
    except WatchStateError as e:
        print(f"[suivi] tickets non suivis : document du suivi illisible ({e}) : rien envoyé", file=sys.stderr)
        try:
            store.save("watch_health.json", {"at": now.isoformat(timespec="minutes"), "ok": False, "why": f"document illisible : {e}"})
        except OSError:
            pass
        return
    except Exception as e:                          # noqa: BLE001 - jamais muet : la panne est comptée et, si elle dure, dite
        print(f"[suivi] tickets ignorés : {http.err(e)}", file=sys.stderr)
        try:
            d = _load()
            _health(d, now, False, f"erreur interne ({type(e).__name__})", int(d["health"].get("open") or 0))
            store.save(FILE, d)
            msg = compose(d["out"])
        except Exception:                           # noqa: BLE001
            return
    if not msg:
        return
    try:
        d = _load()
    except WatchStateError:
        return
    sent_lines = list(d["out"])
    if C.WATCH_QUIET:                                    # gardé pour le réveil la nuit : chaque message (groupé, puis ⏰ 🔥 🎉 à part) l'un après l'autre
        ok = all([quiet.send(m, now, kind="suivi") for m in [msg, *getattr(msg, "more", ())]])
    else:
        ok = telegram.send(msg)                          # par défaut : à toute heure, même la nuit (le groupé, puis ses messages à part)
    queued = telegram.LAST_QUEUED
    if ok or queued:
        d["out"] = [o for o in d["out"] if o not in sent_lines]
        d["health"]["send_fails"] = 0
        print(f"[suivi] tickets : message {'envoyé' if ok and not queued else 'mis en attente'}", file=sys.stderr)
    else:
        n = int(d["health"].get("send_fails") or 0) + 1
        d["health"]["send_fails"] = n
        if n >= SEND_TRIES:
            d["out"] = [o for o in d["out"] if o not in sent_lines]
            d["health"]["send_fails"] = 0
            print(f"[suivi] tickets : envoi impossible {n} fois de suite, {len(sent_lines)} ligne(s) abandonnée(s)", file=sys.stderr)
        else:
            print(f"[suivi] tickets : envoi échoué, gardé pour le prochain passage ({n}/{SEND_TRIES})", file=sys.stderr)
    store.save(FILE, d)


SEND_TRIES = 3


def tick(now=None):
    """Le passage de 10 minutes : alertes gardées la nuit (quiet.flush), suivi des tickets, opportunité live. Un seul passage à la fois (verrou),
    délai global C.WATCH_DEADLINE_S pour les téléchargements. Ne lève jamais."""
    now = now or _now()
    lock = _lock()
    if lock is None:
        print("[suivi] un autre passage du suivi tourne déjà : rien cette fois", file=sys.stderr)
        return
    http.set_deadline(C.WATCH_DEADLINE_S)
    try:
        try:
            n = quiet.flush(now)
            if not C.WATCH_QUIET:                       # suivi des matchs gardé avant ce réglage (ou par un ancien passage) : part tout de suite
                n += quiet.flush(now, follow_only=True)
            if n:
                print(f"[suivi] alertes de la nuit : {n} envoyée(s)", file=sys.stderr)
        except Exception as e:                      # noqa: BLE001
            print(f"[suivi] alertes de la nuit ignorées : {http.err(e)}", file=sys.stderr)
        _tickets(now)
        board(now)                                  # 📌 le tableau du jour, modifié sur place
        try:
            msg = opportunity(now)
            if msg:
                quiet.send(msg, now)
                print("[suivi] opportunité live : message envoyé", file=sys.stderr)
        except Exception as e:                      # noqa: BLE001
            print(f"[suivi] opportunité live ignorée : {http.err(e)}", file=sys.stderr)
    finally:
        http.set_deadline(None)
        lock.close()
