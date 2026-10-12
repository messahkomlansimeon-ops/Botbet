"""Apprendre plus vite sur les stats (hors ligne) : fair.record suit en silence jusqu'à C.FAIR_TRACK_STATS pronostics de corners, cartons, tirs, fautes…
sans requête de plus — tous les marchés de stats d'un match déjà relevé, puis ceux des championnats aux CSV football-data complets, réglés avec ces seuls
CSV (« free ») ; un pronostic par match d'abord ; un pronostic montré plus tard se règle normalement. Lancer : python -m tests.test_track_stats"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-trackstats-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import config as C, fair, settle, stats, store  # noqa: E402
from kelly.sources import footballdata as fd  # noqa: E402

DAY = "2026-10-10"


def p(mid, market, sk="soccer_epl", score=0.1, name="Over"):
    return {"key": f"{mid}|{market}||8.5|{name}", "id": mid, "sport_key": sk, "home": f"H{mid}", "away": f"A{mid}", "commence": f"{DAY}T15:00:00Z",
            "market": market, "name": name, "point": 8.5, "prob": 0.66, "p0": 0.66, "score": score}


def rows_by_key():
    return {r["key"]: r for r in fair.load()}


def test_selection():
    store.save(fair.FILE, [])
    assert "soccer_epl" in fd.STATS_SPORTS and "soccer_brazil_campeonato" not in fd.STATS_SPORTS     # CSV « new » : sans corners ni tirs
    shown = [p("br1", "alternate_totals_corners", "soccer_brazil_campeonato", 0.9)]
    pronos = shown + [
        p("br1", "team_fouls_total", "soccer_brazil_campeonato"), p("br1", "team_throws_total", "soccer_brazil_campeonato"),   # même match : relevé de toute façon
        p("br2", "alternate_totals_cards", "soccer_brazil_campeonato"), p("br2", "team_throws_home", "soccer_brazil_campeonato"),   # lu par 365scores
        p("xx1", "alternate_totals_cards", "soccer_xx_unknown"),               # ni CSV ni 365scores : il coûterait une requête
        p("en1", "alternate_totals_cards"), p("en1", "team_fouls_away"),
        p("en4", "team_sot_home"), p("en4", "alternate_totals_corners"),        # tirs cadrés suivis par C.FAIR_TRACK_SHOTS (API-Football) : match déjà relevé
        p("en1", "team_offsides_total"),                                         # hors-jeu : absents des CSV
        p("en2", "player_shots_on_target"),                                      # joueurs : jamais
        p("en3", "totals"),                                                      # buts : déjà suivis par FAIR_TRACK_FREE
    ]
    fair.record(pronos, DAY, shown=shown)
    got = rows_by_key()
    k = lambda mid, m: f"{mid}|{m}||8.5|Over"                                     # noqa: E731
    assert k("br1", "team_fouls_total") in got and k("br1", "team_throws_total") in got
    assert not got[k("br1", "team_fouls_total")].get("free") and got[k("br1", "team_fouls_total")]["x"] == 1
    assert got[k("br2", "alternate_totals_cards")].get("free") == 1 and got[k("br2", "team_throws_home")].get("free") == 1     # 365scores : touches comprises
    assert k("xx1", "alternate_totals_cards") not in got
    for m in ("alternate_totals_cards", "team_fouls_away"):
        assert got[k("en1", m)].get("free") == 1 and got[k("en1", m)]["x"] == 1, got[k("en1", m)]
    assert not got[k("en4", "team_sot_home")].get("free") and not got[k("en4", "alternate_totals_corners")].get("free")      # relevé de toute façon
    assert k("en1", "team_offsides_total") not in got and k("en2", "player_shots_on_target") not in got
    # montré plus tard : il sort du silence et se règle normalement (API-Football d'abord)
    fair.record([p("en1", "alternate_totals_cards")], DAY, shown=[p("en1", "alternate_totals_cards")])
    r = rows_by_key()[k("en1", "alternate_totals_cards")]
    assert "free" not in r and "x" not in r, r
    print("ok  suivi en silence : match déjà relevé, ou CSV football-data ; jamais hors-jeu, joueurs, ni un match qui coûterait une requête")


def test_cap_and_spread():
    store.save(fair.FILE, [])
    markets = ("alternate_totals_corners", "alternate_totals_cards", "team_fouls_home", "team_fouls_total")     # hors des plafonds tirs / corners d'une équipe
    pronos = [p(f"m{i}", m, score=0.5 - j * 0.01) for i in range(40) for j, m in enumerate(markets)]
    old = C.FAIR_TRACK_STATS
    C.FAIR_TRACK_STATS = 60
    try:
        fair.record(pronos, DAY, shown=[])
    finally:
        C.FAIR_TRACK_STATS = old
    got = [r for r in fair.load() if r.get("free")]
    assert len(got) == 60, len(got)
    per = {}
    for r in got:
        per[r["id"]] = per.get(r["id"], 0) + 1
    assert len(per) == 40 and max(per.values()) == 2, per                       # les 40 matchs d'abord, puis un 2e pronostic pour 20 d'entre eux
    print("ok  plafond C.FAIR_TRACK_STATS, un pronostic par match d'abord")


class FD:
    def __init__(self, m):
        self.m = m

    def match(self, b):
        return self.m

    stats = settle.FootballData.stats                                            # la vraie lecture des CSV, sur un match fixé


def test_free_settlement():
    def boom(*a, **k):
        raise AssertionError("requête interdite pour un suivi « free »")
    real = stats.find_fixture, stats.fixture_stats, settle.fmmatch.match_stats, settle.s365.match_stats
    stats.find_fixture = stats.fixture_stats = settle.fmmatch.match_stats = settle.s365.match_stats = boom
    try:
        m = {"hg": 1, "ag": 0, "hc": 6, "ac": 4, "hy": 2, "ay": 3, "hr": 0, "ar": 1, "hs": 12, "as": 9, "hst": 5, "ast": 3, "hf": 11, "af": 14}
        fdata = FD(m)
        row = p("en1", "team_fouls_away") | {"group": "soccer", "free": 1}
        fst = settle.match_stats(row, fdata)
        assert fst["side"]["away"]["fouls"] == 14 and settle.shot_value("team_fouls_away", fst) == 14, fst     # fautes d'une équipe : depuis les CSV
        assert settle.match_stats(p("en1", "alternate_totals_cards") | {"group": "soccer", "free": 1}, fdata)["cards"] == 6
        assert settle.match_stats(p("en1", "totals") | {"group": "soccer", "free": 1}, fdata) is None
        assert settle.match_stats(row, FD(None)) is None                          # CSV pas encore à jour : on attendra (WAIT_DAYS_STATS)
        # la mémoire de 365scores (matchs lus par le passage horaire) passe d'abord, sans requête ; il lui manque la stat : les CSV
        calls = []
        old = settle.s365._match_stats
        settle.s365._match_stats = lambda b, net=True: calls.append(net) or {"fouls": 21, "side": {"home": {"fouls": 9}, "away": {"fouls": 12}}}
        try:
            assert settle.match_stats(row, fdata)["side"]["away"]["fouls"] == 12 and calls == [False], calls
            assert settle.match_stats(p("en1", "alternate_totals_cards") | {"group": "soccer", "free": 1}, fdata)["cards"] == 6      # pas de cartons : CSV
        finally:
            settle.s365._match_stats = old
    finally:
        stats.find_fixture, stats.fixture_stats, settle.fmmatch.match_stats, settle.s365.match_stats = real
    print("ok  règlement « free » : CSV football-data seuls, fautes par équipe comprises, aucune requête")


def test_cups_settlement_only():
    from datetime import timedelta
    from kelly.sources import s365
    d = {"teams": {}, "seen": {}, "pending": {}, "lists": {}, "recent": {}}
    m = {"id": 9001, "d": DAY, "hid": 1, "hn": "Paris SG", "aid": 2, "an": "Arsenal", "s": {"corners": [6, 4], "goals": [1, 0]}}
    assert s365.record(d, m, "soccer_uefa_champs_league") and d["recent"][9001]["sk"] == "soccer_uefa_champs_league"
    assert d["teams"] == {}, d["teams"]                                       # coupe : jamais les profils d'équipes (ni le rangement par ligue)
    assert s365.record(d, m | {"id": 9002}, "soccer_brazil_campeonato") and set(d["teams"]) == {1, 2}      # championnat : profils comme avant
    assert {"soccer_uefa_champs_league", "soccer_conmebol_copa_libertadores", "soccer_fa_cup"} <= set(s365.scope())
    today = s365._today()
    old, new = str(today - timedelta(days=C.S365_FRESH_DAYS + 3)), str(today - timedelta(days=1))
    d = {"teams": {}, "seen": {}, "pending": {
        "c_old": {"k": "soccer_uefa_champs_league", "d": old, "h": 1, "a": 2},
        "c_new": {"k": "soccer_uefa_champs_league", "d": new, "h": 1, "a": 2},
        "l_new": {"k": "soccer_brazil_campeonato", "d": new, "h": 3, "a": 4}}, "lists": {}, "recent": {}}
    order = s365.targets(d, s365.scope())
    assert "c_old" not in order and order.index("l_new") < order.index("c_new"), order      # coupe : récent seulement, après les championnats
    d["lists"] = {"soccer_uefa_champs_league": {"prev": "x", "old": new, "at": 0, "n": 20, "ns": 5},
                  "soccer_brazil_campeonato": {"prev": "x", "old": new, "at": 0, "n": 20, "ns": 5}}
    modes = dict(s365.due_lists(d, ["soccer_uefa_champs_league", "soccer_brazil_campeonato"]))
    assert modes == {"soccer_uefa_champs_league": "first", "soccer_brazil_campeonato": "prev"}, modes   # jamais les pages anciennes d'une coupe
    print("ok  coupes 365scores : règlement seulement (pas les profils), matchs récents, après les championnats, pas de pages anciennes")


def main():
    test_selection()
    test_cap_and_spread()
    test_free_settlement()
    test_cups_settlement_only()
    print("tests suivi des stats en silence : OK")


if __name__ == "__main__":
    main()
