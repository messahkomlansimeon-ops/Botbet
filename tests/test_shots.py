"""Tirs vers le but et tirs cadrés : gardés pour chaque match même quand corners ou cartons scorent mieux, combinés « spécial tirs »
à vérifier chez 1xBet, libellés de 1xBet, boutons et suivi. Lancer : python -m tests.test_shots"""
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-shots-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-shots-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-shots-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, combos as CB, config as C, fair, fmt, odds, played, store  # noqa: E402

DAY = "2026-10-10"


def test_shots_kept_per_match():
    """Corners et cartons scorent mieux, mais le meilleur pronostic tirs et le meilleur tirs cadrés du match restent."""
    ev = {"id": "m1", "sport_key": "soccer_epl", "home_team": "Arsenal", "away_team": "Chelsea", "commence_time": f"{DAY}T15:00:00Z"}
    opts = [("alternate_totals_corners", "Over", 9.5, 0.80, "c"), ("alternate_totals_cards", "Under", 4.5, 0.80, "k"),
            ("totals", "Under", 3.5, 0.80, "g"), ("team_shots_total", "Over", 22.5, 0.70, "s"), ("team_sot_total", "Over", 7.5, 0.70, "t")]
    old_opts, old_num = fair._options, odds.match_numbers
    fair._options = lambda f, h, a: (opts, {})
    odds.match_numbers = lambda h, a, k: {"n_home": 8, "n_away": 8}
    try:
        pronos = fair.make({"leagues": {"soccer_epl": [ev]}}, day=date.fromisoformat(DAY))
        every = fair.make({"leagues": {"soccer_epl": [ev]}}, day=date.fromisoformat(DAY), all_families=True)
    finally:
        fair._options, odds.match_numbers = old_opts, old_num
    markets = [c["market"] for c in pronos]
    assert "team_shots_total" in markets and "team_sot_total" in markets and len(markets) == 4, markets
    assert len(every) == 5 and all(c.get("alts") for c in every), "relevé 1xBet : chaque marché du match est présenté à 1xBet"
    assert C.MARKET_WEIGHT["team_shots_total"] == C.MARKET_WEIGHT["alternate_totals_corners"]
    print("ok  tirs gardés pour chaque match")


def prono(i, market, p=0.66):
    return {"key": f"m{i}|{market}||7.5|Over", "id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}",
            "commence": f"{DAY}T{12 + i}:00:00Z", "start": f"{12 + i}:00", "league": "L", "market": market, "name": "Over",
            "point": 7.5, "prob": p, "p0": p, "min_odds": round(1 / p * 1.05, 2), "conf": 0.6, "score": 0.1,
            "pick": fmt.label(market, "Over", 7.5, None, "soccer")}


def test_shots_combos_and_buttons():
    pronos = [prono(i, "team_sot_total") for i in range(5)] + [prono(10 + i, "team_shots_total") for i in range(5)]
    plan = dict(CB.fair_shots_plan(pronos))
    assert plan["tc5"] and all(c["market"].startswith("team_sot_") for c in plan["tc5"][0])
    assert plan["tv5"] and all(c["market"].startswith("team_shots_") for c in plan["tv5"][0])
    assert 5.0 <= plan["tc5"][1] <= 7.0 and len({c["id"] for c in plan["tc5"][0]}) == len(plan["tc5"][0])
    store.save("bets.json", {"picks": [], "combos": []})
    msgs, _, _ = app.build_day([], date.fromisoformat(DAY), pronos=pronos, fair_combos=list(plan.items()))
    titles = [m.split("\n")[0] for m in msgs]
    assert "<b>🎯 COMBINÉ TIRS CADRÉS — À VÉRIFIER CHEZ 1XBET</b>" in titles, titles
    assert "<b>🎯 COMBINÉ TIRS VERS LE BUT — À VÉRIFIER CHEZ 1XBET</b>" in titles, titles
    fair.record_combos(list(plan.items()), DAY)
    t = played.find("vtc5", DAY)
    assert not isinstance(t, str) and t is not None and "Tirs cadrés" in played.todo(DAY), t
    assert "Tirs vers le but" in played.todo(DAY) and played.find("vxx", DAY) is None
    print("ok  combinés spécial tirs, titres et boutons")


def test_shots_tracked_and_labels():
    store.save("fair.json", [])
    pronos = [prono(i, "alternate_totals_corners") for i in range(C.FAIR_TRACK)] + [prono(50 + i, "team_shots_total") for i in range(30)]
    fair.record(pronos, DAY)
    rows = store.load("fair.json", [])
    assert sum(r["market"] == "team_shots_total" for r in rows) == C.FAIR_TRACK_SHOTS, len(rows)
    assert fmt.label("team_shots_total", "Over", 25.5, None, "soccer") == "Plus de 25,5 tirs vers le but au total"
    assert fmt.label("team_sot_home", "Under", 4.5, "Arsenal", "soccer") == "Arsenal : moins de 4,5 tirs cadrés"
    print("ok  suivi des pronostics tirs et libellés 1xBet")


def main():
    test_shots_kept_per_match()
    test_shots_combos_and_buttons()
    test_shots_tracked_and_labels()
    print("tests tirs : OK")


if __name__ == "__main__":
    main()
