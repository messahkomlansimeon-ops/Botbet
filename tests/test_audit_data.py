"""Tests hors ligne des correctifs de l'audit « données » : championnats africains vus par 1xBet (B1), règlement FotMob des championnats
1xBet (B2), qualité et effectif par source (B3), buts du classement réduits (B4), base recréée / export prudent (B5), remplissage
FotMob (B6). Lancer : python -m tests.test_audit_data"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

TMP = tempfile.mkdtemp(prefix="kelly-audit-data-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["ODDS_CACHE_DIR"] = os.path.join(TMP, "odds")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import config as C, fair, monde, prematch, settle, store  # noqa: E402
from kelly.sources import features, fmmatch, fotmob  # noqa: E402

EGYPT, ALGERIA, ROMANIA = "africa_egypt_premier_league", "soccer_xb_algeria", "soccer_xb_romania"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "data"))
    store.DATA_DIR = os.path.join(TMP, "data")
    features._cache.clear()


def prof(sk, n, **stats):
    """Profil d'équipe au format apistats / fmmatch : mêmes chiffres à domicile, à l'extérieur et au total."""
    view = {"n": n} | stats
    return {"nm": "x", "sk": list(sk), "all": view, "home": view, "away": view}


def table(teams, matches=()):
    return {"leagues": {}, "teams": teams, "matches": list(matches)}


def stderr_of(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        res = fn(*a, **kw)
    return res, buf.getvalue()


# ---------- B1 : Égypte / Maroc / Algérie ----------

def test_b1_africa_candidates():
    for key in (EGYPT, "africa_morocco_botola_pro", ALGERIA):
        teams = {"A": prof(["africa"], 8), "B": prof([key], 8), "C": prof(["soccer_xb_romania"], 8), "D": prof(["africa_other"], 8)}
        assert set(features._aps_candidates(teams, key)) == {"A", "B"}, key
        assert set(features._fm_candidates(teams, key)) == {"A", "B"}, key
    teams = {"A": prof(["africa"], 8), "B": prof([EGYPT], 8), "C": prof(["soccer_xb_romania"], 8), "D": prof(["africa_other"], 8)}
    assert set(features._aps_candidates(teams, ROMANIA)) == {"C"}, "les autres championnats ne changent pas"
    assert set(features._fm_candidates(teams, "africa")) == {"A", "B", "D"}


def test_b1_match_features_egypt():
    fresh()
    stats = dict(corners_for=5.0, corners_against=5.0, goals_for=1.4, goals_against=1.1, cards_for=2.0, cards_against=2.0)
    features._cache["aps"] = {"teams": {"Cairo FC": prof(["africa"], 8, **stats), "Delta SC": prof(["africa"], 8, **stats)}}
    features._cache["fmm"] = {"teams": {}}
    features._cache["fm"] = {"leagues": {EGYPT: table({"Cairo FC": {"n": 8, "gf": 12, "ga": 8}, "Delta SC": {"n": 8, "gf": 9, "ga": 10}})}}
    f = features.match_features("Cairo FC", "Delta SC", EGYPT)
    assert "API-Football" in f["sources"] and "exp_corners" in f, f
    assert "FotMob (classement)" in f["sources"] and "exp_goals_home" in f, f      # buts du classement (_fotmob) aussi
    assert f["std_n"] == (8, 8) and f["aps_n"] == (8, 8)
    # l'Algérie (clé soccer_*) passait déjà par _fotmob : ses profils « africa » doivent maintenant servir
    features._cache["fm"] = {"leagues": {ALGERIA: table({"Cairo FC": {"n": 8, "gf": 12, "ga": 8}, "Delta SC": {"n": 8, "gf": 9, "ga": 10}})}}
    f = features.match_features("Cairo FC", "Delta SC", ALGERIA)
    assert "API-Football" in f["sources"] and "exp_corners" in f, f


def test_b1_monde_no_double_count():
    ko = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)
    ev = {"id": "xb1", "sport_key": EGYPT, "home_team": "Al Ahly", "away_team": "Zamalek", "commence_time": "2026-10-09T17:00:00Z"}
    scan = {"leagues": {EGYPT: [ev]}}
    af = [{"id": 1, "lg": "Premier League (Egypt)", "home": "Al Ahly", "away": "Zamalek", "ts": int(ko.timestamp())},
          {"id": 2, "lg": "Premier League (Egypt)", "home": "Pyramids", "away": "Ismaily", "ts": int(ko.timestamp())}]
    data = monde.build(scan, "2026-10-09", NOW, africa=af)
    assert [m[1:3] for m in data["africa"]] == [["Pyramids", "Ismaily"]], data["africa"]
    assert monde.total(data) == 2, "1 match 1xBet + 1 match API-Football seulement, le doublon n'est pas compté"


# ---------- B2 : règlement par FotMob ----------

def fm_league(key, matches, lgid):
    store.save(fotmob.PROFILE_FILE, {"leagues": {key: {"id": lgid, "teams": {}, "matches": matches}}})


def row(key, home, away, commence, market="totals", name="Over", point=1.5, sk="soccer_xb_romania"):
    return {"key": key, "id": key, "sport_key": sk, "home": home, "away": away, "commence": commence, "market": market, "name": name,
            "point": point, "prob": 0.6, "p0": 0.6, "pick": f"{name} {point}", "min_odds": 1.8, "result": None}


def test_b2_fotmob_fallback():
    fresh()
    fm_league(ROMANIA, [{"d": "2026-10-07", "home": "Sepsi OSK Sfantu Gheorghe", "away": "FCSB", "hg": 2.0, "ag": 1.0},
                        {"d": "2026-10-07", "home": "Slavia Prague", "away": "Sparta Prague", "hg": 0.0, "ag": 0.0}], 189)
    b = row("k1", "ACS Sepsi OSK Sfantul Gheorghe", "FCSB", "2026-10-08T16:00:00Z")           # coup d'envoi 1xBet : un jour après la date FotMob
    ev = settle.fotmob_score(b)
    assert ev and settle._score_map(ev) == {"ACS Sepsi OSK Sfantul Gheorghe": 2.0, "FCSB": 1.0}, ev
    assert settle.fotmob_score(row("k2", "ACS Sepsi OSK Sfantul Gheorghe", "FCSB", "2026-10-12T16:00:00Z")) is None, "trop loin dans le temps"
    assert settle.fotmob_score(row("k3", "Sepsi OSK", "Rapid", "2026-10-07T16:00:00Z")) is None, "l'autre équipe ne correspond pas"
    # règlement complet d'un pronostic de buts : ni API-Football ni football-data
    store.save(fair.FILE, [b | {"date": "2026-10-08"}])
    rows, done = settle.settle_fair(NOW)
    assert rows[0]["result"] == "win" and rows[0]["final"] == "2-1" and done, rows


def test_b2_combo_with_unresolved_leg():
    fresh()
    fm_league(ROMANIA, [{"d": "2026-09-20", "home": "FCSB", "away": "Rapid", "hg": 2.0, "ag": 0.0}], 189)
    ok = row("a", "FCSB", "Rapid", "2026-09-20T16:00:00Z")
    miss = row("b", "Gamma", "Delta", "2026-09-20T18:00:00Z")                    # aucun score nulle part, délai d'attente dépassé
    mk = lambda legs: {"date": "2026-09-20", "tier": 5, "min_odds": 5.0, "prob": 0.2, "keys": [x["key"] for x in legs],   # noqa: E731
                       "legs": legs, "result": None}
    store.save(fair.COMBOS_FILE, [mk([dict(ok), dict(miss)]), mk([dict(ok), dict(ok, key="c")])])
    rows, _ = settle.settle_fair_combos(NOW)
    assert rows[0]["legs"][0]["result"] == "win" and rows[0]["legs"][1]["result"] == settle.UNKNOWN, rows[0]
    assert rows[0]["result"] == settle.UNKNOWN, "jamais « win » tant qu'une jambe n'est pas résolue"
    assert rows[1]["result"] == "win"


# ---------- B3 / B4 : qualité, effectif, classement réduit ----------

def std_cache(n, key=ROMANIA, extra=()):
    teams = {"Alpha": {"n": n, "gf": 1.5 * n, "ga": 1.0 * n}, "Bravo": {"n": n, "gf": 1.2 * n, "ga": 1.3 * n}} | dict(extra)
    features._cache["fm"] = {"leagues": {key: table(teams)}}


def fm_stats(n):
    return dict(corners_for=5.5, corners_against=5.0, goals_for=1.6, goals_against=1.2, shots_for=12.0, shots_against=11.0)


def test_b3_quality_min_and_sample_per_source():
    fresh()
    std_cache(20)                                                  # classement : 20 matchs, qualité 0,50
    features._cache["fmm"] = {"teams": {"Alpha": prof([ROMANIA], 3, **fm_stats(3)), "Bravo": prof([ROMANIA], 3, **fm_stats(3))}}
    f = features.match_features("Alpha", "Bravo", ROMANIA)
    assert f["std_n"] == (20, 20) and f["aps_n"] == (3, 3) and f["n_home"] == 20, f
    assert f["aps_q"] == features.aps_quality(3) == 0.2, "FotMob 3 matchs : la qualité ne remonte pas à celle du classement"
    assert fair.stat_n(f, "totals") == 20 and fair.stat_n(f, prematch.CORNERS) == 3 and fair.stat_n(f, "team_shots_total") == 3
    opts = fair.options(f, "Alpha", "Bravo")
    tags = {o[0]: o[4] for o in opts}
    assert "3 derniers matchs seulement" in tags[prematch.CORNERS], tags
    assert "derniers matchs" not in tags["totals"], "buts du classement (20 matchs) : pas de mention"
    # l'autre sens : classement court (4 matchs, qualité 0,34), FotMob sans buts mais riche (8 matchs, 0,50) -> la qualité la plus basse l'emporte
    fresh()
    std_cache(4)
    nogoals = {k: v for k, v in fm_stats(8).items() if not k.startswith("goals")}
    features._cache["fmm"] = {"teams": {"Alpha": prof([ROMANIA], 8, **nogoals), "Bravo": prof([ROMANIA], 8, **nogoals)}}
    out = {"sources": []}
    features._fotmob(out, "Alpha", "Bravo", ROMANIA)
    assert out["aps_q"] == 0.34
    features._fmmatch(out, "Alpha", "Bravo", ROMANIA)
    assert out["aps_q"] == 0.34 < features.aps_quality(8) == 0.5 and out["std_n"] == (4, 4) and out["aps_n"] == (8, 8), out
    assert fair.stat_n(out, prematch.CORNERS) == 8 and fair.stat_n(out, "totals") == 4, "chaque chiffre garde l'effectif de sa source"


def test_b3_fmmatch_goals_replace_standings():
    fresh()
    std_cache(20)
    features._cache["fmm"] = {"teams": {"Alpha": prof([ROMANIA], 7, **fm_stats(7)), "Bravo": prof([ROMANIA], 7, **fm_stats(7))}}
    f = features.match_features("Alpha", "Bravo", ROMANIA)
    assert "std_n" not in f and f["n_home"] == 7 and "FotMob (classement)" not in f["sources"], f
    assert f["exp_goals_home"] == round((1.6 + 1.2) / 2, 2), f                      # chiffres des matchs FotMob, pas ceux du classement
    assert f["aps_q"] == features.aps_quality(7)
    # moins de APISTATS_GOOD_N matchs : le classement garde ses buts
    features._cache["fmm"] = {"teams": {"Alpha": prof([ROMANIA], 5, **fm_stats(5)), "Bravo": prof([ROMANIA], 5, **fm_stats(5))}}
    f = features.match_features("Alpha", "Bravo", ROMANIA)
    assert f["std_n"] == (20, 20) and f["exp_goals_home"] != round((1.6 + 1.2) / 2, 2)


def test_b4_standings_shrunk():
    t = {"n": 3, "gf": 9.0, "ga": 3.0}
    assert round(features._shrunk(t, "gf", 1.0), 4) == round((3 * 3.0 + 6 * 1.0) / 9, 4)
    assert features._shrunk(t, "gf", None) == 3.0
    league = {f"T{i}": {"n": 3, "gf": 3.0, "ga": 3.0} for i in range(10)}
    assert features._league_rate(league, "gf") == 1.0 and features._league_rate({}, "gf") is None
    hot = {"n": 3, "gf": 9.0, "ga": 3.0}
    raw, shr = {"sources": []}, {"sources": []}
    features._standings_goals(raw, "A", "B", hot, hot)
    features._standings_goals(shr, "A", "B", hot, hot, league=league | {"A": hot, "B": hot})
    assert 0 < shr["exp_goals_total"] < raw["exp_goals_total"], (raw, shr)
    late = {"n": 36, "gf": 108.0, "ga": 36.0}                                        # fin de saison : presque pas de rapprochement
    full, full_s = {"sources": []}, {"sources": []}
    features._standings_goals(full, "A", "B", late, late)
    features._standings_goals(full_s, "A", "B", late, late, league={"A": late, "B": late})
    assert abs(full["exp_goals_total"] - full_s["exp_goals_total"]) < 0.1 * full["exp_goals_total"]


# ---------- B5 : base recréée, export prudent ----------

def make_backup(files):
    bd = os.path.join(store.DATA_DIR, "sauvegarde")
    for name, obj in files.items():
        p = os.path.join(bd, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    return bd


def test_b5_recreate_from_backup():
    fresh()
    make_backup({"bets.json": {"picks": [1], "combos": []}, "fair.json": [{"key": "a"}], "archive/tracked-2026.json": [{"key": "t"}]})
    assert not os.path.exists(store.db_path())
    got, err = stderr_of(store.load, "bets.json", None)
    assert got == {"picks": [1], "combos": []} and "ATTENTION" in err and "3 document(s)" in err, err
    assert store.load("fair.json", None) == [{"key": "a"}] and store.load("archive/tracked-2026.json", None) == [{"key": "t"}]
    assert len(os.listdir(os.path.join(store.DATA_DIR, "sauvegarde"))) == 3, "la sauvegarde reste en place"
    # des anciens fichiers data/*.json l'emportent (pas de restauration en plus)
    fresh()
    make_backup({"bets.json": {"old": True}})
    with open(os.path.join(store.DATA_DIR, "bets.json"), "w") as f:
        json.dump({"new": True}, f)
    assert stderr_of(store.load, "bets.json", None)[0] == {"new": True}


def test_b5_export_refuses_mass_deletion():
    fresh()
    files = {f"doc{i}.json": {"i": i} for i in range(6)} | {"bets.json": {}, "tracked.json": [], "fair.json": []}
    bd = make_backup(files)
    assert stderr_of(store.names)[0]                                          # base restaurée depuis la sauvegarde : tout y est
    for name in list(store.names()):
        store.remove(name)                                                   # puis vidée (kelly.db corrompue, remplacée...)
    store.save("doc0.json", {"i": 0})
    n, err = stderr_of(store.export)
    assert n == 1 and "ATTENTION" in err and len(os.listdir(bd)) == 9, (n, err, os.listdir(bd))
    # bets.json manque alors que le dossier l'a : refus même avec la moitié des documents
    for name in files:
        if name not in ("bets.json", "tracked.json"):
            store.save(name, files[name])
    n, err = stderr_of(store.export)
    assert "ATTENTION" in err and os.path.exists(os.path.join(bd, "bets.json")), err
    # base complète sauf un document périmé : l'effacement normal continue
    for name in files:
        store.save(name, files[name])
    store.remove("doc5.json")
    n, err = stderr_of(store.export)
    assert not err and not os.path.exists(os.path.join(bd, "doc5.json")) and os.path.exists(os.path.join(bd, "doc1.json")), err
    # restes d'un export interrompu (processus mort) : supprimés ; celui d'un processus vivant reste
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone, live = os.path.join(bd, f"doc1.json.{dead.pid}.tmp"), os.path.join(bd, f"doc2.json.{os.getppid()}.tmp")
    for p in (gone, live):
        open(p, "w").close()
    store.export()
    assert not os.path.exists(gone) and os.path.exists(live)


# ---------- B6 : remplissage FotMob, ligues 1xBet ----------

def test_b6_fill_interleaves_and_flag():
    fresh()
    today = datetime(2026, 10, 9).date()
    est, xb = "soccer_epl", ROMANIA

    def mk(sk, k):
        done = [{"id": f"{sk}{i}", "url": "u", "d": "2026-10-01", "home": f"H{sk}{i}", "away": f"A{sk}{i}", "hid": f"h{sk}{i}", "aid": f"a{sk}{i}",
                 "done": True} for i in range(k)]
        return done
    ms = {est: mk(est, 3), xb: mk(xb, 10)}
    old = (fmmatch.leagues, fmmatch.league_matches)
    fmmatch.leagues = lambda: [xb, est]
    fmmatch.league_matches = lambda sk: ms[sk]
    try:
        cands = fmmatch.candidates(fmmatch.load(), today)
    finally:
        fmmatch.leagues, fmmatch.league_matches = old
    first = [sk for sk, _ in cands[:6]]
    assert first.count(est) == 3, "les ligues établies gardent la moitié des pages même après 10 équipes neuves"
    assert len(cands) == 13
    assert (ROMANIA in fotmob.EXTRA_LEAGUES) == C.XWORLD_ENABLED
    code = "import os; os.environ['XWORLD_ENABLED']='0'; from kelly.sources import fotmob; print(fotmob.EXTRA_LEAGUES.get('soccer_xb_romania'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=os.getcwd())).stdout.strip()
    assert out == "None", out


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests)} tests audit données : OK")


if __name__ == "__main__":
    main()
