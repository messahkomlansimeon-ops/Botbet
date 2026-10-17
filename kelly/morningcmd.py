"""Commande Telegram « matin » : relance le passage du matin (`bot.py --daily`) s'il n'est pas fait, le refait avec « matin force ».

Le processus Telegram (`--serve`) ne lance pas le matin lui-même : il dépose une DEMANDE (data/daily_request.json, fichier simple) que le runner (termux_runner.py) lit à chaque tour et lance
(`--daily`, plus `--force` pour « matin force »). Le runner n'a qu'une demande à la fois et un seul passage à la fois ; en plus, `bot.py --daily` prend le verrou « daily » (kelly/passlock.py) : deux
lancements simultanés (commande + runner + ligne de commande) ne se doublent jamais. « force » : les combinés déjà enregistrés du jour sont renvoyés tels quels, sans doublon (app._resend_real /
_resend_fair, relance du même jour)."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import passlock, store

REQUEST = "daily_request.json"
LOCK = "daily"
STALE_MIN = 45                          # une demande que le runner n'a pas prise en 45 min (runner arrêté) ne bloque plus


def _now():
    return datetime.now(timezone.utc)


def read_request():
    """La demande en attente {at, force} ou None (absente, illisible, périmée)."""
    try:
        with open(store.path(REQUEST), encoding="utf-8") as f:
            d = json.load(f)
        at = datetime.fromisoformat(d["at"])
        if _now() - at > timedelta(minutes=STALE_MIN):
            return None
        return {"at": d["at"], "force": bool(d.get("force"))}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_request(force):
    """Dépose la demande de façon atomique (fichier temporaire puis remplacement) ; jamais deux demandes : « force » l'emporte."""
    cur = read_request()
    force = bool(force or (cur and cur["force"]))
    tmp = store.path(REQUEST + ".tmp")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": _now().isoformat(timespec="seconds"), "force": force}, f)
    os.replace(tmp, store.path(REQUEST))
    return force


def take_request(path=None):
    """Lue par le runner : rend la demande et la supprime (une seule fois) ; None sans demande valide."""
    p = path or store.path(REQUEST)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        os.remove(p)
        at = datetime.fromisoformat(d["at"])
        return {"force": bool(d.get("force"))} if _now() - at <= timedelta(minutes=STALE_MIN) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def hm(iso):
    try:
        return datetime.fromisoformat(str(iso)).astimezone(timezone.utc).strftime("%Hh%M")
    except (TypeError, ValueError):
        return "?"


def handle(args, now=None):
    """Réponse à « matin » / « matin force » (texte brut). Ne lève jamais."""
    try:
        from . import app
        now = now or app.now_utc()
        force = any(str(a).lower().strip("-/") in ("force", "forcer") for a in args)
        done = store.load(app.DAILY_FILE, {})
        req = read_request()
        if passlock.busy(LOCK):
            return "Le matin tourne déjà (environ 30 min) : les messages arrivent au fil du passage."
        if req:
            return f"Le matin est déjà demandé à {hm(req['at'])}{' (force)' if req['force'] else ''} : il démarre au prochain tour du runner (quelques secondes)."
        if app.daily_skip_reason(now) is None:
            write_request(False)
            return "🌅 Je lance le matin maintenant (environ 30 min). Les messages arrivent au fil du passage."
        if not force:
            return f"Le matin est déjà fait à {hm((done or {}).get('at'))}, écris « matin force » pour le refaire."
        write_request(True)
        return ("🌅 Je refais le matin (force) : les combinés déjà enregistrés aujourd'hui sont renvoyés tels quels, sans doublon. "
                "Environ 30 min.")
    except Exception as e:                          # noqa: BLE001
        print(f"[matin] commande ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return "Je n'ai pas pu lancer le matin (voir « erreurs »)."
