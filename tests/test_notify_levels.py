"""Niveaux de bruit (hors ligne) : un message marqué silencieux part avec disable_notification (message, pages suivantes, boutons, file d'attente du réseau, heures calmes) ;
les réponses aux demandes, les ⏳, le bilan du matin et l'état partent sans sonnerie ; les combinés du matin, ⚠️, 💰, ⏰, 🎉 et le suivi des matchs sonnent ; NOTIFY_LEVELS=0 rétablit tout.
Lancer : python -m tests.test_notify_levels"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-notify-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-notify-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import app, config as C, quiet, store, telegram, xalerts  # noqa: E402

SENT = []
FAIL = [False]
REAL = (telegram.api, telegram.creds, quiet.window)


def fake_api(method, http_timeout=40, **kw):
    if method == "sendMessage":
        if FAIL[0]:
            raise telegram.TelegramError("réseau : coupé", None)
        SENT.append(kw)
        return {"message_id": 7}
    return True


def setup():
    telegram.api = fake_api
    telegram.creds = lambda: ("T", "42")
    SENT.clear()
    FAIL[0] = False
    store.save(telegram.OUTBOX, [])


def test_flag_and_pages():
    setup()
    telegram.send(telegram.Html("sonne"))
    telegram.send(telegram.Html("muet"), silent=True)
    m = telegram.Html("page 1")
    m.more = [telegram.Html("page 2")]
    telegram.send(m, silent=True)
    telegram.send(telegram.with_buttons(telegram.silent(telegram.Html("boutons")), telegram.inline([[("a", "b")]])))
    got = [(k["text"], bool(k.get("disable_notification"))) for k in SENT]
    assert got == [("sonne", False), ("muet", True), ("page 1", True), ("page 2", True), ("boutons", True)], got
    assert isinstance(telegram.silent("brut"), telegram.Text) and telegram.silent("brut").silent
    C.NOTIFY_LEVELS = False
    try:
        SENT.clear()
        telegram.send(telegram.Html("muet"), silent=True)
        assert not SENT[0].get("disable_notification"), "NOTIFY_LEVELS=0 : tout sonne comme avant"
    finally:
        C.NOTIFY_LEVELS = True
    print("ok  silent() : message, pages suivantes, boutons ; NOTIFY_LEVELS=0 rétablit la sonnerie")


def test_outbox_and_quiet_hours():
    setup()
    FAIL[0] = True
    telegram.send(telegram.Html("muet en attente"), silent=True)
    telegram.send(telegram.Html("sonne en attente"))
    box = store.load(telegram.OUTBOX, [])
    assert [bool(e.get("silent")) for e in box] == [True, False], box
    FAIL[0] = False
    telegram.flush_outbox()
    assert [(k["text"], bool(k.get("disable_notification"))) for k in SENT] == [("muet en attente", True), ("sonne en attente", False)], SENT
    # heures calmes : le niveau est gardé avec l'alerte
    setup()
    night = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)
    store.save(quiet.FILE, [])
    quiet.window = lambda: (22, 7)
    quiet.send(telegram.silent(telegram.Html("alerte muette")), night, kind="autre")
    quiet.send(telegram.Html("alerte qui sonne"), night, kind="autre")
    held = store.load(quiet.FILE, [])
    assert [bool(h.get("silent")) for h in held] == [True, False], held
    quiet.flush(datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
    by_text = {k["text"]: bool(k.get("disable_notification")) for k in SENT}
    assert by_text["alerte muette"] is True and by_text["alerte qui sonne"] is False, by_text
    print("ok  file d'attente du réseau et heures calmes : le niveau de bruit suit le message")


def test_replies_silent():
    setup()
    telegram._run(lambda t: "réponse à ta demande", "x")
    telegram._run(lambda t: telegram.Html("réponse HTML"), "x")
    assert all(k.get("disable_notification") for k in SENT) and len(SENT) == 2, SENT
    print("ok  réponses aux demandes et aux boutons : sans sonnerie")


def item(kind, key="k"):
    return {"type": kind, "key": key + kind, "rank": 1.0, "text": f"alerte {kind}", "coupon": None, "buttons": [], "odds": 2.0}


def test_who_rings():
    """Qui sonne et qui se tait, par le comportement : alertes 1xBet (seuls 📈 / ✅ se taisent), matin (bilan silencieux, combinés et 🛑 qui sonnent), file d'attente."""
    setup()
    held, real_send = [], quiet.send
    quiet.send = lambda msg, now=None, kind="autre": held.append(bool(getattr(msg, "silent", False)))
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    try:
        for i, (kinds, expect_silent) in enumerate((((("rise",)), True), (("good",), True), (("rise", "good"), True), (("danger",), False), (("lag",), False),
                                                    (("rise", "danger"), False))):
            held.clear()
            store.save(xalerts.FILE, {"day": "2026-10-1%d" % i, "items": {}})
            xalerts.send([item(k, f"k{i}") for k in kinds], f"2026-10-1{i}", now)       # (les écarts sharp 💰 gap passent par gapbets.message, non silencieux : RING)
            assert held == [expect_silent], (kinds, held)
    finally:
        quiet.send = real_send
    # matin : bilan silencieux, 🛑 et combinés qui sonnent
    setup()
    cut = telegram.Html("🛑 voie coupée")
    cut.loud = True
    report = [telegram.Html("📊 bilan"), cut]
    combos = [telegram.Html("🎯 combiné du matin")]
    app.deliver_morning([m if getattr(m, "loud", False) else telegram.silent(m) for m in report] + combos, "2026-10-15", now, sample=True)
    assert [(k["text"], bool(k.get("disable_notification"))) for k in SENT] == [("📊 bilan", True), ("🛑 voie coupée", False), ("🎯 combiné du matin", False)], SENT
    kd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kelly")
    src = "".join(open(os.path.join(kd, f), encoding="utf-8").read() for f in ("app.py", "cmd_custom.py", "commands.py", "run_refresh.py", "run_track.py", "analysis.py", "cmd_season.py", "run_check.py"))      # app.py et les modules de son découpage
    assert 'm if getattr(m, "loud", False) else telegram.silent(m)' in src, "app.run envoie le bilan comme ce test"
    assert src.count("queue=False, silent=True") >= 10, "les messages ⏳ sont sans sonnerie"
    print("ok  qui sonne : ⚠️, 💰, écarts, 🛑, combinés du matin ; qui se tait : 📈, ✅ seuls, bilan, ⏳, annonces")


def main():
    try:
        test_flag_and_pages()
        test_outbox_and_quiet_hours()
        test_replies_silent()
        test_who_rings()
    finally:
        telegram.api, telegram.creds, quiet.window = REAL
    print("tests niveaux de bruit : OK")


if __name__ == "__main__":
    main()
