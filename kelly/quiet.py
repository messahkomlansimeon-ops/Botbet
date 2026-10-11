"""Heures calmes des alertes (C.QUIET_HOURS, « 23-7 » par défaut, heure d'Abidjan = UTC) : une alerte envoyée la nuit (💰 / ⚠️ / 📈, 🧮 stats ouvertes,
suivi des tickets, opportunités live) est gardée dans data/kelly.db (quiet_outbox.json) et partira au premier passage après la fin des heures calmes
(watch.tick toutes les 10 min, flush). Les réponses à tes commandes et le message du matin ne passent jamais par ici. Ne lève jamais."""
import sys
from datetime import datetime, timezone

from . import config as C, store, telegram

FILE = "quiet_outbox.json"      # [{"text", "html", "markup"}]


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


def send(msg, now=None):
    """Envoie l'alerte `msg`, ou la garde pour la fin des heures calmes. -> True si envoyée ou gardée."""
    if not is_quiet(now):
        return telegram.send(msg)
    try:
        held = store.load(FILE, [])
        held = held if isinstance(held, list) else []
        held.append({"text": str(msg), "html": isinstance(msg, telegram.Html), "markup": getattr(msg, "markup", None)})
        store.save(FILE, held[-30:])
        return True
    except Exception as e:                          # noqa: BLE001 - au pire elle part tout de suite
        print(f"[calme] alerte non gardée : {type(e).__name__}: {e}", file=sys.stderr)
        return telegram.send(msg)


def flush(now=None):
    """Hors des heures calmes : envoie les alertes gardées pendant la nuit (dans l'ordre), précédées d'un mot. -> nombre envoyé."""
    if is_quiet(now):
        return 0
    held = store.load(FILE, [])
    if not isinstance(held, list) or not held:
        return 0
    store.save(FILE, [])
    telegram.send(f"🌙 {len(held)} alerte{'s' if len(held) > 1 else ''} de la nuit, gardée{'s' if len(held) > 1 else ''} pendant les heures calmes :")
    for h in held:
        text = telegram.Html(h["text"]) if h.get("html") else h["text"]
        telegram.send(telegram.with_buttons(text, h["markup"]) if h.get("markup") else text)
    return len(held)
