"""Heures calmes des alertes (C.QUIET_HOURS, « 23-7 » par défaut, heure d'Abidjan = UTC) : une alerte envoyée la nuit (💰 / ⚠️ / 📈, 🧮 stats ouvertes,
suivi des tickets, opportunités live) est gardée dans data/kelly.db (quiet_outbox.json) et partira au premier passage après la fin des heures calmes
(watch.tick toutes les 10 min, flush). Les réponses à tes commandes et le message du matin ne passent jamais par ici. Ne lève jamais."""
import html
import sys
from datetime import datetime, timezone

from . import config as C, store, telegram

FILE = "quiet_outbox.json"      # [{"text", "html", "markup", "kind": "suivi" | "autre", "at"}]


def _now():
    return datetime.now(timezone.utc)


def window():
    """(début, fin) des heures calmes en heures entières ; None si éteint (QUIET_HOURS vide ou « off »)."""
    raw = str(C.QUIET_HOURS or "").strip().lower()
    if not raw or raw in ("off", "0", "non"):
        return None
    try:
        a, b = (int(x) for x in raw.split("-"))
        return a % 24, b % 24
    except ValueError:
        return None


def is_quiet(now=None):
    w = window()
    if not w:
        return False
    h = (now or _now()).hour
    a, b = w
    return (a <= h or h < b) if a > b else (a <= h < b)


QUIET_MAX = 200                 # alertes « autre » gardées au plus pendant la nuit (les plus anciennes partent d'abord) ; le suivi des matchs n'est jamais jeté


def send(msg, now=None, kind="autre"):
    """Envoie l'alerte `msg`, ou la garde pour la fin des heures calmes. `kind` : « suivi » (suivi des matchs : jamais jeté, regroupé au réveil en un
    seul résumé) ou « autre ». Ajout atomique (store.update) : le suivi et le relevé horaire peuvent garder une alerte en même temps.
    -> True si envoyée ou gardée (ou mise en attente par Telegram)."""
    if not is_quiet(now):
        return telegram.send(msg)
    entry = {"text": str(msg), "html": isinstance(msg, telegram.Html), "markup": getattr(msg, "markup", None), "kind": kind, "silent": bool(getattr(msg, "silent", False)),
             **({"topic": msg.topic} if getattr(msg, "topic", None) else {}),
             "at": (now or _now()).isoformat(timespec="minutes")}

    def add(held):
        held = (held if isinstance(held, list) else []) + [entry]
        others = [i for i, h in enumerate(held) if h.get("kind") != "suivi"]
        drop = set(others[:max(0, len(others) - QUIET_MAX)])          # trop d'alertes « autre » : les plus anciennes partent
        if drop:
            print(f"[calme] {len(drop)} alerte(s) de la nuit trop ancienne(s) abandonnée(s)", file=sys.stderr)
        return [h for i, h in enumerate(held) if i not in drop], True
    try:
        return store.update(FILE, add, [])
    except Exception as e:                          # noqa: BLE001 - au pire elle part tout de suite
        print(f"[calme] alerte non gardée : {type(e).__name__}: {e}", file=sys.stderr)
        return telegram.send(msg)


def _hm(at):
    """« 02h10 » depuis l'horodatage ISO d'une alerte gardée ; '' s'il manque (ancienne entrée)."""
    try:
        return datetime.fromisoformat(str(at)).strftime("%Hh%M")
    except ValueError:
        return ""


def flush(now=None, follow_only=False):
    """Hors des heures calmes : envoie les alertes gardées pendant la nuit. Le suivi des matchs part en UN résumé (« 🌙 Tes matchs pendant la nuit »,
    chaque passage précédé de son heure), puis les autres alertes, dans l'ordre, précédées d'un mot. La file est vidée d'un coup (atomique) avant
    l'envoi : un message qui échoue est mis en attente par Telegram (data/outbox.json), jamais perdu. `follow_only` : à toute heure, seulement le
    suivi des matchs gardé (C.WATCH_QUIET éteint : le suivi ignore les heures calmes) ; les autres alertes restent pour le réveil. -> nombre envoyé."""
    if is_quiet(now) and not follow_only:
        return 0

    def take(h):
        h = h if isinstance(h, list) else []
        if not follow_only:
            return [], h
        return [x for x in h if x.get("kind") != "suivi"], [x for x in h if x.get("kind") == "suivi"]
    try:
        held = store.update(FILE, take, [])
    except OSError as e:
        print(f"[calme] alertes de la nuit non relues : {e}", file=sys.stderr)
        return 0
    if not held:
        return 0
    follow = [h for h in held if h.get("kind") == "suivi"]
    others = [h for h in held if h.get("kind") != "suivi"]
    if follow:
        parts = [(f"<b>{_hm(h.get('at'))}</b>\n" if _hm(h.get("at")) else "") + (h["text"] if h.get("html") else html.escape(h["text"], quote=False))
                 for h in follow]
        telegram.send(telegram.topic(telegram.Html("🌙 <b>Tes matchs pendant la nuit</b>\n\n" + "\n\n".join(parts)), "live"))
    if others:
        telegram.send(telegram.topic(f"🌙 {len(others)} alerte{'s' if len(others) > 1 else ''} de la nuit, gardée{'s' if len(others) > 1 else ''} pendant les heures "
                                     "calmes :", "alerts"), silent=all(h.get("silent") for h in others))      # l'en-tête sonne si une des alertes sonne
        for h in others:
            text = telegram.Html(h["text"]) if h.get("html") else h["text"]
            if h.get("markup"):
                text = telegram.with_buttons(text, h["markup"])
            if h.get("topic"):
                text = telegram.topic(text, h["topic"])
            telegram.send(text, silent=bool(h.get("silent")))
    return len(held)
