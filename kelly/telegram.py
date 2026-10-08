"""Envoi de messages et lecture des commandes Telegram."""
import html as _html
import os
import re
import sys
import time

import requests

from . import store

LAST_ERROR = ""        # dernière erreur d'envoi (description renvoyée par l'API Telegram)


class Html(str):
    """Message au format HTML Telegram (balises <b> et <i>, texte externe échappé). Envoyé en parse_mode HTML ; si
    Telegram refuse le HTML, le même message part en texte brut. Un `str` ordinaire part en texte brut."""


class Text(str):
    """Message en texte brut qui porte des boutons (attribut `markup`, voir with_buttons)."""


def with_buttons(text, markup):
    """Le même message (HTML ou texte brut) avec des boutons : `markup` = inline(...) ou menu_keyboard(...)."""
    out = (Html if isinstance(text, Html) else Text)(text)
    out.markup = markup
    return out


def inline(rows):
    """Boutons sous un message : rows = [[(libellé, donnée renvoyée au clic, 64 octets au plus)]] ; donnée None = bouton inerte."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data or "noop"} for label, data in row] for row in rows]}


def menu_keyboard(rows):
    """Menu fixe en bas de Telegram, à la place du clavier : rows = [[libellé]] ; un appui envoie le libellé comme message."""
    return {"keyboard": [[{"text": label} for label in row] for row in rows], "resize_keyboard": True, "is_persistent": True}


def plain(text):
    """Texte brut d'un message HTML (console, repli quand Telegram refuse le HTML)."""
    return _html.unescape(re.sub(r"</?(b|i|u|s|code|pre)>", "", str(text)))


def _chunks(text, size=4000):
    """Morceaux de `size` caractères au plus, coupés de préférence entre deux lignes (jamais au milieu d'une balise)."""
    out = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        cut = cut if cut > 0 else (text.rfind("\n", 0, size) if text.rfind("\n", 0, size) > 0 else size)
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text]


class TelegramError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def creds():
    return os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def _scrub(text):
    token, _ = creds()
    return str(text).replace(token, "***") if token else str(text)


def api(method, **payload):
    """Appel Bot API -> `result`. Lève TelegramError avec la description de l'API (sans le token)."""
    token, _ = creds()
    if not token:
        raise TelegramError("TELEGRAM_TOKEN absent")
    for attempt in (0, 1):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=40)
        except requests.RequestException as e:
            raise TelegramError(f"réseau : {_scrub(e)}") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.ok and body.get("ok", True):
            return body.get("result")
        wait = (body.get("parameters") or {}).get("retry_after")
        if r.status_code == 429 and wait and attempt == 0:      # trop de messages : on patiente
            time.sleep(min(int(wait) + 1, 30))
            continue
        break
    raise TelegramError(_scrub(body.get("description") or f"HTTP {r.status_code}"), body.get("error_code", r.status_code))


def send(text, chat_id=None):
    """Envoie `text` (découpé). `Html` : parse_mode HTML, avec repli en texte brut si Telegram rejette le HTML. Les boutons du
    message (attribut `markup`, with_buttons) vont sur le dernier morceau. Sans identifiants : affiche. Ne lève jamais : renvoie
    l'id du dernier message envoyé (True sans identifiants), ou False et note l'erreur (stderr + LAST_ERROR) si Telegram refuse."""
    global LAST_ERROR
    token, chat = creds()
    chat_id = chat_id or chat
    if not (token and chat_id):
        print(plain(text) + "\n")
        return True
    is_html = isinstance(text, Html)
    parts, markup, sent = _chunks(str(text)), getattr(text, "markup", None), True
    for i, part in enumerate(parts):
        extra = {"reply_markup": markup} if markup and i == len(parts) - 1 else {}
        try:
            if is_html:
                try:
                    sent = api("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True, **extra)
                    continue
                except TelegramError as e:
                    if "parse" not in str(e).lower() and "entities" not in str(e).lower():
                        raise
                    print("[telegram] HTML refusé, renvoi en texte brut", file=sys.stderr)
                    part = plain(part)
            sent = api("sendMessage", chat_id=chat_id, text=part, **extra)
        except TelegramError as e:
            LAST_ERROR = f"{e} (code {e.code})" if e.code else str(e)
            print(f"[telegram] envoi échoué : {LAST_ERROR}", file=sys.stderr)
            return False
    return (sent or {}).get("message_id", True) if isinstance(sent, dict) else True


def answer_callback(callback_id, text=None):
    """Acquitte un appui sur un bouton (sinon Telegram laisse un sablier). Ne lève jamais."""
    try:
        api("answerCallbackQuery", callback_query_id=callback_id, **({"text": text} if text else {}))
    except TelegramError as e:
        print(f"[telegram] acquittement du bouton échoué : {e}", file=sys.stderr)


def edit_buttons(message_id, markup, chat_id=None):
    """Remplace les boutons d'un message déjà envoyé (« ✅ Je l'ai joué » -> « ✅ Joué à 5,40 »). Ne lève jamais."""
    token, chat = creds()
    if not (token and (chat_id or chat) and message_id):
        return False
    try:
        api("editMessageReplyMarkup", chat_id=chat_id or chat, message_id=message_id, reply_markup=markup)
        return True
    except TelegramError as e:
        print(f"[telegram] boutons non modifiés : {e}", file=sys.stderr)
        return False


def poll(handler, timeout=0, on_callback=None):
    """Lit les nouveaux messages et appuis sur les boutons (une passe) et répond. Ne répond qu'au chat configuré : personne
    d'autre ne peut piloter le bot. `on_callback(donnée, id du message)` : réponse à un appui sur un bouton (None = rien à dire)."""
    _, chat = creds()
    state = store.load("telegram.json", {"offset": 0})
    try:
        updates = api("getUpdates", offset=state["offset"], timeout=timeout,
                      allowed_updates=["message", "callback_query"]) or []
    except TelegramError as e:
        print(f"[telegram] lecture des commandes échouée : {e}", file=sys.stderr)
        if timeout:
            time.sleep(5)                           # mode serveur : évite de boucler à vide
        return 0
    for u in updates:
        state["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if cq:
            answer_callback(cq.get("id"))
            cmsg = cq.get("message") or {}
            if on_callback and str(cmsg.get("chat", {}).get("id")) == str(chat):
                try:
                    reply = on_callback(cq.get("data") or "", cmsg.get("message_id"))
                except Exception as e:              # noqa: BLE001 - on répond l'erreur
                    reply = f"Erreur : {e}"
                if reply:
                    send(reply)
            store.save("telegram.json", state)
            continue
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text or str(msg.get("chat", {}).get("id")) != str(chat):
            continue
        try:
            reply = handler(text)
        except Exception as e:                      # noqa: BLE001 - on répond l'erreur
            reply = f"Erreur : {e}"
        if reply:
            send(reply)
        store.save("telegram.json", state)          # offset sauvé à chaque commande : jamais traitée deux fois
    store.save("telegram.json", state)
    return len(updates)
