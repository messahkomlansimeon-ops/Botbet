"""Exploitation : verrou des données partagé avec systemd, messages mis en attente quand le réseau tombe, matin jamais doublé.
Lancer : python -m tests.test_fix_ops"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-ops-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ops-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ops-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_ID", None)
os.environ.pop("XDG_RUNTIME_DIR", None)

from kelly import app, config as C, odds, stats, store, telegram  # noqa: E402

UTC = timezone.utc


class FakeApi:
    """Remplace telegram.api : `mode` = None (tout passe), "net" (réseau coupé), "500", "400" ; enregistre les appels."""

    def __init__(self, updates=()):
        self.calls, self.updates, self.mode, self.n = [], list(updates), None, 100

    def __call__(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        if method == "sendMessage" and self.mode:
            raise {"net": telegram.TelegramError("réseau : coupé"), "500": telegram.TelegramError("Bad Gateway", 502),
                   "400": telegram.TelegramError("Bad Request: chat not found", 400)}[self.mode]
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n}
        return True

    def texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


@contextlib.contextmanager
def telegram_env(fake):
    old = (telegram.api, {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")})
    os.environ.update(TELEGRAM_TOKEN="tok", TELEGRAM_CHAT_ID="42")
    telegram.api = fake
    telegram._last_fail[0] = float("-inf")
    store.save(telegram.OUTBOX, [])
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield fake
    finally:
        telegram.api = old[0]
        for k, v in old[1].items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class Holder:
    """Tient le verrou des données dans un fil (autre open() = autre description de fichier : conflit, comme un autre processus)."""

    def __init__(self, hold=None):
        self.have, self.release, self.hold = threading.Event(), threading.Event(), hold
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with store.data_lock():
            self.have.set()
            self.release.wait(self.hold)

    def __enter__(self):
        self.t.start()
        assert self.have.wait(5)
        return self

    def __exit__(self, *a):
        self.release.set()
        self.t.join(5)


def test_lock():
    assert store.lock_path() == os.path.join(store.DATA_DIR, ".kelly.lock")
    with Holder():
        t0 = time.monotonic()
        try:
            with store.data_lock(wait=0):
                raise AssertionError("verrou pris deux fois")
        except store.LockTimeout:
            pass
        busy = []
        try:
            with store.data_lock(wait=0.5, on_busy=lambda: busy.append(1)):
                raise AssertionError("verrou pris deux fois")
        except store.LockTimeout:
            assert busy == [1] and time.monotonic() - t0 >= 0.5
    with store.data_lock(wait=0):                   # relâché à la sortie
        pass
    # attente puis acquisition une fois libéré
    h = Holder(hold=0.6)
    with h:
        t0 = time.monotonic()
        with store.data_lock(wait=10):
            assert 0.3 < time.monotonic() - t0 < 5
    # l'exception du corps ne ressort pas en LockTimeout et le verrou est relâché
    try:
        with store.data_lock(wait=0):
            raise ValueError("boum")
    except ValueError:
        pass
    with store.data_lock(wait=0):
        pass
    # même fichier que les unités systemd : XDG_RUNTIME_DIR/kelly.lock, et interopérable avec flock(1)
    run = tempfile.mkdtemp(prefix="kelly-run-")
    os.environ["XDG_RUNTIME_DIR"] = run
    try:
        assert store.lock_path() == os.path.join(run, "kelly.lock")
        if shutil.which("flock"):
            with store.data_lock(wait=0):
                assert subprocess.run(["flock", "-n", store.lock_path(), "true"]).returncode == 1
            assert subprocess.run(["flock", "-n", store.lock_path(), "true"]).returncode == 0
            p = subprocess.Popen(["flock", store.lock_path(), "sleep", "1"])
            time.sleep(0.3)
            try:
                with store.data_lock(wait=0):
                    raise AssertionError("flock(1) tient le verrou")
            except store.LockTimeout:
                pass
            p.wait()
            with store.data_lock(wait=0):
                pass
    finally:
        os.environ.pop("XDG_RUNTIME_DIR")
    print("ok  verrou des données (conflit, attente, délai, flock(1))")


def test_serve_lock():
    msg = lambda text, cid=42: {"update_id": 1, "message": {"text": text, "chat": {"id": cid}}}                       # noqa: E731
    cb = {"update_id": 2, "callback_query": {"id": "cb1", "data": "skip|sur|2026-10-09", "message": {"message_id": 7, "chat": {"id": 42}}}}
    handled = []
    handler = lambda text: handled.append(("msg", text, time.monotonic())) or f"re:{text}"                         # noqa: E731
    on_cb = lambda data, mid: handled.append(("cb", data, time.monotonic())) or "fait"                              # noqa: E731
    store.save("telegram.json", {"offset": 0})
    # verrou libre : traité, aucun « ⏳ »
    with telegram_env(FakeApi([msg("/top")])) as fake:
        telegram.poll(handler, on_callback=on_cb, lock=lambda b: store.data_lock(wait=5, on_busy=b))
        assert fake.texts() == ["re:/top"] and handled[0][:2] == ("msg", "/top")
    # verrou pris : accusé du bouton tout de suite, « ⏳ » tout de suite, puis l'action et sa réponse après la libération
    handled.clear()
    with telegram_env(FakeApi([msg("/joue sûr 5"), cb])) as fake:
        h = Holder(hold=1.0)
        released = []
        with h:
            threading.Timer(0.8, lambda: released.append(time.monotonic())).start()
            telegram.poll(handler, on_callback=on_cb, lock=lambda b: store.data_lock(wait=10, on_busy=b))
        assert fake.texts()[0] == telegram.BUSY and fake.texts()[1] == "re:/joue sûr 5", fake.texts()
        assert fake.texts().count(telegram.BUSY) == 1 and fake.texts()[-1] == "fait", fake.texts()      # attente une fois, puis verrou libre
        assert all(t >= released[0] - 0.05 for _, _, t in handled), "l'action attend la libération du verrou"
        order = [m for m, _ in fake.calls]
        assert order.index("answerCallbackQuery") > order.index("sendMessage")     # le 1er message (texte) est traité avant le bouton
        assert "⏳ Kelly est en plein travail" in telegram.BUSY and "dès qu'elle a fini" in telegram.BUSY
        with store.data_lock(wait=0):               # le verrou n'est pas gardé après la passe
            pass
    # bouton pendant que le verrou est pris : acquitté AVANT l'attente (pas de sablier), puis « ⏳ », puis l'action
    handled.clear()
    with telegram_env(FakeApi([cb])) as fake:
        with Holder(hold=0.6):
            telegram.poll(handler, on_callback=on_cb, lock=lambda b: store.data_lock(wait=10, on_busy=b))
        assert [m for m, _ in fake.calls if m != "getUpdates"] == ["answerCallbackQuery", "sendMessage", "sendMessage"]
        assert fake.texts() == [telegram.BUSY, "fait"] and handled[0][:2] == ("cb", "skip|sur|2026-10-09")
    # attente épuisée : l'action n'est pas faite, l'utilisateur est prévenu
    handled.clear()
    with telegram_env(FakeApi([msg("/joue sûr 5")])) as fake:
        with Holder():
            telegram.poll(handler, on_callback=on_cb, lock=lambda b: store.data_lock(wait=0.4, on_busy=b))
        assert fake.texts() == [telegram.BUSY, telegram.TIMEOUT] and not handled and "pas pu être faite" in telegram.TIMEOUT
    # le message « ⏳ » n'est jamais mis en attente dans la file (éphémère) et une erreur du handler est répondue sous le verrou
    with telegram_env(FakeApi([msg("x")])) as fake:
        def boom(text):
            raise RuntimeError("zut")
        telegram.poll(boom, lock=lambda b: store.data_lock(wait=1, on_busy=b))
        assert fake.texts() == ["Erreur : zut"]
        fake.mode = "net"
        telegram.send(telegram.BUSY, queue=False)
        assert store.load(telegram.OUTBOX, []) == []
    print("ok  boucle serveur sous verrou (notice ⏳, attente, délai, accusé immédiat)")


def test_outbox():
    # envoi qui échoue -> en attente, valeur de retour inchangée (False) ; un 400 définitif n'est pas gardé
    with telegram_env(FakeApi()) as fake:
        assert telegram.send("a") == 101 and telegram.send(telegram.Html("<b>b</b>")) == 102
        fake.mode = "net"
        mk = telegram.inline([[("ok", "x|1")]])
        assert telegram.send(telegram.with_buttons(telegram.Html("<b>un</b>"), mk)) is False
        fake.mode = "500"
        assert telegram.send("deux") is False and telegram.send("trois", chat_id="7") is False   # derrière les autres : ordre gardé
        box = store.load(telegram.OUTBOX, [])
        assert [e["text"] for e in box] == ["<b>un</b>", "deux", "trois"] and box[0]["html"] is True and box[1]["html"] is False
        assert box[0]["markup"] == mk and box[2]["chat_id"] == "7" and box[0]["chat_id"] is None
        fake.mode = "400"
        assert telegram.send("mal formé") is False and len(store.load(telegram.OUTBOX, [])) == 3 + 1   # file non vide : ordre d'abord
    with telegram_env(FakeApi()) as fake:
        fake.mode = "400"
        assert telegram.send("mal formé") is False and store.load(telegram.OUTBOX, []) == []
        fake.mode = "net"
        assert telegram.send("x" * 9000) is False       # 3 morceaux, tous gardés
        assert len(store.load(telegram.OUTBOX, [])) == 3
    # 2e morceau en échec : seuls les morceaux non envoyés sont gardés
    with telegram_env(FakeApi()) as fake:
        real = fake.__call__
        n = []

        def flaky(method, **p):
            if method == "sendMessage" and len(n) == 1:
                raise telegram.TelegramError("coupé")
            n.append(1)
            return real(method, **p)
        telegram.api = flaky
        assert telegram.send("y" * 9000) is False and len(store.load(telegram.OUTBOX, [])) == 2
    # flush : dans l'ordre, s'arrête au premier échec, garde le reste ; note de retard sur le premier
    with telegram_env(FakeApi()) as fake:
        old = time.time() - 3600
        mk = telegram.inline([[("ok", "x|1")]])
        store.save(telegram.OUTBOX, [{"text": f"m{i}", "html": i == 0, "markup": mk if i == 0 else None, "chat_id": None, "ts": old + i}
                                     for i in range(4)])
        sent_ok = []
        real = fake.__call__

        def stop_at_third(method, **p):
            if method == "sendMessage" and len(sent_ok) == 2:
                raise telegram.TelegramError("coupé")
            sent_ok.append(1)
            return real(method, **p)
        telegram.api = stop_at_third
        assert telegram.flush_outbox() == (2, 2)
        texts = fake.texts()
        assert texts[0].startswith(telegram.LATE_NOTE) and texts[0].endswith("m0") and texts[1] == "m1", texts
        first = [p for m, p in fake.calls if m == "sendMessage"][0]
        assert first["parse_mode"] == "HTML" and first["reply_markup"] == mk and first["chat_id"] == "42"
        assert [e["text"] for e in store.load(telegram.OUTBOX, [])] == ["m2", "m3"]
        telegram.api = fake
        fake.calls.clear()
        assert telegram.flush_outbox() == (2, 0)
        assert fake.texts()[0].startswith(telegram.LATE_NOTE) and fake.texts()[0].endswith("m2") and fake.texts()[1] == "m3", fake.texts()
        assert store.load(telegram.OUTBOX, []) == [] and telegram.flush_outbox() == (0, 0)
    # trop vieux : abandonné ; refus définitif : abandonné sans bloquer la suite
    with telegram_env(FakeApi()) as fake:
        now = time.time()
        store.save(telegram.OUTBOX, [{"text": "vieux", "html": False, "markup": None, "chat_id": None, "ts": now - (C.OUTBOX_MAX_HOURS + 1) * 3600},
                                     {"text": "frais", "html": False, "markup": None, "chat_id": None, "ts": now - 5}])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert telegram.flush_outbox() == (1, 0)
        assert fake.texts() == ["frais"] and "abandonné" in err.getvalue()
        store.save(telegram.OUTBOX, [{"text": "a", "html": False, "markup": None, "chat_id": None, "ts": now},
                                     {"text": "b", "html": False, "markup": None, "chat_id": None, "ts": now}])
        fake.calls = []
        orig = fake.__call__

        def first_bad(method, **p):
            if method == "sendMessage" and p["text"] == "a":
                raise telegram.TelegramError("Bad Request", 400)
            return orig(method, **p)
        telegram.api = first_bad
        assert telegram.flush_outbox() == (1, 0) and fake.texts() == ["b"]      # « a » refusé pour de bon : « b » part quand même
    # envoi suivant : si le réseau est revenu, la file part d'abord (ordre), sinon le nouveau message attend derrière
    with telegram_env(FakeApi()) as fake:
        fake.mode = "net"
        telegram.send("premier")
        fake.mode = None
        telegram._last_fail[0] = float("-inf")
        fake.calls.clear()
        assert telegram.send("second")
        assert fake.texts()[0].startswith(telegram.LATE_NOTE) is False and fake.texts() == ["premier", "second"]
        fake.mode = "net"
        telegram.send("a")
        fake.mode = None
        telegram._last_fail[0] = time.monotonic()      # tout juste échoué : on n'insiste pas, on garde l'ordre
        assert telegram.send("b") is False
        assert [e["text"] for e in store.load(telegram.OUTBOX, [])] == ["a", "b"]
    # sans identifiants : on affiche, rien n'est mis en attente
    with contextlib.redirect_stdout(io.StringIO()):
        assert telegram.send("local") is True and telegram.flush_outbox()[0] == 0
    print("ok  file d'attente d'envoi (mise en attente, ordre, arrêt au 1er échec, âge, refus définitif)")


def test_serve_flush():
    with telegram_env(FakeApi()) as fake:
        app._flush_outbox_locked()                  # file vide : rien
        assert fake.calls == []
        store.save(telegram.OUTBOX, [{"text": "m", "html": False, "markup": None, "chat_id": None, "ts": time.time()}])
        with Holder():
            app._flush_outbox_locked()              # verrou pris (le matin tourne) : on laisse faire, il renverra lui-même
        assert fake.calls == [] and len(store.load(telegram.OUTBOX, [])) == 1
        app._flush_outbox_locked()
        assert fake.texts() == ["m"] and store.load(telegram.OUTBOX, []) == []
    print("ok  renvoi périodique du serveur sous verrou")


def test_daily_guard():
    for f in ("daily_done.json",):
        if store.exists(f):
            store.remove(f)
    d = lambda y, m, dd, h: datetime(y, m, dd, h, 5, tzinfo=UTC)                                                  # noqa: E731
    assert app.daily_skip_reason(d(2026, 10, 9, 8)) is None                      # jamais tourné
    store.save(app.DAILY_FILE, {"day": "2026-10-09"})
    assert "déjà fait" in app.daily_skip_reason(d(2026, 10, 9, 8))               # 2e passage du jour : doublon
    assert "déjà fait" in app.daily_skip_reason(d(2026, 10, 9, 20))              # relance manuelle
    assert app.daily_skip_reason(d(2026, 10, 10, 8)) is None                     # lendemain 08:00 : normal
    assert app.daily_skip_reason(d(2026, 10, 10, 3)) is not None                 # lendemain 03:00 (rattrapage au démarrage) : la veille court encore
    assert app.daily_skip_reason(d(2026, 10, 10, C.DAY_END_HOUR - 1)) is not None and app.daily_skip_reason(d(2026, 10, 10, C.DAY_END_HOUR)) is None
    assert app.daily_skip_reason(d(2026, 10, 12, 3)) is None                     # veille jamais faite : rattrapage autorisé
    # PC éteint à 08:00 le 10, allumé à 03:00 le 11 : rattrapage, puis le timer de 08:00 est ignoré
    assert app.daily_skip_reason(d(2026, 10, 11, 3)) is None                     # (la veille 10 n'a pas tourné)
    store.save(app.DAILY_FILE, {"day": "2026-10-11"})
    assert app.daily_skip_reason(d(2026, 10, 11, 8)) is not None

    # daily_entry : ignoré (code 0, run_daily non appelé) sauf --force ; code 1 si rien n'a pu partir
    calls, real_run, real_now = [], app.run_daily, app.now_utc
    app.now_utc = lambda: d(2026, 10, 11, 8)
    app.run_daily = lambda sample=False, **k: calls.append(sample) or app.DELIVERY.update(sent=2, failed=0) or []
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert app.daily_entry() == 0 and calls == [] and "ignoré" in err.getvalue() and "--force" in err.getvalue()
        assert app.daily_entry(force=True) == 0 and calls == [False]
        app.now_utc = lambda: d(2026, 10, 12, 8)                                 # nouveau jour : tourne sans --force
        assert app.daily_entry() == 0 and calls == [False, False]
        app.run_daily = lambda sample=False, **k: app.DELIVERY.update(sent=0, failed=3) or []
        assert app.daily_entry() == 1                                            # rien n'est parti : systemd doit le voir
        app.run_daily = lambda sample=False, **k: app.DELIVERY.update(sent=1, failed=2) or []
        assert app.daily_entry() == 0                                            # au moins un message livré
        app.run_daily = lambda sample=False, **k: calls.append(("s", sample)) or app.DELIVERY.update(sent=0, failed=0) or []
        store.save(app.DAILY_FILE, {"day": "2026-10-12"})
        assert app.daily_entry(sample=True) == 0 and calls[-1] == ("s", True)    # --sample ne tient pas compte du garde-fou
    finally:
        app.run_daily, app.now_utc = real_run, real_now
    print("ok  garde-fou du matin (doublon, rattrapage avant 08:00, --force, code de sortie)")


def test_run_daily_wiring():
    """Le vrai run_daily : garde en file les messages quand Telegram est en panne, note la journée faite, renvoie en début de passage."""
    saved = {n: getattr(m, n) for m, n in ((app, "refresh_sources"), (app, "morning_report"), (odds, "scan_today"), (app, "fill_profiles"),
                                           (odds, "fetch_events"), (stats, "enrich"), (app, "world_report"), (app, "run_track"),
                                           (app, "now_utc"))}
    app.refresh_sources = lambda: {}
    app.morning_report = lambda now: (["📋 bilan d'hier"], {})
    odds.scan_today = lambda day, after: {"paid": True}
    app.fill_profiles = lambda day, scan: None
    odds.fetch_events = lambda day, after=None, scan=None: []
    stats.enrich = lambda picks, day: picks
    app.world_report = lambda scan, picks, day, now: (None, [])
    app.run_track = lambda *a, **k: (0, [])
    app.now_utc = lambda: datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    if store.exists(app.DAILY_FILE):
        store.remove(app.DAILY_FILE)
    try:
        with telegram_env(FakeApi()) as fake:
            store.save(telegram.OUTBOX, [{"text": "hier", "html": False, "markup": None, "chat_id": None, "ts": time.time() - 7200}])
            fake.mode = "net"
            with contextlib.redirect_stdout(io.StringIO()):
                assert app.daily_entry() == 1                       # Telegram hors service : tout en attente, code 1
            box = store.load(telegram.OUTBOX, [])
            assert box[0]["text"] == "hier" and len(box) >= 2 and "bilan d'hier" in box[1]["text"], [e["text"][:20] for e in box]
            assert app.DELIVERY["sent"] == 0 and app.DELIVERY["failed"] >= 1
            assert store.load(app.DAILY_FILE, {})["day"] == "2026-10-20"      # journée notée malgré la panne : pas de doublon des mises
            with contextlib.redirect_stderr(io.StringIO()):
                assert app.daily_entry() == 0 and len(store.load(telegram.OUTBOX, [])) == len(box)   # 2e passage ignoré
            fake.mode = None
            telegram._last_fail[0] = float("-inf")
            fake.calls.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                assert app.daily_entry(force=True) == 0
            texts = fake.texts()
            assert texts[0].startswith(telegram.LATE_NOTE) and texts[0].endswith("hier"), texts   # file renvoyée d'abord, avec la note
            assert store.load(telegram.OUTBOX, []) == [] and app.DELIVERY["failed"] == 0
    finally:
        for n, v in saved.items():
            setattr(odds if n in ("scan_today", "fetch_events") else stats if n == "enrich" else app, n, v)
    # run_track renvoie aussi la file au début
    calls = []
    real = telegram.flush_outbox
    telegram.flush_outbox = lambda: calls.append(1) or (0, 0)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            app.run_track(events_for=lambda sk, eid, m: None, now=datetime(2026, 10, 20, 12, tzinfo=UTC))
    finally:
        telegram.flush_outbox = real
    assert calls == [1]
    print("ok  run_daily / run_track : file d'attente renvoyée, journée notée, code de sortie")


def main():
    test_lock()
    test_serve_lock()
    test_outbox()
    test_serve_flush()
    test_daily_guard()
    test_run_daily_wiring()
    print("TOUS LES TESTS D'EXPLOITATION PASSENT")


if __name__ == "__main__":
    main()
