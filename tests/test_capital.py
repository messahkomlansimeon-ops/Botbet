"""Mode capital (hors ligne) : « capital 50000 », « perte max 5000 », « objectif 18000 ». Le capital suit les paris notés ; la limite du jour est la plus
petite de la perte max et de 10 % du capital, moins ce qui est déjà misé aujourd'hui ; plus de mise une fois la limite atteinte ; l'objectif est
affiché (ce que rapporte la mise, ce qu'il faudrait pour l'atteindre d'un coup), jamais utilisé pour grossir une mise ; sans capital, « Kelly ne
conseille pas de miser dessus » reste dit. Lancer : python -m tests.test_capital"""
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-capital-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, chat, fmt, money, store, telegram  # noqa: E402

TODAY = datetime.now(timezone.utc).date()


def combo(day, stake, odds, result=None, tier="🛡️ SÛR"):
    return {"date": str(day), "tier": tier, "keys": [], "odds": odds, "prob": 0.3, "result": result,
            "played": {"odds": odds, "stake": stake, "at": "10:00"}}


def reset(combos=()):
    store.save(money.FILE, {})
    store.save("bets.json", {"picks": [], "combos": list(combos)})
    store.save("tracked.json", [])
    store.save("fair_combos.json", [])
    money._cap_memo.clear()


def test_commands_and_limit():
    reset()
    assert not money.capital_mode() and not money.active()
    out = app.handle_command("capital 50000")
    txt = telegram.plain(out)
    assert txt.startswith("✅ 💼 Mon capital\n\n💰 Capital : 50 000 FCFA\n📅 Depuis le ") and "Limite : 5 000 FCFA" in txt and "perte max 5000" in txt, txt
    assert "<b>50 000 FCFA</b>" in out and "🏁 Ajoute un objectif : <i>objectif 18000</i>" in out and "📈 Hier" not in out, out    # pas de pari hier
    st = money.capital_state()
    assert st["capital"] == 50000 and st["limit"] == 5000 and st["left"] == 5000 and money.daily() == 5000 and money.active()
    out = telegram.plain(app.handle_command("perte max 5000"))
    assert "Reste : 5 000 FCFA\n▱▱▱▱▱▱▱▱▱▱ 0 %" in out and "Fixe ta perte max" not in out, out
    assert "Limite : 3 000 FCFA" in telegram.plain(app.handle_command("limite 3000")) and money.daily() == 3000      # la perte max plafonne
    app.handle_command("perte max 5 000")
    assert money.daily() == 5000
    out = telegram.plain(app.handle_command("objectif 18000"))
    assert "🏁 Objectif : +18 000 FCFA par jour" in out and "n'augmentera jamais une mise" in out, out
    assert chat.route("💼 Mon capital")[0] == "/capital" and chat.route("💰 Mon budget")[0] == "/budget"       # ancien bouton : toujours compris
    assert "mode capital" in app.handle_command("budget 5000")                     # pas de mélange des deux modes
    assert "💰 Capital : 2 000 FCFA" in telegram.plain(app.handle_command("capital 2k")) and money.capital_state()["limit"] == 200
    assert "Montant invalide" in app.handle_command("objectif abc") and "FCFA" in app.handle_command("capital 50 €")
    assert app.handle_command("capital 0").startswith("💼 Mode capital arrêté") and not money.active()
    assert "Fixe d'abord ton capital" in app.handle_command("objectif 18000")
    print("ok  commandes capital / perte max / objectif ; limite du jour = min(perte max, 10 % du capital)")


def test_follows_results_and_stops():
    reset()
    app.handle_command("capital 50000")
    app.handle_command("perte max 5000")
    yday = TODAY - timedelta(days=1)
    money._save_capital(since=str(yday))                                           # capital fixé hier
    store.save("bets.json", {"picks": [], "combos": [combo(yday, 2000, 4.0, "win"), combo(yday, 3000, 3.0, "loss", "⚖️ COSTAUD"),
                                                      combo(yday - timedelta(days=3), 9000, 2.0, "loss", "🎯 X")]})   # avant le capital : ignoré
    st = money.capital_state()
    assert st["capital"] == 50000 + 6000 - 3000 and st["yesterday"] == 3000, st
    assert "📈 Hier : +3 000 FCFA" in telegram.plain(money.capital_card())
    # montants à centimes (gains à cote décimale) : toujours arrondis au franc à l'écran
    store.save("bets.json", {"picks": [], "combos": [combo(yday, 5368.03, 2.0, "loss"), combo(TODAY, 4800, 3.0)]})
    card = telegram.plain(money.capital_card())
    assert "💰 Capital : 44 632 FCFA" in card and "📈 Hier : −5 368 FCFA" in card and not re.search(r"\d,\d", card), card
    assert "Déjà misé : 4 800 FCFA\nReste : 0 FCFA · limite atteinte" in card and "▰▰▰▰▰▰▰▰▰▰ 100 %" in card, card      # limite 4 450 (10 %)
    assert money.capital_line(html=False) == "💼 Capital 44 632 FCFA · reste aujourd'hui 0 / 4 450 FCFA"      # 10 % de 44 632, arrondi à 50
    store.save("bets.json", {"picks": [], "combos": [combo(TODAY, 4800, 3.0)]})
    card = telegram.plain(money.capital_card())
    assert "Reste : 200 FCFA\n▰▰▰▰▰▰▰▰▰▱ 96 %" in card, card
    # grosses pertes : le capital baisse, la limite aussi (10 % du capital passe sous la perte max), jamais l'inverse
    store.save("bets.json", {"picks": [], "combos": [combo(yday, 20000, 2.0, "loss")]})
    st = money.capital_state()
    assert st["capital"] == 30000 and st["limit"] == 3000, st
    # mises notées aujourd'hui : la limite se consomme, puis plus aucune mise ce jour-là
    store.save("bets.json", {"picks": [], "combos": [combo(TODAY, 2000, 3.0), combo(TODAY, 1000, 5.0, tier="⚖️ C")]})
    st = money.capital_state()
    assert st["staked"] == 3000 and st["left"] == 2000 and money.daily() == 2000, st
    store.save("bets.json", {"picks": [], "combos": [combo(TODAY, 5000, 3.0)]})
    assert money.daily() == 0 and money.active()
    assert money.allocate([{"kind": "pari", "tier": "pari", "prob": 0.7, "odds": 2.0}])[0] == [0]
    assert money.stake_words(0, 0.7, 2.0) == "pas de mise : limite du jour atteinte (5 000 FCFA misés aujourd'hui)"
    assert money.stake_words(0, 0.4, 2.0) == money.NO_EDGE                         # espérance négative : dit d'abord
    store.save("bets.json", {"picks": [], "combos": [combo(yday, 60000, 2.0, "loss")]})
    assert money.capital_state()["capital"] == 0 and "capital épuisé" in money.stake_words(0, 0.7, 2.0)
    assert "⛔ Capital épuisé : écris capital 50000 pour repartir." in telegram.plain(money.capital_card())
    print("ok  le capital suit les paris notés ; la limite baisse après des pertes ; plus de mise une fois la limite atteinte")


def test_display_and_goal():
    reset()
    app.handle_command("capital 50000")
    app.handle_command("perte max 5000")
    app.handle_command("objectif 18000")
    assert money.stake_words(1500, 0.3, 5.0) == "1 500 FCFA → si ça passe : +6 000 FCFA"
    # 18 000 à cote 5 : 4 500 à miser, 90 % de la limite : trop risqué ; à cote 40 : 500, raisonnable ; à cote 2 : au-dessus de la limite
    assert money.goal_line(5.0) == "🎯 Pour +18 000 FCFA d'un coup : il faudrait miser 4 500 FCFA (90 % de ta limite du jour) ⚠️ trop risqué"
    assert money.goal_line(40.0) == "🎯 Pour +18 000 FCFA d'un coup : il faudrait miser 500 FCFA (10 % de ta limite du jour)"
    assert money.goal_line(2.0).endswith("⚠️ au-dessus de ce qu'il te reste aujourd'hui (5 000 FCFA)")
    leg = {"key": "k1", "id": "e1", "home": "A", "away": "B", "start": "19:00", "league": "L", "sport_key": "soccer_epl", "market": "h2h",
           "pick": "A gagne", "odds": 2.5, "prob": 0.5, "p_x": 0.45}
    legs = [leg, leg | {"key": "k2", "id": "e2", "home": "C", "away": "D"}]
    msg = str(fmt.combo("🛡️ SÛR (cote 5 à 7)", legs, 6.25, 0.25, stake_txt=money.stake_words(1500, 0.25, 6.25)))
    assert "💰 Mise : <b>1 500 FCFA → si ça passe : +7 875 FCFA</b>\n<i>🎯 Pour +18 000 FCFA d'un coup : il faudrait miser 3 450 FCFA (69 % de ta " \
           "limite du jour) ⚠️ trop risqué</i>" in msg, msg
    assert "🎯 Pour" not in str(fmt.combo("🛡️ SÛR", legs, 6.25, 0.25, stake_txt=money.NO_EDGE))      # pas de mise conseillée : pas d'objectif affiché
    assert app.money_line([]).startswith("💼 Capital 50 000 FCFA · reste aujourd'hui 5 000 / 5 000 FCFA · objectif +18 000 FCFA (écris « capital »")
    # sans capital ni budget : pas de montant, mais l'espérance négative reste dite
    reset()
    assert money.stake_words(None, 0.4, 2.0) == money.NO_EDGE and money.stake_words(None, 0.6, 2.0) is None
    assert "fixe ton capital avec « 💼 Mon capital »" in str(fmt.combo("🛡️ SÛR", legs, 6.25, 0.25))
    print("ok  « si ça passe : +X », objectif affiché et jamais forcé, ligne du bilan ; sans capital : espérance négative dite")


class Ticket:
    """Un pari trouvé (played.find) réduit à ce que played.note lit."""
    def __init__(self, ref, ref_word="cote 1xBet"):
        self.item, self.ref, self.stake, self.label, self.ref_word = {"cur": "FCFA", "date": str(TODAY)}, ref, 0, "combi sur mesure (cote 50)", ref_word
        self.save = lambda: None


def test_note_confirmation():
    from kelly import played
    reset()
    out = played.note(Ticket(50.52), 50.52, 10, ref_at="12h06")
    assert out.splitlines() == ["✅ Noté · combi sur mesure (cote 50)", "💰 10 FCFA à 50,52 → si ça passe : +495 FCFA",
                                "🕒 Cote lue par Kelly à 12h06. Si 1xBet t'a payé autre chose : « ✏️ Autre cote »."], out     # écart nul : pas de ligne
    assert "📈 3 % de mieux que la cote 1xBet (49,05)" in played.note(Ticket(49.05), 50.52, 10)
    assert "📉 4 % de moins que la cote mini (52,50)" in played.note(Ticket(52.5, "cote mini"), 50.52, 10)
    assert "📈" not in played.note(Ticket(50.2), 50.52, 10) and "💼" not in out                    # moins de 1 % ; pas de capital : pas de ligne 💼
    app.handle_command("capital 50000")
    app.handle_command("perte max 5000")
    store.save("bets.json", {"picks": [], "combos": [combo(TODAY, 4800, 3.0)]})
    assert "💼 Reste aujourd'hui : 200 / 5 000 FCFA" in played.note(Ticket(50.52), 50.52, 10)
    print("ok  confirmation « ✅ Noté » : gain si ça passe, écart dit à partir de 1 %, reste du jour en mode capital")


def test_over_limit():
    from kelly import played
    reset()
    assert money.over_limit(10 ** 6) is None                                       # hors mode capital : jamais
    app.handle_command("capital 50000")
    app.handle_command("perte max 5000")
    store.save("bets.json", {"picks": [], "combos": [combo(TODAY, 4800, 3.0)]})
    money._cap_memo.clear()
    assert money.over_limit(600) == 200 and money.over_limit(200) is None          # 600 conseillés à l'envoi, 200 restants : la mise sera demandée
    store.save("bets.json", {"picks": [], "combos": [combo(TODAY, 5600, 3.0)]})
    money._cap_memo.clear()
    out = played.note(Ticket(50.52), 50.52, 10)
    assert "⚠️ Limite du jour dépassée de 600 FCFA" in out and out.startswith("✅ Noté"), out        # noté (c'est ce qui a été joué), mais dit
    print("ok  mode capital : mise conseillée au-delà du reste du jour -> demandée ; dépassement dit dans la confirmation")


def main():
    test_commands_and_limit()
    test_follows_results_and_stops()
    test_display_and_goal()
    test_note_confirmation()
    test_over_limit()
    print("tests mode capital : OK")


if __name__ == "__main__":
    main()
