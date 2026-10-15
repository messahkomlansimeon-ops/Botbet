"""Mises rapides (hors ligne) : sous la question « quelle mise ? » (cote 1xBet connue, pas de mise conseillée), des boutons 500 / 1 000 / 2 000 / 5 000 ; un appui = la mise tapée (mêmes
contrôles, question refermée, boutons retirés, un 2e appui n'ajoute rien) ; zone de saisie avec un texte indicatif. Lancer : python -m tests.test_quick_stakes"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-quick-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-quick-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from tests import test_audit_fix as F, test_notes_fcfa as N  # noqa: E402
from kelly import chat, config as C, money, played, store, telegram  # noqa: E402

DAY = F.DAY


def datas(msg):
    return [b["callback_data"] for r in msg.markup["inline_keyboard"] for b in r]


def test_quick_stakes():
    F.world_known(budget=0)
    chat.clear_pending()
    with N.Fakes() as f:
        ask = chat.on_callback(f"joue|sur|{DAY}", 31)
        assert ask.startswith(played.NEED_STAKE) and datas(ask) == [f"st|{q}|sur|{DAY}" for q in (500, 1000, 2000, 5000)], (str(ask), getattr(ask, "markup", None))
        assert [b["text"] for r in ask.markup["inline_keyboard"] for b in r] == ["500", "1 000", "2 000", "5 000"]
        assert chat.get_pending() and chat.get_pending()["odds"], "la question reste ouverte jusqu'à la mise"
        out = chat.on_callback(f"st|1000|sur|{DAY}", 31)
        c = next(x for x in store.load("bets.json", {})["combos"] if x.get("tier") == N.SUR)
        assert str(out).startswith("✅ Noté") and c["played"]["stake"] == 1000, (out, c.get("played"))
        assert chat.get_pending() is None and (31, {"inline_keyboard": []}) in f.edits, "question refermée, boutons retirés"
        again = chat.on_callback(f"st|2000|sur|{DAY}", 31)
        c2 = next(x for x in store.load("bets.json", {})["combos"] if x.get("tier") == N.SUR)
        assert "n'est plus ouverte" in again and c2["played"]["stake"] == 1000, "un 2e appui n'ajoute rien"
    # une mise rapide ne répond jamais à une autre question (cote du combiné, nombre de paris)
    chat.set_pending(chat.COMBI_ASK, "", 40)
    assert "n'est plus ouverte" in chat.on_callback(f"st|500|sur|{DAY}", 40)
    chat.clear_pending()
    assert "n'est plus ouverte" in chat.on_callback(f"st|500|sur|{DAY}", 41), "aucune question ouverte"
    assert chat.on_callback(f"st|abc|sur|{DAY}", 41) is None, "montant illisible : ignoré"
    print("ok  mises rapides : 500 / 1 000 / 2 000 / 5 000, un appui = la mise tapée, boutons retirés, 2e appui et autres questions sans effet")


def test_old_question_and_zero():
    """Le bouton d'une ANCIENNE question ne note jamais la mise d'un autre pari ; « st|0 » n'est pas une mise."""
    F.world_known(budget=0)
    chat.clear_pending()
    with N.Fakes():
        chat.on_callback(f"joue|sur|{DAY}", 51)                 # question du pari « sur » (boutons st|…|sur|jour)
        chat.on_callback(f"joue|pari|{DAY}", 52)                # puis question d'un AUTRE pari : le pending devient « pari »
        assert chat.get_pending()["what"] == "pari"
        out = chat.on_callback(f"st|1000|sur|{DAY}", 51)        # appui sous la première question
        assert "n'est plus ouverte" in out and chat.get_pending()["what"] == "pari", out
        assert not any(x.get("played") for x in store.load("bets.json", {})["combos"] + store.load("bets.json", {})["picks"]), "aucune mise notée nulle part"
        chat.on_callback(f"st|0|pari|{DAY}", 52)
        assert chat.get_pending() and chat.get_pending()["what"] == "pari", "une mise à 0 n'est pas prise : la question reste ouverte"
        assert not any(x.get("played") for x in store.load("bets.json", {})["combos"] + store.load("bets.json", {})["picks"])
    print("ok  ancienne question : le bouton ne note rien sur un autre pari ; « st|0 » refusé")


def test_limit_and_placeholder():
    msg = chat._stake_buttons("mise ?", "sur", DAY, left=1500)
    assert datas(msg) == [f"st|500|sur|{DAY}", f"st|1000|sur|{DAY}"], "jamais plus que ce qu'il reste sur la limite du jour"
    assert chat._stake_buttons("mise ?", "sur", DAY, left=100) == "mise ?", "rien à proposer : la question seule"
    C.QUICK_STAKES = (300, 700)
    try:
        assert datas(chat._stake_buttons("mise ?", "sur", DAY)) == [f"st|300|sur|{DAY}", f"st|700|sur|{DAY}"]
    finally:
        C.QUICK_STAKES = (500.0, 1000.0, 2000.0, 5000.0)
    kb = chat.menu()
    assert kb["input_field_placeholder"] and len(kb["input_field_placeholder"]) <= 64, "texte indicatif (Telegram : 64 caractères au plus)"
    print("ok  mises rapides limitées par la limite du jour, réglage QUICK_STAKES, zone de saisie avec texte indicatif")


def main():
    test_quick_stakes()
    test_old_question_and_zero()
    test_limit_and_placeholder()
    print("tests mises rapides : OK")


if __name__ == "__main__":
    main()
