"""Orchestrateur tout-en-un pour Termux (Android / GrapheneOS).
Remplace systemd : garde `bot.py --serve` en vie (relance avec attente croissante) et planifie les passages périodiques
(--watch, --track, --daily, --xbet, --evening, --export, --tune). Une seule instance à la fois (verrou logs/runner.lock) ;
SIGTERM / SIGHUP / Ctrl+C arrêtent proprement le bot ; chemins absolus (peut être lancé depuis n'importe quel dossier).
Tout est en UTC. L'horaire est décidé par `due_tasks` (fonction pure, testée dans tests/test_runner_fixes.py)."""
import atexit
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(ROOT, "bot.py")
LOGS = os.path.join(ROOT, "logs")

DAILY_HOUR = 4               # passage du matin dès 04:00 UTC (= C.DAY_END_HOUR) ; rattrapage ensuite, une seule fois par jour
DAILY_LAST_HOUR = 20         # après cette heure on n'ouvre plus de rattrapage du matin (attendre le lendemain)
DAILY_MAX_TRIES = 3          # essais du matin par jour (échec, délai dépassé) ...
DAILY_RETRY_GAP = 1800       # ... espacés d'au moins 30 min
WATCH_EVERY = 600
EXPORT_HOUR = 23             # sauvegarde lisible de la base (data/sauvegarde/)
BACKUP_HOUR = 12             # sauvegarde de la base envoyée sur Telegram (téléphone perdu ou en panne), sans sonnerie, dès 12:00 UTC ...
BACKUP_EVERY_DAYS = 2        # ... tous les 2 jours ...
BACKUP_RETRY = 3600          # ... retentée toutes les heures si l'envoi échoue
TUNE_WEEKDAY, TUNE_HOURS = 6, (2, 3)    # dimanche 02:00-03:59 UTC (avant le matin ; --tune reprend où il s'est arrêté)

# délai maximal par passage (s) : un appel réseau figé ne bloque plus tout le runner (--daily dure ~30 min, attente du verrou comprise)
TIMEOUTS = {"--watch": 540, "--track": 1500, "--daily": 5400, "--xbet": 1500, "--evening": 1500, "--export": 900, "--tune": 2400, "--backup-auto": 600}

HC_EVERY = 300               # signal de vie à healthchecks.io (HC_PING_URL dans .env) toutes les 5 min, PAR LE PROXY (ton IP n'est jamais révélée) ...
SERVE_DOWN_FAIL = 900        # ... « en panne » (/fail) si l'écoute Telegram est arrêtée depuis 15 min : healthchecks te prévient sur Telegram par son propre bot
PROXY_FILE = os.path.expanduser("~/.config/scrap_stat/sofa.env")
HEALTH_EVERY = 60            # surveillance du runner (alertes Telegram, une fois chacune) : écoute plantée, disque, base, alertes en attente
DISK_LOW_MB, DISK_OK_MB = 200, 400       # place libre du téléphone : alerte sous 200 Mo, « libérée » au-dessus de 400 Mo
DBCHECK_HOUR = 3             # contrôle quotidien de la base (PRAGMA quick_check) dès 03:00 UTC, avant le matin
BACKUP_FAILS_ALERT = 6       # sauvegardes Telegram ratées de suite (une par heure) avant de prévenir

BACKOFF_BASE, BACKOFF_CAP, STABLE_SECS = 15, 900, 120      # relance de --serve : 15 s, 30 s, 1 min ... 15 min ; stable après 2 min de vie
STOP = threading.Event()
SIGNALS = []                 # signaux reçus (le journal s'écrit hors du gestionnaire)


def log(msg, err=False):
    print(f"[termux {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


LOG_MAX = 5_000_000              # journal du runner (KELLY_LOG, logs/termux.log) au-delà de 5 Mo : copié en .1 puis vidé (rotate_log)


def rotate_log(path=None, limit=LOG_MAX):
    """Journal trop gros : copié en « .1 » (l'ancien .1 est remplacé) puis vidé sur place. Le script kelly l'ouvre en ajout (>>) : le runner continue
    d'écrire au début du fichier vidé. -> vrai si le journal a été tourné. Ne lève jamais."""
    path = path or os.environ.get("KELLY_LOG")
    if not path:
        return False
    try:
        if os.path.getsize(path) <= limit:
            return False
        shutil.copyfile(path, path + ".1")
        with open(path, "r+", encoding="utf-8") as f:
            f.truncate(0)
        return True
    except OSError:
        return False


def _safe_log(msg):
    try:
        log(msg, err=True)
    except Exception:                                   # journal impossible à écrire (disque plein) : on continue quand même
        pass


# ---------- décisions (pures) ----------

def new_state():
    return {"watch_at": 0.0, "track_key": "", "daily_ok_day": "", "daily_try_day": "", "daily_tries": 0, "daily_try_at": 0.0,
            "xbet_day": "", "evening_day": "", "export_day": "", "tune_day": "", "backup_day": "", "backup_try": 0.0,
            "backup_fails": 0, "dbcheck_day": "", "said": {}, "pending": {}, "hc_at": 0.0}


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
    if hour >= BACKUP_HOUR and backup_due(state, now) and epoch - state["backup_try"] >= BACKUP_RETRY:
        out.append(["--backup-auto"])
    if epoch - state["watch_at"] >= WATCH_EVERY:
        out.append(["--watch"])
    return out


def backup_due(state, now):
    """La sauvegarde sur Telegram est-elle due (jamais faite, ou la dernière réussie date d'au moins BACKUP_EVERY_DAYS jours) ?"""
    try:
        return (now.date() - date.fromisoformat(state["backup_day"])).days >= BACKUP_EVERY_DAYS
    except (TypeError, ValueError):
        return True


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
    elif name == "--backup-auto":                   # réussie : la prochaine dans BACKUP_EVERY_DAYS jours ; ratée : retentée dans l'heure
        state["backup_try"] = epoch
        if rc == 0:
            state["backup_day"], state["backup_fails"] = day, 0
        else:
            state["backup_fails"] += 1
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
        self.down_since = None                          # arrêtée (ou relancée en boucle sans tenir) depuis cet instant (clock) ; None : elle tourne
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
                if now - self.started >= STABLE_SECS:
                    self.fails, self.down_since = 0, None   # a tenu assez longtemps : on repart de zéro
                return
            if self.down_since is None:
                self.down_since = now
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


    def down_for(self):
        """Secondes depuis lesquelles l'écoute Telegram ne tourne pas (ou plante en boucle) ; 0 si elle tourne."""
        d = self.down_since
        return 0 if d is None else max(0.0, self.clock() - d)


# ---------- alertes Telegram du runner ----------

def telegram_say(text, env=os.environ, post=None):
    """Message Telegram envoyé DIRECTEMENT par le runner (sans le code de Kelly : marche même si bot.py est cassé ou si l'écoute plante) -> True s'il est
    parti. Jamais l'adresse dans le journal (elle contient le token)."""
    token, chat = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        if post is None:
            import requests
            post = requests.post
        data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"} | ({"disable_notification": "true"} if quiet_now(env=env) else {})
        r = post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=(10, 30))
        return getattr(r, "status_code", 200) < 400
    except Exception:                                   # noqa: BLE001 - réseau : l'alerte reste en attente (Runner.alert)
        return False


def quiet_now(now=None, env=os.environ):
    """Heures calmes (QUIET_HOURS, « 23-7 » par défaut, heure d'Abidjan = UTC ; « off » : jamais) : les alertes du runner arrivent alors sans sonnerie."""
    spec = (env.get("QUIET_HOURS") or "23-7").strip().lower()
    try:
        a, b = (int(x) for x in spec.split("-"))
    except ValueError:
        return False
    h = (now or datetime.now(timezone.utc)).hour
    return a <= h or h < b if a > b else a <= h < b


def daily_why(rc):
    """Raison d'un passage du matin raté, en mots simples."""
    return {124: "trop long, interrompu au bout de 90 min", 75: "liste des matchs injoignable", 143: "arrêté en route"}.get(rc, f"erreur, code {rc}")


def disk_free_mb(path=ROOT):
    try:
        return shutil.disk_usage(path).free / 1e6
    except OSError:
        return None


def db_check(path=None):
    """Contrôle rapide de la base (PRAGMA quick_check, lecture seule) -> None si elle va bien (ou n'existe pas), sinon ce qui ne va pas."""
    import sqlite3
    path = path or os.path.join(os.environ.get("DATA_DIR") or os.path.join(ROOT, "data"), "kelly.db")
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        try:
            res = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        return None if "locked" in str(e) else str(e)[:120]       # occupée : on réessaiera
    except sqlite3.Error as e:
        return str(e)[:120]
    return None if res and res[0] == "ok" else str(res[0] if res else "contrôle impossible")[:120]


# ---------- signal de vie (healthchecks.io) ----------

def proxy_url(env=os.environ, path=PROXY_FILE):
    """Proxy Webshare de 1xBet (comme kelly.sources.xbet.proxy) : XBET_PROXY, sinon la ligne XBET_PROXY= du fichier de scrap_stat ; None sinon."""
    if env.get("XBET_PROXY"):
        return env["XBET_PROXY"].strip()
    try:
        with open(env.get("XBET_PROXY_FILE") or path, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == "XBET_PROXY" and v.strip():
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


class Heartbeat:
    """Signal de vie envoyé à healthchecks.io toutes les HC_EVERY s (fil à part : jamais retardé par un passage de 30 min). Sans signal pendant la
    « période + délai de grâce » réglés sur le site (15 + 45 min conseillés), healthchecks te prévient : téléphone éteint, sans réseau, Termux tué,
    Kelly arrêtée. L'écoute Telegram arrêtée depuis SERVE_DOWN_FAIL s : signal « en panne » (/fail) avec la raison, tout de suite. TOUJOURS par le proxy
    (jamais en direct : ton IP n'est pas révélée) ; sans proxy ou sans HC_PING_URL : rien n'est envoyé. L'adresse n'est jamais écrite dans le journal."""

    def __init__(self, url, serve, proxy=proxy_url, post=None, on_ok=None):
        self.url, self.serve, self.proxy, self._post, self.on_ok = (url or "").strip().rstrip("/"), serve, proxy, post, on_ok
        self.failing, self.said = False, set()

    def _say_once(self, key, msg):
        if key not in self.said:
            self.said.add(key)
            log(msg, err=True)

    def status(self):
        """(en panne ?, texte envoyé avec le signal)."""
        if not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
            return True, "TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID absent du .env : Kelly ne peut rien t'envoyer"
        down = self.serve.down_for()
        if down >= SERVE_DOWN_FAIL:
            return True, f"écoute Telegram arrêtée depuis {down / 60:.0f} min (plantages répétés : kelly logs)"
        return False, "ok · écoute Telegram active" if self.serve.proc is not None else "ok"

    def beat(self, force=None):
        """Envoie un signal (succès ou /fail) -> True s'il est parti. `force` : (en panne ?, texte) imposés (kelly test-alerte)."""
        if not self.url:
            return False
        px = self.proxy()
        if not px:
            self._say_once("proxy", "signal de vie non envoyé : proxy introuvable (jamais en direct)")
            return False
        fail, text = force or self.status()
        try:
            post = self._post
            if post is None:
                import requests
                post = requests.post
            r = post(self.url + ("/fail" if fail else ""), data=text.encode("utf-8"), proxies={"http": px, "https": px}, timeout=(10, 20))
            ok = getattr(r, "status_code", 200) < 400
        except Exception as e:                          # noqa: BLE001 - réseau, proxy : on réessaie au prochain signal
            ok = False
            self._say_once("err", f"signal de vie non envoyé ({type(e).__name__}) ; nouvel essai toutes les {HC_EVERY // 60} min")
        if ok:
            self.said.discard("err")
            if self.on_ok:
                self.on_ok()
            if fail != self.failing:
                log(("⚠️ signal « en panne » envoyé à healthchecks : " + text) if fail else "signal de vie : de nouveau « ok »", err=fail)
            self.failing = fail
        return ok

    def loop(self, every=HC_EVERY):
        while True:
            try:
                self.beat()
            except Exception as e:                      # noqa: BLE001 - le fil ne meurt jamais
                _safe_log(f"signal de vie : {type(e).__name__}: {e}")
            if STOP.wait(every):
                return


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
    def __init__(self, serve_cmd=None, tick=15, run=run_cmd, plan=due_tasks, state=None, watch_every=5, watch_thread=True):
        os.makedirs(LOGS, exist_ok=True)
        self.serve = ServeSupervisor(serve_cmd or [sys.executable, "-u", BOT, "--serve"], pidfile=os.path.join(LOGS, "serve.pid"))
        self.tick, self.run, self.plan, self.watch_every = tick, run, plan, watch_every
        self.state = state if state is not None else load_state()
        self.watch_thread = watch_thread                # suivi des matchs (--watch) dans son propre fil : jamais bloqué par --track, --daily, --xbet...
        self.state_lock = threading.Lock()
        self.heartbeat = Heartbeat(os.environ.get("HC_PING_URL"), self.serve, on_ok=self._hc_ok)
        self.say = telegram_say                         # alertes du runner (remplaçable : tests)
        self.disk, self.dbcheck = disk_free_mb, db_check
        self._disk_at = 0.0

    def _hc_ok(self):
        """Dernier signal de vie parti (montré par 🩺 status)."""
        with self.state_lock:
            self.state["hc_at"] = time.time()
            save_state(self.state)

    def alert(self, key, text):
        """Alerte Telegram UNE fois par `key` (runner_state.json « said ») ; réseau coupé : gardée (« pending ») et renvoyée au prochain contrôle."""
        with self.state_lock:
            if key in self.state["said"] or key in self.state["pending"]:
                return
            self.state["pending"][key] = text
            save_state(self.state)
        log(f"alerte : {text[:120]}", err=True)
        self.flush_alerts()

    def flush_alerts(self):
        with self.state_lock:
            todo = list(self.state["pending"].items())
        for key, text in todo:
            if not self.say(text):
                return                                  # réseau ou Telegram en panne : on réessaie au prochain contrôle
            with self.state_lock:
                self.state["pending"].pop(key, None)
                self.state["said"][key] = time.time()
                cut = time.time() - 30 * 86400
                self.state["said"] = {k: v for k, v in self.state["said"].items() if v >= cut}
                save_state(self.state)

    def forget(self, key):
        """`key` n'est plus en cours (l'écoute est revenue, la place est libérée) : True s'il avait été annoncé."""
        with self.state_lock:
            was = self.state["said"].pop(key, None) is not None or self.state["pending"].pop(key, None) is not None
            if was:
                save_state(self.state)
        return was

    def check_health(self, now=None):
        """Un contrôle (toutes les HEALTH_EVERY s, fil à part) : alertes en attente, écoute Telegram plantée depuis 15 min (puis « rétablie »), place
        libre du téléphone (toutes les 10 min), contrôle de la base une fois par jour dès DBCHECK_HOUR h UTC."""
        now = now or datetime.now(timezone.utc)
        self.flush_alerts()
        down = self.serve.down_for()
        if down >= SERVE_DOWN_FAIL:
            self.alert("serve", f"⚠️ Kelly : l'écoute Telegram plante en boucle depuis {down / 60:.0f} min, tes commandes et boutons ne sont pas lus. "
                                "Les passages (matin, suivi des matchs) continuent. Détail : kelly logs (dans Termux).")
        elif down == 0 and self.forget("serve"):
            self.alert(f"serve-ok|{now:%Y-%m-%d %H:%M}", "✅ Kelly : l'écoute Telegram est rétablie, tes commandes sont de nouveau lues.")
        if time.time() - self._disk_at >= 600:
            self._disk_at = time.time()
            free = self.disk()
            if free is not None and free < DISK_LOW_MB:
                self.alert("disk", f"⚠️ Kelly : plus que {free:.0f} Mo de libre sur le téléphone, elle risque de ne plus pouvoir écrire ses données. "
                                   "Libère de la place (photos, vidéos, applis).")
            elif free is not None and free > DISK_OK_MB and self.forget("disk"):
                self.alert(f"disk-ok|{now:%Y-%m-%d}", f"✅ Kelly : place libérée sur le téléphone ({free:.0f} Mo libres).")
        day = str(now.date())
        if now.hour >= DBCHECK_HOUR and self.state["dbcheck_day"] != day:
            bad = self.dbcheck()
            with self.state_lock:
                self.state["dbcheck_day"] = day
                save_state(self.state)
            if bad:
                self.alert(f"db|{day}", f"⚠️ Kelly : sa base de données est abîmée (contrôle du jour : {bad}). Restaure la dernière sauvegarde reçue ici : "
                                        "kelly restaurer (dans Termux).")

    def health_loop(self, every=HEALTH_EVERY):
        while not STOP.wait(every):
            try:
                self.check_health()
            except Exception as e:                      # noqa: BLE001 - le fil ne meurt jamais
                _safe_log(f"surveillance : {type(e).__name__}: {e}")

    def _done(self, args, now, rc):
        with self.state_lock:
            mark_done(self.state, args, now, time.time(), rc)
            save_state(self.state)
            st = dict(self.state)
        day = str(now.date())
        if args[0] == "--daily" and rc != 0 and st["daily_ok_day"] != day and st["daily_tries"] >= DAILY_MAX_TRIES:
            self.alert(f"daily|{day}", f"⚠️ Kelly : le passage du matin du {now:%d/%m} a échoué {DAILY_MAX_TRIES} fois ({daily_why(rc)}) : pas de paris ce "
                                       "matin. Le suivi des matchs et tes commandes continuent. Pour le relancer : cd ~/kelly && python bot.py --daily --force")
        if args[0] == "--backup-auto" and st["backup_fails"] >= BACKUP_FAILS_ALERT:
            self.alert(f"backup|{st['backup_day'] or 'jamais'}", f"⚠️ Kelly : la sauvegarde sur Telegram ne part plus ({st['backup_fails']} essais ratés"
                                                                 f"{', dernière réussie le ' + st['backup_day'] if st['backup_day'] else ''}). Détail : kelly logs.")

    def _watch_wanted(self):
        """Un passage du suivi est-il demandé avant les 10 min (⏰ 5 min avant le 1er match d'un coupon) ? data/watch_next.json (watch.plan_next) donne
        l'instant voulu : dû s'il est passé et qu'aucun passage n'a commencé depuis. Fichier absent ou illisible : non."""
        try:
            with open(os.path.join(os.environ.get("DATA_DIR") or os.path.join(ROOT, "data"), "watch_next.json"), encoding="utf-8") as f:
                at = json.load(f).get("at")
            return isinstance(at, (int, float)) and self.state["watch_at"] < at <= time.time()
        except (OSError, ValueError, AttributeError):
            return False

    def watch_loop(self):
        """Toutes les WATCH_EVERY s, le suivi des matchs (--watch), en parallèle des autres passages : il ne prend pas le verrou des données (sa
        file de messages et ses compteurs s'écrivent de façon atomique), un --track de 25 min ou le matin ne le retardent plus."""
        while not STOP.is_set():
            try:
                with self.state_lock:
                    due = time.time() - self.state["watch_at"] >= WATCH_EVERY or self._watch_wanted()
                if due:
                    rc = self.run(["--watch"])
                    self._done(["--watch"], datetime.now(timezone.utc), rc)
            except Exception as e:                      # une erreur imprévue ne doit jamais arrêter le suivi des matchs en silence
                _safe_log(f"suivi des matchs : erreur inattendue ({type(e).__name__}: {e}), on continue")
            STOP.wait(min(self.tick, 15))

    def shutdown(self):
        self.serve.stop()

    def loop(self):
        threading.Thread(target=self.serve.watch, args=(self.watch_every,), daemon=True).start()
        if self.watch_thread:
            threading.Thread(target=self.watch_loop, daemon=True).start()
        if self.heartbeat.url:
            threading.Thread(target=self.heartbeat.loop, daemon=True).start()
        threading.Thread(target=self.health_loop, daemon=True).start()
        while not STOP.is_set():
            try:
                self._pass()
            except Exception as e:                      # une erreur imprévue (disque plein…) ne doit jamais arrêter Kelly en silence
                _safe_log(f"passage : erreur inattendue ({type(e).__name__}: {e}), on continue")
            STOP.wait(self.tick)

    def _pass(self):
        """Un tour de la boucle principale : journal tourné s'il est trop gros, surveille --serve, puis lance les passages dus (--daily, --track…)."""
        rotate_log()
        self.serve.check()
        now, epoch = datetime.now(timezone.utc), time.time()
        if now.hour >= DAILY_LAST_HOUR and self.state["daily_ok_day"] != str(now.date()) and self.state["daily_try_day"] != str(now.date()):
            log(f"⚠️ le matin du {now.date()} n'a pas tourné (téléphone éteint ?) ; prochain passage demain à {DAILY_HOUR:02d}:00 UTC", err=True)
            self.state["daily_try_day"], self.state["daily_tries"] = str(now.date()), DAILY_MAX_TRIES
            self.alert(f"nodaily|{now.date()}", f"⚠️ Kelly : le passage du matin du {now:%d/%m} n'a pas tourné (téléphone éteint ou Kelly arrêtée jusqu'à "
                                                f"{DAILY_LAST_HOUR}h UTC). Prochain passage demain à {DAILY_HOUR:02d}h00.")
        with self.state_lock:
            todo = [a for a in self.plan(now, self.state, epoch) if not (self.watch_thread and a[0] == "--watch")]
        for args in todo:
            if STOP.is_set():
                break
            if args[0] == "--daily" and now.hour > DAILY_HOUR:
                log(f"rattrapage du matin à {now:%H:%M} UTC (pas fait à {DAILY_HOUR:02d}:00)")
            rc = self.run(args)
            self._done(args, now, rc)
            self.serve.check()
            now = datetime.now(timezone.utc)


LOCK_WAIT = 30                                 # secondes d'attente du verrou au démarrage


def test_alert():
    """`kelly test-alerte` : envoie un signal « en panne » à healthchecks.io (par le proxy) pour vérifier que l'alerte arrive bien sur Telegram ; le
    prochain signal de vie de Kelly (5 min au plus) la remet « ok ». -> code de sortie."""
    from kelly import envfile
    envfile.load()
    if not os.environ.get("HC_PING_URL"):
        print("❌ HC_PING_URL absent du .env : voir DEPLOY_TERMUX.md (surveillance extérieure).")
        return 1
    hb = Heartbeat(os.environ["HC_PING_URL"], ServeSupervisor([]))
    if hb.beat(force=(True, "test de l'alerte (kelly test-alerte) : tout va bien, Kelly la remet « ok » dans 5 min")):
        print("✅ Signal « en panne » envoyé : l'alerte de healthchecks.io doit arriver sur Telegram d'ici 1 à 2 min, puis « ok » d'ici 5 min.")
        return 0
    print("❌ Signal non envoyé (proxy introuvable ou injoignable, adresse HC_PING_URL refusée) : voir kelly logs.")
    return 1


def main():
    if "--test-alerte" in sys.argv:
        os.chdir(ROOT)
        return test_alert()
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
    try:                                       # HC_PING_URL, XBET_PROXY... du fichier .env (sans écraser l'environnement)
        from kelly import envfile
        envfile.load()
    except Exception as e:                     # noqa: BLE001 - le runner tourne même si le paquet kelly est cassé
        log(f"⚠️ .env non lu ({type(e).__name__}: {e})", err=True)
    log("🔥 Démarrage de Kelly sur GrapheneOS / Termux..."
        + (" (wake-lock demandé)" if os.environ.get("KELLY_WAKELOCK") == "1" else " (sans wake-lock : le téléphone peut mettre Kelly en veille)"))
    kill_stale_serve(os.path.join(LOGS, "serve.pid"))
    runner = Runner()
    log("📡 Surveillance extérieure (healthchecks.io) : " + ("active, par le proxy" if runner.heartbeat.url else "non réglée (HC_PING_URL absent du .env)"))
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
