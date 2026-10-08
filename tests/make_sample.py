"""Régénère tests/sample_events.json : 12 matchs fictifs d'une même journée, 14 bookmakers chacun, avec une dispersion de
prix réaliste (faible sur le 1N2, forte sur corners/cartons/joueurs) et quelques valeurs cachées. Reproductible.
Lancer : python -m tests.make_sample"""
import json
import math
import os
import random

TEAMS = [("PSG", "Lyon"), ("Real Madrid", "Getafe"), ("Arsenal", "Everton"), ("Inter", "Torino"), ("Bayern", "Mainz"),
         ("Roma", "Lazio"), ("Porto", "Braga"), ("Milan", "Genoa"), ("Ajax", "PSV"), ("Benfica", "Sporting"),
         ("Lille", "Nice"), ("Betis", "Sevilla")]
BOOKS = ["Pinnacle", "Betfair", "Unibet", "Coolbet", "Matchbook", "Bet365", "William Hill", "Betway", "888sport",
         "Marathonbet", "Betclic", "NordicBet", "Winamax", "Codere"]
PLAYERS = ["Mbappé", "Kane", "Lautaro", "Haaland", "Vinícius", "Osimhen", "Gyökeres", "Salah", "Isak", "Lewandowski",
           "Ferran", "Gimenez"]


def book_prices(rng, true, sd, margin):
    """Cotes d'un book pour des issues de probas vraies `true` (somme 1) : bruit propre au book, marge `margin`."""
    q = [t * math.exp(rng.gauss(0, sd)) for t in true]
    s = sum(q)
    return [round(max(1.02, 1 / (x / s * margin)), 2) for x in q]


def two(rng, p_over, sd, margin, point, desc=None):
    out = []
    for b in range(len(BOOKS)):
        po, pu = book_prices(rng, [p_over, 1 - p_over], sd, margin)
        out.append(([("Over", po), ("Under", pu)], point, desc))
    return out


def main():
    rng = random.Random(2026)
    events = []
    for i, (h, a) in enumerate(TEAMS):
        strength = rng.uniform(-0.6, 1.4) if i != 1 else 1.6            # Real Madrid - Getafe : grosse affiche
        ph = min(0.8, max(0.28, 0.45 + 0.17 * strength))
        pd = 0.27 - 0.04 * abs(strength)
        h2h = [ph, pd, 1 - ph - pd]
        over = rng.uniform(0.42, 0.68)
        btts = rng.uniform(0.42, 0.62)
        corners = rng.uniform(0.42, 0.62)
        cards = rng.uniform(0.40, 0.65)
        shots = rng.uniform(0.55, 0.78)
        fouls = rng.uniform(0.45, 0.6)
        n_books = [14, 14, 13, 12, 14, 12, 10, 12, 11, 12, 10, 10][i]
        books = []
        for b, title in enumerate(BOOKS[:n_books]):
            ms = []
            hp = book_prices(rng, h2h, 0.045, 1.052)
            ms.append({"key": "h2h", "outcomes": [{"name": h, "price": hp[0]}, {"name": "Draw", "price": hp[1]},
                                                 {"name": a, "price": hp[2]}]})
            po, pu = book_prices(rng, [over, 1 - over], 0.04, 1.05)
            ms.append({"key": "totals", "outcomes": [{"name": "Over", "price": po, "point": 2.5},
                                                     {"name": "Under", "price": pu, "point": 2.5}]})
            py, pn = book_prices(rng, [btts, 1 - btts], 0.05, 1.06)
            ms.append({"key": "btts", "outcomes": [{"name": "Yes", "price": py}, {"name": "No", "price": pn}]})
            if b < 9:                                                  # les marchés détaillés : moins de books, plus de bruit
                for mk, p, pt, sd, desc in (("alternate_totals_corners", corners, 9.5, 0.09, None),
                                            ("alternate_totals_cards", cards, 3.5, 0.09, None),
                                            ("player_shots_on_target", shots, 1.5, 0.10, PLAYERS[i]),
                                            ("player_fouls", fouls, 1.5, 0.10, PLAYERS[(i + 5) % 12])):
                    po, pu = book_prices(rng, [p, 1 - p], sd, 1.06)
                    o1 = {"name": "Over", "price": po, "point": pt}
                    o2 = {"name": "Under", "price": pu, "point": pt}
                    if desc:
                        o1["description"] = o2["description"] = desc
                    ms.append({"key": mk, "outcomes": [o1, o2]})
            books.append({"key": f"b{b}", "title": title, "markets": ms})
        events.append({"id": str(i), "sport_key": "soccer_test_league", "sport_title": "Football",
                       "commence_time": f"2026-10-08T{14 + i // 2:02d}:{'00' if i % 2 == 0 else '30'}:00Z",
                       "home_team": h, "away_team": a, "bookmakers": books})
    # Valeurs cachées : un tiers des books paie 12 à 18 % de plus sur une issue (book en retard) -> la cote typique dépasse
    # le consensus. Une issue par match, tantôt un marché détaillé (mal pricé), tantôt le 1N2 ou les buts.
    kinds = ["alternate_totals_corners", "alternate_totals_cards", "player_shots_on_target", "totals", "h2h", "btts"]
    for i, ev in enumerate(events):
        mk = kinds[i % len(kinds)]
        side = rng.choice([0, 1]) if mk != "h2h" else rng.choice([0, 2])
        boost = rng.uniform(1.12, 1.18)
        slow = [b for j, b in enumerate(ev["bookmakers"]) if j % 3 == 1]
        for bk in slow:
            for m in bk["markets"]:
                if m["key"] == mk:
                    o = m["outcomes"][side]
                    o["price"] = round(o["price"] * boost, 2)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_events.json")
    with open(path, "w") as f:
        json.dump(events, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(events)} matchs écrits dans {path}")


if __name__ == "__main__":
    main()
