"""Suivi des matchs, chances en direct et alertes (hors ligne) : la chance d'un pari à la minute 0 est celle d'avant-match, elle bouge avec le score, le
temps et les stats (rythme du match en clair) ; « ⚠️ pari en danger » une fois ; « ❗ dernier pari » une fois avec le gain ; 🌙 bilan de la soirée une fois
par jour. Lancer : python -m tests.test_watch_chances"""
import contextlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-watchch-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import config as C, exposure, inplay, store, watch  # noqa: E402

KO = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)
DAY = str(KO.date())


def leg(i, market="totals", name="Over", point=2.5, prob=0.55, ko=KO):
    return {"id": f"e{i}", "key": f"e{i}|{market}||{point}|{name}", "home": f"H{i}", "away": f"A{i}", "market": market, "name": name, "point": point,
            "commence": ko.isoformat(), "pick": f"Plus de {str(point).replace('.', ',')} buts" if name == "Over" else f"Moins de {point}", "prob": prob}


def ticket(what, legs, stake=100, odds=3.0):
    return {"what": what, "tid": f"{DAY}|{what}", "name": f"Combi {what}", "stake": stake, "odds": odds, "legs": legs}


GAMES = {i: {"id": i, "start": KO.isoformat(), "home": {"id": 100 + i, "name": f"H{i}"}, "away": {"id": 200 + i, "name": f"A{i}"}} for i in range(4)}


@contextlib.contextmanager
def tickets(ts):
    old = exposure.tickets
    exposure.tickets = lambda day: ts if str(day) == DAY else []
    try:
        yield
    finally:
        exposure.tickets = old


def run(minutes, status, stats=None):
    return watch.follow(KO + timedelta(minutes=minutes), games=GAMES, live_for=lambda ids: status, stats_for=lambda g: stats or {},
                        final_for=lambda g: None, detail_for=lambda g: None)


def live(score, minute, **kw):
    return {"live": True, "score": score, "minute": minute, "period": 1 if minute <= 45 else 2} | kw


def test_chance_math():
    for lg, key, side, stats in ((leg(0), "goals", None, None), (leg(0, "team_totals_home", point=1.5, prob=0.4), "goals", None, None),
                                 (leg(0, "alternate_totals_corners", point=9.5, prob=0.58), "corners", "total", {"corners": (0, 0)})):
        p, _ = inplay.chance(lg, key, side, (0, 0), 0, 1, stats=stats)
        assert abs(p - lg["prob"]) < 0.005, (lg["market"], p)                 # minute 0 : la chance d'avant-match
    o = leg(0)
    ps = [inplay.chance(o, "goals", None, (0, 0), m, 1 if m <= 45 else 2)[0] for m in (10, 30, 60, 80)]
    assert ps == sorted(ps, reverse=True), ps                                  # 0-0 : plus le temps passe, moins « plus de 2,5 » a de chances
    assert inplay.chance(o, "goals", None, (2, 0), 60, 2)[0] > inplay.chance(o, "goals", None, (1, 0), 60, 2)[0]
    assert inplay.chance(o, "goals", None, (3, 0), 60, 2)[0] == 1.0
    p_red = inplay.chance(leg(0, "team_totals_home", point=0.5, prob=0.7), "goals", None, (0, 0), 60, 2, reds=(1, 0))[0]
    assert p_red < inplay.chance(leg(0, "team_totals_home", point=0.5, prob=0.7), "goals", None, (0, 0), 60, 2)[0], "à 10 : moins de buts"
    c = leg(0, "alternate_totals_corners", point=9.5, prob=0.58)
    p, rhythm = inplay.chance(c, "corners", "total", (0, 0), 55, 2, stats={"corners": (4, 2)})
    assert rhythm == "6 corners à la 55e, rythme 9,8 sur 90 min" and 0 < p < 1, (p, rhythm)
    assert inplay.chance(leg(0, "h2h", name="H0"), "goals", None, (1, 0), 60, 2)[0] is None, "1N2 sans modèle : pas de chance"
    p, _ = inplay.chance(leg(0, "h2h", name="H0"), "goals", None, (1, 0), 60, 2, pred={"lh": 1.4, "la": 1.1})
    assert 0.6 < p < 0.95, p
    print("ok  chances : minute 0 = avant-match ; le temps, le score, les cartons rouges et le rythme des stats la font bouger")


def test_goal_line_with_chance():
    store.save(watch.FILE, {})
    with tickets([ticket("vm1", [leg(0)])]):
        run(20, {0: live((0, 0), 20)})
        msg = str(run(31, {0: live((1, 0), 31)}))
    assert "⚽ H0 1-0 A0 (31e)\n   Plus de 2,5 buts : il manque 2 buts · 60 % (55 % avant le match)" in msg, msg
    print("ok  sur un but : la chance du pari, avec celle d'avant-match")


def test_danger_once():
    store.save(watch.FILE, {})
    with tickets([ticket("vm1", [leg(0, point=1.5, prob=0.7)]), ticket("vm2", [leg(0, point=1.5, prob=0.7)])]):
        assert "danger" not in str(run(15, {0: live((0, 0), 15)})), "avant la 20e : jamais"
        msg = str(run(70, {0: live((0, 0), 70)}))
        assert "⚠️ Pari en danger · H0 – A0 (70e, 0-0) : Plus de 1,5 buts · " in msg and "(70 % avant le match) · 🎫 Combi vm1, 🎫 Combi vm2" in msg, msg
        assert msg.count("Pari en danger") == 1, "un pari dans 2 tickets : dit une fois"
        assert "danger" not in str(run(80, {0: live((0, 0), 80)})), "une fois par pari"
    print("ok  pari en danger : une fois, avec les tickets concernés, jamais avant la 20e")


def test_last_leg():
    store.save(watch.FILE, {})
    later = KO + timedelta(hours=2)
    t = ticket("vm1", [leg(0, point=0.5), leg(1, point=1.5, ko=later)], stake=1000, odds=18.4)
    with tickets([t]):
        run(20, {0: live((0, 0), 20)})
        msg = str(run(31, {0: live((1, 0), 31)}))
        assert "❗ Il ne manque que H1 – A1 (Plus de 1,5 buts) pour gagner 18 400 FCFA avec Combi vm1 · coup d'envoi 21h00 UTC" in msg, msg
        assert "❗" not in str(run(40, {0: live((1, 0), 40)})), "une fois par ticket"
    print("ok  dernier pari restant : une fois, avec le gain et l'heure du coup d'envoi")


def test_recap_once():
    store.save(watch.FILE, {})
    won = ticket("vm1", [leg(0, point=0.5)], stake=500, odds=2.0)
    near = ticket("vm2", [leg(0, point=0.5), leg(1, point=2.5)], stake=1000, odds=4.0)
    with tickets([won, near]):
        run(20, {0: live((0, 0), 20), 1: live((0, 0), 20)})
        run(60, {0: live((1, 0), 60), 1: live((1, 0), 60)})
        run(115, {0: {"live": False, "ended": True, "score": (1, 0), "minute": 90}, 1: {"live": False, "ended": True, "score": (1, 0), "minute": 90}})
        old = C.WATCH_RECAP_HOUR
        C.WATCH_RECAP_HOUR = 21
        try:
            msg = str(run(130, {}))
            assert run(140, {}) is None or "Bilan" not in str(run(150, {})), "une fois par jour"
        finally:
            C.WATCH_RECAP_HOUR = old
    assert "🌙 <b>Bilan de la soirée</b> · mardi 01/01" in msg, msg
    assert "🎫 2 tickets · 🏆 1 gagné · ❌ 1 perdu" in msg and "💰 Misé 1 500 FCFA · encaissé 1 000 FCFA · −500 FCFA" in msg, msg
    assert "😬 Perdus d'un seul pari :\n   • Combi vm2 (1/2) : H1 – A1, Plus de 2,5 buts (1-0)" in msg and "✅ 1 paris gagnés sur 2 (50 %)" in msg, msg
    print("ok  bilan de la soirée : une fois, tickets, argent, perdus d'un seul pari, paris gagnés")


def main():
    test_chance_math()
    test_goal_line_with_chance()
    test_danger_once()
    test_last_leg()
    test_recap_once()
    print("tests chances en direct et alertes : OK")


if __name__ == "__main__":
    main()
