"""Résilience (hors ligne) : signal de vie healthchecks.io (par le proxy seulement, « en panne » si l'écoute Telegram est arrêtée depuis 15 min) ; matin
refait si la liste des matchs manque (rien réglé ni envoyé, 3 essais) ; paris réels réglés par FotMob et le suivi des matchs quand API-Football se tait ;
apprentissage compté par match ; une seule ligne par match et marché dans un passage ; documents précieux : instantanés, réparation d'un document
abîmé, annonce ; script kelly (garde, Termux:Boot, mise à jour testée avec retour arrière). Lancer : python -m tests.test_resilience"""
import os
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-resil-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "HC_PING_URL", "STATS_API_KEY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

import termux_runner as R  # noqa: E402
from kelly import app, calib, fair, odds, settle, stats, store, telegram, watch  # noqa: E402

UTC = timezone.utc
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Proc:
    def __init__(self, rc=None):
        self.rc, self.pid = rc, 4242

    def poll(self):
        return self.rc


def test_heartbeat():
    clk = Clock()
    sv = R.ServeSupervisor(["x"], clock=clk)
    sent = []
    post = lambda url, data=None, proxies=None, timeout=None: sent.append((url, data.decode(), proxies)) or type("R", (), {"status_code": 200})()  # noqa: E731
    hb = R.Heartbeat("https://hc-ping.com/abc/", sv, proxy=lambda: "http://u:p@proxy:80", post=post)
    sv.proc, sv.started = Proc(), clk.t
    assert hb.beat() and sent[-1][0] == "https://hc-ping.com/abc" and sent[-1][2] == {"http": "http://u:p@proxy:80", "https": "http://u:p@proxy:80"}
    assert sent[-1][1].startswith("ok")
    # l'écoute plante en boucle : « en panne » seulement après 15 min
    sv.proc.rc = 1
    sv.check()
    clk.t += 600
    assert sv.down_for() == 600 and hb.beat() and not sent[-1][0].endswith("/fail")
    sv.proc, sv.started = Proc(), clk.t                       # relancée... et replante aussitôt : toujours « arrêtée »
    clk.t += 30
    sv.proc.rc = 1
    sv.check()
    clk.t += 300
    assert sv.down_for() >= R.SERVE_DOWN_FAIL and hb.beat() and sent[-1][0] == "https://hc-ping.com/abc/fail" and "écoute Telegram arrêtée" in sent[-1][1]
    sv.proc, sv.started = Proc(), clk.t                       # elle tient 2 min : de nouveau « ok »
    clk.t += R.STABLE_SECS
    sv.check()
    assert sv.down_for() == 0 and hb.beat() and sent[-1][0] == "https://hc-ping.com/abc"
    # jamais en direct ; sans adresse : rien
    n = len(sent)
    assert not R.Heartbeat("https://hc-ping.com/abc", sv, proxy=lambda: None, post=post).beat() and len(sent) == n
    assert not R.Heartbeat("", sv, proxy=lambda: "http://p:1", post=post).beat() and len(sent) == n
    assert R.Heartbeat("u", sv, proxy=lambda: "http://p:1", post=lambda *a, **k: (_ for _ in ()).throw(OSError("réseau"))).beat() is False
    assert R.proxy_url({"XBET_PROXY": " http://a:b@c:1 "}) == "http://a:b@c:1" and R.proxy_url({}, path="/absent") is None
    print("ok  signal de vie : par le proxy seulement (jamais en direct), « en panne » quand l'écoute Telegram plante depuis 15 min, « ok » au retour")


def test_morning_retry():
    old = (app.refresh_sources, odds.scan_today, app.morning_report)
    sent = []
    called = []

    class Stop(Exception):
        pass
    app.refresh_sources = lambda: None
    odds.scan_today = lambda day, after: {"day": str(day), "sports": [], "leagues": {}, "complete": False, "error": "HTTPError 503", "failed": []}
    app.morning_report = lambda now: called.append(1) or (_ for _ in ()).throw(Stop())
    old_send = telegram.send
    telegram.send = lambda *a, **k: sent.append(a) or True
    try:
        assert app.daily_entry() == app.RETRY_RC and app.daily_entry() == app.RETRY_RC
        assert not called and not sent and not store.exists(app.DAILY_FILE), (called, sent)       # rien réglé, rien envoyé, matin pas « fait »
        assert store.load(app.TRY_FILE, {})["n"] == 2
        try:
            app.daily_entry()                                 # 3e essai : il part quand même (le message dira que la liste manque)
            raise AssertionError("le 3e essai aurait dû continuer")
        except Stop:
            pass
        assert called == [1]
        store.remove(app.TRY_FILE)
        try:
            app.daily_entry(force=True)                       # à la main (--force) : jamais d'attente
        except Stop:
            pass
        assert called == [1, 1]
    finally:
        app.refresh_sources, odds.scan_today, app.morning_report = old
        telegram.send = old_send
    print("ok  matin sans liste des matchs : rien réglé ni envoyé, refait 30 min plus tard (code 75), part quand même au 3e essai ; --force jamais retenu")


def test_settle_fallbacks():
    ko = "2030-01-01T19:00:00Z"
    now = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
    row = {"key": "a", "id": "m1", "sport_key": "soccer_xb_argentina_2", "group": "soccer", "event": "Alpha - Bravo", "home": "Alpha", "away": "Bravo",
           "league": "L", "commence": ko, "market": "h2h", "who": None, "point": None, "name": "Alpha", "pick": "Alpha", "odds": 2.0, "prob": .5,
           "sent": True, "date": "2030-01-01", "result": None}
    from kelly import track
    track.save([dict(row)])
    store.save("bets.json", {"picks": [], "combos": []})
    store.save(watch.FILE, {"v": watch.V, "legs": {"2030-01-01|vm1|a": {"gid": 7, "at": now.isoformat()}},
                            "games": {"7": {"score": "3-1", "s90": "2-1", "et": 1, "done": 1, "at": now.isoformat()}}})
    old = (settle.FootballData.match, settle.FootballData.fotmob_score, settle.fmmatch.match_stats, settle.football_score, odds.fetch_scores)
    paid = []
    settle.FootballData.match = lambda self, b: None
    settle.FootballData.fotmob_score = lambda self, b: None
    settle.fmmatch.match_stats = lambda b: None
    settle.football_score = lambda b: None
    odds.fetch_scores = lambda sk: paid.append(sk) or []
    os.environ["STATS_API_KEY"] = "x"
    stats._halted = True
    try:
        bets, done = settle.settle_pending(now, scores_by_sport={}, stats_for=lambda b: {})
        r = track.load()[0]
        assert r["result"] == "win" and r.get("final") and not paid, (r, paid)          # score à 90 min (2-1) du suivi : Alpha gagne
        assert watch.final_score({"key": "a", "id": "m1", "home": "Alpha", "away": "Bravo"})["scores"][0]["score"] == "2"
        assert watch.final_score({"key": "zz", "home": "A", "away": "B"}) is None
        store.save(watch.FILE, {"v": watch.V, "legs": {}, "games": {}})
        track.save([dict(row, key="b", id="m2")])
        settle.settle_pending(now, scores_by_sport={}, stats_for=lambda b: {})
        assert paid == ["soccer_xb_argentina_2"], paid                # API-Football en panne : le repli payant redevient possible
    finally:
        (settle.FootballData.match, settle.FootballData.fotmob_score, settle.fmmatch.match_stats, settle.football_score, odds.fetch_scores) = old
        os.environ.pop("STATS_API_KEY", None)
        stats._halted = False
    print("ok  paris réels : réglés par FotMob ou par le score final du suivi (90 min en cas de prolongation) quand API-Football se tait ;"
          " API-Football en panne : repli payant permis")


def test_calib_per_match():
    store.save(calib.FILE, {})
    calib._memo.clear()

    def r(i, mid, market, name, point, won, p=0.6, day="2030-01-01"):
        return {"key": f"k{i}", "id": mid, "sport_key": "soccer_epl", "home": "A", "away": "B", "market": market, "name": name, "point": point,
                "prob": p, "p0": p, "date": day, "result": "win" if won else "loss"}
    rows = [r(1, "m1", "team_shots_home", "Over", 10.5, True), r(2, "m1", "team_shots_total", "Over", 22.5, True),
            r(3, "m1", "team_shots_away", "Under", 9.5, False), r(4, "m2", "team_shots_total", "Over", 21.5, False)]
    assert calib.add(rows, date(2030, 1, 2)) == 4
    d = calib.load()
    assert abs(d["shots"][0] - 2) < 1e-3 and abs(d["shots"][1] - 2 / 3) < 1e-3 and d["_v"] == calib.VERSION, d["shots"]     # 2 matchs, pas 4 lignes
    # migration : cumuls v1 (par ligne) ramenés au rapport matchs / lignes des pronostics déjà comptés
    store.save(calib.FILE, {"shots": [43, 18, 30.0], "goals": [10, 5, 6.0], "_cell": {"shots|plus": [40, 17, 28.0]}, "_lg": {"soccer_epl": {"shots": [43, 18, 30.0]}}})
    calib._memo.clear()
    old = [dict(x, calib=True) for x in rows[:3]] + [dict(rows[3], calib=True)] + [r(9, "m9", "totals", "Over", 2.5, True)]
    old[-1]["calib"] = True
    calib.add(old + [r(10, "m3", "totals", "Over", 2.5, True)], date(2030, 1, 3))
    d = calib.load()
    assert d["_v"] == 2 and d["_cell"]["shots|plus"][0] < 21 and d["_lg"]["soccer_epl"]["shots"][0] < 22, d       # 2 matchs / 4 lignes : x0,5
    goals_n = d["goals"][0]
    calib.add(old + [r(11, "m4", "totals", "Over", 2.5, False)], date(2030, 1, 3))           # une seule fois
    assert calib.load()["_cell"]["shots|plus"][0] == d["_cell"]["shots|plus"][0] and calib.load()["goals"][0] > goals_n
    calib._memo.clear()
    print("ok  fiabilité comptée par match (k lignes liées d'un match pèsent 1/k) ; anciens cumuls ramenés une fois au rapport matchs / lignes")


def test_fair_one_row_per_market():
    store.save(fair.FILE, [])
    base = {"id": "m1", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2030-01-01T19:00:00Z", "market": "alternate_totals_corners",
            "prob": 0.7, "p0": 0.7}
    pronos = [base | {"key": "m1|c|10.5|Under", "name": "Under", "point": 10.5}, base | {"key": "m1|c|9.5|Under", "name": "Under", "point": 9.5}]
    fair.record(pronos, date(2030, 1, 1), shown=pronos)
    rows = fair.load()
    assert len(rows) == 1 and rows[0]["point"] == 10.5, rows
    print("ok  pronostics suivis : une seule ligne par match et marché dans un même passage (la mieux classée)")


def _corrupt(name):
    with sqlite3.connect(store.db_path()) as con:
        con.execute("UPDATE docs SET body='{abîmé' WHERE name=?", (name,))


def test_protected_docs():
    store.save("calib.json", {"goals": [100, 60, 58.0]})
    store.save("bets.json", {"picks": [], "combos": [{"tier": "x"}]})
    store.save("cache_x.json", {"a": 1})
    assert store.snapshot("2030-01-01") >= 2
    store.save("calib.json", {"goals": [120, 70, 69.0]})
    _corrupt("calib.json")
    assert store.load("calib.json", {}) == {"goals": [100, 60, 58.0]}                   # remis tel qu'il était à l'instantané
    assert store.load("calib.json", {}) == {"goals": [100, 60, 58.0]} and "calib.json" not in store._unreadable
    _corrupt("cache_x.json")
    assert store.load("cache_x.json", {"vide": 1}) == {"vide": 1}                         # un cache n'est pas précieux : comme avant
    # sans instantané : gelé (aucune écriture ne le remplace)
    store.save("means.json", {"f": {"corners": [1, 2]}})
    _corrupt("means.json")
    assert store.load("means.json", {}) == {} and "means.json" in store._unreadable
    store.save("means.json", {})
    with sqlite3.connect(store.db_path()) as con:
        assert con.execute("SELECT body FROM docs WHERE name='means.json'").fetchone()[0] == "{abîmé"
    store._unreadable.discard("means.json")
    for _ in range(3):
        store.load("means.json", {})
    store._unreadable.discard("means.json")
    # refait en entier à chaque calcul (réglage, modèle) : jamais gelé, le prochain calcul le remplace, rien à annoncer
    store.save("tuning.json", {"E0": 1})
    _corrupt("tuning.json")
    assert store.load("tuning.json", {}) == {} and "tuning.json" not in store._unreadable
    reps = store.load(store.REPAIRS, [])
    assert [(x["name"], x["from"]) for x in reps] == [("calib.json", "2030-01-01"), ("means.json", None)], reps
    sent = []
    old = telegram.send
    telegram.send = lambda m, *a, **k: sent.append(str(m)) or True
    try:
        app.announce_repairs()
        app.announce_repairs()
    finally:
        telegram.send = old
    assert len(sent) == 1 and "la fiabilité par marché : document abîmé, remis tel qu'il était le mardi 1er janvier" in sent[0], sent
    assert "les moyennes des stats : document abîmé et pas encore de copie" in sent[0] and "kelly restaurer" in sent[0], sent
    # 3 jours gardés, jamais l'instantané d'un document abîmé
    for day in ("2030-01-02", "2030-01-03", "2030-01-04"):
        store.snapshot(day)
    snaps = store.names(store.SNAP)
    assert {s.split("/")[1] for s in snaps} == {"2030-01-02", "2030-01-03", "2030-01-04"} and not any(s.endswith("/means.json") for s in snaps), snaps
    out = tempfile.mkdtemp(prefix="kelly-exp-")
    store.export(out)
    assert not os.path.exists(os.path.join(out, "snap")), os.listdir(out)
    print("ok  mémoire précieuse : instantané par jour (3 gardés), document abîmé remis tel qu'il était (ou gelé sans copie), annoncé une fois sur Telegram")


def test_kelly_script():
    path = os.path.join(ROOT, "scripts", "kelly")
    assert subprocess.run(["bash", "-n", path]).returncode == 0
    s = open(path, encoding="utf-8").read()
    for part in ("install_boot", "$HOME/.termux/boot", "start_guard", "    guard)", "selftest", 'git reset -q --hard "$old"', "keep_mirror", "test-alerte)",
                 'touch "$STOPPED"', "python bot.py --sample"):
        assert part in s, part
    upd = s[s.index("    update)"):s.index("    guard)")]
    assert upd.index("git pull") < upd.index("selftest") < upd.index('cp "$DIR/scripts/kelly"'), "le script n'est installé qu'après le test"
    print("ok  script kelly : garde (relance toutes les 5 min), Termux:Boot, mise à jour testée avant d'être lancée, retour arrière automatique")


def main():
    test_heartbeat()
    test_morning_retry()
    test_settle_fallbacks()
    test_calib_per_match()
    test_fair_one_row_per_market()
    test_protected_docs()
    test_kelly_script()
    print("tests résilience : OK")


if __name__ == "__main__":
    main()
