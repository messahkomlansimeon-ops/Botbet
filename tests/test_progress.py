"""Progression du passage du matin (kelly/progress.py) : pourcentage pondéré par la durée des étapes, fin estimée, durées apprises d'un passage
à l'autre, passage interrompu, STATUS= systemd, « état » répondu sans le verrou des données. Lancer : python -m tests.test_progress"""
import os
import socket
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-progress-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("NOTIFY_SOCKET", None)

from kelly import chat, progress, store, telegram  # noqa: E402

T0 = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)


def _state(step, spent_s, weights=None):
    return {"kind": "daily", "pid": os.getpid(), "started": T0.isoformat(), "step": step,
            "step_at": (T0 + timedelta(seconds=600)).isoformat(), "durations": {},
            "weights": weights or {k: 100 for k, _, _ in progress.STEPS}} | {"_spent": spent_s}


def test_pct():
    st = _state("monde", 0)                       # 3e étape sur 12, poids égaux : 2/12 fait
    now = T0 + timedelta(seconds=600)
    pct, eta = progress._pct(st, now)
    assert pct == 16 and eta == now + timedelta(seconds=1000), (pct, eta)
    pct, _ = progress._pct(st, now + timedelta(seconds=50))
    assert pct == 20, pct                         # moitié de l'étape en cours
    pct, eta = progress._pct(st, now + timedelta(seconds=5000))
    assert pct == 24 and eta == now + timedelta(seconds=5000 + 5 + 900), (pct, eta)   # étape trop longue : plafonnée, jamais l'étape suivante
    assert progress._pct(st | {"finished": now.isoformat()})[0] == 100
    print("ok  pourcentage pondéré et fin estimée")


def test_run_and_learned_times():
    store.remove(progress.TIMES) if store.exists(progress.TIMES) else None
    progress.start()
    st = store.load(progress.FILE, {})
    assert st["step"] == "sources" and st["pid"] == os.getpid() and not st.get("finished")
    for k, _, _ in progress.STEPS[1:]:
        progress.step(k)
    progress.finish()
    st = store.load(progress.FILE, {})
    assert st["finished"] and st["minutes"] == 0
    times = store.load(progress.TIMES, {})
    default = dict((k, d) for k, _, d in progress.STEPS)
    assert times["sources"] < default["sources"] and abs(times["sources"] - default["sources"] / 2) < 1, times     # moyenne glissante 50/50
    assert progress.expected()["sources"] == times["sources"]
    progress.step("bilan")                        # après la fin : rien ne bouge
    assert store.load(progress.FILE, {})["finished"]
    assert progress.describe().startswith("✅ Passage du matin terminé")
    print("ok  passage complet, durées apprises")


def test_describe_running_and_dead():
    now = datetime.now(timezone.utc)
    st = {"kind": "daily", "pid": os.getpid(), "started": (now - timedelta(minutes=5)).isoformat(), "step": "profils",
          "step_at": (now - timedelta(seconds=30)).isoformat(), "durations": {}, "weights": progress.expected()}
    store.save(progress.FILE, st)
    txt = progress.describe()
    assert txt.startswith("⏳ Passage du matin : ") and "stats mondiales API-Football" in txt and "fin estimée vers" in txt, txt
    assert "passage du matin :" in progress.busy_suffix() and "état" in telegram._busy()
    store.save(progress.FILE, st | {"pid": 2 ** 22 + 12345})       # processus disparu (arrêt, plantage)
    assert progress.describe().startswith("⚠️ Passage du matin interrompu pendant : stats mondiales"), progress.describe()
    assert progress.busy_suffix() == "" and telegram._busy() == telegram.BUSY
    store.remove(progress.FILE)
    assert progress.describe().startswith("Pas encore de passage")
    print("ok  « état » en cours, interrompu, jamais lancé")


def test_quick_words():
    for t in ("état", "Etat", "/etat", "/etat@KellyBot", "statut", "progression ?", "avancement"):
        assert chat.quick(t), t
    for t in ("bilan", "Etats-Unis", "PSG", "", "combi 20"):
        assert chat.quick(t) is None, t
    assert chat.route("état")[0] == "/etat"       # --commands (sans serveur) : commande normale
    print("ok  « état » reconnu, le reste passe au traitement normal")


def test_poll_quick_skips_lock():
    sent, handled, locked = [], [], []
    old = (telegram.creds, telegram.api, telegram.send)
    telegram.creds = lambda: ("t", "42")
    telegram.api = lambda *a, **k: [{"update_id": 1, "message": {"text": "état", "chat": {"id": 42}}},
                                     {"update_id": 2, "message": {"text": "bilan", "chat": {"id": 42}}}]
    telegram.send = lambda text, **k: sent.append(str(text)) or True

    class Lock:
        def __init__(self, on_busy):
            locked.append(1)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    try:
        telegram.poll(lambda t: handled.append(t) or "réponse", lock=Lock, quick=chat.quick)
    finally:
        telegram.creds, telegram.api, telegram.send = old
    assert handled == ["bilan"] and len(locked) == 1 and len(sent) == 2, (handled, locked, sent)
    print("ok  « état » répondu sans prendre le verrou")


def test_poll_defer():
    """Serveur : verrou pris par un passage -> « bilan » mis de côté (BUSY), « état » répond quand même ; verrou libre -> « bilan » traité, dans l'ordre."""
    import fcntl
    sent, handled = [], []
    batches = [[{"update_id": 10, "message": {"text": "bilan", "chat": {"id": 42}}},
                {"update_id": 11, "message": {"text": "mes paris", "chat": {"id": 42}}},
                {"update_id": 12, "message": {"text": "état", "chat": {"id": 42}}}], []]
    old = (telegram.creds, telegram.api, telegram.send)
    telegram.creds = lambda: ("t", "42")
    telegram.api = lambda *a, **k: batches.pop(0) if batches else []
    telegram.send = lambda text, **k: sent.append(str(text)) or True
    f = open(store.lock_path(), "a")
    fcntl.flock(f, fcntl.LOCK_EX)                 # un passage tient le verrou
    try:
        telegram.poll(lambda t: handled.append(t) or "ok " + t, defer=True, quick=chat.quick)
        assert handled == [] and len(store.load(telegram.LATER, [])) == 2, (handled, sent)
        assert sum(x.startswith("⏳ Kelly est en plein travail") for x in sent) == 2 and len(sent) == 3, sent
        fcntl.flock(f, fcntl.LOCK_UN)             # le passage se termine
        telegram.poll(lambda t: handled.append(t) or "ok " + t, defer=True, quick=chat.quick)
        assert handled == ["bilan", "mes paris"] and store.load(telegram.LATER, []) == [], handled
        assert sent[-2:] == ["ok bilan", "ok mes paris"], sent
    finally:
        f.close()
        telegram.creds, telegram.api, telegram.send = old
    print("ok  message mis de côté pendant un passage, « état » répond, traité à la fin")


def test_sd_notify():
    d = tempfile.mkdtemp(prefix="kelly-notify-")
    path = os.path.join(d, "notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    os.environ["NOTIFY_SOCKET"] = path
    try:
        progress._notify("42 % · test")
        assert srv.recv(256) == "STATUS=42 % · test".encode()
        os.environ["NOTIFY_SOCKET"] = os.path.join(d, "absent")
        progress._notify("rien")                   # socket absent : jamais d'erreur
    finally:
        os.environ.pop("NOTIFY_SOCKET", None)
        srv.close()
    print("ok  STATUS= systemd")


def main():
    test_pct()
    test_run_and_learned_times()
    test_describe_running_and_dead()
    test_quick_words()
    test_poll_quick_skips_lock()
    test_poll_defer()
    test_sd_notify()
    print("tests progression : OK")


if __name__ == "__main__":
    main()
