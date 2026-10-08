"""Botbet : envoie sur Telegram les meilleurs combinés de la journée.

Source des cotes : The Odds API (https://the-odds-api.com).
Méthode : probabilité de consensus des bookmakers (marge retirée), une sélection
par match, puis combinés qui visent une cote cible avec la plus grande probabilité.
"""
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.the-odds-api.com/v4"
MIN_BOOKMAKERS = 3
ODDS_RANGE = (1.15, 6.0)
MAX_SPORTS = int(os.environ.get("MAX_SPORTS", 25))          # championnats interrogés
PROP_EVENTS = int(os.environ.get("PROP_EVENTS", 8))         # matchs avec marchés détaillés
FEATURED = os.environ.get("FEATURED_MARKETS", "h2h,totals")
# Marchés détaillés (1 appel par match et par marché). Ceux que ton forfait ne
# fournit pas sont ignorés sans erreur.
PROP_MARKETS = os.environ.get(
    "PROP_MARKETS",
    "btts,double_chance,alternate_totals_corners,alternate_totals_cards,"
    "player_shots_on_target,player_shots,player_fouls",
).split(",")
# (tier, titre, nb de matchs, cote minimale)
COMBOS = [
    ("🛡️ SÛR (cote >= 5)", 3, 5.0),
    ("⚖️ COSTAUD (cote >= 10)", 5, 10.0),
    ("🔥 MÉCHANT (cote >= 40)", 7, 40.0),
    ("💀 MONSTRE (cote >= 120)", 9, 120.0),
]
# Les marchés secondaires sont moins bien pricés par les bookmakers : bonus.
MARKET_WEIGHT = {"h2h": 1.0, "totals": 1.0, "double_chance": 1.0, "btts": 1.1,
                 "alternate_totals_corners": 1.3, "alternate_totals_cards": 1.3}
PLAYER_WEIGHT = 1.4                      # player_* : les plus inefficaces
HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "odds_history.json")
UNITS = {
    "totals": "buts", "alternate_totals": "buts",
    "totals_corners": "corners", "alternate_totals_corners": "corners",
    "totals_cards": "cartons", "alternate_totals_cards": "cartons",
    "player_shots_on_target": "tirs cadrés", "player_shots": "tirs",
    "player_fouls": "fautes", "player_fouls_committed": "fautes",
}


def get(path, **params):
    params["apiKey"] = os.environ["ODDS_API_KEY"]
    r = requests.get(f"{API}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_events(today):
    """Tous les matchs de foot du jour (plusieurs championnats) + marchés détaillés."""
    window = {
        "commenceTimeFrom": f"{today}T00:00:00Z",
        "commenceTimeTo": f"{today}T23:59:59Z",
    }
    sports = [s for s in get("/sports") if s["key"].startswith("soccer_") and not s.get("has_outrights")]
    events = []
    for s in sports[:MAX_SPORTS]:
        try:
            for ev in get(f"/sports/{s['key']}/odds", regions="eu", markets=FEATURED,
                          oddsFormat="decimal", **window):
                ev["sport_key"] = s["key"]
                events.append(ev)
        except requests.HTTPError as e:
            print(f"[{s['key']}] ignoré : {e}", file=sys.stderr)
    events.sort(key=lambda e: -len(e.get("bookmakers", [])))
    for ev in events[:PROP_EVENTS]:
        for market in PROP_MARKETS:
            try:
                data = get(f"/sports/{ev['sport_key']}/events/{ev['id']}/odds", regions="eu",
                           markets=market, oddsFormat="decimal")
            except requests.HTTPError:
                continue                      # marché indisponible : on passe
            for bk in data.get("bookmakers", []):
                mine = next((b for b in ev["bookmakers"] if b["key"] == bk["key"]), None)
                if mine is None:
                    ev["bookmakers"].append(bk)
                else:
                    mine["markets"].extend(bk["markets"])
    return events


def label(market, name, point, who):
    unit = UNITS.get(market)
    if unit:
        side = "Plus" if name == "Over" else "Moins"
        return f"{who + ' : ' if who else ''}{side} de {point} {unit}"
    if market == "btts":
        return "Les 2 équipes marquent : " + ("Oui" if name == "Yes" else "Non")
    return "Match nul" if name == "Draw" else name


def load_history():
    try:
        with open(HISTORY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_history(picks):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w") as f:
        json.dump({c["key"]: round(c["prob"], 4) for c in picks}, f)


def build_picks(events, today=None, after=None, history=None):
    """Toutes les sélections : proba de consensus, meilleure cote, valeur et flair.

    score = valeur (meilleure cote vs consensus)
          + désaccord entre bookmakers (un book en retard = opportunité)
          + mouvement de la cote depuis le dernier passage (argent des pros)
          x poids du marché (les marchés secondaires sont moins efficaces)
    """
    today = today or datetime.now(timezone.utc).date()
    history = history or {}
    picks = []
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start.date() != today or (after and start <= after):
            continue
        probs, best = {}, {}
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
                        best[k] = max(best.get(k, 0), o["price"])
        for k, ps in probs.items():
            if len(ps) < MIN_BOOKMAKERS:
                continue
            market, who, point, name = k
            p, odds = sum(ps) / len(ps), best[k]
            if not ODDS_RANGE[0] <= odds <= ODDS_RANGE[1]:
                continue
            key = f'{ev["id"]}|{market}|{who or ""}|{point or ""}|{name}'
            edge = p * odds - 1
            spread = (max(ps) - min(ps)) / p            # désaccord relatif des books
            steam = (p - history[key]) / history[key] if key in history else 0.0
            weight = PLAYER_WEIGHT if market.startswith("player_") else MARKET_WEIGHT.get(market, 1.2)
            signals = []
            if edge > 0.03:
                signals.append(f"cote {odds:.2f} > consensus {1/p:.2f}")
            if spread > 0.12:
                signals.append("books en désaccord")
            if steam > 0.04:
                signals.append(f"cote en baisse ({steam*100:+.0f}% de proba)")
            picks.append({
                "event": f'{ev["home_team"]} - {ev["away_team"]}',
                "league": ev.get("sport_title", ""),
                "start": start.strftime("%H:%M"),
                "pick": label(market, name, point, who),
                "prob": p, "odds": odds, "edge": edge, "id": ev["id"], "key": key,
                "score": (edge + 0.5 * spread + 2 * steam) * weight,
                "why": ", ".join(signals),
            })
    return picks


def one_per_event(picks):
    """Un seul pari par match (les paris d'un même match sont corrélés)."""
    best = {}
    for c in picks:
        if c["id"] not in best or c["score"] > best[c["id"]]["score"]:
            best[c["id"]] = c
    return list(best.values())


def make_combo(pool, size, min_odds):
    """Combiné de `size` matchs qui atteint `min_odds` avec la plus forte proba."""
    pool = sorted(pool, key=lambda c: c["score"], reverse=True)[:16]
    best = None
    for combo in itertools.combinations(pool, size):
        odds = prob = 1.0
        for c in combo:
            odds *= c["odds"]
            prob *= c["prob"]
        if odds >= min_odds and (best is None or prob > best[2]):
            best = (combo, odds, prob)
    return best


def fmt_combo(title, combo):
    if not combo:
        return f"{title}\nPas assez de matchs aujourd'hui pour ce combiné."
    picks, odds, prob = combo
    lines = [title]
    for c in picks:
        why = f' ({c["why"]})' if c["why"] else ""
        lines.append(f'• {c["start"]} {c["event"]}\n   → {c["pick"]} @ {c["odds"]:.2f}{why}')
    lines.append(f"💰 Cote totale : {odds:.1f} | Proba estimée : {prob*100:.1f}%")
    return "\n".join(lines)


def build_messages(picks):
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    pool = one_per_event(picks)
    parts = [f"🔥 COMBINÉS DU {today} 🔥\n{len(pool)} matchs analysés, {len(picks)} paris étudiés"]
    if pool:
        top = max(pool, key=lambda c: c["score"])
        parts.append(f'⭐ PARI DU JOUR\n{top["start"]} {top["event"]} ({top["league"]})\n→ {top["pick"]} @ {top["odds"]:.2f} '
                     f'(proba {top["prob"]*100:.0f}%)' + (f'\nPourquoi : {top["why"]}' if top["why"] else ""))
    for title, size, min_odds in COMBOS:
        parts.append(fmt_combo(title, make_combo(pool, size, min_odds)))
    parts.append("Probabilités estimées, aucun gain garanti. Les grosses cotes passent très rarement : "
                 "mise petit. 18+")
    return parts


def send_telegram(text, token, chat_id):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=30)
    r.raise_for_status()


def main():
    sample = "--sample" in sys.argv
    if sample:
        with open(os.path.join(os.path.dirname(__file__), "tests", "sample_events.json")) as f:
            events = json.load(f)
        today, after = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date(), None
    else:
        now = datetime.now(timezone.utc)
        snapshot_only = bool(os.environ.get("SNAPSHOT_ONLY"))
        today = now.date() + timedelta(days=1) if snapshot_only else now.date()
        after = None if snapshot_only else now
        events = fetch_events(today)
    picks = build_picks(events, today, after, history=None if sample else load_history())
    if not sample:
        save_history(picks)
        if snapshot_only:
            print(f"{len(picks)} cotes relevées pour demain.")
            return
    messages = build_messages(picks)
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat and not sample:
        for m in messages:
            send_telegram(m, token, chat)
    else:
        print("\n\n".join(messages))


if __name__ == "__main__":
    main()
