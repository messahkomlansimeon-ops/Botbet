"""Cotes (The Odds API) et construction des sélections."""
import os
import sys
from datetime import datetime, timezone

import requests

from . import config as C, learn


def sport_group(sport_key):
    return sport_key.split("_", 1)[0]


def get(path, **params):
    params["apiKey"] = os.environ["ODDS_API_KEY"]
    r = requests.get(f"{C.ODDS_API}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def list_sports():
    out = []
    for s in get("/sports"):
        if s.get("has_outrights"):
            continue
        if any(s["key"].startswith(p) for p in C.SPORTS):
            out.append(s)
    return out[:C.MAX_SPORTS]


def fetch_events(day, props=True):
    """Matchs du jour `day` (date UTC) sur tous les sports suivis + marchés détaillés."""
    window = {"commenceTimeFrom": f"{day}T00:00:00Z", "commenceTimeTo": f"{day}T23:59:59Z"}
    events = []
    for s in list_sports():
        group = sport_group(s["key"])
        try:
            for ev in get(f"/sports/{s['key']}/odds", regions="eu", oddsFormat="decimal",
                          markets=C.FEATURED.get(group, "h2h"), **window):
                ev["sport_key"] = s["key"]
                events.append(ev)
        except requests.HTTPError as e:
            print(f"[{s['key']}] ignoré : {e}", file=sys.stderr)
    if not props:
        return events
    events.sort(key=lambda e: -len(e.get("bookmakers", [])))
    for ev in events[:C.PROP_EVENTS]:
        for market in C.PROP_MARKETS.get(sport_group(ev["sport_key"]), []):
            try:
                data = get(f"/sports/{ev['sport_key']}/events/{ev['id']}/odds", regions="eu",
                           markets=market, oddsFormat="decimal")
            except requests.HTTPError:
                continue
            for bk in data.get("bookmakers", []):
                mine = next((b for b in ev["bookmakers"] if b["key"] == bk["key"]), None)
                if mine is None:
                    ev["bookmakers"].append(bk)
                else:
                    mine["markets"].extend(bk["markets"])
    return events


def fetch_scores(sport_key, days=3):
    return get(f"/sports/{sport_key}/scores", daysFrom=days)


def label(market, name, point, who, group):
    unit = C.UNITS.get(market)
    if isinstance(unit, dict):
        unit = unit.get(group, "")
    if unit:
        side = "Plus" if name == "Over" else "Moins"
        return f"{who + ' : ' if who else ''}{side} de {point} {unit}"
    if market == "btts":
        return "Les 2 équipes marquent : " + ("Oui" if name == "Yes" else "Non")
    if market == "spreads":
        return f"{name} {point:+g} (handicap)"
    if market == "double_chance":
        return "Double chance : " + name.replace(" or ", " ou ").replace("Draw", "Nul")
    return "Match nul" if name == "Draw" else name


def market_weight(market):
    return C.PLAYER_WEIGHT if market.startswith("player_") else C.MARKET_WEIGHT.get(market, 1.2)


def build_picks(events, day, after=None, history=None, model=None):
    """Toutes les sélections : proba de consensus, meilleure cote, valeur et flair.

    score = valeur (meilleure cote vs consensus)
          + désaccord entre bookmakers (un book en retard = opportunité)
          + mouvement de la cote depuis le dernier relevé (argent des pros)
          x poids du marché (secondaires = mal pricés)
    Si un modèle appris est adopté (learn.train_clv), le score devient le CLV
    prédit, et la proba est calibrée par les résultats quand c'est actif.
    """
    now = datetime.now(timezone.utc)
    history = history or {}
    model = model or {}
    picks = []
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start.date() != day or (after and start <= after):
            continue
        group = sport_group(ev.get("sport_key", "soccer_"))
        probs, best, bests_book = {}, {}, {}
        for bk in ev.get("bookmakers", []):
            for m in bk["markets"]:
                groups = {}
                for o in m["outcomes"]:
                    groups.setdefault((o.get("description"), o.get("point")), []).append(o)
                for (who, point), outs in groups.items():
                    if len(outs) < 2:
                        continue
                    total = sum(1 / o["price"] for o in outs)
                    for o in outs:
                        k = (m["key"], who, point, o["name"])
                        probs.setdefault(k, []).append(1 / o["price"] / total)
                        if o["price"] > best.get(k, 0):
                            best[k], bests_book[k] = o["price"], bk.get("title", bk["key"])
        for k, ps in probs.items():
            if len(ps) < C.MIN_BOOKMAKERS:
                continue
            market, who, point, name = k
            p, odds = sum(ps) / len(ps), best[k]
            if not C.ODDS_RANGE[0] <= odds <= C.ODDS_RANGE[1]:
                continue
            key = f'{ev["id"]}|{market}|{who or ""}|{point if point is not None else ""}|{name}'
            edge = p * odds - 1
            spread = (max(ps) - min(ps)) / p
            prev = history.get(key)
            prev_p = prev["p"] if isinstance(prev, dict) else prev
            steam = (p - prev_p) / prev_p if prev_p else 0.0
            weight = market_weight(market)
            signals = []
            if edge > 0.03:
                signals.append(f"cote {odds:.2f} chez {bests_book[k]} > consensus {1/p:.2f}")
            if spread > 0.12:
                signals.append("books en désaccord")
            if steam > 0.04:
                signals.append(f"cote en baisse ({steam*100:+.0f}% de proba)")
            p_cal = learn.calibrated(model, market, p)
            c = {
                "key": key, "id": ev["id"], "sport_key": ev.get("sport_key", ""), "group": group,
                "event": f'{ev["home_team"]} - {ev["away_team"]}',
                "home": ev["home_team"], "away": ev["away_team"],
                "league": ev.get("sport_title", ""), "start": start.strftime("%H:%M"),
                "commence": ev["commence_time"],
                "market": market, "who": who, "point": point, "name": name,
                "pick": label(market, name, point, who, group),
                "prob": p_cal, "prob_book": p, "odds": odds, "book": bests_book[k], "edge": p_cal * odds - 1,
                "spread": spread, "steam": steam, "n_books": len(ps), "weight": weight,
                "score": (edge + 0.5 * spread + 2 * steam) * weight + (weight - 1) * 0.05,
                "why": ", ".join(signals),
            }
            if model and model.get("adopted"):
                c["heuristic"] = c["score"]
                c["score"] = learn.predict(model, learn.features(c, now))
            picks.append(c)
    return picks


def snapshot(picks):
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    return {c["key"]: {"p": round(c["prob"], 4), "o": c["odds"], "t": now} for c in picks}
