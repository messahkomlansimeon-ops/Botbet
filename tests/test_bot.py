"""Test de bout en bout hors ligne : journée -> clôture -> apprentissage ->
règlement -> bilan -> calibration -> commandes. Lancer : python -m tests.test_bot"""
import contextlib
import copy
import io
import math
import os
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-test-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, bankroll, config as C, learn, odds, settle, store, telegram, track  # noqa: E402


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


def fake_odds_api(state, calls):
    """Faux odds.get : décompte les crédits comme l'API (1 par marché) et remplit odds.CREDITS."""
    sports = [{"key": k, "has_outrights": False} for k in ("soccer_epl", "soccer_uefa_champs_league_women", "soccer_spain_la_liga")]
    sports.append({"key": "soccer_epl_winner", "has_outrights": True})

    def get(path, **params):
        calls.append((path, params.get("markets")))
        if path != "/sports":
            state["remaining"] -= len(params["markets"].split(","))
        odds.CREDITS.update(remaining=state["remaining"], used=500 - state["remaining"])
        if path == "/sports":
            return sports
        if path.endswith("/odds") and "/events/" not in path:
            sk = path.split("/")[2]
            return [{"id": f"{sk}-{i}", "commence_time": "2026-10-08T18:00:00Z", "home_team": "A", "away_team": "B",
                     "bookmakers": [{"key": f"b{j}", "markets": []} for j in range(3)]} for i in range(2)]
        if params["markets"] == "double_chance":          # marché non supporté : 422
            raise odds.OddsError("HTTP 422 INVALID_MARKET", 422, "INVALID_MARKET")
        return {"id": "x", "bookmakers": [{"key": "b0", "markets": [{"key": params["markets"], "outcomes": []}]}]}
    return get


def test_credit_guard():
    real_get, saved = odds.get, (dict(odds.CREDITS), set(odds._warned), C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS)
    C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS = 30, 6, 3
    sports = [{"key": k} for k in ("soccer_uefa_champs_league_women", "soccer_uefa_champs_league", "basketball_nba",
                                   "soccer_epl", "x_sport")]
    try:
        # Sélection des sports : ordre de C.SPORTS, clés exactes (pas la version féminine), préfixe avec « _ ».
        saved_sports = C.SPORTS
        C.SPORTS = ["soccer_epl", "soccer_uefa_champs_league", "basketball_nba"]
        assert [x["key"] for x in odds.select_sports(sports)] == ["soccer_epl", "soccer_uefa_champs_league", "basketball_nba"]
        C.SPORTS = ["soccer_"]
        assert len(odds.select_sports(sports)) == 3
        C.SPORTS = saved_sports

        # Crédits suffisants : marchés détaillés demandés, 422 ignoré sans erreur.
        state, calls = {"remaining": 400}, []
        odds.get = fake_odds_api(state, calls)
        events = odds.fetch_events(datetime(2026, 10, 8).date())
        props = [c for c in calls if "/events/" in c[0]]
        assert len(events) == 4 and len(props) == 3 * len(C.PROP_MARKETS["soccer"]), (len(events), len(props))
        assert not any("women" in c[0] or "winner" in c[0] for c in calls), calls

        # Crédits bas : plus aucun marché détaillé, ni balayage large (optional), mais pas d'exception.
        odds.CREDITS.update(remaining=None)
        state, calls = {"remaining": 33}, []
        odds.get = fake_odds_api(state, calls)
        events = odds.fetch_events(datetime(2026, 10, 8).date())          # 2 ligues x 2 crédits : 33 -> 29 < 30
        assert events and not [c for c in calls if "/events/" in c[0]], calls
        assert odds.low_credits() and not odds.credits_ok("test")
        calls.clear()
        assert odds.fetch_events(datetime(2026, 10, 8).date(), optional=True) == []
        assert [c[0] for c in calls] == ["/sports"], calls                  # seul /sports (gratuit)

        # Clôture : crédits bas -> seuls les paris envoyés sont relevés (marchés envoyés uniquement).
        saved_rows = track.load()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="minutes")
        base = {"closed_at": None, "commence": soon, "odds": 2.0, "sport_key": "soccer_epl", "group": "soccer"}
        track.save([dict(base, key="g|h2h", id="ghost", market="h2h", sent=False),
                    dict(base, key="s|h2h", id="sent", market="h2h", sent=True),
                    dict(base, key="s|totals", id="sent", market="totals", sent=False),
                    dict(base, key="m|btts", id="mixed", market="btts", sent=False)])
        for low, expect in ((False, {"ghost", "sent", "mixed"}), (True, {"sent"})):
            odds.CREDITS.update(remaining=10 if low else 400)
            asked = []

            def get(path, **params):
                asked.append((path.split("/")[4], params["markets"]))
                return {"id": path.split("/")[4], "commence_time": soon, "home_team": "A",
                        "away_team": "B", "bookmakers": []}
            odds.get = get
            app.run_track()
            assert {a[0] for a in asked} == expect, (low, asked)
            if low:
                assert asked == [("sent", "h2h")], asked                      # marché fantôme (totals) non demandé
            elif asked[0][0] != "sent":
                raise AssertionError(f"les envoyés doivent passer d'abord : {asked}")
            track.save([dict(r, closed_at=None) for r in track.load()])
        track.save(saved_rows)
    finally:
        odds.get = real_get
        odds.CREDITS.update(saved[0])
        odds._warned.clear(); odds._warned.update(saved[1])
        C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS = saved[2:]
        C.SPORTS = saved_sports
    print("garde-fou crédits : OK")


def test_build_picks_shapes():
    """handicaps (points opposés) et double chance (somme des probas = 2) bien lus."""
    def bk(i, spread_h, spread_a, dc):
        return {"key": f"b{i}", "title": f"B{i}", "markets": [
            {"key": "spreads", "outcomes": [{"name": "A", "price": spread_h, "point": -1.5},
                                            {"name": "B", "price": spread_a, "point": 1.5}]},
            {"key": "double_chance", "outcomes": [{"name": "A or Draw", "price": dc[0]}, {"name": "A or B", "price": dc[1]},
                                                  {"name": "Draw or B", "price": dc[2]}]}]}
    ev = {"id": "e1", "sport_key": "basketball_nba", "sport_title": "NBA", "commence_time": "2026-10-08T18:00:00Z",
          "home_team": "A", "away_team": "B", "bookmakers": [bk(i, 1.9, 1.95, (1.5, 1.3, 2.0)) for i in range(3)]}
    picks = {(c["market"], c["name"]): c for c in odds.build_picks([ev], datetime(2026, 10, 8).date())}
    assert ("spreads", "A") in picks and picks[("spreads", "A")]["point"] == -1.5 and picks[("spreads", "B")]["point"] == 1.5
    dc = [c["prob_book"] for k, c in picks.items() if k[0] == "double_chance"]
    assert len(dc) == 3 and abs(sum(dc) - 2) < 1e-9, dc


def test_check():
    """--check : rapport français, aucun secret affiché, indices sur les erreurs courantes."""
    real = (odds.get, telegram.api, app.requests.get, dict(odds.CREDITS))
    env = {"ODDS_API_KEY": "odds-secret-xyz", "TELEGRAM_TOKEN": "123:tok-secret", "TELEGRAM_CHAT_ID": "42",
           "STATS_API_KEY": "rapidapi-secret-key"}
    old_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    sent = []

    class Resp:
        status_code = 200
        def json(self):
            return {"errors": {"token": "Error/Missing application key"}, "response": []}

    def run():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = app.run_check()
        return ok, buf.getvalue()
    try:
        def odds_get(path, **p):
            odds.CREDITS.update(remaining=412, used=88)
            return [{"key": "soccer_epl", "has_outrights": False}]
        odds.get = odds_get
        app.requests.get = lambda *a, **k: Resp()

        def tg(method, **p):
            if method == "getMe":
                return {"username": "kelly_bot"}
            sent.append(p["text"])
            raise telegram.TelegramError("Bad Request: chat not found", 400)
        telegram.api = tg
        ok, out = run()
        assert not ok and "clé valide" in out and "412 restants" in out and "kelly_bot" in out, out
        assert "RapidAPI" in out and "/start" in out and sent[0].startswith("✅ Kelly est en ligne"), out
        assert not any(v in out + sent[0] for v in env.values()), "secret affiché"

        # Odds 401 : indice clé ; Telegram OK ; stats absente (ignorée).
        def bad_get(path, **p):
            raise odds.OddsError("HTTP 401 INVALID_KEY", 401, "INVALID_KEY")
        odds.get = bad_get
        del os.environ["STATS_API_KEY"]
        telegram.api = lambda method, **p: {"username": "kelly_bot"} if method == "getMe" else {}
        ok, out = run()
        assert not ok and "clé refusée" in out and "STATS_API_KEY absente" in out and "message de test envoyé" in out, out
    finally:
        odds.get, telegram.api, app.requests.get = real[:3]
        odds.CREDITS.update(real[3])
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("diagnostic --check : OK")


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
    test_build_picks_shapes()
    test_credit_guard()
    test_check()
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("OK : tout le flux passe hors ligne.")


if __name__ == "__main__":
    main()
