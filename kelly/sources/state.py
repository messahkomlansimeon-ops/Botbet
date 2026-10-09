import json
import time
from datetime import datetime, timedelta, timezone

from .. import store

FILE = "sources_state.json"
BLOCK_HOURS = 24
CLOUDFLARE_HOURS = 24 * 7
LABELS = {"footballdata": "football-data", "understat": "Understat", "fbref": "FBref",
          "fotmob": "FotMob", "betexplorer": "BetExplorer", "xbet": "1xBet", "s365": "365scores"}
MAX_PROFILE_BYTES = 450_000
clock = time.time


def now():
    return datetime.fromtimestamp(clock(), tz=timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _all():
    d = store.load(FILE, {})
    return d if isinstance(d, dict) else {}


def _put(name, **kw):
    d = _all()
    cur = d.get(name) if isinstance(d.get(name), dict) else {}
    cur.update(kw)
    d[name] = cur
    store.save(FILE, d)


_priority = set()                  # clés (ligues) à actualiser en premier dans ce passage (sources.refresh_all)


def set_priority(keys=()):
    """Ligues qui jouent aujourd'hui ou demain : les sources à budget de temps (FotMob, BetExplorer) les téléchargent d'abord."""
    _priority.clear()
    _priority.update(keys or ())


def priority():
    return frozenset(_priority)


def order(keys, age=None):
    """`keys` (ligues d'une source) dans l'ordre où les actualiser quand le délai du passage ne suffit pas pour toutes : d'abord celles qui
    jouent bientôt (set_priority), puis la page la plus ANCIENNE en premier (`age(clé)` : secondes depuis son dernier téléchargement,
    None = jamais téléchargée = la plus ancienne) ; à égalité l'ordre d'origine (catalogue). Le début de liste change ainsi d'un jour à
    l'autre : la fin de liste n'est pas condamnée à rester périmée."""
    keys = list(keys)
    rank = {k: i for i, k in enumerate(keys)}

    def old(k):
        a = age(k) if age else None
        return float("inf") if a is None else a
    return sorted(keys, key=lambda k: (k not in _priority, -old(k), rank[k]))


def health(name):
    s = {"ok": None, "last_success": None, "last_error": None, "rows": 0, "blocked_until": None}
    cur = _all().get(name)
    if isinstance(cur, dict):
        s.update({k: cur[k] for k in s if k in cur})
    if s["blocked_until"]:
        try:
            if datetime.fromisoformat(s["blocked_until"]) <= now():
                s["blocked_until"] = None
        except ValueError:
            s["blocked_until"] = None
    return s


def stale_count(name):
    """Pages que le dernier passage de la source `name` a servies depuis un cache périmé (téléchargement reporté ou refusé) ; 0 sinon."""
    cur = _all().get(name)
    n = cur.get("stale") if isinstance(cur, dict) else 0
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def is_blocked(name):
    return bool(health(name)["blocked_until"])


def block(name, reason, cloudflare=False):
    until = _iso(now() + timedelta(hours=CLOUDFLARE_HOURS if cloudflare else BLOCK_HOURS))
    when = until[:16].replace("T", " ")
    if cloudflare:
        msg = f"{LABELS.get(name, name)} bloque les robots (Cloudflare) : source désactivée jusqu'au {when} UTC"
    else:
        msg = f"bloquée par le site ({reason}), pas de nouvelle requête avant {when} UTC"
    _put(name, ok=False, blocked_until=until, last_error=msg)


def record_run(name, rows, errors, stale=0):
    """Enregistre le passage d'une source. `stale` : nombre de pages servies depuis un cache périmé (téléchargement impossible ou reporté :
    délai épuisé, accès refusé) ; l'appelant en met aussi le détail en tête de `errors`, pour que le rapport et --check le disent."""
    errors = [e for e in dict.fromkeys(errors) if e]
    blocked = is_blocked(name)
    kw = {"rows": int(rows), "ok": rows > 0 and not blocked, "stale": int(stale)}
    if blocked:
        pass
    elif errors:
        kw["last_error"] = " ; ".join(errors[:3]) + (f" (+{len(errors) - 3})" if len(errors) > 3 else "")
    else:
        kw["last_error"] = None
    if rows > 0:
        kw["last_success"] = _iso(now())
    _put(name, **kw)
    return health(name)


def save_profile(filename, obj):
    store.save(filename, obj)
    return len(json.dumps(obj, ensure_ascii=False).encode())
