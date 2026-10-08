"""Test de bout en bout hors ligne : journée -> règlement -> bilan ->
apprentissage -> alertes -> commandes. Lancer : python -m tests.test_bot"""
import os
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="botbet-test-")
os.environ.pop("TELEGRAM_TOKEN", None)

from botbet import app, bankroll, learn, odds, settle, store  # noqa: E402


def fake_scores(bets):
    """Scores fictifs pour chaque match parié."""
    out = {}
    random.seed(7)
    for b in bets["picks"]:
        out.setdefault(b["sport_key"], {})
        if b["id"] not in out[b["sport_key"]]:
            out[b["sport_key"]][b["id"]] = {"id": b["id"], "completed": True, "scores": [
                {"name": b["home"], "score": str(random.randint(0, 3))},
                {"name": b["away"], "score": str(random.randint(0, 2))}]}
    return {k: list(v.values()) for k, v in out.items()}


def fake_stats(b):
    random.seed(hash(b["id"]) % 1000)
    return {"corners": random.randint(6, 13), "cards": random.randint(1, 6),
            "players": {"Kylian Mbappé": {"shots": 3, "shots_on_target": 2, "fouls": 1},
                        "Casemiro": {"shots": 1, "shots_on_target": 0, "fouls": 3}}}


def main():
    bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
    msgs = app.run_daily(sample=True)
    assert any("SÛR" in m for m in msgs) and any("Mise conseillée" in m for m in msgs)
    bets = store.load("bets.json", None)
    assert bets and len(bets["picks"]) >= 5 and len(bets["combos"]) >= 1, "paris non enregistrés"
    assert all(b["result"] is None for b in bets["picks"])

    # Le lendemain : règlement avec résultats fictifs.
    later = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)
    bets, settled = settle.settle_pending(later, scores_by_sport=fake_scores(bets), stats_for=fake_stats)
    assert settled and all(b["result"] in ("win", "loss", "push") for b in settled), settled
    assert all(c["result"] in ("win", "loss", "void") for c in bets["combos"]), bets["combos"]
    b = bankroll.apply_results(bets["combos"])
    assert b["bankroll"] != 1000.0 or all(c["result"] == "void" for c in bets["combos"])
    text = settle.bilan(bets)
    assert "BILAN" in text and "Par marché" in text, text
    print(text, "\n")
    print(bankroll.summary(bets["combos"]), "\n")

    # Apprentissage : les facteurs existent, restent bornés, et modifient le poids.
    model = learn.update(bets)
    assert model["n"] == len(settled)
    for v in model["market"].values():
        assert 0.5 <= v["factor"] <= 1.6
    w0 = odds.market_weight("alternate_totals_cards")
    w1 = odds.market_weight("alternate_totals_cards", model)
    print("poids cartons :", w0, "->", w1)

    # Alertes : on simule une chute de cote depuis le relevé de la veille.
    events = app.load_sample()
    day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
    picks = odds.build_picks(events, day)
    hist = odds.snapshot(picks)
    for k in list(hist)[:3]:
        hist[k]["o"] = round(hist[k]["o"] * 1.25, 2)
        hist[k]["p"] = round(hist[k]["p"] * 0.85, 4)
    store.save("odds_history.json", hist)
    alerts = app.run_alert(sample=True)
    assert alerts and "ALERTE" in alerts[0] and "➜" in alerts[0], alerts
    print(alerts[0], "\n")

    # Commandes.
    assert "BILAN" in app.handle_command("/bilan")
    assert "PSG" in app.handle_command("/match psg")
    assert "COMBINÉ SUR MESURE" in app.handle_command("/combi 30"), app.handle_command("/combi 30")
    assert "TOP 10" in app.handle_command("/top")
    assert "réglée" in app.handle_command("/bank 50000")
    assert "Commandes" in app.handle_command("/aide")
    print(app.handle_command("/combi 30"))
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("\nOK : tout le flux passe hors ligne.")


if __name__ == "__main__":
    main()
