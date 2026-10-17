"""Tests hors ligne des corrections de lignes (strength._lines_near) et de la confiance des pronostics du modèle approfondi
(fair.py). Lancer : python -m tests.test_fix_lines"""
import math
import os
import shutil
import tempfile
from datetime import date, datetime, timezone

TMP = tempfile.mkdtemp(prefix="kelly-fixlines-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")

from kelly import config as C, fair, prematch, strength as S  # noqa: E402
from kelly.sources import features, footballdata as fd, state  # noqa: E402

from tests import synth  # noqa: E402

EV = {"id": "m1", "sport_key": "soccer_epl", "home_team": "Arsenal", "away_team": "Manchester City",
      "commence_time": "2025-12-01T19:00:00Z"}
STAT_OF = {prematch.CORNERS: "corners", prematch.CARDS: "cards"}


def synth_matches(seasons=("2324", "2425", "2526"), until=date(2025, 11, 29)):
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s, until=until if s == seasons[-1] else None), "E0", s)
        assert not probs, probs
        out += rows
    return out


def with_clock(day, fn):
    old = state.clock
    state.clock = lambda: datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc).timestamp()
    try:
        return fn()
    finally:
        state.clock = old


def team_total_options(exp, market="team_shots_total"):
    return [o for o in fair.options({"n_home": 8, "n_away": 8, "exp_shots": float(exp), "exp_sot": 9.0}, "A", "B") if o[0] == market]


def test_lines_cover_both_sides():
    """Bug 1 : à forte moyenne / dispersion, les lignes au-dessus de la moyenne ne doivent pas être coupées."""
    for mean, alpha in ((25.0, 0.03), (42.0, 0.03), (13.0, 0.05), (30.0, 0.05), (8.5, 0.05), (2.0, 0.05)):
        pts = S._lines_near(mean, alpha, 1)
        assert 3 <= len(pts) <= 9 and pts == sorted(set(pts)), (mean, pts)
        assert all(x % 1 == 0.5 and x >= 0.5 for x in pts), pts
        assert min(pts) < mean < max(pts), (mean, pts)                          # la moyenne est dans la fenêtre
        c = math.floor(mean) + 0.5
        assert abs((c - min(pts)) - (max(pts) - c)) <= 1 or min(pts) == 0.5, (mean, pts)    # fenêtre centrée
    assert max(S._lines_near(25.0, 0.03, 1)) >= 27.5 and max(S._lines_near(42.0, 0.03, 1)) >= 44.5
    assert S._lines_near(2.5, 0.0, 1) == [0.5, 1.5, 2.5, 3.5, 4.5], S._lines_near(2.5, 0.0, 1)   # petite dispersion : inchangé
    print("lignes des deux côtés de la moyenne : OK")


def test_no_negative_line():
    """Bug 2 : jamais de ligne sous 0,5 (« Over -0.5 » à 100 %)."""
    for mean in (0.05, 0.3, 1.0, 1.3, 2.2):
        for lo in (0, 1):
            pts = S._lines_near(mean, 0.05, lo)
            assert pts and min(pts) >= 0.5, (mean, lo, pts)
    assert S._lines_near(1.3, 0.05, 0)[0] == 0.5
    pred = {"lh": 1.4, "la": 1.1, "rho": 0.0, "q": 0.8, "qs": {"shots": 0.8}, "stats": {"shots": {"h": 1.3, "a": 0.9, "al": 0.05, "at": 0.04}},
            "base": {"lh": 1.4, "la": 1.1, "stats": {"shots": {"h": 1.3, "a": 0.9}}}}
    g = S.grid(pred)
    for part in ("total", "home", "away"):
        assert all(float(k) >= 0.5 for k in g["shots"][part]), g["shots"][part]
    assert all(l["point"] is None or l["point"] >= 0.5 for l in S.lines(pred, "A", "B"))
    print("pas de ligne négative : OK")


def test_under_is_offered_on_high_counts():
    """Bug 1 (fair) : Under proposé sur tirs / tirs cadrés à forte moyenne, et best_per_family peut le choisir."""
    shots = team_total_options(25.0)
    assert {o[1] for o in shots} == {"Over", "Under"}
    assert any(o[1] == "Under" and o[2] > 25 for o in shots), sorted((o[1], o[2]) for o in shots)
    assert any(o[1] == "Over" and o[2] < 25 for o in shots)
    p_under = [round(fair.shrink(o[3]), 3) for o in shots if o[1] == "Under" and o[2] > 25]
    assert any(C.FAIR_P_RANGE[0] <= p <= C.FAIR_P_RANGE[1] for p in p_under), p_under
    sides = set()                                   # sur toute une plage de moyennes : tantôt Over, tantôt Under
    for exp in range(14, 45):
        got = fair.best_per_family(team_total_options(exp), {"team_shots_total"})
        assert len(got) == 1, (exp, got)
        sides.add(got[0][1])
        assert abs(got[0][3] - C.FAIR_P_TARGET) < 0.08, (exp, got)
    assert sides == {"Over", "Under"}, sides
    opts = [("team_shots_total", "Over", 24.5, 0.80, "x"), ("team_shots_total", "Under", 27.5, 0.67, "x")]
    assert fair.best_per_family(opts, {"team_shots_total"})[0][1] == "Under"
    assert max(len(team_total_options(e)) for e in range(14, 45)) <= 18             # nombre de lignes raisonnable (Telegram)
    print("Under proposé sur les fortes moyennes : OK")


def deep_features():
    ms = synth_matches()
    day = date(2025, 12, 1)
    old = S.load_matches
    S.load_matches = lambda div: ms if div == "E0" else []
    try:
        S.reload()
        features.reload()
        f = with_clock(day, lambda: features.match_features("Arsenal", "Manchester City", "soccer_epl", referee="M Oliver"))
    finally:
        S.load_matches = old
    return f, day


def test_per_stat_confidence():
    """Bug 3 : conf / score / garde de qualité par statistique, pas la qualité du modèle de buts."""
    f, day = deep_features()
    d = f["deep"]
    assert set(d["qs"]) >= {"goals", "corners", "cards", "shots", "sot"} and d["q"] == d["qs"]["goals"]
    for m in ("totals", "btts", "double_chance"):
        assert fair.stat_q(f, m) == d["q"], m
    assert fair.stat_q(f, prematch.CORNERS) == d["qs"]["corners"] and fair.stat_q(f, prematch.CARDS) == d["qs"]["cards"]
    assert fair.stat_q(f, "team_shots_home") == d["qs"]["shots"] and fair.stat_q(f, "team_sot_total") == d["qs"]["sot"]
    qs = dict(d["qs"], goals=0.9, corners=0.42, cards=0.44, shots=0.46, sot=0.48)    # buts excellents, autres statistiques moyennes
    f2 = dict(f, deep=dict(d, q=0.9, qs=qs))
    opts, bases = fair._options(f2, "Arsenal", "Manchester City")
    assert {o[0] for o in opts} >= {prematch.CORNERS, prematch.CARDS, "team_shots_total", "team_sot_total"}
    seen = set()
    for fam in (prematch.CORNERS, prematch.CARDS, "team_shots_total", "team_sot_total", "team_shots_home", "team_sot_away"):
        got = fair.best_per_family(opts, {fam}, bases)
        if not got:
            continue
        c = fair._with_base(fair.make_pick(EV, *got[0], "football-data"), bases[got[0][:3]], fair.stat_q(f2, fam))
        assert c["conf"] == round(qs[STAT_OF.get(fam, fam.split("_")[1])], 2) < 0.9, (fam, c["conf"])
        seen.add(fam)
    assert len(seen) >= 3, seen
    # via make() : chaque pronostic porte la qualité de SA statistique
    old, orig = S.load_matches, fair.odds.match_numbers
    S.load_matches = lambda d_: []
    fair.odds.match_numbers = lambda h, a, k, **kw: f2
    try:
        pronos = fair.make({"leagues": {"soccer_epl": [EV]}}, [], day)
    finally:
        S.load_matches, fair.odds.match_numbers = old, orig
    assert pronos
    for c in pronos:
        assert c["conf"] == round(fair.stat_q(f2, c["market"]), 2), (c["market"], c["conf"])
        assert (c["conf"] == 0.9) == (c["market"] in fair.FREE_MARKETS), c           # tous les marchés du modèle de buts (+ 1N2 et buts d'une équipe, que la fin du rapprochement de 0,9 laisse maintenant sortir)
    # garde de qualité par statistique pour corners / cartons (comme tirs / tirs cadrés)
    fl = dict(f, deep=dict(d, qs=dict(d["qs"], corners=0.1, cards=0.2, shots=0.1)))
    mk = {o[0] for o in fair.deep_options(fl, "Arsenal", "Manchester City")}
    assert prematch.CORNERS not in mk and prematch.CARDS not in mk and not {m for m in mk if m.startswith("team_shots")}, mk
    assert "totals" in mk and "team_sot_total" in mk
    opts, bases = fair._options(fl, "Arsenal", "Manchester City")
    assert not fair.best_per_family(opts, {prematch.CORNERS, prematch.CARDS}, bases)
    print("confiance par statistique : OK")


def main():
    test_lines_cover_both_sides()
    test_no_negative_line()
    test_under_is_offered_on_high_counts()
    test_per_stat_confidence()
    shutil.rmtree(TMP, ignore_errors=True)
    print("tests lignes / confiance : OK")


if __name__ == "__main__":
    main()
