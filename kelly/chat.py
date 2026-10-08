"""Kelly sans commandes : menu de boutons fixe, bouton « ✅ Je l'ai joué » sous les paris, texte libre (« bilan », « combi 20 »,
« Flamengo »...). Les commandes « / » restent valables : route() traduit le reste en commande « / », ou répond directement.

Pari en attente de cote : data/pending.json = {what, day, msg, at} après un appui sur ✅, valable PENDING_MINUTES ; la réponse
« 5,40 » (ou « 5,40 2000 » avec la mise) le note avec played.command, puis le bouton devient « ✅ Joué à 5,40 ».
"""
import re
from datetime import datetime, timedelta, timezone

from . import fmt, played, store, telegram

PENDING_FILE = "pending.json"
PENDING_MINUTES = 30
MENU = [["🔝 Paris du jour", "🎯 Un combiné"], ["📊 Mes résultats", "🧾 À noter"], ["❓ Aide"]]
BUTTONS = {"🔝 Paris du jour": "/top", "🎯 Un combiné": None, "📊 Mes résultats": "/bilan", "🧾 À noter": "/joue", "❓ Aide": "/aide"}
COMBI_TARGETS = (3, 5, 10, 20, 50)
NUM = re.compile(r"^\s*(\d+(?:[.,]\d+)?)(?:\s+(\d+(?:[.,]\d+)?))?\s*$")
WORDS = {"bilan": "/bilan", "resultats": "/bilan", "resultat": "/bilan", "top": "/top", "paris": "/top", "meilleurs": "/top",
         "aide": "/aide", "help": "/aide", "menu": "/aide", "?": "/aide", "start": "/aide", "bonjour": "/aide", "salut": "/aide",
         "joue": "/joue", "jouer": "/joue", "noter": "/joue", "bank": "/bank", "bankroll": "/bank", "monde": "/monde",
         "ligues": "/ligues", "stats": "/stats", "analyse": "/analyse", "match": "/match", "combi": "/combi", "combine": "/combi"}


def _now():
    return datetime.now(timezone.utc)


def menu():
    return telegram.menu_keyboard(MENU)


def played_button(what, day):
    """Bouton « ✅ Je l'ai joué » d'un pari du jour `day` (what : sur, costaud..., pari, v5...)."""
    return telegram.inline([[("✅ Je l'ai joué", f"joue|{what}|{str(day)[:10]}")]])


def combi_picker():
    return telegram.with_buttons("🎯 Quelle cote veux-tu pour ton combiné ?",
                                 telegram.inline([[(f"cote {t}", f"combi|{t}") for t in COMBI_TARGETS]]))


# ---------- pari en attente de cote ----------

def set_pending(what, day, msg):
    store.save(PENDING_FILE, {"what": what, "day": day, "msg": msg, "at": _now().isoformat(timespec="seconds")})


def get_pending():
    p = store.load(PENDING_FILE, {})
    try:
        if p and _now() - datetime.fromisoformat(p["at"]) <= timedelta(minutes=PENDING_MINUTES):
            return p
    except (KeyError, TypeError, ValueError):
        pass
    return None


def clear_pending():
    if store.load(PENDING_FILE, None):
        store.save(PENDING_FILE, {})


def _note(p, odds, stake):
    """La cote envoyée après un appui sur ✅ : notée avec played.command ; le bouton du message devient « ✅ Joué à … »."""
    clear_pending()
    reply = played.command([p["what"], odds] + ([stake] if stake else []), p["day"])
    if reply.startswith("✅ Noté") and p.get("msg"):
        telegram.edit_buttons(p["msg"], telegram.inline([[(f"✅ Joué à {odds}", None)]]))
    return reply


# ---------- texte libre ----------

def _team_command(text, today):
    """« Flamengo » : l'analyse du match du jour qui le contient, sinon ses stats ; None si l'équipe est inconnue."""
    from .sources import names, team_profile
    q = played.norm(text)
    for c in list(today.get("picks") or []) + list(today.get("pronos") or []):
        for team in (c.get("home"), c.get("away")):
            if team and (q in played.norm(team) or names.team_sim(text, team) >= 0.8):
                return f"/analyse {text}"
    try:
        if team_profile(text):
            return f"/stats {text}"
    except Exception:                                 # noqa: BLE001 - profils facultatifs
        pass
    return None


def route(text):
    """Ce qu'il faut faire d'un message : (commande « / », None) à passer au traitement des commandes, ou (None, réponse)."""
    t = (text or "").strip()
    num = NUM.match(t)
    p = get_pending()
    if num and p:
        return None, _note(p, num[1], num[2])
    if num:
        return None, ("Appuie d'abord sur « ✅ Je l'ai joué » sous le pari, puis envoie la cote. "
                      "Ou écris tout d'un coup, par exemple : joué sûr 5,40")
    clear_pending()
    if t.startswith("/"):
        return t, None
    if t in BUTTONS:
        return (BUTTONS[t], None) if BUTTONS[t] else (None, combi_picker())
    words = t.split()
    if not words:
        return None, None
    head, rest = played.norm(words[0]).rstrip(" :"), words[1:]
    if head == "paris" and played.norm(" ".join(rest)) in ("du jour", ""):
        return "/top", None
    if head in ("combi", "combine") and not rest:
        return None, combi_picker()
    if head in ("bilan", "resultats", "resultat") and any(played.norm(w).startswith("detail") for w in rest):
        return "/bilan détail " + " ".join(w for w in rest if w.isdigit()), None
    if head in WORDS:
        return (WORDS[head] + (" " + " ".join(rest) if rest else "")).strip(), None
    cmd = _team_command(t, store.load("today.json", {}))
    if cmd:
        return cmd, None
    return None, telegram.with_buttons("Je n'ai pas compris. Utilise les boutons en bas, ou écris par exemple : bilan, combi 20, "
                                       "Flamengo.", menu())


# ---------- appuis sur les boutons ----------

def on_callback(data, message_id=None):
    """Réponse à un appui sur un bouton sous un message (voir les callback_data de ce module)."""
    from . import app
    parts = (data or "").split("|")
    if parts[0] == "joue" and len(parts) == 3:
        t = played.find(parts[1], parts[2])
        if t is None or isinstance(t, str):
            return t or "Ce pari n'existe plus."
        if t.item.get("result") is not None:
            return f"Le {t.label} est déjà réglé : trop tard pour le noter."
        set_pending(parts[1], parts[2], message_id)
        return (f"À quelle cote as-tu joué le {t.label} chez 1xBet ?\nRéponds juste la cote, par exemple "
                f"{fmt.num(t.ref)} (ou {fmt.num(t.ref)} 2000 pour donner aussi ta mise).")
    if parts[0] == "combi" and len(parts) == 2:
        return app.handle_command(f"/combi {parts[1]}")
    if parts[0] == "bilan":
        return app.handle_command("/bilan détail")
    if parts[0] == "noter":
        return app.handle_command("/joue")
    return None
