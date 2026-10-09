"""Corrections de kelly/odds.py : jour Kelly (matchs du soir des Amériques), steam sans faux signal, lignes asiatiques et
entières écartées, sans-marge refusé sur issues incomplètes. Hors ligne. Lancer : python -m tests.test_fix_odds"""
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-test-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import config as C, odds  # noqa: E402

DAY = date(2026, 10, 8)
UTC = timezone.utc
BOOKS = ("A", "B", "C", "D")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def event(eid="e1", start=None, sport="soccer_epl", markets=None, books=BOOKS):
    """Événement au format The Odds API ; `markets` : fonction (book) -> liste de marchés."""
    start = start or datetime(2026, 10, 8, 19, 0, tzinfo=UTC)
    return {"id": eid, "sport_key": sport, "home_team": "Alpha", "away_team": "Beta", "commence_time": iso(start),
            "bookmakers": [{"key": b.lower(), "title": b, "markets": markets(b)} for b in books]}


def mk(key, *outs):
    return {"key": key, "outcomes": [_out(*o) for o in outs if o is not None]}


def _out(name, price, point=None, description=None):
    o = {"name": name, "price": price}
    if point is not None:
        o["point"] = point
    if description is not None:
        o["description"] = description
    return o


def h2h(book):
    return mk("h2h", ("Alpha", 2.0), ("Draw", 3.5), ("Beta", 4.0))


def picks_of(events, **kw):
    return {(c["market"], c["who"], c["point"], c["name"]): c for c in odds.build_picks(events, kw.pop("day", DAY), **kw)}


# ---------- 1. jour Kelly ----------

def test_kelly_day():
    C.DAY_END_HOUR = 8                                    # les heures ci-dessous supposent une journée Kelly qui finit à 8 h
    after = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
    w = odds.day_window(DAY, after)
    assert w["commenceTimeFrom"] == "2026-10-08T08:00:00Z", w
    assert w["commenceTimeTo"] == "2026-10-09T07:59:59Z", w           # lendemain 08:00 UTC (exclu)
    assert odds.day_window(DAY)["commenceTimeFrom"] == "2026-10-08T00:00:00Z"
    ev = lambda hh, mm, d=9: event(f"e{d}{hh}{mm}", datetime(2026, 10, d, hh, mm, tzinfo=UTC), markets=lambda b: [h2h(b)])
    late = ev(0, 30)                                                 # Brasileirão 21:30 locale = 00:30 UTC le lendemain
    next_morning = ev(9, 0)
    same_day = event("today", datetime(2026, 10, 8, 19, 0, tzinfo=UTC), markets=lambda b: [h2h(b)])
    started = event("past", datetime(2026, 10, 8, 7, 0, tzinfo=UTC), markets=lambda b: [h2h(b)])
    got = {c["id"] for c in odds.build_picks([late, next_morning, same_day, started], DAY, after)}
    assert got == {late["id"], "today"}, got                          # 00:30 D+1 inclus, 09:00 D+1 et match déjà commencé exclus
    assert {c["id"] for c in odds.build_picks([next_morning], date(2026, 10, 9))} == {next_morning["id"]}
    # le relevé gratuit et les achats demandent la même fenêtre
    seen = []
    orig = odds.league_events
    odds.league_events = lambda key, window: seen.append(window) or []
    try:
        odds.scan_today(DAY, after, sports=[{"key": "soccer_brazil_campeonato"}])
    finally:
        odds.league_events = orig
    assert seen and seen[0]["commenceTimeTo"] == "2026-10-09T07:59:59Z", seen
    # les jours suivants commencent là où le jour Kelly s'arrête
    seen.clear()
    odds.league_events = lambda key, window: seen.append(window) or []
    try:
        odds.scan_ahead(DAY, ["soccer_epl"], days=3)
    finally:
        odds.league_events = orig
    assert seen[0]["commenceTimeFrom"] == "2026-10-09T08:00:00Z", seen
    print("jour Kelly : OK")


# ---------- 2. steam ----------

def test_snapshot_stores_raw_consensus():
    evs = [event(markets=lambda b: [h2h(b)])]
    first = odds.build_picks(evs, DAY)
    assert first and all(c["steam"] == 0 for c in first)
    for c in first:                                                   # stats.enrich / prematch.apply écrasent prob
        c["prob"] = min(0.99, c["prob_book"] + 0.12)
    snap = odds.snapshot(first)
    for c in first:
        assert abs(snap[c["key"]]["p"] - c["prob_book"]) < 1e-3, (snap[c["key"]], c["prob_book"])
        assert abs(snap[c["key"]]["pb"] - c["prob_book"]) < 1e-3
    again = odds.build_picks(evs, DAY, history=snap)                  # cotes inchangées -> aucun steam
    assert all(abs(c["steam"]) < 1e-3 and "pros" not in c["why"] for c in again), [(c["pick"], c["steam"]) for c in again]
    # anciens relevés (proba mêlée au modèle, sans « pb ») : ignorés
    old = {k: {"p": v["p"] * 0.7, "o": v["o"], "t": v["t"]} for k, v in snap.items()}
    assert all(c["steam"] == 0 for c in odds.build_picks(evs, DAY, history=old))
    # un vrai mouvement reste vu : la proba de consensus était plus basse au relevé précédent
    moved = {k: dict(v, pb=v["pb"] * 0.8) for k, v in snap.items()}
    assert all(c["steam"] > 0.2 for c in odds.build_picks(evs, DAY, history=moved))
    print("steam : OK")


# ---------- 3. lignes asiatiques et entières ----------

def test_only_half_lines():
    def totals(book):
        pts = (2.0, 2.25, 2.5, 2.75, 3.0, 3.5)
        return mk("totals", *[(n, 1.9, p) for p in pts for n in ("Over", "Under")])

    def spreads(book):
        return mk("spreads", ("Alpha", 1.9, -1.0), ("Beta", 1.9, 1.0), ("Alpha", 1.9, -1.5), ("Beta", 1.9, 1.5),
                  ("Alpha", 1.9, -0.25), ("Beta", 1.9, 0.25))

    def props(book):
        return mk("player_shots", ("Over", 1.9, 1.0, "Joe"), ("Under", 1.9, 1.0, "Joe"),
                  ("Over", 1.9, 1.5, "Joe"), ("Under", 1.9, 1.5, "Joe"))

    got = picks_of([event(markets=lambda b: [totals(b), spreads(b), props(b), h2h(b)])])
    pts = {(m, p) for m, _, p, _ in got if p is not None}
    assert pts == {("totals", 2.5), ("totals", 3.5), ("spreads", -1.5), ("spreads", 1.5), ("player_shots", 1.5)}, pts
    assert ("h2h", None, None, "Draw") in got                         # les marchés sans ligne restent
    print("lignes : OK")


# ---------- 4. issues incomplètes ----------

def test_incomplete_outcome_sets():
    def dc(book, full=True):
        outs = [("Alpha or Draw", 1.4), ("Alpha or Beta", 1.35), ("Draw or Beta", 2.2)]
        return mk("double_chance", *(outs if full else outs[1:]))

    def partial_h2h(book):                                            # le book B oublie le nul
        return mk("h2h", ("Alpha", 2.0), ("Beta", 4.0)) if book == "B" else h2h(book)

    def marks(book):
        return [partial_h2h(book), dc(book, full=book != "C"), mk("btts", ("Yes", 1.8), ("No", 2.0)),
                mk("totals", ("Over", 1.9, 2.5), ("Under", 1.9, 2.5) if book != "D" else None)]
    # book D n'affiche que l'Over : mk() écarte le None
    got = picks_of([event(markets=marks)], day=DAY)
    n_books = lambda k: got[k]["n_books"]
    assert n_books(("h2h", None, None, "Alpha")) == 3, n_books(("h2h", None, None, "Alpha"))     # B écarté
    assert n_books(("double_chance", None, None, "Alpha or Draw")) == 3     # C incomplet écarté
    assert ("totals", None, 2.5, "Over") in got and n_books(("totals", None, 2.5, "Over")) == 3  # D sans Under écarté
    # sans l'écart : le 1X2 à 2 issues d'un book ne produit aucune proba > 1 pour la double chance
    only_two = event(markets=lambda b: [mk("double_chance", ("Alpha or Draw", 1.4), ("Alpha or Beta", 1.35))])
    assert odds.build_picks([only_two], DAY) == []
    # valeur fantôme : 2 books sur 3 avec 1X2 sans Draw ne passent plus MIN_BOOKMAKERS
    ghost = event(markets=lambda b: [mk("h2h", ("Alpha", 2.0), ("Beta", 4.0)) if b in "AB" else h2h(b)])
    ps = picks_of([ghost])
    assert ("h2h", None, None, "Alpha") not in ps and ("h2h", None, None, "Beta") not in ps, list(ps)   # 2 books complets < MIN_BOOKMAKERS
    for c in ps.values():
        assert c["prob_book"] <= 1.0
    # tennis / basket : h2h à 2 issues reste accepté
    ten = event(sport="tennis_atp_x", markets=lambda b: [mk("h2h", ("Alpha", 1.6), ("Beta", 2.4))])
    assert ("h2h", None, None, "Alpha") in picks_of([ten])
    print("issues incomplètes : OK")


def main():
    test_kelly_day()
    test_snapshot_stores_raw_consensus()
    test_only_half_lines()
    test_incomplete_outcome_sets()
    for k in ("DATA_DIR", "SOURCES_CACHE_DIR", "ODDS_CACHE_DIR"):
        shutil.rmtree(os.environ[k], ignore_errors=True)
    print("OK : correctifs de odds.py.")


if __name__ == "__main__":
    main()
