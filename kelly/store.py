"""Stockage JSON dans data/ (DATA_DIR pour les tests)."""
import json
import os

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
    """Écrit data/<name> ; `compact` : une seule ligne, sans espace (gros fichiers de profils)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path(name), "w") as f:
        json.dump(obj, f, ensure_ascii=False, **({"separators": (",", ":")} if compact else {"indent": 0}))
