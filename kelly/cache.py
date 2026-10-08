"""Caches et compteurs : ne jamais redemander à une API ce qu'on a déjà.

1. Compteurs par source (réutilisé / appelé, crédits ou requêtes économisés) -> ligne « ♻️ Cache » du matin et de --check.
   Le cumul du jour est gardé dans data/cache_stats.json (--check le relit : un nouveau processus repart de zéro).
2. Cache disque des réponses de The Odds API (.cache/odds, gzip, avec l'heure du relevé) : un second passage du matin
   (relance manuelle) relit les cotes du jour sans dépenser un crédit.
3. Mode hors ligne (`offline()`) : les commandes Telegram n'ont droit à aucun appel réseau, payant ou non.
"""
import contextlib
import gzip
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import config as C, store

STATS_FILE = "cache_stats.json"
LABELS = {"odds": "Odds API", "events": "Odds API (gratuit)", "api-football": "API-Football", "footballdata": "football-data",
          "understat": "Understat", "fbref": "FBref"}
UNITS = {"odds": "crédits"}                      # les autres : requêtes
COUNTS = {}                                      # source -> {hit, miss, saved, spent}
OFFLINE = False
clock = time.time                                # remplaçable dans les tests


class Offline(Exception):
    """Appel réseau refusé en mode hors ligne."""


def reset():
    COUNTS.clear()


def _c(src):
    return COUNTS.setdefault(src, {"hit": 0, "miss": 0, "saved": 0, "spent": 0})


def hit(src, saved=1):
    """Une réponse relue du cache : `saved` crédits / requêtes évités."""
    c = _c(src)
    c["hit"] += 1
    c["saved"] += saved


def miss(src, spent=1):
    """Un vrai appel réseau (`spent` crédits / requêtes)."""
    c = _c(src)
    c["miss"] += 1
    c["spent"] += spent


@contextlib.contextmanager
def offline():
    """Aucun appel réseau dans ce bloc (commandes Telegram : elles ne lisent que les données du matin)."""
    global OFFLINE
    old, OFFLINE = OFFLINE, True
    try:
        yield
    finally:
        OFFLINE = old


def check_online(what):
    if OFFLINE:
        raise Offline(f"{what} : réseau interdit hors des passages programmés")


# ---------- compteurs : texte et cumul du jour ----------

def _today():
    return datetime.fromtimestamp(clock(), tz=timezone.utc).date().isoformat()


def flush():
    """Ajoute les compteurs de ce passage au cumul du jour (data/cache_stats.json), puis les remet à zéro."""
    if not COUNTS:
        return
    cur = store.load(STATS_FILE, {})
    if not isinstance(cur, dict) or cur.get("day") != _today():
        cur = {"day": _today(), "sources": {}}
    for src, c in COUNTS.items():
        t = cur["sources"].setdefault(src, {"hit": 0, "miss": 0, "saved": 0, "spent": 0})
        for k in t:
            t[k] += c[k]
    store.save(STATS_FILE, cur)
    COUNTS.clear()


def today_totals():
    """Cumul du jour (passages déjà terminés) + passage en cours."""
    cur = store.load(STATS_FILE, {})
    out = {k: dict(v) for k, v in (cur.get("sources", {}) if isinstance(cur, dict) and cur.get("day") == _today() else {}).items()}
    for src, c in COUNTS.items():
        t = out.setdefault(src, {"hit": 0, "miss": 0, "saved": 0, "spent": 0})
        for k in t:
            t[k] += c[k]
    return out


def line(totals=None):
    """« ♻️ Cache : Odds API 3 relues / 2 appels (14 crédits évités) · API-Football 12 / 3 (12 requêtes évitées) »."""
    totals = COUNTS if totals is None else totals
    parts = []
    for src, c in totals.items():
        if not (c["hit"] or c["miss"]):
            continue
        unit = UNITS.get(src, "requêtes")
        saved = f" ({c['saved']} {unit} évités)" if c["saved"] else ""
        parts.append(f"{LABELS.get(src, src)} {c['hit']} en cache / {c['miss']} appel{'s' if c['miss'] > 1 else ''}{saved}")
    return "♻️ Cache : " + (" · ".join(parts) if parts else "aucun appel réseau évité ni fait")


# ---------- cache disque des réponses d'API ----------

def odds_dir():
    return os.environ.get("ODDS_CACHE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "odds")


def _path(key):
    return os.path.join(odds_dir(), hashlib.sha1(key.encode()).hexdigest()[:24] + ".json.gz")


def load(key, max_age_hours):
    """(données, méta) d'une réponse mise en cache il y a moins de `max_age_hours`, sinon None. Ne lève jamais."""
    try:
        with gzip.open(_path(key), "rt", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError, EOFError):
        return None
    if obj.get("key") != key or clock() - obj.get("fetched_at", 0) > max_age_hours * 3600:
        return None
    return obj.get("data"), {k: v for k, v in obj.items() if k not in ("data", "key")}


def save(key, data, **meta):
    """Écrit une réponse dans le cache disque (échec silencieux : le cache est facultatif). Nettoie les vieux fichiers."""
    try:
        os.makedirs(odds_dir(), exist_ok=True)
        tmp = _path(key) + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"key": key, "fetched_at": clock(), "data": data} | meta, f)
        os.replace(tmp, _path(key))
        prune()
    except OSError:
        pass


def prune(days=2):
    """Supprime les réponses de plus de `days` jours : le dossier ne grossit pas."""
    limit = clock() - days * 86400
    try:
        for name in os.listdir(odds_dir()):
            p = os.path.join(odds_dir(), name)
            if os.path.getmtime(p) < limit:
                os.remove(p)
    except OSError:
        pass
