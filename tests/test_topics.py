"""Fils de discussion (hors ligne, API Telegram simulée) : éteints par défaut, le chat privé ne change pas d'un octet ; allumés, chaque message part dans son fil (marqué, ou celui de la demande), un fil
supprimé ne fait rien perdre, le groupe n'obéit qu'à son créateur (messages ET boutons), « /cmd@NomDuBot » est compris, `kelly fils` est idempotent. Lancer : python -m tests.test_topics"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-topics-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-topics-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, quiet, store, telegram  # noqa: E402

CALLS = []
UPDATES = []
STATE = {"dead": set(), "forum": True, "admin": True, "creator": 1, "next": 100, "private": False}


def fake_api(method, http_timeout=40, **kw):
    CALLS.append((method, kw))
    if method == "sendMessage":
        if kw.get("message_thread_id") in STATE["dead"]:
            raise telegram.TelegramError("Bad Request: message thread not found", 400)
        return {"message_id": 7}
    if method == "getUpdates":
        out = list(UPDATES)
        UPDATES.clear()
        return out
    if method == "getChatAdministrators":
        return [{"status": "administrator", "user": {"id": 9}}, {"status": "creator", "user": {"id": STATE["creator"]}}] if STATE["creator"] else []
    if method == "getMe":
        return {"id": 555, "username": "KellyBot"}
    if method == "getChat":
        return {"is_forum": STATE["forum"]}
    if method == "getChatMember":
        return {"status": "administrator" if STATE["admin"] else "member", "can_manage_topics": STATE["admin"]}
    if method == "createForumTopic":
        STATE["next"] += 1
        return {"message_thread_id": STATE["next"], "name": kw["name"]}
    return True


def setup(chat="-1001", topics=True):
    telegram.api = fake_api
    telegram.creds = lambda: ("T", chat)
    telegram._creator.update(chat=None, id=None, at=float("-inf"))
    telegram._bot.clear()
    CALLS.clear()
    UPDATES.clear()
    STATE.update(dead=set(), forum=True, admin=True, creator=1, next=100)
    store.save(telegram.OUTBOX, [])
    store.save(telegram.LATER, [])
    store.save("telegram.json", {"offset": 0})
    store.save(telegram.TOPICS_FILE, {"chat": chat, "ids": {"combos": 11, "alerts": 12, "live": 13, "report": 14, "tech": 15}} if topics else {})
    C.TOPICS = topics


def sent():
    return [kw for m, kw in CALLS if m == "sendMessage"]


def test_off_is_unchanged():
    setup("42", topics=False)
    telegram.send(telegram.topic("marqué", "combos"))
    with telegram.in_thread(5):
        telegram.send("dans un contexte")
    assert all("message_thread_id" not in kw for kw in sent()), sent()
    assert telegram.topic_ids() == {} and telegram.sender_ok("42", {"id": 3})
    print("ok  fils éteints : aucun message_thread_id, chat privé inchangé")


def test_routing():
    setup()
    telegram.send(telegram.topic("combiné", "combos"))
    telegram.send("sans fil")
    with telegram.in_thread(5):
        telegram.send("réponse")
        telegram.send(telegram.topic("alerte", "alerts"))
    got = [(k["text"], k.get("message_thread_id")) for k in sent()]
    assert got == [("combiné", 11), ("sans fil", None), ("réponse", 5), ("alerte", 5)], got
    m = telegram.with_buttons(telegram.topic("avec boutons", "live"), telegram.inline([[("a", "b")]]))
    p = telegram.Html("page 1")
    p.more = [telegram.Html("page 2")]
    telegram.topic(p, "report")
    CALLS.clear()
    telegram.send(m)
    telegram.send(p)
    assert [(k["text"], k.get("message_thread_id")) for k in sent()] == [("avec boutons", 13), ("page 1", 14), ("page 2", 14)]
    store.save(telegram.TOPICS_FILE, {"chat": "-999", "ids": {"combos": 11}})
    CALLS.clear()
    telegram.send(telegram.topic("autre groupe", "combos"))
    assert "message_thread_id" not in sent()[0], "fils créés pour un autre groupe : ignorés"
    print("ok  routage : fil marqué, fil de la demande, pages et boutons, fils d'un autre groupe ignorés")


def test_dead_thread_and_outbox():
    setup()
    STATE["dead"] = {11}
    assert telegram.send(telegram.topic("combiné", "combos")) == 7
    s = sent()
    assert [k.get("message_thread_id") for k in s] == [11, None] and "reply_to_message_id" not in s[1], "renvoyé hors fil, une seule fois"
    CALLS.clear()
    telegram._enqueue(["en attente"], False, None, None, thread=12)
    assert store.load(telegram.OUTBOX, [])[0]["thread"] == 12
    telegram.flush_outbox()
    assert sent()[0]["message_thread_id"] == 12 and not store.load(telegram.OUTBOX, []), "la file d'attente garde le fil"
    print("ok  fil supprimé : message renvoyé dans le fil général (jamais perdu) ; la file d'attente garde le fil")


def test_file_fallback():
    setup()
    posts = []

    class R:
        def __init__(self, ok, d=None):
            self.ok, self.d, self.status_code = ok, d or {}, 400

        def json(self):
            return {"ok": self.ok, "result": {"message_id": 1}} if self.ok else {"ok": False, "error_code": 400, "description": "Bad Request: message thread not found"}

    def post(url, data=None, files=None, timeout=None):
        posts.append(dict(data))
        return R("message_thread_id" not in data)
    real = telegram.requests.post
    telegram.requests.post = post
    path = os.path.join(os.environ["DATA_DIR"], "f.bin")
    open(path, "wb").write(b"x")
    try:
        telegram.send_document(path, topic="tech")
        with telegram.in_thread(5):
            telegram.send_photo(path)
    finally:
        telegram.requests.post = real
    assert [p.get("message_thread_id") for p in posts] == [15, None, 5, None], posts
    print("ok  fichiers : fil de la demande ou fil marqué, renvoyé hors fil s'il est supprimé")


def test_quiet_keeps_topic():
    setup()
    real = quiet.is_quiet
    quiet.is_quiet = lambda now=None: True
    try:
        quiet.send(telegram.topic("alerte de nuit", "alerts"))
    finally:
        quiet.is_quiet = real
    assert store.load(quiet.FILE, [])[-1]["topic"] == "alerts"
    quiet.is_quiet = lambda now=None: False
    try:
        quiet.flush()
    finally:
        quiet.is_quiet = real
    assert any(k["text"] == "alerte de nuit" and k.get("message_thread_id") == 12 for k in sent()), sent()
    print("ok  heures calmes : le fil est gardé et rétabli au réveil")


def upd(i, text=None, user=1, thread=None, forum=True, chat=-1001, data=None):
    base = {"chat": {"id": chat}, "message_id": i, **({"message_thread_id": thread, "is_topic_message": forum} if thread else {})}
    if data:
        return {"update_id": i, "callback_query": {"id": f"c{i}", "from": {"id": user}, "data": data, "message": base}}
    return {"update_id": i, "message": base | {"text": text, "from": {"id": user}}}


def test_poll_security_and_threads():
    setup()
    seen = []

    def handler(text):
        seen.append(text)
        return f"re: {text}"

    def cb(data, mid):
        seen.append("cb:" + data)
        return "bouton"
    UPDATES.extend([upd(1, "bonjour", user=777, thread=5), upd(2, "salut", user=1, thread=5), upd(3, None, user=777, thread=5, data="x|1"),
                    upd(4, None, user=1, thread=6, data="y|2"), upd(5, "/top@KellyBot", user=1), upd(6, "/top@AutreBot", user=1), upd(7, "/top", user=1, thread=5, forum=False)])
    telegram.poll(handler, on_callback=cb)
    assert seen == ["salut", "cb:y|2", "/top", "/top"], seen
    out = [(k["text"], k.get("message_thread_id")) for k in sent()]
    note = ("↪️ Réponse dans le fil « 🎯 Combinés ».", None)
    assert out == [("re: salut", 5), ("bouton", 6), note, ("re: /top", 11), note, ("re: /top", 11)], out
    print("ok  groupe : seul le créateur obéit (messages et boutons), réponse dans le fil de la demande, /top@NomDuBot compris, autre bot ignoré, commande du fil général : réponse dans le fil de son thème")
    setup()
    STATE["creator"] = None
    UPDATES.append(upd(1, "salut", user=1, thread=5))
    telegram.poll(handler)
    assert not sent(), "créateur introuvable : personne ne passe"
    setup("42", topics=False)
    UPDATES.append({"update_id": 1, "message": {"chat": {"id": 42}, "message_id": 1, "text": "privé", "from": {"id": 42}}})
    seen.clear()
    telegram.poll(handler)
    assert seen == ["privé"] and "message_thread_id" not in sent()[0]
    assert not [m for m, _ in CALLS if m in ("getChatAdministrators", "getMe")], "chat privé : aucun appel de plus"
    print("ok  créateur introuvable : personne ne passe ; chat privé : aucun appel de plus")


def test_general_routes_to_theme():
    setup()
    assert [telegram.route_kind(t) for t in ("/bilan", "🔝 Du jour", "courbes", "/status@KellyBot", "🔴 Live", "5,40 1000", "Lens", "🎯 Combiné", "/combi 5")] == \
        ["report", "combos", "report", "tech", "live", None, None, "combos", "combos"]
    UPDATES.extend([upd(1, "/bilan", user=1), upd(2, "Lens", user=1)])
    telegram.poll(lambda t: f"re: {t}")
    out = [(k["text"], k.get("message_thread_id")) for k in sent()]
    assert out == [("↪️ Réponse dans le fil « 📊 Bilan ».", None), ("re: /bilan", 14), ("re: Lens", None)], out
    print("ok  commande écrite dans le fil général : réponse dans le fil de son thème (avec un mot dans le général), autre texte : reste dans le général")


def test_defer_keeps_thread():
    setup()
    seen = []
    real = store.data_lock

    class Busy:
        def __init__(self, wait=0, on_busy=None):
            raise store.LockTimeout("pris")
    store.data_lock = Busy
    try:
        UPDATES.append(upd(1, "plus tard", user=1, thread=5))
        telegram.poll(lambda t: seen.append(t) or "fait", defer=True)
    finally:
        store.data_lock = real
    assert store.load(telegram.LATER, [])[0]["thread"] == 5 and sent()[0]["message_thread_id"] == 5, "BUSY dans le fil de la demande"
    CALLS.clear()
    telegram._run_later(lambda t: seen.append(t) or "fait", None)
    assert seen == ["plus tard"] and sent()[0]["message_thread_id"] == 5 and sent()[0]["text"] == "fait"
    print("ok  message mis de côté : le fil est gardé, la réponse y revient")


def test_setup_topics():
    setup(topics=False)
    ok, txt = telegram.setup_topics()
    assert ok and len([1 for m, _ in CALLS if m == "createForumTopic"]) == 5 and set(store.load(telegram.TOPICS_FILE, {})["ids"]) == set(telegram.TOPIC_NAMES)
    assert "ÉTEINTS" in txt
    CALLS.clear()
    ok, txt = telegram.setup_topics()
    assert ok and not [1 for m, _ in CALLS if m == "createForumTopic"] and "Rien à créer" in txt, "idempotent"
    CALLS.clear()
    ok, _ = telegram.setup_topics(refaire=True)
    assert ok and len([1 for m, _ in CALLS if m == "createForumTopic"]) == 5
    STATE["forum"] = False
    assert not telegram.setup_topics()[0]
    STATE.update(forum=True, admin=False)
    assert not telegram.setup_topics()[0]
    setup("42", topics=False)
    ok, txt = telegram.setup_topics()
    assert not ok and "chat privé" in txt and not [1 for m, _ in CALLS if m == "createForumTopic"]
    print("ok  kelly fils : crée les 5 fils, idempotent, « refaire » les recrée, refuse chat privé / sans sujets / sans droit")


def main():
    saved = (telegram.api, telegram.creds, C.TOPICS)
    try:
        test_off_is_unchanged()
        test_routing()
        test_dead_thread_and_outbox()
        test_file_fallback()
        test_quiet_keeps_topic()
        test_poll_security_and_threads()
        test_general_routes_to_theme()
        test_defer_keeps_thread()
        test_setup_topics()
    finally:
        telegram.api, telegram.creds, C.TOPICS = saved
    print("tests fils de discussion : OK")


if __name__ == "__main__":
    main()
