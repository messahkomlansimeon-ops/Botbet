"""Botbet : envoie sur Telegram les meilleurs combinés de la journée.

Source des cotes : The Odds API (https://the-odds-api.com, offre gratuite).
Méthode : probabilité de consensus des bookmakers (marge retirée), puis
sélection des paris à forte probabilité / valeur positive et assemblage en combinés.
"""
import itertools
import json
import os
import sys
from datetime import datetime, timezone

import requests

ODDS_URL = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
MIN_BOOKMAKERS = 3
ODDS_RANGE = (1.20, 2.50)


def fetch_events(api_key):
    r = requests.get(
        ODDS_URL,
        params={"apiKey": api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def build_picks(events, today=None):
    """Une sélection par issue : proba de consensus, meilleure cote, valeur."""
    today = today or datetime.now(timezone.utc).date()
    picks = []
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start.date() != today:
            continue
        probs, best = {}, {}
        books = 0
        for bk in ev.get("bookmakers", []):
            market = next((m for m in bk["markets"] if m["key"] == "h2h"), None)
            if not market:
                continue
            books += 1
            inv = {o["name"]: 1 / o["price"] for o in market["outcomes"]}
            total = sum(inv.values())
            for name, v in inv.items():
                probs.setdefault(name, []).append(v / total)
            for o in market["outcomes"]:
                best[o["name"]] = max(best.get(o["name"], 0), o["price"])
        if books < MIN_BOOKMAKERS:
            continue
        for name, ps in probs.items():
            p = sum(ps) / len(ps)
            odds = best[name]
            if not ODDS_RANGE[0] <= odds <= ODDS_RANGE[1]:
                continue
            picks.append({
                "event": f'{ev["home_team"]} - {ev["away_team"]}',
                "league": ev.get("sport_title", ""),
                "start": start.strftime("%H:%M UTC"),
                "pick": name if name != "Draw" else "Match nul",
                "prob": p,
                "odds": odds,
                "edge": p * odds - 1,
                "id": ev["id"],
            })
    return picks


def make_combo(picks, size, key, min_odds=0.0):
    """Meilleur combiné de `size` matchs différents selon `key`."""
    pool = sorted(picks, key=key, reverse=True)[:12]
    best = None
    for combo in itertools.combinations(pool, size):
        if len({c["id"] for c in combo}) < size:
            continue
        odds = 1.0
        prob = 1.0
        for c in combo:
            odds *= c["odds"]
            prob *= c["prob"]
        if odds < min_odds:
            continue
        score = key({"prob": prob, "odds": odds, "edge": prob * odds - 1})
        if best is None or score > best[0]:
            best = (score, combo, odds, prob)
    return best


def fmt_combo(title, combo):
    if not combo:
        return f"{title}\nPas assez de matchs aujourd'hui."
    _, picks, odds, prob = combo
    lines = [title]
    for c in picks:
        lines.append(f'• {c["start"]} {c["event"]} ({c["league"]})\n  → {c["pick"]} @ {c["odds"]:.2f}')
    lines.append(f"Cote totale : {odds:.2f} | Proba estimée : {prob*100:.0f}%")
    return "\n".join(lines)


def build_message(picks):
    safe = make_combo(picks, 3, key=lambda c: c["prob"])
    value = make_combo(picks, 3, key=lambda c: c["edge"], min_odds=3.0)
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    return "\n\n".join([
        f"Combinés du {today}",
        fmt_combo("SÛR (probabilité maximale)", safe),
        fmt_combo("VALEUR (meilleure valeur, cote >= 3)", value),
        "Probabilités estimées, aucun gain garanti. Ne mise que ce que tu peux perdre. 18+",
    ])


def send_telegram(text, token, chat_id):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    r.raise_for_status()


def main():
    if "--sample" in sys.argv:
        with open(os.path.join(os.path.dirname(__file__), "tests", "sample_events.json")) as f:
            events = json.load(f)
        today = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
    else:
        events, today = fetch_events(os.environ["ODDS_API_KEY"]), None
    text = build_message(build_picks(events, today))
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat and "--sample" not in sys.argv:
        send_telegram(text, token, chat)
    else:
        print(text)


if __name__ == "__main__":
    main()
