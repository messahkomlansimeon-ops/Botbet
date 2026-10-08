"""Test de bout en bout hors ligne : journée -> clôture -> apprentissage ->
règlement -> bilan -> calibration -> commandes. Lancer : python -m tests.test_bot"""
import contextlib
import copy
import io
import math
import os
import random
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-test-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-cache-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import (app, bankroll, bootstrap, budget, combos as CB, config as C, learn, odds, settle, sources,  # noqa: E402
                   stats, store, telegram, track)
from kelly.sources import features, footballdata as fd, http, state  # noqa: E402
from tests import synth  # noqa: E402


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


UTC = timezone.utc
DEFAULT_SPORTS = list(C.SPORTS)
BOOKS = ("b1", "b2", "b3", "b4")


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}
        self.ok, self.text = status < 400, str(data)[:200]

    def json(self):
        return self._d


class FakeOdds:
    """Faux The Odds API (remplace requests.get) qui facture comme la vraie : /sports et /events gratuits,
    /odds = marchés x régions, /scores = 2. Renvoie les en-têtes x-requests-* ; garde le journal des appels.
    leagues : {sport_key: [(id, domicile, extérieur, commence ISO)]}."""

    def __init__(self, remaining, leagues, sports=()):
        self.remaining, self.leagues, self.log = remaining, leagues, []        # log : (chemin, marchés, coût)
        self.sports = list(sports) or list(leagues)
        self.finished = {}                                                     # id -> (buts dom, buts ext)
        self.req = []                                                          # (chemin, paramètres)

    def paid(self):
        return [c for c in self.log if c[2]]

    def spent(self):
        return sum(c[2] for c in self.log)

    def _books(self, ev, markets):
        n = sum(map(ord, ev["id"]))
        out = []
        for i, bk in enumerate(BOOKS):
            j = 1 + 0.01 * ((n + 3 * i) % 5 - 2)
            ms = []
            for m in markets:
                two = lambda point, a="Over", b="Under": [{"name": a, "price": round(1.9 * j, 2), "point": point},
                                                          {"name": b, "price": round(1.95 / j, 2), "point": point}]
                if m == "h2h":
                    ms.append({"key": m, "outcomes": [{"name": ev["home_team"], "price": round(2.0 * j, 2)},
                                                      {"name": ev["away_team"], "price": round(3.4 / j, 2)},
                                                      {"name": "Draw", "price": round(3.3 * j, 2)}]})
                elif m == "totals":
                    ms.append({"key": m, "outcomes": two(2.5)})
                elif m == "alternate_totals_corners":
                    ms.append({"key": m, "outcomes": two(9.5)})
                elif m == "alternate_totals_cards":
                    ms.append({"key": m, "outcomes": two(4.5)})
                elif m == "btts":
                    ms.append({"key": m, "outcomes": two(None, "Yes", "No")})
            ms and out.append({"key": bk, "title": bk.upper(), "markets": ms})
        return out

    def __call__(self, url, params=None, timeout=None, **kw):
        path, p = url.split("/v4", 1)[1], dict(params or {})
        markets = [m for m in str(p.get("markets", "")).split(",") if m]
        cost = 0
        if path.endswith("/odds"):
            cost = len(markets) * len(str(p.get("regions", "eu")).split(","))
        elif path.endswith("/scores"):
            cost = 2
        self.log.append((path, ",".join(markets), cost))
        self.req.append((path, p))
        hdr = lambda: {"x-requests-remaining": str(self.remaining), "x-requests-used": str(500 - self.remaining)}
        if cost > self.remaining:
            return Resp({"message": "usage quota", "error_code": "OUT_OF_USAGE_CREDITS"}, 401, hdr())
        self.remaining -= cost
        parts = path.strip("/").split("/")
        if path == "/sports":
            data = [{"key": k, "has_outrights": False} for k in self.sports] + [{"key": "soccer_epl_winner", "has_outrights": True}]
        else:
            lg = self.leagues.get(parts[1], [])
            lo, hi = p.get("commenceTimeFrom", ""), p.get("commenceTimeTo", "~")
            evs = [{"id": i, "sport_key": parts[1], "sport_title": parts[1], "home_team": h, "away_team": a, "commence_time": t}
                   for i, h, a, t in lg if lo <= t <= hi]
            if "eventIds" in p:
                evs = [e for e in evs if e["id"] in p["eventIds"].split(",")]
            if path.endswith("/scores"):
                data = [{"id": e["id"], "completed": True, "scores": [{"name": e["home_team"], "score": str(self.finished[e["id"]][0])},
                                                                    {"name": e["away_team"], "score": str(self.finished[e["id"]][1])}]}
                        for e in evs if e["id"] in self.finished]
            elif path.endswith("/events"):
                data = evs
            elif "/events/" in path:
                e = next(e for e in evs if e["id"] == parts[3])
                data = dict(e, bookmakers=self._books(e, markets))
            else:
                data = [dict(e, bookmakers=self._books(e, markets)) for e in evs]
        return Resp(data, 200, hdr())


class Sandbox:
    """Environnement isolé : faux The Odds API, horloge simulée, données vides, réglages par défaut."""

    def __enter__(self):
        self.saved = (requests_get(), dict(odds.CREDITS), budget._now, app.now_utc, os.environ.get("ODDS_API_KEY"),
                      os.environ.get("STATS_API_KEY"), C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS, C.CLOSE_GHOSTS,
                      dict(C.BUDGET_SHARES), C.ODDS_RESET_DAY, C.ODDS_MONTHLY_CREDITS, C.SPORTS, C.SOURCES_REFRESH)
        for f in os.listdir(store.DATA_DIR):
            os.remove(os.path.join(store.DATA_DIR, f))
        odds.CREDITS.update(remaining=None, used=None)
        odds._warned.clear()
        os.environ["ODDS_API_KEY"] = "test-odds-key"
        os.environ.pop("STATS_API_KEY", None)
        C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS, C.CLOSE_GHOSTS = 30, 6, 8, False
        C.SOURCES_REFRESH = False                  # hors ligne : les tests des sources l'activent avec un faux réseau
        features.reload()
        C.BUDGET_SHARES.clear(); C.BUDGET_SHARES.update(daily=0.6, closing=0.4)
        C.ODDS_RESET_DAY, C.ODDS_MONTHLY_CREDITS = 1, 500
        C.SPORTS = ["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga",
                    "soccer_france_ligue_one", "soccer_uefa_champs_league"]
        self.set_now(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
        return self

    def set_now(self, now):
        budget._now = app.now_utc = lambda: now

    def serve(self, fake):
        odds.requests.get = fake
        return fake

    def __exit__(self, *exc):
        (g, cr, bn, an, ok, sk, C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS, C.CLOSE_GHOSTS, shares,
         C.ODDS_RESET_DAY, C.ODDS_MONTHLY_CREDITS, C.SPORTS, C.SOURCES_REFRESH) = self.saved
        odds.requests.get = g
        odds.CREDITS.update(cr)
        budget._now, app.now_utc = bn, an
        for k, v in (("ODDS_API_KEY", ok), ("STATS_API_KEY", sk)):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        C.BUDGET_SHARES.clear(); C.BUDGET_SHARES.update(shares)


def requests_get():
    return odds.requests.get


def ko_at(day, hour, minute=0):
    return f"{day}T{hour:02d}:{minute:02d}:00Z"


def test_budget():
    """500 crédits le 1er -> 16/jour ; dépense refusée au-delà ; report du non-dépensé ; remise à zéro ;
    jamais d'appel quand il ne reste rien ; le plancher ne vaut pas pour la clôture."""
    with Sandbox() as sb:
        fake = sb.serve(FakeOdds(500, {"soccer_epl": [("e1", "A", "B", ko_at("2026-10-01", 15))]}))
        assert budget.days_left() == 31 and budget.allowance() == 16
        assert (budget.share("daily"), budget.share("closing"), budget.share("alert")) == (9, 6, 0)
        assert budget.can_spend(9, "daily") and not budget.can_spend(10, "daily")
        assert not budget.can_spend(1, "alert") and not budget.can_spend(1, "snapshot")      # désactivés sur gratuit
        # dépense via odds.get : décomptée (marchés x régions), persistée dans budget.json
        odds.get("/sports/soccer_epl/odds", use="daily", regions="eu", markets="h2h,totals")
        assert fake.spent() == 2 and budget.spent("daily") == 2 and budget.left("daily") == 7
        st = store.load("budget.json", {})
        assert st["day"] == "2026-10-01" and st["spent"] == {"daily": 2} and st["remaining"] == 498, st
        odds.get("/sports/soccer_epl/events/e1/odds", use="daily", regions="eu", markets="alternate_totals_corners")
        # refus propre au-delà de la part : BudgetError, aucune requête envoyée
        n = len(fake.log)
        try:
            odds.get("/sports/soccer_epl/odds", use="daily", regions="eu", markets=",".join(["h2h"] * 8))
            raise AssertionError("aurait dû être refusé")
        except odds.BudgetError as e:
            assert "budget" in str(e) and "test-odds-key" not in str(e)
        try:
            odds.get("/sports/soccer_epl/odds", use="alert", regions="eu", markets="h2h")
            raise AssertionError("alert n'a pas de budget")
        except odds.BudgetError:
            pass
        assert len(fake.log) == n
        # le non-dépensé est reporté : autre processus (CREDITS vide) et lendemain
        odds.CREDITS.update(remaining=None, used=None)
        assert budget.allowance() == 16 and budget.left("daily") == 6
        sb.set_now(datetime(2026, 10, 2, 8, tzinfo=UTC))
        assert budget.spent("daily") == 0 and budget.allowance() == int(497 / 30) == 16
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))                       # une semaine sans rien dépenser
        assert budget.allowance() == int(497 / 24) == 20
        # remise à zéro franchie (état du 30/09 avec 12 crédits -> 500 le 1er) ; jour de remise réglable
        store.save("budget.json", {"day": "2026-09-30", "spent": {"daily": 5}, "remaining": 12})
        odds.CREDITS.update(remaining=None)
        sb.set_now(datetime(2026, 10, 1, 8, tzinfo=UTC))
        assert budget.allowance() == 16
        C.ODDS_RESET_DAY = 15
        sb.set_now(datetime(2026, 10, 20, 8, tzinfo=UTC))
        assert budget.days_left() == 26 and budget.next_reset(datetime(2026, 12, 20).date()).isoformat() == "2027-01-15"
        # plus aucun crédit : aucun appel, jamais d'exception
        C.ODDS_RESET_DAY = 1
        sb.set_now(datetime(2026, 10, 15, 8, tzinfo=UTC))
        odds.CREDITS.update(remaining=0)
        n = len(fake.log)
        assert not budget.can_spend(1, "daily") and not budget.can_spend(1, "closing")
        for use in ("daily", "closing"):
            try:
                odds.get("/sports/soccer_epl/odds", use=use, regions="eu", markets="h2h")
                raise AssertionError("remaining=0 : appel interdit")
            except odds.BudgetError:
                pass
        with contextlib.redirect_stderr(io.StringIO()):
            assert odds.fetch_events(datetime(2026, 10, 15).date()) == [] and len(fake.log) == n   # aucun appel
        # plancher MIN_CREDITS : le matin s'arrête à 30, la clôture des paris envoyés peut puiser dedans
        sb.set_now(datetime(2026, 10, 31, 8, tzinfo=UTC))                      # dernier jour : tout le reste est dépensable
        odds.CREDITS.update(remaining=33)
        store.save("budget.json", {"day": "2026-10-31", "spent": {}, "remaining": 33})
        assert budget.allowance() == 33 and budget.can_spend(3, "daily") and not budget.can_spend(4, "daily")
        assert budget.can_spend(13, "closing") and not budget.can_spend(14, "closing")   # 40 % de 33 = 13
        # un budget.json illisible ne casse rien : on repart du forfait mensuel
        with open(store.path("budget.json"), "w") as f:
            f.write("{pas du json")
        odds.CREDITS.update(remaining=None)
        sb.set_now(datetime(2026, 10, 1, 8, tzinfo=UTC))
        assert budget.allowance() == 16
    print("budget : OK")


def test_free_events_and_typical_day():
    """/events gratuit : une ligue sans match ne coûte rien. Journée type (5 ligues dont 2 jouent, 12 matchs) :
    matin + clôtures horaires <= allocation du jour ; corners/cartons avant le reste des marchés détaillés."""
    day = "2026-10-01"
    with Sandbox() as sb:
        epl = [(f"epl{i}", f"E{i}", f"F{i}", ko_at(day, 13 + i % 8, 30 * (i % 2))) for i in range(7)]
        l1 = [(f"l1{i}", f"L{i}", f"M{i}", ko_at(day, 14 + i, 0)) for i in range(5)]
        tomorrow = [("sa1", "S1", "S2", ko_at("2026-10-02", 18))]            # demain : ne compte pas
        fake = sb.serve(FakeOdds(500, {"soccer_epl": epl, "soccer_france_ligue_one": l1, "soccer_italy_serie_a": tomorrow},
                                 sports=["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
                                         "soccer_germany_bundesliga", "soccer_france_ligue_one"]))
        # 1) le matin seul
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            events = odds.fetch_events(datetime(2026, 10, 1).date(), after=datetime(2026, 10, 1, 8, tzinfo=UTC))
        leagues_paid = {c[0].split("/")[2] for c in fake.paid()}
        assert leagues_paid == {"soccer_epl", "soccer_france_ligue_one"}, leagues_paid   # 3 ligues sans match : 0 appel payant
        assert not any(c[2] for c in fake.log if c[0].endswith("/events") or c[0] == "/sports")
        assert {c[0].split("/")[2] for c in fake.log if c[0].endswith("/events")} >= {"soccer_spain_la_liga", "soccer_germany_bundesliga"}
        assert len(events) == 12
        bulk = [c for c in fake.paid() if "/events/" not in c[0]]
        assert [c[1] for c in bulk] == ["h2h,totals"] * 2 and fake.spent() == 9 == budget.spent("daily")
        det = [c for c in fake.paid() if "/events/" in c[0]]
        assert len(det) == 5 and all(c[1] == "alternate_totals_corners" for c in det), det   # corners d'abord, pas de btts
        # 2) journée complète : matin (run_daily) + passages horaires de clôture
        for f in os.listdir(store.DATA_DIR):
            os.remove(os.path.join(store.DATA_DIR, f))
        odds.CREDITS.update(remaining=None, used=None)
        fake = sb.serve(FakeOdds(500, {"soccer_epl": epl, "soccer_france_ligue_one": l1}))
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            app.run_daily()
        morning = fake.spent()
        sent = [r for r in track.load() if r["sent"]]
        assert sent and morning == 9, (len(sent), morning)
        for h in range(11, 23):
            sb.set_now(datetime(2026, 10, 1, h, 5, tzinfo=UTC))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                app.run_track()
        total, allowance = fake.spent(), 16
        rows = track.load()
        closed_sent = sum(1 for r in rows if r["sent"] and r.get("clv") is not None)
        assert total <= allowance and budget.spent("closing") <= budget.share("closing") == 6, total
        assert budget.spent("daily") == 9 and fake.spent() == budget.spent("daily") + budget.spent("closing")
        assert closed_sent > 0
        closing = budget.spent("closing")
        sb.set_now(datetime(2026, 10, 2, 8, tzinfo=UTC))                       # le non-dépensé est reporté
        assert budget.allowance() == int((500 - total) / 30)
        print(f"journée type : 5 ligues dont 2 jouent, 12 matchs -> matin {morning}/9 crédits, "
              f"clôtures {closing}/6 ({closed_sent} paris envoyés clôturés sur {len(sent)}), total {total}/{allowance} ; "
              f"allocation du lendemain {budget.allowance()} (reste {fake.remaining}).")
    print("/events gratuit + journée type : OK")


def mk_row(key, ev, market, sent, ko, sport_key="soccer_epl"):
    return {"key": key, "id": ev, "sport_key": sport_key, "group": "soccer", "market": market, "odds": 2.0,
            "commence": ko, "closed_at": None, "sent": sent, "result": None, "close_odds": None, "clv": None}


def test_closing_plan():
    """Clôture : bulk par ligue quand >= 2 matchs, fantômes ignorés, Sûr d'abord, budget respecté sans erreur."""
    now = datetime(2026, 10, 1, 15, 5, tzinfo=UTC)
    ko = lambda m: (now + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    leagues = {"soccer_epl": [("e1", "Alpha", "Bravo", ko(30)), ("e2", "Cobra", "Delta", ko(45)), ("e3", "Echo", "Foxtrot", ko(60))],
               "soccer_france_ligue_one": [("f1", "Golf", "Hotel", ko(50))]}
    k = dict(e1="e1|h2h|||Alpha", e2="e2|totals||2.5|Over", e3="e3|alternate_totals_corners||9.5|Over",
             f1="f1|h2h|||Golf", g1="e1|btts|||Yes")
    rows = lambda: [mk_row(k["e1"], "e1", "h2h", True, ko(30)), mk_row(k["e2"], "e2", "totals", True, ko(45)),
                    mk_row(k["e3"], "e3", "alternate_totals_corners", True, ko(60)),
                    mk_row(k["f1"], "f1", "h2h", True, ko(50), "soccer_france_ligue_one"),
                    mk_row(k["g1"], "e1", "btts", False, ko(30))]            # fantôme : jamais relevé
    combos = [{"tier": C.COMBOS[1][0], "keys": [k["e1"], k["e2"], k["e3"]], "stake": 3.0, "result": None},
              {"tier": C.COMBOS[0][0], "keys": [k["f1"]], "stake": 5.0, "result": None}]

    # le plan seul : f1 (Sûr) d'abord ; bulk epl pour e1+e2 (h2h,totals = 2 crédits) ; e3 (corners) en appel par match
    calls = app.plan_closing([r for r in rows() if r["sent"]], combos)
    assert calls[0]["ids"] == ["f1"] and calls[0]["kind"] == "event", calls
    assert sorted((c["kind"], tuple(c["ids"]), tuple(sorted(c["markets"]))) for c in calls) == [
        ("bulk", ("e1", "e2"), ("h2h", "totals")), ("event", ("e3",), ("alternate_totals_corners",)), ("event", ("f1",), ("h2h",))]
    assert sum(c["cost"] for c in calls) == 4
    assert [c["kind"] for c in app.plan_closing([r for r in rows() if r["sent"]], combos, bulk=False)] == ["event"] * 4
    one = app.plan_closing([mk_row(k["e1"], "e1", "h2h", True, ko(30))], [])          # un seul match : pas de bulk
    assert [c["kind"] for c in one] == ["event"]

    def run(remaining, at, expect_calls):
        with Sandbox() as sb:
            sb.set_now(at)                                                    # horloge du budget (jour d'allocation)
            fake = sb.serve(FakeOdds(remaining, leagues))
            track.save(rows())
            store.save("bets.json", {"picks": [], "combos": combos})
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                n, _ = app.run_track(now=now)
            got = [(c[0].split("/odds")[0].split("/")[-1] if "/events/" in c[0] else "bulk:" + c[0].split("/")[2], c[1]) for c in fake.paid()]
            assert got == expect_calls, got
            return n, fake, track.load()
    # budget confortable (part clôture : 6) : tout est relevé, dans l'ordre de priorité, 4 crédits
    n, fake, out = run(500, now, [("f1", "h2h"), ("e3", "alternate_totals_corners"), ("bulk:soccer_epl", "h2h,totals")])
    assert n == 4 and fake.spent() == 4
    bulk = next(p for path, p in fake.req if path == "/sports/soccer_epl/odds")
    assert bulk["eventIds"] == "e1,e2" and bulk["markets"] == "h2h,totals", bulk      # ni e3 ni le fantôme btts
    assert all(r["clv"] is not None for r in out if r["sent"]) and out[-1]["closed_at"] is None   # fantôme intact
    # part clôture = 1 crédit : seul le Sûr (f1) est relevé, le reste est sauté sans erreur
    n, fake, out = run(100, now, [("f1", "h2h")])
    assert n == 1 and sum(1 for r in out if r["clv"] is not None) == 1 and out[3]["clv"] is not None
    # presque plus de crédits (10, sous le plancher de 30) : la clôture des paris envoyés puise quand même dedans
    n, fake, out = run(10, datetime(2026, 10, 31, 15, 5, tzinfo=UTC), [("f1", "h2h"), ("e3", "alternate_totals_corners"),
                                                                        ("bulk:soccer_epl", "h2h,totals")])
    assert n == 4 and fake.remaining == 6
    print("clôture ciblée : OK")


def test_select_sports():
    """Ordre de C.SPORTS, clés exactes (pas la version féminine), préfixe avec « _ », plafond MAX_SPORTS."""
    sports = [{"key": k} for k in ("soccer_uefa_champs_league_women", "soccer_uefa_champs_league", "basketball_nba",
                                   "soccer_epl", "x_sport")] + [{"key": "soccer_epl_winner", "has_outrights": True}]
    with Sandbox():
        C.SPORTS = ["soccer_epl", "soccer_uefa_champs_league", "basketball_nba"]
        assert [x["key"] for x in odds.select_sports(sports)] == ["soccer_epl", "soccer_uefa_champs_league", "basketball_nba"]
        C.SPORTS = ["soccer_"]
        assert len(odds.select_sports(sports)) == 3
        C.MAX_SPORTS = 2
        assert len(odds.select_sports(sports)) == 2 and len(odds.select_sports(sports, limit=False)) == 3
        assert DEFAULT_SPORTS and all(k.startswith("soccer_") for k in DEFAULT_SPORTS), DEFAULT_SPORTS   # gratuit : foot seul
    print("sélection des sports : OK")


def test_budget_never_crashes():
    """Budget refusé, 422, plafond de ligues : on saute et on logue en français, jamais d'exception."""
    day = "2026-10-01"
    with Sandbox() as sb:
        lg = {"soccer_epl": [("e1", "A", "B", ko_at(day, 15)), ("e2", "C", "D", ko_at(day, 16))],
              "soccer_spain_la_liga": [("s1", "E", "F", ko_at(day, 17))]}
        fake = sb.serve(FakeOdds(33, lg))
        sb.set_now(datetime(2026, 10, 31, 8, tzinfo=UTC))                      # dernier jour : allocation = 33
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            events = odds.fetch_events(datetime(2026, 10, 31).date())
        assert fake.remaining == 33 and events == []                           # aucun match le 31 : rien d'acheté
        lg["soccer_epl"][:] = [("e1", "A", "B", ko_at("2026-10-31", 15)), ("e2", "C", "D", ko_at("2026-10-31", 16))]
        lg["soccer_spain_la_liga"][:] = [("s1", "E", "F", ko_at("2026-10-31", 17))]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            events = odds.fetch_events(datetime(2026, 10, 31).date())
        # 33 crédits, plancher 30 : l'EPL (2 crédits) passe, la Liga ferait descendre à 29 < 30 -> sautée ;
        # il reste de quoi acheter UN marché détaillé (31 -> 30), les autres sont sautés
        assert len(events) == 2 and fake.remaining == 30 and [c[1] for c in fake.paid()] == ["h2h,totals", "alternate_totals_corners"]
        assert "soccer_spain_la_liga ignorée" in err.getvalue() and "ignoré(s)" in err.getvalue(), err.getvalue()
        # alertes / relevé du soir : aucun budget sur gratuit -> aucun appel du tout, liste vide
        n = len(fake.log)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert odds.fetch_events(datetime(2026, 10, 31).date(), optional=True, use="alert") == []
            assert odds.fetch_events(datetime(2026, 10, 31).date(), optional=True, use="snapshot") == []
        assert len(fake.log) == n and "n'a pas de budget" in err.getvalue()
        # budget réactivé (BUDGET_SHARES) : le balayage large repasse par le budget
        C.BUDGET_SHARES["alert"] = 0.2
        assert budget.share("alert") == 6
        # plafond de ligues avec matchs : les plus chargées d'abord
        C.BUDGET_SHARES.update(daily=1.0)
        C.MAX_SPORTS, C.MIN_CREDITS = 1, 0
        fake.remaining = 300
        events = odds.fetch_events(datetime(2026, 10, 31).date(), props=False)
        assert {e["sport_key"] for e in events} == {"soccer_epl"}              # 2 matchs > 1
        # run_track sans budget de clôture / sans crédit ne plante pas et ne dépense rien
        track.save([mk_row("e1|h2h|||A", "e1", "h2h", True, ko_at("2026-10-31", 8, 40))])
        store.save("bets.json", {"picks": [], "combos": []})
        C.BUDGET_SHARES["closing"] = 0
        odds.CREDITS.update(remaining=300)
        before = fake.remaining
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            app.run_track(now=datetime(2026, 10, 31, 8, 5, tzinfo=UTC))
        assert fake.remaining == before and "sautée" in err.getvalue()
    print("budget refusé sans erreur : OK")


def test_free_settlement():
    """Foot réglé via API-Football (aucun appel /scores, aucun crédit) ; /scores seulement en repli."""
    day = date.fromisoformat(stats._today()) - timedelta(days=1)
    ts = int(datetime.fromisoformat(f"{day}T19:00:00+00:00").timestamp())
    ko = f"{day}T19:00:00Z"
    phase = {"s": "NS"}
    football = []

    def api_football(url, params):
        football.append(params)
        if phase["s"] == "NS":
            fx = [(1, "Alpha", "Bravo", "NS", (None, None), None), (2, "Charlie", "Delta", "NS", (None, None), None)]
        else:
            fx = [(1, "Alpha", "Bravo", "FT", (2, 1), None), (2, "Charlie", "Delta", "AET", (3, 2), (1, 1))]
        return Resp({"errors": [], "response": [
            {"fixture": {"id": i, "timestamp": ts, "status": {"short": st}}, "goals": {"home": g[0], "away": g[1]},
             "teams": {"home": {"id": i * 10, "name": h}, "away": {"id": i * 10 + 1, "name": a}},
             **({"score": {"fulltime": {"home": ft[0], "away": ft[1]}}} if ft else {})} for i, h, a, st, g, ft in fx]})

    def row(key, ev, h, a, market, name, point, sent=True):
        return mk_row(key, ev, market, sent, ko) | {"home": h, "away": a, "name": name, "point": point, "who": None}
    rows = [row("m1|h2h|||Alpha", "m1", "Alpha", "Bravo", "h2h", "Alpha", None),
            row("m1|totals||2.5|Over", "m1", "Alpha", "Bravo", "totals", "Over", 2.5),
            row("m1|btts|||Yes", "m1", "Alpha", "Bravo", "btts", "Yes", None, sent=False),
            row("m2|h2h|||Draw", "m2", "Charlie", "Delta", "h2h", "Draw", None),
            row("m2|totals||2.5|Over", "m2", "Charlie", "Delta", "totals", "Over", 2.5)]
    with Sandbox() as sb:
        odds_fake = FakeOdds(500, {"soccer_epl": [("m1", "Alpha", "Bravo", ko), ("m2", "Charlie", "Delta", ko)]})
        odds_fake.finished.update(m1=(2, 1), m2=(3, 2))

        def net(url, **kw):
            return api_football(url, kw.get("params")) if "api-sports.io" in url else odds_fake(url, **kw)
        sb.serve(net)
        os.environ["STATS_API_KEY"] = "test-stats-key"
        stats._calls, stats._halted = 0, False
        stats._refreshed.clear()
        track.save(rows)
        # le matin du match : cache du jour avec statut NS (1 requête)
        assert stats.find_fixture(str(day), "Alpha", "Bravo", kickoff=ko) and len(football) == 1
        # le lendemain (autre exécution) : le cache NS d'un jour passé est re-téléchargé, puis tout est réglé gratuitement
        phase["s"] = "FT"
        stats._refreshed.clear()
        _, settled = settle.settle_pending(datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=8))
        res = {r["key"]: r["result"] for r in track.load()}
        assert res == {"m1|h2h|||Alpha": "win", "m1|totals||2.5|Over": "win", "m1|btts|||Yes": "win",
                       "m2|h2h|||Draw": "win",                                 # 90 min : 1-1 (pas 3-2 après prolongation)
                       "m2|totals||2.5|Over": "loss"}, res
        assert len(settled) == 5 and len(football) == 2, len(football)         # 1 requête de plus, pour tout le jour
        assert odds_fake.log == [] and budget.spent("daily") == 0              # aucun appel The Odds API, aucun crédit
        # sans STATS_API_KEY : repli /scores (2 crédits, budgétés) ; budget vide : on réessaiera, sans erreur
        os.environ.pop("STATS_API_KEY")
        odds.CREDITS.update(remaining=None)
        track.save([row("x1|h2h|||Zulu", "x1", "Zulu", "Yankee", "h2h", "Zulu", None)])
        odds_fake.leagues["soccer_epl"].append(("x1", "Zulu", "Yankee", ko))
        odds_fake.finished["x1"] = (1, 0)
        late = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=8)
        sb.set_now(late)
        odds_fake.remaining = 0
        odds.CREDITS.update(remaining=0)
        n = len(odds_fake.log)
        with contextlib.redirect_stderr(io.StringIO()):
            settle.settle_pending(late)
        assert len(odds_fake.log) == n and track.load()[0]["result"] is None   # budget refusé : pas d'appel, pas de crash
        odds_fake.remaining = 400
        odds.CREDITS.update(remaining=400)
        settle.settle_pending(late)
        assert track.load()[0]["result"] == "win" and [c[:1] + c[2:] for c in odds_fake.paid()] == [(f"/sports/soccer_epl/scores", 2)]
        assert budget.spent("daily") == 2
    print("règlement gratuit via API-Football : OK")


def test_check_is_free():
    """--check : rapport du budget et des ligues du jour, sans aucun appel payant ni secret affiché."""
    with Sandbox() as sb:
        fake = sb.serve(FakeOdds(480, {"soccer_epl": [("e1", "A", "B", ko_at("2026-10-01", 15)), ("e2", "C", "D", ko_at("2026-10-01", 17))]},
                                 sports=["soccer_epl", "soccer_spain_la_liga"]))
        real_api, old = telegram.api, {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
        os.environ.update(TELEGRAM_TOKEN="123:tok-secret", TELEGRAM_CHAT_ID="42")
        sent = []
        telegram.api = lambda method, **p: {"username": "kelly_bot"} if method == "getMe" else sent.append(p["text"]) or {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = app.run_check()
        finally:
            telegram.api = real_api
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        out = buf.getvalue()
        assert ok and fake.paid() == [] and fake.remaining == 480, fake.paid()
        assert "Allocation du jour : 15 crédits" in out and "31 jour(s) avant la remise à zéro" in out, out
        assert "soccer_epl (2)" in out and "spain" not in out.split("matchs aujourd'hui")[1].split("\n")[0], out
        assert "test-odds-key" not in out + "".join(sent) and "tok-secret" not in out + "".join(sent)
        assert "📡 Sources de stats" in out and "football-data : pas encore lancée" in out, out      # état des sources, sans réseau
        assert "Sources de stats" in sent[0]
    print("--check gratuit : OK")


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


class Raw:
    """Réponse HTTP brute (football-data) pour kelly.sources.http."""

    def __init__(self, status=200, body=b""):
        self.status_code, self.content, self.headers = status, body, {}


def fd_cache(csv_text, div="E0"):
    """Écrit un CSV football-data dans le cache disque des sources (périmé : fetched_at = 0), pour toutes les
    saisons que lit le règlement gratuit."""
    shutil.rmtree(http.cache_dir(), ignore_errors=True)
    for sn in fd.default_seasons():
        http._write(http._key(fd.url(div, sn)), {"fetched_at": 0, "status": 200}, csv_text.encode())


def test_settle_football_data():
    """Corners et cartons réglés gratuitement avec football-data (API-Football absent, ou quota épuisé) ; ces paris
    attendent 8 jours (football-data ne se met à jour que ~2 fois par semaine) avant d'être annulés, les buts 3."""
    now = datetime(2026, 9, 30, 8, tzinfo=UTC)
    ko = lambda n: f"{(now - timedelta(days=n)).date()}T15:00:00Z"                      # noqa: E731
    played = (now - timedelta(days=2)).strftime("%d/%m/%Y")
    csv = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC,HY,AY,HR,AR\n"
           f"E0,{played},Man City,Arsenal,2,1,7,5,2,3,0,1\n")                            # 12 corners, 6 cartons

    def row(key, market, name, point, n, home="Manchester City", away="Arsenal", ev="m1", who=None):
        return mk_row(key, ev, market, True, ko(n)) | {"home": home, "away": away, "name": name, "point": point, "who": who}
    rows = [row("c_over", "alternate_totals_corners", "Over", 11.5, 2), row("c_under", "alternate_totals_corners", "Under", 11.5, 2),
            row("k_over", "alternate_totals_cards", "Over", 5.5, 2), row("k_under", "alternate_totals_cards", "Under", 6.5, 2),
            row("goal", "h2h", "Manchester City", None, 2), row("shots", "player_shots", "Over", 1.5, 2, who="Bukayo Saka"),
            # match absent du fichier : corners/cartons attendent 8 jours, buts et tirs de joueurs 3 jours
            row("c_5d", "alternate_totals_corners", "Over", 8.5, 5, "Chelsea", "Liverpool", "m2"),
            row("k_5d", "alternate_totals_cards", "Over", 3.5, 5, "Chelsea", "Liverpool", "m2"),
            row("c_9d", "alternate_totals_corners", "Over", 8.5, 9, "Chelsea", "Liverpool", "m2"),
            row("k_9d", "alternate_totals_cards", "Over", 3.5, 9, "Chelsea", "Liverpool", "m2")]
    expect = {"c_over": "win", "c_under": "loss", "k_over": "win", "k_under": "win", "goal": "win", "shots": None,
              "c_5d": None, "k_5d": None, "c_9d": "void", "k_9d": "void"}
    shots_5d = row("s_5d", "player_shots", "Over", 1.5, 5, "Chelsea", "Liverpool", "m2", who="Cole Palmer")
    with Sandbox() as sb:
        fake = sb.serve(FakeOdds(500, {}))
        fd_cache(csv)
        track.save(rows + [shots_5d])
        with contextlib.redirect_stderr(io.StringIO()):
            _, settled = settle.settle_pending(now)
        res = {r["key"]: r["result"] for r in track.load()}
        assert res == expect | {"s_5d": "void"}, res
        assert len(settled) == 5 and fake.paid() == []          # aucun crédit : /scores ne sert qu'aux marchés de buts
        # un pari de buts introuvable : repli payant /scores (2 crédits, une fois), puis annulé après 3 jours seulement
        goal5 = row("g_5d", "h2h", "Chelsea", None, 5, "Chelsea", "Liverpool", "m2")
        goal2 = row("g_2d", "h2h", "Chelsea", None, 2, "Chelsea", "Liverpool", "m2")
        track.save([goal5, goal2])
        with contextlib.redirect_stderr(io.StringIO()):
            settle.settle_pending(now)
        assert {r["key"]: r["result"] for r in track.load()} == {"g_5d": "void", "g_2d": None}
        assert [c[0] for c in fake.paid()] == ["/sports/soccer_epl/scores"]
        # API-Football configuré mais quota du jour épuisé : même règlement, sans aucune requête api-sports
        os.environ["STATS_API_KEY"] = "k"
        stats._calls, stats._halted = 0, False
        store.save("stats_quota.json", {"day": stats._today(), "n": 999})
        track.save([dict(r, result=None) for r in rows[:6]])
        n = len(fake.log)
        sb.serve(lambda url, **kw: (_ for _ in ()).throw(AssertionError(f"requête inattendue : {url}")))
        settle.settle_pending(now)
        assert {r["key"]: r["result"] for r in track.load()} == {k: v for k, v in expect.items() if k in
                                                                  [r["key"] for r in rows[:6]]}
        assert len(fake.log) == n
        # fichier absent du cache : rien n'est réglé (corners attendent), pas d'erreur
        shutil.rmtree(http.cache_dir(), ignore_errors=True)
        track.save([dict(rows[0], result=None)])
        settle.settle_pending(now)
        assert track.load()[0]["result"] is None
    print("règlement corners/cartons via football-data (8 jours) : OK")


def hist_boot(seasons=("2324", "2425", "2526")):
    ms = []
    for sn in seasons:
        ms += fd.parse_csv(synth.csv_for(sn), "E0", sn)[0]
    return bootstrap.build(ms)


def test_bootstrap_training():
    """Amorçage : modèle entraîné dès le premier jour sur l'historique football-data (test sur la tranche la plus
    récente, mise de côté), puis validé sur les paris réels dès qu'il y en a assez ; le 🧠 dit combien de lignes."""
    obj = hist_boot()
    boot = learn.boot_rows(obj)
    # 1) jour 1, aucun pari réel : adopté sur la tranche historique mise de côté
    m = learn.train_clv([], None, boot)
    tr, te = learn._split(boot)
    assert m["adopted"] and m["oos_source"] == "historique" and m["n"] == 0 and m["boot_n"] == len(boot), m
    assert m["test_rows"] == len(te) > 20 and max(r["date"] for r in tr) < min(r["date"] for r in te)
    assert m["oos_model"] >= m["oos_heuristic"] and "bootstrap" in m["names"] and "gap_goals" in m["names"]
    assert learn.predict(m, boot[0]["features"]) is not None
    # 2) ni réel ni assez d'historique : pas de modèle
    few = learn.train_clv([], None, boot[:C.BOOT_MIN_ROWS - 1])
    assert not few["adopted"] and "pas assez de clôtures" in few["reason"]
    assert not learn.train_clv([], None, [])["adopted"]
    # 3) assez de paris réels (>= 40 sur >= 2 jours) : le test se fait sur les paris réels, pas sur l'historique
    random.seed(4)
    real = []
    for d in range(6):
        for i in range(12):
            edge = random.uniform(0, 0.25)
            real.append({"date": f"2026-09-{10 + d}", "clv": round(0.5 * edge + random.gauss(0, 0.02), 4), "sent": False,
                         "features": {"edge": edge, "heuristic": edge * 0.8, "log_odds": 0.7, "market=h2h": 1.0, "group=soccer": 1.0}})
    r = learn.train_clv(real, None, boot)
    assert r["oos_source"] == "réel" and r["n"] == 72 and r["test_rows"] == 24 and r["boot_n"] == len(boot), r
    assert r["adopted"] and r["oos_model"] >= r["oos_heuristic"]
    # une seule journée de paris réels : pas de vrai test hors échantillon possible -> tranche historique
    one = learn.train_clv([dict(x, date="2026-09-10") for x in real], None, boot)
    assert one["oos_source"] == "historique" and one["n"] == 72
    # sans historique, comportement d'avant (test sur les paris réels)
    assert learn.train_clv(real)["oos_source"] == "réel" and learn.train_clv(real[:30])["reason"].startswith("pas assez")
    # 4) un modèle adopté sur l'historique ne survit pas à un test réel perdu ; un modèle adopté sur du réel, si
    old_predict = learn.predict
    learn.predict = lambda model, f: -f.get("heuristic", 0.0)               # classement à l'envers : pire que l'heuristique
    try:
        lost = learn.train_clv(real, {"adopted": True, "oos_source": "historique", "names": ["edge"], "mean": [0], "std": [1], "w": [1, 0]}, boot)
        assert not lost["adopted"] and "ne bat pas" in lost["reason"], lost
        kept = learn.train_clv(real, {"adopted": True, "oos_source": "réel", "names": ["edge"], "mean": [0], "std": [1], "w": [1, 0]}, boot)
        assert kept["adopted"] and "ancien conservé" in kept["reason"] and kept["names"] == ["edge"]
    finally:
        learn.predict = old_predict
    # 5) learn.update lit data/bootstrap.json ; le 🧠 annonce les lignes historiques même sans paris suivis
    with Sandbox():
        store.save("bootstrap.json", obj)
        model = learn.update([])
        assert model["adopted"] and model["boot_n"] == len(obj["rows"]) and store.load("model.json", {})["boot_n"] == model["boot_n"]
        d = learn.describe(model, [])
        assert d.startswith("🧠 KELLY APPREND") and f"Historique football-data : {len(obj['rows'])} lignes" in d, d
        assert "matchs historiques" in d
        store.save("bootstrap.json", {"rows": "illisible"})                 # fichier corrompu : sans effet
        assert not learn.update([])["adopted"]
        os.remove(store.path("bootstrap.json"))
        assert learn.describe(learn.update([]), []) == ""
    print("amorçage de l'apprentissage : OK")


def fixture_event(i, home, away, ko, stars=False):
    """Match avec 1N2, corners (9,5), cartons (4,5) et tirs d'un joueur, chez 4 books."""
    books = []
    for b in range(4):
        j = 1 + 0.01 * ((i + b) % 3 - 1)
        ms = [{"key": "h2h", "outcomes": [{"name": home, "price": round(2.0 * j, 2)}, {"name": away, "price": round(3.4 / j, 2)},
                                         {"name": "Draw", "price": round(3.3 * j, 2)}]},
              {"key": "totals", "outcomes": [{"name": "Over", "price": round(1.9 * j, 2), "point": 2.5},
                                             {"name": "Under", "price": round(1.95 / j, 2), "point": 2.5}]},
              {"key": "alternate_totals_corners", "outcomes": [{"name": "Over", "price": round(1.9 * j, 2), "point": 9.5},
                                                               {"name": "Under", "price": round(1.95 / j, 2), "point": 9.5}]},
              {"key": "alternate_totals_cards", "outcomes": [{"name": "Over", "price": round(1.9 * j, 2), "point": 4.5},
                                                             {"name": "Under", "price": round(1.95 / j, 2), "point": 4.5}]}]
        if stars:
            ms.append({"key": "player_shots", "outcomes": [
                {"name": "Over", "description": "Bukayo Saka", "price": round(2.0 * j, 2), "point": 1.5},
                {"name": "Under", "description": "Bukayo Saka", "price": round(1.85 / j, 2), "point": 1.5}]})
        books.append({"key": f"b{b}", "title": f"B{b}", "markets": ms})
    return {"id": f"ev{i}", "sport_key": "soccer_epl", "sport_title": "EPL", "home_team": home, "away_team": away,
            "commence_time": ko, "bookmakers": books}


def put_profiles():
    v = lambda cf, ca, kf, ka, gf, ga: {"n": 10, "corners_for": cf, "corners_against": ca, "cards_for": kf, "cards_against": ka,   # noqa: E731
                                        "shots_for": 14.0, "shots_against": 9.0, "sot_for": 5.0, "sot_against": 3.0,
                                        "fouls_for": 10.0, "fouls_against": 11.0, "goals_for": gf, "goals_against": ga}
    team = lambda *a: {"div": "E0", "last": "2026-09-27", "all": v(*a), "home": v(*a), "away": v(*a)}                             # noqa: E731
    store.save("src_footballdata.json", {"updated": "2026-09-30T08:00", "referees": {}, "leagues": {}, "teams": {
        "Arsenal": team(7, 4, 1.5, 2, 2.0, 0.8), "Chelsea": team(5, 6, 2, 2, 1.4, 1.2), "Liverpool": team(6, 5, 1.8, 2.1, 1.9, 1.0)}})
    f = lambda xg, xga: {"n": 6, "xg": xg, "xga": xga}                                                                              # noqa: E731
    u = lambda xg, xga: {"all": f(xg, xga), "home": f(xg, xga), "away": f(xg, xga)}                                                 # noqa: E731
    store.save("src_understat.json", {"leagues": {"EPL": {"teams": {"Arsenal": u(1.9, 1.0), "Chelsea": u(1.3, 1.5)}, "players": {
        "Arsenal": {"Bukayo Saka": {"min": 2000, "pos": "F", "shots90": 2.9}}}}}})
    features.reload()


def test_stats_and_analyse_commands():
    """/stats <équipe> (profil des derniers matchs) et /analyse <match> (chiffres d'avant-match + meilleurs paris)."""
    day = date(2026, 10, 1)
    with Sandbox():
        put_profiles()
        assert "/stats" in app.handle_command("/aide") and "/analyse" in app.handle_command("/aide")
        t = app.handle_command("/stats arsenal")
        assert t.startswith("📈 Arsenal (Premier League)") and "10 derniers matchs (football-data)" in t, t
        assert "Corners : 7.0 pour / 4.0 contre" in t and "Cartons (jaunes + rouges) : 1.5 pour / 2.0 contre" in t, t
        assert "Tirs : 14.0 pour / 9.0 contre" in t and "Fautes" in t and "Buts : 2.0 pour / 0.8 contre" in t, t
        assert "xG (Understat, 6 derniers) : 1.90 pour / 1.00 contre ; domicile 1.90/1.00" in t and "actualisées le 30/09" in t, t
        assert "Corners" in app.handle_command("/stats Chelsea") and "xG" not in app.handle_command("/stats Liverpool")
        assert app.handle_command("/stats").startswith("Exemple") and "Aucune stat pour « Zzz »" in app.handle_command("/stats Zzz")
        # /stats fonctionne sans que la journée ait été analysée ; /analyse non
        assert app.handle_command("/analyse arsenal").startswith("Kelly n'a pas encore analysé")
        events = [fixture_event(1, "Arsenal", "Chelsea", "2026-10-01T18:00:00Z", stars=True),
                  fixture_event(2, "Chelsea", "Liverpool", "2026-10-01T19:00:00Z")]
        picks = stats.enrich(odds.build_picks(events, day), day)
        store.save("today.json", {"day": str(day), "picks": picks})
        a = app.handle_command("/analyse arsenal")
        assert a.startswith("🔬 Arsenal - Chelsea (18:00 UTC)"), a
        assert "Corners attendus : 11.0 (6.5 + 4.5)" in a and "Cartons attendus : 3.8 (1.8 + 2.0)" in a, a
        assert "xG attendus : 2.85 (1.70 - 1.15)" in a and "football-data, understat" in a, a
        assert "Meilleurs paris :" in a and "[modèle 11.0 vs ligne 9.5]" in a and "stats réelles : 11.0 corners attendus" in a, a
        assert "Bukayo Saka : Plus de 1.5 tirs" in a and "[modèle 2.7 vs ligne 1.5]" in a and "stats réelles : 2.9 tirs/90 vs ligne 1.5" in a, a
        assert a.count("• ") >= 7                                       # 6 paris + lignes de chiffres
        assert app.handle_command("/analyse xyz").startswith("Aucun match du jour")
        assert app.handle_command("/analyse").startswith("Exemple")
        both = app.handle_command("/analyse chelsea")
        assert both.startswith("Plusieurs matchs correspondent") and "Arsenal - Chelsea" in both and "Chelsea - Liverpool" in both
        # équipes inconnues des sources : message clair, paris quand même listés
        features._cache["fd"], features._cache["us"] = {}, {}
        b = app.handle_command("/analyse liverpool")
        assert "Pas de stats réelles pour ce match" in b and "Meilleurs paris" in b
        features.reload()
    print("commandes /stats et /analyse : OK")


def test_special_combo():
    """Combiné spécial : uniquement corners, cartons et stats joueurs, cote 6 à 15, un pari par match."""
    day = date(2026, 10, 1)
    pairs = [("A", "B"), ("C", "D"), ("E", "F"), ("G", "H"), ("I", "J"), ("K", "L")]
    events = [fixture_event(i, h, a, "2026-10-01T1%d:00:00Z" % i, stars=i == 0) for i, (h, a) in enumerate(pairs)]
    with Sandbox():
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        picks = odds.build_picks(events, day)
        assert any(c["market"] == "h2h" for c in picks)
        messages, sent, combos = app.build_day(picks, day)
        assert C.SPECIAL in [t for t, _, _ in C.COMBOS] and [t for t, _, _ in C.COMBOS][2] == C.SPECIAL
        msg = next(m for m in messages if m.startswith(C.SPECIAL))
        assert "Pas assez" not in msg and "Mise conseillée" in msg, msg
        sp = next(c for c in combos if c["tier"] == C.SPECIAL)
        legs = [next(c for c in picks if c["key"] == k) for k in sp["keys"]]
        assert 6.0 <= sp["odds"] <= 15.0 and len(legs) >= 2 and len({c["id"] for c in legs}) == len(legs)
        assert all(CB.soft_market(c) and (c["market"].startswith("player_") or c["market"] in
                   ("alternate_totals_corners", "alternate_totals_cards")) for c in legs), [c["market"] for c in legs]
        assert sp["stake"] <= 1000 * C.STAKE_CAP["🎯"] + 1e-9
        assert all(c["key"] in {x["key"] for x in sent} for c in legs)                       # suivis comme les autres combinés
        pool = CB.make_special_pool(picks)
        assert pool and all(CB.pool_for(C.SPECIAL, picks, [])[i]["key"] == pool[i]["key"] for i in range(len(pool)))
        assert CB.pool_for(C.COMBOS[0][0], picks, ["x"]) == ["x"]
        # priorité de clôture : après le Costaud, avant le Méchant
        rk = track.ranks([{"tier": C.SPECIAL, "keys": ["k1"], "stake": 1, "result": None},
                          {"tier": C.COMBOS[1][0], "keys": ["k2"], "stake": 1, "result": None},
                          {"tier": C.COMBOS[3][0], "keys": ["k3"], "stake": 1, "result": None}])
        assert rk["k2"] < rk["k1"] < rk["k3"], rk
        # sans marché mal pricé : message « pas assez de matchs »
        only_h2h = [c for c in picks if c["market"] in ("h2h", "totals")]
        out = app.build_day(only_h2h, day)[0]
        assert next(m for m in out if m.startswith(C.SPECIAL)).endswith("Pas assez de matchs aujourd'hui pour ce combiné.")
    print("combiné spécial corners/cartons : OK")


def test_run_daily_with_sources():
    """run_daily : sources actualisées AVANT le bilan, amorçage, estimations d'avant-match dans les paris et les
    variables d'apprentissage, message 📡 ; une panne des sources ne casse jamais le run."""
    csvs = {sn: synth.csv_for(sn, until=date(2026, 9, 28) if sn == "2627" else None) for sn in ("2223", "2324", "2425", "2526", "2627")}
    leagues = {"soccer_epl": [("ev1", "Arsenal", "Chelsea", ko_at("2026-10-01", 18)),
                              ("ev2", "Liverpool", "Manchester City", ko_at("2026-10-01", 19))]}
    saved = (state.clock, http._sleep, sources.refresh_all, bootstrap.ensure, settle.settle_pending)
    state.clock, http._sleep = lambda: datetime(2026, 10, 1, 8, tzinfo=UTC).timestamp(), lambda s: None
    order = []
    try:
        with Sandbox() as sb:
            C.SOURCES_REFRESH, C.SPORTS = True, ["soccer_epl"]
            bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
            fake = FakeOdds(500, leagues)
            urls = []

            def net(url, **kw):
                if "the-odds-api" in url:
                    return fake(url, **kw)
                urls.append(url)
                m = re.search(r"mmz4281/(\d{4})/E0\.csv", url)
                if url.endswith("/robots.txt"):
                    return Raw(200, b"User-agent: *\nDisallow:\n")
                return Raw(200, csvs[m[1]].encode()) if m and m[1] in csvs else Raw(404, b"")
            sb.serve(net)
            shutil.rmtree(http.cache_dir(), ignore_errors=True)
            http.reset()
            features.reload()
            real_refresh, real_settle = sources.refresh_all, settle.settle_pending
            sources.refresh_all = lambda keys=None: order.append(("refresh", keys)) or real_refresh(keys)
            settle.settle_pending = lambda *a, **k: order.append(("settle",)) or real_settle(*a, **k)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                msgs = app.run_daily()
            assert order[0] == ("refresh", ["soccer_epl"]) and order[1] == ("settle",), order       # CSV à jour avant le règlement
            src = next(m for m in msgs if m.startswith("📡 Sources de stats"))
            assert "✅ football-data" in src and "Understat" in src and "2/2 matchs couverts" in src, src
            today = store.load("today.json", {})["picks"]
            cor = [c for c in today if c["market"] == "alternate_totals_corners"]
            assert cor and all(c["pm"]["src"] == "sources" and c["pm"]["kind"] == "corners" for c in cor), cor[:1]
            assert any(c["pm"]["kind"] == "goals" for c in today if c["market"] == "totals" and c.get("pm"))
            assert any(c.get("pm", {}).get("xgd") for c in today if c["market"] == "h2h")
            rows = track.load()
            assert any("gap_corners" in r["features"] and "pm_gap" in r["features"] for r in rows)
            assert all("heuristic" in r["features"] for r in rows) and not any("bootstrap" in r["features"] for r in rows)
            boot = store.load("bootstrap.json", {})
            assert len(boot["rows"]) > 200 and boot["seasons"] == ["2324", "2425", "2526", "2627"], boot.get("seasons")
            assert os.path.getsize(store.path("bootstrap.json")) < 100_000
            assert os.path.exists(store.path("src_footballdata.json")) and not any("api-sports" in u for u in urls)
            # le lendemain : le modèle appris sur l'historique est utilisé et le 🧠 annonce les lignes historiques
            model = learn.update(track.load())
            assert model["adopted"] and model["boot_n"] == len(boot["rows"]) and "Historique football-data" in learn.describe(model, track.load())

        # panne des sources : exception de refresh_all ET de l'amorçage -> le run continue, journée complète envoyée
        with Sandbox() as sb:
            C.SOURCES_REFRESH, C.SPORTS = True, ["soccer_epl"]
            bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
            sb.serve(FakeOdds(500, leagues))
            calls = []

            def boom(*a, **k):
                calls.append(1)
                raise RuntimeError("source en panne")
            sources.refresh_all, bootstrap.ensure = boom, boom
            settle.settle_pending = real_settle
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                msgs = app.run_daily()
            assert len(calls) == 2 and "actualisation ignorée" in err.getvalue() and "amorçage ignoré" in err.getvalue()
            assert any(m.startswith("🔥 KELLY") for m in msgs) and any("PARI DU JOUR" in m for m in msgs) and track.load()
            assert "pas encore lancée" in next(m for m in msgs if m.startswith("📡 Sources de stats"))
            # kill-switch : SOURCES_REFRESH=0 n'appelle rien
            C.SOURCES_REFRESH = False
            calls.clear()
            assert app.refresh_sources() == {} and calls == []
    finally:
        state.clock, http._sleep, sources.refresh_all, bootstrap.ensure, settle.settle_pending = saved
        features.reload()
    print("run_daily + sources (et pannes) : OK")


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
    C.CLOSE_GHOSTS = True                      # payant : les fantômes sont aussi clôturés (données d'apprentissage)
    for h in range(13, 23):
        n, _ = app.run_track(events_for=fetch, now=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h))
        total += n
    C.CLOSE_GHOSTS = False
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
    test_select_sports()
    test_budget()
    test_free_events_and_typical_day()
    test_closing_plan()
    test_budget_never_crashes()
    test_free_settlement()
    test_check_is_free()
    test_check()
    test_settle_football_data()
    test_bootstrap_training()
    test_stats_and_analyse_commands()
    test_special_combo()
    test_run_daily_with_sources()
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("OK : tout le flux passe hors ligne.")


if __name__ == "__main__":
    main()
