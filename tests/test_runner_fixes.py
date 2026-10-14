"""Exploitation Termux : verrou du matin (--daily), runner (rattrapage, instance unique, relance avec attente, signaux, délais), .gitignore.
Hors ligne. Lancer : python -m tests.test_runner_fixes"""
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-runner-")
os.environ["QUIET_HOURS"] = "off"
for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "XBET_PROXY", "S365_PROXY", "XDG_RUNTIME_DIR"):
    os.environ.pop(k, None)

from kelly import envfile  # noqa: E402

envfile.load = lambda *a, **k: []               # bot.py ne lit jamais le vrai .env pendant les tests
import bot  # noqa: E402
import termux_runner as R  # noqa: E402
from kelly import app, store, telegram  # noqa: E402

UTC = timezone.utc
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def quiet():
    return contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())


def free():
    """Le verrou des données est-il libre pour un AUTRE fil ? (autre open() = autre description de fichier : conflit, comme un autre processus)"""
    out = []

    def probe():
        try:
            with store.data_lock(wait=0):
                out.append(True)
        except store.LockTimeout:
            out.append(False)
    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return out[0]


# ---------- #8 : --daily sous le verrou des données ----------

def test_daily_lock():
    seen = {}
    real = app.daily_entry

    def fake(sample=False, force=False):
        seen["free_during"] = free()
        with store.data_lock(wait=0):               # code du matin qui reprend le verrou (fmmatch / statswindow --own-lock) : ni inter-blocage ni LockTimeout
            with store.data_lock(wait=0):
                seen["nested"] = True
        seen["free_after_nested"] = free()
        seen["args"] = (sample, force)
        return 0
    app.daily_entry = fake
    try:
        assert free()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                bot.main(["--daily", "--force"])
        except SystemExit as e:
            assert e.code == 0
        assert seen["free_during"] is False and seen["nested"] and seen["free_after_nested"] is False   # tenu pendant TOUT le matin, y compris après un verrou imbriqué
        assert seen["args"] == (False, True)
        assert free()                               # relâché à la fin
        # code de sortie de daily_entry transmis, verrou relâché même si le matin plante
        app.daily_entry = lambda sample=False, force=False: 1
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                bot.main(["--daily"])
        except SystemExit as e:
            assert e.code == 1
        assert free()

        def boom(sample=False, force=False):
            raise RuntimeError("boum")
        app.daily_entry = boom
        try:
            bot.main(["--daily"])
            raise AssertionError("pas d'exception")
        except RuntimeError:
            pass
        assert free()
        # --sample et --no-lock : aucun verrou
        for opt in ("--sample", "--no-lock"):
            app.daily_entry = lambda sample=False, force=False: seen.__setitem__("f", free()) or 0
            try:
                bot.main(["--daily", opt])
            except SystemExit:
                pass
            assert seen["f"] is True, opt
    finally:
        app.daily_entry = real
    print("ok  --daily : verrou tenu pendant tout le matin, ré-entrant, relâché même sur erreur, --sample / --no-lock sans verrou")


def test_daily_lock_busy_exit():
    real, real_lock = app.daily_entry, store.data_lock
    ran = []
    app.daily_entry = lambda sample=False, force=False: ran.append(1) or 0
    store.data_lock = lambda wait=None, on_busy=None, reentrant=False: real_lock(wait=0.3, on_busy=on_busy, reentrant=reentrant)   # attente de 30 min ramenée à 0,3 s
    holder_in, holder_out = threading.Event(), threading.Event()

    def hold():
        with real_lock():
            holder_in.set()
            holder_out.wait(10)
    t = threading.Thread(target=hold)
    t.start()
    holder_in.wait(5)
    try:
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                bot.main(["--daily"])
        except SystemExit as e:
            assert e.code == 1
        assert not ran and "attente de la fin" in out.getvalue() and "abandonné" in err.getvalue()
    finally:
        holder_out.set()
        t.join(5)
        app.daily_entry, store.data_lock = real, real_lock
    print("ok  --daily : verrou pris trop longtemps -> abandon propre, code 1, matin non lancé")


def test_serve_reports_busy_during_daily():
    """Pendant le matin, un message traité par --serve (telegram._later) n'écrit rien : « Kelly est en plein travail » ; traité à la fin."""
    real, sent = app.daily_entry, []
    real_send = telegram.send
    telegram.send = lambda text, *a, **k: sent.append(text) or True
    inside, release = threading.Event(), threading.Event()

    def fake(sample=False, force=False):
        inside.set()
        release.wait(10)
        return 0
    app.daily_entry = fake

    def morning():
        with quiet()[0], quiet()[1]:
            try:
                bot.main(["--daily"])
            except SystemExit:
                pass
    t = threading.Thread(target=morning)
    t.start()
    inside.wait(5)
    try:
        done = []
        telegram._later(lambda *a: done.append(a), ("joué",), False)
        assert not done and sent and sent[-1].startswith("⏳ Kelly est en plein travail"), sent
        assert store.load(telegram.LATER, [])       # mis de côté, pas perdu
        release.set()
        t.join(5)
        assert telegram._run_later(lambda *a: done.append(a), None) is True and done == [("joué",)]   # traité dès le matin fini
    finally:
        release.set()
        app.daily_entry = real
        telegram.send = real_send
    print("ok  --serve : « Kelly est en plein travail » pendant le matin, action rejouée ensuite")


def test_reentrant_scope():
    """La ré-entrance (reentrant=True) ne vaut que pour le fil qui tient le verrou ; un autre fil (ou processus) reste en conflit ; par défaut inchangé."""
    with store.data_lock(wait=0, reentrant=True):
        with store.data_lock(wait=0):
            with store.data_lock(wait=0):
                pass
            assert free() is False
        assert free() is False
    assert free()
    with store.data_lock(wait=0):                   # sans reentrant : un verrou imbriqué se heurte toujours au premier (comportement d'avant)
        try:
            with store.data_lock(wait=0):
                raise AssertionError("imbriqué sans reentrant")
        except store.LockTimeout:
            pass
    assert free()
    print("ok  data_lock reentrant=True : ré-entrant dans le fil seulement, défaut inchangé")


# ---------- #9 : planning ----------

def at(h, m=0, d=12, mo=10):                        # 2026-10-12 est un lundi ; le 11 un dimanche
    return datetime(2026, mo, d, h, m, tzinfo=UTC)


def test_due_tasks():
    st = R.new_state()
    names = lambda now, s=st, e=1e9: [a[0] for a in R.due_tasks(now, s, e)]       # noqa: E731
    assert "--daily" not in names(at(3, 59))
    assert names(at(4, 0))[0] == "--daily" and "--track" not in names(at(4, 30))     # 04h réservé au matin
    # rattrapage : téléphone éteint, runner relancé à 05h13 -> le matin part ; une fois réussi plus jamais aujourd'hui
    assert names(at(5, 13))[:2] == ["--daily", "--track"]
    R.mark_done(st, ["--daily"], at(5, 13), 100.0, 0)
    assert "--daily" not in names(at(5, 14)) and "--daily" not in names(at(11, 0))
    assert "--daily" in names(at(4, 0, d=13))                                          # le lendemain : de nouveau dû
    # trop tard dans la journée : pas de rattrapage (on attend demain)
    assert "--daily" not in names(at(21, 0), R.new_state())
    assert "--daily" in names(at(19, 59), R.new_state())
    # échec / délai dépassé : nouvel essai après 30 min, 3 essais au plus
    s2 = R.new_state()
    R.mark_done(s2, ["--daily"], at(4, 0), 1000.0, 124)
    assert s2["daily_ok_day"] == "" and "--daily" not in names(at(4, 10), s2, 1000.0 + 600)
    assert "--daily" in names(at(4, 40), s2, 1000.0 + 1800)
    R.mark_done(s2, ["--daily"], at(4, 40), 2800.0, 1)
    R.mark_done(s2, ["--daily"], at(5, 15), 4700.0, 1)
    assert s2["daily_tries"] == 3 and "--daily" not in names(at(9, 0), s2, 1e9)
    # le matin ignoré car déjà fait à la main (bot.py rend 0) compte comme fait : pas de boucle
    s3 = R.new_state()
    R.mark_done(s3, ["--daily"], at(6, 0), 5.0, 0)
    assert "--daily" not in names(at(6, 1), s3, 1e9)
    # passage horaire : une fois par heure, à partir de la 5e minute
    s4 = R.new_state()
    assert "--track" not in names(at(11, 4), s4) and "--track" in names(at(11, 5), s4)
    R.mark_done(s4, ["--track", "--own-lock"], at(11, 5), 1.0, 0)
    assert "--track" not in names(at(11, 50), s4) and "--track" in names(at(12, 5), s4)
    # xbet 15h, evening 18h, export 23h, tune le dimanche 02-03h ; une fois par jour
    s5 = R.new_state()
    assert "--xbet" in names(at(15), s5) and "--xbet" not in names(at(14), s5) and "--evening" in names(at(18), s5)
    exp = R.due_tasks(at(23, 10), s5, 1e9)
    assert ["--export", "--own-lock"] in exp and ["--export"] not in exp
    assert "--tune" not in names(at(2, 30, d=12), s5) and "--tune" in names(at(2, 30, d=11), s5) and "--tune" not in names(at(5, 0, d=11), s5)
    for a in (["--xbet"], ["--evening"], ["--export", "--own-lock"]):
        R.mark_done(s5, a, at(15), 1.0, 0)
    R.mark_done(s5, ["--tune"], at(2, 30, d=11), 1.0, 0)
    assert not {"--xbet", "--evening", "--export", "--tune"} & set(names(at(15), s5)) and "--tune" not in names(at(3, 0, d=11), s5)
    # watch : toutes les 10 min
    s6 = R.new_state()
    R.mark_done(s6, ["--watch"], at(10), 1000.0, 0)
    assert "--watch" not in names(at(10), s6, 1599.0) and "--watch" in names(at(10), s6, 1600.0)
    for a in R.due_tasks(at(15, 10), R.new_state(), 1e9):
        assert a[0] in R.TIMEOUTS, a                # chaque passage planifié a un délai maximal
    print("ok  planning : matin rattrapé une seule fois par jour, essais limités, track / xbet / evening / export / tune / watch")


def test_state_roundtrip():
    p = os.path.join(tempfile.mkdtemp(prefix="kelly-state-"), "sub", "runner_state.json")
    st = R.new_state()
    st["xbet_day"] = "2026-10-12"
    R.save_state(st, p)
    assert R.load_state(p)["xbet_day"] == "2026-10-12"
    open(p, "w").write("{pas du json")
    assert R.load_state(p) == R.new_state()
    open(p, "w").write('{"watch_at": "x", "inconnu": 1, "xbet_day": "2026-10-13"}')
    got = R.load_state(p)
    assert got["watch_at"] == 0.0 and got["xbet_day"] == "2026-10-13" and "inconnu" not in got
    print("ok  état du planning : écriture, relecture, fichier abîmé ignoré")


def test_single_instance():
    p = os.path.join(tempfile.mkdtemp(prefix="kelly-inst-"), "logs", "runner.lock")
    a = R.acquire_single_instance(p)
    assert a is not None and open(p).read().strip() == str(os.getpid())
    assert R.acquire_single_instance(p) is None                          # même processus, autre description : refusé
    code = "import sys, termux_runner as R; sys.exit(0 if R.acquire_single_instance(sys.argv[1]) is None else 7)"
    assert subprocess.run([sys.executable, "-c", code, p], cwd=ROOT).returncode == 0     # un autre processus aussi
    a.close()                                                            # libéré (comme à la mort du runner)
    b = R.acquire_single_instance(p)
    assert b is not None
    b.close()
    assert subprocess.run([sys.executable, "-c", code, p], cwd=ROOT).returncode == 7
    print("ok  instance unique : second runner refusé, verrou libéré à la fermeture")


def test_backoff():
    assert [R.backoff_delay(n) for n in range(0, 9)] == [0, 15, 30, 60, 120, 240, 480, 900, 900]
    # superviseur : arrêts rapprochés -> attente croissante ; longue vie -> relance immédiate
    now = [1000.0]
    d = tempfile.mkdtemp(prefix="kelly-sup-")
    sup = R.ServeSupervisor([sys.executable, "-c", "import sys; sys.exit(3)"], pidfile=os.path.join(d, "serve.pid"), cwd=d, clock=lambda: now[0])
    spawned = []
    real_spawn = sup._spawn

    def spawn():
        real_spawn()
        spawned.append(now[0])
    sup._spawn = spawn
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            sup.check()                                                   # premier lancement
            expect = [15, 30, 60, 120, 240, 480, 900, 900]
            for delay in expect:
                sup.proc.wait(10)
                sup.check()                                               # constate l'arrêt
                assert sup.proc is None and sup.next_start == now[0] + delay, (delay, sup.next_start - now[0])
                now[0] += delay - 1
                sup.check()
                assert sup.proc is None                                   # pas encore l'heure
                now[0] += 1
                sup.check()
                assert sup.proc is not None
            assert [round(b - a) for a, b in zip(spawned, spawned[1:])] == expect
            # un passage stable (> STABLE_SECS) remet le compteur à zéro et la relance est immédiate
            sup.proc.wait(10)
            now[0] += R.STABLE_SECS + 5
            sup.check()
            assert sup.fails == 0 and sup.next_start == now[0] and sup.proc is None
            sup.check()
            assert sup.proc is not None
    finally:
        sup.stop()
    log = err.getvalue()
    assert "arrêts rapprochés n°1 -> relance dans 15 s" in log and "plafond 900 s" in log and "relance immédiate" in log, log
    assert not os.path.exists(os.path.join(d, "serve.pid"))
    print("ok  relance de --serve : 15 s, 30 s ... plafond 900 s, remise à zéro après une longue vie, ligne de journal claire")


def test_supervisor_survives_missing_script():
    sup = R.ServeSupervisor(["/chemin/inexistant/python"], cwd=tempfile.mkdtemp(), clock=lambda: 0.0)
    with quiet()[0], quiet()[1]:
        sup.check()
    assert sup.proc is None and sup.fails == 1 and sup.next_start == 15
    print("ok  lancement impossible de --serve : pas d'exception, attente croissante")


def helper_proc(logs, sighup_ignored=False):
    code = ("import sys, atexit, termux_runner as R\n"
            "R.LOGS = sys.argv[1]\n"
            "r = R.Runner(serve_cmd=[sys.executable, '-c', 'import time; time.sleep(60)'], tick=0.2, plan=lambda n, s, e: [], state=R.new_state(), watch_thread=False)\n"
            "atexit.register(r.shutdown)\n"
            "R.install_signals()\n"
            "print('pret', flush=True)\n"
            "r.loop(); r.shutdown(); print('fin', flush=True)\n")
    # SIGHUP explicitement remis par défaut quand il n'est pas ignoré : un test lancé sous nohup (disposition SIG_IGN héritée par les sous-processus) ne doit pas changer de résultat
    pre = (lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN)) if sighup_ignored else (lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL))
    p = subprocess.Popen([sys.executable, "-c", code, logs], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=pre)
    pidfile = os.path.join(logs, "serve.pid")
    end = time.time() + 10
    while time.time() < end and not (os.path.exists(pidfile) and open(pidfile).read().strip()):
        time.sleep(0.05)
    return p, int(open(pidfile).read())


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def test_signals():
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        logs = tempfile.mkdtemp(prefix="kelly-sig-")
        p, child = helper_proc(logs)
        try:
            assert alive(child)
            p.send_signal(sig)
            out, err = p.communicate(timeout=20)
            assert p.returncode == 0 and "fin" in out, (sig, p.returncode, out, err)
            assert not alive(child), f"{sig.name} : bot.py --serve a survécu"
            assert not os.path.exists(os.path.join(logs, "serve.pid"))
        finally:
            if p.poll() is None:
                p.kill()
            if alive(child):
                os.kill(child, signal.SIGKILL)
    # lancé par nohup (SIGHUP ignoré) : la fermeture de la session ne l'arrête pas ; SIGTERM, si
    logs = tempfile.mkdtemp(prefix="kelly-sig-")
    p, child = helper_proc(logs, sighup_ignored=True)
    try:
        p.send_signal(signal.SIGHUP)
        time.sleep(1)
        assert p.poll() is None and alive(child)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        assert p.returncode == 0 and not alive(child)
    finally:
        if p.poll() is None:
            p.kill()
        if alive(child):
            os.kill(child, signal.SIGKILL)
    print("ok  SIGTERM / SIGHUP / SIGINT : --serve arrêté et PID effacé ; SIGHUP ignoré sous nohup")


def test_atexit_and_stale():
    # atexit : le runner qui sort sans passer par le signal (exception) arrête quand même --serve
    logs = tempfile.mkdtemp(prefix="kelly-atexit-")
    code = ("import sys, atexit, termux_runner as R\n"
            "R.LOGS = sys.argv[1]\n"
            "r = R.Runner(serve_cmd=[sys.executable, '-c', 'import time; time.sleep(60)'], tick=0.2, plan=lambda n, s, e: [], state=R.new_state(), watch_thread=False)\n"
            "atexit.register(r.shutdown)\n"
            "r.serve.check(); print('PIDLINE', r.serve.proc.pid, flush=True)\n"
            "raise SystemExit(0)\n")
    out = subprocess.run([sys.executable, "-c", code, logs], cwd=ROOT, capture_output=True, text=True, timeout=30)
    child = int(out.stdout.split("PIDLINE")[1].split()[0])
    assert not alive(child)
    # ancien --serve orphelin (runner tué par SIGKILL) : arrêté au démarrage ; un PID étranger n'est jamais touché
    d = tempfile.mkdtemp(prefix="kelly-stale-")
    pf = os.path.join(d, "serve.pid")
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "bot.py", "--serve"], start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        open(pf, "w").write(f"{other.pid}\n")
        with quiet()[0], quiet()[1]:
            assert R.kill_stale_serve(pf) is False and alive(other.pid)
            open(pf, "w").write(f"{orphan.pid}\n")
            assert R.kill_stale_serve(pf) is True
        orphan.wait(5)
        assert not alive(orphan.pid)
        open(pf, "w").write("pas un pid")
        assert R.kill_stale_serve(pf) is False and R.kill_stale_serve(os.path.join(d, "absent")) is False
    finally:
        for q in (orphan, other):
            if q.poll() is None:
                q.kill()
                q.wait()
    print("ok  atexit arrête --serve ; ancien --serve orphelin arrêté, PID étranger intact")


def test_run_cmd_timeout_cwd():
    d = tempfile.mkdtemp(prefix="kelly-cmd-")
    script = os.path.join(d, "fakebot.py")
    open(script, "w").write("import os, sys, time\nopen(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cwd.txt'), 'w').write(os.getcwd())\n"
                            "if '--hang' in sys.argv:\n    time.sleep(60)\nsys.exit(5 if '--fail' in sys.argv else 0)\n")
    real_bot = R.BOT
    R.BOT = script
    try:
        assert os.path.isabs(real_bot) and real_bot == os.path.join(ROOT, "bot.py") and os.path.isabs(R.ROOT)
        with quiet()[0], quiet()[1]:
            assert R.run_cmd(["--ok"], timeout=20, cwd=d) == 0
            assert open(os.path.join(d, "cwd.txt")).read() == os.path.realpath(d) or open(os.path.join(d, "cwd.txt")).read() == d
            assert R.run_cmd(["--fail"], timeout=20, cwd=d) == 5
            t0 = time.monotonic()
            assert R.run_cmd(["--hang"], timeout=2, cwd=d) == 124 and time.monotonic() - t0 < 15          # un appel figé est coupé
            # arrêt du runner pendant un passage : le passage est coupé
            threading.Timer(1.5, R.STOP.set).start()
            t0 = time.monotonic()
            assert R.run_cmd(["--hang"], timeout=60, cwd=d) == 143 and time.monotonic() - t0 < 20
    finally:
        R.BOT = real_bot
        R.STOP.clear()
    print("ok  passages : chemin absolu, dossier du dépôt, délai maximal (124), coupé à l'arrêt (143)")


def test_watch_thread_runs_during_long_task():
    """Le suivi des matchs (--watch) tourne dans son propre fil : il passe PENDANT un --track long, et n'est plus lancé par la boucle principale."""
    logs = tempfile.mkdtemp(prefix="kelly-watchthread-")
    real_logs, real_stop = R.LOGS, R.STOP
    R.LOGS, R.STOP = logs, threading.Event()
    ran, lock = [], threading.Lock()
    st = R.new_state()

    def fake(args):
        with lock:
            ran.append((args[0], time.time()))
        if args[0] == "--track":
            time.sleep(1.5)                         # un relevé horaire long
        return 0
    r = R.Runner(serve_cmd=[sys.executable, "-c", "import time; time.sleep(60)"], tick=0.05, state=st, run=fake,
                 plan=lambda now, s, e: [["--track"], ["--watch"]] if not any(a == "--track" for a, _ in ran) else [])
    t = threading.Thread(target=r.loop)
    t.start()
    try:
        end = time.time() + 5
        while time.time() < end and not any(a == "--watch" for a, _ in ran):
            time.sleep(0.05)
        R.STOP.set()
        t.join(5)
    finally:
        r.shutdown()
        R.LOGS, R.STOP = real_logs, real_stop
    tr = next(x for a, x in ran if a == "--track")
    w = [x for a, x in ran if a == "--watch"]
    assert len(w) == 1 and w[0] < tr + 1.5, ran        # lancé par son fil pendant le --track, une seule fois (jamais par la boucle principale)
    assert st["watch_at"] > 0, st
    print("ok  suivi des matchs dans son propre fil : passe pendant un relevé long")


def test_loop_supervises_during_task():
    """--serve est relancé PENDANT un passage long (fil de surveillance), et le planning marque le passage fait."""
    logs = tempfile.mkdtemp(prefix="kelly-loop-")
    real_logs, real_base = R.LOGS, R.BACKOFF_BASE
    R.LOGS, R.BACKOFF_BASE = logs, 0.2
    ran, pids = [], []
    st = R.new_state()
    r = R.Runner(serve_cmd=[sys.executable, "-c", "import time; time.sleep(60)"], tick=0.1, state=st, watch_every=0.1,
                 plan=lambda now, s, e: [["--watch"]] if not ran else [], watch_thread=False)
    pidfile = os.path.join(logs, "serve.pid")

    def long_task(args):                            # un passage de plusieurs secondes pendant lequel --serve est tué
        ran.append(args)
        end = time.time() + 10
        while not os.path.exists(pidfile) and time.time() < end:
            time.sleep(0.05)
        pids.append(int(open(pidfile).read()))
        os.kill(pids[0], signal.SIGKILL)
        while time.time() < end:
            if int(open(pidfile).read()) != pids[0]:
                pids.append(int(open(pidfile).read()))
                break
            time.sleep(0.05)
        return 0
    r.run = long_task
    t = threading.Thread(target=r.loop)
    try:
        with quiet()[0], quiet()[1]:
            t.start()
            t.join(0.5)
            end = time.time() + 15
            while len(pids) < 2 and time.time() < end:
                time.sleep(0.1)
            time.sleep(0.3)
            R.STOP.set()
            t.join(10)
        assert ran == [["--watch"]] and st["watch_at"] > 0 and len(pids) == 2 and pids[0] != pids[1], (ran, pids)
        assert os.path.exists(os.path.join(logs, "runner_state.json"))      # planning sauvegardé après le passage
    finally:
        R.STOP.clear()
        r.shutdown()
        R.LOGS, R.BACKOFF_BASE = real_logs, real_base
    print("ok  boucle : --serve relancé pendant un passage long, planning sauvegardé, arrêt propre")


# ---------- #11 : .gitignore ----------

def test_gitignore():
    paths = ["data/kelly.db", "data/kelly.db-wal", "data/kelly.db-shm", "data/kelly.db-journal", "data/.kelly.lock", "data/bets.json.123.tmp",
             "data/sauvegarde/bets.json.123.tmp", "data/sauvegarde/bets.json", "logs/termux.log", "logs/runner.lock", ".env", "sofa.env", "probe/x.json"]
    r = subprocess.run(["git", "check-ignore"] + paths, cwd=ROOT, capture_output=True, text=True)
    ignored = set(r.stdout.split())
    assert ignored == set(paths), set(paths) - ignored
    keep = subprocess.run(["git", "check-ignore", "bot.py", "termux_runner.py", "tests/test_runner_fixes.py"],
                          cwd=ROOT, capture_output=True, text=True)
    assert keep.stdout.strip() == "", keep.stdout                        # le code reste suivi
    print("ok  .gitignore : base, verrous, temporaires, logs et miroir data/sauvegarde ignorés ; code suivi")


def test_unexpected_error_never_stops_the_loops():
    """Une erreur imprévue (planning illisible, disque plein…) dans la boucle principale ou dans le fil du suivi des matchs : notée dans le
    journal, et la boucle continue au tour suivant (ni Kelly ni le suivi ne s'arrêtent en silence)."""
    logs = tempfile.mkdtemp(prefix="kelly-loopguard-")
    real_logs, real_stop = R.LOGS, R.STOP
    R.LOGS, R.STOP = logs, threading.Event()
    calls = {"plan": 0, "watch": 0}

    def plan(now, s, e):
        calls["plan"] += 1
        if calls["plan"] == 1:
            raise ValueError("planning illisible")
        return []

    def fake(args):
        calls["watch"] += 1
        if calls["watch"] == 1:
            raise OSError("disque plein")
        return 0
    st = R.new_state()
    r = R.Runner(serve_cmd=[sys.executable, "-c", "import time; time.sleep(60)"], tick=0.05, state=st, run=fake, plan=plan)
    old_every = R.WATCH_EVERY
    R.WATCH_EVERY = 0
    t = threading.Thread(target=r.loop)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            t.start()
            end = time.time() + 5
            while time.time() < end and (calls["plan"] < 3 or calls["watch"] < 3):
                time.sleep(0.05)
            R.STOP.set()
            t.join(5)
    finally:
        r.shutdown()
        R.LOGS, R.STOP, R.WATCH_EVERY = real_logs, real_stop, old_every
    assert calls["plan"] >= 3 and calls["watch"] >= 3, calls
    out = err.getvalue()
    assert "passage : erreur inattendue (ValueError: planning illisible), on continue" in out, out
    assert "suivi des matchs : erreur inattendue (OSError: disque plein), on continue" in out, out
    print("ok  erreur imprévue dans la boucle ou dans le suivi des matchs : notée, et la boucle continue")


def test_log_rotation():
    """Journal du runner au-delà de LOG_MAX : copié en .1 puis vidé sur place ; en dessous, rien ; sans chemin ou fichier absent : rien, jamais d'erreur."""
    d = tempfile.mkdtemp(prefix="kelly-logrot-")
    path = os.path.join(d, "termux.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("x" * 120)
    assert not R.rotate_log(path, limit=200) and os.path.getsize(path) == 120
    with open(path, "a", encoding="utf-8") as f:
        f.write("y" * 100)
    assert R.rotate_log(path, limit=200) and os.path.getsize(path) == 0 and open(path + ".1").read() == "x" * 120 + "y" * 100
    assert not R.rotate_log(os.path.join(d, "absent.log"), limit=1) and not R.rotate_log(None)
    print("ok  journal trop gros : copié en .1 puis vidé ; petit, absent ou sans chemin : rien")


def main():
    test_reentrant_scope()
    test_daily_lock()
    test_daily_lock_busy_exit()
    test_serve_reports_busy_during_daily()
    test_due_tasks()
    test_state_roundtrip()
    test_single_instance()
    test_backoff()
    test_supervisor_survives_missing_script()
    test_signals()
    test_atexit_and_stale()
    test_run_cmd_timeout_cwd()
    test_watch_thread_runs_during_long_task()
    test_unexpected_error_never_stops_the_loops()
    test_log_rotation()
    test_loop_supervises_during_task()
    test_gitignore()
    print("TOUS LES TESTS DU RUNNER PASSENT")


if __name__ == "__main__":
    main()
