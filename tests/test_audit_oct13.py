"""Audit du 13/10 (hors ligne) : chances d'un combiné selon 1xBet (sans sa marge, ou cote 1xBet moins une marge supposée), prudence de 5 % par jambe
de Kelly et de 1 % par pari réel aux probas de Pinnacle, paris de stats jamais proposés en coupe (moyennes de championnats différents), équipe
reléguée jamais jugée comme une promue. Lancer : python -m tests.test_audit_oct13"""
import os
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-oct13-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import combos as CB, config as C, strength  # noqa: E402


def leg(i, **kw):
    return {"key": f"k{i}", "id": f"e{i}", "market": "totals", "name": "Over", "point": 2.5, "sport_key": "soccer_epl", "prob": 0.70, "odds": 1.8,
            "xodds": 1.8, "conf": 0.9, "n_min": 10} | kw


def test_market_prob_and_prudence():
    assert CB.leg_market_p(leg(1, p_x=0.52)) == 0.52 and CB.leg_market_p(leg(1, xp=0.5)) == 0.5
    assert abs(CB.leg_market_p(leg(1)) - 1 / (1.8 * (1 + C.X_MARGIN_GUESS))) < 1e-12           # cote 1xBet seule : sa marge supposée retirée
    assert CB.leg_market_p(leg(1, xodds=None)) is None and CB.market_prob([leg(1, p_x=0.5), leg(2, xodds=None)]) is None
    assert abs(CB.market_prob([leg(1, p_x=0.5), leg(2, p_x=0.4)]) - 0.2) < 1e-12
    kelly = [leg(1), leg(2)]
    real = [leg(1, prob_book=0.70), leg(2, prob_book=0.70)]
    assert abs(CB.combo_prob(kelly) - (0.70 * 0.95) ** 2) < 1e-12 and abs(CB.combo_prob(real) - (0.70 * 0.99) ** 2) < 1e-12
    print("ok  chances selon 1xBet (sans marge, sinon marge supposée) ; prudence 5 % par jambe de Kelly, 1 % par pari réel aux probas de Pinnacle")


def test_no_stats_in_cups():
    ldc = {"sport_key": "soccer_uefa_champs_league"}
    for m in ("alternate_totals_corners", "alternate_totals_cards", "team_shots_total", "team_sot_home", "team_fouls_away", "team_yellow_total",
              "player_shots"):
        assert CB.cross_league_stat(leg(1, market=m) | ldc), m
        assert not CB.data_ok(leg(1, market=m) | ldc), m
    for sk in ("soccer_uefa_europa_league", "soccer_conmebol_copa_libertadores", "soccer_fa_cup", "soccer_international_friendlies"):
        assert CB.cross_league_stat(leg(1, market="alternate_totals_corners", sport_key=sk)), sk
    assert not CB.cross_league_stat(leg(1) | ldc) and CB.data_ok(leg(1) | ldc)                  # les buts en coupe : oui (le marché pèse 88 %)
    assert not CB.cross_league_stat(leg(1, market="alternate_totals_corners")) and CB.data_ok(leg(1, market="alternate_totals_corners"))
    real = [leg(1, market="alternate_totals_corners", edge=0.08, weight=1.0, n_books=10, pm=None) | ldc,
            leg(2, edge=0.08, weight=1.0, n_books=10, pm=None) | ldc]
    assert [c["key"] for c in CB.quality_legs(real)] == ["k2"]                                   # paris réels aussi : pas de corners en Ligue des champions
    print("ok  coupes d'Europe, d'Amérique, coupes nationales, matchs internationaux : jamais de paris de stats ; les buts restent")


def _season(teams, start, n_rounds, step=7):
    out, d = [], start
    for r in range(n_rounds):
        for i in range(0, len(teams) - 1, 2):
            h, a = (teams[i], teams[i + 1]) if r % 2 == 0 else (teams[i + 1], teams[i])
            out.append({"date": d.isoformat(), "home": h, "away": a, "hg": (r + i) % 3, "ag": (r * 2 + i) % 2})
        teams = teams[:1] + teams[-1:] + teams[1:-1]                    # rotation : tout le monde se rencontre
        d += timedelta(days=step)
    return out


def test_relegated_not_promoted():
    base = [f"T{i}" for i in range(8)]
    s1 = _season(base + ["Old1", "Old2"], date(2025, 8, 2), 36)       # E1 2025-26
    s2 = _season(base + ["Down", "Up"], date(2026, 8, 1), 10)          # E1 2026-27 : « Down » descend de E0, « Up » monte de E2
    upper = _season(["Down", "Top1", "Top2", "Top3"], date(2025, 8, 2), 30)
    on = date(2026, 10, 13)
    first = {"Down": "2026-08-01", "Up": "2026-08-01", "T0": "2025-08-02"}
    assert strength.relegated(first, on.isoformat(), upper) == {"Down"}
    assert strength.relegated(first, "2025-01-01", upper) == set(), "rien du futur : les matchs d'en haut après la date ne comptent pas"
    without = strength.build(s1 + s2, on, stats=("goals",), div="E1")
    with_up = strength.build(s1 + s2, on, stats=("goals",), div="E1", upper=upper)
    assert sorted(without["stats"]["goals"]["promo"]) == ["Down", "Up"], without["stats"]["goals"]["promo"]      # avant : la reléguée prise pour une promue
    assert with_up["stats"]["goals"]["promo"] == ["Up"], with_up["stats"]["goals"]["promo"]
    assert strength.UPPER["E1"] == "E0" and strength.upper_matches("E0") is None
    print("ok  équipe reléguée (vue dans la division du dessus avant) : a priori neutre, plus celui des promus ; la promue le garde")


def main():
    test_market_prob_and_prudence()
    test_no_stats_in_cups()
    test_relegated_not_promoted()
    print("tests audit du 13/10 : OK")


if __name__ == "__main__":
    main()
