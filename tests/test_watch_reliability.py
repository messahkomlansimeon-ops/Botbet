"""Suivi des matchs, fiabilité (hors ligne) : verdicts des tickets rangés par identifiant daté (le combi m1 d'aujourd'hui n'hérite plus de celui d'hier),
migration de l'ancien document ; jamais de panne silencieuse (365scores bloqué, proxy absent : averti tout de suite ; pas de réponse : au 2e passage ;
une seule fois, puis « rétabli ») ; un pari en erreur n'arrête pas les autres ; document illisible : rien envoyé ; lignes gardées tant qu'elles ne sont
pas parties ; paris non suivis dits une fois ; but annulé (VAR) et décision corrigée ; nuit : un seul résumé, le suivi jamais jeté ; file des messages
sûre à deux processus. Lancer : python -m tests.test_watch_reliability"""
import contextlib
import multiprocessing
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-watchrel-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import config as C, exposure, quiet, store, telegram, watch  # noqa: E402
from kelly.sources import s365, state as src_state  # noqa: E402

KO = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)
DAY = str(KO.date())


def leg(i, market="totals", name="Over", point=1.5, home=None, away=None):
    h, a = home or f"H{i}", away or f"A{i}"
    return {"id": f"e{i}", "key": f"e{i}|{market}||{point}|{name}", "home": h, "away": a, "market": market, "name": name, "point": point,
            "commence": KO.isoformat(), "pick": f"{market} {name} {point}"}


def ticket(what, legs, day=DAY, stake=100, odds=3.0):
    return {"what": what, "tid": f"{day}|{what}", "name": f"Combi {what}", "stake": stake, "odds": odds, "legs": legs}


GAMES = {i: {"id": i, "start": KO.isoformat(), "home": {"id": 100 + i, "name": f"H{i}"}, "away": {"id": 200 + i, "name": f"A{i}"}} for i in range(4)}


@contextlib.contextmanager
def tickets(ts):
    """exposure.tickets rend `ts` pour le jour du match (et rien la veille)."""
    old = exposure.tickets
    exposure.tickets = lambda day: ts if str(day) == DAY else []
    try:
        yield
    finally:
        exposure.tickets = old


def reset(doc=None):
    store.save(watch.FILE, doc or {})


def kw(live, stats=None):
    return {"games": GAMES, "live_for": lambda ids: live, "stats_for": lambda g: stats or {}, "final_for": lambda g: None}


def test_tid_no_inheritance():
    reset({"tickets": {"vm1": "lost", f"{str(KO.date() - timedelta(days=1))}|vm1": "won"}})       # ancien nom sans date + le vm1 d'hier
    ts = [ticket("vm1", [leg(0), leg(1)]), ticket("pari", [leg(2)]) | {"tid": f"{DAY}|pari|{leg(2)['key']}"},
          ticket("pari", [leg(3)]) | {"tid": f"{DAY}|pari|{leg(3)['key']}"}]
    live = {0: {"live": True, "score": (0, 0), "minute": 20}, 1: {"live": True, "score": (0, 0), "minute": 20},
            2: {"live": True, "score": (0, 0), "minute": 20}, 3: {"live": True, "score": (0, 0), "minute": 20}}
    with tickets(ts):
        assert watch.follow(KO + timedelta(minutes=20), **kw(live)) is None
        live[2] = {"live": True, "score": (2, 0), "minute": 50}
        out = watch.follow(KO + timedelta(minutes=50), **kw(live))
        msg, all_ = str(out), watch.all_text(out)
    d = store.load(watch.FILE, {})
    assert "vm1" not in d["tickets"] and d["v"] == 2, d["tickets"]                       # ancien verdict sans date oublié (migration)
    assert "✅ H2 – A2" in msg and "déjà perdu" not in msg and "COUPON GAGNÉ" in all_, all_     # le vm1 d'hier gagné ne cache pas celui du jour
    assert d["tickets"] == {f"{DAY}|pari|{leg(2)['key']}": "won", f"{str(KO.date() - timedelta(days=1))}|vm1": "won"}, d["tickets"]   # l'autre pari seul : à part
    print("ok  verdicts par identifiant daté : le m1 du jour n'hérite pas de celui d'hier, deux paris seuls ne se confondent pas")


def test_never_silent():
    reset()
    ts = [ticket("vm1", [leg(0)])]
    now = KO + timedelta(minutes=30)
    with tickets(ts):
        src_state.block("s365", "HTTP 429")
        try:
            msg = str(watch.follow(now))
            assert "⚠️ <b>Suivi des matchs en panne</b>" in msg and "bloqué jusqu'au" in msg and "1 match de tes tickets n" in msg, msg
            assert watch.follow(now + timedelta(minutes=10)) is None, "une seule fois par panne"
        finally:
            src_state._put("s365", blocked_until=None)
        live = {0: {"live": True, "score": (0, 0), "minute": 40}}
        msg = str(watch.follow(now + timedelta(minutes=20), **kw(live)))
        assert "✅ <b>Suivi des matchs rétabli</b> (coupé 20 min)" in msg, msg
        assert watch.follow(now + timedelta(minutes=30), **kw(live)) is None
        # proxy absent : panne dure, dite tout de suite
        old = s365._via_proxy
        s365._via_proxy = contextlib.contextmanager(lambda settle=False: (yield "proxy introuvable (XBET_PROXY)"))
        try:
            assert "proxy introuvable" in str(watch.follow(now + timedelta(minutes=40)))
        finally:
            s365._via_proxy = old
        reset()
        # pas de réponse : dit au 2e passage seulement
        assert watch.follow(now, **kw(None) | {"live_for": lambda ids: None}) is None
        msg = str(watch.follow(now + timedelta(minutes=10), **kw(None) | {"live_for": lambda ids: None}))
        assert "365scores ne répond pas" in msg, msg
    reset()
    with tickets([]):                                    # rien à suivre : jamais d'avertissement
        src_state.block("s365", "HTTP 429")
        try:
            assert watch.follow(now) is None
        finally:
            src_state._put("s365", blocked_until=None)
    print("ok  jamais silencieux : 365scores bloqué / proxy absent dits tout de suite, pas de réponse au 2e passage, une fois, puis « rétabli »")


def test_stale_is_no_data():
    from kelly.sources import http
    old = http.fetch
    http.fetch = lambda *a, **k: http.Result("u", b'{"games": []}', 200, from_cache=True, stale=True)
    try:
        assert watch._live(s365, {1}, KO) is None and watch._stats(s365, GAMES[0]) == {} and watch._final(s365, GAMES[0]) is None
    finally:
        http.fetch = old
    print("ok  vieille copie (après une erreur) ou source bloquée : aucune donnée, jamais un score périmé")


def test_one_bad_leg():
    reset()
    ts = [ticket("vm1", [leg(0), leg(1)])]
    live = {0: {"live": True, "score": (0, 0), "minute": 20}, 1: {"live": True, "score": (0, 0), "minute": 20}}
    real = watch.state

    def boom(lg, *a):
        if lg["home"] == "H0":
            raise ValueError("pari abîmé")
        return real(lg, *a)
    with tickets(ts):
        watch.follow(KO + timedelta(minutes=20), **kw(live))
        live[1] = {"live": True, "score": (2, 0), "minute": 50}
        watch.state = boom
        try:
            msg = str(watch.follow(KO + timedelta(minutes=50), **kw(live)))
        finally:
            watch.state = real
    assert "✅ H1 – A1" in msg, msg
    print("ok  un pari en erreur n'empêche pas les autres matchs")


def test_unreadable_doc():
    reset()
    old = store.load

    def bad(name, default, strict=False):
        if name == watch.FILE:
            if strict:
                raise OSError("database is locked")
            return default
        return old(name, default, strict)
    sent = []
    store.load, old_send = bad, telegram.send
    telegram.send = lambda *a, **k: sent.append(a) or True
    try:
        try:
            watch.follow(KO)
            raise AssertionError("WatchStateError attendue")
        except watch.WatchStateError:
            pass
        watch._tickets(KO)
    finally:
        store.load, telegram.send = old, old_send
    assert sent == [], "document illisible : rien envoyé (sinon tous les ✅ / ❌ déjà dits repartiraient)"
    print("ok  document du suivi illisible : aucun message")


def test_lines_kept_until_sent():
    reset()
    real = watch._follow

    def fake(d, now, *a, **k):
        watch._out(d, now, "ev", "⚽ H0 1-0 A0 (10e)")
    watch._follow = fake
    old_send = telegram.send                       # le suivi des matchs envoie directement (il ignore les heures calmes : C.WATCH_QUIET éteint)
    try:
        telegram.send = lambda *a, **k: False
        telegram.LAST_QUEUED = False
        watch._tickets(KO)
        assert [o["text"] for o in store.load(watch.FILE, {})["out"]] == ["⚽ H0 1-0 A0 (10e)"], "envoi raté : gardé pour le passage suivant"
        got = []
        telegram.send = lambda msg, *a, **k: got.append(str(msg)) or True
        watch._tickets(KO + timedelta(minutes=10))
        assert got == ["⚽ H0 1-0 A0 (10e)"] and store.load(watch.FILE, {})["out"] == [], got
    finally:
        watch._follow, telegram.send = real, old_send
    print("ok  lignes gardées tant qu'elles ne sont pas parties (ni envoyées ni mises en attente)")


def test_unfollowed_once():
    reset()
    sp = leg(0, market="spreads", name="H0", point=-1.5)
    ts = [ticket("vm1", [sp, leg(1)])]
    live = {1: {"live": True, "score": (0, 0), "minute": 20}}
    with tickets(ts):
        msg = str(watch.follow(KO + timedelta(minutes=20), **kw(live)))
        assert "👁️ Pas suivi en direct (verdict au règlement du matin) : H0 – A0 (spreads H0 -1.5) : marché non suivi en direct" in msg, msg
        assert watch.follow(KO + timedelta(minutes=30), **kw(live)) is None, "dit une fois"
    print("ok  pari non suivi en direct : dit une fois, avec la raison")


def test_var_and_correction():
    reset()
    ts = [ticket("vm1", [leg(0)])]
    live = {0: {"live": True, "score": (1, 0), "minute": 30}}
    with tickets(ts):
        watch.follow(KO + timedelta(minutes=30), **kw(live))
        live[0] = {"live": True, "score": (2, 0), "minute": 60}
        msg = watch.all_text(watch.follow(KO + timedelta(minutes=60), **kw(live)))
        assert "✅ H0 – A0" in msg and "COUPON GAGNÉ" in msg, msg
        assert store.load(watch.FILE, {})["legs"][f"vm1|{leg(0)['key']}"]["prov"] == 1, "décidé en cours de match : provisoire"
        live[0] = {"live": True, "score": (1, 0), "minute": 63}        # but annulé par la VAR
        msg = str(watch.follow(KO + timedelta(minutes=70), **kw(live)))
        assert "↩️ But annulé : H0 1-0 A0 (63e)" in msg and "⚠️ Corrigé : H0 – A0 : totals Over 1.5 : de nouveau en jeu (1-0)" in msg, msg
        assert "⚠️ Ticket corrigé : de nouveau en jeu" in msg, msg
        live[0] = {"live": False, "ended": True, "score": (1, 0), "minute": 90}
        msg = str(watch.follow(KO + timedelta(minutes=115), **kw(live)))
        assert "❌ H0 – A0" in msg and "❌ Ticket perdu." in msg, msg
    print("ok  but annulé (VAR) : dit, pari et ticket corrigés ; verdict final juste")


def test_quiet_digest():
    old_hours, old_max = C.QUIET_HOURS, quiet.QUIET_MAX
    C.QUIET_HOURS, quiet.QUIET_MAX = "23-7", 2
    store.save(quiet.FILE, [])
    sent = []
    old = telegram.send
    telegram.send = lambda text, *a, **k: sent.append(str(text)) or True
    try:
        night = KO.replace(hour=2)
        quiet.send(telegram.Html("⚽ H0 1-0 A0"), night, kind="suivi")
        for i in range(3):
            quiet.send(f"📈 alerte {i}", night + timedelta(minutes=i))
        quiet.send(telegram.Html("✅ H1 – A1"), night.replace(hour=3, minute=10), kind="suivi")
        held = store.load(quiet.FILE, [])
        assert [h["kind"] for h in held] == ["suivi", "autre", "autre", "suivi"] and held[0]["at"].startswith("2030-01-01T02:00"), held
        assert quiet.flush(KO.replace(hour=7)) == 4
        assert sent[0] == "🌙 <b>Tes matchs pendant la nuit</b>\n\n<b>02h00</b>\n⚽ H0 1-0 A0\n\n<b>03h10</b>\n✅ H1 – A1", sent[0]
        assert sent[1].startswith("🌙 2 alertes de la nuit") and sent[2:] == ["📈 alerte 1", "📈 alerte 2"], sent
    finally:
        telegram.send, C.QUIET_HOURS, quiet.QUIET_MAX = old, old_hours, old_max
    print("ok  nuit : horodatées, suivi en un seul résumé et jamais jeté, autres alertes plafonnées")


def _append(n):
    for i in range(n):
        store.update(telegram.OUTBOX, lambda box, i=i: ((box or []) + [{"text": f"{os.getpid()}-{i}", "ts": 0}], None), [])


def test_outbox_two_processes():
    store.save(telegram.OUTBOX, [])
    ctx = multiprocessing.get_context("fork")         # même dossier de données (Python 3.14 démarre autrement par défaut : le module serait relu)
    ps = [ctx.Process(target=_append, args=(100,)) for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
    assert len(store.load(telegram.OUTBOX, [])) == 200, len(store.load(telegram.OUTBOX, []))
    # le renvoi ne retire que ce qu'il a envoyé : une entrée ajoutée pendant le renvoi reste
    store.save(telegram.OUTBOX, [{"text": "a", "html": False, "markup": None, "chat_id": None, "ts": 9e12}])
    os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
    old = telegram._deliver

    def deliver(text, *a):
        store.update(telegram.OUTBOX, lambda box: ((box or []) + [{"text": "b", "html": False, "markup": None, "chat_id": None, "ts": 9e12}], None), [])
        return {"message_id": 1}
    telegram._deliver = deliver
    try:
        assert telegram.flush_outbox() == (1, 1)
    finally:
        telegram._deliver = old
        os.environ.pop("TELEGRAM_TOKEN"), os.environ.pop("TELEGRAM_CHAT_ID")
    assert [e["text"] for e in store.load(telegram.OUTBOX, [])] == ["b"]
    store.save(telegram.OUTBOX, [])
    print("ok  file des messages : deux processus ajoutent sans s'écraser ; le renvoi ne retire que ce qu'il a envoyé")


def test_status_lines():
    from kelly import status
    now = KO + timedelta(minutes=50)
    reset({"health": {"at": (now - timedelta(minutes=5)).isoformat(timespec="minutes"), "ok": True, "open": 3}})
    assert status.watch_lines(now) == ["✅ Actif · 3 matchs à suivre · dernier passage 19h45 UTC"], status.watch_lines(now)
    reset({"health": {"at": (now - timedelta(minutes=47)).isoformat(timespec="minutes"), "ok": True, "open": 2}})
    assert status.watch_lines(now)[0] == "❌ Dernier passage il y a 47 min · 2 matchs à suivre"
    reset({"health": {"at": now.isoformat(timespec="minutes"), "ok": False, "why": "365scores ne répond pas", "since": "2030-01-01T19:20", "open": 1},
           "out": [{"at": "x", "k": "ev", "text": "t"}]})
    out = status.watch_lines(now)
    assert out[0] == "❌ En panne depuis 19h20 UTC : 365scores ne répond pas · 1 match à suivre" and out[-1] == "⏳ 1 ligne(s) en attente d'envoi", out
    reset()
    assert status.watch_lines(now) == []
    print("ok  status : bloc « 🎫 Suivi des matchs » (actif, en panne, passage trop vieux, lignes en attente)")


def test_coupon_on_each_bet():
    reset({"tickets": {f"{DAY}|vm5": "lost"}})
    ts = [ticket(f"vm{i}", [leg(0)]) | {"coupon": f"CODE{i}"} for i in range(1, 6)]
    live = {0: {"live": True, "score": (0, 0), "minute": 20}}
    with tickets(ts):
        watch.follow(KO + timedelta(minutes=20), **kw(live))
        live[0] = {"live": True, "score": (2, 0), "minute": 50}
        out = watch.follow(KO + timedelta(minutes=50), **kw(live))
        msg = watch.all_text(out)
    assert "✅ H0 – A0 : totals Over 1.5 (2-0) · 🎟️ CODE1, 🎟️ CODE2, 🎟️ CODE3 +2" in msg, msg        # en jeu d'abord, au plus 3, puis +N
    assert "🎫 <b>Combi vm1</b> · 🎟️ CODE1" in msg and msg.count("COUPON GAGNÉ") == 4, msg           # 4 coupons gagnés : 4 messages festifs
    assert [b["text"] for row in out.markup["inline_keyboard"] for b in row] == [f"🎫 CODE{i}" for i in range(1, 6)], out.markup      # boutons des coupons
    assert watch._from(leg(0)["key"], {leg(0)["key"]: [("🎟️ A", True), ("🎟️ B", False)]}) == " · 🎟️ A, 🎟️ B (perdu)"
    print("ok  chaque pari dit de quel(s) coupon(s) il vient (code 1xBet), le perdu marqué")


def test_follow_ignores_quiet_hours():
    old_hours = C.QUIET_HOURS
    C.QUIET_HOURS = "23-7"
    night = KO.replace(hour=2)
    store.save(quiet.FILE, [{"text": "⚽ gardé avant", "html": True, "markup": None, "kind": "suivi", "at": night.isoformat()},
                            {"text": "📈 autre alerte", "html": False, "markup": None, "kind": "autre", "at": night.isoformat()}])
    reset()
    real, sent, old_send = watch._follow, [], telegram.send

    def fake(d, now, *a, **k):
        watch._out(d, now, "ev", "⚽ H0 1-0 A0 (10e)")
    watch._follow, telegram.send = fake, (lambda text, *a, **k: sent.append(str(text)) or True)
    try:
        watch.tick(night)
    finally:
        watch._follow, telegram.send, C.QUIET_HOURS = real, old_send, old_hours
    assert any("Tes matchs pendant la nuit" in x and "⚽ gardé avant" in x for x in sent), sent          # le suivi gardé part tout de suite
    assert "⚽ H0 1-0 A0 (10e)" in sent, sent                                                           # le suivi du passage : envoyé la nuit
    assert [h["text"] for h in store.load(quiet.FILE, [])] == ["📈 autre alerte"], "les autres alertes attendent le réveil"
    assert store.load(watch.FILE, {})["out"] == []
    store.save(quiet.FILE, [])
    print("ok  le suivi des matchs ignore les heures calmes (même ce qui était gardé) ; les autres alertes attendent le réveil")


def main():
    test_tid_no_inheritance()
    test_never_silent()
    test_stale_is_no_data()
    test_one_bad_leg()
    test_unreadable_doc()
    test_lines_kept_until_sent()
    test_unfollowed_once()
    test_var_and_correction()
    test_quiet_digest()
    test_outbox_two_processes()
    test_status_lines()
    test_coupon_on_each_bet()
    test_follow_ignores_quiet_hours()
    print("tests fiabilité du suivi des matchs : OK")


if __name__ == "__main__":
    main()
