"""Envoi de messages et lecture des commandes Telegram."""
import os
import sys
import time

import requests

from . import store

LAST_ERROR = ""        # dernière erreur d'envoi (description renvoyée par l'API Telegram)


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
    """Envoie `text` (découpé). Sans identifiants : affiche. Ne lève jamais : renvoie False
    et note l'erreur (stderr + LAST_ERROR) si Telegram refuse."""
    global LAST_ERROR
    token, chat = creds()
    chat_id = chat_id or chat
    if not (token and chat_id):
        print(text + "\n")
        return True
    for i in range(0, len(text), 4000):             # limite Telegram : 4096 caractères
        try:
            api("sendMessage", chat_id=chat_id, text=text[i:i + 4000])
        except TelegramError as e:
            LAST_ERROR = f"{e} (code {e.code})" if e.code else str(e)
            print(f"[telegram] envoi échoué : {LAST_ERROR}", file=sys.stderr)
            return False
    return True


def poll(handler, timeout=0):
    """Lit les nouvelles commandes (une passe) et répond. Ne répond qu'au chat
    configuré : personne d'autre ne peut piloter le bot."""
    _, chat = creds()
    state = store.load("telegram.json", {"offset": 0})
    try:
        updates = api("getUpdates", offset=state["offset"], timeout=timeout) or []
    except TelegramError as e:
        print(f"[telegram] lecture des commandes échouée : {e}", file=sys.stderr)
        if timeout:
            time.sleep(5)                           # mode serveur : évite de boucler à vide
        return 0
    for u in updates:
        state["offset"] = u["update_id"] + 1
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
