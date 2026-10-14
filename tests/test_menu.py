"""Menu Telegram (hors ligne) : clavier du bas de 6 boutons en 3 rangées de 2, sous-menu « ➕ Plus » en boutons, anciens libellés qui répondent encore, menu natif « / ».
Lancer : python -m tests.test_menu"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-menu-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-menu-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import app, chat, telegram  # noqa: E402


def test_keyboard():
    assert len(chat.MENU) == 3 and all(len(r) == 2 for r in chat.MENU), chat.MENU
    flat = [b for r in chat.MENU for b in r]
    assert len(flat) == 6 and len(set(flat)) == 6 and chat.MORE_BUTTON in flat
    kb = chat.menu()
    assert [[b["text"] for b in r] for r in kb["keyboard"]] == chat.MENU and kb["is_persistent"] and kb["resize_keyboard"]
    # chaque bouton du bas répond : commande, sélecteur, ou sous-menu
    for label in flat:
        cmd, reply = chat.route(label)
        assert cmd or reply, label
    assert chat.route("🔝 Du jour")[0] == "/top" and chat.route("📊 Résultats")[0] == "/bilan" and chat.route("🧾 Mes paris")[0] == "/joue"
    print("ok  clavier du bas : 6 boutons en 3 rangées de 2, tous répondent")


def test_more():
    cmd, reply = chat.route("➕ Plus")
    assert cmd is None and reply is not None
    rows = reply.markup["inline_keyboard"]
    assert [b["text"] for r in rows for b in r] == [t for r in chat.MORE for t, _ in r] and all(len(r) <= 2 for r in rows)
    datas = [d for r in chat.MORE for _, d in r]
    assert len(set(datas)) == len(datas) == 7
    for d in datas:
        kind, key = d.split("|")
        assert (kind == "m" and key in chat.MORE_CMD) or (kind == "q" and key == "status"), d
    for key in ("cap", "sai", "ten", "mar", "aid"):
        out = chat.on_callback(f"m|{key}")
        assert out, key
    assert chat.on_callback("m|zzz") is None
    print("ok  « ➕ Plus » : 7 boutons, chaque appui lance sa commande")


def test_status_without_lock():
    """« 🩺 Status » de « ➕ Plus » est répondu par telegram.poll SANS le verrou (comme le mot écrit), même quand tout le reste est mis en attente (defer)."""
    sent, later = [], []
    ups = [{"update_id": 1, "callback_query": {"id": "c1", "data": "q|status", "message": {"chat": {"id": 42}, "message_id": 9}}},
           {"update_id": 2, "callback_query": {"id": "c2", "data": "m|cap", "message": {"chat": {"id": 42}, "message_id": 9}}}]
    real = (telegram.api, telegram.creds, telegram.send, telegram._later, telegram.answer_callback, telegram._save_offset, telegram._outage_back)
    telegram.api = lambda method, **kw: ups if method == "getUpdates" else True
    telegram.creds = lambda: ("T", "42")
    telegram.send = lambda text, *a, **kw: sent.append(text)
    telegram._later = lambda fn, args, cb: later.append(args)
    telegram.answer_callback = lambda *a, **kw: None
    telegram._save_offset = lambda state: None
    telegram._outage_back = lambda: None
    try:
        telegram.poll(lambda t: None, on_callback=lambda d, m: None, defer=True, quick=lambda t: "STATUS " + t if t == "status" else None)
    finally:
        telegram.api, telegram.creds, telegram.send, telegram._later, telegram.answer_callback, telegram._save_offset, telegram._outage_back = real
    assert sent == ["STATUS status"] and later == [["m|cap", 9]], (sent, later)
    print("ok  « 🩺 Status » du sous-menu : répondu sans le verrou, les autres boutons restent mis en attente")


def test_old_labels():
    for old, cmd in (("🔝 Paris du jour", "/top"), ("📊 Mes résultats", "/bilan"), ("💼 Mon capital", "/capital"), ("📈 Tendances", "/tendances"), ("🩺 Status", "/status"),
                     ("❓ Aide", "/aide"), ("📊 Marchés", "/marches"), ("🌱 Saison", "/saison"), ("🔄 Actualiser les cotes", "/cotes")):
        assert chat.route(old)[0] == cmd, old
    assert chat.route("🎯 Un combiné")[1] is not None and chat.route("🎯 Combiné")[1] is not None
    print("ok  anciens libellés (claviers déjà affichés) : répondent encore")


def test_native_commands():
    sent = []
    real = telegram.api
    telegram.api = lambda method, **kw: sent.append((method, kw)) or True
    try:
        assert telegram.set_commands() is True
    finally:
        telegram.api = real
    method, kw = sent[0]
    names = [c["command"] for c in kw["commands"]]
    assert method == "setMyCommands" and len(names) == len(set(names)) <= 100 and all(n.isascii() and n == n.lower() and n.replace("_", "").isalnum() and len(n) <= 32 for n in names)
    assert all(1 <= len(c["description"]) <= 256 for c in kw["commands"])
    assert {"combi", "top", "joue", "live", "bilan", "capital", "saison", "tendances", "marches", "status", "aide"} == set(names)
    for n in names:                                # chaque commande du menu « / » est comprise par le bot
        assert chat.route("/" + n)[0] == "/" + n
    assert telegram.set_commands() is False        # sans jeton : pas d'exception
    assert "➕ <b>Plus</b>" in str(app.HELP) and "Le bouton <b>/</b>" in str(app.HELP)
    print("ok  menu natif « / » : 11 commandes valides, sans jeton rien ne plante, l'aide décrit les boutons")


def main():
    test_keyboard()
    test_more()
    test_old_labels()
    test_status_without_lock()
    test_native_commands()
    print("tests menu : OK")


if __name__ == "__main__":
    main()
