"""Corrections complémentaires après la revue : date du coup d'envoi pour API-Football, alertes sur la proba brute des books,
fautes au total réglées avec football-data, matchs africains en double, moyennes sur trop peu de matchs, un combiné par palier
et par jour. Lancer : python -m tests.test_fix_extra"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-extra-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-extra-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-extra-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, fair, settle, stats, store  # noqa: E402
from kelly.sources import footballdata as fd  # noqa: E402


def test_fixture_list_of_the_kickoff_date():
    """Un match de 00:30 UTC (soir aux Amériques) se cherche dans la liste API-Football de sa date, pas du jour d'analyse."""
    seen = []
    old = stats._day_fixtures
    stats._day_fixtures = lambda day: seen.append(day) or []
    try:
        stats.find_fixture("2026-10-08", "Fluminense", "Coritiba", "2026-10-09T00:30:00Z")
        stats.find_fixture("2026-10-08", "Santos", "Flamengo")
    finally:
        stats._day_fixtures = old
    assert seen == ["2026-10-09", "2026-10-08"], seen


def test_alerts_compare_raw_book_probs():
    """Cotes inchangées : aucune alerte, même si la proba annoncée (modèle) diffère de celle des books ; l'ancien historique (« p »
    seul, mêlé au modèle) ne déclenche rien."""
    c = {"key": "k", "odds": 1.95, "prob": 0.48, "prob_book": 0.41}
    assert app.find_alerts([c], {"k": {"p": 0.41, "pb": 0.41, "o": 1.95}}) == []
    assert app.find_alerts([c], {"k": {"p": 0.30, "o": 1.95}}) == []
    assert app.find_alerts([c | {"prob_book": 0.47}], {"k": {"p": 0.41, "pb": 0.41, "o": 1.95}})        # vraie hausse : alerte


def test_fouls_total_settled_with_football_data():
    fstats = {"fouls": 24, "side": {"home": {"shots": 10, "sot": 4}, "away": {"shots": 8, "sot": 3}}}
    assert settle.shot_value("team_fouls_total", fstats) == 24
    nul = {"fouls": 12, "side": {"home": {"shots": 10, "nul": ["fouls"]}, "away": {"fouls": 12}}}
    assert settle.shot_value("team_fouls_total", nul) is None                     # null d'API-Football : inconnu, pas 0


def test_african_match_not_analysed_twice():
    a = {"home_team": "Ivory Coast", "away_team": "Kenya", "commence_time": "2026-10-09T16:00:00Z"}
    assert fair.same_match(a, dict(a, commence_time="2026-10-09T17:00:00Z"))
    assert not fair.same_match(a, dict(a, home_team="Senegal")) and not fair.same_match(a, dict(a, commence_time="2026-10-10T16:00:00Z"))


def test_averages_need_enough_matches():
    recs = [{"throws": (20, 18) if i < 2 else (None, None)} for i in range(10)]
    assert "throws_for" not in fd._avg_view(recs, keys=("throws",), min_count=3)
    assert fd._avg_view(recs, keys=("throws",))["throws_for"] == 20                  # football-data : inchangé


def test_one_combo_per_tier_and_day():
    store.save("bets.json", {"picks": [], "combos": []})
    settle.record_combos([{"tier": "🛡️ SÛR", "keys": ["a", "b"], "odds": 5.0, "prob": 0.2, "stake": 1}], "2026-10-08")
    settle.record_combos([{"tier": "🛡️ SÛR", "keys": ["a", "c"], "odds": 5.5, "prob": 0.2, "stake": 1}], "2026-10-08")
    combos = store.load("bets.json", {})["combos"]
    assert len(combos) == 1 and combos[0]["keys"] == ["a", "b"]


def main():
    for t in (test_fixture_list_of_the_kickoff_date, test_alerts_compare_raw_book_probs, test_fouls_total_settled_with_football_data,
              test_african_match_not_analysed_twice, test_averages_need_enough_matches, test_one_combo_per_tier_and_day):
        t()
        print(f"ok  {t.__name__}")
    print("corrections complémentaires : OK")


if __name__ == "__main__":
    main()
