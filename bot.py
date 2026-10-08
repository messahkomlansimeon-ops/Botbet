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
ODDS_RANGE = (1.15, 3.50)


def fetch_events(api_key):
    r = requests.get(
        ODDS_URL,
        params={"apiKey": api_key, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def label(market, name, point, home, away):
    if market == "totals":
        return f'{"Plus" if name == "Over" else "Moins"} de {point} buts'
    return "Match nul" if name == "Draw" else name


def build_picks(events, today=None):
    """Une sélection par issue : proba de consensus, meilleure cote, valeur."""
    today = today or datetime.now(timezone.utc).date()
    picks = []
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start.date() != today:
            continue
        probs, best, books = {}, {}, {}
        for bk in ev.get("bookmakers", []):
            for m in bk["markets"]:
                groups = {}
                for o in m["outcomes"]:
                    groups.setdefault(o.get("point"), []).append(o)
                for point, outs in groups.items():
                    total = sum(1 / o["price"] for o in outs)
                    for o in outs:
                        k = (m["key"], point, o["name"])
                        probs.setdefault(k, []).append(1 / o["price"] / total)
                        best[k] = max(best.get(k, 0), o["price"])
        for k, ps in probs.items():
            if len(ps) < MIN_BOOKMAKERS:
                continue
            market, point, name = k
            p, odds = sum(ps) / len(ps), best[k]
            if not ODDS_RANGE[0] <= odds <= ODDS_RANGE[1]:
                continue
            picks.append({
                "event": f'{ev["home_team"]} - {ev["away_team"]}',
                "league": ev.get("sport_title", ""),
                "start": start.strftime("%H:%M UTC"),
                "pick": label(market, name, point, ev["home_team"], ev["away_team"]),
                "prob": p, "odds": odds, "edge": p * odds - 1, "id": ev["id"],
            })
    return picks


def make_combo(picks, size, key, min_odds=0.0):
    """Meilleur combiné de `size` matchs différents selon `key`."""
    pool = sorted(picks, key=key, reverse=True)[:14]
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
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    parts = [f"🔥 COMBINÉS DU {today} 🔥"]
    if picks:
        top = max(picks, key=lambda c: c["edge"])
        parts.append(
            f'⭐ PARI DU JOUR\n{top["start"]} {top["event"]}\n→ {top["pick"]} @ {top["odds"]:.2f} '
            f'(proba {top["prob"]*100:.0f}%)'
        )
    parts += [
        fmt_combo("🛡️ SÛR (3 matchs, proba max)", make_combo(picks, 3, key=lambda c: c["prob"])),
        fmt_combo("⚖️ ÉQUILIBRÉ (4 matchs, cote >= 4)", make_combo(picks, 4, key=lambda c: c["edge"], min_odds=4.0)),
        fmt_combo("🚀 GROS COUP (5 matchs, cote >= 8)", make_combo(picks, 5, key=lambda c: c["edge"], min_odds=8.0)),
        "Probabilités estimées, aucun gain garanti. Ne mise que ce que tu peux perdre. 18+",
    ]
    return "\n\n".join(parts)


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
