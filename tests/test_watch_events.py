"""Suivi des matchs, moments du match (hors ligne) : fiche détaillée 365scores lue (vraies fiches allégées), buteur et penalty sur un but, plafond de
fiches par jour, mi-temps et fin dites une fois, carton rouge (365scores en direct ou fiche), but refusé par la VAR, prolongation (paris de buts réglés
sur le score à 90 min, fiche hors plafond), mi-temps nombreuses en une ligne. Lancer : python -m tests.test_watch_events"""
import contextlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-watchev-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import config as C, exposure, store, watch  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "s365")
KO = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)
DAY = str(KO.date())


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def leg(i, market="totals", name="Over", point=2.5):
    return {"id": f"e{i}", "key": f"e{i}|{market}||{point}|{name}", "home": f"H{i}", "away": f"A{i}", "market": market, "name": name, "point": point,
            "commence": KO.isoformat(), "pick": {"totals": f"{'Plus' if name == 'Over' else 'Moins'} de {str(point).replace('.', ',')} buts"}.get(market, market)}


def ticket(what, legs):
    return {"what": what, "tid": f"{DAY}|{what}", "name": f"Combi {what}", "stake": 100, "odds": 3.0, "legs": legs}


def games(n):
    return {i: {"id": i, "start": KO.isoformat(), "home": {"id": 100 + i, "name": f"H{i}"}, "away": {"id": 200 + i, "name": f"A{i}"}} for i in range(n)}


@contextlib.contextmanager
def tickets(ts):
    old = exposure.tickets
    exposure.tickets = lambda day: ts if str(day) == DAY else []
    try:
        yield
    finally:
        exposure.tickets = old


def live(score, minute, **kw):
    return {"live": True, "score": score, "minute": minute, "period": 1 if minute <= 45 else 2} | kw


def run(minutes, status, detail=None, n=1, calls=None):
    calls = calls if calls is not None else []

    def det(g):
        calls.append(g["id"])
        return detail(g) if callable(detail) else detail
    return watch.follow(KO + timedelta(minutes=minutes), games=games(n), live_for=lambda ids: status, stats_for=lambda g: {},
                        final_for=lambda g: None, detail_for=det)


def goal_ev(i, side, minute, player, sub="Field Goal", kind="goal"):
    return {"id": f"1|{minute}|0|{i}", "kind": kind, "sub": sub, "side": side, "min": f"{minute}'", "player": player}


def test_parse_detail():
    d = watch.parse_detail(fixture("watch_detail_ended2.json"))
    kinds = [(e["kind"], e["side"], e["min"], e["player"]) for e in d["events"]]
    assert kinds[:4] == [("goal", "home", "4'", kinds[0][3]), ("goal", "home", "6'", kinds[1][3]), ("goal", "home", "30'", kinds[2][3]),
                         ("goal", "away", "37'", kinds[3][3])] and all(k[3] for k in kinds[:4]), kinds
    assert ("var", "away", "62'") == kinds[4][:3] and ("red", "home", "89'") == kinds[5][:3], kinds
    assert d["s90"] == (3, 1) and d["ht"] == (3, 1) and d["text"] == "Ended"
    live_d = watch.parse_detail(fixture("watch_detail_live.json"))
    assert live_d["s90"] is None and live_d["ht"] == (0, 1) and [e["kind"] for e in live_d["events"]] == ["goal"] * 3, live_d     # en cours : pas de score à 90 min
    assert [e["player"] for e in live_d["events"]] == ["Asani", "Sassi", "Goudarzi"] and live_d["events"][1]["side"] == "home"
    assert watch.parse_detail({"x": 1}) is None
    print("ok  fiche détaillée : buts (buteur, minute), but refusé VAR, carton rouge, score à la mi-temps et à 90 min")


def test_goal_scorer_and_cap():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0)])]
    calls = []
    with tickets(ts):
        run(20, {0: live((0, 0), 20)}, calls=calls)
        det = {"events": [goal_ev(1, "home", 55, "Saka", sub="Penalty")], "s90": None, "ht": None, "text": "2nd Half"}
        msg = str(run(56, {0: live((1, 0), 56)}, det, calls=calls))
        assert msg.startswith("⚽ H0 1-0 A0 (56e) — Saka (penalty) 55'\n   Plus de 2,5 buts : il manque 2 buts"), msg
        assert calls == [0] and store.load(watch.FILE, {})["budget"]["detail"] == 1, calls
        old = C.WATCH_DETAIL_DAY
        C.WATCH_DETAIL_DAY = 1                         # plafond du jour atteint : pas de fiche, le but est dit sans buteur
        try:
            msg = str(run(70, {0: live((2, 0), 70)}, det, calls=calls))
        finally:
            C.WATCH_DETAIL_DAY = old
        assert msg.startswith("⚽ H0 2-0 A0 (70e)\n") and "—" not in msg and calls == [0], msg
        own = {"events": [goal_ev(2, "away", 80, "Dupont", sub="Own Goal")], "s90": None, "ht": None, "text": ""}
        msg = str(run(81, {0: live((3, 0), 81)}, own, calls=calls))
        assert "— Dupont (contre son camp) 80'" in msg and "✅ H0 – A0 : Plus de 2,5 buts (3-0)" in msg, msg
    print("ok  but : buteur et penalty / contre son camp par la fiche ; plafond du jour atteint : but dit sans buteur")


def test_halftime_fulltime_once():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0, name="Under", point=2.5)])]
    with tickets(ts):
        run(30, {0: live((1, 0), 30)})
        msg = str(run(47, {0: live((1, 0), 45, ht=True)}))
        assert msg.startswith("⏸️ Mi-temps · H0 1-0 A0\n   ⚽ Moins de 2,5 buts : encore 1 but de marge"), msg
        assert run(50, {0: live((1, 0), 45, ht=True)}) is None, "mi-temps dite une fois"
        msg = str(run(115, {0: {"live": False, "ended": True, "score": (1, 0), "minute": 90}}))
        assert msg.startswith("🏁 Terminé · H0 1-0 A0\n✅ H0 – A0 : Moins de 2,5 buts (1-0)"), msg
    print("ok  mi-temps et fin du match : une fois chacune, avec où en sont tes paris")


def test_red_card():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0)])]
    with tickets(ts):
        run(30, {0: live((0, 0), 30)})                                          # pas de redCards : 365scores ne les donne que s'il y en a
        msg = str(run(60, {0: live((0, 0), 60, red=(0, 1))}))
        assert msg == "🟥 Carton rouge pour A0 (60e) · H0 0-0 A0", msg            # 365scores en direct (redCards), le 1er rouge compris
        assert run(65, {0: live((0, 0), 65, red=(0, 1))}) is None, "dit une fois"
        det = {"events": [goal_ev(3, "home", 70, "X"), {"id": "3|72|0|9", "kind": "red", "sub": "", "side": "home", "min": "72'", "player": "Y"}],
               "s90": None, "ht": None, "text": ""}
        msg = str(run(73, {0: live((1, 0), 73)}, det))
        assert "🟥 Carton rouge pour H0 (72') · H0 1-0 A0" in msg and "⚽ H0 1-0 A0 (73e) — X 70'" in msg, msg     # par la fiche
    print("ok  carton rouge : par 365scores en direct, ou par la fiche lue sur un but")


def test_var_disallowed():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0)])]
    with tickets(ts):
        run(30, {0: live((1, 0), 30)})
        det = {"events": [goal_ev(4, "home", 50, "Z"), {"id": "11|62|0|5", "kind": "var", "sub": "Var", "side": "away", "min": "62'", "player": None}],
               "s90": None, "ht": None, "text": ""}
        msg = str(run(63, {0: live((2, 0), 63)}, det))
        assert "↩️ But refusé par la VAR (62') · H0 2-0 A0" in msg, msg
    print("ok  but refusé par la VAR : dit (fiche)")


def test_extra_time():
    store.save(watch.FILE, {})
    ts = [ticket("vm1", [leg(0, point=2.5), leg(0, market="alternate_totals_corners", point=9.5)])]       # + un pari de corners du même match, en jeu jusqu'au bout
    calls = []
    old, old_after = C.WATCH_DETAIL_DAY, C.WATCH_AFTER_LOSS
    C.WATCH_AFTER_LOSS = True                          # ce test couvre le texte de fin d'un match dont le coupon est déjà perdu (suivi demandé) ; le défaut ne le suit plus
    C.WATCH_DETAIL_DAY = 0                             # plafond du jour à zéro : la fiche de la prolongation passe quand même (elle règle tes paris)
    try:
        with tickets(ts):
            run(80, {0: live((1, 1), 80)}, calls=calls)
            det = {"events": [], "s90": (1, 1), "ht": (0, 1), "text": "Extra Time"}
            msg = str(run(100, {0: live((2, 1), 100, et=True)}, det, calls=calls))
            assert "⏱️ Prolongation · H0 – A0 : 1-1 après 90 min. Tes paris de buts se règlent sur ce score (temps réglementaire)." in msg, msg
            assert "❌ H0 – A0 : Plus de 2,5 buts (1-1 à 90 min)" in msg and "(prolongation, 100e) · sans effet sur tes paris de buts" in msg, msg
            assert calls == [0], calls
            msg = str(run(130, {0: {"live": False, "ended": True, "score": (3, 1), "minute": 120, "et": True}}, det, calls=calls))
            assert "🏁 Terminé · H0 1-1 à 90 min, 3-1 au final A0" in msg and "Prolongation" not in msg and "✅" not in msg, msg
            assert calls == [0], "score à 90 min déjà connu : pas de 2e fiche"
    finally:
        C.WATCH_DETAIL_DAY, C.WATCH_AFTER_LOSS = old, old_after
    print("ok  prolongation : dite une fois, paris de buts réglés sur le score à 90 min, fiche lue malgré le plafond du jour")


def test_compact_halftimes():
    store.save(watch.FILE, {})
    n = watch.HT_COMPACT + 1
    ts = [ticket(f"vm{i}", [leg(i)]) for i in range(n)]
    with tickets(ts):
        run(30, {i: live((0, 0), 30) for i in range(n)}, n=n)
        msg = str(run(47, {i: live((0, 0), 45, ht=True) for i in range(n)}, n=n))
    assert msg.startswith("⏸️ <b>Mi-temps</b> : H0 0-0 A0 · H1 0-0 A1 · ") and msg.count("⏸️") == 1 and "Plus de 2,5" not in msg, msg
    print(f"ok  plus de {watch.HT_COMPACT} mi-temps à la fois : une seule ligne")


def test_budget_new_day():
    d = {"budget": {"day": "2029-12-31", "detail": 40}}
    assert watch._budget_ok(d, KO) and d["budget"] == {"day": DAY, "detail": 0}
    print("ok  plafond de fiches remis à zéro chaque jour")


def main():
    test_parse_detail()
    test_goal_scorer_and_cap()
    test_halftime_fulltime_once()
    test_red_card()
    test_var_disallowed()
    test_extra_time()
    test_compact_halftimes()
    test_budget_new_day()
    print("tests moments du match : OK")


if __name__ == "__main__":
    main()
