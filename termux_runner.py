"""Orchestrateur tout-en-un pour Termux (Android / GrapheneOS).
Remplace systemd : garde `bot.py --serve` en vie (relance avec attente croissante) et planifie les passages périodiques
(--watch, --track, --daily, --xbet, --evening, --export, --tune). Une seule instance à la fois (verrou logs/runner.lock) ;
SIGTERM / SIGHUP / Ctrl+C arrêtent proprement le bot ; chemins absolus (peut être lancé depuis n'importe quel dossier).
Tout est en UTC. L'horaire est décidé par `due_tasks` (fonction pure, testée dans tests/test_runner_fixes.py)."""
import atexit
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(ROOT, "bot.py")
LOGS = os.path.join(ROOT, "logs")

DAILY_HOUR = 4               # passage du matin dès 04:00 UTC (= C.DAY_END_HOUR) ; rattrapage ensuite, une seule fois par jour
DAILY_LAST_HOUR = 20         # après cette heure on n'ouvre plus de rattrapage du matin (attendre le lendemain)
DAILY_MAX_TRIES = 3          # essais du matin par jour (échec, délai dépassé) ...
DAILY_RETRY_GAP = 1800       # ... espacés d'au moins 30 min
WATCH_EVERY = 600
EXPORT_HOUR = 23             # sauvegarde lisible de la base (data/sauvegarde/)
TUNE_WEEKDAY, TUNE_HOURS = 6, (2, 3)    # dimanche 02:00-03:59 UTC (avant le matin ; --tune reprend où il s'est arrêté)

# délai maximal par passage (s) : un appel réseau figé ne bloque plus tout le runner (--daily dure ~30 min, attente du verrou comprise)
TIMEOUTS = {"--watch": 540, "--track": 1500, "--daily": 5400, "--xbet": 1500, "--evening": 1500, "--export": 900, "--tune": 2400}

BACKOFF_BASE, BACKOFF_CAP, STABLE_SECS = 15, 900, 120      # relance de --serve : 15 s, 30 s, 1 min ... 15 min ; stable après 2 min de vie
STOP = threading.Event()
SIGNALS = []                 # signaux reçus (le journal s'écrit hors du gestionnaire)


def log(msg, err=False):
    print(f"[termux {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


# ---------- décisions (pures) ----------

def new_state():
    return {"watch_at": 0.0, "track_key": "", "daily_ok_day": "", "daily_try_day": "", "daily_tries": 0, "daily_try_at": 0.0,
            "xbet_day": "", "evening_day": "", "export_day": "", "tune_day": ""}


def backoff_delay(fails):
    """Attente avant de relancer --serve après `fails` arrêts rapprochés (0 = il a vécu longtemps : tout de suite)."""
    return 0 if fails <= 0 else min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (fails - 1))


def due_tasks(now, state, epoch):
    """Passages à lancer maintenant -> liste d'arguments de bot.py, dans l'ordre. `now` : datetime UTC ; `epoch` : secondes (time.time()).
    Ne modifie pas `state` (voir mark_done). Le matin est RATTRAPÉ : dès 04:00 et jusqu'à DAILY_LAST_HOUR, tant qu'il n'a pas réussi
    aujourd'hui (téléphone éteint à 04:00, runner relancé à 05:00...) ; `bot.py --daily` s'ignore de lui-même s'il est déjà fait
    (data/daily_done.json, app.daily_skip_reason), donc un passage manuel n'est jamais doublé."""
    day, hour = str(now.date()), now.hour
    out = []
    if (DAILY_HOUR <= hour < DAILY_LAST_HOUR and state["daily_ok_day"] != day
            and (state["daily_try_day"] != day or (state["daily_tries"] < DAILY_MAX_TRIES and epoch - state["daily_try_at"] >= DAILY_RETRY_GAP))):
        out.append(["--daily"])
    if now.minute >= 5 and hour != DAILY_HOUR and state["track_key"] != f"{day} {hour}":
        out.append(["--track", "--own-lock"])
    if hour == 15 and state["xbet_day"] != day:
        out.append(["--xbet"])
    if hour == 18 and state["evening_day"] != day:
        out.append(["--evening"])
    if hour == EXPORT_HOUR and state["export_day"] != day:
        out.append(["--export", "--own-lock"])
    if now.weekday() == TUNE_WEEKDAY and hour in TUNE_HOURS and state["tune_day"] != day:
        out.append(["--tune"])
    if epoch - state["watch_at"] >= WATCH_EVERY:
        out.append(["--watch"])
    return out


def mark_done(state, args, now, epoch, rc):
    """Note qu'un passage a été lancé (rc = code de sortie). Le matin n'est « fait » que s'il réussit (0, ou ignoré car déjà fait) ; sinon il est
    retenté (DAILY_MAX_TRIES, DAILY_RETRY_GAP). Les autres passages ne sont jamais retentés dans la même tranche (comme avant)."""
    day, name = str(now.date()), args[0]
    if name == "--daily":
        if state["daily_try_day"] != day:
            state["daily_try_day"], state["daily_tries"] = day, 0
        state["daily_tries"] += 1
        state["daily_try_at"] = epoch
        if rc == 0:
            state["daily_ok_day"] = day
    elif name == "--track":
        state["track_key"] = f"{day} {now.hour}"
    elif name == "--watch":
        state["watch_at"] = epoch
    else:
        key = {"--xbet": "xbet_day", "--evening": "evening_day", "--export": "export_day", "--tune": "tune_day"}.get(name)
        if key:
            state[key] = day


def state_path():
    return os.path.join(LOGS, "runner_state.json")


def load_state(path=None):
    """État du planning gardé entre deux démarrages du runner (pas de relance de --xbet / --export en double après un redémarrage)."""
    st = new_state()
    try:
        with open(path or state_path(), encoding="utf-8") as f:
            saved = json.load(f)
        st.update({k: v for k, v in saved.items() if k in st and type(v) is type(st[k])})
    except (OSError, ValueError):
        pass
    return st


def save_state(state, path=None):
    path = path or state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as e:
        log(f"état du planning non écrit ({e})", err=True)


# ---------- instance unique, processus ----------

def acquire_single_instance(path):
    """Verrou exclusif (flock) du runner : -> fichier à garder ouvert, ou None si un autre runner tourne. Libéré à la mort du processus."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None


def kill_stale_serve(pidfile):
    """`bot.py --serve` resté d'un ancien runner tué sans prévenir (SIGKILL, plantage) : l'arrête avant d'en lancer un autre (sinon Telegram 409 :
    deux serveurs se volent les messages). Ne tue que si /proc confirme que le PID est bien « bot.py --serve ». -> True si un processus a été arrêté."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    cmd = _cmdline(pid)
    if not cmd or "bot.py" not in cmd or "--serve" not in cmd:
        return False
    log(f"ancien bot.py --serve (PID {pid}) trouvé : arrêt avant relance")
    stop_group(pid, grace=10)
    return True


def stop_group(pid, grace=10):
    """SIGTERM au groupe de processus du fils (lancé avec start_new_session), puis SIGKILL après `grace` secondes."""
    def alive():
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3)):
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)       # notre fils : récolte le zombie
                if done:
                    return
            except ChildProcessError:
                if not alive():                             # pas notre fils (ancien runner) : on regarde si le PID a disparu
                    return
            time.sleep(0.1)


class ServeSupervisor:
    """Garde `bot.py --serve` en vie. Arrêt rapide (< STABLE_SECS) répété : attente 15 s, 30 s, 1 min ... plafonnée à 15 min, avec une ligne de journal
    claire ; arrêt après une longue vie : relance tout de suite. check() est sûr depuis plusieurs fils (boucle principale + fil de surveillance)."""

    def __init__(self, cmd, pidfile=None, cwd=ROOT, clock=time.monotonic):
        self.cmd, self.pidfile, self.cwd, self.clock = cmd, pidfile, cwd, clock
        self.proc, self.fails, self.started, self.next_start, self.stopped = None, 0, 0.0, 0.0, False
        self.lock = threading.Lock()

    def _spawn(self):
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, start_new_session=True)
        self.started = self.clock()
        if self.pidfile:
            try:
                with open(self.pidfile, "w") as f:
                    f.write(f"{self.proc.pid}\n")
            except OSError:
                pass
        log(f"✅ Bot Telegram actif (PID {self.proc.pid})")

    def check(self):
        with self.lock:
            if self.stopped:
                return
            now = self.clock()
            if self.proc is None:
                if now >= self.next_start:
                    try:
                        self._spawn()
                    except OSError as e:
                        self.fails += 1
                        self.next_start = now + backoff_delay(self.fails)
                        log(f"⚠️ lancement de bot.py --serve impossible ({e}) ; nouvel essai dans {backoff_delay(self.fails)} s", err=True)
                return
            rc = self.proc.poll()
            if rc is None:
                if self.fails and now - self.started >= STABLE_SECS:
                    self.fails = 0                          # a tenu assez longtemps : on repart de zéro
                return
            up = now - self.started
            self.fails = 0 if up >= STABLE_SECS else self.fails + 1
            delay = backoff_delay(self.fails)
            self.proc, self.next_start = None, now + delay
            log(f"⚠️ bot.py --serve s'est arrêté (code {rc}) après {up:.0f} s ; "
                + (f"arrêts rapprochés n°{self.fails} -> relance dans {delay} s (plafond {BACKOFF_CAP} s)" if delay else "relance immédiate"), err=True)

    def stop(self):
        with self.lock:
            self.stopped = True
            p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            log(f"arrêt de bot.py --serve (PID {p.pid})")
            stop_group(p.pid)
        if self.pidfile:
            try:
                os.remove(self.pidfile)
            except OSError:
                pass

    def watch(self, every=5):
        """Fil de surveillance : relance --serve même pendant un passage long (--daily ~30 min)."""
        while not STOP.wait(every):
            try:
                self.check()
            except Exception as e:                          # noqa: BLE001 - le fil ne meurt jamais
                log(f"surveillance de --serve : {type(e).__name__}: {e}", err=True)


# ---------- passages ----------

def run_cmd(args, timeout=None, cwd=ROOT, stop=STOP):
    """Lance `bot.py <args>` (chemin absolu, dossier du dépôt) avec délai maximal ; coupé aussi à l'arrêt du runner. -> code de sortie (124 = délai
    dépassé, 143 = interrompu par l'arrêt, 1 = impossible à lancer)."""
    timeout = timeout or TIMEOUTS.get(args[0], 1800)
    try:
        p = subprocess.Popen([sys.executable, "-u", BOT] + args, cwd=cwd, start_new_session=True)
    except OSError as e:
        log(f"Erreur {args}: {e}", err=True)
        return 1
    end = time.monotonic() + timeout
    while True:
        try:
            return p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if stop.is_set():
            stop_group(p.pid)
            return 143
        if time.monotonic() >= end:
            log(f"⏱️ {' '.join(args)} dépasse {timeout} s : interrompu", err=True)
            stop_group(p.pid)
            return 124


def install_signals():
    """SIGTERM (kill, Android), SIGHUP (session Termux fermée, sauf sous nohup), SIGINT : arrêt propre. Le gestionnaire ne fait que lever STOP."""
    def handler(signum, _frame):
        SIGNALS.append(signum)
        STOP.set()
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        if s == signal.SIGHUP and signal.getsignal(s) == signal.SIG_IGN:
            continue                    # lancé par `nohup` : la fermeture de la session Termux ne doit PAS arrêter Kelly
        signal.signal(s, handler)


class Runner:
    def __init__(self, serve_cmd=None, tick=15, run=run_cmd, plan=due_tasks, state=None, watch_every=5):
        os.makedirs(LOGS, exist_ok=True)
        self.serve = ServeSupervisor(serve_cmd or [sys.executable, "-u", BOT, "--serve"], pidfile=os.path.join(LOGS, "serve.pid"))
        self.tick, self.run, self.plan, self.watch_every = tick, run, plan, watch_every
        self.state = state if state is not None else load_state()

    def shutdown(self):
        self.serve.stop()

    def loop(self):
        threading.Thread(target=self.serve.watch, args=(self.watch_every,), daemon=True).start()
        while not STOP.is_set():
            self.serve.check()
            now, epoch = datetime.now(timezone.utc), time.time()
            if now.hour >= DAILY_LAST_HOUR and self.state["daily_ok_day"] != str(now.date()) and self.state["daily_try_day"] != str(now.date()):
                log(f"⚠️ le matin du {now.date()} n'a pas tourné (téléphone éteint ?) ; prochain passage demain à {DAILY_HOUR:02d}:00 UTC", err=True)
                self.state["daily_try_day"], self.state["daily_tries"] = str(now.date()), DAILY_MAX_TRIES
            for args in self.plan(now, self.state, epoch):
                if STOP.is_set():
                    break
                if args[0] == "--daily" and now.hour > DAILY_HOUR:
                    log(f"rattrapage du matin à {now:%H:%M} UTC (pas fait à {DAILY_HOUR:02d}:00)")
                rc = self.run(args)
                mark_done(self.state, args, now, time.time(), rc)
                save_state(self.state)
                self.serve.check()
                now = datetime.now(timezone.utc)
            STOP.wait(self.tick)


LOCK_WAIT = 30                                 # secondes d'attente du verrou au démarrage


def main():
    os.chdir(ROOT)
    os.makedirs(LOGS, exist_ok=True)
    lock = acquire_single_instance(os.path.join(LOGS, "runner.lock"))
    for _ in range(LOCK_WAIT):                 # ancien runner encore en train de s'arrêter (redémarrage rapide) : on l'attend un peu
        if lock is not None:
            break
        time.sleep(1)
        lock = acquire_single_instance(os.path.join(LOGS, "runner.lock"))
    if lock is None:
        log("❌ un autre termux_runner.py tourne déjà (verrou logs/runner.lock) : arrêt", err=True)
        return 1
    log("🔥 Démarrage de Kelly sur GrapheneOS / Termux..."
        + (" (wake-lock demandé)" if os.environ.get("KELLY_WAKELOCK") == "1" else " (sans wake-lock : le téléphone peut mettre Kelly en veille)"))
    kill_stale_serve(os.path.join(LOGS, "serve.pid"))
    runner = Runner()
    atexit.register(runner.shutdown)
    install_signals()
    try:
        runner.loop()
    finally:
        if SIGNALS:
            log(f"signal {signal.Signals(SIGNALS[0]).name} reçu : arrêt demandé")
        runner.shutdown()
        log("Kelly arrêtée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
