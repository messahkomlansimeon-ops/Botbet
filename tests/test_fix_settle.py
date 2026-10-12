"""Tests de non-régression du règlement : jambe invérifiable (« unknown »), pari du jour marqué explicitement, calibration sur la
proba brute des bookmakers. Hors ligne. Lancer : python -m tests.test_fix_settle"""
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-fix-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-fix-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-fix-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("STATS_API_KEY", None)

from kelly import app, bankroll, calib, config as C, fair, learn, played, settle, store, track  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
OLD = "2026-09-20T19:00:00Z"                    # bien plus vieux que WAIT_DAYS_STATS : l'attente est terminée


def wipe():
    for f in os.listdir(store.DATA_DIR):
        os.remove(os.path.join(store.DATA_DIR, f))
    calib._memo.clear()


def no_network():
    """Aucune source gratuite : ni CSV football-data, ni page FotMob. Rend de quoi tout remettre."""
    saved = (settle.FootballData.match, settle.fmmatch.match_stats)
    settle.FootballData.match = lambda self, b: None
    settle.fmmatch.match_stats = lambda b: None
    return saved


def restore(saved):
    settle.FootballData.match, settle.fmmatch.match_stats = saved


def row(key, market, name, point, odds, home="Alpha", away="Bravo", ev="m1", date_="2026-09-20", **kw):
    return {"key": key, "id": ev, "sport_key": "soccer_epl", "group": "soccer", "event": f"{home} - {away}", "home": home,
            "away": away, "league": "L", "commence": OLD, "market": market, "who": None, "point": point, "name": name,
            "pick": f"{name} {point or ''}".strip(), "odds": odds, "prob": 0.5, "sent": True, "date": date_, "result": None} | kw


def scores(ev="m1", h=2, a=0, home="Alpha", away="Bravo"):
    return {"id": ev, "completed": True, "scores": [{"name": home, "score": str(h)}, {"name": away, "score": str(a)}]}


# ---------- 1. jambe invérifiable ----------

def test_unknown_leg_pending():
    """h2h gagné 2-0, corners sans stats, Under 2,5 gagné : le combiné n'est PAS gagné (avant : « void » traité comme annulé)."""
    wipe()
    saved = no_network()
    try:
        rows = [row("a1", "h2h", "Alpha", None, 1.5), row("a2", "totals", "Under", 2.5, 2.0),
                row("a3", "alternate_totals_corners", "Over", 9.5, 1.9),
                row("b1", "h2h", "Bravo", None, 3.0),                                   # perdu
                row("v1", "h2h", "Gamma", None, 2.0, home="Gamma", away="Delta", ev="m2")]   # match jamais joué : aucun score
        track.save(rows)
        combo = lambda tier, keys, odds_: {"tier": tier, "keys": keys, "odds": odds_, "prob": 0.2, "stake": 10.0,   # noqa: E731
                                           "date": "2026-09-20", "result": None}
        store.save("bets.json", {"picks": [], "combos": [
            combo("T-unknown", ["a1", "a2", "a3"], 5.7), combo("T-loss", ["a3", "b1"], 5.7), combo("T-void", ["a1", "v1"], 3.0),
            combo("T-novoid", ["v1"], 2.0)]})
        bets, settled = settle.settle_pending(NOW, scores_by_sport={"soccer_epl": [scores()]}, stats_for=lambda b: {})
        res = {r["key"]: r["result"] for r in track.load()}
        assert res == {"a1": "win", "a2": "win", "a3": "unknown", "b1": "loss", "v1": "unknown"}, res   # score absent après WAIT_DAYS : non vérifiable (lot I), jamais « annulé »
        got = {c["tier"]: c for c in bets["combos"]}
        assert got["T-unknown"]["result"] == "unknown" and "odds_final" not in got["T-unknown"], got["T-unknown"]
        assert got["T-loss"]["result"] == "loss"                                       # une jambe perdue l'emporte
        # lot I : une jambe sans score trouvé n'est plus « annulée » (le combiné ne devient pas « gagné » à une cote recalculée sans elle) : il est non vérifiable
        assert got["T-void"]["result"] == "unknown" and "odds_final" not in got["T-void"], got["T-void"]
        assert got["T-novoid"]["result"] == "unknown"
    finally:
        restore(saved)


def test_unknown_not_counted():
    """Un combiné « unknown » ne bouge pas la bankroll (ni en mode normal, ni en mode réel), n'entre pas dans les comptes et
    s'affiche « non vérifiable »."""
    wipe()
    rows = [row("a1", "h2h", "Alpha", None, 1.5, result="win"), row("a3", "alternate_totals_corners", "Over", 9.5, 1.9, result="unknown")]
    unk = {"tier": C.COMBOS[0][0], "keys": ["a1", "a3"], "odds": 2.85, "prob": 0.3, "stake": 10.0, "date": "2026-09-20",
           "result": "unknown"}
    bankroll.save({"bankroll": 100.0, "initial": 100.0})
    bankroll.apply_results([unk])
    assert bankroll.load()["bankroll"] == 100.0 and not unk.get("counted"), (bankroll.load(), unk)
    played_unk = unk | {"played": {"odds": 2.7, "stake": 10.0, "at": "x"}}
    bankroll.save({"bankroll": 100.0, "initial": 100.0, "real": True})
    bankroll.apply_results([played_unk])
    bankroll.apply_results([row("s", "h2h", "A", None, 2.0, result="unknown", played={"odds": 2.0, "stake": 5.0, "at": "x"})],
                           played_only=True)
    assert bankroll.load()["bankroll"] == 100.0, bankroll.load()
    assert bankroll.played_gain(played_unk) == 0.0 and bankroll.streak([unk]) == 0
    by_key = {r["key"]: r for r in rows}
    txt = app.combo_result(unk, by_key)
    assert "❔" in txt and "non vérifiable" in txt and "vérifie chez 1xBet" in txt and "Kelly n'a pas trouvé les stats de" in txt, txt
    assert "Alpha – Bravo" in txt and "gagné" not in txt.replace("non vérifiable", ""), txt
    bets = {"picks": [], "combos": [unk]}
    msg = app.results_message(bets, rows, [])
    assert msg and "non vérifiable" in msg and "Hier :" not in msg, msg                # exclu des comptes gagnés/total
    assert app.leg_result_line(rows[1]).startswith("❔")
    assert "Aucun pari réglé" in settle.bilan(bets, rows=[]), "unknown exclu du bilan"
    pl = played.summary_lines({"combos": [played_unk]}, [], [])
    assert pl and "non vérifiable" in pl[0] and "gagné" not in pl[0], pl


def test_fair_unknown_and_void():
    """Combinés/pronostics à cote juste : jambe invérifiable -> « unknown » ; jambes toutes remboursées/annulées -> « void » (pas
    « win » payé plein tarif) ; pronostic invérifiable exclu de la calibration."""
    wipe()
    saved = no_network()
    games = {"m1": scores("m1", 2, 0), "m3": scores("m3", 1, 1, "Echo", "Foxtrot")}      # m2 : jamais joué
    real_score = settle.FootballData.score
    settle.FootballData.score = lambda self, b: games.get(b["id"])
    try:
        def leg(key, market, name, point, ev="m1", home="Alpha", away="Bravo", mo=1.8):
            return {"key": key, "id": ev, "sport_key": "soccer_epl", "home": home, "away": away, "commence": OLD, "market": market,
                    "name": name, "point": point, "prob": 0.6, "p0": 0.6, "pick": f"{name} {point}", "min_odds": mo, "result": None}
        win = leg("w", "h2h", "Alpha", None)
        push = leg("p", "totals", "Over", 2.0)                                   # 2 buts pile : remboursé
        void = leg("v", "h2h", "Gamma", None, "m2", "Gamma", "Delta")             # pas de score : annulé
        unk = leg("u", "alternate_totals_corners", "Over", 9.5)                   # score connu, pas de corners
        mk = lambda keys, legs: {"date": "2026-09-20", "tier": 5, "min_odds": 5.0, "prob": 0.2, "keys": keys, "legs": legs,  # noqa: E731
                                 "result": None, "played": {"odds": 5.0, "stake": 1.0, "at": "x"}}
        store.save(fair.COMBOS_FILE, [mk(["w", "u"], [dict(win), dict(unk)]), mk(["p", "p2"], [dict(push), dict(push, key="p2")]),
                                      mk(["w", "p"], [dict(win), dict(push)]), mk(["w", "v"], [dict(win), dict(void)])])
        rows, done = settle.settle_fair_combos(NOW, stats_for=lambda b: {})
        # jambe sans score trouvé = « unknown » (jamais « void ») : le combiné n'est jamais gagné tant qu'une jambe n'est pas résolue
        assert [c["result"] for c in rows] == ["unknown", "void", "win", "unknown"], [c["result"] for c in rows]
        assert rows[3]["legs"][1]["result"] == "unknown"
        bankroll.save({"bankroll": 100.0, "initial": 100.0, "real": True})
        assert bankroll.played_gain(rows[0]) == 0.0 and bankroll.played_gain(rows[1]) == 0.0
        assert bankroll.played_gain(rows[2] | {"legs": [dict(win, result="win")]}) > 0
        bankroll.apply_results(rows, played_only=True)
        assert round(bankroll.load()["bankroll"], 2) == round(100.0 + bankroll.played_gain(rows[2]), 2), bankroll.load()
        # ancienne ligne « win » sans aucune jambe gagnée : rien à payer
        legacy = mk(["p", "v"], [dict(push, result="push"), dict(void, result="void")]) | {"result": "win"}
        assert bankroll.played_gain(legacy) == 0.0
        # pronostics sans cote
        store.save(fair.FILE, [dict(unk), dict(win)])
        frows, fdone = settle.settle_fair(NOW, stats_for=lambda b: {})
        assert [r["result"] for r in frows] == ["unknown", "win"], frows
        assert calib.load().get("corners") is None and calib.load()["h2h"][0] == 1, calib.load()   # le non vérifiable n'est pas compté
    finally:
        settle.FootballData.score = real_score
        restore(saved)


# ---------- 2. pari du jour ----------

def leg_pick(i, odds_, prob, score, ev=None):
    return {"key": f"k{i}", "id": ev or f"e{i}", "sport_key": "soccer_epl", "group": "soccer", "odds": odds_, "best_odds": odds_,
            "prob": prob, "prob_book": prob - 0.02, "edge": prob * odds_ - 1, "n_books": 14, "weight": 1.0, "score": score,
            "market": "h2h", "name": "Over", "who": None, "point": None, "why": "", "pm": None, "book": "bk",
            "commence": f"2026-10-08T{12 + i % 8:02d}:00:00Z", "home": f"H{i}", "away": f"A{i}", "event": f"H{i} - A{i}",
            "league": "L", "start": f"{12 + i % 8}:00", "pick": f"Pari {i}"}


def test_single_flag():
    """Le pari du jour (`top`) est aussi jambe d'un combiné : /joue pari doit tomber dessus, pas sur un vieux pari non réglé."""
    wipe()
    day = date(2026, 10, 8)
    bankroll.save({"bankroll": 100.0, "initial": 100.0})
    picks = [leg_pick(0, 1.45, 0.72, 5.0)] + [leg_pick(i, 1.45, 0.72, 0.1) for i in range(1, 5)] + \
            [leg_pick(10 + i, 2.0, 0.55, 0.1) for i in range(12)]
    messages, sent_picks, sent_combos = app.build_day(picks, day)
    top = app.single_key(sent_picks)
    assert top == "k0" and any(top in c["keys"] for c in sent_combos), (top, sent_combos)   # le cas du bug : top dans un combiné
    # hier : un pari envoyé seul, pas réglé
    yesterday = leg_pick(99, 2.0, 0.55, 0.0) | {"key": "old"}
    track.save([{"key": "old", "date": "2026-10-07", "sent": True, "odds": 2.0, "prob": 0.55, "pick": "Vieux pari", "result": None,
                 "market": "h2h", "commence": "2026-10-07T18:00:00Z"}])
    store.save("bets.json", {"picks": [], "combos": []})
    track.record(picks, {c["key"] for c in sent_picks}, day, NOW, single_key=top)
    rows = track.load()
    flagged = [r["key"] for r in rows if r.get("single")]
    assert flagged == ["k0"], flagged
    assert next(r for r in rows if r["key"] == "k0")["prob_book"] == 0.70                  # proba brute gardée pour la calibration
    settle.record_combos(sent_combos, day)
    assert app.handle_command("/joue pari 1,5").startswith("Avec quelle mise en FCFA ?")        # sans budget du jour : la mise est demandée
    out = app.handle_command("/joue pari 1,5 100")
    assert "Noté" in out and "pari du jour du" in out and "Pari 0" in out and "💰 100 FCFA à" in out, out
    rows = track.load()
    assert next(r for r in rows if r["key"] == "k0").get("played") and not next(r for r in rows if r["key"] == "old").get("played")
    assert {r["key"] for r in played.single_rows(rows, store.load("bets.json", {})["combos"])} == {"old", "k0"}
    # anciennes lignes sans marque : l'ancienne règle (envoyé et dans aucun combiné) s'applique encore
    legacy = [{"key": "x", "date": "2026-10-08", "sent": True}, {"key": "y", "date": "2026-10-08", "sent": True},
              {"key": "z", "date": "2026-10-08", "sent": False, "single": True}]
    assert [r["key"] for r in played.single_rows(legacy, [{"date": "2026-10-08", "keys": ["y"]}])] == ["x"]
    # le règlement du pari du jour est annoncé même s'il est aussi dans un combiné
    rows = track.load()
    k0 = next(r for r in rows if r["key"] == "k0")
    k0 |= {"result": "win", "final": "2-0"}
    msg = app.results_message({"picks": [], "combos": []}, rows, [k0])
    assert msg and "Pari du jour" in msg, msg


# ---------- 3. calibration ----------

def test_calibration_uses_book_prob():
    """La calibration s'ajuste sur la proba brute des bookmakers (celle qu'elle corrige ensuite), pas sur r["prob"] déjà calibrée :
    ré-entraîner ne doit pas annuler l'entraînement précédent."""
    rows = []
    for i in range(C.CALIB_MIN_ROWS + 60):
        p = 0.35 + 0.3 * (i % 12) / 11                                           # proba brute 35 % -> 65 %
        win = (i * 7919 % 100) / 100 < 0.5 + 0.5 * (p - 0.5) + 0.05             # les bookmakers sont trop sûrs d'eux
        rows.append({"market": "h2h", "prob_book": p, "prob": p, "result": "win" if win else "loss"})
    cal1 = learn.calibrate(rows)
    assert cal1["resultat"]["active"], cal1
    model = {"calibration": cal1}
    # 2e entraînement : r["prob"] est maintenant la proba calibrée (comme odds.build_picks), prob_book reste la brute
    rows2 = [r | {"prob": learn.calibrated(model, "h2h", r["prob_book"])} for r in rows]
    cal2 = learn.calibrate(rows2)
    assert cal2 == cal1, (cal1, cal2)
    # anciennes lignes sans prob_book (ou None) : repli sur prob
    old = [{k: v for k, v in r.items() if k != "prob_book"} for r in rows]
    assert learn.calibrate(old) == cal1
    assert learn.calibrate([r | {"prob_book": None} for r in rows]) == cal1
    # le Brier du bilan détaillé reste celui de la proba annoncée r["prob"]
    wipe()
    done = [row(f"d{i}", "h2h", "Alpha", None, 2.0, result="win" if i % 2 else "loss", prob=0.8, prob_book=0.5) for i in range(4)]
    txt = settle.bilan({"picks": [], "combos": []}, rows=done, detail=True)
    assert "Brier (justesse des probas, plus bas = mieux, 0.25 = hasard) : 0.340" in txt, txt


def main():
    test_unknown_leg_pending()
    print("jambe invérifiable (settle_pending) : OK")
    test_unknown_not_counted()
    print("unknown : bankroll, messages, bilan : OK")
    test_fair_unknown_and_void()
    print("cote juste : unknown / void : OK")
    test_single_flag()
    print("pari du jour marqué (r['single']) : OK")
    test_calibration_uses_book_prob()
    print("calibration sur prob_book : OK")
    print("TOUS LES TESTS test_fix_settle : OK")


if __name__ == "__main__":
    main()
