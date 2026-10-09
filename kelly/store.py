"""Stockage JSON dans data/ (DATA_DIR pour les tests)."""
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def path(name):
    return os.path.join(DATA_DIR, name)


def load(name, default):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, obj, compact=False):
    """Écrit data/<name> ; `compact` : une seule ligne, sans espace (gros fichiers de profils). Écriture atomique : fichier temporaire
    puis remplacement, un lecteur (le bot Telegram pendant un passage programmé) ne voit jamais un fichier à moitié écrit."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = f"{path(name)}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, **({"separators": (",", ":")} if compact else {"indent": 0}))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path(name))


class LockTimeout(Exception):
    """Le verrou des données est resté pris plus longtemps que prévu."""


def lock_path():
    """Fichier de verrou : le MÊME que celui des unités systemd (`flock $XDG_RUNTIME_DIR/kelly.lock python bot.py --daily`) ;
    sans XDG_RUNTIME_DIR (tests, autre machine) : data/.kelly.lock. flock(1) et fcntl.flock utilisent tous deux flock(2)."""
    run = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(run, "kelly.lock") if run else os.path.join(DATA_DIR, ".kelly.lock")


@contextmanager
def data_lock(wait=None, on_busy=None):
    """Verrou exclusif sur les données (bets.json, tracked.json...) : le bot Telegram écrit pendant que le matin ou le relevé horaire
    tournent, et l'un écraserait l'autre (mise à jour perdue). `wait` : secondes d'attente au plus (None = sans limite, 0 = ne pas
    attendre) ; dépassé -> LockTimeout. `on_busy()` est appelé une fois si le verrou est déjà pris, avant d'attendre. Le verrou se
    libère en sortant (et si le processus meurt). Ne l'utilise pas autour d'un code déjà lancé sous `flock` : inter-blocage."""
    os.makedirs(os.path.dirname(lock_path()), exist_ok=True)
    try:
        f = open(lock_path(), "a")
    except OSError as e:                            # verrou impossible : mieux vaut travailler sans que ne plus répondre
        print(f"[store] verrou indisponible ({e}) : travail sans verrou", file=sys.stderr)
        yield
        return
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if wait == 0:
                raise LockTimeout(lock_path()) from None
            if on_busy:
                on_busy()
            if wait is None:
                fcntl.flock(f, fcntl.LOCK_EX)
            else:
                end = time.monotonic() + wait
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            raise LockTimeout(lock_path()) from None
                        time.sleep(0.25)
        yield
    finally:
        f.close()                                   # libère le verrou
