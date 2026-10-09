"""Kelly sans commandes : menu de boutons fixe, bouton « ✅ Je l'ai joué » sous les paris, texte libre (« bilan », « combi 20 »,
« Flamengo »...). Les commandes « / » restent valables : route() traduit le reste en commande « / », ou répond directement.

Seul un pari que tu NOTES compte (kelly/played.py) : sous un pari pas noté, « ✅ Je l'ai joué » (avec la mise conseillée du budget du jour
quand il y en a une) ; sous un pari noté, « ✏️ Autre cote » et « ↩️ Annuler » (callback « annule »). Les anciens boutons « ❌ Pas joué » /
« ↩️ Finalement joué » (callbacks « skip » / « unskip ») répondent encore, mais aucun message neuf ne les crée.

Pari en attente de cote : data/pending.json = {what, day, msg, at} après un appui sur ✅, valable PENDING_MINUTES ; la réponse
« 5,40 » (ou « 5,40 2000 » avec la mise en FCFA) le note avec played.command, puis les boutons deviennent « ✅ Joué à 5,40 » + « ✏️ Autre
cote » + « ↩️ Annuler ». Sans mise conseillée, la question reste ouverte jusqu'à ce que la mise soit donnée.

Sous un combiné « combi 20 », « ✅ Je le joue » (callback « cjoue|m<n>|<jour> ») le note comme un combiné du matin : voir kelly/custom.py.
"""
import re
from datetime import datetime, timedelta, timezone

from . import fmt, money, played, store, telegram

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


def played_button(what, day, stake=0, played_=None):
    """Boutons d'un pari du jour `day` (what : sur, costaud..., pari, v5...). Pas noté : « ✅ Je l'ai joué », avec la mise conseillée (`stake`,
    FCFA) dans le libellé quand il y en a une. Noté (`played_` : sa note {odds, stake}) : « ✅ Joué à … » (inerte), « ✏️ Autre cote » et
    « ↩️ Annuler »."""
    from . import money
    day = str(day)[:10]
    if played_:
        return telegram.inline([[(f"✅ Joué à {fmt.odds_txt(played_['odds'])} · {money.money(played_['stake'])}", None)],
                                [("✏️ Autre cote", f"joue|{what}|{day}"), ("↩️ Annuler", f"annule|{what}|{day}")]])
    return telegram.inline([[("✅ Je l'ai joué" + (f" — {money.money(stake)}" if stake else ""), f"joue|{what}|{day}")]])


def state_buttons(what, day):
    """Boutons d'un pari d'après son état dans le suivi (noté ou pas, mise conseillée du moment) ; None si le pari n'existe plus."""
    t = played.find(what, str(day)[:10])
    if t is None or isinstance(t, str):
        return None
    p = t.item.get("played")
    return played_button(what, day, 0 if p else t.stake, p)


def with_played_button(text, what, day, stake=0, refresh=None, xrefresh=False, lookup=False, code=None):
    """Le message `text` avec ses boutons de pari. `stake` : la mise conseillée (FCFA) affichée dans « ✅ Je l'ai joué » ; `lookup` : boutons
    d'après l'état du pari dans le suivi (state_buttons : un combiné renvoyé peut être noté ou pas). `refresh` : palier d'un combiné à cote
    juste aux cotes 1xBet -> bouton « 🎟️ Vérifier ce combiné » (cotes relues en direct, paris retirés remplacés, nouveau code). `xrefresh` :
    combiné réel (ou pari du jour) remis aux cotes 1xBet -> même bouton, pour `what` (cotes relues, nouveau code). `code` : le code coupon
    du message (un seul) -> ligne « 📋 Copier le code XXXX » en dernier."""
    markup = state_buttons(what, day) if lookup else played_button(what, day, stake)
    extra = ([("🎟️ Vérifier ce combiné", f"xmaj|{refresh}|{str(day)[:10]}")] if refresh is not None
             else [("🎟️ Vérifier ce combiné", f"xmajr|{what}|{str(day)[:10]}")] if xrefresh else None)
    rows = ((markup or {"inline_keyboard": []})["inline_keyboard"] + (telegram.inline([extra])["inline_keyboard"] if extra else [])
            + telegram.copy_row(code))
    return telegram.with_buttons(text, {"inline_keyboard": rows}) if rows else text


def refresh_button(what, day, label="🎟️ Vérifier ce combiné"):
    """Bouton « 🎟️ Vérifier ce combiné » du pari `what` du jour `day` : combiné à cote juste « v<palier> » ou combiné réel / pari du jour
    aux cotes 1xBet (jamais pour un pari « à vérifier » à cote mini, ni réglé) ; None sinon."""
    day = str(day)[:10]
    t = played.find(what, day)
    if t is None or isinstance(t, str) or t.item.get("result") is not None:
        return None
    if str(what).startswith("v"):
        return (label, f"xmaj|{what[1:]}|{day}") if played.is_real(t.item) else None
    return (label, f"xmajr|{what}|{day}") if t.item.get("xlegs") or t.item.get("xodds") else None


def refresh_row(what, day):
    """Ligne « 🎟️ Vérifier ce combiné » d'un combiné aux cotes 1xBet (« v<palier> », combiné réel, pari du jour), à garder quand les
    boutons du pari changent ([] sinon)."""
    b = refresh_button(what, day)
    return telegram.inline([[b]])["inline_keyboard"] if b else []


def copy_rows(what, day):
    """Ligne « 📋 Copier le code XXXX » du code coupon de la version en cours du pari `what` ; [] sans code (ou pari disparu)."""
    t = played.find(what, str(day)[:10])
    if t is None or isinstance(t, str):
        return []
    return telegram.copy_row(played.price_info(what, t.item).get("coupon"))


def keep_refresh(markup, what, day):
    """`markup` (boutons du pari) avec, en plus, la ligne « 🎟️ Vérifier ce combiné » et la ligne « 📋 Copier le code » s'il y a lieu : quand les
    boutons d'un message sont remplacés (note, annulation, ancien « ❌ Pas joué »...), ni la vérification ni le code ne se perdent."""
    extra = refresh_row(what, day) + copy_rows(what, day)
    if not extra:
        return markup
    return {"inline_keyboard": ((markup or {}).get("inline_keyboard") or []) + extra}


def combi_picker():
    return telegram.with_buttons("🎯 Quelle cote veux-tu pour ton combiné ?",
                                 telegram.inline([[(f"cote {t}", f"combi|{t}") for t in COMBI_TARGETS]]))


# ---------- pari en attente de cote ----------

def set_pending(what, day, msg, odds=None):
    """Attend la réponse de l'utilisateur pour noter le pari `what` du jour `day`. `odds` : la cote est déjà CONNUE (cote 1xBet lue par Kelly, ou
    cote tapée avec /joue) : on n'attend que la mise (un montant seul, « 1000 » ou « 1 000 », est alors une mise) ; sans, la cote (et la mise)."""
    store.save(PENDING_FILE, {"what": what, "day": day, "msg": msg, "at": _now().isoformat(timespec="seconds")} | ({"odds": odds} if odds else {}))


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


STAKE_HELP = "Réponds ta mise, par exemple 1 000"


def _bad_answer(p, ref, known):
    """Réponse non comprise : ce qu'il faut répondre (la question reste ouverte)."""
    ex = fmt.num(ref) if ref else "5,40"
    if known:
        return (f"Je n'ai pas compris. {STAKE_HELP} (la cote {fmt.num(known)} est retenue), ou la cote et la mise : {ex} 1 000.")
    return (f"Je n'ai pas compris la cote. Réponds juste la cote, par exemple {ex} (ou {ex} 2 000 pour donner aussi ta mise en FCFA). "
            "Une cote entière avec une mise s'écrit avec ses décimales : 2,00 500.")


def _answer(p, text):
    """La réponse envoyée après un appui sur ✅ / ✏️ (« 5,40 », « 5,40 2 000 »...) : notée avec played.command ; les boutons du message
    deviennent « ✅ Joué à … », « ✏️ Autre cote », « ↩️ Annuler ». Cote douteuse ou ambiguë (« 2 000 ») : on redemande, l'attente reste
    ouverte. Cote maxi : 1000, ou le double de la cote du pari pour un gros combiné (« combi 10000 » : 11 061,90).
    Cote déjà CONNUE (p["odds"] : cote 1xBet lue par Kelly, ou cote tapée) : on n'attend que la mise, un montant seul (« 1000 », « 1 000 »,
    « 2000 ») est la mise (aucune ambiguïté à garder : ce n'est pas une cote), et « cote mise » (« 11000 1000 ») corrige aussi la cote. Un
    décimal seul (« 5,40 ») est une cote : la mise est alors demandée, avec cette cote. Sans mise conseillée, la mise est demandée et l'attente
    reste ouverte."""
    t = played.find(p["what"], p["day"])
    ref = t.ref if t is not None and not isinstance(t, str) and t.ref else None
    known = p.get("odds")
    top = min(played.MAX_ODDS, max(1000.0, 2 * (ref or known or 0)))
    args = [text]
    if known:                                       # cote connue : un montant seul est la mise
        tokens = money.split_tokens([text])
        if len(tokens) > 1 and money.CUR_RE.fullmatch(tokens[-1]):
            tokens = tokens[:-1]
        amount, n = money.take_amount(tokens)
        if amount is not None and n == len(tokens):
            if amount > 0 and float(amount).is_integer():
                args = [f"{known:.2f}", str(int(amount))]
            elif not 1 < amount < top:
                return _bad_answer(p, ref, known)
            else:                                   # « 5,40 » : une autre cote, la mise reste à donner
                args = [text]
    if args == [text]:
        odds, stake, err = played.parse_odds_stake([text], strict=not known)
        if err or odds is None or not 1 < odds < top:
            return _bad_answer(p, ref, known)
    reply = played.command([p["what"]] + args, p["day"])
    if reply.startswith(played.NEED_STAKE):          # pas de mise : la question reste ouverte, la cote donnée est retenue
        set_pending(p["what"], p["day"], p.get("msg"), odds=played.parse_odds_stake(args)[0])
        return reply
    clear_pending()
    if reply.startswith("✅ Noté") and p.get("msg"):
        telegram.edit_buttons(p["msg"], keep_refresh(state_buttons(p["what"], p["day"]), p["what"], p["day"]))
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
        return None, ("Appuie d'abord sur « ✅ Je l'ai joué » (ou « ✏️ Autre cote ») sous le pari, puis envoie la cote. "
                      "Ou écris tout d'un coup, par exemple : joué sûr 5,40 1000")
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

def _ask(t):
    """La question posée après « ✅ Je l'ai joué » / « ✏️ Autre cote » sur le pari `t` (played.find). Cote 1xBet CONNUE (t.known) et pas encore
    noté : jamais la cote, seulement la mise (on_callback a noté tout de suite quand une mise est conseillée). Sinon la cote, et la mise quand
    Kelly n'en conseille aucune (ou à corriger) ; « ✏️ Autre cote » demande toujours la cote, la mise notée reste la même si seule la cote est donnée."""
    ex = fmt.num(t.ref)
    prev = t.item.get("played")
    if prev:
        return (f"À quelle cote as-tu finalement joué le {t.label} chez 1xBet ?\nRéponds juste la nouvelle cote, par exemple {ex} (ta mise reste "
                f"{money.money(prev['stake'], t.item.get('cur'))}), ou {ex} 2000 pour changer aussi la mise.")
    if t.known:
        return (f"{played.NEED_STAKE} {STAKE_HELP} : le {t.label} est noté à la cote 1xBet {fmt.odds_txt(t.ref)} lue par Kelly. "
                f"Si 1xBet t'a payé autre chose, réponds la cote et la mise : {ex} 1 000.")
    if t.stake > 0:
        return (f"À quelle cote as-tu joué le {t.label} chez 1xBet ?\nRéponds juste la cote, par exemple {ex} : ta mise sera de "
                f"{money.money(t.stake)} (ou {ex} 2000 pour une autre mise).")
    return (f"À quelle cote et avec quelle mise en FCFA ? (le {t.label} chez 1xBet)\n"
            f"Réponds la cote et la mise, par exemple {ex} 1 000.")


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
        if t.known and not t.item.get("played"):     # cote 1xBet connue : jamais demandée
            if t.stake > 0:                          # et mise conseillée : noté tout de suite (« ✏️ Autre cote » / « ↩️ Annuler » corrigent)
                reply = played.note(t, t.ref, t.stake)
                if message_id:
                    telegram.edit_buttons(message_id, keep_refresh(state_buttons(parts[1], parts[2]), parts[1], parts[2]))
                return reply
            set_pending(parts[1], parts[2], message_id, odds=t.ref)      # sinon seule la mise est demandée
            return _ask(t)
        set_pending(parts[1], parts[2], message_id)
        return _ask(t)
    if parts[0] == "annule" and len(parts) == 3:          # « ↩️ Annuler » : la note du pari est retirée (comme /joue <quoi> 0)
        reply = played.command([parts[1], "0"], parts[2])
        if message_id and reply.startswith("Note annulée"):
            telegram.edit_buttons(message_id, keep_refresh(state_buttons(parts[1], parts[2]), parts[1], parts[2]) or {"inline_keyboard": []})
        return reply
    if parts[0] in ("skip", "unskip") and len(parts) in (3, 4):
        from . import money
        reply = money.skip(parts[1], parts[2], undo=parts[0] == "unskip")
        if message_id and not reply.startswith(("Pas de", "Ce pari", "Le ")):       # « Le … est déjà réglé » : rien ne change
            if len(parts) == 4:                       # depuis la liste « Mes paris » : la liste entière est rafraîchie
                markup = getattr(played.todo(parts[2]), "markup", None) or {"inline_keyboard": []}
            else:
                markup = keep_refresh(telegram.inline([[("↩️ Finalement joué", f"unskip|{parts[1]}|{parts[2]}")]]) if parts[0] == "skip"
                                      else state_buttons(parts[1], parts[2]), parts[1], parts[2])
            telegram.edit_buttons(message_id, markup)
        return reply
    if parts[0] == "budget" and len(parts) == 2:
        return app.handle_command(f"/budget {parts[1]}")
    if parts[0] == "combi" and len(parts) == 2:
        return app.handle_command(f"/combi {parts[1]}")
    if parts[0] == "cjoue" and len(parts) == 3:       # « ✅ Je le joue » sous un combiné « combi N » : il devient un pari suivi
        from . import custom
        return custom.track(parts[1], parts[2], message_id)
    if parts[0] == "xmaj" and len(parts) == 3:
        return app.refresh_combo(parts[1], parts[2])
    if parts[0] == "xmajr" and len(parts) == 3:
        return app.refresh_real(parts[1], parts[2])
    if parts[0] == "bilan":
        return app.handle_command("/bilan détail")
    if parts[0] == "noter":
        return app.handle_command("/joue")
    return None
