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
        force = any(str(a).lower().strip("-/").startswith("forc") for a in args)
        done = store.load(app.DAILY_FILE, {})
        req = read_request()
        if passlock.busy(LOCK):
            return "Le matin tourne déjà (environ 30 min) : les messages arrivent au fil du passage."
        if req:
            if force and not req["force"]:
                write_request(True)
                return "Le matin était déjà demandé : je le passe en « force » (combinés déjà enregistrés renvoyés sans doublon)."
            return (f"Le matin est déjà demandé à {hm(req['at'])}{' (force)' if req['force'] else ''} : il démarre dès que le runner le prend (quelques secondes ; "
                    "sans nouvelles, « status » dit si Kelly tourne).")
        reason = app.daily_skip_reason(now)
        if reason is None:
            write_request(False)
            return "🌅 Je lance le matin maintenant (environ 30 min). Les messages arrivent au fil du passage."
        day, at = (done or {}).get("day"), (done or {}).get("at")
        if now.hour < app.C.DAY_END_HOUR and day != str(now.date()):         # avant 04h UTC : le jour Kelly d'hier court encore ; refaire poserait « fait aujourd'hui » et ferait sauter le vrai matin
            return (f"Le matin du {day} (fait à {hm(at)} le {str(at)[8:10]}/{str(at)[5:7]}) couvre encore le jour en cours : le prochain matin est à {app.C.DAY_END_HOUR:02d}h00 UTC. "
                    "« matin force » n'est pas proposé avant cette heure.")
        if not force:
            return f"Le matin est déjà fait à {hm(at)}, écris « matin force » pour le refaire."
        write_request(True)
        return ("🌅 Je refais le matin (force) : les combinés déjà enregistrés sont renvoyés (ceux que tu as notés joués ou qui sont réglés restent tels quels), pas de doublon. "
                "Les questions et le bilan repartent aussi. Environ 30 min.")
    except Exception as e:                          # noqa: BLE001
        print(f"[matin] commande ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return "Je n'ai pas pu lancer le matin (voir « erreurs »)."
