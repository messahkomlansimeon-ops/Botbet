"""Verrou « un seul passage de ce nom à la fois » (fcntl.flock non bloquant sur un fichier du dossier de données) : le matin ne peut pas être lancé deux fois en même temps (commande
Telegram « matin », runner, ligne de commande). Le verrou disparaît avec le processus (même tué). `hold(nom)` -> True si pris ; `busy(nom)` -> un autre processus le tient."""
import contextlib
import os

from . import store


def _file(name):
    return store.path(f"{name}.lock")


@contextlib.contextmanager
def hold(name):
    """with hold("daily") as got : got est vrai si le verrou est pris (et gardé jusqu'à la sortie du bloc), faux si un autre processus le tient. Ne lève jamais (pas de fcntl : toujours vrai)."""
    try:
        import fcntl
    except ImportError:                             # pas de fcntl (hors Linux / Termux) : pas de verrou
        yield True
        return
    os.makedirs(os.path.dirname(_file(name)), exist_ok=True)
    fd = os.open(_file(name), os.O_CREAT | os.O_RDWR, 0o644)
    got = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def busy(name):
    """Un autre processus tient-il le verrou `name` ? (Essaie de le prendre et le relâche aussitôt.)"""
    with hold(name) as got:
        return not got
