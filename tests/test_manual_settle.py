"""« réglé vm3 gagné 12/10 » (hors ligne) : le résultat d'un ticket noté que le règlement gratuit ne trouve pas (petite ligue sans stats) se donne à la
main ; seulement un ticket noté pas encore réglé (ou non vérifiable), du bon jour ; gagné : payé à la cote notée (jambes ouvertes gagnées), perdu, ou
remboursé ; le capital le compte aussitôt ; gains au franc près. Lancer : python -m tests.test_manual_settle"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-manual-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, bankroll, chat, fair, money, played, store  # noqa: E402

TODAY = "2026-10-13"


def combo(tier, day, legs_res, stake=500, odds=26.89, result=None):
    legs = [{"key": f"{tier}{i}", "id": f"e{i}", "home": f"H{i}", "away": f"A{i}", "market": "alternate_totals_corners", "name": "Under",
             "point": 8.5, "commence": f"{day}T20:00:00Z", "prob": 0.7, "result": r, "xodds": 1.9} for i, r in enumerate(legs_res)]
    return {"date": day, "tier": tier, "custom": True, "min_odds": odds, "prob": 0.1, "keys": [x["key"] for x in legs], "legs": legs, "result": result,
            "cur": "FCFA", "played": {"odds": odds, "stake": stake, "at": f"{day}T12:00+00:00"}}


def reset(rows):
    store.save(fair.COMBOS_FILE, rows)
    store.save("bets.json", {"picks": [], "combos": []})
    store.save("tracked.json", [])
    store.save(money.FILE, {})
    money._cap_memo.clear()


def test_manual_settle():
    reset([combo("m3", "2026-10-12", ["win"] * 4 + [None]), combo("m3", TODAY, [None, None], stake=100, odds=5.0)])
    app.handle_command("capital 50000")
    money._save_capital(since="2026-10-01")
    assert chat.route("réglé vm3 gagné 12/10")[0] == "/regle vm3 gagné 12/10"
    out = played.settle_command(["m3", "gagné", "12/10"], TODAY)                 # « m3 » (nom du combi sur mesure) : compris comme « vm3 »
    assert out.startswith("✅ Réglé à la main · combi sur mesure (cote 26,9) du lundi 12 octobre : gagné → +12 945 FCFA"), out     # 500 x 25,89
    assert "💼 Capital : 62 945 FCFA" in out, out
    rows = fair.load_combos()
    old = next(r for r in rows if r["date"] == "2026-10-12")
    assert old["result"] == "win" and old["manual"] and all(x["result"] == "win" for x in old["legs"]), old
    assert next(r for r in rows if r["date"] == TODAY)["result"] is None, "celui du jour n'est pas touché"
    assert "déjà réglé" in played.settle_command(["vm3", "perdu", "12/10"], TODAY)
    assert played.settle_command(["vm3", "perdu"], TODAY).endswith(": perdu → −100 FCFA\n💼 Capital : 62 845 FCFA")      # sans date : celui du jour
    assert played.settle_command(["vm3", "gagné", "11/10"], TODAY).startswith("Pas de ")                       # aucun vm3 ce jour-là
    assert played.settle_command(["vm3"], TODAY) == played.SETTLE_HELP and played.settle_command([], TODAY) == played.SETTLE_HELP
    reset([combo("m4", TODAY, [None], result="unknown")])
    assert ": remboursé" in played.settle_command(["vm4", "remboursé"], TODAY) and not money.gain(fair.load_combos()[0])      # non vérifiable : réglable ; mise rendue, rien ne bouge
    unplayed = combo("m5", TODAY, [None])
    unplayed.pop("played")
    reset([unplayed])
    assert "n'est pas noté comme joué" in played.settle_command(["vm5", "gagné"], TODAY)
    assert played._day_arg("hier", TODAY) == "2026-10-12" and played._day_arg("12/10/2026", TODAY) == "2026-10-12" and played._day_arg("vm3", TODAY) is None
    assert played._day_arg("25/12", TODAY) == "2025-12-25", "une date à venir sans année : l'an dernier"
    print("ok  réglé à la main : gagné (jambes ouvertes gagnées, cote notée), perdu, remboursé ; bon jour ; jamais un ticket déjà réglé ou pas noté")


def test_ask_buttons():
    from datetime import datetime, timezone
    store.save(played.ASK_FILE, {})
    reset([combo("m3", "2026-10-12", ["win"] * 4 + [None]), combo("m1", TODAY, [None, None], stake=100, odds=5.0)])
    app.handle_command("capital 50000")
    money._save_capital(since="2026-10-01")
    early = played.ask_messages(datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))
    assert early == [], "matchs commencés il y a moins de 24 h : le règlement du matin a encore sa chance"
    msgs = played.ask_messages(datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc))
    assert len(msgs) == 1, msgs                                                # le combi du 13 : pas encore (moins de 24 h)
    m = msgs[0]
    txt = str(m)
    assert txt.startswith("❓ <b>Résultat introuvable</b> · combi sur mesure") and "❓ H4 – A4 · Moins de 8,5 corners" in txt, txt
    assert [b["callback_data"] for b in m.markup["inline_keyboard"][0]] == ["rg|vm3|2026-10-12|w", "rg|vm3|2026-10-12|l", "rg|vm3|2026-10-12|v"]
    assert played.ask_messages(datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc)) == []          # une seule fois
    edits = []
    old = chat.telegram.edit_buttons
    chat.telegram.edit_buttons = lambda mid, markup, chat_id=None: edits.append((mid, markup))
    try:
        out = chat.on_callback("rg|vm3|2026-10-12|w", 77)
        assert out.startswith("✅ Réglé à la main · combi sur mesure (cote 26,9) du lundi 12 octobre : gagné → +12 945 FCFA"), out
        assert edits == [(77, {"inline_keyboard": []})]                       # les boutons disparaissent
        assert "déjà réglé" in chat.on_callback("rg|vm3|2026-10-12|l", 77)   # double appui : rien ne change
    finally:
        chat.telegram.edit_buttons = old
    assert played.unsettled(datetime(2026, 10, 20, 4, 30, tzinfo=timezone.utc))[0][0] == "vm1"      # réglé : plus jamais demandé ; celui du 13 l'est ensuite
    print("ok  ❓ résultat introuvable : demandé une fois, 24 h après le dernier match, avec ses matchs ; un appui règle (✅ ❌ ↩️), les boutons disparaissent")


def test_gain_whole_fcfa():
    x = {"result": "win", "odds": 3.0, "played": {"odds": 3.075, "stake": 500}}
    assert bankroll.played_gain(x) == 1038.0                                    # 1 037,5 -> 1 038 : au franc près, comme 1xBet
    print("ok  gains au franc près (plus de « 1 037,5 FCFA »)")


def main():
    test_manual_settle()
    test_ask_buttons()
    test_gain_whole_fcfa()
    print("tests règlement à la main : OK")


if __name__ == "__main__":
    main()
