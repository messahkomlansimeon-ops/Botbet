"""Budget du jour : répartition selon la confiance, jours pauvres, mises dans les messages, « ❌ Pas joué », bilan en argent.
Lancer : python -m tests.test_budget"""
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-budget-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-budget-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-budget-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, chat, config as C, money, played, settle, store, track  # noqa: E402

DAY = date(2026, 10, 9)


def leg(i, odds_, prob, edge):
    return {"key": f"k{i}", "id": f"e{i}", "odds": odds_, "prob": prob, "edge": edge, "n_books": 14, "weight": 1.0, "score": edge,
            "market": "h2h", "name": f"H{i}", "why": "", "pm": None, "commence": f"2026-10-09T{12 + i % 10:02d}:00:00Z",
            "home": f"H{i}", "away": f"A{i}", "event": f"H{i} - A{i}", "league": "L", "start": f"{12 + i % 10}:00",
            "pick": f"H{i} gagne", "group": "soccer", "sport_key": "soccer_epl"}


def test_allocation():
    money.set_daily(5000)
    assert money.round_step(5000) == 50 and money.round_step(50000) == 500 and money.round_step(10) == 0.5
    assert money.money(1500) == "1 500 FCFA" and money.signed(-250) == "−250 FCFA"
    rich = [{"kind": "pari", "tier": "pari", "prob": 0.6, "odds": 1.9}] + \
           [{"kind": "combo", "tier": t, "prob": 0.3, "odds": 4.0} for t, _, _ in C.COMBOS] + \
           [{"kind": "fair", "tier": 5.5, "prob": 0.25, "odds": 5.5}, {"kind": "fair", "tier": 13, "prob": 0.1, "odds": 13}]
    stakes, spent, kept = money.allocate(rich)
    assert spent <= 5000 and kept < money.round_step(5000) * len(rich) and all(s % 50 == 0 for s in stakes), (stakes, spent)
    assert stakes[1] > stakes[2] > stakes[4] > stakes[5], stakes             # Sûr > Costaud > Méchant > Monstre
    poor = [{"kind": "combo", "tier": C.COMBOS[0][0], "prob": 0.2, "odds": 5.2}]           # espérance +4 % : confiance réduite
    stakes, spent, kept = money.allocate(poor)
    assert 0 < spent < 5000 * money.SHARE["🛡️"] and kept == 5000 - spent, (stakes, spent, kept)
    assert money.allocate([{"kind": "combo", "tier": "💀", "prob": 0.003, "odds": 200}], 1000)[0] == [0]   # plus petit que l'arrondi
    print("ok  répartition du budget")


def test_command_and_messages():
    store.save(money.FILE, {})
    r = app.handle_command("budget")
    assert "Pas encore de budget" in r and r.markup["inline_keyboard"][0][1]["callback_data"] == "budget|5000"
    assert "invalide" in app.handle_command("budget abc")
    assert "✅ Budget du jour : 5 000 FCFA" in app.handle_command("budget 5000") and money.daily() == 5000
    assert "5 000 FCFA" in chat.on_callback("budget|5000")
    store.save("bets.json", {"picks": [], "combos": []})
    legs = [leg(i, 1.9, 0.6, 0.08) for i in range(8)]
    fair_stakes = {}
    msgs, sent_picks, combos = app.build_day(legs, DAY, fair_stakes=fair_stakes)
    head = msgs[0]
    assert "💰 Budget du jour : <b>5 000 FCFA</b>" in head, head
    sur = next(m for m in msgs if "SÛR" in m)
    assert "💰 mise : <b>" in sur and "FCFA" in sur, sur
    assert [b["callback_data"] for b in sur.markup["inline_keyboard"][0]] == ["skip|sur|2026-10-09", "joue|sur|2026-10-09"]
    assert all(c.get("cur") == "FCFA" and c["stake"] > 0 for c in combos) and sent_picks[0]["cur"] == "FCFA"
    assert sum(c["stake"] for c in combos) + sent_picks[0]["stake"] <= 5000
    print("ok  commande budget et messages du matin")


def test_skip_and_money_bilan():
    money.set_daily(5000)
    bets = {"picks": [], "combos": [
        {"tier": C.COMBOS[0][0], "keys": ["a"], "odds": 5.0, "prob": 0.2, "stake": 1000, "cur": "FCFA", "date": "2026-10-09",
         "result": "win", "odds_final": 5.0},
        {"tier": C.COMBOS[1][0], "keys": ["b"], "odds": 13.0, "prob": 0.08, "stake": 500, "cur": "FCFA", "date": "2026-10-09",
         "result": "loss"},
        {"tier": C.COMBOS[3][0], "keys": ["c"], "odds": 50.0, "prob": 0.02, "stake": 200, "cur": "FCFA", "date": "2026-10-09",
         "result": None}]}
    store.save("bets.json", bets)
    track.save([{"key": "s", "id": "s", "date": "2026-10-09", "sent": True, "single": True, "odds": 1.9, "prob": 0.6, "stake": 800,
                 "cur": "FCFA", "result": "loss", "pick": "X gagne", "market": "h2h"}])
    store.save("fair_combos.json", [{"date": "2026-10-09", "tier": 5, "min_odds": 5.5, "prob": 0.2, "keys": ["f1", "f2"],
                                     "stake": 400, "cur": "FCFA", "result": "win",
                                     "legs": [{"key": "f1", "min_odds": 2.2, "result": "win"}, {"key": "f2", "min_odds": 2.5, "result": "win"}]}])
    n, total, staked = money.result(store.load("bets.json", {}), track.load(), store.load("fair_combos.json", []))
    assert n == 4 and total == 1000 * 4 - 500 - 800 + 400 * 4.5 and staked == 1000 + 500 + 800 + 400, (n, total, staked)
    # ❌ Pas joué sur le Costaud perdu : refusé, il est déjà réglé (la perte reste au bilan) ; accepté sur le Méchant en attente, ↩️ le remet
    assert "déjà réglé" in chat.on_callback("skip|costaud|2026-10-09")
    assert money.result(store.load("bets.json", {}), track.load(), store.load("fair_combos.json", []))[1] == total
    assert "retiré de ton bilan" in chat.on_callback("skip|mechant|2026-10-09")
    assert "compté comme joué" in chat.on_callback("unskip|mechant|2026-10-09")
    # ✏️ autre cote : le gain suit la cote de 1xBet
    assert "déjà réglé" in played.command(["sur", "5,5"], "2026-10-09")               # trop tard : le Sûr est réglé
    assert "Noté" in played.command(["mechant", "60"], "2026-10-09")                 # le Méchant n'est pas encore réglé
    b = store.load("bets.json", {})
    b["combos"][2].update(result="win", odds_final=50.0)
    assert money.gain(b["combos"][2]) == 200 * (60 - 1)
    lines = money.summary_lines(store.load("bets.json", {}), track.load(), store.load("fair_combos.json", []), today="2026-10-09")
    assert lines and lines[0].startswith("7 derniers jours : +") and "FCFA" in lines[0], lines
    simple = settle.bilan(store.load("bets.json", {}), rows=track.load())
    assert "💰 <b>Tes mises</b>" in simple and "Joués chez 1xBet" not in simple and "FCFA" in simple, simple
    todo = app.handle_command("🧾 Mes paris")
    assert "Méchant" in todo and any(b["callback_data"].endswith("|l") for row in todo.markup["inline_keyboard"] for b in row), todo
    print("ok  pas joué, autre cote et bilan en argent")


def main():
    test_allocation()
    test_command_and_messages()
    test_skip_and_money_bilan()
    print("tests budget : OK")


if __name__ == "__main__":
    main()
