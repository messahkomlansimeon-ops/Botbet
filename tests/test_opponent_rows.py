"""Identifiant de l'ADVERSAIRE en dernier élément des lignes de matchs de FotMob, 365scores et API-Football (kelly/sources/rows.py) : les anciennes lignes restent lisibles, tous les
lecteurs par indice sont inchangés, la voie `saison` compte les équipes « avec adversaire », la ligne d'état de status. Hors ligne. Lancer : python -m tests.test_opponent_rows"""
import os
import tempfile
from datetime import date, datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-opp-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-opp-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import apistats, config as C, lanes, season, seasonlane as SL, store  # noqa: E402
from kelly.sources import fmmatch, rows as R, s365  # noqa: E402

SK = "soccer_epl"


def test_helpers():
    assert R.opponent([260901, 1, 2, 1, 5, 3], 2) is None, "ligne ancienne (nombre pair après l'en-tête) : pas d'adversaire"
    assert R.opponent([260901, 1, 2, 1, 5, 3, "77"], 2) == "77" and R.opponent([260901, 0, 5, 1, None], 3) is None
    assert R.opponent([260901, 3, 1, 2, 1, 5, 3, 99], 3) == 99 and R.opponent([], 2) is None and R.opponent(None, 2) is None
    assert R.opponent([260901, 1, 2, 1, 5, 3, None], 2) is None, "adversaire inconnu"
    assert R.with_opponent([1, 1, 2, 3], "9", 2) == [1, 1, 2, 3, "9"] and R.with_opponent([1, 1, 2, 3], None, 2) == [1, 1, 2, 3]
    assert R.with_opponent([1, 1, 2, 3, 4], "9", 2) == [1, 1, 2, 3, 4], "une ligne déjà impaire n'est jamais allongée"
    print("ok  parité : ligne ancienne = pas d'adversaire, ligne neuve = adversaire en dernier ; jamais deux fois")


def test_cell_never_reads_the_opponent():
    old = [260901, 1, 2, 1, 5, 3]
    new = [260901, 1, 2, 1, 5, 3, "77"]
    assert [R.cell(old, j, 2) for j in range(2, 8)] == [2, 1, 5, 3, None, None]
    assert [R.cell(new, j, 2) for j in range(2, 9)] == [2, 1, 5, 3, None, None, None], "la case de l'adversaire est une case vide pour un lecteur de statistiques, même après l'ajout d'une clé à KEYS"
    # une clé ajoutée plus tard : l'ancien lecteur par indice lirait l'adversaire comme sa 12e paire sans cell()
    assert [R.cell(new, j, 2) for j in (6, 7)] == [None, None]
    print("ok  rows.cell : jamais l'adversaire comme statistique, ligne ancienne plus courte = None")


def fm_match(i, hid="11", aid="22"):
    return {"id": f"fm{i}", "d": "2026-09-0%d" % (i + 1), "home": "Alpha", "away": "Beta", "hid": hid, "aid": aid,
            "s": {"goals": [2, 1], "shots": [12, 7], "corners": [6, 3]}}


def test_fotmob_and_365():
    d = {"teams": {}, "seen": {}, "recent": {}, "retry": {}}
    assert fmmatch.record(d, fm_match(1), SK)
    h, a = d["teams"]["11"]["m"][0], d["teams"]["22"]["m"][0]
    assert R.opponent(h, fmmatch.R_STATS) == "22" and R.opponent(a, fmmatch.R_STATS) == "11", (h, a)
    assert len(h) == 2 + 2 * len(fmmatch.KEYS) + 1, "toutes les paires, puis l'adversaire"
    assert h[fmmatch.R_STATS] == 2 and h[fmmatch.R_STATS + 1] == 1 and a[fmmatch.R_STATS] == 1 and a[fmmatch.R_STATS + 1] == 2, "les paires pour / contre sont inchangées"
    # lecture par indice : profils inchangés avec la ligne neuve ET une ligne ancienne (sans adversaire, plus courte)
    old = [h[0] - 3, 1] + [2, 0, 9, 4] + [None] * (2 * len(fmmatch.KEYS) - 4)
    d["teams"]["11"]["m"] += [old] * 4
    v = fmmatch._views(d["teams"]["11"]["m"])
    assert v["all"]["goals_for"] is not None
    # 365scores
    e = {"teams": {}, "seen": {}, "pending": {}, "recent": {}}
    m = {"id": "s1", "d": "2026-09-02", "hid": "5", "hn": "Alpha", "aid": "6", "an": "Beta", "s": {"goals": [1, 1], "shots": [9, 8]}}
    assert s365.record(e, m, SK)
    assert R.opponent(e["teams"]["5"]["m"][0], s365.R_STATS) == "6" and R.opponent(e["teams"]["6"]["m"][0], s365.R_STATS) == "5"
    assert s365._views(e["teams"]["5"]["m"])["all"]["goals_for"] is not None
    print("ok  FotMob et 365scores : l'adversaire est le dernier élément, les paires et les profils sont inchangés, une ligne ancienne plus courte se lit toujours")


def test_apistats():
    d = apistats.load()
    d["teams"] = {}
    ts = int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp())
    e = {"id": 701, "ts": ts, "home_id": 10, "away_id": 20, "home": "Alpha", "away": "Beta", "gh": 2, "ga": 0, "lg": "Premier League", "lk": SK}
    side = lambda: {"shots": 10, "sot": 4, "corners": 5, "fouls": 11, "cards": 2, "yel": 2, "nul": []}      # noqa: E731
    st = {"teams": {"10": side(), "20": side() | {"shots": 7}}}
    assert apistats.record(d, e, st) == 2
    h = d["teams"]["10"]["m"][0]
    assert R.opponent(h, apistats.R_STATS) == 20 and R.opponent(d["teams"]["20"]["m"][0], apistats.R_STATS) == 10, h
    assert len(h) == apistats.R_STATS + 2 * len(apistats.KEYS) + 1, "toutes les paires écrites (xG / jaunes inconnus compris) avant l'adversaire"
    # le lecteur par indice ne confond jamais l'adversaire avec une colonne de statistique
    v = apistats._views(d["teams"]["10"]["m"])
    assert v["all"]["goals_for"] == 2 and v["all"]["goals_against"] == 0, v["all"]
    cell = lambda r, j: r[j] if j < len(r) else None      # noqa: E731
    assert cell(h, apistats.R_STATS + 2 * apistats.KEYS.index("xg")) is None and cell(h, apistats.R_STATS + 2 * apistats.KEYS.index("yellow")) in (None, 2)
    print("ok  API-Football : l'adversaire est le dernier élément, toutes les paires restent écrites (l'adversaire n'est jamais lu comme un xG)")


def test_season_coverage_and_status():
    on = date(2026, 10, 14)
    mk = lambda tid, i, opp: {"team": f"T{tid}", "d": f"2026-09-{i + 1:02d}", "home": i % 2, "s": {"goals": (1, 1)}, "tid": str(tid), "opp": opp}      # noqa: E731
    obs = [mk(t, i, "x" if t < 6 else None) for t in range(10) for i in range(5)]
    assert season.coverage(obs, on, SK) == (10, 6), "10 équipes suivies (>= 4 matchs), 6 ont l'adversaire sur tous leurs matchs"
    partial = [mk(1, i, "x" if i < 2 else None) for i in range(5)]
    assert season.coverage(partial, on, SK) == (1, 0), "2 matchs avec adversaire sur 5 (< 4) : pas encore « avec adversaire »"
    got = season.snapshot(on, ["soccer_a", "soccer_b", "soccer_c"], {"football-data": lambda: obs})
    assert set(got) == {"soccer_a", "soccer_b", "soccer_c"} and got["soccer_a"] == {"source": "football-data", "teams": 10, "opp": 6}
    assert season.snapshot(on, ["soccer_a"], {"football-data": lambda: obs[:8], "fotmob": lambda: [], "365scores": lambda: [], "api-football": lambda: []}) == {}, "ligue non couverte : absente"
    store.save(SL.STATE_FILE, {})
    assert SL.state_text() == "ligues suivies : pas encore mesuré"
    store.save(SL.STATE_FILE, {"day": "2026-10-14", "leagues": got})
    assert SL.state_text() == "ligues suivies : 3 · équipes : 30 · avec adversaire : 18 (au 2026-10-14)", SL.state_text()
    line = next(x for x in lanes.lines() if x.startswith("saison :"))
    assert line.endswith("ligues suivies : 3 · équipes : 30 · avec adversaire : 18 (au 2026-10-14)"), line
    # un obs de football-data porte toujours l'adversaire
    ms = [{"date": "2026-09-01", "div": "E0", "home": "A", "away": "B", "hg": 1, "ag": 0, "hs": 5, "as": 4, "hst": 2, "ast": 1, "hf": 9, "af": 8, "hc": 5, "ac": 3, "hy": 1, "ay": 2, "hr": 0, "ar": 0}]
    o = season._from_matches(ms)
    assert {(x["team"], x["opp"]) for x in o} == {("A", "B"), ("B", "A")}
    print("ok  état de la voie : ligues suivies, équipes (>= 4 matchs), équipes avec adversaire (>= 4 matchs qui le portent) ; ligne « saison » de status")


def test_old_rows_in_obs():
    teams = {"1": {"nm": "Alpha", "sk": [SK], "m": [[260920, 1, 2, 1] + [None] * (2 * len(fmmatch.KEYS) - 2),
                                                   [260921, 0, 0, 0] + [None] * (2 * len(fmmatch.KEYS) - 2) + ["22"]]}}
    obs = season._from_team_rows(teams, SK, fmmatch.KEYS, fmmatch.R_STATS)
    assert [o["opp"] for o in obs] == [None, "22"] and all(o["tid"] == "1" for o in obs), obs
    print("ok  observations : l'adversaire d'une ligne neuve est repris (identifiant de la source), une ligne ancienne n'en a pas")


def main():
    for t in (test_helpers, test_cell_never_reads_the_opponent, test_fotmob_and_365, test_apistats, test_season_coverage_and_status, test_old_rows_in_obs):
        t()
    print("tests adversaire dans les lignes : OK")


if __name__ == "__main__":
    main()
