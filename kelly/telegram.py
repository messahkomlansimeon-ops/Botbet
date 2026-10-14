"""Envoi de messages et lecture des commandes Telegram."""
import calendar
import fcntl
import html as _html
import os
import re
import sys
import time

import requests

from . import config as C, store

LAST_ERROR = ""        # dernière erreur d'envoi (description renvoyée par l'API Telegram)
LAST_QUEUED = False    # le dernier envoi (send) a-t-il été mis en attente (data/outbox.json) plutôt que perdu ? (suivi des matchs : ses lignes partiront plus tard)
OUTBOX = "outbox.json"  # messages dont l'envoi a échoué (réseau / Telegram en panne), renvoyés dans l'ordre par flush_outbox
LATE_NOTE = "📬 Message en retard (réseau indisponible à l'heure prévue)"
BUSY = "⏳ Kelly est en plein travail (message du matin ou relevé horaire) : ton action sera faite dès qu'elle a fini."
BUSY_MORNING = "⏳ Kelly est en plein travail{} : ton action sera faite dès qu'elle a fini. Écris « état » pour suivre."
TIMEOUT = "⚠️ Kelly est restée trop longtemps occupée : ton action n'a pas pu être faite. Renvoie-la dans quelques minutes."
_last_fail = [float("-inf")]     # instant (monotonic) du dernier échec réseau : pas de renvoi automatique avant 60 s
# Connexion gardée ouverte entre deux appels (0,1 s au lieu de 0,3 s, et presque plus de connexions neuves). Connexion neuve bloquée
# (route IPv6 vers Telegram : ~1 sur 15 reste muette) : abandon au bout de CONNECT_TIMEOUT s et essai de l'adresse suivante (IPv4),
# au lieu d'attendre http_timeout (15 à 40 s) avant de répondre.
_session = requests.Session()
CONNECT_TIMEOUT = 3


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


def _button(label, data):
    """Un bouton : `data` = donnée renvoyée au clic (64 octets au plus, None = bouton inerte) ou {"copy_text": texte} = bouton qui copie `texte`
    dans le presse-papiers (Bot API 7.11 : « copy_text », aucun aller-retour avec Kelly)."""
    if isinstance(data, dict) and "copy_text" in data:
        return {"text": label, "copy_text": {"text": str(data["copy_text"])[:256]}}
    return {"text": label, "callback_data": data or "noop"}


def inline(rows):
    """Boutons sous un message : rows = [[(libellé, donnée renvoyée au clic, 64 octets au plus)]] ; donnée None = bouton inerte, donnée
    {"copy_text": texte} = bouton « copier » (voir copy_button)."""
    return {"inline_keyboard": [[_button(label, data) for label, data in row] for row in rows]}


def copy_button(code):
    """Le bouton « 📋 Copier le code XXXX » d'un code coupon : (libellé, donnée) pour inline(). Un appui copie le code, sans rien envoyer à Kelly."""
    return f"📋 Copier le code {code}", {"copy_text": code}


def copy_row(code):
    """La ligne de boutons « 📋 Copier le code XXXX » (format inline_keyboard, à ajouter sous les autres boutons) ; [] sans code."""
    return inline([[copy_button(code)]])["inline_keyboard"] if code else []


def menu_keyboard(rows):
    """Menu fixe en bas de Telegram, à la place du clavier : rows = [[libellé]] ; un appui envoie le libellé comme message."""
    return {"keyboard": [[{"text": label} for label in row] for row in rows], "resize_keyboard": True, "is_persistent": True}


def plain(text):
    """Texte brut d'un message HTML (console, repli quand Telegram refuse le HTML)."""
    return _html.unescape(re.sub(r"</?(b|i|u|s|code|pre)>", "", str(text)))


def _chunks(text, size=4000):
    """Morceaux de `size` caractères au plus, coupés de préférence entre deux lignes (jamais au milieu d'une balise). Une coupe qui laisserait un
    morceau minuscule (un simple en-tête avant une longue liste) n'est pas retenue : on coupe plutôt à la dernière fin de ligne."""
    out = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:                                    # un bloc trop court avant la coupe : couper à la fin de ligne la plus proche
            line = text.rfind("\n", 0, size)
            cut = line if line > cut else cut
        cut = cut if cut > 0 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text]


class TelegramError(Exception):
    def __init__(self, msg, code=None, maybe_sent=False):
        super().__init__(msg)
        self.code = code
        self.maybe_sent = maybe_sent        # envoi dont l'issue est INCONNUE (délai de lecture, coupure en route) : Telegram a peut-être déjà livré le message


def creds():
    return os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def _scrub(text):
    token, _ = creds()
    return str(text).replace(token, "***") if token else str(text)


def _not_sent(e):
    """L'erreur est survenue AVANT l'envoi de la requête (connexion impossible : délai de connexion, proxy, connexion neuve refusée) : la
    rejouer ne risque pas de doubler un message. Une connexion gardée coupée en route ou un délai de lecture : Telegram a pu le recevoir
    (comme sources.http._not_sent)."""
    if isinstance(e, (requests.ConnectTimeout, requests.exceptions.ProxyError)):
        return True
    # connexion gardée que Telegram a fermée pendant l'attente (« Connection aborted », RemoteDisconnected) : fermée AVANT de lire la
    # requête, le cas le plus courant ; la rejouer tout de suite évite de mettre la réponse en attente une minute
    return (isinstance(e, requests.ConnectionError) and not isinstance(e, requests.Timeout)
            and any(k in repr(e) for k in ("NewConnectionError", "RemoteDisconnected", "Connection aborted")))


def api(method, http_timeout=40, **payload):
    """Appel Bot API -> `result`. Lève TelegramError avec la description de l'API (sans le token). `http_timeout` : attente maximale
    de la réponse (une connexion coupée en silence n'est détectée qu'au bout de ce délai)."""
    token, _ = creds()
    if not token:
        raise TelegramError("TELEGRAM_TOKEN absent")
    for attempt in (0, 1):
        try:
            try:
                r = _session.post(f"https://api.telegram.org/bot{token}/{method}", json=payload,
                                  timeout=(CONNECT_TIMEOUT, http_timeout))
            except requests.ConnectionError as e:   # un nouvel essai, sur une connexion neuve, SEULEMENT si la requête n'est pas partie
                if method.startswith("send") and not _not_sent(e):      # coupure en route : Telegram a pu recevoir le message, le
                    raise                                               # renvoyer le doublerait (getUpdates, édition : sans risque)
                r = _session.post(f"https://api.telegram.org/bot{token}/{method}", json=payload,
                                  timeout=(CONNECT_TIMEOUT, http_timeout))
        except requests.RequestException as e:
            # un envoi (send*) qui a pu partir avant l'erreur (délai de lecture, coupure en route) a peut-être été livré : jamais remis en attente ni renvoyé
            raise TelegramError(f"réseau : {_scrub(e)}", maybe_sent=method.startswith("send") and not _not_sent(e)) from None
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


def _transient(e):
    """Panne passagère (réseau, 429, 5xx) : le message vaut d'être remis plus tard. Un 400 (message mal formé, chat inconnu) ne passera jamais. Un envoi
    dont l'issue est inconnue (e.maybe_sent : délai de lecture sur sendMessage) n'est PAS rejouable : le message est peut-être déjà chez toi, le remettre
    en attente le doublerait (le message du matin deux fois)."""
    if getattr(e, "maybe_sent", False):
        return False
    return e.code is None or e.code == 429 or e.code >= 500


def _deliver(part, is_html, markup, chat_id, reply_to=None):
    """Un morceau de message -> réponse de l'API ; lève TelegramError. HTML refusé par Telegram : repli en texte brut. `reply_to` : id d'un message
    auquel celui-ci répond (toucher la citation y ramène) ; message disparu : envoyé quand même, sans citation."""
    extra = {"reply_markup": markup} if markup else {}
    if reply_to:
        extra |= {"reply_to_message_id": reply_to, "allow_sending_without_reply": True}
    if is_html:
        try:
            return api("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML", disable_web_page_preview=True, **extra)
        except TelegramError as e:
            if "parse" not in str(e).lower() and "entities" not in str(e).lower():
                raise
            print("[telegram] HTML refusé, renvoi en texte brut", file=sys.stderr)
            part = plain(part)
    return api("sendMessage", chat_id=chat_id, text=part, **extra)


def _enqueue(parts, is_html, markup, chat_id, reply_to=None, ttl=None):
    """Met des morceaux en attente dans data/outbox.json (les boutons vont sur le dernier, la citation `reply_to` sur le premier). Ajout ATOMIQUE
    (store.update) : le bot Telegram et le suivi des matchs peuvent ajouter en même temps sans s'écraser. LAST_QUEUED passe à vrai (le message n'est
    pas perdu : il partira plus tard)."""
    global LAST_QUEUED
    now = time.time()
    new = [{"text": p, "html": is_html, "markup": markup if i == len(parts) - 1 else None, "chat_id": chat_id, "ts": now}
           | ({"reply_to": reply_to} if reply_to and i == 0 else {}) | ({"ttl": ttl} if ttl else {}) for i, p in enumerate(parts)]

    def add(box):
        box = (box if isinstance(box, list) else []) + new
        return box, len(box)
    try:
        n = store.update(OUTBOX, add, [])
    except OSError as e:
        print(f"[telegram] {len(parts)} message(s) NON mis en attente (base refusée : {e})", file=sys.stderr)
        return
    LAST_QUEUED = True
    print(f"[telegram] {len(parts)} message(s) mis en attente ({n} dans la file) : renvoi dès que le réseau revient", file=sys.stderr)


def _send_one(text, chat_id=None, queue=True):
    """Envoie `text` (découpé), sans ses pages suivantes (voir send). `Html` : parse_mode HTML, avec repli en texte brut si Telegram rejette le HTML. Les boutons du
    message (attribut `markup`, with_buttons) vont sur le dernier morceau. Sans identifiants : affiche. Ne lève jamais : renvoie
    l'id du dernier message envoyé (True sans identifiants), ou False et note l'erreur (stderr + LAST_ERROR) si Telegram refuse.
    Panne passagère (réseau, 429, 5xx) : le message est mis en attente (data/outbox.json, voir flush_outbox) derrière ceux qui
    attendent déjà, pour garder l'ordre ; un refus définitif (400) est abandonné. `queue=False` : message éphémère, jamais mis en attente."""
    global LAST_ERROR, LAST_QUEUED
    LAST_QUEUED = False
    token, chat = creds()
    chat_id = chat_id or chat
    if not (token and chat_id):
        print(plain(text) + "\n")
        return True
    is_html = isinstance(text, Html)
    parts, markup, sent = _chunks(str(text)), getattr(text, "markup", None), True
    reply_to, ttl = getattr(text, "reply_to", None), getattr(text, "ttl", None)
    explicit = chat_id if chat_id != chat else None
    if queue and store.load(OUTBOX, []):                        # des messages plus anciens attendent : d'abord eux
        if time.monotonic() - _last_fail[0] > 60:
            flush_outbox()
        if store.load(OUTBOX, []):
            _enqueue(parts, is_html, markup, explicit, reply_to, ttl)
            return False
    for i, part in enumerate(parts):
        try:
            sent = _deliver(part, is_html, markup if i == len(parts) - 1 else None, chat_id, reply_to if i == 0 else None)
        except TelegramError as e:
            LAST_ERROR = f"{e} (code {e.code})" if e.code else str(e)
            print(f"[telegram] envoi échoué : {LAST_ERROR}", file=sys.stderr)
            if e.maybe_sent:                                      # issue inconnue : ce morceau n'est pas remis en attente (doublon possible), les suivants le sont
                print("[telegram] délai sans réponse : le message a peut-être été livré, il n'est pas renvoyé", file=sys.stderr)
                if queue and parts[i + 1:]:
                    _last_fail[0] = time.monotonic()
                    _enqueue(parts[i + 1:], is_html, markup, explicit, ttl=ttl)
            elif queue and _transient(e):
                _last_fail[0] = time.monotonic()
                _enqueue(parts[i:], is_html, markup, explicit, reply_to if i == 0 else None, ttl)
            return False
    return (sent or {}).get("message_id", True) if isinstance(sent, dict) else True


def send(text, chat_id=None, queue=True):
    """Envoie `text` (voir _send_one) puis, dans l'ordre, ses pages suivantes (attribut `more` : une longue liste « Mes paris » a une page par message,
    chacune avec ses boutons). -> l'id du dernier message envoyé, ou False si l'un d'eux n'est pas parti (il est alors en attente, comme les suivants :
    l'ordre est gardé)."""
    results = [_send_one(m, chat_id, queue) for m in [text, *getattr(text, "more", ())]]
    return results[-1] if all(results) else False


def flush_outbox():
    """Renvoie dans l'ordre les messages mis en attente ; s'arrête au premier échec passager (le reste attend). Un message plus vieux
    que C.OUTBOX_MAX_HOURS h est abandonné (note sur stderr), un refus définitif aussi. Le premier message renvoyé, s'il a plus de
    2 minutes de retard, porte LATE_NOTE. Ne lève jamais. -> (envoyés, restants)."""
    token, chat = creds()
    box = store.load(OUTBOX, [])
    if not box or not token:
        return 0, len(box)
    lock = _outbox_lock()
    if lock is None:                                 # un autre processus vide déjà la file : jamais deux renvois du même message
        return 0, len(box)
    try:
        return _flush(box, chat)
    finally:
        lock.close()


def _outbox_lock():
    """Verrou NON bloquant data/.outbox.lock (fichier ouvert, à fermer) ; None s'il est déjà pris par un autre processus."""
    try:
        os.makedirs(store.DATA_DIR, exist_ok=True)
        f = open(store.path(".outbox.lock"), "a")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def _flush(box, chat):
    """Corps de flush_outbox (sous le verrou de la file). À la fin, ne retire de la file QUE les messages envoyés ou abandonnés ici : un message
    ajouté pendant le renvoi par un autre processus reste."""
    now = time.time()
    done = []                                        # entrées traitées (envoyées ou abandonnées), retirées de la file à la fin
    keep = [e for e in box if now - e.get("ts", now) <= (e.get("ttl") or C.OUTBOX_MAX_HOURS * 3600)]      # « ttl » : message du direct, vite périmé
    if len(keep) < len(box):
        print(f"[telegram] {len(box) - len(keep)} message(s) trop vieux abandonné(s) (direct : {C.WATCH_LIVE_TTL_MIN} min, autres : "
              f"{C.OUTBOX_MAX_HOURS} h)", file=sys.stderr)
        done += [e for e in box if e not in keep]
    sent = 0
    while keep:
        e = keep[0]
        target = e.get("chat_id") or chat
        text = e["text"]
        if target and sent == 0 and now - e.get("ts", now) > 120:
            text = f"{LATE_NOTE}\n\n{text}"
        try:
            if not target:
                raise TelegramError("TELEGRAM_CHAT_ID absent")
            _deliver(text, e.get("html"), e.get("markup"), target, e.get("reply_to"))
        except TelegramError as err:
            if err.maybe_sent:                       # issue inconnue : abandonné plutôt que doublé (note sur stderr), la suite continue
                print(f"[telegram] message en attente sans réponse (peut-être livré), abandonné : {err}", file=sys.stderr)
                _last_fail[0] = time.monotonic()
                done.append(keep.pop(0))
                continue
            if _transient(err) or not target:
                _last_fail[0] = time.monotonic()
                print(f"[telegram] renvoi des messages en attente échoué : {err}", file=sys.stderr)
                break
            print(f"[telegram] message en attente refusé définitivement, abandonné : {err}", file=sys.stderr)
        else:
            sent += 1
        done.append(keep.pop(0))
    if done:
        def drop(cur):
            cur, left = list(cur if isinstance(cur, list) else []), list(done)
            for e in list(cur):
                if e in left:                         # une entrée traitée ici (même contenu, même heure) : retirée une fois
                    left.remove(e)
                    cur.remove(e)
            return cur, len(cur)
        try:
            remaining = store.update(OUTBOX, drop, [])
        except OSError as e:
            print(f"[telegram] file des messages non mise à jour : {e}", file=sys.stderr)
            remaining = len(keep)
    else:
        remaining = len(keep)
    if sent:
        print(f"[telegram] {sent} message(s) en attente renvoyé(s), {remaining} restant(s)", file=sys.stderr)
    return sent, remaining


COMMANDS = [("combi", "Un combiné à la cote voulue"), ("top", "Les meilleurs paris du jour"), ("joue", "Mes paris du jour, à noter"), ("live", "Un combiné sur les matchs en cours"),
            ("bilan", "Mes résultats"), ("capital", "Mon capital et ma limite du jour"), ("saison", "La voie « saison en cours »"), ("tendances", "Corners, cartons, tirs par équipe"),
            ("marches", "Marché par marché : Kelly est-elle fiable ?"), ("status", "L'état de Kelly"), ("aide", "Mode d'emploi et boutons")]      # le menu « / » de Telegram


def set_commands(commands=None):
    """Enregistre le menu « / » (liste des commandes avec leur description) auprès de Telegram ; True si accepté, False sinon (jamais d'exception : cosmétique)."""
    try:
        api("setMyCommands", commands=[{"command": c, "description": d} for c, d in (commands or COMMANDS)])
        return True
    except Exception as e:      # noqa: BLE001 - sans réseau ou sans jeton : le clavier du bas marche quand même
        print(f"[telegram] menu « / » non enregistré : {e}", file=sys.stderr)
        return False


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


def pin(message_id, chat_id=None):
    """Épingle un message sans notification (le tableau du jour du suivi des matchs). -> vrai si c'est fait. Ne lève jamais."""
    token, chat = creds()
    if not (token and (chat_id or chat) and message_id):
        return False
    try:
        api("pinChatMessage", chat_id=chat_id or chat, message_id=message_id, disable_notification=True)
        return True
    except TelegramError as e:
        print(f"[telegram] message non épinglé : {e}", file=sys.stderr)
        return False


def unpin(message_id, chat_id=None):
    """Désépingle un message (le tableau de la veille). Ne lève jamais."""
    token, chat = creds()
    if not (token and (chat_id or chat) and message_id):
        return False
    try:
        api("unpinChatMessage", chat_id=chat_id or chat, message_id=message_id)
        return True
    except TelegramError as e:
        print(f"[telegram] message non désépinglé : {e}", file=sys.stderr)
        return False


GONE = ("message to edit not found", "message can't be edited", "message_id_invalid", "message not found")


def edit_text(message_id, text, chat_id=None):
    """Remplace le texte d'un message (tableau du jour) -> "ok" (fait, ou déjà identique), "gone" (message effacé ou plus modifiable : en envoyer un
    autre), "fail" (panne passagère : réessayer plus tard, SURTOUT pas un 2e message). Ne lève jamais."""
    token, chat = creds()
    if not (token and (chat_id or chat) and message_id):
        return "fail"
    payload = dict(chat_id=chat_id or chat, message_id=message_id, text=str(text), disable_web_page_preview=True)
    if isinstance(text, Html):
        payload["parse_mode"] = "HTML"
    try:
        api("editMessageText", **payload)
        return "ok"
    except TelegramError as e:
        low = str(e).lower()
        if "not modified" in low:
            return "ok"
        print(f"[telegram] texte non modifié : {e}", file=sys.stderr)
        return "gone" if any(g in low for g in GONE) else "fail"


def edit_message(message_id, text, markup=None, chat_id=None):
    """Remplace le texte ET les boutons d'un message déjà envoyé (la liste « Mes paris » après une note : ses lignes disent « joué à … »). Texte trop
    long pour un message (plus de 4096 caractères) ou refusé par Telegram : seuls les boutons sont remplacés. Ne lève jamais."""
    token, chat = creds()
    if not (token and (chat_id or chat) and message_id):
        return edit_buttons(message_id, markup or {"inline_keyboard": []}, chat_id)      # hors ligne : au moins les boutons passent par edit_buttons
    if len(str(text)) <= 4096:
        payload = dict(chat_id=chat_id or chat, message_id=message_id, text=str(text), disable_web_page_preview=True)
        if isinstance(text, Html):
            payload["parse_mode"] = "HTML"
        if markup:
            payload["reply_markup"] = markup
        try:
            api("editMessageText", **payload)
            return True
        except TelegramError as e:
            if "not modified" in str(e).lower():               # texte et boutons identiques : rien à faire
                return True
            print(f"[telegram] texte non modifié : {e}", file=sys.stderr)
    return edit_buttons(message_id, markup or {"inline_keyboard": []}, chat_id)


def _run(fn, *args):
    """Exécute fn(*args) et envoie sa réponse (« Erreur : ... » si elle plante)."""
    try:
        reply = fn(*args)
    except Exception as e:                          # noqa: BLE001 - on répond l'erreur
        reply = f"Erreur : {e}"
    if reply:
        send(reply)


def _handle(lock, fn, *args):
    """_run(fn, *args). Avec `lock` (appelable qui prend `on_busy` et renvoie un gestionnaire de contexte, ex. store.data_lock) : le tout sous
    le verrou des données ; verrou déjà pris -> BUSY tout de suite, puis attente ; attente épuisée -> TIMEOUT. Le verrou est relâché entre
    deux mises à jour, jamais tenu pendant l'attente longue de Telegram."""
    if lock is None:
        return _run(fn, *args)
    try:
        with lock(lambda: send(_busy(), queue=False)):
            _run(fn, *args)
    except store.LockTimeout:
        send(TIMEOUT, queue=False)


LATER = "telegram_later.json"       # serveur (poll defer=True) : messages et boutons reçus pendant un passage, traités dès que le verrou est libre
LATER_MAX_MIN = 60                  # ... sauf s'ils attendent depuis plus de N min (redémarrage, très long passage) : jamais rejoués sur d'autres données


def _run_later(handler, on_callback):
    """File des messages mis de côté (verrou pris) : traités dans l'ordre, chacun sous le verrou des données pris SANS attendre ; s'arrête dès
    qu'un passage le reprend. -> True si la file est vide."""
    later = store.load(LATER, [])
    while later:
        item = later[0]
        if _too_old(item):                          # trop vieux : le jour ou les paris ont pu changer, on ne le rejoue pas en silence
            later.pop(0)
            store.save(LATER, later)
            what = f"ton message « {item['args'][0]} »" if not item.get("cb") else "ton appui sur un bouton"
            send(f"⚠️ Kelly était occupée trop longtemps : {what} (à {str(item.get('at') or '')[11:16]}) n'a pas été fait. Renvoie-le si tu le veux "
                 "encore.", queue=False)
            continue
        fn = on_callback if item.get("cb") else handler
        try:
            with store.data_lock(wait=0):
                later.pop(0)
                store.save(LATER, later)            # retiré avant d'être traité : jamais fait deux fois (un plantage le perd, comme avant)
                if fn:
                    _run(fn, *item["args"])
        except store.LockTimeout:
            break
    return not later


def _too_old(item):
    """Message mis de côté depuis plus de LATER_MAX_MIN minutes (heure inconnue : trop vieux)."""
    try:
        at = calendar.timegm(time.strptime(item["at"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, TypeError, ValueError):
        return True
    return time.time() - at > LATER_MAX_MIN * 60


def _later(fn, args, cb):
    """Traite tout de suite sous le verrou s'il est libre et que rien n'attend avant ; sinon met de côté (ordre gardé) et répond BUSY : le
    serveur continue de lire les messages suivants (« état » répond pendant le passage du matin)."""
    later = store.load(LATER, [])
    if not later:
        try:
            with store.data_lock(wait=0):
                _run(fn, *args)
            return
        except store.LockTimeout:
            pass
    later.append({"cb": cb, "args": list(args), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    store.save(LATER, later)
    send(_busy(), queue=False)


def _busy():
    """Message « Kelly est en plein travail », avec la progression du passage du matin s'il est en cours (progress.busy_suffix)."""
    from . import progress
    suffix = progress.busy_suffix()
    return BUSY_MORNING.format(suffix) if suffix else BUSY


POLL_MARGIN = 15                 # s d'attente HTTP au-delà du long polling de getUpdates
POLL_PAUSE_MAX = 15              # s : pause maximale entre deux essais pendant une coupure (2, 4, 8, 15, 15…)
OUTAGE_REMIND = 10               # pendant une longue coupure : un rappel dans le journal tous les N échecs
_outage = {"n": 0, "t0": None}   # coupure en cours : nombre d'échecs de suite, début (monotonic)


def _outage_failed(e):
    """Échec de getUpdates : une ligne au premier échec d'une coupure, puis un rappel tous les OUTAGE_REMIND échecs (plus une ligne toutes les 2 s)."""
    _outage["n"] += 1
    if _outage["n"] == 1:
        _outage["t0"] = time.monotonic()
        print(f"[telegram] lecture des commandes échouée : {e}", file=sys.stderr)
    elif _outage["n"] % OUTAGE_REMIND == 0:
        print(f"[telegram] connexion toujours coupée : {_outage['n']} échecs depuis {round(time.monotonic() - _outage['t0'])} s ({e})", file=sys.stderr)


def _outage_back():
    """getUpdates a répondu : fin de la coupure en cours, s'il y en avait une (« connexion rétablie après N échec(s) (X s) »)."""
    if _outage["n"]:
        n, dur = _outage["n"], round(time.monotonic() - _outage["t0"])
        print(f"[telegram] connexion rétablie après {n} échec{'s' if n > 1 else ''} ({dur} s)", file=sys.stderr)
    _outage.update(n=0, t0=None)


def poll(handler, timeout=0, on_callback=None, lock=None, quick=None, defer=False):
    """Lit les nouveaux messages et appuis sur les boutons (une passe) et répond. Ne répond qu'au chat configuré : personne
    d'autre ne peut piloter le bot. `on_callback(donnée, id du message)` : réponse à un appui sur un bouton (None = rien à dire).
    `lock` : voir _handle (le serveur y met store.data_lock, pour ne pas écrire les données pendant le matin ou le relevé horaire).
    `quick(texte)` : réponse en lecture seule donnée SANS le verrou (« état » pendant le passage du matin), None pour le traitement normal.
    `defer` (serveur, à la place de `lock`) : verrou pris -> le message est mis de côté (BUSY) au lieu d'attendre, et traité à une passe
    suivante dès que le verrou est libre (_run_later) : un message en attente ne bloque plus les suivants."""
    _, chat = creds()
    if defer:
        _run_later(handler, on_callback)
    state = store.load("telegram.json", {"offset": 0})
    state = state if isinstance(state, dict) else {"offset": 0}
    state["offset"] = max(int(state.get("offset") or 0), _OFFSET[0])     # base illisible ou disque plein : la mémoire de ce processus fait foi
    start = state["offset"]
    try:
        # attente Telegram `timeout` s ; une connexion coupée en silence est détectée POLL_MARGIN s après. Marge large : sur réseau mobile
        # (téléphone qui sort de veille), 5 s ne suffisaient pas et chaque réponse un peu lente comptait comme une coupure.
        updates = api("getUpdates", http_timeout=timeout + POLL_MARGIN, offset=state["offset"], timeout=timeout,
                      allowed_updates=["message", "callback_query"]) or []
    except TelegramError as e:
        _outage_failed(e)
        if timeout:                                 # mode serveur : pause de plus en plus longue pendant une coupure, puis on se reconnecte
            time.sleep(min(2 * 2 ** min(_outage["n"] - 1, 4), POLL_PAUSE_MAX))
        return 0
    _outage_back()
    for u in updates:
        state["offset"] = _OFFSET[0] = max(_OFFSET[0], u["update_id"] + 1)
        cq = u.get("callback_query")
        if cq:
            answer_callback(cq.get("id"))           # tout de suite, avant une éventuelle attente du verrou : pas de sablier
            cmsg = cq.get("message") or {}
            data = cq.get("data") or ""
            if quick and data.startswith("q|") and str(cmsg.get("chat", {}).get("id")) == str(chat):      # « 🩺 Status » de « ➕ Plus » : répondu SANS le verrou, comme le mot écrit (même pendant le passage du matin)
                try:
                    fast = quick(data[2:])
                except Exception as e:                  # noqa: BLE001 - traitement normal à la place
                    print(f"[telegram] réponse rapide ignorée : {type(e).__name__}: {e}", file=sys.stderr)
                    fast = None
                if fast:
                    send(fast)
                    _save_offset(state)
                    continue
            if on_callback and str(cmsg.get("chat", {}).get("id")) == str(chat):
                if defer:
                    _later(on_callback, [cq.get("data") or "", cmsg.get("message_id")], True)
                else:
                    _handle(lock, on_callback, cq.get("data") or "", cmsg.get("message_id"))
            _save_offset(state)
            continue
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text or str(msg.get("chat", {}).get("id")) != str(chat):
            continue
        fast = None
        if quick:
            try:
                fast = quick(text)
            except Exception as e:                  # noqa: BLE001 - traitement normal à la place
                print(f"[telegram] réponse rapide ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        if fast:
            send(fast)
        elif defer:
            _later(handler, [text], False)
        else:
            _handle(lock, handler, text)
        _save_offset(state)                         # offset sauvé à chaque commande : jamais traitée deux fois
    if state["offset"] != start:                    # rien de nouveau (la plupart des passages du serveur) : pas d'écriture inutile
        _save_offset(state)
    return len(updates)


_OFFSET = [0]           # dernier décalage connu de ce processus : une écriture refusée (disque plein, base abîmée) ne fait jamais retraiter une commande


def _save_offset(state):
    try:
        store.save("telegram.json", state)
    except OSError as e:                            # gardé en mémoire (_OFFSET) : la commande n'est pas retraitée au passage suivant
        print(f"[telegram] décalage non enregistré ({e}) : gardé en mémoire", file=sys.stderr)


def send_document(filepath, caption=None, chat_id=None, silent=False):
    """Envoie un fichier (document) sur Telegram ; `silent` : sans sonnerie (sauvegarde automatique). Lève TelegramError en cas d'échec."""
    token, chat = creds()
    chat_id = chat_id or chat
    if not (token and chat_id):
        raise TelegramError("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant")
    with open(filepath, "rb") as f:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                              data={"chat_id": chat_id, "caption": caption or ""} | ({"disable_notification": "true"} if silent else {}),
                              files={"document": (os.path.basename(filepath), f)},
                              timeout=(30, 300))
        except requests.RequestException as e:
            raise TelegramError(f"réseau : {_scrub(e)}") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not body.get("ok"):
        raise TelegramError(body.get("description") or f"erreur HTTP {r.status_code}", body.get("error_code"))
    return body.get("result")

