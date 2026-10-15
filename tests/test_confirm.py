"""Confirmation avant une action grave (hors ligne) : annuler une note, changer ou arrêter le capital, changer ou retirer la perte max, supprimer le budget, régler à la main. Un message
« ⚠️ Confirmer ? » avec ✅ / ❌ ; rien ne change avant ✅ ; ✅ une seule fois ; ❌ ou délai dépassé : rien ; première fixation et changement de budget : sans confirmation.
Lancer : python -m tests.test_confirm"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-confirm-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-confirm-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import app, chat, confirm, money, store, telegram  # noqa: E402

EDITS = []
telegram.edit_buttons = lambda mid, markup, chat_id=None: EDITS.append((mid, markup)) or True


def datas(msg):
    return [b["callback_data"] for r in msg.markup["inline_keyboard"] for b in r]


def test_capital_and_budget():
    store.save(money.FILE, {})
    assert "50 000" in telegram.plain(app.handle_command("capital 50000")), "première fixation : sans confirmation"
    assert money.capital_state()["capital"] == 50000
    ask = app.handle_command("capital 20000")
    assert "Confirmer" in telegram.plain(ask) and "50 000 FCFA à 20 000 FCFA" in telegram.plain(ask), ask
    yes, no = datas(ask)
    assert yes.startswith("cf|") and no.startswith("cn|") and yes[3:] == no[3:]
    assert money.capital_state()["capital"] == 50000, "rien n'a changé avant ✅"
    out = chat.on_callback(yes, 77)
    assert money.capital_state()["capital"] == 20000 and "20 000" in telegram.plain(out) and EDITS[-1] == (77, {"inline_keyboard": []})
    assert "trop ancienne ou déjà traitée" in chat.on_callback(yes, 77), "un 2e appui sur ✅ ne refait rien"
    assert money.capital_state()["capital"] == 20000
    # le même montant ou une perte max posée pour la première fois : sans confirmation
    assert "Confirmer" not in telegram.plain(app.handle_command("capital 20000"))
    assert "Confirmer" not in telegram.plain(app.handle_command("perte max 600"))
    ask = app.handle_command("perte max 0")
    assert "retirer ta perte max" in telegram.plain(ask)
    assert "Annulé" in chat.on_callback(datas(ask)[1], 78) and money.capital_state()["max_day"] == 600, "❌ : rien ne change"
    ask = app.handle_command("capital 0")
    assert "arrêter le mode capital" in telegram.plain(ask) and money.capital_mode()
    assert "Mode capital arrêté" in telegram.plain(chat.on_callback(datas(ask)[0], 79)) and not money.capital_mode()
    # budget : le supprimer demande confirmation, le changer non
    assert "Confirmer" not in telegram.plain(app.handle_command("budget 5000")) and money.daily() == 5000
    assert "Confirmer" not in telegram.plain(app.handle_command("budget 7000")) and money.daily() == 7000
    ask = app.handle_command("budget 0")
    assert "supprimer ton budget du jour" in telegram.plain(ask) and money.daily() == 7000
    chat.on_callback(datas(ask)[0], 80)
    assert not money.active()
    assert "Confirmer" not in telegram.plain(app.handle_command("budget 0")), "rien à supprimer : pas de confirmation"
    print("ok  capital, perte max et budget : ✅ / ❌, rien avant ✅, un seul ✅, première fixation et changement de budget sans confirmation")


def test_expiry_and_manual_settle():
    ask = app.handle_command("réglé vm3 gagné")
    assert "Confirmer" in telegram.plain(ask) and "régler à la main" in telegram.plain(ask)
    cid = datas(ask)[0].split("|")[1]
    box = store.load(confirm.FILE, {})
    box[cid]["at"] = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    store.save(confirm.FILE, box)
    assert "trop ancienne" in chat.on_callback(f"cf|{cid}", 81), "demande périmée : rien n'est fait"
    ask = app.handle_command("réglé vm3 gagné")
    out = chat.on_callback(datas(ask)[0], 82)
    assert "Confirmer" not in telegram.plain(out) and out, out            # exécuté (vm3 n'existe pas : message de la commande elle-même)
    assert confirm.check("/budget") is None and confirm.check("/regle") is None and confirm.check("") is None
    print("ok  « réglé » à la main : confirmation, délai de 15 min, exécuté après ✅")


def test_cancel_note():
    from kelly import played
    day = str(datetime.now(timezone.utc).date())
    real = (played.find, played.command, chat._refresh_msg)
    chat._refresh_msg = lambda *a, **k: None                          # le rafraîchissement des boutons du message d'origine a ses propres tests

    class T:
        label = "Sûr"
        item = {"played": {"odds": 2.0, "stake": 500}}
    done = []
    played.find = lambda what, d: T()
    played.command = lambda args, d, **kw: done.append((args, d)) or "Note annulée : Sûr."
    try:
        ask = chat.on_callback(f"annule|v5|{day}", 90)
        assert "annuler la note du Sûr" in telegram.plain(ask) and datas(ask)[0].startswith("cf|") and not done, "rien n'est annulé avant ✅"
        cid = datas(ask)[0].split("|")[1]
        assert store.load(confirm.FILE, {})[cid]["cb"] == f"annule|v5|{day}" and store.load(confirm.FILE, {})[cid]["msg"] == 90
        assert "Note annulée" in chat.on_callback(f"cf|{cid}", 91) and done == [(["v5", "0"], day)], done
        assert "trop ancienne" in chat.on_callback(f"cf|{cid}", 91) and len(done) == 1, "un 2e ✅ n'annule rien de plus"
        ask = chat.on_callback(f"annule|v5|{day}", 90)
        assert "Annulé : rien" in chat.on_callback(datas(ask)[1], 91) and len(done) == 1, "❌ : la note reste"
        T.item = {}                                                  # pas noté : pas de confirmation, la réponse habituelle
        assert "Confirmer" not in telegram.plain(chat.on_callback(f"annule|v5|{day}", 90))
        T.item = {"played": {"odds": 2.0, "stake": 500}}
        before = len(done)
        typed = app.handle_command("/joue v5 0")                     # la même annulation écrite : même confirmation
        assert "Confirmer" in telegram.plain(typed) and "annuler la note du Sûr" in telegram.plain(typed) and len(done) == before
    finally:
        played.find, played.command, chat._refresh_msg = real
    print("ok  ↩️ Annuler une note (bouton ou écrit) : confirmation, bouton d'origine rejoué après ✅ seulement, ❌ et 2e appui sans effet")


def test_bypass_and_stale():
    """Les écritures qui contourneraient la garde (relevées par la relecture) : « capital 0 » qui efface un budget, même montant (le suivi repart d'aujourd'hui), cote 0 écrite autrement, @robot, état changé."""
    store.save(money.FILE, {"daily": 5000})
    ask = app.handle_command("capital 0")
    assert "effacer ton budget du jour" in telegram.plain(ask) and money.daily() == 5000
    store.save(money.FILE, {"capital": 40000, "since": "2026-01-01"})
    ask = app.handle_command("capital 40000")
    assert "refixer ton capital" in telegram.plain(ask) and "2026-01-01" in telegram.plain(ask), ask
    store.save(money.FILE, {"capital": 40000, "since": str(money._today())})
    assert "Confirmer" not in telegram.plain(app.handle_command("capital 40000")), "déjà compté depuis aujourd'hui : rien ne change"
    store.save(money.FILE, {"capital": 40000, "since": "2026-01-01"})
    assert "Confirmer" in telegram.plain(confirm.check("/capital@KellyBot 0")), "le nom du robot ne contourne pas la garde"
    # l'état change entre la demande et ✅ : refusé
    ask = app.handle_command("capital 10000")
    store.save(money.FILE, {"capital": 80000, "since": "2026-01-01"})
    assert "a changé depuis cette demande" in chat.on_callback(datas(ask)[0], 95) and money.capital_state()["capital"] == 80000
    # cote à zéro écrite autrement
    from kelly import played
    real = played.find

    class T:
        label = "Sûr"
        item = {"played": {"odds": 2.0, "stake": 500}}
    played.find = lambda what, d: T()
    try:
        for typed in ("/joue v5 0", "/joue v5 0,00", "/joue v5 00", "/joue v5 0 1000"):
            assert "annuler la note du Sûr" in telegram.plain(confirm.check(typed) or ""), typed
        assert confirm.check("/joue v5 2,40 1000") is None and confirm.check("/joue v5 annule") is None, "une vraie cote, ou un mot : pas de confirmation"
    finally:
        played.find = real
    assert confirm.check("/regle aide") is None and confirm.check("/regle vm3") is None and confirm.check("/regle vm3 gagné") is not None
    print("ok  contournements fermés : capital 0 sur un budget, même montant, cote 0 écrite autrement, @robot, état changé avant ✅, /regle sans résultat")


def main():
    test_capital_and_budget()
    test_bypass_and_stale()
    test_expiry_and_manual_settle()
    test_cancel_note()
    print("tests confirmations : OK")


if __name__ == "__main__":
    main()
