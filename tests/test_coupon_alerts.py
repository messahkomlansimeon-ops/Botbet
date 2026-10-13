"""Alertes par coupon (hors ligne) : ⏰ « Dans 5 min » avant le 1er match d'un coupon (matchs à la même heure groupés, une fois, sans « 🟢 coup
d'envoi » en plus, heure voulue écrite pour le runner), 🎉 coupon gagné (message à part, capital et objectif, conseil après un gros gain), 🔥 plus
qu'un match (puis « dernier match du coupon » sur sa ligne), boutons « 🎫 » et fiche du coupon, réponse au message du coupon (Telegram).
Lancer : python -m tests.test_coupon_alerts"""
import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-coupon-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, chat, exposure, money, store, telegram, watch  # noqa: E402

KO = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)
DAY = str(KO.date())


def leg(i, point=2.5, ko=KO):
    return {"id": f"e{i}", "key": f"e{i}|totals||{point}|Over", "home": f"H{i}", "away": f"A{i}", "market": "totals", "name": "Over",
            "point": point, "commence": ko.isoformat(), "pick": f"Plus de {str(point).replace('.', ',')} buts", "prob": 0.6}


def ticket(what, legs, coupon=None, stake=1000, odds=10.0, msg=None):
    return {"what": what, "tid": f"{DAY}|{what}", "name": f"Combi {what}", "stake": stake, "odds": odds, "legs": legs, "coupon": coupon, "msg": msg}


GAMES = {i: {"id": i, "start": KO.isoformat(), "home": {"id": 100 + i, "name": f"H{i}"}, "away": {"id": 200 + i, "name": f"A{i}"}} for i in range(4)}


@contextlib.contextmanager
def tickets(ts):
    old = exposure.tickets
    exposure.tickets = lambda day: ts if str(day) == DAY else []
    try:
        yield
    finally:
        exposure.tickets = old


def live(score, minute):
    return {"live": True, "score": score, "minute": minute, "period": 1 if minute <= 45 else 2}


def run(minutes, status=None):
    return watch.follow(KO + timedelta(minutes=minutes), games=GAMES, live_for=lambda ids: status or {}, stats_for=lambda g: {},
                        final_for=lambda g: None, detail_for=lambda g: None)


def test_prekick_grouped_once():
    store.save(watch.FILE, {})
    later = KO + timedelta(hours=2)
    ts = [ticket("vm1", [leg(0), leg(1, point=1.5), leg(2, ko=later)], coupon="AB12", msg=77), ticket("vm2", [leg(3, ko=later)], coupon="CD34")]
    with tickets(ts):
        assert watch.plan_next(KO - timedelta(minutes=60)) == (KO - timedelta(minutes=5)).timestamp()     # le runner passera à 18h55
        assert json.load(open(store.path("watch_next.json")))["at"] == (KO - timedelta(minutes=5)).timestamp()
        assert run(-20) is None, "20 min avant : rien"
        out = run(-5)
        pre = str(out)
        assert pre == ("⏰ <b>Dans 5 min</b> · 🎫 Combi vm1 · 🎟️ AB12\n2 matchs à 19h00 :\n   ⚽ H0 – A0 · Plus de 2,5 buts\n   ⚽ H1 – A1 · Plus de 1,5 buts\n"
                       "Puis 1 match (le dernier à 21h00) · si tout passe : <b>10 000 FCFA</b>"), pre
        assert out.reply_to == 77 and out.markup["inline_keyboard"][0][0]["text"] == "🎫 AB12", (out.reply_to, out.markup)   # réponse au message du coupon
        assert watch.plan_next(KO - timedelta(minutes=5)) == (later - timedelta(minutes=5)).timestamp(), "annoncé : le suivant est vm2"
        assert run(-3) is None, "une fois par coupon"
        msg = watch.all_text(run(6, {0: live((0, 0), 6), 1: live((0, 0), 6)}))
        assert "Coup d'envoi" not in msg, msg                                   # déjà annoncés 5 min avant : pas de 🟢 en plus
    print("ok  ⏰ dans 5 min : un message par coupon, matchs à la même heure groupés, une fois, sans 🟢 en plus ; heure voulue écrite pour le runner")


def test_runner_wakes_for_prekick():
    import termux_runner as R
    r = R.Runner.__new__(R.Runner)
    r.state = R.new_state() | {"watch_at": time.time() - 120}
    path = store.path("watch_next.json")
    for at, want in ((time.time() - 30, True), (time.time() + 60, False), (time.time() - 300, False), (None, False)):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"at": at}, f)
        assert r._watch_wanted() is want, (at, want)                            # passé et pas encore servi : dû ; à venir ou déjà servi : non
    os.remove(path)
    assert r._watch_wanted() is False
    print("ok  le runner déclenche un passage du suivi à l'heure voulue (5 min avant), une fois")


def test_won_festive_and_card():
    store.save(watch.FILE, {})
    store.save(money.FILE, {})
    store.save("bets.json", {"picks": [], "combos": []})
    money._cap_memo.clear()
    app.handle_command("capital 50000")
    app.handle_command("objectif 10000")
    t = ticket("vm1", [leg(0, point=0.5), leg(1, point=0.5)], coupon="AB12", stake=1000, odds=4.0, msg=55)
    with tickets([t]):
        run(20, {0: live((0, 0), 20), 1: live((0, 0), 20)})
        out = run(30, {0: live((1, 0), 30), 1: live((0, 0), 30)})
        last = out.more[0]
        assert str(last).startswith("🔥 <b>PLUS QU'UN MATCH !</b> · 🎫 Combi vm1 · 🎟️ AB12\n✅ 1/2 déjà gagnés\n⏳ H1 – A1 · Plus de 0,5 buts (0-0)"), str(last)
        assert last.reply_to == 55 and "💰 Si ça passe : <b>4 000 FCFA</b>" in str(last)
        msg = watch.all_text(run(40, {0: live((1, 0), 40), 1: live((0, 0), 40)}))
        old_now, watch._now = watch._now, lambda: KO + timedelta(minutes=40)
        try:
            card = str(chat.on_callback("tk|" + watch._hid(t["tid"])))             # bouton « 🎫 AB12 » : la fiche du coupon
        finally:
            watch._now = old_now
        assert card.startswith("🎫 <b>Combi vm1</b> · 🎟️ <code>AB12</code>\ncote 4,00 · mise 1 000 FCFA · si tout passe : 4 000 FCFA\n"), card
        assert "✅ H0 – A0 1-0\n    Plus de 0,5 buts" in card and "⏳ H1 – A1 0-0 (40e)\n    Plus de 0,5 buts" in card and "📊 1 gagné, 1 en cours" in card, card
        out = run(52, {0: live((1, 0), 52), 1: live((0, 1), 52)})
        main, won = str(out), out.more[0]
        assert "🔥 dernier match du coupon" in main, main                        # la ligne du dernier match le dit
        assert str(won).startswith("🎉🏆 <b>COUPON GAGNÉ !</b> 🏆🎉\n🎫 <b>Combi vm1</b> · 🎟️ AB12\n✅ H0 – A0 · Plus de 0,5 buts (1-0)\n"
                                   "✅ H1 – A1 · Plus de 0,5 buts (0-1)\n💰 1 000 FCFA × 4,00 = <b>4 000 FCFA</b> encaissés (+3 000 FCFA)"), str(won)
        assert "💼 Capital après le règlement de demain matin : 53 000 FCFA · objectif du mois ▰▰▰▱▱▱▱▱▱▱ 30 %" in str(won), str(won)
        assert "mets-en une partie de côté" in str(won) and won.reply_to == 55       # +3 000 = 6 % du capital : conseil de discipline
        assert "COUPON GAGNÉ" not in watch.all_text(run(60, {0: live((1, 0), 60), 1: live((0, 1), 60)})), "fêté une fois"
    assert chat.on_callback("tk|0000000000") == "Ce coupon n'est plus suivi (il date de plus de 2 jours)."
    print("ok  🔥 plus qu'un match puis 🎉 coupon gagné (messages à part, en réponse au coupon, capital et objectif, conseil), fiche du coupon au bouton 🎫")


def test_telegram_reply_to():
    calls = []
    old = (telegram.api, telegram.creds)
    telegram.api = lambda method, **kw: calls.append((method, kw)) or {"message_id": 9}
    telegram.creds = lambda: ("tok", "chat")
    try:
        m = telegram.Html("🎉 gagné")
        m.reply_to = 55
        nxt = telegram.Html("suite")
        m.more = [nxt]
        assert telegram.send(m) == 9
    finally:
        telegram.api, telegram.creds = old
    assert calls[0][1]["reply_to_message_id"] == 55 and calls[0][1]["allow_sending_without_reply"] is True and "reply_to_message_id" not in calls[1][1]
    print("ok  Telegram : réponse au message du coupon (envoyé même s'il a disparu), messages suivants sans citation")


def test_coupon_message_remembered():
    from kelly import fair
    row = {"date": "2026-10-13", "tier": "m1", "custom": True, "min_odds": 5.0, "prob": 0.3, "keys": ["k1"], "result": None, "coupon": "XY9",
           "legs": [{"key": "k1", "id": "e1", "home": "H", "away": "A", "market": "totals", "name": "Over", "point": 2.5,
                     "commence": "2026-10-13T19:00:00Z", "prob": 0.6, "result": None}]}
    store.save(fair.COMBOS_FILE, [row])
    chat.on_callback("joue|vm1|2026-10-13", 321)                                  # « ✅ Je l'ai joué » sous le message du coupon
    assert fair.load_combos()[0]["msg"] == 321
    chat.on_callback("joue|vm1|2026-10-13|l0", 999)                               # depuis la liste « Mes paris » : pas le message du coupon
    assert fair.load_combos()[0]["msg"] == 321
    row = fair.load_combos()[0] | {"played": {"odds": 5.0, "stake": 100, "at": "2026-10-13T12:00+00:00"}}
    store.save(fair.COMBOS_FILE, [row])
    assert exposure.tickets("2026-10-13")[0]["msg"] == 321
    print("ok  le message du coupon (« ✅ Je l'ai joué ») est retenu : les alertes y répondent ; jamais celui de la liste « Mes paris »")


def main():
    test_coupon_message_remembered()
    test_prekick_grouped_once()
    test_runner_wakes_for_prekick()
    test_won_festive_and_card()
    test_telegram_reply_to()
    print("tests alertes par coupon : OK")


if __name__ == "__main__":
    main()
