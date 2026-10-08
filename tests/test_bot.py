"""Test de bout en bout hors ligne : journée -> clôture -> apprentissage ->
règlement -> bilan -> calibration -> commandes. Lancer : python -m tests.test_bot"""
import copy
import math
import os
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-test-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, bankroll, learn, odds, settle, store, track  # noqa: E402


def fake_scores(rows):
    out = {}
    random.seed(7)
    for b in rows:
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


def closing_events(events, tracked):
    """Cotes de clôture fictives : les paris à forte valeur (edge) voient leur
    cote baisser (le marché nous donne raison), les autres bougent au hasard."""
    by_key = {r["key"]: r for r in tracked}
    mean_edge = sum(r["edge"] for r in tracked) / len(tracked)
    random.seed(3)

    def fetch(sport_key, ev_id, markets):
        ev = copy.deepcopy(next(e for e in events if e["id"] == ev_id))
        for bk in ev["bookmakers"]:
            for m in bk["markets"]:
                for o in m["outcomes"]:
                    key = f'{ev["id"]}|{m["key"]}|{o.get("description") or ""}|{o.get("point") if o.get("point") is not None else ""}|{o["name"]}'
                    r = by_key.get(key)
                    drift = -0.12 * ((r["edge"] - mean_edge) * 10 + r.get("steam", 0)) if r else 0
                    o["price"] = round(max(1.05, o["price"] * (1 + drift + random.uniform(-0.03, 0.03))), 2)
        return ev
    return fetch


def main():
    bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
    events = app.load_sample()
    day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()

    # --- Journée 1 : envoi + suivi (envoyés + fantômes)
    msgs = app.run_daily(sample=True)
    assert any("KELLY" in m for m in msgs) and any("Mise conseillée" in m for m in msgs)
    rows = track.load()
    assert len(rows) >= 30 and sum(r["sent"] for r in rows) >= 5, (len(rows))
    assert all(r["closed_at"] is None and "heuristic" in r["features"] for r in rows)

    # --- Clôture horaire : on simule les passages de 13:00 à 22:00
    fetch = closing_events(events, rows)
    total = 0
    for h in range(13, 23):
        n, _ = app.run_track(events_for=fetch, now=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
        total += n
    rows = track.load()
    closed = track.with_clv(rows)
    assert total == len(closed) >= 25, (total, len(closed))
    assert sum(1 for r in rows if r["closed_at"] == "missed") == 0

    # --- Apprentissage : on fabrique plusieurs jours en dupliquant avec bruit
    #     (pas assez de lignes avec une seule journée) puis on entraîne.
    synth = []
    random.seed(11)
    for d in range(1, 9):
        for r in closed:
            s = copy.deepcopy(r)
            s["key"] += f"|d{d}"; s["date"] = str(day - timedelta(days=d)); s["sent"] = random.random() < 0.3
            noise = random.gauss(0, 0.02)
            s["clv"] = round(0.3 * r["features"]["edge"] + 0.5 * r["features"]["steam"] + noise, 4)
            synth.append(s)
    all_rows = rows + synth
    model = learn.train_clv(all_rows)
    assert model["n"] == len(closed) + len(synth) and "oos_model" in model, model
    print(f"modèle : n={model['n']} adopté={model['adopted']} oos modèle={model['oos_model']:+.4f} "
          f"heuristique={model['oos_heuristic']:+.4f}")
    assert model["adopted"], model.get("reason")
    top = sorted(zip(model["names"], model["w"]), key=lambda kv: -abs(kv[1]))[:3]
    print("signaux dominants :", [(n, round(w, 3)) for n, w in top])
    assert any(n == "edge" for n, _ in top), "le modèle devrait retrouver que la valeur compte"
    # Le modèle change bien l'ordre des sélections.
    p0 = odds.build_picks(events, day)
    p1 = odds.build_picks(events, day, model=model)
    assert [c["key"] for c in sorted(p0, key=lambda c: -c["score"])[:10]] != \
           [c["key"] for c in sorted(p1, key=lambda c: -c["score"])[:10]]
    assert all("heuristic" in c for c in p1)

    # --- Règlement le lendemain + bankroll + bilan
    later = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1, hours=8)
    bets, settled = settle.settle_pending(later, scores_by_sport=fake_scores(rows), stats_for=fake_stats)
    assert settled and all(b["result"] in ("win", "loss", "push") for b in settled)
    assert all(c["result"] in ("win", "loss", "void") for c in bets["combos"])
    bankroll.apply_results(bets["combos"])
    text = settle.bilan(bets)
    assert "Envoyés" in text and "Suivis en silence" in text and "Brier" in text and "CLV" in text, text
    print("\n" + text + "\n")

    # --- Calibration : 400 résultats synthétiques « corners/cartons » dont les
    #     probas sont systématiquement trop optimistes -> Platt doit corriger.
    random.seed(5)
    cal_rows = []
    for i in range(400):
        p = random.uniform(0.35, 0.75)
        true_p = max(0.05, p - 0.12)
        cal_rows.append({"market": "alternate_totals_corners", "prob": p,
                         "result": "win" if random.random() < true_p else "loss"})
    cal = learn.calibrate(cal_rows)["corners_cartons"]
    assert cal["active"] and cal["brier_after"] < cal["brier_before"], cal
    m2 = {"calibration": {"corners_cartons": cal}}
    assert learn.calibrated(m2, "alternate_totals_corners", 0.6) < 0.6
    assert learn.calibrated(m2, "h2h", 0.6) == 0.6
    print(f"calibration : Brier {cal['brier_before']:.4f} -> {cal['brier_after']:.4f}, 60% devient "
          f"{learn.calibrated(m2, 'alternate_totals_corners', 0.6)*100:.0f}%")

    # --- Message du matin et commandes
    model = learn.update(track.load())
    d = learn.describe(model, track.load())
    assert d.startswith("🧠 KELLY APPREND"), d
    print(d, "\n")
    assert "BILAN" in app.handle_command("/bilan")
    assert "PSG" in app.handle_command("/match psg")
    assert "COMBINÉ SUR MESURE" in app.handle_command("/combi 30")
    assert "TOP 10" in app.handle_command("/top")
    assert "réglée" in app.handle_command("/bank 50000")
    assert "Kelly" in app.handle_command("/aide")
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("OK : tout le flux passe hors ligne.")


if __name__ == "__main__":
    main()
