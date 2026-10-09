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
MENU = [["🔝 Paris du jour", "🎯 Un combiné"], ["📊 Mes résultats", "🧾 Mes paris"], ["💰 Mon budget", "🔄 Mettre à jour tous les paris"],
        ["❓ Aide"]]
BUTTONS = {"🔝 Paris du jour": "/top", "🎯 Un combiné": None, "📊 Mes résultats": "/bilan", "🧾 Mes paris": "/joue",
           "🧾 À noter": "/joue", "💰 Mon budget": "/budget", "❓ Aide": "/aide",      # « À noter » : ancien libellé
           "🔄 Mettre à jour tous les paris": "/cotes", "🔄 Actualiser les cotes": "/cotes"}     # 2e : ancien libellé du menu
COMBI_TARGETS = (3, 5, 10, 20, 50)
NUM = re.compile(r"^\s*\d[\d\s.,]*(?:\s*(?:[^\W\d_]{1,8}|[€$£¥]))?\s*$")       # « 5,40 », « 5,40 2 000 », « 5,40 2.000 FCFA »
TOP_TEXTS = ("paris", "paris du jour", "mes paris")                               # exactement : « Paris FC » est une équipe
WORDS = {"bilan": "/bilan", "resultats": "/bilan", "resultat": "/bilan", "top": "/top", "meilleurs": "/top",
         "aide": "/aide", "help": "/aide", "menu": "/aide", "?": "/aide", "start": "/aide", "bonjour": "/aide", "salut": "/aide",
         "joue": "/joue", "jouer": "/joue", "noter": "/joue", "bank": "/bank", "bankroll": "/bank", "monde": "/monde",
         "ligues": "/ligues", "stats": "/stats", "analyse": "/analyse", "match": "/match", "combi": "/combi", "combine": "/combi",
         "budget": "/budget", "actualiser": "/cotes", "actualise": "/cotes", "cotes": "/cotes", "rafraichir": "/cotes",
         "maj": "/cotes"}


def _now():
    return datetime.now(timezone.utc)


def menu():
    return telegram.menu_keyboard(MENU)


def played_button(what, day, funded=True):
    """Boutons d'un pari du jour `day` (what : sur, costaud..., pari, v5...). Avec un budget du jour : le pari compte comme joué,
    « ❌ Pas joué » le retire et « ✏️ Autre cote » corrige la cote (aucun bouton si `funded` est faux : mise de 0) ; sans budget :
    « ✅ Je l'ai joué »."""
    from . import money
    day = str(day)[:10]
    if money.active():
        if not funded:                              # mise de 0 (trop petite pour le budget) : rien à jouer, rien à corriger
            return None
        return telegram.inline([[("❌ Pas joué", f"skip|{what}|{day}"), ("✏️ Autre cote", f"joue|{what}|{day}")]])
    return telegram.inline([[("✅ Je l'ai joué", f"joue|{what}|{day}")]])


def with_played_button(text, what, day, funded=True, refresh=None):
    """Le message `text` avec ses boutons de pari ; sans bouton (texte seul) quand il n'y a rien à jouer. `refresh` : palier d'un
    combiné aux cotes 1xBet -> bouton « 🎟️ Vérifier ce combiné » (cotes relues en direct, paris retirés remplacés, nouveau code)."""
    markup = played_button(what, day, funded)
    if refresh is not None:
        row = telegram.inline([[("🎟️ Vérifier ce combiné", f"xmaj|{refresh}|{str(day)[:10]}")]])["inline_keyboard"]
        markup = {"inline_keyboard": (markup or {"inline_keyboard": []})["inline_keyboard"] + row}
    return telegram.with_buttons(text, markup) if markup else text


def refresh_row(what, day):
    """Ligne « 🎟️ Vérifier ce combiné » d'un combiné « v<palier> » aux cotes 1xBet, à garder quand les boutons du pari changent ([] sinon)."""
    if not str(what).startswith("v"):
        return []
    t = played.find(what, str(day)[:10])
    if t is None or isinstance(t, str) or not played.is_real(t.item):
        return []
    return telegram.inline([[("🎟️ Vérifier ce combiné", f"xmaj|{what[1:]}|{str(day)[:10]}")]])["inline_keyboard"]


def keep_refresh(markup, what, day):
    """`markup` (boutons du pari) avec, en plus, la ligne « 🎟️ Vérifier ce combiné » s'il y a lieu."""
    extra = refresh_row(what, day)
    if not extra:
        return markup
    return {"inline_keyboard": ((markup or {}).get("inline_keyboard") or []) + extra}


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


def _answer(p, text):
    """La réponse envoyée après un appui sur ✅ / ✏️ (« 5,40 », « 5,40 2 000 »...) : notée avec played.command ; le bouton du
    message devient « ✅ Joué à … » (avec « ❌ Pas joué » en budget). Cote douteuse ou ambiguë (« 2 000 ») : on redemande, l'attente
    reste ouverte."""
    from . import money
    odds, stake, err = played.parse_odds_stake([text], strict=True)
    if err or odds is None or not 1 < odds < 1000:
        return ("Je n'ai pas compris la cote. Réponds juste la cote, par exemple 5,40 (ou 5,40 2 000 pour donner aussi ta mise). "
                "Une cote entière avec une mise s'écrit avec ses décimales : 2,00 500.")
    clear_pending()
    reply = played.command([p["what"], text], p["day"])
    if reply.startswith("✅ Noté") and p.get("msg"):
        label = (f"✅ Joué à {money.split_tokens([text])[0]}", None)
        telegram.edit_buttons(p["msg"], keep_refresh(telegram.inline([[label, ("❌ Pas joué", f"skip|{p['what']}|{p['day']}")]]
                                                                     if money.active() else [[label]]), p["what"], p["day"]))
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
        return None, _answer(p, t)
    if num:
        from . import money
        button = "✏️ Autre cote" if money.active() else "✅ Je l'ai joué"
        return None, (f"Appuie d'abord sur « {button} » sous le pari, puis envoie la cote. "
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
    if played.norm(" ".join(words)).rstrip(" :") in TOP_TEXTS:
        return "/top", None
    if played.norm(" ".join(words)).startswith("mettre a jour"):          # « mettre à jour (tous les paris) »
        return "/cotes", None
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
        from . import money
        if played.unfunded(t) and money.active():
            return f"Le {t.label} n'a pas de mise sur ton budget du jour (trop petite) : rien à corriger."
        set_pending(parts[1], parts[2], message_id)
        return (f"À quelle cote as-tu joué le {t.label} chez 1xBet ?\nRéponds juste la cote, par exemple "
                f"{fmt.num(t.ref)} (ou {fmt.num(t.ref)} 2000 pour donner aussi ta mise).")
    if parts[0] in ("skip", "unskip") and len(parts) in (3, 4):
        from . import money
        reply = money.skip(parts[1], parts[2], undo=parts[0] == "unskip")
        if message_id and not reply.startswith(("Pas de", "Ce pari", "Le ")):       # « Le … est déjà réglé » : rien ne change
            if len(parts) == 4:                       # depuis la liste « Mes paris » : la liste entière est rafraîchie
                markup = getattr(played.todo(parts[2]), "markup", None) or {"inline_keyboard": []}
            else:
                markup = keep_refresh(telegram.inline([[("↩️ Finalement joué", f"unskip|{parts[1]}|{parts[2]}")]]) if parts[0] == "skip"
                                      else played_button(parts[1], parts[2]), parts[1], parts[2])
            telegram.edit_buttons(message_id, markup)
        return reply
    if parts[0] == "budget" and len(parts) == 2:
        return app.handle_command(f"/budget {parts[1]}")
    if parts[0] == "combi" and len(parts) == 2:
        return app.handle_command(f"/combi {parts[1]}")
    if parts[0] == "xmaj" and len(parts) == 3:
        return app.refresh_combo(parts[1], parts[2])
    if parts[0] == "bilan":
        return app.handle_command("/bilan détail")
    if parts[0] == "noter":
        return app.handle_command("/joue")
    return None
