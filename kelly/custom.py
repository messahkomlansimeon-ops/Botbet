"""Combinés sur mesure (« combi 20 ») que l'on joue d'un appui sur « ✅ Je le joue ».

Un combiné demandé à la main n'est suivi que si l'utilisateur le dit : sinon le bilan compterait des combinés jamais joués. À l'envoi
(app.custom_combo), il est seulement gardé en mémoire dans data/combi_sent.json ({"day", "n", "items": {"m<n>": {...}}} : un document par
jour, KEEP combinés au plus) et son message porte le bouton « ✅ Je le joue » (callback « cjoue|m<n>|<jour> »). L'appui (track) en fait une
ligne de data/fair_combos.json (palier « m<n> », marque « custom ») exactement comme un combiné du matin : « 🧾 Mes paris » (nom « Combi sur
mesure (cote 20) »), « ❌ Pas joué » / « ✏️ Autre cote », règlement gratuit, bilan, argent, bouton « 🎟️ Vérifier ce combiné » (app.refresh_combo)
et alertes 1xBet (kelly/xalerts.py). Jamais remplacé tout seul à 15:00 (le ticket est déjà placé) ni compté dans la fiabilité des combinés
du matin (settle.fair_combo_line : choisi par l'utilisateur, pas par Kelly). Un combiné aux cotes de référence d'autres bookmakers (jamais
chiffré par 1xBet) n'a pas ce bouton.
"""
import re

from . import chat, fair, fmt, money, played, store, telegram

FILE = "combi_sent.json"
KEEP = 30                       # combinés gardés en mémoire par jour : les plus anciens sortent
GONE = "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
LABEL = "✅ Je le joue"


def _load(day):
    """Document du jour `day` ; vide si un autre jour y est écrit (la numérotation « m<n> » repart de 1 chaque jour)."""
    d = store.load(FILE, {})
    if isinstance(d, dict) and d.get("day") == day and isinstance(d.get("items"), dict):
        return d | {"n": max(d["n"] if isinstance(d.get("n"), int) else 0, len(d["items"]))}
    return {"day": day, "n": 0, "items": {}}


def remember(day, target, lo, hi, combo, stake=None, cur=None, coupon=None, at=None, src="pronos"):
    """Garde en mémoire le combiné sur mesure `combo` (jambes, cote, proba) qui vient d'être envoyé -> son identifiant « m<n> » (None sans
    jour : rien à suivre). `target` : la cote demandée, `lo` / `hi` : la fourchette de cote visée (reprise par « Vérifier ce combiné »),
    `stake` : la mise affichée dans le message (devise `cur` avec un budget du jour, unités sinon), `coupon` : son code coupon 1xBet,
    `at` : l'heure « HH:MM » de ses cotes, `src` : « pronos » (pronostics 💎 / à cote mini) ou « picks » (paris réels remis au prix 1xBet)."""
    day = str(day or "")[:10]
    if not day:
        return None
    legs, total, prob = combo
    doc = _load(day)
    doc["n"] += 1
    cid = f"m{doc['n']}"
    doc["items"][cid] = {"legs": [fair.combo_leg(c) for c in legs], "total": round(total, 3), "prob": round(prob, 5), "target": target,
                         "lo": round(lo, 3), "hi": round(hi, 3), "stake": stake, "cur": cur, "coupon": coupon, "at": at, "src": src}
    while len(doc["items"]) > KEEP:
        doc["items"].pop(next(iter(doc["items"])))
    store.save(FILE, doc)
    return cid


def button(cid, day):
    """Bouton « ✅ Je le joue » du combiné `cid` du jour `day` (64 octets au plus : « cjoue|m12|2026-10-09 »)."""
    return telegram.inline([[(LABEL, f"cjoue|{cid}|{str(day)[:10]}")]])


def with_button(msg, cid, day):
    """Le message `msg` d'un combiné sur mesure avec son bouton « ✅ Je le joue » ; inchangé sans identifiant (rien à suivre)."""
    return telegram.with_buttons(msg, button(cid, day)) if cid else msg


def _row(cid, day, item):
    """Ligne de suivi (fair_combos.json) d'un combiné gardé en mémoire : comme un combiné du matin, avec sa mise (devise `cur` avec un
    budget du jour, sinon la mise en unités affichée avec le message, sans devise), son code coupon et l'heure de ses cotes."""
    row = fair.combo_row(cid, (item["legs"], item["total"], item["prob"]), day, item.get("stake"), item.get("cur"),
                         item.get("coupon"), item.get("at"))
    if not item.get("cur"):
        row.pop("cur", None)
    return row | {"custom": True, "target": item["target"], "lo": item["lo"], "hi": item["hi"], "src": item.get("src") or "pronos"}


def _buttons(message_id, what, day, row):
    """Boutons d'un combiné devenu suivi : ceux de tous les paris du jour (« ❌ Pas joué » + « ✏️ Autre cote » avec un budget, « ✅ Je l'ai
    joué » sans), et « 🎟️ Vérifier ce combiné » s'il est aux cotes 1xBet. Aucun bouton quand sa mise est de 0 (trop petite pour le budget)."""
    if message_id:
        markup = chat.keep_refresh(chat.played_button(what, day, funded=bool(row.get("stake"))), what, day)
        telegram.edit_buttons(message_id, markup or {"inline_keyboard": []})


def track(cid, day, message_id=None):
    """Appui sur « ✅ Je le joue » : le combiné sur mesure `cid` du jour `day` devient un pari suivi (fair_combos.json), comme un combiné du
    matin. Avec un budget du jour il est compté joué à la mise affichée (boutons « ❌ Pas joué » / « ✏️ Autre cote ») ; sans budget Kelly
    demande à quelle cote il a été joué (flux de « ✅ Je l'ai joué »). Un deuxième appui ne crée rien de plus. `message_id` : le message du
    combiné, dont les boutons sont remplacés. -> la réponse."""
    day = str(day)[:10]
    what = f"v{cid}"
    row = next((r for r in fair.load_combos() if played.is_custom(r) and r.get("date") == day and str(r.get("tier")) == cid), None)
    if row is not None:                              # déjà suivi (deuxième appui) : rien n'est ajouté, les boutons sont remis
        if not row.get("skipped"):
            _buttons(message_id, what, day, row)
        name = played.custom_name(row)
        if row.get("skipped"):
            return f"{name} est dans « 🧾 Mes paris » mais tu l'as retiré (« ❌ Pas joué ») : « ↩️ Finalement joué » le remet."
        return f"✅ {name} est déjà dans « 🧾 Mes paris »" + (f" (joué à {fmt.odds_txt(row['played']['odds'])})." if row.get("played") else ".")
    item = _load(day)["items"].get(cid) if re.fullmatch(r"m\d{1,4}", str(cid)) else None
    if item is None:
        return GONE
    row = _row(cid, day, item)
    fair.record_custom(row, day)
    _buttons(message_id, what, day, row)
    name, real = played.custom_name(row), played.is_real(row)
    head = f"✅ Combiné suivi : {name}, {'cote' if real else 'cote mini'} {fmt.odds_txt(row['min_odds'])}"
    if row.get("cur"):                               # budget du jour : compté joué à la mise affichée
        return (f"{head}, mise {money.money(row['stake'], row['cur'])}.\nIl est dans « 🧾 Mes paris » et compté dans ton bilan "
                "(« ❌ Pas joué » le retire, « ✏️ Autre cote » corrige la cote)."
                + (" Kelly te prévient si un de ses paris n'est plus jouable chez 1xBet." if real else ""))
    if row.get("stake") == 0:                        # budget du jour, mise trop petite : suivi sans mise (comme un combiné du matin)
        return (f"{head}, mais ton budget du jour n'en laisse aucune mise (trop petite) : il est dans « 🧾 Mes paris » sans mise et "
                "n'est pas compté dans ton bilan.")
    return f"{head} : il est dans « 🧾 Mes paris ».\n" + chat.on_callback(f"joue|{what}|{day}", message_id)
