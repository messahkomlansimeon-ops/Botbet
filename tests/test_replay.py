"""Rejeu de la saison passée (hors ligne) : chaque match est prédit avec un modèle ajusté sur les SEULS matchs d'avant (jamais l'avenir), réglé avec
les stats du CSV, compté par famille, championnat, sens et tranche ; repris championnat par championnat ; la correction qu'il mesure sert de point
de départ aux vrais résultats, qui s'en écartent dès qu'ils sont assez nombreux, sans jamais être mélangés. Lancer : python -m tests.test_replay"""
import os
import random
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-replay-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, config as C, replay, store, strength  # noqa: E402

TEAMS = [f"T{i}" for i in range(12)]
TODAY = date(2026, 10, 12)


def season(seed=1, start=date(2024, 8, 10), days=790):
    """Matchs fictifs d'un championnat : 6 matchs tous les 4 jours, stats tirées au hasard autour de moyennes fixes par équipe."""
    rnd = random.Random(seed)
    out = []
    d = start
    while d < start + timedelta(days=days):
        teams = TEAMS[:]
        rnd.shuffle(teams)
        for h, a in zip(teams[::2], teams[1::2]):
            sh, sa = 10 + TEAMS.index(h) % 5, 9 + TEAMS.index(a) % 4
            m = {"date": str(d), "div": "E0", "home": h, "away": a, "hg": rnd.randint(0, 3), "ag": rnd.randint(0, 2), "referee": None,
                 "hs": max(0, int(rnd.gauss(sh, 3))), "as": max(0, int(rnd.gauss(sa, 3))), "hc": rnd.randint(2, 9), "ac": rnd.randint(1, 8),
                 "hf": rnd.randint(7, 15), "af": rnd.randint(7, 15), "hy": rnd.randint(0, 4), "ay": rnd.randint(0, 4), "hr": 0, "ar": 0, "odds": {}}
            m["hst"], m["ast"] = m["hs"] // 3, m["as"] // 3
            m["ftr"] = "H" if m["hg"] > m["ag"] else "A" if m["hg"] < m["ag"] else "D"
            out.append(m)
        d += timedelta(days=4)
    return out


def reset():
    store.save(calib.FILE, {})
    store.save(calib.BOOT_FILE, {})
    calib._memo.clear()


def test_no_future():
    ms = season()
    seen = []
    real_build = strength.build

    def spy(matches, on, *a, **k):
        seen.append(on)
        return real_build(matches, on, *a, **k)
    strength.build = spy
    real_picks = replay.picks
    used = []
    replay.picks = lambda model, m, sk, on: used.append((model and model["last"], m["date"])) or real_picks(model, m, sk, on)
    try:
        res = replay.replay_div("E0", ms, "2025-10-01", "2026-04-01")
    finally:
        strength.build, replay.picks = real_build, real_picks
    assert res["matches"] > 200 and res["picks"] > res["matches"] * 5, res
    assert all(last is None or last < day for last, day in used), "un modèle a vu le match qu'il prédit, ou un match d'après"
    assert len(seen) == len({d for d in seen}) and all((b - a).days >= C.REPLAY_STEP_DAYS for a, b in zip(seen, seen[1:])), seen
    fams = {f for f in res["acc"] if not f.startswith("_")}
    assert {"corners", "cards", "shots", "sot", "fouls", "yellow"} <= fams and "goals" not in fams, fams
    assert set(res["acc"]["_lg"]) == {"soccer_epl"} and any(k.startswith("shots|plus") for k in res["acc"]["_cell"]), res["acc"]["_cell"].keys()
    n, w, s = res["acc"]["corners"]
    assert 0 < w < n and 0.55 * n <= s <= 0.80 * n, res["acc"]["corners"]          # probas dans C.FAIR_P_RANGE
    print("ok  rejeu : modèle ajusté sur les seuls matchs d'avant, réajusté tous les 7 jours, stats seulement (pas les buts), par championnat et case")


def test_resume_and_force():
    reset()
    loads, have = [], {"E0"}
    loader = lambda div: loads.append(div) or (season() if div in have else [])     # noqa: E731 - SP1 : CSV absents d'abord
    old_divs = replay.divs
    replay.divs = lambda: ["E0", "SP1"]
    try:
        doc = replay.run(seconds=0, today=TODAY, loader=loader)           # plus de temps dès le premier match : rien d'écrit
        assert not doc["divs"] and not replay.load() and not replay.done()
        doc = replay.run(today=TODAY, loader=loader)
        assert doc["from"] == "2025-10-12" and doc["to"] == "2026-10-12" and loads == ["E0", "E0", "SP1"], (doc["from"], loads)
        assert doc["divs"]["SP1"]["matches"] == 0 and doc["acc"]["corners"] == doc["divs"]["E0"]["acc"]["corners"]
        assert not replay.done() and replay.missing() == ["SP1"] and replay.status_line().endswith(" · 1/2 championnats"), replay.status_line()
        msg = str(replay.message())
        assert "1 championnats" not in msg and "⚠️ Pas assez de données pour : Liga" in msg, msg
        have.add("SP1")                                                   # CSV arrivés : seul SP1 est refait, sur la même période
        doc = replay.run(today=TODAY + timedelta(days=9), loader=loader)
        assert doc["to"] == "2026-10-12" and loads == ["E0", "E0", "SP1", "SP1"] and replay.done(), loads
        assert replay.run(today=TODAY + timedelta(days=9), loader=loader) and loads == ["E0", "E0", "SP1", "SP1"]      # rien à refaire
        txt = replay.status_line()
        assert txt.startswith("📚 Rejeu de la saison passée : ") and " matchs, " in txt and "championnats" not in txt, txt
        assert "⚠️" not in str(replay.message())
        assert replay.run(force=True, today=TODAY + timedelta(days=9), loader=loader)["to"] == "2026-10-21"
        lines = replay.family_lines()
        assert lines and all(x[0] in "🟢🔴" and "%" in x for x in lines), lines
    finally:
        replay.divs = old_divs
    print("ok  rejeu repris championnat par championnat ; un championnat sans CSV est refait plus tard ; --force refait tout ; résumé lisible")


def test_matches_fetches_missing():
    from kelly.sources import footballdata as fd, http
    seasons = fd.default_seasons()
    got = []
    real = http.cached, fd.fetch_season
    http.cached = lambda url, headers=None: None if seasons[1] in url else type("R", (), {"text": "Date,HomeTeam,AwayTeam,FTHG,FTAG\n01/09/2026,A,B,1,0\n"})()
    fd.fetch_season = lambda div, s: got.append((div, s)) or ([{"date": "2025-09-01", "home": "C", "away": "D", "hg": 0, "ag": 0}], [])
    try:
        ms = replay.matches("E2")
    finally:
        http.cached, fd.fetch_season = real
    assert got == [("E2", seasons[1])] and len(ms) == 3, (got, ms)          # seule la saison absente du cache est téléchargée
    print("ok  saisons absentes du cache téléchargées (une fois), les autres lues sur place")


def test_prior_not_mixed():
    reset()
    acc = {}
    for i in range(400):                                       # rejeu : corners « plus » 58 % réalisés pour 66 % annoncés
        calib.accumulate(acc, {"market": "alternate_totals_corners", "name": "Over", "p0": 0.66, "sport_key": "soccer_epl"}, i % 50 < 29)
    store.save(calib.BOOT_FILE, {"v": replay.VERSION, "divs": {"E0": {}}, "acc": acc})
    calib._memo.clear()
    kb = calib.factor("alternate_totals_corners", side="plus", p=0.66)
    assert 0.4 < kb < 0.85, kb                                 # sans vrai résultat : la correction du rejeu s'applique
    assert calib.load() == {} and calib.factor("totals") == 1.0          # jamais mélangé aux vrais résultats ; autre famille : rien
    assert abs(calib.factor("alternate_totals_corners", side="moins", p=0.66) - calib.factor("alternate_totals_corners", acc, base=1.0)) < 1e-9
    from kelly import combos
    leg = {"market": "alternate_totals_corners", "name": "Over", "home": "A", "away": "B", "sport_key": "soccer_epl", "p0": 0.66, "conf": 0.9, "n_min": 10}
    assert combos.data_ok(leg)                                 # le rejeu corrige la proba, il n'écarte jamais un marché des combinés à lui seul
    # vrais résultats : bien plus mauvais (45 %) -> ils l'emportent, la correction descend plus bas que celle du rejeu
    store.save(calib.FILE, {"corners": [120, 54, 79.2]})
    calib._memo.clear()
    kr = calib.factor("alternate_totals_corners")
    assert not combos.data_ok(leg)                             # tes vrais résultats, eux, l'écartent
    assert kr < calib.factor("alternate_totals_corners", acc, base=1.0) and kr == calib._k([120, 54, 79.2], calib.factor("alternate_totals_corners", acc, base=1.0), fam="corners"), kr
    # peu de vrais résultats (sous le seuil) : le rejeu seul
    store.save(calib.FILE, {"corners": [20, 9, 13.2]})
    calib._memo.clear()
    assert calib.factor("alternate_totals_corners") == calib.factor("alternate_totals_corners", acc, base=1.0)
    old = C.REPLAY_PRIOR
    C.REPLAY_PRIOR = 0
    try:
        assert calib.factor("alternate_totals_corners") == 1.0                   # rejeu ignoré
    finally:
        C.REPLAY_PRIOR = old
    print("ok  rejeu = point de départ de la correction ; les vrais résultats s'en écartent, jamais mélangés ; REPLAY_PRIOR=0 l'ignore")


def test_bilan_shows_replay():
    from datetime import datetime, timezone
    from kelly import app, track
    reset()
    store.save(calib.FILE, {"shots": [67, 30, 43.5], "corners": [49, 30, 31.9]})
    day = str(datetime.now(timezone.utc).date())
    track.save([{"key": f"k{i}", "id": f"e{i}", "date": day, "market": "totals", "sent": True, "odds": 2.0, "prob": 0.55, "result": "win" if i % 2 else "loss"}
                for i in range(4)])
    store.save("bets.json", {"picks": [], "combos": []})
    old_divs = replay.divs
    replay.divs = lambda: ["E0"]
    try:
        replay.run(today=TODAY, loader=lambda div: season())
        out = str(app.handle_command("/bilan détail"))
    finally:
        replay.divs = old_divs
    assert "📚 <b>Rejeu de la saison passée</b>" in out and "🔴 Tirs · 67 · trop confiante (45 % / 65 %) · ×0,40 → écartés des combinés" in out, out
    assert "⏳ Corners · 49/60 · ×0," in out and "(rejeu)" in out, out                # pas assez de vrais résultats : la correction du rejeu, dite
    from kelly import status
    assert any(x.startswith("📚 Rejeu de la saison passée : ") for x in status.learning_lines())
    print("ok  bilan détail et status : bloc « 📚 Rejeu de la saison passée », tes vrais résultats inchangés")


def main():
    test_no_future()
    test_resume_and_force()
    test_matches_fetches_missing()
    test_prior_not_mixed()
    test_bilan_shows_replay()
    print("tests rejeu de la saison passée : OK")


if __name__ == "__main__":
    main()
