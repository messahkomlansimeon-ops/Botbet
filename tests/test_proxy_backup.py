"""Proxy strict et secours FotMob (hors ligne) : 1xBet et 365scores ne partent jamais en direct hors des tests (rien n'est envoyé sans proxy, même
avec S365_PROXY=0 / XBET_DIRECT=1) ; proxy injoignable : tenu pour en panne 2 min, les requêtes suivantes n'attendent plus ; cause d'un échec en
mots simples ; suivi des matchs : 🟢 coup d'envoi groupé, 365scores muet -> secours FotMob (scores, mi-temps, fin), retour de 365scores sans rien
annoncer deux fois. Lancer : python -m tests.test_proxy_backup"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-proxybk-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

import requests  # noqa: E402

from kelly import config as C, exposure, store, watch  # noqa: E402
from kelly.sources import http, s365, xbet  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KO = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)
DAY = str(KO.date())


def test_never_direct_outside_tests():
    env = {k: v for k, v in os.environ.items() if k != "DATA_DIR"} | {"S365_PROXY": "0", "XBET_DIRECT": "1"}
    out = subprocess.run([sys.executable, "-c", "from kelly import config as C; print(C.SANDBOX, C.S365_PROXY, C.XBET_DIRECT)"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["False", "True", "False"], (out.stdout, out.stderr)       # sur le téléphone : réglages de connexion directe ignorés
    assert {xbet.DOMAIN, s365.HOST} <= http.STRICT
    calls = []
    old = (requests.get, C.SANDBOX)
    requests.get = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("requête directe"))
    C.SANDBOX = False
    try:
        http.PROXIES.pop(s365.HOST, None)
        r = http.fetch(f"https://{s365.HOST}/web/games/?x=1", s365.NAME, 60, check_robots=False)
        assert not r.ok and r.error == http.NO_PROXY and not calls, (r, calls)
        assert http.post(f"https://{xbet.DOMAIN}/service-api/x", {"a": 1}).error == http.NO_PROXY and not calls
    finally:
        requests.get, C.SANDBOX = old
    print("ok  1xBet et 365scores : jamais en direct hors des tests (S365_PROXY=0 / XBET_DIRECT=1 ignorés), rien n'est envoyé sans proxy")


class _Ses:
    def __init__(self, exc, calls):
        self.exc, self.calls = exc, calls

    def get(self, *a, **k):
        self.calls.append(a[0])
        raise self.exc

    def close(self):
        pass


def test_proxy_breaker_and_cause():
    http.reset()
    proxy = "http://user-GB-1:pw@proxy.example:80"
    calls = []
    old = requests.Session
    requests.Session = lambda: _Ses(requests.ConnectTimeout("connect timeout"), calls)
    http.PROXIES["x.test"] = proxy
    try:
        r = http.fetch("https://x.test/a", "test", 60, check_robots=False)
        assert not r.ok and len(calls) == 2, calls                            # 2 échecs de suite (1 minute) : le serveur du proxy est tenu pour en panne
        r = http.fetch("https://x.test/b", "test", 60, check_robots=False)
        assert not r.ok and "proxy injoignable" in r.error and len(calls) == 2, (r, calls)     # tenu pour en panne : rien n'attend
        assert http.cause(r.error) == "le proxy ne répond pas"
        http._proxy_down.clear()
        calls.clear()
        requests.Session = lambda: _Ses(requests.exceptions.ProxyError("Tunnel connection failed: 502 Bad Gateway"), calls)
        http._sessions.clear()
        http.fetch("https://x.test/c", "test", 60, check_robots=False)
        assert len(calls) == 2 and not http._proxy_down, calls                 # session Webshare en panne : une autre peut marcher (xbet), pas le proxy entier
        # 13/10 13h06 : UNE coupure en route ne met plus tout le proxy en panne (les relevés suivants et la rotation des sessions continuent)
        calls.clear()
        requests.Session = lambda: _Ses(requests.exceptions.ProxyError("Unable to connect to proxy', RemoteDisconnected('Remote end closed connection')"), calls)
        http._sessions.clear()
        r = http.fetch("https://x.test/d", "test", 60, check_robots=False)
        assert not http._proxy_down and len(calls) == 2 and "connexion coupée en route" in r.error, (r.error, calls)
        calls.clear()
        requests.Session = lambda: _Ses(requests.exceptions.ProxyError("Unable to connect to proxy', NewConnectionError('Failed to establish a new connection: [Errno 111] Connection refused')"), calls)
        http._sessions.clear()
        http.reset()
        http.PROXIES["x.test"] = proxy
        r = http.fetch("https://x.test/e", "test", 60, check_robots=False)
        assert http._proxy_down and "connexion au proxy impossible" in r.error, r.error        # serveur du proxy refusé 2 fois : en panne 2 min
    finally:
        requests.Session = old
        http.PROXIES.pop("x.test", None)
        http._sessions.clear()
        http.reset()
    old_sb, old_dns = C.SANDBOX, http._dns
    C.SANDBOX, http._dns = False, lambda host: False                          # adresse du proxy introuvable (DNS) : tenu pour en panne sans attendre
    try:
        assert not http.proxy_alive(proxy) and not http.proxy_alive(proxy)
    finally:
        C.SANDBOX, http._dns = old_sb, old_dns
        http.reset()
    assert http.cause("HTTP 504") == "365scores en panne (erreur 504)" and http.cause("HTTP 429 : accès refusé") == "365scores refuse l'accès (erreur 429)"
    assert http.cause("réseau indisponible (ReadTimeout)") == "365scores trop lent (délai dépassé)" and http.cause(http.NO_PROXY).startswith("proxy absent")
    assert http.cause("réseau indisponible (ProxyError)") == "le proxy ne répond pas" and http.cause(None, "FotMob") == "FotMob ne répond pas"
    print("ok  proxy injoignable : tenu pour en panne 2 min (rien n'attend), session Webshare en panne : non ; cause en mots simples")


# ---------- suivi des matchs ----------

def leg(i, point=2.5, sk="soccer_epl"):
    return {"id": f"e{i}", "key": f"e{i}|totals||{point}|Over", "home": f"Home{i}", "away": f"Away{i}", "market": "totals", "name": "Over",
            "point": point, "commence": KO.isoformat(), "pick": f"Plus de {str(point).replace('.', ',')} buts", "prob": 0.55, "sport_key": sk}


def ticket(what, legs, coupon=None):
    return {"what": what, "tid": f"{DAY}|{what}", "name": f"Combi {what}", "stake": 100, "odds": 3.0, "legs": legs} | ({"coupon": coupon} if coupon else {})


GAMES = {i: {"id": i, "start": KO.isoformat(), "home": {"id": 100 + i, "name": f"Home{i}"}, "away": {"id": 200 + i, "name": f"Away{i}"}} for i in range(3)}


@contextlib.contextmanager
def tickets(ts):
    old = exposure.tickets
    exposure.tickets = lambda day: ts if str(day) == DAY else []
    try:
        yield
    finally:
        exposure.tickets = old


def live(score, minute, **kw):
    return {"live": True, "score": score, "minute": minute, "period": 1 if minute <= 45 else 2} | kw


def fm(i, score, minute, **st):
    """Match de la page FotMob : statut comme FotMob l'écrit."""
    status = {"utcTime": KO.isoformat().replace("+00:00", "Z"), "started": True, "finished": False, "cancelled": False,
              "scoreStr": f"{score[0]} - {score[1]}", "liveTime": {"short": f"{minute}’"}} | st
    return {"id": f"fm{90 + i}", "start": KO.isoformat(), "home": {"id": 1, "name": f"Home {i} FC"}, "away": {"id": 2, "name": f"Away{i}"},
            "fm": watch._fm_status(status)}


def run(minutes, status=None, fm_games=None, why="365scores en panne (erreur 504)"):
    def live_for(ids):
        if status is None:
            watch._WHY["live"] = why
        return status
    return watch.follow(KO + timedelta(minutes=minutes), games=GAMES, live_for=live_for, stats_for=lambda g: {}, final_for=lambda g: None,
                        detail_for=lambda g: None, fm_for=lambda: (fm_games or {}, "" if fm_games else "FotMob ne répond pas"))


def test_kickoff_grouped():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0), leg(1, point=1.5)], coupon="AB12"), ticket("vm2", [leg(0)], coupon="CD34")]
    with tickets(ts):
        msg = str(run(6, {0: live((0, 0), 6), 1: live((0, 0), 5)}))
        assert msg.count("🟢 Coup d'envoi") == 2, msg
        assert "🟢 Coup d'envoi · Home0 – Away0\n   ⚽ Plus de 2,5 buts · 🎟️ AB12, 🎟️ CD34" in msg, msg
        assert "🟢 Coup d'envoi · Home1 – Away1\n   ⚽ Plus de 1,5 buts · 🎟️ AB12" in msg, msg
        assert run(16, {0: live((0, 0), 16), 1: live((0, 0), 15)}) is None, "dit une fois"
    store.save(watch.FILE, {})
    with tickets(ts):
        assert "Coup d'envoi" not in str(run(40, {0: live((1, 0), 40), 1: live((0, 0), 40)})), "découvert à la 40e : pas de coup d'envoi"
    print("ok  🟢 coup d'envoi : un bloc par match avec ses paris et leurs coupons, une fois ; match découvert en cours de route : rien")


def test_fotmob_backup():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0)], coupon="AB12")]
    with tickets(ts):
        run(6, {0: live((0, 0), 6)})                                                    # 365scores : coup d'envoi
        msg = str(run(31, None, {"fm90": fm(0, (1, 0), 31)}))                           # 365scores muet : FotMob prend le relais tout de suite
        assert msg.startswith("⚽ Home0 1-0 Away0 (31e)") and "Plus de 2,5 buts : il manque 2 buts" in msg and "panne" not in msg, msg   # 1er passage : pas d'alarme
        msg = str(run(47, None, {"fm90": fm(0, (1, 0), 45, reason={"short": "HT"})}))
        assert msg.startswith("⚠️ <b>365scores en panne</b> : 365scores en panne (erreur 504). Je suis tes matchs par FotMob : scores, mi-temps et fins "
                              "de match. Corners, cartons, tirs et fautes : au règlement du matin.") and "⏸️ Mi-temps · Home0 1-0 Away0" in msg, msg
        assert "365scores en panne" not in str(run(57, None, {"fm90": fm(0, (1, 0), 55)})), "panne dite une fois"
        msg = str(run(60, {0: live((1, 0), 60)}))                                       # 365scores revient : même match, rien de redit
        assert "✅ <b>365scores rétabli</b> (coupé 29 min) : suivi complet de nouveau." in msg and "⚽" not in msg and "Mi-temps" not in msg, msg
        msg = str(run(75, None, {"fm90": fm(0, (3, 1), 75)}))                           # nouvelle panne : FotMob voit 2 buts de plus
        assert "⚽ Home0 3-1 Away0 (75e)" in msg and "✅ Home0 – Away0 : Plus de 2,5 buts (3-1)" in msg, msg
        st = fm(0, (3, 1), 90, finished=True, reason={"short": "FT"})
        msg = str(run(115, None, {"fm90": st}))
        assert "🏁 Terminé · Home0 3-1 Away0" in msg, msg
    # secours impossible : la panne classique, avec la raison
    store.save(watch.FILE, {})
    with tickets(ts):
        run(6, {0: live((0, 0), 6)})
        assert run(31, None, None, why="le proxy ne répond pas") is None                 # panne douce : dite au 2e passage
        msg = str(run(41, None, None, why="le proxy ne répond pas"))
        assert "Suivi des matchs en panne</b> : le proxy ne répond pas ; secours FotMob impossible : FotMob ne répond pas" in msg, msg
    print("ok  365scores muet : secours FotMob (buts, mi-temps, verdicts, fin), panne dite une fois avec sa cause ; retour sans rien redire")


def test_fm_status_and_page():
    s = watch._fm_status
    base = {"utcTime": "2030-01-01T19:00:00Z", "started": True, "finished": False, "cancelled": False, "scoreStr": "2 - 1"}
    assert s(base | {"liveTime": {"short": "45+2’"}})["minute"] == 45 and s(base | {"liveTime": {"short": "67’"}})["period"] == 2
    ht = s(base | {"reason": {"short": "HT"}})
    assert ht["ht"] and ht["live"] and ht["score"] == (2, 1)
    ft = s(base | {"finished": True, "reason": {"short": "FT"}})
    assert ft["ended"] and not ft["live"] and not ft["et"]
    assert s(base | {"finished": True, "reason": {"short": "AET"}})["et"] and s(base | {"finished": True, "reason": {"short": "Pen"}})["et"]
    pp = s(base | {"started": False, "cancelled": True, "scoreStr": None, "reason": {"short": "PP"}})
    assert not pp["live"] and not pp["ended"] and pp["score"] is None
    assert s({"utcTime": "2030-01-01T19:00:00Z", "started": False, "finished": False})["score"] is None
    page = {"props": {"pageProps": {"fixtures": {"allMatches": [
        {"id": 4242, "home": {"id": 1, "name": "Home 0 FC"}, "away": {"id": 2, "name": "Away0"}, "status": base | {"liveTime": {"short": "10’"}}}]}}}}
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(page)}</script></html>'.encode()
    seen = []
    old = http.fetch
    http.fetch = lambda url, src, ttl, **k: seen.append((url, src, ttl)) or http.Result(url, html, 200)
    try:
        games, why = watch._fm_games([(None, leg(0), "k"), (None, leg(5, sk="soccer_xb_nowhere"), "k5")], KO + timedelta(minutes=12))
    finally:
        http.fetch = old
    assert seen == [("https://www.fotmob.com/leagues/47", "fotmob", C.WATCH_FM_TTL_S)] and why == "", (seen, why)    # une page par championnat
    assert games["fm4242"]["fm"]["score"] == (2, 1) and watch.find(leg(0), games)["id"] == "fm4242", games
    print("ok  page de championnat FotMob : statut (direct, mi-temps, fin, prolongation, reporté), une page par championnat, match retrouvé")


def main():
    test_never_direct_outside_tests()
    test_proxy_breaker_and_cause()
    test_kickoff_grouped()
    test_fotmob_backup()
    test_fm_status_and_page()
    print("tests proxy strict et secours FotMob : OK")


if __name__ == "__main__":
    main()
