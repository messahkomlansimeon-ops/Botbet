"""Charge les clés depuis le fichier .env à la racine du projet.

Formats acceptés : NOM=valeur ou NOM:valeur (guillemets et espaces ignorés).
Une variable déjà définie (secret GitHub, terminal) a toujours la priorité.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path=None):
    path = path or os.path.join(ROOT, ".env")
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    loaded = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        cut = min((i for i in (line.find("="), line.find(":")) if i > 0), default=-1)
        if cut < 0:
            continue
        name, value = line[:cut].strip(), line[cut + 1:].strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return loaded
