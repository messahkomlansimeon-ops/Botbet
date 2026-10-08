import json
import time
from datetime import datetime, timedelta, timezone

from .. import store

FILE = "sources_state.json"
BLOCK_HOURS = 24
CLOUDFLARE_HOURS = 24 * 7
LABELS = {"footballdata": "football-data", "understat": "Understat", "fbref": "FBref",
          "fotmob": "FotMob", "betexplorer": "BetExplorer"}
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


def record_run(name, rows, errors):
    errors = [e for e in dict.fromkeys(errors) if e]
    blocked = is_blocked(name)
    kw = {"rows": int(rows), "ok": rows > 0 and not blocked}
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
