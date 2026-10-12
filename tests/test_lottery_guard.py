"""Garde-fou « loterie » et bilan lisible (hors ligne). Mode capital : un pari à moins de 5 % de chances a sa mise plafonnée à 5 % de la limite du jour
(250 FCFA pour 5 000), le message le dit ; à partir du 4e ticket noté dans la journée, la confirmation le signale sans rien bloquer. Bilan : l'argent
d'abord (arrondi, jauge de la mise récupérée), combinés par palier, fiabilité à pastilles, « Par championnat ». Lancer : python -m tests.test_lottery_guard"""
import os
import re
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-lottery-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from tests import test_capital as T  # noqa: E402
from kelly import app, calib, money, played, settle, store  # noqa: E402


def test_lottery_cap():
    T.reset()
    item = [{"kind": "combo", "tier": "🎯", "prob": 0.02, "odds": 60.0}]        # combiné audacieux : 2 % de chances, espérance +20 %
    big = money.allocate(item, budget=5000)[0][0]
    assert big > 250, big                                                       # sans capital : rien n'est plafonné
    assert money.lottery_cap() is None and "loterie" not in money.stake_words(big, 0.02, 60.0)
    app.handle_command("capital 50000")
    app.handle_command("perte max 5000")
    assert money.lottery_cap() == 250
    st = money.allocate(item)[0][0]
    assert st == 250, st                                                        # 750 sans le plafond
    assert money.stake_words(st, 0.02, 60.0) == "250 FCFA · ticket loterie, mise plafonnée → si ça passe : +14 750 FCFA"
    solid = [{"kind": "combo", "tier": "🛡️", "prob": 0.25, "odds": 6.0}]
    assert money.allocate(solid)[0][0] > 250                                    # 25 % de chances : pas un ticket loterie
    both = money.allocate(item + solid)[0]
    assert both[0] <= 250 and both[1] > 250, both                               # le plafond ne touche que le ticket loterie
    assert "loterie" not in money.stake_words(both[1], 0.25, 6.0)
    print("ok  ticket loterie (< 5 %) : mise plafonnée à 5 % de la limite du jour, dit dans le message ; rien hors mode capital")


def test_too_many_tickets():
    T.reset()
    app.handle_command("capital 50000")
    app.handle_command("perte max 5000")
    store.save("bets.json", {"picks": [], "combos": [T.combo(T.TODAY, 100, 3.0, tier=f"t{i}") for i in range(3)]})
    money._cap_memo.clear()
    assert money.capital_state()["tickets"] == 3
    out = played.note(T.Ticket(50.52), 50.52, 10)                               # le ticket factice n'est pas dans les données : toujours 3
    assert "⚠️" not in out, out
    store.save("bets.json", {"picks": [], "combos": [T.combo(T.TODAY, 100, 3.0, tier=f"t{i}") for i in range(4)]})
    out = played.note(T.Ticket(50.52), 50.52, 10)
    assert out.splitlines()[-1] == ("⚠️ 4e ticket aujourd'hui : au-delà de 3, tes tickets partagent souvent les mêmes matchs et les pertes "
                                    "s'additionnent."), out
    assert out.startswith("✅ Noté")                                            # signalé, jamais bloqué
    T.reset()
    assert "⚠️ 4e" not in played.note(T.Ticket(50.52), 50.52, 10)               # hors mode capital : rien
    print("ok  4e ticket du jour signalé (mode capital), la note est enregistrée ; tickets du jour comptés")


def test_bilan_card():
    T.reset()
    combos = [T.combo(T.TODAY, 1000, 2.0, "loss", tier=f"🎯 X{i}") for i in range(8)] + [T.combo(T.TODAY, 5368.03, 1.5, "loss", tier="⚖️ COSTAUD (cote 10)"),
                                                                                       T.combo(T.TODAY, 400, 3.0, "win", tier="🛡️ SÛR (cote 5 à 7)")]
    store.save("bets.json", {"picks": [], "combos": combos})
    store.save(calib.FILE, {"goals": [131, 93, 90.4], "shots": [67, 30, 43.5],
                            "_lg": {"soccer_xb_argentina_2": {"corners": [20, 12, 14.0]}}})
    calib._memo.clear()
    out = str(settle.bilan(store.load("bets.json", {})))
    lines = out.split("\n")
    i = lines.index("💰 <b>Ton argent</b> <i>(paris notés)</i>")
    assert lines[i + 1] == "7 derniers jours : −12 568 FCFA · 1 gagné sur 10", lines[i + 1]
    assert lines[i + 2] == "▱▱▱▱▱▱▱▱▱▱ 9 % de la mise récupérée", lines[i + 2]          # 1 200 rendus sur 13 768 misés (moins d'une case)
    assert "🛡️ SÛR · 1/1 · tes paris +800 FCFA" in out and "⚖️ COSTAUD · 0/1 · tes paris −5 368 FCFA" in out, out
    assert "🔴 Tirs · 67 · trop confiante (45 % / 65 %) → écartés des combinés" in out and "🟢 Buts · 131 · juste (71 % / 69 %)" in out, out
    assert "🏟 <b>Par championnat</b>\n🔴 Primera Nacional argentine · 20 · trop confiante (60 % / 70 %)" in out, out
    assert "meilleurs championnats" not in out and not re.search(r"\d,\d\d FCFA|-\d+ %", out), out    # montants arrondis, vrai signe moins
    print("ok  bilan : argent d'abord (arrondi, jauge), combinés par palier, fiabilité à pastilles, par championnat")


def main():
    test_lottery_cap()
    test_too_many_tickets()
    test_bilan_card()
    print("tests garde-fou loterie et bilan : OK")


if __name__ == "__main__":
    main()
