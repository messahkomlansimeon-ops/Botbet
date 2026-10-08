"""Envoi de messages et lecture des commandes Telegram."""
import os

import requests

from . import store


def creds():
    return os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def api(method, **payload):
    token, _ = creds()
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=40)
    r.raise_for_status()
    return r.json().get("result")


def send(text, chat_id=None):
    token, chat = creds()
    chat_id = chat_id or chat
    if not (token and chat_id):
        print(text + "\n")
        return
    for i in range(0, len(text), 4000):             # limite Telegram : 4096 caractères
        api("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


def poll(handler, timeout=0):
    """Lit les nouvelles commandes (une passe) et répond. Ne répond qu'au chat
    configuré : personne d'autre ne peut piloter le bot."""
    _, chat = creds()
    state = store.load("telegram.json", {"offset": 0})
    updates = api("getUpdates", offset=state["offset"], timeout=timeout) or []
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
    store.save("telegram.json", state)
    return len(updates)
