"""Alertes et visibilité (hors ligne). Runner : alertes Telegram directes, une fois chacune, gardées si le réseau manque (écoute plantée 15 min puis
« rétablie », matin raté 3 fois, matin pas tourné, sauvegarde qui ne part plus, disque presque plein, base abîmée), sans sonnerie aux heures calmes,
signal « en panne » sans clés Telegram. 1xBet : relevé coupé, format suspect (menu vide, cotes vides), 429 = pause d'une heure, proxy qui refuse
(407 / 402). Matin : « ⚠️ Ce matin » (journée inhabituelle, 1xBet coupé, proxy refusé), marchés entrés / sortis des combinés. Status : bloc
« 📱 Kelly sur le téléphone », 1xBet, forfait Webshare, FotMob bloqué = ❌. Lancer : python -m tests.test_infra_alerts"""
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-infra-")
for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "XBET_PROXY", "S365_PROXY", "HC_PING_URL"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

import requests  # noqa: E402

import termux_runner as R  # noqa: E402
from kelly import calib, config as C, dayhealth, status, store, telegram  # noqa: E402
from kelly.sources import http, state, xbet  # noqa: E402

UTC = timezone.utc


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


def runner(logs):
    R.LOGS = logs
    r = R.Runner(serve_cmd=["x"], state=R.new_state(), watch_thread=False)
    clk = Clock()
    r.serve.clock = clk
    said, net = [], {"up": True}
    r.say = lambda text: net["up"] and (said.append(text) or True)
    r.disk, r.dbcheck = (lambda: 5000.0), (lambda: None)
    return r, clk, said, net


def test_runner_alerts():
    logs = tempfile.mkdtemp(prefix="kelly-logs-")
    real_logs = R.LOGS
    try:
        r, clk, said, net = runner(logs)
        now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
        r.serve.proc, r.serve.started = Proc(), clk.t
        r.check_health(now)
        assert not said
        # écoute qui plante en boucle : une alerte après 15 min, une seule, puis « rétablie »
        r.serve.proc.rc = 1
        r.serve.check()
        clk.t += 600
        r.check_health(now)
        assert not said
        clk.t += 400
        r.check_health(now)
        r.check_health(now)
        assert len(said) == 1 and "l'écoute Telegram plante en boucle depuis 17 min" in said[0], said
        r.serve.proc, r.serve.started = Proc(), clk.t
        clk.t += R.STABLE_SECS
        r.serve.check()
        r.check_health(now)
        assert len(said) == 2 and said[1].startswith("✅ Kelly : l'écoute Telegram est rétablie"), said
        # réseau coupé : l'alerte attend (gardée dans runner_state.json) et part au retour
        net["up"] = False
        r._done(["--daily"], now, 124)
        r._done(["--daily"], now, 1)
        r._done(["--daily"], now, 124)
        assert len(said) == 2 and json.load(open(os.path.join(logs, "runner_state.json")))["pending"], "alerte gardée"
        net["up"] = True
        r.check_health(now)
        assert len(said) == 3 and "le passage du matin du 01/01 a échoué 3 fois (trop long, interrompu au bout de 90 min)" in said[2], said
        r._done(["--daily"], now, 124)
        r.check_health(now)
        assert len(said) == 3                                       # une fois par jour
        # sauvegarde Telegram : prévenu au 6e échec de suite
        for i in range(R.BACKUP_FAILS_ALERT):
            r._done(["--backup-auto"], now, 1)
        assert len(said) == 4 and "la sauvegarde sur Telegram ne part plus (6 essais ratés)" in said[3], said
        r._done(["--backup-auto"], now, 0)
        assert r.state["backup_fails"] == 0
        # disque presque plein, puis libéré ; base abîmée (une fois par jour)
        r.disk = lambda: 120.0
        r._disk_at = 0
        r.check_health(now)
        assert "plus que 120 Mo de libre" in said[-1], said
        r.disk, r._disk_at = (lambda: 900.0), 0
        r.check_health(now)
        assert said[-1].startswith("✅ Kelly : place libérée"), said
        r.dbcheck = lambda: "database disk image is malformed"
        n = len(said)
        r.check_health(now + timedelta(days=1))
        r.check_health(now + timedelta(days=1))
        assert len(said) == n + 1 and "base de données est abîmée" in said[-1] and "kelly restaurer" in said[-1], said
        # état gardé après un redémarrage du runner
        st = R.load_state(os.path.join(logs, "runner_state.json"))
        assert "serve" not in st["said"] and any(k.startswith("daily|") for k in st["said"]), st["said"]
    finally:
        R.LOGS = real_logs
    print("ok  runner : alertes Telegram une fois chacune (écoute plantée puis rétablie, matin raté, sauvegarde, disque, base), gardées sans réseau")


def test_runner_helpers():
    assert R.quiet_now(datetime(2030, 1, 1, 23, 30, tzinfo=UTC), env={}) and R.quiet_now(datetime(2030, 1, 1, 6, 0, tzinfo=UTC), env={})
    assert not R.quiet_now(datetime(2030, 1, 1, 12, 0, tzinfo=UTC), env={}) and not R.quiet_now(env={"QUIET_HOURS": "off"})
    sent = []
    post = lambda url, data=None, timeout=None: sent.append((url, data)) or type("R", (), {"status_code": 200})()     # noqa: E731
    assert not R.telegram_say("x", env={}, post=post) and not sent
    assert R.telegram_say("bonjour", env={"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "QUIET_HOURS": "off"}, post=post)
    assert sent[0][0] == "https://api.telegram.org/bott/sendMessage" and sent[0][1]["text"] == "bonjour" and "disable_notification" not in sent[0][1]
    sv = R.ServeSupervisor(["x"], clock=Clock())
    hb = R.Heartbeat("u", sv, proxy=lambda: "http://p:1")
    assert R.ENV_FILE is None and hb.status()[0] and "TELEGRAM_TOKEN" in hb.status()[1]      # tests : jamais le vrai .env ; sans clés : « en panne »
    os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="c")
    try:
        assert not hb.status()[0]
    finally:
        os.environ.pop("TELEGRAM_TOKEN"), os.environ.pop("TELEGRAM_CHAT_ID")
    db = os.path.join(tempfile.mkdtemp(), "kelly.db")
    assert R.db_check(db) is None
    with open(db, "wb") as f:
        f.write(b"SQLite format 3\x00" + b"\x00" * 200)
    assert R.db_check(db)
    print("ok  runner : heures calmes sans sonnerie, envoi direct (jamais l'adresse dans le journal), clés Telegram absentes = « en panne », contrôle de la base")


def test_xbet_cut_and_format():
    store.save(state.FILE, {})
    old = (xbet._Session, xbet.prices)

    class Ses:
        why, url = None, "http://p:1"

        def __init__(self):
            self.n = 0

        def full(self):
            return False

        def call(self, fn, *a, **k):
            return fn(*a, **k)

        def close(self, what):
            pass
    ev = lambda i: ("soccer_epl", {"id": f"e{i}", "home_team": f"H{i}", "away_team": f"A{i}", "xgid": i + 1})      # noqa: E731
    xbet._Session = Ses
    xbet.prices = lambda g, m, h, a: {}                                   # 1xBet répond, mais sans aucune cote
    try:
        xbet.fetch_day([ev(i) for i in range(6)])
        cut = state._all()["xbet"]["cut"]
        assert cut["format"] and cut["read"] == 6 and not cut["why"], cut
        xbet.prices = lambda g, m, h, a: {"k": 2.0}
        xbet.fetch_day([ev(i) for i in range(6)], deadline=time.monotonic() - 1)
        cut = state._all()["xbet"]["cut"]
        assert cut["why"] == "délai dépassé" and cut["read"] == 0 and cut["left"] == 6 and not cut["format"], cut
        xbet.fetch_day([ev(i) for i in range(2)])                          # relevé suivant sans coupure (paris réels, en cache) : la coupure reste
        allx = state._all()["xbet"]
        assert allx["cut"]["why"] == "délai dépassé" and allx["run"]["read"] == 2, allx
        Ses.why = "le proxy ne répond pas : aucun relevé (nouvel essai dans 2 min), jamais en direct"
        xbet.fetch_day([ev(i) for i in range(4)])                          # relevé sauté (proxy, plafond, blocage) : noté aussi
        assert state._all()["xbet"]["cut"]["why"] == "le proxy ne répond pas" and state._all()["xbet"]["cut"]["left"] == 4
    finally:
        xbet._Session, xbet.prices = old
        Ses.why = None
    old_get = xbet._get_json
    xbet._get_json = lambda address, ttl: [{"sports": [{"id": 4, "ligas": []}]}]
    try:
        xbet.menu()
        raise AssertionError("menu vide accepté")
    except xbet.XbetError as e:
        assert xbet.FORMAT in str(e)
    finally:
        xbet._get_json = old_get
    print("ok  1xBet : relevé coupé noté (délai, plafond, panne), format suspect (menu sans championnat, cotes vides)")


def test_proxy_refusal_and_429():
    e407 = requests.exceptions.ProxyError("HTTPSConnectionPool: (Caused by ProxyError('Unable to connect', OSError('Tunnel connection failed: 407 Proxy Authentication Required')))")
    e402 = requests.exceptions.ProxyError("(Caused by ProxyError('x', OSError('Tunnel connection failed: 402 Payment Required')))")
    assert http.proxy_refusal(e407) == "proxy : mot de passe refusé (407)" and http.proxy_refusal(e402) == "proxy : forfait épuisé (402)"
    assert http.proxy_refusal(requests.exceptions.ProxyError("Tunnel connection failed: 502 Bad Gateway")) is None
    assert http.cause("proxy : mot de passe refusé (407)") == "le proxy refuse le mot de passe (vérifie XBET_PROXY)"
    assert http.cause("proxy : forfait épuisé (402)") == "forfait du proxy Webshare épuisé"
    http._proxy_failed("http://u:p@proxy:80", e407)
    assert not http.proxy_alive("http://u:p@proxy:80")                    # tenu en panne : aucune session ne marchera
    http.reset()
    t0 = state.now()
    state.block("xbet", "HTTP 429", hours=state.RATE_HOURS)
    until = datetime.fromisoformat(state.health("xbet")["blocked_until"])
    assert timedelta(minutes=55) < until - t0 <= timedelta(hours=1, minutes=1), until - t0
    state.block("xbet", "HTTP 403")
    assert datetime.fromisoformat(state.health("xbet")["blocked_until"]) - t0 > timedelta(hours=23)
    store.save(state.FILE, {})
    print("ok  proxy : mot de passe refusé (407) / forfait épuisé (402) dits clairement et tenus en panne ; 429 = pause d'une heure (403 : 24 h)")


def test_dayhealth():
    store.save(dayhealth.FILE, {})
    store.save(state.FILE, {})
    day = date(2030, 1, 10)
    normal = {"matches": 120, "leagues": 30, "picks": 15, "pronos": 60, "xbet": 50}
    for i in range(3):
        dayhealth.record(day - timedelta(days=5 - i), normal)
    assert dayhealth.message(day - timedelta(days=2), normal) is None              # 3 matins : pas encore de référence
    assert dayhealth.message(day - timedelta(days=1), normal) is None
    assert dayhealth.message(day, normal | {"matches": 14, "xbet": 3, "pronos": 5}, datetime(2030, 1, 10, 5, 0, tzinfo=UTC)) is None   # trêve : rien
    bad = normal | {"xbet": 3, "picks": 2}
    msg = telegram.plain(dayhealth.message(day, bad, datetime(2030, 1, 10, 5, 0, tzinfo=UTC)))
    assert msg.startswith("⚠️ Ce matin\n• Journée inhabituelle : programme normal mais 3 matchs lus chez 1xBet (d'habitude ~50)."), msg     # paris : jamais
    assert store.load(dayhealth.FILE, {})[str(day)] == bad
    now = datetime.now(UTC)
    state._put("xbet", cut={"at": now.isoformat(), "read": 40, "why": "délai dépassé", "left": 22, "format": False})
    state._put("proxy", refused="proxy : forfait épuisé (402)", refused_at=now.isoformat())
    msg = telegram.plain(dayhealth.message(day + timedelta(days=1), normal, now))
    assert "• Relevé 1xBet coupé (délai dépassé) : 40 matchs lus, 22 pas lus" in msg and "• Le proxy : forfait épuisé : 1xBet et 365scores ne sont plus lus" in msg, msg
    store.save(state.FILE, {})
    print("ok  « ⚠️ Ce matin » : journée inhabituelle (moitié de la médiane), relevé 1xBet coupé, proxy refusé ; rien quand tout va bien")


def test_combo_changes():
    store.save(calib.COMBO_FILE, None)
    store.remove(calib.COMBO_FILE)
    calib._memo.clear()
    good = {"shots": [80, 52, 52.0], "goals": [100, 68, 68.0]}
    assert calib.combo_changes(good) == []                               # premier matin : la référence
    bad = {"shots": [80, 30, 52.0], "goals": [100, 68, 68.0]}
    lines = calib.combo_changes(bad)
    assert len(lines) == 1 and lines[0].startswith("📏 Tirs : retirés des combinés (réussite 38 % pour 65 % annoncés)"), lines
    assert calib.combo_changes(bad) == []
    assert calib.combo_changes(good)[0].startswith("📏 Tirs : de retour dans les combinés (réussite 65 % pour 65 % annoncés)")
    print("ok  marché retiré des combinés ou de retour : une ligne le matin, une fois")


def test_status_phone():
    root = tempfile.mkdtemp(prefix="kelly-root-")
    assert status.phone_lines(root=root) == []                            # PC : pas de runner, pas de bloc
    logs = os.path.join(root, "logs")
    os.makedirs(logs)
    open(os.path.join(logs, "runner.lock"), "w").close()
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    st = R.new_state() | {"watch_at": now.timestamp() - 240, "backup_day": "2029-12-31", "hc_at": now.timestamp() - 120, "pending": {"x": "y"}}
    json.dump(st, open(os.path.join(logs, "runner_state.json"), "w"))
    store.save("daily_done.json", {"day": "2030-01-01", "at": "2030-01-01T04:32:10+00:00"})
    lines = status.phone_lines(now, root=root)
    assert lines[0].startswith("❌ Le programme principal de Kelly est arrêté"), lines          # verrou libre : le runner ne tourne pas
    assert "🌅 Matin fait à 04h32 UTC" in lines and "💾 Sauvegarde Telegram du 31/12" in lines, lines
    assert "✅ Surveillance extérieure : dernier signal il y a 2 min" in lines and "⏳ 1 alerte(s) en attente d'envoi (réseau ?)" in lines, lines
    import fcntl
    with open(os.path.join(logs, "runner.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)                                      # flock propre à ce fichier ouvert : vu comme « tenu » par un autre
        lines = status.phone_lines(now, root=root)
    assert lines[0] == "✅ Kelly tourne · suivi des matchs il y a 4 min", lines
    st |= {"hc_at": now.timestamp() - 3600, "backup_fails": 4}
    json.dump(st, open(os.path.join(logs, "runner_state.json"), "w"))
    lines = status.phone_lines(now, root=root)
    assert "❌ Surveillance extérieure : dernier signal il y a 60 min (proxy en panne ?)" in lines and any("4 essais ratés" in x for x in lines), lines
    # 1xBet, FotMob bloqué, forfait Webshare
    store.save(state.FILE, {"xbet": {"ok": True, "rows": 38, "last_success": "2030-01-01T04:31:00+00:00",
                                     "cut": {"at": "2030-01-01T10:31:00+00:00", "why": "délai dépassé", "read": 38, "left": 24, "format": False}},
                            "fotmob": {"ok": False, "blocked_until": "2099-01-01T00:00:00+00:00", "last_error": "FotMob bloque les robots (Cloudflare)"}})
    xl = status.xbet_lines(now)
    assert xl == ["✅ 1xBet · 38 matchs lus · 04h31", "⚠️ 1xBet : relevé coupé à 10h31 UTC (délai dépassé), 38 lus, 24 pas lus"], xl
    assert any(x.startswith("❌ FotMob : FotMob bloque les robots") for x in status.source_lines(now)), status.source_lines(now)
    assert any(x.startswith(("✅", "⚠️")) and "Forfait Webshare (Kelly)" in x for x in status.api_lines()), status.api_lines()
    store.save(state.FILE, {})
    print("ok  status : bloc « 📱 Kelly sur le téléphone » (programme, matin, sauvegarde, surveillance, alertes en attente), 1xBet, FotMob bloqué = ❌,"
          " forfait Webshare")


def main():
    test_runner_alerts()
    test_runner_helpers()
    test_xbet_cut_and_format()
    test_proxy_refusal_and_429()
    test_dayhealth()
    test_combo_changes()
    test_status_phone()
    print("tests alertes et visibilité : OK")


if __name__ == "__main__":
    main()
