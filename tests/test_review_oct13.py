"""Corrections de la relecture indépendante du 13/10 (hors ligne) : chaque test reproduit le cas réel trouvé par les relecteurs.
Lancer : python -m tests.test_review_oct13"""
from tests import test_proxy_backup as P      # fixe DATA_DIR (dossier temporaire)

import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

import requests  # noqa: E402

import termux_runner as R  # noqa: E402
from kelly import app, backup, calib, fair, settle, store, telegram, watch  # noqa: E402
from kelly.sources import http, state  # noqa: E402

UTC = timezone.utc


# ---------- réseau ----------

def test_robots_cause_and_stale_copy():
    os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-cache-")      # jamais le vrai cache des sources
    http.reset()
    proxy = "http://user-GB-20:pw@proxy.example:80"
    http.PROXIES["r.test"] = proxy
    old = http._request
    http._request = lambda url, headers=None: (None, "réseau indisponible (ProxyError, tunnel 502)")
    try:
        ok, why, blocked = http.allowed("https://r.test/a")
        assert not ok and not blocked and "ProxyError" in why, why          # la cause reste : xbet change de session Webshare
        http._write(http._key("https://r.test/robots.txt"), {"fetched_at": 0, "status": 200}, b"User-agent: *\nAllow: /\n")
        http._robots.clear()
        ok, why, blocked = http.allowed("https://r.test/a")
        assert ok, why                                                      # robots.txt périmé mais connu : repris (une coupure ne bloque rien)
    finally:
        http._request = old
        http.PROXIES.pop("r.test", None)
        http.reset()
        os.environ.pop("SOURCES_CACHE_DIR", None)
    print("ok  robots.txt injoignable : la cause réseau est dite (rotation des sessions 1xBet), l'ancienne copie est reprise")


def test_net_wording():
    dns = requests.exceptions.ConnectionError("HTTPSConnectionPool(host='www.fotmob.com'): (Caused by NameResolutionError(\"Failed to resolve\"))")
    w = http.why_net(dns)
    assert w == "adresse introuvable (DNS)" and "proxy" not in w
    err = f"réseau indisponible (ConnectionError, {w})"
    assert not http.cause(err, "FotMob").startswith("proxy absent"), http.cause(err, "FotMob")
    assert http.cause("proxy introuvable (XBET_PROXY) : 365scores n'est jamais lu en direct").startswith("proxy absent")
    assert http.failure_kind("réseau indisponible (ReadTimeout, lecture trop lente)") != "skip"       # un délai de lecture compte (files d'attente)
    print("ok  libellés réseau : une panne DNS n'est plus « proxy absent », un délai de lecture compte comme une panne")


def test_block_rules():
    store.save(state.FILE, {})
    t0 = state.now()
    hrs = lambda n: (datetime.fromisoformat(state.health(n)["blocked_until"]) - t0).total_seconds() / 3600     # noqa: E731
    state.block("fotmob", "HTTP 403")
    state.block("fotmob", "HTTP 403")
    assert hrs("fotmob") <= 24.1                                             # FotMob : secours du suivi, jamais plus de 24 h
    state.block("xbet", "Cloudflare", cloudflare=True)
    assert hrs("xbet") <= 24.1                                               # 1xBet : même Cloudflare, 24 h au plus
    store.save(state.FILE, {"understat": {"blocks": 3}})
    state.block("understat", "HTTP 403")
    state.record_run("understat", 500, [])                                   # des données lues, même si bloquée en fin de passage : compte remis à zéro
    assert state._all()["understat"]["blocks"] == 0
    store.save(state.FILE, {})
    print("ok  blocages : FotMob, 1xBet, 365scores jamais plus de 24 h (même Cloudflare) ; un passage qui a lu des données remet le compte à zéro")


# ---------- suivi, tableau ----------

def test_board_no_double_on_transient_error():
    store.save(watch.FILE, {"board": {"day": str(P.KO.date()), "msg": 101, "hash": "x", "pinned": True}})
    ts = [P.ticket("vm1", [P.leg(0)])]
    sent = []
    old = (telegram.edit_text, telegram.send, telegram.pin)
    telegram.send, telegram.pin = (lambda t, **k: sent.append(t) or 202), (lambda mid: True)
    try:
        with P.tickets(ts):
            telegram.edit_text = lambda mid, text: "fail"                    # Telegram en panne passagère : on attend, jamais un 2e tableau
            watch.board(P.KO - timedelta(minutes=10))
            assert not sent and store.load(watch.FILE, {})["board"]["msg"] == 101
            telegram.edit_text = lambda mid, text: "gone"                    # message effacé : un nouveau
            watch.board(P.KO - timedelta(minutes=5))
            assert len(sent) == 1 and store.load(watch.FILE, {})["board"]["msg"] == 202
    finally:
        telegram.edit_text, telegram.send, telegram.pin = old
    print("ok  tableau du jour : panne passagère de Telegram = on réessaie (jamais deux tableaux épinglés) ; message effacé = un nouveau")


# ---------- règlement, apprentissage ----------

def test_zero_stats_chain():
    b = {"market": "team_fouls_total", "name": "Over", "point": 22.5, "group": "soccer", "home": "A", "away": "B", "id": "m1",
         "commence": "2026-10-10T19:00:00Z", "key": "m1|team_fouls_total||22.5|Over"}
    zero = {"side": {"home": {"fouls": 0}, "away": {"fouls": 0}}}
    good = {"side": {"home": {"fouls": 12}, "away": {"fouls": 13}}}
    old = (settle.fmmatch.match_stats, settle.s365.match_stats, settle.watch_stats)
    fd = settle.FootballData()
    fd.stats = lambda x: good
    settle.fmmatch.match_stats = lambda x: zero                              # FotMob rend 0-0 (stats manquantes)
    settle.s365.match_stats = lambda x: None
    settle.watch_stats = lambda x: None
    try:
        got = settle.match_stats(b, fd, stats_for=lambda x: zero)            # API-Football aussi à 0-0
        assert settle.shot_value(b["market"], got) == 25, got                 # la source suivante (football-data) est essayée
        corners = {"market": "team_corners_home", "name": "Under", "point": 4.5}
        assert settle.zero_stats(corners, {"side": {"home": {"corners": 0}, "away": {"corners": 0}}})
    finally:
        settle.fmmatch.match_stats, settle.s365.match_stats, settle.watch_stats = old
    print("ok  stats à 0 : la source suivante est essayée (plus de pari « non vérifiable » à tort) ; corners d'une équipe protégés aussi")


def test_fair_rerun_keeps_shown():
    store.save(fair.FILE, [])
    base = {"id": "m1", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2030-01-01T19:00:00Z", "market": "alternate_totals_corners"}
    c1 = base | {"key": "m1|c|20.5|Over", "name": "Over", "point": 20.5, "prob": 0.70, "p0": 0.70}
    c2 = base | {"key": "m1|c|22.5|Over", "name": "Over", "point": 22.5, "prob": 0.60, "p0": 0.60}
    for _ in range(2):                                                       # matin refait le même jour (--force, relance du runner)
        fair.record([c1, c2], date(2030, 1, 1), shown=[c1])
    rows = fair.load()
    assert len(rows) == 1 and rows[0]["point"] == 20.5 and rows[0]["prob"] == 0.70, rows
    print("ok  pronostics suivis : un matin refait le même jour ne remplace plus la ligne montrée par une ligne silencieuse")


def test_morning_try_from_runner():
    store.remove(app.TRY_FILE)
    os.environ["KELLY_DAILY_TRY"] = "3"                                      # le runner : 3e et dernier essai (les 2 premiers coupés autrement)
    try:
        again, n = app._scan_failed(date(2030, 1, 1), "HTTPError 503")
        assert not again and n == 3                                          # il part quand même
    finally:
        os.environ.pop("KELLY_DAILY_TRY")
    again, n = app._scan_failed(date(2030, 1, 2), "HTTPError 503")
    assert again and n == 1
    print("ok  matin sans liste des matchs : le dernier essai accordé par le runner part toujours (comptes accordés)")


def test_repair_never_overwrites_newer():
    store.save("calib.json", {"goals": [10, 6, 6.0]})
    store.snapshot("2030-01-01")
    with sqlite3.connect(store.db_path()) as con:
        con.execute("UPDATE docs SET body='{abîmé' WHERE name='calib.json'")
    good = {"goals": [99, 60, 60.0]}
    real = store._db

    class Race:                                                              # un autre processus a réparé et réécrit juste avant
        def __enter__(self):
            with sqlite3.connect(store.db_path()) as c2:
                c2.execute("UPDATE docs SET body=? WHERE name='calib.json'", (json.dumps(good),))
            self.cm = real()
            return self.cm.__enter__()

        def __exit__(self, *a):
            return self.cm.__exit__(*a)
    store._db = lambda: Race()
    try:
        got = store._repair("calib.json")
    finally:
        store._db = real
    assert got == good and store.load("calib.json", {}) == good, (got, store.load("calib.json", {}))
    print("ok  réparation : jamais par-dessus une version plus récente (relu et remis d'un coup)")


def test_combo_reference_kept():
    store.save(calib.COMBO_FILE, {"shots": False, "sot": True})
    lines = calib.combo_changes({"goals": [100, 68, 68.0], "sot": [19, 12, 12.0]})          # tirs et tirs cadrés passés sous 20
    assert lines == [] and store.load(calib.COMBO_FILE, {})["shots"] is False
    assert calib.combo_changes({"shots": [80, 30, 52.0], "goals": [100, 68, 68.0]}) == []   # toujours écartés : jamais ré-annoncés
    print("ok  marchés entrés / sortis : une famille passée sous 20 garde son état (jamais ré-annoncée à tort)")


def test_restore_atomic():
    db = store.db_path()
    store.save("notes.json", {"v": "actuelle"})
    zdir = tempfile.mkdtemp()
    snap = os.path.join(zdir, "kelly.db")
    with sqlite3.connect(db) as src, sqlite3.connect(snap) as dst:
        src.backup(dst)
    real = backup.shutil.copyfile
    backup.shutil.copyfile = lambda a, b: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    try:
        backup.restore(snap)
        raise AssertionError("restauration acceptée sans place")
    except backup.BackupError:
        pass
    finally:
        backup.shutil.copyfile = real
    assert store.load("notes.json", {}) == {"v": "actuelle"} and os.path.getsize(db) > 0       # base actuelle intacte
    assert not os.path.exists(db + ".restauration")
    print("ok  restauration : copie complète d'abord ; disque plein = ta base actuelle reste intacte (le message dit vrai)")


# ---------- runner ----------

def test_runner_review():
    logs = tempfile.mkdtemp(prefix="kelly-logs-")
    real_logs = R.LOGS
    R.LOGS = logs
    try:
        # signal de vie : session Webshare morte -> la suivante, puis retenue
        tried = []

        def post(url, data=None, proxies=None, timeout=None):
            tried.append(proxies["https"])
            if "-20:" in proxies["https"]:
                raise requests.exceptions.ProxyError("Tunnel connection failed: 502 Bad Gateway")
            return type("R", (), {"status_code": 200})()
        sv = R.ServeSupervisor(["x"])
        os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="c")
        try:
            hb = R.Heartbeat("https://hc-ping.com/abc", sv, proxy=lambda: "http://user-GB-20:pw@p.webshare.io:80", post=post)
            assert hb.beat() and tried[-1].endswith("-21:pw@p.webshare.io:80") and hb.last == 21
            tried.clear()
            assert hb.beat() and len(tried) == 1 and "-21:" in tried[0]       # la session qui marche d'abord
        finally:
            os.environ.pop("TELEGRAM_TOKEN"), os.environ.pop("TELEGRAM_CHAT_ID")
        # .env : jamais lu hors de main() ; lu frais quand main() l'a posé
        assert R.ENV_FILE is None and "HC_PING_URL" not in R.env_now()
        env = os.path.join(logs, ".env")
        open(env, "w").write("HC_PING_URL=https://hc-ping.com/x\n")
        R.ENV_FILE = env
        try:
            assert R.env_now()["HC_PING_URL"] == "https://hc-ping.com/x" and "HC_PING_URL" not in os.environ
        finally:
            R.ENV_FILE = None
        # matin raté 2 fois puis 20h : dit ; « rétablie » seulement après 2 min de vie et si l'alerte était partie
        r = R.Runner(serve_cmd=["x"], state=R.new_state(), watch_thread=False)
        said = []
        r.say = lambda t: said.append(t) or True
        r.disk, r.dbcheck = (lambda: 5000.0), (lambda: None)
        now = datetime.now(UTC)
        r.state.update(daily_try_day=str(now.date()), daily_tries=2)
        r.plan = lambda n, s, e: []
        real_now = R.datetime

        class Late(real_now):
            @classmethod
            def now(cls, tz=None):
                return real_now.combine(now.date(), real_now.min.time(), tzinfo=UTC).replace(hour=21)
        R.datetime = Late
        try:
            r._pass()
        finally:
            R.datetime = real_now
        assert said and "n'a pas pu se faire (essais ratés, puis trop tard" in said[-1], said
        clk = [1000.0]
        r.serve.clock = lambda: clk[0]

        class Proc:
            pid = 1

            def __init__(self, rc=None):
                self.rc = rc

            def poll(self):
                return self.rc
        r.serve.proc, r.serve.started = Proc(), clk[0]                       # runner neuf, écoute relancée qui va replanter
        n = len(said)
        r.state["said"]["serve"] = time.time()
        r.check_health()
        assert len(said) == n                                                # pas encore stable : pas de « rétablie »
        clk[0] += R.STABLE_SECS
        r.check_health()
        assert len(said) == n + 1 and "rétablie" in said[-1], said
        r.state["pending"] = {"serve": {"text": "x", "at": time.time()}}
        r.say = lambda t: False                                              # alerte jamais partie (réseau) ...
        r.check_health()
        r.say = lambda t: said.append(t) or True
        n = len(said)
        r.check_health()                                                     # ... l'écoute tient : retirée SANS « rétablie »
        assert "serve" not in r.state["pending"] and len(said) == n, (r.state["pending"], said[n:])
        # alerte restée sans réseau plus de 2 jours : abandonnée ; base occupée : contrôle refait
        r.state["pending"] = {"vieille": {"text": "x", "at": time.time() - 3 * 86400}}
        r.flush_alerts()
        assert "vieille" not in r.state["pending"]
        r.dbcheck = lambda: R.BUSY
        r.state["dbcheck_day"] = ""
        r.check_health(datetime(2030, 1, 1, 5, 0, tzinfo=UTC))
        assert r.state["dbcheck_day"] == ""
    finally:
        R.LOGS = real_logs
    s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "kelly"), encoding="utf-8").read()
    assert "[ -f $L/stopped ] &&" in s and "git merge --abort" in s and "boot.log" in s
    print("ok  runner : signal de vie qui change de session Webshare, .env jamais lu par les tests et relu frais, matin raté puis trop tard dit,"
          " « rétablie » seulement après 2 min, vieilles alertes abandonnées, base occupée recontrôlée ; démarrage qui respecte kelly stop")


def main():
    test_robots_cause_and_stale_copy()
    test_net_wording()
    test_block_rules()
    test_board_no_double_on_transient_error()
    test_zero_stats_chain()
    test_fair_rerun_keeps_shown()
    test_morning_try_from_runner()
    test_repair_never_overwrites_newer()
    test_combo_reference_kept()
    test_restore_atomic()
    test_runner_review()
    print("tests relecture du 13/10 : OK")


if __name__ == "__main__":
    main()
