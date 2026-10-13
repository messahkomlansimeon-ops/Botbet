"""Bouton « 📊 Marchés » (hors ligne) : bouton du menu et mots reconnus ; carte rangée en 🟢 solides / 🔴 à problème / ⏳ en attente, avec la fiabilité,
les marchés écartés des combinés et le compteur « nouvelle règle », qui de Kelly ou de 1xBet a raison (selon la force du signal), le rejeu.
Lancer : python -m tests.test_markets"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-markets-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, calib, chat, ensemble, markets, store, telegram  # noqa: E402


def ll(best, k):
    """Log-vraisemblance en cloche autour du poids d'indice `best` (k : netteté)."""
    return [-k * (i - best) ** 2 for i in range(40)]


def test_button_and_words():
    assert any("📊 Marchés" in row for row in chat.MENU) and chat.BUTTONS["📊 Marchés"] == "/marches"
    assert chat.route("📊 Marchés")[0] == chat.route("marchés")[0] == chat.route("Marches")[0] == "/marches"
    assert "📊 <b>Marchés</b>" in str(app.HELP)
    print("ok  bouton « 📊 Marchés » dans le menu, « marchés » compris, aide à jour")


def test_card():
    store.save(calib.FILE, {"goals": [131, 93, 90.4], "shots": [67, 30, 43.5], "corners": [49, 30, 31.9]})
    store.save(calib.BOOT_FILE, {"acc": {"corners": [18032, 11180, 11721], "cards": [6011, 3787, 3907]}})
    store.save(ensemble.FILE, {"fam": {"shots": {"m": {"ll": ll(0, 0.01), "n": 100}},             # avance sur moitié-moitié : 3,6 -> nette
                                       "corners": {"m": {"ll": ll(27, 0.01), "n": 165}},          # 0,5 -> égalité
                                       "offsides": {"m": {"ll": ll(6, 0.01), "n": 26}}}, "done": {}})
    calib._memo.clear()
    ensemble.reset()
    assert markets.who_is_right("shots") == "1xBet nettement devant (100)"
    assert markets.who_is_right("corners") == "Kelly et 1xBet à égalité (165)" and markets.who_is_right("offsides") == "trop peu de résultats (26)"
    assert markets.who_is_right("goals") == ""
    out = telegram.plain(app.handle_command("/marches"))
    good, bad, wait = (out.index(t) for t in ("🟢 Solides", "🔴 À problème", "⏳ Pas encore assez de résultats"))
    assert good < out.index("⚽ Buts · 71 % / 69 % (131) · juste") < bad, out
    assert bad < out.index("🎯 Tirs · 45 % / 65 % (67) · trop confiante · écartés des combinés · nouvelle règle 0/30") < wait, out
    assert "⚖️ 1xBet nettement devant (100)" in out, out
    assert out.index("🚩 Corners · 49/60 résultats · correction du rejeu ×0,") > wait and "⚖️ Kelly et 1xBet à égalité (165) · 📚 rejeu 62 % / 65 %" in out, out
    assert "🟨 Cartons · 0/60 résultats" in out and "🏁 Hors-jeu · 0/30 résultats" in out and "🎯 En tout : " in out, out
    store.save(calib.FILE, {})
    store.save(calib.BOOT_FILE, {})
    store.save(ensemble.FILE, {})
    calib._memo.clear()
    ensemble.reset()
    assert "Aucun résultat pour l'instant" in telegram.plain(markets.card())
    print("ok  carte des marchés : solides / à problème / en attente, écartés et compteur, Kelly ou 1xBet selon la force du signal, rejeu")


def test_typed_odds_after_picker():
    """Après « 🎯 Un combiné » (ou « 🔴 Live »), une cote ÉCRITE (sans toucher de bouton) est la cote voulue : jamais « Appuie d'abord sur ✅ Je l'ai
    joué »."""
    cmd, reply = chat.route("🎯 Un combiné")
    assert cmd is None and "écris-la" in str(reply), reply
    cmd, reply = chat.route("20")
    assert "Appuie d'abord" not in str(reply or "") and (cmd or reply), (cmd, reply)
    chat.route("🔴 Live")
    cmd, reply = chat.route("20")
    assert cmd == "/live 20" and reply is None, (cmd, reply)
    chat.clear_pending()
    assert "Appuie d'abord" in str(chat.route("5,40")[1]), "sans question ouverte : le rappel des paris joués"
    print("ok  cote écrite juste après « 🎯 Un combiné » / « 🔴 Live » : comprise comme la cote voulue")


def main():
    test_button_and_words()
    test_typed_odds_after_picker()
    test_card()
    print("tests bouton Marchés : OK")


if __name__ == "__main__":
    main()
