"""Statistiques réelles (API-Football, api-sports.io). Optionnel : sans clé
STATS_API_KEY, tout renvoie None et le bot fonctionne avec les cotes seules.

Sert à deux choses :
1. régler les paris corners / cartons / stats joueurs (résultats réels) ;
2. estimer les lignes corners/cartons avant le match (moyennes récentes des
   deux équipes) pour repérer les lignes mal placées par les bookmakers.
"""
import difflib
import math
import os
from datetime import datetime, timezone

import requests

from . import store

BASE = "https://v3.football.api-sports.io"
BUDGET = int(os.environ.get("STATS_BUDGET", 60))     # appels max par exécution
LAST_N = 6
_calls = 0


def enabled():
    return bool(os.environ.get("STATS_API_KEY"))


def get(path, **params):
    global _calls
    if not enabled() or _calls >= BUDGET:
        return None
    _calls += 1
    r = requests.get(f"{BASE}{path}", params=params, timeout=30,
                     headers={"x-apisports-key": os.environ["STATS_API_KEY"]})
    r.raise_for_status()
    return r.json().get("response") or None


def similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_fixture(day, home, away):
    """Fixture API-Football d'un match (nom d'équipes approchés)."""
    cache = store.load("fixtures.json", {})
    k = f"{day}|{home}|{away}"
    if k in cache:
        return cache[k]
    fixtures = get("/fixtures", date=str(day)) or []
    best, best_s = None, 0.0
    for f in fixtures:
        s = similar(f["teams"]["home"]["name"], home) + similar(f["teams"]["away"]["name"], away)
        if s > best_s:
            best, best_s = f, s
    found = best if best and best_s > 1.3 else None
    if found:
        cache[k] = {"id": found["fixture"]["id"], "home_id": found["teams"]["home"]["id"],
                    "away_id": found["teams"]["away"]["id"], "status": found["fixture"]["status"]["short"]}
        store.save("fixtures.json", cache)
        return cache[k]
    return None


def _num(v):
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def fixture_stats(fixture_id):
    """Totaux du match : corners, cartons, et stats par joueur. Mis en cache."""
    cache = store.load("stats_cache.json", {})
    if str(fixture_id) in cache:
        return cache[str(fixture_id)]
    teams = get("/fixtures/statistics", fixture=fixture_id)
    if not teams:
        return None
    out = {"corners": 0, "cards": 0, "shots": 0, "fouls": 0, "teams": {}, "players": {}}
    for t in teams:
        st = {s["type"]: _num(s["value"]) for s in t["statistics"]}
        tv = {"corners": st.get("Corner Kicks", 0), "cards": st.get("Yellow Cards", 0) + st.get("Red Cards", 0),
              "shots": st.get("Total Shots", 0), "fouls": st.get("Fouls", 0)}
        out["teams"][str(t["team"]["id"])] = tv
        for k2 in ("corners", "cards", "shots", "fouls"):
            out[k2] += tv[k2]
    players = get("/fixtures/players", fixture=fixture_id) or []
    for team in players:
        for p in team.get("players", []):
            s = (p.get("statistics") or [{}])[0]
            out["players"][p["player"]["name"]] = {
                "shots": _num((s.get("shots") or {}).get("total")),
                "shots_on_target": _num((s.get("shots") or {}).get("on")),
                "fouls": _num((s.get("fouls") or {}).get("committed")),
            }
    cache[str(fixture_id)] = out
    store.save("stats_cache.json", cache)
    return out


def player_stat(stats, name, field):
    """Stat d'un joueur par nom approché (les books écrivent « K. Mbappé »)."""
    if not stats or not stats["players"]:
        return None
    best, best_s = None, 0.0
    short = name.split(".")[-1].strip()
    for pname, st in stats["players"].items():
        s = max(similar(pname, name), similar(pname.split()[-1], short))
        if s > best_s:
            best, best_s = st, s
    return best.get(field) if best and best_s > 0.6 else None


def team_averages(team_id, day):
    """Moyennes corners/cartons pour et contre sur les derniers matchs."""
    cache = store.load("team_avg.json", {})
    k = f"{team_id}|{day}"
    if k in cache:
        return cache[k]
    fixtures = get("/fixtures", team=team_id, last=LAST_N, status="FT") or []
    tot = {"corners_for": 0, "corners_against": 0, "cards_for": 0, "cards_against": 0, "n": 0}
    for f in fixtures:
        st = fixture_stats(f["fixture"]["id"])
        if not st or str(team_id) not in st["teams"]:
            continue
        mine = st["teams"][str(team_id)]
        other = next((v for tid, v in st["teams"].items() if tid != str(team_id)), None)
        if not other:
            continue
        tot["corners_for"] += mine["corners"]; tot["corners_against"] += other["corners"]
        tot["cards_for"] += mine["cards"]; tot["cards_against"] += other["cards"]
        tot["n"] += 1
    if tot["n"] == 0:
        return None
    avg = {k2: v / tot["n"] for k2, v in tot.items() if k2 != "n"}
    avg["n"] = tot["n"]
    cache[k] = avg
    store.save("team_avg.json", cache)
    return avg


def poisson_over(mean, point):
    """P(X > point) pour X ~ Poisson(mean)."""
    k_max = int(math.floor(point))
    cdf = sum(math.exp(-mean) * mean ** k / math.factorial(k) for k in range(k_max + 1))
    return 1 - cdf


def expected_totals(day, home, away):
    """Corners et cartons attendus pour un match (None si indisponible)."""
    fx = find_fixture(day, home, away)
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
    """Mélange l'estimation « vraies stats » à la proba des books sur les
    corners/cartons : 60 % books, 40 % modèle. Ajoute un signal lisible."""
    if not enabled():
        return picks
    done = {}
    for c in picks:
        if c["group"] != "soccer" or c["market"] not in ("alternate_totals_corners", "alternate_totals_cards"):
            continue
        if c["id"] not in done:
            if len(done) >= max_events:
                continue
            done[c["id"]] = expected_totals(day, c["home"], c["away"])
        exp = done[c["id"]]
        if not exp:
            continue
        kind = "corners" if "corners" in c["market"] else "cards"
        p_model = poisson_over(exp[kind], c["point"])
        if c["name"] == "Under":
            p_model = 1 - p_model
        old = c["prob"]
        c["prob"] = 0.6 * old + 0.4 * p_model
        c["edge"] = c["prob"] * c["odds"] - 1
        c["score"] += (c["edge"] - (old * c["odds"] - 1)) * c["weight"]
        if abs(p_model - old) > 0.08:
            unit = "corners" if kind == "corners" else "cartons"
            tag = f"stats réelles : {exp[kind]:.1f} {unit} attendus vs ligne {c['point']}"
            c["why"] = ", ".join(filter(None, [c["why"], tag]))
    return picks
