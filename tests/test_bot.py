"""Test de bout en bout hors ligne : journée -> clôture -> apprentissage ->
règlement -> bilan -> calibration -> commandes. Lancer : python -m tests.test_bot"""
import contextlib
import copy
import io
import json
import math
import os
import random
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-test-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

def corrupt(name):
    """Document illisible dans la base (JSON cassé), comme un ancien fichier abîmé."""
    with store._db() as con:
        con.execute("INSERT OR REPLACE INTO docs (name, body, updated) VALUES (?, '{pas du json', 'x')", (name,))


from kelly import (app, bankroll, bootstrap, budget, cache, calib, chat, combos as CB, fair, fmt, config as C, learn, odds, played,  # noqa: E402
                   settle, sources, stats, store, telegram, track)
from kelly.sources import features, footballdata as fd, http, state  # noqa: E402
from tests import make_sample, synth  # noqa: E402

# Les matchs fictifs sont datés de la journée de test : on ne les écarte pas comme « déjà commencés » selon l'heure réelle
# (le filtre lui-même est testé dans test_started_matches_excluded).
_real_upcoming = app.upcoming
app.upcoming = lambda items, now=None: list(items)
from tests.fake_api import FakeFootballAPI, match as fake_match, seed_history, ts_of  # noqa: E402


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
                        "Casemiro": {"shots": 1, "shots_on_target": 0, "fouls": 3}}
                       | {n: {"shots": random.randint(0, 5), "shots_on_target": random.randint(0, 3), "fouls": random.randint(0, 4)}
                          for n in make_sample.PLAYERS}}


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
            k = 1.3 if i in (1, 3) else 1.0                 # deux books en retard paient 30 % de plus sur « plus de » / domicile
            ms = []
            for m in markets:
                two = lambda point, a="Over", b="Under": [{"name": a, "price": round(1.9 * j * k, 2), "point": point},
                                                          {"name": b, "price": round(1.95 / j, 2), "point": point}]
                if m == "h2h":
                    ms.append({"key": m, "outcomes": [{"name": ev["home_team"], "price": round(2.0 * j * k, 2)},
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


def wipe():
    """Données et caches vides (un nouveau « jour » : rien en cache, ni cotes, ni compteurs)."""
    for f in os.listdir(store.DATA_DIR):
        os.remove(os.path.join(store.DATA_DIR, f))
    shutil.rmtree(cache.odds_dir(), ignore_errors=True)
    cache.reset()
    stats._neg.clear()


class Sandbox:
    """Environnement isolé : faux The Odds API, horloge simulée, données vides, réglages par défaut."""

    def __enter__(self):
        self.saved = (requests_get(), dict(odds.CREDITS), budget._now, app.now_utc, os.environ.get("ODDS_API_KEY"),
                      os.environ.get("STATS_API_KEY"), C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS, C.CLOSE_GHOSTS,
                      dict(C.BUDGET_SHARES), C.ODDS_RESET_DAY, C.ODDS_MONTHLY_CREDITS, C.SPORTS, C.SOURCES_REFRESH, stats._today)
        wipe()
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
        stats._today = lambda: now.date().isoformat()         # l'horloge d'API-Football (fenêtre hier..demain du forfait gratuit) suit celle du test

    def serve(self, fake):
        odds.requests.get = fake
        return fake

    def __exit__(self, *exc):
        (g, cr, bn, an, ok, sk, C.MIN_CREDITS, C.MAX_SPORTS, C.PROP_EVENTS, C.CLOSE_GHOSTS, shares,
         C.ODDS_RESET_DAY, C.ODDS_MONTHLY_CREDITS, C.SPORTS, C.SOURCES_REFRESH, stats._today) = self.saved
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
        corrupt("budget.json")
        odds.CREDITS.update(remaining=None)
        sb.set_now(datetime(2026, 10, 1, 8, tzinfo=UTC))
        assert budget.allowance() == 16
    print("budget : OK")


def put_team_profiles(by_div):
    """Profils football-data fictifs {division: [équipes]} : les matchs de ces équipes ont un modèle corners/cartons."""
    v = {"n": 10, "corners_for": 5.5, "corners_against": 5.0, "cards_for": 2.0, "cards_against": 2.0, "goals_for": 1.4,
         "goals_against": 1.3}
    store.save("src_footballdata.json", {"updated": "2026-09-30T08:00", "referees": {}, "leagues": {}, "teams": {
        t: {"div": d, "last": "2026-09-27", "all": v, "home": v, "away": v} for d, ts in by_div.items() for t in ts}})
    features.reload()


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
        profiles = {"E0": [t for e in epl for t in e[1:3]], "F1": [t for e in l1 for t in e[1:3]]}
        put_team_profiles(profiles)                                    # sans profil football-data : aucun crédit en corners
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
        put_team_profiles(profiles)
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
        put_team_profiles({"E0": ["A", "B", "C", "D"], "SP1": ["E", "F"]})
        sb.set_now(datetime(2026, 10, 31, 8, tzinfo=UTC))                      # dernier jour : allocation = 33
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            events = odds.fetch_events(datetime(2026, 10, 31).date())
        assert fake.remaining == 33 and events == []                           # aucun match le 31 : rien d'acheté
        shutil.rmtree(cache.odds_dir(), ignore_errors=True)                    # le monde change : le cache du relevé gratuit aussi
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


def age_fixture_cache(hours=24):
    """Vieillit le relevé d'API-Football de `hours` heures (simule le passage au lendemain)."""
    fx = store.load("fixtures_day.json", {})
    fx["_fetched"] = {d: t - hours * 3600 for d, t in fx.get("_fetched", {}).items()}
    store.save("fixtures_day.json", fx)


def test_free_settlement():
    """Foot réglé via API-Football (aucun appel /scores, aucun crédit) ; /scores seulement en repli."""
    day = date(2026, 10, 7)
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
        sb.set_now(datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=8))
        # le matin du match : cache du jour avec statut NS (1 requête)
        assert stats.find_fixture(str(day), "Alpha", "Bravo", kickoff=ko) and len(football) == 1
        # le lendemain (autre exécution) : le cache NS d'un jour passé est re-téléchargé, puis tout est réglé gratuitement
        sb.set_now(datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=8))
        phase["s"] = "FT"
        stats._refreshed.clear()
        age_fixture_cache()                                                    # « le lendemain matin » : le relevé NS date de 24 h
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
        assert "♻️ Cache :" in out, out                                                              # compteurs de cache dans --check
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
        store.remove("bootstrap.json")
        assert learn.describe(learn.update([]), []) == ""
    print("amorçage de l'apprentissage : OK")


def fixture_event(i, home, away, ko, stars=False, value=False):
    """Match avec 1N2, corners (9,5), cartons (4,5) et tirs d'un joueur, chez 4 books. `value` : deux books paient 30 % de
    plus sur les « plus de » des marchés détaillés (la cote typique dépasse alors le consensus)."""
    books = []
    for b in range(4):
        j = 1 + 0.01 * ((i + b) % 3 - 1)
        k = 1.30 if value and b in (1, 3) else 1.0
        ms = [{"key": "h2h", "outcomes": [{"name": home, "price": round(2.0 * j, 2)}, {"name": away, "price": round(3.4 / j, 2)},
                                         {"name": "Draw", "price": round(3.3 * j, 2)}]},
              {"key": "totals", "outcomes": [{"name": "Over", "price": round(1.9 * j, 2), "point": 2.5},
                                             {"name": "Under", "price": round(1.95 / j, 2), "point": 2.5}]},
              {"key": "alternate_totals_corners", "outcomes": [{"name": "Over", "price": round(1.9 * j * k, 2), "point": 9.5},
                                                               {"name": "Under", "price": round(1.95 / j, 2), "point": 9.5}]},
              {"key": "alternate_totals_cards", "outcomes": [{"name": "Over", "price": round(1.9 * j * k, 2), "point": 4.5},
                                                             {"name": "Under", "price": round(1.95 / j, 2), "point": 4.5}]}]
        if stars:
            ms.append({"key": "player_shots", "outcomes": [
                {"name": "Over", "description": "Bukayo Saka", "price": round(2.0 * j * k, 2), "point": 1.5},
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
        assert t.startswith("📈 Arsenal (Premier League)\n\n") and "10 derniers matchs (football-data)" in t, t
        assert "Corners : 7,0 pour / 4,0 contre" in t and "Cartons (jaunes + rouges) : 1,5 pour / 2,0 contre" in t, t
        assert "Tirs : 14,0 pour / 9,0 contre" in t and "Fautes" in t and "Buts : 2,0 pour / 0,8 contre" in t, t
        assert "xG (Understat, 6 derniers) : 1,90 pour / 1,00 contre ; domicile 1,90/1,00" in t and "actualisées le 30/09" in t, t
        assert "Corners" in app.handle_command("/stats Chelsea") and "xG" not in app.handle_command("/stats Liverpool")
        assert app.handle_command("/stats").startswith("Exemple") and "Aucune stat pour « Zzz »" in app.handle_command("/stats Zzz")
        # /stats fonctionne sans que la journée ait été analysée ; /analyse non
        assert app.handle_command("/analyse arsenal").startswith("Kelly n'a pas encore analysé")
        events = [fixture_event(1, "Arsenal", "Chelsea", "2026-10-01T18:00:00Z", stars=True),
                  fixture_event(2, "Chelsea", "Liverpool", "2026-10-01T19:00:00Z")]
        picks = stats.enrich(odds.build_picks(events, day), day)
        store.save("today.json", {"day": str(day), "picks": picks})
        a = app.handle_command("/analyse arsenal")
        assert a.startswith("🔬 <b>Arsenal – Chelsea</b>\n<i>18h00 · Premier League</i>"), a
        assert "Corners : <b>11,0</b>  <i>(6,5 + 4,5)</i>" in a and "Cartons : <b>3,8</b>  <i>(1,8 + 2,0)</i>" in a, a
        assert "Buts attendus d'après les occasions (xG) : <b>2,9</b>" in a and "football-data, understat" in a, a
        assert "Meilleurs paris" in a and "Kelly s'attend à 11,0 corners ; la ligne est à 9,5" in a, a
        assert "Bukayo Saka : 2 tirs ou plus" in a and "Il fait 2,9 tirs par match en moyenne ; la ligne est à 1,5" in a, a
        assert a.count("👉 ") >= 6 and a.count("• ") >= 3              # 6 paris + lignes de chiffres
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
    """Combiné spécial : uniquement corners, cartons et stats joueurs, cote 6 à 15, un pari par match, jambes de qualité."""
    day = date(2026, 10, 1)
    pairs = [(chr(65 + 2 * i), chr(66 + 2 * i)) for i in range(12)]
    events = [fixture_event(i, h, a, "2026-10-01T%02d:00:00Z" % (10 + i), stars=i == 0, value=True) for i, (h, a) in enumerate(pairs)]
    with Sandbox():
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        picks = odds.build_picks(events, day)
        assert any(c["market"] == "h2h" for c in picks)
        messages, sent, combos = app.build_day(picks, day)
        assert [t for t, _, _ in C.COMBOS][2] == C.SPECIAL
        msg = next(m for m in messages if m.startswith("<b>🎯 SPÉCIAL"))
        assert "Pas assez" not in msg and "mise conseillée" in msg, msg
        sp = next(c for c in combos if c["tier"] == C.SPECIAL)
        legs = [next(c for c in picks if c["key"] == k) for k in sp["keys"]]
        assert 6.0 <= sp["odds"] <= 15.0 and len(legs) >= 2 and len({c["id"] for c in legs}) == len(legs)
        assert all(CB.soft_market(c) and (c["market"].startswith("player_") or c["market"] in
                   ("alternate_totals_corners", "alternate_totals_cards")) for c in legs), [c["market"] for c in legs]
        assert all(CB.leg_reason(c) for c in legs)                                          # chaque jambe a une raison
        assert sp["stake"] <= 1000 * C.STAKE_CAP["🎯"] + 1e-9
        assert all(c["key"] in {x["key"] for x in sent} for c in legs)                       # suivis comme les autres combinés
        pool = CB.make_special_pool(picks)
        assert pool and [c["key"] for c in CB.pool_for(C.SPECIAL, picks, [])] == [c["key"] for c in pool]
        assert CB.pool_for(C.COMBOS[0][0], picks, ["x"]) == ["x"]
        # priorité de clôture : après le Costaud, avant le Méchant
        rk = track.ranks([{"tier": C.SPECIAL, "keys": ["k1"], "stake": 1, "result": None},
                          {"tier": C.COMBOS[1][0], "keys": ["k2"], "stake": 1, "result": None},
                          {"tier": C.COMBOS[3][0], "keys": ["k3"], "stake": 1, "result": None}])
        assert rk["k2"] < rk["k1"] < rk["k3"], rk
        # sans marché mal pricé : pas de spécial, dit UNE fois en une ligne
        only_h2h = [c for c in picks if c["market"] in ("h2h", "totals")]
        out = app.build_day(only_h2h, day)[0]
        assert not any(m.startswith("<b>🎯 SPÉCIAL") for m in out) and "Pas assez" not in "\n".join(out)
        assert sum(m.count("Journée trop pauvre") for m in out) == 1 and "ni pari ni combiné de qualité" in "\n".join(out)
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
            err1 = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err1):
                msgs = app.run_daily()
            assert order[0] == ("refresh", ["soccer_epl"]) and order[1] == ("settle",), order       # CSV à jour avant le règlement
            src = err1.getvalue()                                  # l'état des sources est technique : logs, pas Telegram
            assert "✅ football-data" in src and "Understat" in src and "2/2 matchs couverts" in src, src
            assert not any(m.startswith("📡") for m in msgs)
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
            assert store.size("bootstrap.json") < 100_000
            assert store.exists("src_footballdata.json") and not any("api-sports" in u for u in urls)
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
            assert any("Kelly —" in m for m in msgs) and track.load()
            assert "pas encore lancée" in sources.report()
            # kill-switch : SOURCES_REFRESH=0 n'appelle rien
            C.SOURCES_REFRESH = False
            calls.clear()
            assert app.refresh_sources() == {} and calls == []
    finally:
        state.clock, http._sleep, sources.refresh_all, bootstrap.ensure, settle.settle_pending = saved
        features.reload()
    print("run_daily + sources (et pannes) : OK")


# ---------- catalogue mondial, tour du monde, pronostics sans cote ----------

WDAY = "2026-10-08"


def wevs(prefix, pairs, hours, day=WDAY):
    return [(f"{prefix}{i}", h, a, ko_at(day, hours[i % len(hours)], 15 * (i % 3))) for i, (h, a) in enumerate(pairs)]


def break_day_leagues(day=WDAY):
    """Jeudi de trêve internationale : aucune des grandes affiches, 13 ligues en jeu dans le monde."""
    n = lambda p, k: [(f"{p}T{i}", f"{p}T{i + 50}") for i in range(k)]            # noqa: E731
    return {
        "soccer_uefa_nations_league": wevs("nl", [("France", "Italie"), ("Espagne", "Allemagne"), ("Pays-Bas", "Belgique"),
                                                  ("Portugal", "Croatie")], [18, 20], day),
        "soccer_international_friendlies": wevs("fr", [("Côte d'Ivoire", "Sénégal"), ("Maroc", "Égypte"), ("Brésil", "Mexique")], [16, 19], day),
        "soccer_fifa_world_cup_qualifiers_africa": wevs("qa", [("Ghana", "Nigeria"), ("Mali", "Tunisie")], [15], day),
        "soccer_efl_champ": wevs("ch", n("CH", 5), [19], day),
        "soccer_netherlands_eredivisie": wevs("ere", [("Ajax", "PSV"), ("Feyenoord", "AZ"), ("Utrecht", "Twente")], [18, 20], day),
        "soccer_england_league1": wevs("l1", n("L1", 12), [15], day),
        "soccer_germany_bundesliga2": wevs("b2", n("B2", 9), [13, 18], day),
        "soccer_sweden_allsvenskan": wevs("sw", [("Malmo FF", "AIK"), ("Hammarby", "Djurgarden"), ("IFK Goteborg", "Elfsborg")], [17], day),
        "soccer_usa_mls": wevs("mls", [("Inter Miami", "LA Galaxy"), ("Seattle Sounders", "Portland Timbers"), ("NYCFC", "Orlando City")], [23], day),
        "soccer_brazil_campeonato": wevs("br", [("Flamengo", "Palmeiras"), ("Corinthians", "Santos")], [22], day),
        "soccer_argentina_primera_division": wevs("ar", [("River Plate", "Boca Juniors")], [21], day),
        "soccer_japan_j_league": wevs("jp", [("Kawasaki Frontale", "Vissel Kobe"), ("Urawa", "Gamba Osaka")], [9], day),
        "soccer_saudi_arabia_pro_league": wevs("sa", [("Al Hilal", "Al Nassr")], [17], day),
    }


JUNK_SPORTS = ["soccer_xyz_unknown", "soccer_uefa_champs_league_women", "tennis_atp_wimbledon", "soccer_epl", "soccer_spain_la_liga"]


def world_profiles(leagues, divs=None, corners=(7.0, 5.0)):
    """Profils football-data pour les ligues qui ont un modèle (Championship, Eredivisie, League One, 2. Bundesliga,
    Allsvenskan, MLS) : toutes les équipes jouent ~12 corners attendus."""
    divs = divs or {"soccer_efl_champ": "E1", "soccer_netherlands_eredivisie": "N1", "soccer_england_league1": "E2",
                    "soccer_germany_bundesliga2": "D2", "soccer_sweden_allsvenskan": "SWE", "soccer_usa_mls": "USA"}
    v = {"n": 10, "corners_for": corners[0], "corners_against": corners[1], "cards_for": 2.2, "cards_against": 2.0,
         "goals_for": 1.5, "goals_against": 1.1}
    teams = {}
    for key, div in divs.items():
        for e in leagues.get(key, []):
            for t in e[1:3]:
                teams[t] = {"div": div, "last": "2026-10-04", "all": v, "home": v, "away": v}
    store.save("src_footballdata.json", {"updated": "2026-10-08T07:00", "referees": {}, "leagues": {}, "teams": teams})
    features.reload()


def test_catalogue_and_priority():
    """Catalogue par région ; select_sports ignore les clés inconnues, les versions féminines et les outrights ; priorité :
    région, grandes affiches, ligues majeures, nombre de matchs ; l'international passe en tête un jour de trêve."""
    from kelly import catalogue
    keys = C.LEAGUE_KEYS
    assert len(keys) == len(set(keys)) and all(k.startswith("soccer_") for k in keys) and len(keys) >= 50
    must = {"europe": ["soccer_epl", "soccer_uefa_champs_league", "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league",
                       "soccer_portugal_primeira_liga", "soccer_netherlands_eredivisie", "soccer_belgium_first_div",
                       "soccer_turkey_super_league", "soccer_spl", "soccer_efl_champ", "soccer_germany_bundesliga2"],
            "ameriques": ["soccer_brazil_campeonato", "soccer_brazil_serie_b", "soccer_argentina_primera_division", "soccer_usa_mls",
                          "soccer_mexico_ligamx", "soccer_chile_campeonato", "soccer_conmebol_copa_libertadores",
                          "soccer_conmebol_copa_sudamericana"],
            "asie": ["soccer_japan_j_league", "soccer_korea_kleague1", "soccer_china_superleague", "soccer_saudi_arabia_pro_league",
                     "soccer_australia_aleague"],
            "monde": ["soccer_fifa_world_cup_qualifiers_europe", "soccer_uefa_nations_league"]}
    for r, ks in must.items():
        assert all(catalogue.region(k) == r for k in ks), r
    assert catalogue.region("soccer_fifa_world_cup_qualifiers_africa") == "monde"            # préfixe du catalogue
    assert catalogue.name("soccer_fifa_world_cup_qualifiers_africa") == "Qualif. Coupe du monde (africa)"
    assert catalogue.name("soccer_epl") == "Premier League" and catalogue.region("soccer_inconnu") == "autre"
    assert C.SPORTS == keys and C.REGIONS[0][0] == "europe" and [r for r, _ in C.REGIONS][:3] == ["europe", "ameriques", "asie"]
    # la liste réelle de /sports : inconnues, féminines, outrights et autres sports ignorés ; ordre du catalogue
    live = [{"key": k} for k in ("soccer_xyz_unknown", "soccer_uefa_champs_league_women", "tennis_atp", "soccer_usa_mls", "soccer_epl",
                                 "soccer_fifa_world_cup_qualifiers_africa", "soccer_fifa_world_cup_qualifiers_europe",
                                 "soccer_uefa_nations_league", "soccer_fifa_world_cup_womens")] + [
        {"key": "soccer_epl_winner", "has_outrights": True}]
    with Sandbox():
        C.SPORTS = list(C.LEAGUE_KEYS)
        got = [s["key"] for s in odds.select_sports(live, limit=False)]
        assert got == ["soccer_epl", "soccer_usa_mls", "soccer_uefa_nations_league", "soccer_fifa_world_cup_qualifiers_europe",
                       "soccer_fifa_world_cup_qualifiers_africa"], got
        assert len(odds.select_sports(live)) == 5                                  # MAX_SPORTS (6) n'est pas atteint
        C.MAX_SPORTS = 3
        assert len(odds.select_sports(live)) == 3
    # priorité : jour normal -> Europe d'abord, grandes affiches avant le reste, puis Amériques, Asie, international
    normal = {"soccer_epl": 3, "soccer_england_league1": 12, "soccer_efl_champ": 11, "soccer_usa_mls": 9, "soccer_uefa_nations_league": 4,
              "soccer_japan_j_league": 6, "soccer_brazil_campeonato": 2}
    assert not catalogue.is_break_day(normal)
    assert catalogue.prioritise(normal, C.SPORTS) == ["soccer_epl", "soccer_efl_champ", "soccer_england_league1", "soccer_usa_mls",
                                                     "soccer_brazil_campeonato", "soccer_japan_j_league", "soccer_uefa_nations_league"]
    # trêve internationale : aucune grande affiche -> les sélections passent en tête (puis nombre de matchs)
    brk = {k: v for k, v in normal.items() if k != "soccer_epl"} | {"soccer_international_friendlies": 2}
    assert catalogue.is_break_day(brk) and catalogue.is_break_day({})
    assert catalogue.prioritise(brk, C.SPORTS)[:3] == ["soccer_uefa_nations_league", "soccer_international_friendlies", "soccer_efl_champ"]
    assert catalogue.prioritise({"soccer_epl": 0, "soccer_spl": 0}) == []                 # une ligue sans match n'est jamais payée
    # même région et même rang : le plus de matchs d'abord, puis l'ordre du catalogue
    assert catalogue.prioritise({"soccer_france_ligue_one": 4, "soccer_italy_serie_a": 4, "soccer_epl": 2}, C.SPORTS) == [
        "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_epl"]
    # le relevé gratuit ne regarde que les ligues suivies ET en saison, et ne garde que celles qui jouent
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        lg = break_day_leagues()
        fake = sb.serve(FakeOdds(500, lg, sports=list(lg) + JUNK_SPORTS))
        scan = odds.scan_today(datetime(2026, 10, 8).date(), datetime(2026, 10, 8, 8, tzinfo=UTC))
        assert set(scan["leagues"]) == set(lg) and scan["error"] is None and scan["complete"]
        assert sum(len(v) for v in scan["leagues"].values()) == 50 and fake.paid() == [] and fake.spent() == 0
        asked = {c[0].split("/")[2] for c in fake.log if c[0].endswith("/events")}
        assert asked == set(lg) | {"soccer_epl", "soccer_spain_la_liga"}, asked          # jamais les clés inconnues ni féminines
        assert "soccer_epl" not in scan["leagues"] and "soccer_epl" in scan["sports"] and "soccer_xyz_unknown" not in scan["sports"]
        # délai maximal : le relevé s'arrête (priorité d'abord) au lieu de dépasser le temps du workflow
        # (le cache gratuit est vidé : on veut voir /sports repartir)
        shutil.rmtree(cache.odds_dir(), ignore_errors=True)
        n = len(fake.log)
        part = odds.scan_today(datetime(2026, 10, 8).date(), seconds=-1)
        assert not part["complete"] and part["leagues"] == {} and len(fake.log) == n + 1                 # /sports seulement
        # /sports en panne : relevé vide avec l'erreur, jamais d'exception
        sb.serve(lambda url, **kw: Resp({"message": "boom", "error_code": "X"}, 500))
        shutil.rmtree(cache.odds_dir(), ignore_errors=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            bad = odds.scan_today(datetime(2026, 10, 8).date())
        assert bad["error"] and bad["leagues"] == {} and "test-odds-key" not in bad["error"]
    print("catalogue et priorités : OK")


def test_budget_with_many_leagues():
    """13 ligues jouent un jour de trêve : le faux The Odds API facture comme le vrai ; le matin ne dépasse jamais sa part,
    ne paie que les plus prioritaires (sélections d'abord), et garde le reste pour les corners du premier match qui a un modèle."""
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))
        lg = break_day_leagues()
        world_profiles(lg)
        fake = sb.serve(FakeOdds(500, lg, sports=list(lg) + JUNK_SPORTS))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scan = odds.scan_today(datetime(2026, 10, 8).date(), datetime(2026, 10, 8, 8, tzinfo=UTC))
            events = odds.fetch_events(datetime(2026, 10, 8).date(), after=datetime(2026, 10, 8, 8, tzinfo=UTC), scan=scan)
        share = budget.share("daily")
        assert share == 12 and fake.spent() == budget.spent("daily") <= share, (fake.spent(), share)
        bulk = [c for c in fake.paid() if "/events/" not in c[0]]
        assert [c[0].split("/")[2] for c in bulk] == ["soccer_uefa_nations_league", "soccer_international_friendlies",
                                                       "soccer_fifa_world_cup_qualifiers_africa", "soccer_efl_champ"], bulk
        assert all(c[1] == "h2h,totals" for c in bulk) and scan["paid"] == [c[0].split("/")[2] for c in bulk]
        assert len(scan["paid"]) <= C.MAX_SPORTS and len({e["sport_key"] for e in events}) == 4
        det = [c for c in fake.paid() if "/events/" in c[0]]
        assert det and all(c[0].startswith("/sports/soccer_efl_champ/") and c[1] == "alternate_totals_corners" for c in det), det
        assert fake.spent() == 4 * 2 + len(det) == 12                                  # 8 en vedettes + 4 corners : toute la part du matin
        assert not any(k in c[0] for c in fake.log for k in ("soccer_xyz", "women", "tennis"))
        assert not any(c[2] for c in fake.log if c[0].endswith("/events") or c[0] == "/sports")      # le relevé reste gratuit
        assert "ligues avec matchs : soccer_uefa_nations_league, soccer_international_friendlies" in err.getvalue()
        # jour normal : la Premier League joue -> l'Europe passe avant l'international ; sans profil, tout va aux vedettes
        for f in os.listdir(store.DATA_DIR):
            os.remove(os.path.join(store.DATA_DIR, f))
        odds.CREDITS.update(remaining=None, used=None)
        features.reload()
        normal = dict(lg) | {"soccer_epl": wevs("epl", [("Alpha", "Bravo"), ("Cobra", "Delta")], [15])}
        fake = sb.serve(FakeOdds(500, normal, sports=list(normal)))
        with contextlib.redirect_stderr(io.StringIO()):
            scan = odds.scan_today(datetime(2026, 10, 8).date())
            odds.fetch_events(datetime(2026, 10, 8).date(), scan=scan)
        assert scan["paid"] == ["soccer_epl", "soccer_efl_champ", "soccer_netherlands_eredivisie", "soccer_england_league1",
                                "soccer_germany_bundesliga2", "soccer_sweden_allsvenskan"], scan["paid"]   # Europe d'abord, MAX_SPORTS = 6
        assert fake.spent() <= 12 and all(c[1] == "h2h,totals" for c in fake.paid())             # aucun profil : pas de corners achetés
        # peu de crédits : au moins une ligue si le budget le permet, jamais une erreur
        for f in os.listdir(store.DATA_DIR):
            os.remove(os.path.join(store.DATA_DIR, f))
        odds.CREDITS.update(remaining=None, used=None)
        fake = sb.serve(FakeOdds(33, normal, sports=list(normal)))
        sb.set_now(datetime(2026, 10, 31, 8, tzinfo=UTC))
        with contextlib.redirect_stderr(io.StringIO()):
            fake.leagues = {k: [(i, h, a, t.replace("2026-10-08", "2026-10-31")) for i, h, a, t in v] for k, v in normal.items()}
            odds.fetch_events(datetime(2026, 10, 31).date())
        assert fake.remaining == 31 and len(fake.paid()) == 1                       # 33 - plancher 30 : une seule ligue (2 crédits)
    print("budget avec beaucoup de ligues : OK")


def test_fair_pronostics():
    """Pronostics sans cote : proba du modèle, cote juste = 1/proba, vocabulaire 1xBet, aucun pronostic sans données."""
    from kelly import fair, prematch
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        ev = lambda i, h, a, key="soccer_efl_champ": {"id": i, "sport_key": key, "home_team": h, "away_team": a,   # noqa: E731
                                                       "commence_time": f"{WDAY}T19:00:00Z"}
        scan = {"day": WDAY, "leagues": {"soccer_efl_champ": [ev("m1", "Alpha", "Bravo"), ev("m2", "Inconnu A", "Inconnu B")],
                                         "soccer_uefa_nations_league": [ev("m3", "France", "Italie", "soccer_uefa_nations_league")]}}
        put_team_profiles({"E1": ["Alpha", "Bravo"]})
        d = store.load("src_footballdata.json", None)
        for t in d["teams"].values():                                      # 12 corners attendus, 2.6 buts, 4.4 cartons
            for view in ("all", "home", "away"):
                t[view].update(corners_for=7.0, corners_against=5.0, goals_for=1.5, goals_against=1.1)
        store.save("src_footballdata.json", d)
        features.reload()
        pronos = fair.make(scan, [], datetime(2026, 10, 8).date())
        assert pronos and {c["id"] for c in pronos} == {"m1"}, [c["event"] for c in pronos]   # pas de profil / sélection : rien d'inventé
        assert 1 <= len(pronos) <= 2 and pronos == sorted(pronos, key=lambda c: -c["score"])
        top = pronos[0]
        exp_p = 0.5 + (prematch.over_p("Over", prematch.poisson_over(12.0, 10.5)) - 0.5) * C.FAIR_SHRINK
        assert top["market"] == "alternate_totals_corners" and (top["name"], top["point"]) == ("Over", 10.5), top
        assert abs(top["prob"] - exp_p) < 1e-3 and top["fair"] == round(1 / top["prob"], 2)
        assert top["min_odds"] == round(1 / top["prob"] * (1 + C.FAIR_MARGIN), 2) > top["fair"]
        assert top["pick"] == "Plus de 10,5 corners" and top["league"] == "Championship" and top["start"] == "19:00"
        assert top["key"] == "m1|alternate_totals_corners||10.5|Over" and top["why"].startswith("stats réelles : 12.0 corners attendus")
        for c in pronos:                                                   # proba utile : ni hasard ni évidence
            assert C.FAIR_P_RANGE[0] <= c["prob"] <= C.FAIR_P_RANGE[1] and c["min_odds"] >= c["fair"] >= 1 / C.FAIR_P_RANGE[1] - 0.01
        text = fmt.pronos(pronos)
        assert text.startswith("<b>🎲 Pronostics Kelly sans cote</b>"), text
        assert "<b>Alpha – Bravo</b>" in text and "👉 Plus de 10,5 corners — mini <b>" in text, text
        assert f'<b>{fmt.num(top["min_odds"])}</b>' in text and "cote juste" not in text
        # toutes les familles de marchés : buts, les 2 marquent, double chance (libellés familiers), corners, cartons
        f = {"n_home": 10, "n_away": 10, "exp_corners": 10.2, "exp_cards": 4.9, "exp_goals_home": 1.6, "exp_goals_away": 1.3}
        opts = fair.options(f, "Alpha", "Bravo")
        assert {o[0] for o in opts} == {"totals", "btts", "double_chance", "alternate_totals_corners", "alternate_totals_cards"}
        picks = {c["market"]: c for c in (fair.make_pick(ev("m9", "Alpha", "Bravo"), *b, "football-data") for b in
                                          fair.best_per_family(opts, set(fair.FAMILIES)))}
        assert set(picks) == set(fair.FAMILIES), picks.keys()
        labels = {c["pick"] for c in picks.values()}
        assert any(x.startswith(("Plus de", "Moins de")) and x.endswith(" buts") for x in labels), labels
        assert any(x.startswith("Les deux équipes marquent : ") for x in labels) and any(x.startswith("Double chance : ") for x in labels)
        assert any(x.endswith(" corners") for x in labels) and any(x.startswith("Total cartons : ") for x in labels), labels
        assert "Double chance : Alpha ou Nul" in {fair.make_pick(ev("m9", "Alpha", "Bravo"), "double_chance", "Alpha or Draw", None, 0.7,
                                                                 "t", "x")["pick"]}
        # sous 5 matchs récents ou hors bornes : aucune option ; marchés déjà cotés chez les books : pas refaits
        assert fair.options(dict(f, n_home=3), "A", "B") == [] and fair.options(dict(f, exp_corners=40, exp_cards=None), "A", "B")
        paid = [{"id": "m1", "market": "alternate_totals_corners"}, {"id": "m1", "market": "totals"}]
        left = fair.make(scan, paid, datetime(2026, 10, 8).date())
        assert left and all(c["market"] != "alternate_totals_corners" and c["market"] != "totals" for c in left), left
        assert all(c["market"] in ("alternate_totals_corners", "alternate_totals_cards") for c in left)   # match payé : corners/cartons seulement
        # repli API-Football : au plus FAIR_API_EVENTS matchs sans profil, jamais sous la réserve de requêtes
        calls = []
        real = (stats.expected_totals, stats.left_today)
        stats.expected_totals = lambda day, h, a, ko=None: calls.append(h) or {"corners": 11.2, "cards": 4.4, "n": 6}
        stats.left_today = lambda: 80
        try:
            many = {"soccer_efl_champ": [ev(f"u{i}", f"Sans profil {i}", f"Autre {i}") for i in range(5)]}
            api = fair.make({"leagues": many}, [], datetime(2026, 10, 8).date())
            assert len(calls) == C.FAIR_API_EVENTS == 2 and {c["src"] for c in api} == {"api"}, calls
            assert all(c["market"] in ("alternate_totals_corners", "alternate_totals_cards") for c in api)    # API-Football : pas de buts
            calls.clear()
            stats.left_today = lambda: C.FAIR_API_RESERVE                  # quota presque épuisé : on le garde pour le règlement
            assert fair.make({"leagues": many}, [], datetime(2026, 10, 8).date()) == [] and calls == []
        finally:
            stats.expected_totals, stats.left_today = real
        # suivi : au plus FAIR_TRACK par jour, sans doublon, 12 jours d'historique ; bilan de calibration
        many_p = [dict(top, key=f"k{i}", id=f"id{i}") for i in range(30)]
        assert fair.record(many_p, WDAY) == C.FAIR_TRACK and fair.record(many_p, WDAY) == 0
        rows = fair.load()
        assert len(rows) == C.FAIR_TRACK and all(r["result"] is None and "score" not in r for r in rows)
        assert store.size("fair.json") < 5_000 and C.FAIR_TRACK * C.FAIR_KEEP_DAYS * 300 < 40_000      # data/ reste petit
        fair.record([dict(top, key="old")], "2026-11-30")
        assert [r["key"] for r in fair.load()] == ["old"]                  # les lignes de plus de 10 jours disparaissent
        rows = [{"result": "win", "prob": 0.6, "date": "2026-11-29"}, {"result": "loss", "prob": 0.7, "date": "2026-11-29"},
                {"result": "void", "prob": 0.7, "date": "2026-11-29"}]
        assert fair.summary(rows) == "🎲 Pronostics sans cote : 1/2 gagnés (50%) pour une proba annoncée moyenne de 65%"
        assert fair.summary([]) == "" and fair.summary(rows, days=1, today="2026-12-30") == ""
    print("pronostics sans cote : OK")


def run_morning(sb, fake, day=WDAY):
    """run_daily avec Telegram capturé : renvoie les messages envoyés."""
    sent = []
    real = telegram.send
    telegram.send = lambda text, chat_id=None: sent.append(text) or True
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            app.run_daily()
    finally:
        telegram.send = real
    assert all(m.strip() for m in sent), sent
    return sent


def test_morning_always_useful():
    """run_daily envoie toujours quelque chose d'utile : jour de trêve (sélections payées, le reste en pronostics sans cote),
    jour sans crédit (tour du monde + pronostics, pas de faux combinés), aucun match nulle part (prochains jours), panne API."""
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        lg = break_day_leagues()
        # --- 1) trêve internationale, budget normal
        world_profiles(lg)
        fake = sb.serve(FakeOdds(500, lg, sports=list(lg) + JUNK_SPORTS))
        sent = run_morning(sb, fake)
        txt = "\n".join(sent)
        world = next(m for m in sent if m.startswith("<b>🌍 Tour du monde"))
        assert "50 matchs aujourd'hui dans 13 ligues" in world and "Trêve internationale" in world, world
        order = [world.index(x) for x in ("🌐 International", "🇪🇺 Europe", "🌎 Amériques", "🌏 Asie / Océanie")]
        assert order == sorted(order), world                                   # sélections d'abord un jour de trêve
        assert "💳 Ligue des nations UEFA\n" in world and "⚽ MLS\n" in world and "18h00  France – Italie" in world, world
        assert "Cotes réelles achetées : " in world and "crédit" not in world and "🎲" in world
        assert "PARI DU JOUR" in txt and "SÛR" in txt and "🎲 Pronostics Kelly sans cote" in txt
        prono = next(m for m in sent if m.startswith("<b>🎲 Pronostics Kelly sans cote"))
        assert "— mini <b>" in prono and "chez 1xBet" in prono and "cote juste" not in prono
        assert "Alpha" not in prono and "Ligue des nations" not in prono             # les sélections n'ont pas de profil de club
        paid_leagues = {c[0].split("/")[2] for c in fake.paid()}
        assert paid_leagues == {"soccer_uefa_nations_league", "soccer_international_friendlies",
                                "soccer_fifa_world_cup_qualifiers_africa", "soccer_efl_champ"}, paid_leagues
        assert fake.spent() <= 12 and all(m.strip() for m in sent)
        sent_rows = [r for r in track.load() if r["sent"]]
        assert sent_rows and all(r["sport_key"] in paid_leagues for r in sent_rows)
        today = store.load("today.json", {})
        assert today["pronos"] and all("score" not in c for c in today["pronos"])
        data = store.load("monde.json", {})
        assert data["day"] == WDAY and data["brk"] == 1 and data["paid"] == [k for k in data["leagues"] if k in paid_leagues] \
            and sum(len(v) for v in data["leagues"].values()) == 50 and store.size("monde.json") < 20_000
        fair_rows = store.load("fair.json", [])
        shown_rows = [r for r in fair_rows if not r.get("x")]                     # les montrés : un pronostic viable par match
        assert shown_rows and len({r["id"] for r in shown_rows}) == len(shown_rows) and all(r["result"] is None for r in fair_rows)
        assert all(r["market"] in fair.FREE_MARKETS for r in fair_rows if r.get("x")) and all("p0" in r for r in fair_rows)
        # --- commandes lues dans le cache du matin (aucun appel réseau)
        sb.serve(lambda url, **kw: (_ for _ in ()).throw(AssertionError(f"requête inattendue : {url}")))
        m = app.handle_command("/monde")
        assert m.startswith("🌍 MATCHS DU jeu. 08/10 (50 matchs") and "🌐 International" in m and "💳 Ligue des nations UEFA (4)" in m
        assert "• Brasileirão (2) : 22:00 Flamengo - Palmeiras" in m and "• League One (12)" in m and "relevé de 08:00 UTC" in m
        assert m.index("🌐 International") < m.index("🇪🇺 Europe") < m.index("🌎 Amériques") < m.index("🌏 Asie")
        asia = app.handle_command("/monde asie")
        assert "J League" in asia and "Saudi Pro League" in asia and "MLS" not in asia and "Premier" not in asia
        assert "Aucun match dans cette région" in app.handle_command("/monde afrique")
        assert "MLS" in app.handle_command("/monde Amériques") and "Ajax" not in app.handle_command("/monde amériques")
        lig = app.handle_command("/ligues")
        assert lig.startswith(f"📚 LIGUES SUIVIES ({len(C.LEAGUE_KEYS)} dans le catalogue, au plus 6 payées par jour)")
        assert "💳 Ligue des nations UEFA (4)" in lig and "💳 Championship (5)" in lig and "⚽ MLS (3)" in lig
        assert "💤 Serie A" in lig and "· Premier League" in lig and "rédit" not in lig
        for line in ("🇪🇺 Europe", "🌎 Amériques", "🌏 Asie / Océanie", "🌍 Afrique", "🌐 International"):
            assert line in lig
        aide = app.handle_command("/aide")
        assert "/monde" in aide and "/ligues" in aide
        fre = app.handle_command("/match Ajax")                              # match payé ? non : pronostic sans cote ou liste
        assert "Ajax" in fre or "Aucun match" in fre
        fr1 = app.handle_command("/match L1T3")
        assert "— mini <b>" in fr1 and "cote juste" not in fr1, fr1
        # le cache d'un autre jour n'est pas servi
        sb.set_now(datetime(2026, 10, 9, 9, tzinfo=UTC))
        assert app.handle_command("/monde").startswith("Pas encore de relevé aujourd'hui")
        assert "pas encore de relevé aujourd'hui" in app.handle_command("/ligues") and "   💤 Serie A" not in app.handle_command("/ligues")
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))

    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        # --- 2) aucune ligue payée (crédits sous le plancher) : tour du monde + pronostics, pas de faux combinés
        week = {k: v for k, v in break_day_leagues().items() if k in ("soccer_efl_champ", "soccer_england_league1", "soccer_usa_mls",
                                                                      "soccer_brazil_campeonato", "soccer_japan_j_league")}
        world_profiles(week)
        fake = sb.serve(FakeOdds(25, week, sports=list(week)))
        sent = run_morning(sb, fake)
        txt = "\n".join(sent)
        assert fake.paid() == [] and not track.load() and fake.remaining == 25
        assert "🌍 Tour du monde" in txt and "Pas de cote achetée aujourd'hui (budget de cotes du jour épuisé)" in txt, txt
        assert "🎲 Pronostics Kelly sans cote" in txt and "— mini <b>" in txt and "⚽ Brasileirão\n" in txt
        assert "Pas assez de matchs" not in txt and "COMBINÉS DU" not in txt and "PARI DU JOUR" not in txt and "SÛR" not in txt
        assert "Cotes réelles achetées" not in next(m for m in sent if m.startswith("<b>🌍 Tour du monde"))          # aucune ligue payée
        assert not any(r["sent"] for r in track.load())                        # rien n'est suivi comme pari envoyé (pas de cote, pas de CLV)
        assert store.load("fair.json", []) and store.load("monde.json", {})["paid"] == []
        assert "Pronostics Kelly sans cote" in app.handle_command("/top") and app.handle_command("/combi 30").startswith("Pas de cotes réelles")

        # --- 3) aucun match nulle part aujourd'hui : on le dit en une ligne avec les prochains jours
        wipe()
        odds.CREDITS.update(remaining=None, used=None)
        tomorrow = {"soccer_france_ligue_one": wevs("t", [("A1", "B1"), ("C1", "D1")], [20], "2026-10-09"),
                    "soccer_usa_mls": wevs("u", [("E1", "F1")], [23], "2026-10-10")}
        fake = sb.serve(FakeOdds(500, tomorrow, sports=list(tomorrow)))
        sent = run_morning(sb, fake)
        w = next(m for m in sent if m.startswith("<b>🌍 Tour du monde"))
        assert "😴 Aucun match aujourd'hui dans les 2 ligues en saison" in w, w
        assert "Prochains matchs : ven. 09/10 : 2 match(s) (Ligue 1) ; sam. 10/10 : 1 match(s) (MLS)" in w, w
        assert fake.paid() == [] and len(sent) == 2 and sent[-1].startswith("<i>Probabilités estimées"), sent    # monde + avertissement
        assert "Aucun match aujourd'hui." in app.handle_command("/monde") and "Prochains matchs" in app.handle_command("/monde")
        # --- 4) rien non plus les jours suivants
        shutil.rmtree(cache.odds_dir(), ignore_errors=True)
        fake = sb.serve(FakeOdds(500, {}, sports=["soccer_epl"]))
        sent = run_morning(sb, fake)
        assert "Aucun match annoncé dans les 7 prochains jours" in next(m for m in sent if m.startswith("<b>🌍 Tour du monde"))
        # --- 5) The Odds API en panne : message clair, pas d'exception, pas de combiné vide
        shutil.rmtree(cache.odds_dir(), ignore_errors=True)
        sb.serve(lambda url, **kw: Resp({"message": "service indisponible", "error_code": "DOWN"}, 503))
        sent = run_morning(sb, None)
        w = next(m for m in sent if m.startswith("<b>🌍 Tour du monde"))
        assert "Impossible de lister les matchs du jour" in w and "service indisponible" in w and "COMBINÉS" not in "\n".join(sent)
    print("message du matin toujours utile : OK")


def test_africa_and_fair_settlement():
    """Matchs africains d'API-Football dans le tour du monde (une requête, sans cotes) ; pronostics sans cote réglés
    gratuitement le lendemain (football-data) et résumés dans le message du matin et /bilan."""
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))
        lg = {"soccer_usa_mls": wevs("mls", [("Inter Miami", "LA Galaxy")], [23])}
        football = []
        ts = int(datetime(2026, 10, 8, 15, tzinfo=UTC).timestamp())
        api = lambda lgn, ctry, i, h, a: {"fixture": {"id": i, "timestamp": ts, "status": {"short": "NS"}},   # noqa: E731
                                          "league": {"name": lgn, "country": ctry}, "goals": {"home": None, "away": None},
                                          "teams": {"home": {"id": i * 10, "name": h}, "away": {"id": i * 10 + 1, "name": a}}}
        resp = [api("Ligue 1", "Ivory-Coast", 1, "ASEC Mimosas", "Africa Sports"), api("CAF Champions League", "World", 2, "Al Ahly", "Wydad"),
                api("Ligue 1", "France", 3, "Lyon", "Nice"), api("Premier League Women", "Egypt", 4, "X", "Y")]
        odds_fake = FakeOdds(25, lg, sports=list(lg))

        def net(url, **kw):
            if "api-sports.io" in url:
                football.append(kw.get("params"))
                return Resp({"errors": [], "response": resp})
            return odds_fake(url, **kw)
        sb.serve(net)
        os.environ["STATS_API_KEY"] = "test-stats-key"
        stats._calls, stats._halted = 0, False
        stats._refreshed.clear()
        sent = run_morning(sb, odds_fake)
        w = next(m for m in sent if m.startswith("<b>🌍 Tour du monde"))
        assert "🌍 Afrique <i>(sans cotes)</i></b> · 2 matchs" in w, w
        assert "⚽ Ligue 1 (Ivory-Coast)\n      15h00  ASEC Mimosas – Africa Sports" in w and "⚽ CAF Champions League\n      15h00  Al Ahly – Wydad" in w, w
        assert "Lyon" not in w and "Women" not in w
        assert sorted(p["date"] for p in football if p.get("date")) == ["2026-10-07", "2026-10-08", "2026-10-09"]   # hier, aujourd'hui et demain (matchs avant 08:00), une fois chacun
        cached = store.load("fixtures_day.json", {})[WDAY]
        assert sum(1 for f in cached if f.get("lg")) == 2 and len(cached) == 4         # seul l'Afrique garde sa ligue : cache léger
        assert "Al Ahly" in app.handle_command("/monde") and "Al Ahly" in app.handle_command("/monde afrique")
        assert "Al Ahly" not in app.handle_command("/monde europe")
        os.environ.pop("STATS_API_KEY")

    # règlement gratuit des pronostics (CSV football-data) et résumé du matin
    day = datetime(2026, 9, 30, tzinfo=UTC)
    now = datetime(2026, 10, 2, 8, tzinfo=UTC)
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(now)
        sb.serve(FakeOdds(500, {}))
        played = day.strftime("%d/%m/%Y")
        csv = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC,HY,AY,HR,AR\n"
               f"E0,{played},Man City,Arsenal,2,1,7,5,2,3,0,1\n")                               # 12 corners, 6 cartons, 3 buts
        fd_cache(csv)

        def prono(key, market, name, point, p=0.64, home="Manchester City", away="Arsenal", ko="2026-09-30T15:00:00Z"):
            return {"key": key, "id": "m1", "sport_key": "soccer_epl", "event": f"{home} - {away}", "home": home, "away": away,
                    "commence": ko, "market": market, "name": name, "point": point, "who": None, "group": "soccer",
                    "pick": key, "prob": p, "fair": round(1 / p, 2), "league": "Premier League", "date": "2026-09-30", "result": None}
        rows = [prono("c", "alternate_totals_corners", "Over", 10.5), prono("c2", "alternate_totals_corners", "Under", 10.5),
                prono("k", "alternate_totals_cards", "Over", 5.5), prono("g", "totals", "Over", 2.5),
                prono("g2", "totals", "Under", 2.5), prono("b", "btts", "Yes", None), prono("d", "double_chance", "Manchester City or Draw", None),
                prono("d2", "double_chance", "Draw or Arsenal", None),
                prono("nf", "totals", "Over", 2.5, home="Chelsea", away="Liverpool", ko="2026-10-01T15:00:00Z"),      # absent du CSV
                prono("old", "totals", "Over", 2.5, home="Chelsea", away="Liverpool", ko="2026-09-20T15:00:00Z")]
        store.save("fair.json", rows)
        n = len(sb.serve(FakeOdds(500, {})).log)
        msgs = app.fair_report(now)                                                     # règle puis résume
        res = {r["key"]: r["result"] for r in store.load("fair.json", [])}
        assert res == {"c": "win", "c2": "loss", "k": "win", "g": "win", "g2": "loss", "b": "win", "d": "win", "d2": "loss",
                       "nf": None, "old": "void"}, res
        assert len(odds.requests.get.log) == n                                          # aucun appel The Odds API, aucun crédit
        # message du matin : résultats + calibration, une seule fois
        assert len(msgs) == 1 and msgs[0].startswith("<b>🎲 Pronostics sans cote — résultats</b>"), msgs
        assert "<b>Manchester City – Arsenal</b>\n✅ Plus de 10,5 corners (Kelly annonçait 64 % de chances)" in msgs[0] and "❌" in msgs[0]
        assert "Pronostics sans cote : 5 gagnés sur 8 (62 %), Kelly en annonçait 64 % en moyenne." in msgs[0], msgs[0]
        assert app.fair_report(now) == []
        bil = settle.bilan({"picks": [], "combos": []})
        assert "Aucun pari réglé" in bil                                                   # sans paris réglés, le bilan reste inchangé
        track.save([dict(mk_row("x", "e9", "h2h", True, "2026-09-30T15:00:00Z"), result="win", prob=0.5, date="2026-09-30", closed_at="x",
                         clv=0.01)])
        assert "Pronostics sans cote : 5 gagnés sur 8 (62 %)" in settle.bilan({"picks": [], "combos": []})
    print("Afrique et règlement des pronostics : OK")


# ---------- cote typique, désaccord adaptatif, plancher de qualité, combinés à cote fixe ----------

def book_event(i, home, away, ko, prices, who_title=None, market="h2h", name=None):
    """Match dont le 1N2 « domicile » est coté `prices` par autant de books (nul et extérieur calés pour que la proba de
    consensus reste ~ celle des prix)."""
    books = []
    for b, pr in enumerate(prices):
        books.append({"key": f"b{b}", "title": (who_title or {}).get(b, f"Book{b}"), "markets": [
            {"key": "h2h", "outcomes": [{"name": home, "price": pr}, {"name": "Draw", "price": 3.6}, {"name": away, "price": 2.9}]}]})
    return {"id": f"bk{i}", "sport_key": "soccer_epl", "sport_title": "EPL", "home_team": home, "away_team": away,
            "commence_time": ko, "bookmakers": books}


def test_typical_price():
    """Cote typique (75e centile des books) au lieu du maximum : un prix aberrant ne gonfle ni la cote ni la valeur."""
    day = date(2026, 10, 1)
    base = [4.20, 4.25, 4.30, 4.30, 4.35, 4.35, 4.40, 4.40, 4.25, 4.30, 4.20, 4.35, 4.30, 4.25, 4.40, 4.30]
    ev = book_event(1, "KuPS", "Oulu", ko_at("2026-10-01", 17), base[:15] + [4.60], {15: "Coolbet"})
    pick = next(c for c in odds.build_picks([ev], day) if c["name"] == "KuPS")
    assert pick["best_odds"] == 4.60 and pick["book"] == "Coolbet", pick
    assert abs(pick["odds"] - 4.35) < 1e-9 and pick["odds"] < pick["best_odds"], pick       # 5e meilleur sur 16 : (16 // 4) + 1
    p = pick["prob_book"]
    assert abs(pick["edge"] - (pick["prob"] * pick["odds"] - 1)) < 1e-9
    assert pick["edge"] < p * pick["best_odds"] - 1 - 0.03, "la valeur ne doit plus se calculer sur le maximum"
    # un seul prix aberrant ne déplace rien : sans lui, même cote typique
    no_out = next(c for c in odds.build_picks([book_event(1, "KuPS", "Oulu", ko_at("2026-10-01", 17), base[:15])], day)
                  if c["name"] == "KuPS")
    assert abs(no_out["odds"] - pick["odds"]) < 0.06
    assert odds.typical_price([(2.0, "a"), (1.9, "b"), (1.8, "c")]) == (1.9, "b")             # peu de books : au moins le 2e
    assert odds.typical_price([(1.5, "a")] * 3 + [(9.0, "x")])[0] == 1.5                       # le maximum aberrant est ignoré
    # la meilleure cote n'est qu'une note quand elle dépasse nettement la typique
    pick["home"], pick["away"], pick["start"], pick["league"] = "KuPS", "Oulu", "17:00", "Veikkausliiga"
    txt = fmt.leg(1, pick)
    assert "<b>4,35</b>" in txt and "jusqu'à 4,60 chez Coolbet" in txt, txt
    flat = dict(pick, best_odds=pick["odds"] * 1.02)
    assert "jusqu'à" not in fmt.leg(1, flat)
    print("cote typique : OK")


def test_adaptive_disagreement():
    """Seuil « les bookmakers ne sont pas d'accord » : plus haut avec peu de books ; poids du signal plus faible dans le score."""
    assert odds.disagree_threshold(4) > odds.disagree_threshold(8) > odds.disagree_threshold(16) > C.DISAGREE_BASE
    assert odds.few_books_factor(3) > odds.few_books_factor(20) > 1
    day = date(2026, 10, 1)
    wide = [3.2, 3.9, 3.0, 4.1] * 5                                   # écart de probas ~ 0,25 de la proba moyenne
    many = book_event(1, "A", "B", ko_at("2026-10-01", 17), wide)
    few = book_event(2, "C", "D", ko_at("2026-10-01", 17), wide[:4])
    cm = next(c for c in odds.build_picks([many], day) if c["name"] == "A")
    cf = next(c for c in odds.build_picks([few], day) if c["name"] == "C")
    assert cm["n_books"] == 20 and cf["n_books"] == 4
    assert cm["spread"] > odds.disagree_threshold(20) and "pas d'accord" in cm["why"], (cm["spread"], cm["why"])
    mild = [3.5, 3.9, 3.35, 3.8] * 5                                  # écart modéré : suspect à 4 books, banal à 20
    for n in (4, 20):
        c = next(c for c in odds.build_picks([book_event(3, "E", "F", ko_at("2026-10-01", 17), mild[:n])], day) if c["name"] == "E")
        assert (c["spread"] > odds.disagree_threshold(n)) == ("pas d'accord" in c["why"]), (n, c["spread"], c["why"])
    # même écart brut : le désaccord pèse moins dans le score quand il y a peu de books
    sc = lambda c: c["score"] - c["edge"] * c["weight"]                # noqa: E731 - part « désaccord » du score (steam nul)
    assert sc(cf) / cf["spread"] < sc(cm) / cm["spread"]
    print("désaccord adaptatif : OK")


def mkleg(i, odds_, prob, ev=None, market="h2h", edge=None, **kw):
    return {"key": f"k{i}", "id": ev or f"e{i}", "odds": odds_, "prob": prob, "edge": prob * odds_ - 1 if edge is None else edge,
            "n_books": 14, "weight": 1.0, "score": 0.0, "market": market, "name": "Over", "why": "", "pm": None,
            "commence": f"2026-10-01T{10 + i % 12:02d}:00:00Z", "home": f"H{i}", "away": f"A{i}", "event": f"H{i} - A{i}",
            "league": "L", "start": f"{10 + i % 12}:00", "pick": f"Pari {i}"} | kw


def test_quality_floor():
    """Une jambe doit avoir une raison : valeur, stats d'avant-match ou probabilité élevée ; sinon elle n'entre pas dans un combiné."""
    padding = mkleg(1, 1.74, 0.55, edge=-0.04)                          # Plus de 2,5 buts @1,74 sans aucun signal
    assert CB.leg_reason(padding) is None
    assert CB.leg_reason(mkleg(2, 4.40, 0.25, edge=0.06)) == "value"
    assert CB.leg_reason(mkleg(3, 4.60, 0.2305, edge=0.014)) is None   # le nul « Coolbet » de la journée type : valeur trop mince
    assert CB.leg_reason(mkleg(4, 1.39, 0.70, edge=-0.03)) == "safe"
    assert CB.leg_reason(mkleg(5, 1.39, 0.70, edge=-0.10)) is None     # sûr, mais payé trop cher (marge > 9 %)
    assert CB.leg_reason(mkleg(5, 1.35, 0.70, edge=-0.055)) == "safe"   # sûr à petite cote : la marge seule ne l'exclut pas
    assert CB.leg_reason(mkleg(6, 2.30, 0.45, edge=0.0, pm={"kind": "goals", "pgap": 0.07})) == "stats"
    assert CB.leg_reason(mkleg(7, 1.74, 0.55, edge=-0.04, pm={"kind": "goals", "pgap": -0.07})) is None   # le modèle est contre
    assert CB.leg_reason(mkleg(8, 1.49, 0.72, edge=0.0, pm={"kind": "goals", "pgap": -0.06})) is None     # même une jambe sûre
    assert CB.leg_reason(mkleg(9, 1.9, 0.5, edge=0.0, pm={"kind": "h2h", "xgd": 0.7}, name="Home")) == "stats"
    assert CB.leg_reason(mkleg(10, 4.4, 0.23, edge=0.0, pm={"kind": "h2h", "xgd": 0.7}, name="Draw")) is None
    # peu de books : l'écart de valeur exigé est plus grand
    thin = mkleg(11, 2.2, 0.47, edge=0.04, n_books=4)
    assert CB.leg_reason(thin) is None and CB.leg_reason(dict(thin, n_books=20)) == "value"
    # le plancher est dans la config
    assert C.LEG_MIN_EDGE > 0 and 0.5 < C.LEG_SAFE_PROB < 0.9 and C.LEG_REUSE_MAX >= 1 and C.LEG_PM_GAP > 0
    legs = [padding, mkleg(2, 4.40, 0.25, edge=0.06), mkleg(4, 1.39, 0.70, edge=-0.03)]
    assert [c["key"] for c in CB.quality_legs(legs)] == ["k2", "k4"]
    assert [c["key"] for c in CB.quality_legs(legs, special=True)] == []        # le spécial : seulement les marchés mal pricés
    print("plancher de qualité : OK")


def test_poor_day_and_tiers():
    """Journée pauvre : une seule ligne compacte, jamais de bloc « Pas assez de matchs » ; seuls les paliers qui se
    construisent avec des jambes de qualité sont envoyés."""
    day = date(2026, 10, 8)
    with Sandbox():
        bankroll.save({"bankroll": 100.0, "initial": 100.0})
        # 7 matchs, presque tout sans raison, quelques jambes de valeur/sûres : Sûr et Costaud sortent, pas le reste
        legs = [mkleg(i, 1.74, 0.55, edge=-0.04) for i in range(7)]                                    # du remplissage
        legs += [mkleg(20 + i, 2.5, 0.41, ev=f"e{i}", edge=0.025) for i in range(3)]                     # valeur trop mince
        legs += [mkleg(30, 2.4, 0.45, ev="e0", edge=0.08), mkleg(31, 2.6, 0.42, ev="e1", edge=0.09),
                 mkleg(32, 3.2, 0.33, ev="e2", edge=0.056)]
        msgs, sent, combos = app.build_day(legs, day)
        text = "\n".join(msgs)
        assert text.count("Journée trop pauvre") == 1 and "Pas assez" not in text, text
        built = [c["tier"] for c in combos]
        assert built and all(t in [x[0] for x in C.COMBOS] for t in built)
        assert "Journée trop pauvre : pas de " in text and "(7 matchs avec cotes)" in text
        for t, _, _ in C.COMBOS:
            assert (t in built) == (not any(fmt.tier_word(t) in line for line in text.splitlines() if "Journée trop pauvre" in line)), t
        for c in combos:                                                 # chaque jambe d'un combiné a une raison
            assert all(CB.leg_reason(next(l for l in legs if l["key"] == k)) for k in c["keys"])
        # jour sans aucune jambe de qualité : ni pari du jour ni combiné, une ligne
        msgs, sent, combos = app.build_day([mkleg(i, 1.74, 0.55, edge=-0.04) for i in range(7)], day)
        text = "\n".join(msgs)
        assert not sent and not combos and "PARI DU JOUR" not in text
        assert text.count("Journée trop pauvre : ni pari ni combiné de qualité aujourd'hui (7 matchs avec cotes).") == 1, text
        # le message d'exemple de la demande
        assert CB.skip_line([C.COMBOS[3][0], C.COMBOS[4][0]], 7) == "Journée trop pauvre : pas de Méchant/Monstre aujourd'hui (7 matchs avec cotes)."
    print("journée pauvre et paliers : OK")


def test_leg_reuse():
    """Une jambe sûre peut servir dans jusqu'à LEG_REUSE_MAX combinés ; une jambe risquée dans un seul."""
    tiers = [(f"T{i}", lo, hi) for i, (lo, hi) in enumerate([(3.0, 4.0), (3.0, 4.0), (3.0, 4.0), (3.0, 4.0), (3.0, 4.0)])]
    safe = [mkleg(i, 1.45, 0.72, edge=0.04) for i in range(3)]                  # 3 matchs sûrs
    risky = [mkleg(10 + i, 2.0, 0.55, edge=0.1) for i in range(12)]
    plan = CB.plan(safe + risky, tiers)
    uses = {}
    for _, combo in plan:
        for c in (combo[0] if combo else []):
            uses[c["id"]] = uses.get(c["id"], 0) + 1
    assert any(combo for _, combo in plan)
    assert all(n <= C.LEG_REUSE_MAX for i, n in uses.items() if i in {c["id"] for c in safe}), uses
    assert all(n == 1 for i, n in uses.items() if i in {c["id"] for c in risky}), uses       # risqué : une fois par jour
    assert max(uses.get(c["id"], 0) for c in safe) > 1                                       # le sûr est bien réutilisé
    # sans réutilisation autorisée (un seul combiné par jambe), un 2e combiné avec les mêmes matchs est refusé
    u = CB.Usage()
    u.add([safe[0], risky[0]])
    assert u.allowed(safe[0]) and not u.allowed(risky[0]) and not u.allowed(mkleg(99, 2.0, 0.5, ev=risky[0]["id"]))
    for _ in range(C.LEG_REUSE_MAX - 1):
        u.add([safe[0]])
    assert not u.allowed(safe[0]), "au plus LEG_REUSE_MAX combinés pour une jambe sûre"
    assert u.allowed(safe[1])
    # une jambe sûre d'un match déjà pris avec une jambe risquée n'est pas réutilisable
    mixed = CB.Usage()
    mixed.add([mkleg(50, 2.0, 0.5, ev="m")])
    assert not mixed.allowed(mkleg(51, 1.4, 0.75, ev="m"))
    # /combi : mêmes règles, relativement aux combinés envoyés aujourd'hui
    picks = safe + risky
    sent = [{"keys": [risky[0]["key"], risky[1]["key"]], "date": "2026-10-01"},
            {"keys": [safe[0]["key"], safe[1]["key"]], "date": "2026-10-01"}]
    usage = CB.Usage.from_sent(sent, picks)
    combo = CB.custom(picks, 4.0, usage)
    assert combo and not ({c["id"] for c in combo[0]} & {risky[0]["id"], risky[1]["id"]})
    assert 4.0 * 0.75 <= combo[1] <= 4.0 * 1.25
    print("réutilisation des jambes : OK")


def test_fixed_odds_optimiser():
    """Seule la cote compte : 12 jambes à 1,30 pour un Costaud, 3 grosses jambes pour une cote équivalente, 200 candidats en
    moins d'une seconde ; l'optimiseur égale la force brute ; un seul pari par match."""
    import itertools
    import time as _t
    # beaucoup de jambes sûres à cote basse : le nombre de matchs n'est pas une contrainte
    safe = [mkleg(i, 1.30, 0.80) for i in range(14)]
    combo = CB.best_combo(safe, 12.0, 20.0)
    assert combo and 12.0 <= combo[1] <= 20.0 and len(combo[0]) >= 9, (len(combo[0]) if combo else None)
    assert abs(combo[2] - math.prod(c["prob"] for c in combo[0])) < 1e-12
    # peu de jambes à grosse cote
    big = [mkleg(100 + i, o, 0.9 / o) for i, o in enumerate((2.5, 2.6, 3.0, 1.2))]
    c3 = CB.best_combo(big, 12.0, 20.0)
    assert c3 and 12.0 <= c3[1] <= 20.0 and len(c3[0]) in (3, 4)
    # 20 matchs pour un Monstre (cote 150 à 300) si c'est ce qu'il faut
    many = [mkleg(200 + i, 1.30, 0.80) for i in range(30)]
    mon = CB.best_combo(many, 150.0, 300.0)
    assert mon and 150.0 <= mon[1] <= 300.0 and len(mon[0]) >= 19, len(mon[0]) if mon else None
    # un pari par match, même si le même match propose des jambes très attractives
    dup = [mkleg(300 + i, 2.0, 0.6, ev=f"m{i // 3}") for i in range(30)]
    d = CB.best_combo(dup, 20.0, 40.0)
    assert d and len({c["id"] for c in d[0]}) == len(d[0])
    # égalité avec la force brute sur de petits cas
    rng = random.Random(4)
    for _ in range(40):
        legs = [mkleg(400 + i, rng.choice((1.2, 1.5, 1.9, 2.4, 3.1, 4.5)) * rng.uniform(0.97, 1.03), rng.uniform(0.15, 0.8), ev=f"b{i // 2}")
                for i in range(11)]
        lo = rng.choice((3.0, 5.0, 9.0, 14.0))
        hi = lo * 1.4
        best = None
        for k in range(2, 7):
            for cb in itertools.combinations(legs, k):
                if len({c["id"] for c in cb}) < k:
                    continue
                o = math.prod(c["odds"] for c in cb)
                if lo <= o <= hi:
                    pr = math.prod(c["prob"] for c in cb)
                    best = pr if best is None or pr > best else best
        got = CB.best_combo(legs, lo, hi, max_legs=6)
        assert (got is None) == (best is None), (lo, hi, got and got[1], best)
        assert got is None or got[2] >= best * (1 - 1e-9), (lo, hi, got[2], best)
    # vitesse : ~200 jambes, cibles jusqu'à 150+
    cand = [mkleg(1000 + i, rng.choice((1.25, 1.5, 1.9, 2.6, 3.8)) * rng.uniform(0.95, 1.05), rng.uniform(0.2, 0.8), ev=f"g{i // 2}")
            for i in range(200)]
    for lo, hi in ((5.0, 7.0), (12.0, 20.0), (40.0, 80.0), (150.0, 300.0)):
        t0 = _t.perf_counter()
        r = CB.best_combo(cand, lo, hi)
        dt = _t.perf_counter() - t0
        assert r and lo <= r[1] <= hi and dt < 1.0, (lo, hi, dt)
    # borne de sécurité : jamais plus de MAX_LEGS jambes
    r = CB.best_combo(many, 150.0, 300.0, max_legs=22)
    assert r and len(r[0]) <= 22 and 150.0 <= r[1] <= 300.0
    assert CB.best_combo(many[:1], 2.0, 3.0) is None and CB.best_combo([], 2.0, 3.0) is None
    # /combi : cote visée +/- 25 %
    cu = CB.custom(safe, 8.0)
    assert cu and 8.0 * 0.75 <= cu[1] <= 8.0 * 1.25
    assert C.COMBOS[-1][2] == 300.0 and all(lo < (hi or 1e9) for _, lo, hi in C.COMBOS)
    print("optimiseur à cote fixe : OK")


# ---------- caches ----------

def test_odds_cache_and_zero_paid_calls():
    """Cotes du jour payées une seule fois : un 2e passage (même exécution ou relance) et les commandes Telegram ne dépensent
    aucun crédit ; les passages horaires ne rafraîchissent aucune source."""
    day = "2026-10-01"
    leagues = {"soccer_epl": [(f"c{i}", f"Ca{i}", f"Cb{i}", ko_at(day, 13 + i)) for i in range(4)]}
    with Sandbox() as sb:
        bankroll.save({"bankroll": 100.0, "initial": 100.0})
        put_team_profiles({"E0": [t for e in leagues["soccer_epl"] for t in e[1:3]]})
        fake = sb.serve(FakeOdds(500, leagues))
        d = datetime(2026, 10, 1).date()
        after = datetime(2026, 10, 1, 8, tzinfo=UTC)
        with contextlib.redirect_stderr(io.StringIO()):
            ev1 = odds.fetch_events(d, after=after)
        paid1, log1 = fake.spent(), len(fake.log)
        assert paid1 > 0 and cache.COUNTS["odds"]["miss"] == len(fake.paid())
        with contextlib.redirect_stderr(io.StringIO()):
            ev2 = odds.fetch_events(d, after=after)                          # 2e passage de la même exécution
        assert fake.spent() == paid1 and len(fake.paid()) == len([c for c in fake.log if c[2]]), "aucun nouvel appel payant"
        assert len(fake.log) == log1, "pas même un appel gratuit : /sports et /events sont relus du cache"
        assert [e["id"] for e in ev2] == [e["id"] for e in ev1]
        assert all("sport_key" in e and e["bookmakers"] for e in ev2)
        assert cache.COUNTS["odds"]["hit"] >= 1 and cache.COUNTS["odds"]["saved"] >= 2 and cache.COUNTS["events"]["hit"] >= 1
        ln = cache.line()
        assert ln.startswith("♻️ Cache :") and "Odds API" in ln and "crédits évités" in ln, ln
        # relance manuelle du matin (nouveau processus : compteurs à zéro) : mêmes combinés, zéro crédit, pas de doublon
        for hour in (9, 10):
            sb.set_now(datetime(2026, 10, 1, hour, tzinfo=UTC))
            cache.reset()
            n_paid = len(fake.paid())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                app.run_daily()
            assert len(fake.paid()) == n_paid and fake.spent() == paid1, (hour, fake.spent(), paid1)
        n_combos = len(store.load("bets.json", {"combos": []})["combos"])
        sb.set_now(datetime(2026, 10, 1, 11, tzinfo=UTC))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            app.run_daily()
        assert len(store.load("bets.json", {"combos": []})["combos"]) == n_combos, "une relance n'ajoute pas de doublons"
        # Telegram : toutes les commandes sans le moindre appel réseau
        calls = []

        def boom(url, **kw):
            calls.append(url)
            raise AssertionError(f"requête réseau interdite : {url}")
        sb.serve(boom)
        stats_calls = []
        real_post = telegram.requests.post
        telegram.requests.post = boom
        try:
            for cmd in ("/top", "/match Ca0", "/combi 6", "/combi 30", "/analyse Ca1", "/stats Arsenal", "/bilan", "/bilan détail 7",
                        "/monde", "/ligues", "/bank", "/aide"):
                assert app.handle_command(cmd), cmd
            # même appelé directement, un appel payant est refusé en mode hors ligne
            with cache.offline():
                try:
                    odds.get("/sports/soccer_epl/odds", use="daily", regions="eu", markets="h2h")
                    raise AssertionError("hors ligne : appel refusé")
                except odds.OddsError as e:
                    assert e.code == "OFFLINE"
                assert stats.get("/fixtures", date="2026-10-01") is None
        finally:
            telegram.requests.post = real_post
        assert calls == [] and stats_calls == []
        # un appel payant sans « use » ne passe pas outre le budget
        sb.serve(fake)
        try:
            odds.get("/sports/soccer_epl/odds", regions="eu", markets="h2h")
            raise AssertionError("appel payant sans budget")
        except odds.BudgetError:
            pass
        # passage horaire sans clôture due : zéro requête, aucune source rafraîchie
        track.save([])
        saved = (sources.refresh_all, bootstrap.ensure, http._request)
        sources.refresh_all = bootstrap.ensure = lambda *a, **k: (_ for _ in ()).throw(AssertionError("les sources ne se rafraîchissent qu'à 08:00"))
        http._request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("aucun téléchargement"))
        n = len(fake.log)
        try:
            sb.set_now(datetime(2026, 10, 1, 13, 5, tzinfo=UTC))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                app.run_track()
        finally:
            sources.refresh_all, bootstrap.ensure, http._request = saved
        assert len(fake.log) == n, "rien n'est dû : aucun appel"
        # TTL : un cache périmé n'est pas relu
        real_clock = cache.clock
        cache.clock = lambda: real_clock() + C.ODDS_CACHE_HOURS * 3600 + 60
        try:
            assert cache.load("sports", C.EVENTS_CACHE_HOURS) is None
        finally:
            cache.clock = real_clock
    print("cache des cotes et zéro appel payant hors 08:00 : OK")


def test_sources_cache_counters():
    """Sources : cache frais = zéro requête (compté), hors ligne = disque seulement."""
    cache.reset()
    with Sandbox():
        calls = []
        saved = (http._request, state.clock)
        url = "https://football-data.co.uk/mmz4281/2526/E0.csv"
        http._request = lambda u, h=None: (calls.append(u) or Raw(200, b"Div,Date\n"), None)
        try:
            shutil.rmtree(http.cache_dir(), ignore_errors=True)
            http.reset()
            http._robots["football-data.co.uk"] = (state.clock(), type("R", (), {"can_fetch": lambda *a: True})())
            r1 = http.fetch(url, "footballdata", http.FOREVER)
            r2 = http.fetch(url, "footballdata", http.FOREVER)
            assert r1.ok and r2.ok and r2.from_cache and len(calls) == 1
            c = cache.COUNTS["footballdata"]
            assert c["miss"] == 1 and c["hit"] == 1, c
            with cache.offline():                                   # périmé mais hors ligne : le disque, sans requête
                r3 = http.fetch(url, "footballdata", 0)
                assert r3.ok and r3.stale and len(calls) == 1
                assert not http.fetch("https://football-data.co.uk/mmz4281/2526/D1.csv", "footballdata", 0).ok
            assert "football-data 1 en cache / 1 appel" in cache.line()
        finally:
            http._request, state.clock = saved
    print("compteurs du cache des sources : OK")


# ---------- messages ----------

def fake_rows_for_results():
    mk = lambda k, h, a, pick, market, res, final, name="x": {   # noqa: E731
        "key": k, "home": h, "away": a, "pick": pick, "market": market, "name": name, "result": res, "final": final,
        "odds": 1.55, "sent": True}
    return [mk("k1", "Flamengo", "Vasco", "Flamengo gagne", "h2h", "win", "2-0", "Flamengo"),
            mk("k2", "Santos", "Gremio", "Plus de 2,5 buts", "totals", "win", "3-1"),
            mk("k3", "Nautico", "Gremio & Co", "Nautico gagne", "h2h", "loss", "1-1", "Nautico"),
            mk("k4", "Ajax", "PSV", "Match nul", "h2h", "win", "1-1", "Draw")]


def test_messages_human():
    """Messages Telegram : HTML échappé, virgule décimale, heures « 21h00 », layout des combinés et des résultats, pas de jargon."""
    assert fmt.num(1.55) == "1,55" and fmt.odds_txt(156.66) == "156,7" and fmt.odds_txt(6.12) == "6,12"
    assert fmt.pct(0.314) == "31 %" and fmt.pct(0.0069) == "0,7 %" and fmt.pct(0.5) == "50 %"
    assert fmt.hhmm("21:00") == "21h00" and fmt.fr_day(date(2026, 10, 9)) == "vendredi 9 octobre"
    assert fmt.units(2) == "2 u" and fmt.units(0.2) == "0,2 u" and fmt.signed(10.2) == "+10,2 u" and fmt.signed(-0.2) == "−0,2 u"
    assert fmt.esc("Bayer & Co <1>") == "Bayer &amp; Co &lt;1&gt;"
    # libellés comme chez le bookmaker
    L = fmt.label
    assert L("totals", "Over", 2.5, None, "soccer") == "Plus de 2,5 buts" and L("totals", "Under", 2.5, None, "soccer") == "Moins de 2,5 buts"
    assert L("alternate_totals_corners", "Under", 9.5, None, "soccer") == "Moins de 9,5 corners"
    assert L("alternate_totals_cards", "Over", 3.5, None, "soccer") == "Total cartons : plus de 3,5"
    assert L("btts", "Yes", None, None, "soccer") == "Les deux équipes marquent : oui"
    assert L("double_chance", "Draw or Flamengo", None, None, "soccer") == "Double chance : Nul ou Flamengo"
    assert L("player_shots_on_target", "Over", 1.5, "Mbappé", "soccer") == "Mbappé : 2 tirs cadrés ou plus"
    assert L("player_shots_on_target", "Over", 0.5, "Mbappé", "soccer") == "Mbappé : 1 tir cadré ou plus"
    assert L("h2h", "Flamengo", None, None, "soccer") == "Flamengo gagne" and L("h2h", "Draw", None, None, "soccer") == "Match nul"
    # un combiné : titre + cote, chances et mise, jambes numérotées séparées par une ligne vide
    legs = [mkleg(1, 1.55, 0.7, edge=0.0, home="Flamengo", away="Vasco & Fils", league="Brasileirão", pick="Flamengo gagne",
                  why="", start="21:00"),
            mkleg(2, 1.90, 0.53, edge=0.07, home="Santos", away="Gremio", pick="Plus de 2,5 buts", start="22:30",
                  why="cote plus haute qu'ailleurs (1,90 contre 1,78 en moyenne)", best_odds=2.05, book="Betfair")]
    msg = fmt.combo(C.COMBOS[0][0], legs, 2.95, 0.371, 2.0, {"k1": "safe", "k2": "value"})
    assert isinstance(msg, telegram.Html) and msg.startswith(
        "<b>🛡️ SÛR — cote 2,95</b>\n🎯 2 paris · 37 % de chances\n📈 Espérance +9,4 % · 💰 mise conseillée : <b>2 u</b>\n\n"
        "<b>1. Flamengo – Vasco &amp; Fils</b>  <i>21h00 · Brasileirão</i>"), msg
    assert "\n👉 Flamengo gagne — <b>1,55</b>\n💡 <i>Très probable : 70 % de chances</i>\n\n<b>2. Santos – Gremio</b>  <i>22h30" in msg, msg
    assert "<b>1,90</b> <i>(jusqu'à 2,05 chez Betfair)</i>\n💡 <i>Cote plus haute qu'ailleurs (1,90 contre 1,78 en moyenne)</i>" in msg, msg
    # résultats du lendemain
    rows = fake_rows_for_results()
    won = {"tier": C.COMBOS[0][0], "keys": ["k1", "k2"], "odds": 6.12, "stake": 2.0, "result": "win", "odds_final": 6.12, "date": "2026-10-07"}
    lost = {"tier": C.COMBOS[4][0], "keys": ["k1", "k2", "k3", "k4"], "odds": 156.7, "stake": 0.2, "result": "loss", "date": "2026-10-07"}
    by = {r["key"]: r for r in rows}
    w, l_ = app.combo_result(won, by), app.combo_result(lost, by)
    assert w.split("\n")[0] == "<b>✅ SÛR gagné — cote 6,12</b> · +10,2 u", w
    assert w.split("\n")[1:] == ["✅ Flamengo gagne (2-0)", "✅ Santos – Gremio : Plus de 2,5 buts (3-1)"], w
    assert l_ == "❌ <b>MONSTRE perdu</b> — 3 sur 4, raté : Nautico – Gremio &amp; Co (1-1) · −0,2 u", l_
    with Sandbox():
        bankroll.save({"bankroll": 104.3, "initial": 100.0})
        bets = {"picks": [], "combos": [dict(won), dict(lost)]}
        res = app.results_message(bets, rows, [])
        assert res.startswith("<b>📋 Résultats d'hier</b>\n\n<b>✅ SÛR gagné") and res.rstrip().endswith("<i>Hier : 1 combiné gagné sur 2 · bankroll 104,3 u (+4,3)</i>"), res
        assert all(c["reported"] for c in bets["combos"]) and app.results_message(bets, rows, []) is None
    # le message du jour de l'échantillon : pas de jargon, HTML bien formé, virgules
    bankroll.save({"bankroll": 100.0, "initial": 100.0})
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        out = app.run_daily(sample=True)
    plain_all = "\n".join(fmt.plain(m) for m in out).lower()
    for word in fmt.JARGON:
        assert word not in plain_all, (word, plain_all[:3000])
    assert not re.search(r"\d\.\d", plain_all.replace("2026", "")), re.findall(r".{20}\d\.\d.{10}", plain_all)[:3]
    for m in out:
        assert m.count("<b>") == m.count("</b>") and m.count("<i>") == m.count("</i>")
    # Telegram refuse le HTML : le même message repart en texte brut
    sent = []
    real = (telegram.api, telegram.creds)

    def api(method, **p):
        sent.append(p)
        if p.get("parse_mode") == "HTML":
            raise telegram.TelegramError("Bad Request: can't parse entities", 400)
        return {}
    telegram.api, telegram.creds = api, lambda: ("tok", "42")
    try:
        assert telegram.send(telegram.Html("<b>Test &amp; co</b>")) is True
        assert sent[0]["parse_mode"] == "HTML" and "parse_mode" not in sent[1] and sent[1]["text"] == "Test & co", sent
        assert telegram.send("a < b & c") and "parse_mode" not in sent[-1]
        long = telegram.Html("\n\n".join(f"<b>bloc {i}</b>" for i in range(900)))
        sent.clear()
        telegram.api = lambda method, **p: sent.append(p) or {}
        telegram.send(long)
        assert len(sent) >= 2 and all(len(p["text"]) <= 4000 and p["text"].count("<b>") == p["text"].count("</b>") for p in sent)
    finally:
        telegram.api, telegram.creds = real
    # /bilan : simple par défaut, technique avec « détail »
    print("messages lisibles : OK")


# ---------- stats mondiales, tirs, combinés à cote juste (jeu de données réel du 8 octobre) ----------

REAL = os.path.join(os.path.dirname(__file__), "fixtures", "real_2026-10-08")


LEAGUES8 = {1492398: ("Serie A", "Brazil"), 1492390: ("Serie A", "Brazil"), 1520912: ("Serie B", "Brazil"), 1520915: ("Serie B", "Brazil"),
            1638102: ("Veikkausliiga", "Finland"), 1638103: ("Veikkausliiga", "Finland"), 1492780: ("Premier Division", "Ireland")}


def real_world():
    """Vraies données du 8 octobre 2026 : (matchs d'API-Football, tour du monde, paris du jour, ligues pour FakeOdds). Les ligues du
    Brésil, de la Finlande et de l'Irlande sont nommées comme chez API-Football (le fichier d'époque ne les gardait pas)."""
    entries = [dict(e, league=LEAGUES8[e["id"]]) if e["id"] in LEAGUES8 else e
               for e in json.load(open(os.path.join(REAL, "fixtures_day.json")))[WDAY]]
    monde = json.load(open(os.path.join(REAL, "monde.json")))
    today = json.load(open(os.path.join(REAL, "today.json")))
    leagues = {k: [(f"{k[7:13]}{i}", h, a, f"{WDAY}T{t}:00Z") for i, (h, a, t) in enumerate(v)] for k, v in monde["leagues"].items()}
    return entries, monde, today, leagues


def accumulated_world(api, entries):
    """Ce que Kelly aurait accumulé les jours précédents (forfait gratuit : jour après jour) : 6 matchs (profil « bon ») pour les clubs
    brésiliens, finlandais, irlandais et africains du jour, sauf Tanzanie et Ouganda : 4 (« utilisable », confiance réduite) ; la veille (7 octobre), Santos et Flamengo ont encore joué (2 matchs à compter)."""
    from kelly import apileagues
    teams = {}
    for e in entries:
        for side in ("home", "away"):
            t = (e[side + "_id"], e[side])
            if e["id"] in LEAGUES8:
                lk, ln, _ = apileagues.classify(*reversed(LEAGUES8[e["id"]]))
                teams[t] = (6, LEAGUES8[e["id"]], lk, ln)
            elif e.get("lg"):
                name, _, country = e["lg"].rpartition(" (")
                teams[t] = (4 if country.rstrip(")") in ("Tanzania", "Uganda") else 6, (name, country.rstrip(")")), "africa", e["lg"])
    for t, (n, league, lk, ln) in teams.items():
        seed_history(api, t, n, league, lk, ln)
    api.add_day("2026-10-07", [fake_match(9_100_001, (128, "Santos"), (9001, "Vitoria"), ts_of("2026-10-07", 22)),
                               fake_match(9_100_002, (9002, "Bahia"), (127, "Flamengo"), ts_of("2026-10-07", 22))])
    features.reload()
    return teams


def free_scan(leagues):
    return {"day": WDAY, "leagues": {k: [{"id": i, "sport_key": k, "home_team": h, "away_team": a, "commence_time": t}
                                         for i, h, a, t in v] for k, v in leagues.items()}}


def test_fair_stat_markets():
    """Tirs et tirs cadrés (total et par équipe) dans les pronostics sans cote : lignes autour de la moyenne attendue, libellés,
    équipe visée, bornes, règlement avec les tirs de chaque équipe."""
    from kelly import fair, prematch
    f = {"n_home": 8, "n_away": 8, "exp_shots": 24.0, "exp_sot": 8.4, "exp_shots_home": 13.0, "exp_shots_away": 11.0,
         "exp_sot_home": 4.6, "exp_sot_away": 3.8, "exp_corners": 10.4, "aps_keys": ["exp_shots", "exp_sot", "exp_shots_home", "exp_shots_away",
                                                                                      "exp_sot_home", "exp_sot_away", "exp_corners"], "aps_q": 0.45}
    opts = fair.options(f, "Alpha", "Bravo")
    markets = {o[0] for o in opts}
    shots = [m for m in fair.STAT_MARKETS if m.startswith(("team_shots_", "team_sot_"))]
    assert set(shots) <= markets and prematch.CORNERS in markets and not markets & {"team_fouls_total", "team_throws_total"}, markets
    assert "stats réelles : 8.4 tirs cadrés attendus (8 derniers matchs seulement)" in {o[4] for o in opts if o[0] == "team_sot_total"}
    # chaque ligne : proba du côté « plus » + proba du côté « moins » = 1, plus la ligne est haute moins « plus » est probable
    for m in shots:
        by = {(o[1], o[2]): o[3] for o in opts if o[0] == m}
        pts = sorted({pt for _, pt in by})
        assert len(pts) >= 3 and all(abs(by[("Over", pt)] + by[("Under", pt)] - 1) < 1e-9 for pt in pts), m
        assert all(by[("Over", a)] > by[("Over", b)] for a, b in zip(pts, pts[1:])), m
    # une valeur attendue hors bornes (donnée suspecte) ou un profil trop court : rien
    assert not {o[0] for o in fair.options(dict(f, exp_sot=40.0), "A", "B")} & {"team_sot_total"}
    assert fair.options(dict(f, n_home=2), "A", "B") == []                                   # sous 3 matchs, jamais
    short = dict(f, n_home=3, n_away=4, aps_q=0.2)                                           # profil API-Football utilisable (3 matchs) : admis, qualité réduite
    assert {o[0] for o in fair.options(short, "A", "B")} >= set(shots)
    assert fair.options({k: v for k, v in short.items() if k not in ("aps_keys", "aps_q")}, "A", "B") == []   # autres sources : 5 matchs minimum
    ev = {"id": "m1", "sport_key": "soccer_brazil_campeonato", "home_team": "Alpha", "away_team": "Bravo", "commence_time": f"{WDAY}T22:30:00Z"}
    picks = {c["market"]: c for c in (fair.make_pick(ev, *b, "API-Football") for b in fair.best_per_family(opts, set(fair.ALL_FAMILIES)))}
    assert set(shots) <= set(picks)
    home, away, tot = picks["team_sot_home"], picks["team_shots_away"], picks["team_sot_total"]
    assert home["who"] == "Alpha" and away["who"] == "Bravo" and tot["who"] is None
    assert re.fullmatch(r"Alpha : (plus|moins) de \d,5 tirs cadrés", home["pick"]), home["pick"]
    assert re.fullmatch(r"Bravo : (plus|moins) de \d+,5 tirs vers le but", away["pick"]) and re.fullmatch(r"(Plus|Moins) de \d,5 tirs cadrés au total", tot["pick"])
    assert home["key"].startswith("m1|team_sot_home||") and home["key"] != picks["team_sot_total"]["key"]
    assert fair.pick_label({"market": "team_sot_home", "name": "Over", "point": 4.5, "home": "Alpha", "away": "Bravo"}) == "Alpha : plus de 4,5 tirs cadrés"
    assert fair.pick_label({"market": "team_shots_away", "name": "Under", "point": 11.5, "home": "Alpha", "away": "Bravo"}) == "Bravo : moins de 11,5 tirs vers le but"
    # confiance : celle du profil API-Football (court) pour ses chiffres ; le modèle simple monte avec le nombre de matchs
    assert tot["conf"] == 0.45 if "conf" in tot else True
    assert fair.simple_q({"n_home": 10, "n_away": 12}, "totals") > fair.simple_q({"n_home": 5, "n_away": 12}, "totals") == 0.4
    # règlement : tirs de chaque équipe (API-Football : side) ; null = ni gagné ni perdu ; repli CSV football-data
    side = {"home": {"shots": 14, "sot": 5, "nul": []}, "away": {"shots": 9, "sot": 3, "nul": ["sot"]}}
    fst = {"shots": 23, "sot": 8, "side": side}
    row = lambda market, name, point: {"market": market, "name": name, "point": point, "home": "Alpha", "away": "Bravo", "who": None}   # noqa: E731
    ev_ = {"scores": [{"name": "Alpha", "score": "1"}, {"name": "Bravo", "score": "0"}]}
    assert settle.settle_pick(row("team_sot_home", "Over", 4.5), ev_, fst) == "win"
    assert settle.settle_pick(row("team_shots_away", "Under", 9.5), ev_, fst) == "win"
    assert settle.settle_pick(row("team_shots_total", "Over", 23.5), ev_, fst) == "loss" and settle.settle_pick(row("team_shots_total", "Over", 22.5), ev_, fst) == "win"
    assert settle.settle_pick(row("team_sot_away", "Over", 2.5), ev_, fst) is None                  # tirs cadrés null : on ne devine pas
    assert settle.settle_pick(row("team_sot_total", "Over", 7.5), ev_, fst) is None                 # le total dépend d'une valeur null
    assert settle.settle_pick(row("team_sot_home", "Over", 4.5), ev_, None) is None
    assert settle.final_text(row("team_shots_home", "Over", 5.5), ev_, fst) == "14 tirs vers le but" and settle.final_text(row("team_sot_home", "Over", 5.5), ev_, fst) == "5 tirs cadrés"
    from kelly.sources import footballdata as fdm
    m = fdm.match_stats({"hs": 14, "as": 9, "hst": 5, "ast": 3, "hc": 4, "ac": 5})
    assert m["sot"] == 8 and m["side"] == {"home": {"shots": 14, "sot": 5}, "away": {"shots": 9, "sot": 3}}
    assert settle.shot_value("team_sot_away", m) == 3 and settle.shot_value("team_shots_total", m) == 23
    print("tirs et tirs cadrés (pronostics, libellés, règlement) : OK")


def prono(i, p, min_odds=None, conf=0.5, market="alternate_totals_cards", event=None):
    min_odds = min_odds or round(1 / p * 1.05, 2)
    return {"key": f"m{i}|{market}||3.5|Under", "id": f"m{i}", "sport_key": "soccer_brazil_campeonato", "league": "Brasileirão",
            "event": event or f"Eq{i} - Adv{i}", "home": f"Eq{i}", "away": f"Adv{i}", "commence": f"{WDAY}T2{i % 4}:30:00Z",
            "start": f"2{i % 4}:30", "market": market, "name": "Under", "point": 3.5, "who": None, "group": "soccer",
            "pick": "Total cartons : moins de 3,5", "prob": p, "fair": round(1 / p, 2), "min_odds": min_odds, "why": "", "src": "API-Football",
            "conf": conf, "score": p - 0.5}


def test_fair_combos():
    """Combinés à cote juste : jambes solides seulement (proba, qualité), un match une fois, cote mini dans le palier, titres
    clairs, mise indicative (jamais la bankroll), séparés des combinés réels."""
    pronos = [prono(i, 0.62 + 0.01 * (i % 6)) for i in range(24)]
    bad = [prono(90, 0.58), prono(91, 0.72, conf=0.3), prono(92, 0.7, min_odds=1.0)]          # trop faible / données pauvres / sans cote mini
    legs = CB.fair_legs(pronos + bad)
    assert len(legs) == 24 and all(c["odds"] == c["min_odds"] for c in legs) and not {"m90", "m91", "m92"} & {c["id"] for c in legs}
    assert CB.fair_eligible(prono(1, 0.60)) and not CB.fair_eligible(prono(1, 0.599)) and not CB.fair_eligible(prono(1, 0.7, conf=0.39))
    plan = CB.fair_plan(pronos + bad)
    assert [t for t, _ in plan] == [5, 12, 25]
    built = [(t, c) for t, c in plan if c]
    assert [t for t, _ in built] == [5, 12, 25], plan
    for target, (cl, total, prob) in built:
        lo, hi = next((lo, hi) for t, lo, hi in C.FAIR_COMBO_TIERS if t == target)
        assert lo <= total <= hi and abs(total - math.prod(c["min_odds"] for c in cl)) < 1e-9, (target, total)
        assert abs(prob - math.prod(c["prob"] for c in cl)) < 1e-9 and len({c["id"] for c in cl}) == len(cl) >= 2   # un match une fois
        assert all(c["prob"] >= C.FAIR_COMBO_MIN_P for c in cl)
    # messages : titre « à vérifier chez 1xBet », jambes « joue si la cote est au moins X », mise indicative
    for target, (cl, total, prob) in built:
        msg = fmt.fair_combo(total, cl, prob, CB.fair_stake(total))
        assert isinstance(msg, telegram.Html) and msg.startswith(f"<b>🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET</b>\n🎯 Cote mini <b>{fmt.odds_txt(total)}</b>"), msg
        assert msg.count("Joue si 1xBet paie au moins <b>") == len(cl) and "Mise indicative" in msg and "pas calculée sur ta bankroll" in msg
        assert "mise conseillée" not in msg and "cote juste" not in msg and not re.search(r"\d\.\d", fmt.plain(msg))
        assert msg.count("<b>") == msg.count("</b>") and msg.count("<i>") == msg.count("</i>")
    stakes = [CB.fair_stake(t) for _, (_, t, _) in built]
    assert stakes == sorted(stakes, reverse=True) and max(stakes) <= 1.0                          # plus c'est gros, plus la mise indicative est petite
    # la mise indicative ne dépend pas de la bankroll
    with Sandbox():
        bankroll.save({"bankroll": 10.0, "initial": 10.0})
        a = fmt.fair_combo(5.2, built[0][1][0], 0.2, CB.fair_stake(5.2))
        bankroll.save({"bankroll": 5000.0, "initial": 10.0})
        assert a == fmt.fair_combo(5.2, built[0][1][0], 0.2, CB.fair_stake(5.2))
    # réutilisation : un pronostic solide (>= 68 %) peut servir dans plusieurs paliers, un autre une seule fois
    risky = CB.fair_plan([prono(i, 0.62) for i in range(24)])
    ids = [c["id"] for _, cmb in risky if cmb for c in cmb[0]]
    assert len(ids) == len(set(ids)), "jambes à 62 % : un seul combiné chacune"
    # /combi <cote> sans cotes réelles : la cote mini est AU MOINS la cote visée
    fc = CB.fair_custom(pronos, 8)
    assert fc and 8 <= fc[1] <= 8 * (1 + C.CUSTOM_TOLERANCE)
    assert CB.fair_custom(pronos[:3], 25) is None and CB.fair_custom([], 5) is None
    sf = CB.fair_shortfall(pronos[:3], 25)
    assert sf["matches"] == 3 and sf["best_total"] < 25 and sf["need"] >= 7, sf
    # pas assez de matière : aucun palier, aucune exception
    assert [c for _, c in CB.fair_plan(pronos[:2])] == [None, None, None] and CB.fair_plan([]) == [(5, None), (12, None), (25, None)]
    print("combinés à cote juste : OK")


def test_replay_oct8():
    """Rejoue le 8 octobre 2026 avec les vrais fichiers de la journée (tour du monde : 7 matchs de ligues du catalogue, dont le
    Brésil, + 24 matchs africains ; vrais paris de Finlande et d'Irlande) et un faux API-Football du FORFAIT GRATUIT (ni « last », ni
    « ids », ni date hors hier..demain) : l'historique des équipes est celui que Kelly a accumulé les jours précédents, le matin il ajoute
    les matchs d'hier (55 requêtes au plus), le Brésil est couvert en pronostics, un combiné à cote juste est envoyé en plus des combinés
    réels, et /combi 25 en propose un (ou explique ce qui manque)."""
    from kelly import apistats, fair
    entries, monde, today, leagues = real_world()
    picks = today["picks"]
    assert len(picks) == 13 and len(entries) == 149
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))
        bankroll.save({"bankroll": 100.0, "initial": 100.0})
        api = FakeFootballAPI(entries, today=WDAY)
        stats.requests.get = api
        os.environ["STATS_API_KEY"] = "test-stats-key"
        stats._calls, stats._halted = 0, False
        stats._refreshed.clear()
        accumulated_world(api, entries)
        scan = free_scan(leagues)
        day = datetime(2026, 10, 8).date()
        # --- le matin : les matchs d'hier s'ajoutent à l'historique, dans la limite du quota (listes de matchs comprises)
        rep = apistats.fill(day, scan)
        q = store.load("stats_quota.json", {})
        assert q["n"] == len(api.calls) <= C.APISTATS_DAILY and q["aps"] == q["n"] and stats.left_today() >= C.FAIR_API_RESERVE
        assert api.refused == [] and api.count("/fixtures/players") == 0, api.refused           # jamais « last », « ids » ni date interdite
        assert rep["added"] == 3 and rep["requests"] == 5 and rep["tot"] == 30 and rep["cov"] == 30 and rep["unmatched"] == 0, rep
        log = apistats.report_lines(rep)
        assert len(log) == 2 and log[0] == ("🌍 Stats mondiales (accumulation jour après jour, forfait gratuit) : 3 matchs d'hier ajoutés (5 requêtes) · "
                                            "64 équipes suivies, 62 avec 3+ matchs (utilisables), 50 avec 6+ (bonnes) · couverture des matchs du jour : 30/30"), log
        assert "30 gardées pour le règlement" in log[1] and f"profils 5/{C.APISTATS_DAILY}" in log[1], log
        prof = apistats.profiles()["teams"]
        assert all(prof[t]["all"]["n"] >= C.APISTATS_GOOD_N for t in ("Santos", "Flamengo", "Ceará", "Criciuma", "Nautico PE", "Grêmio Novorizontino",
                                                                      "Atletico Paranaense", "Atletico Mineiro")), "le Brésil est couvert"
        assert prof["Santos"]["all"]["n"] == 7 and "Vitoria" not in prof, "1 seul match : suivi mais pas encore utilisable"
        # --- pronostics : le Brésil a désormais corners, cartons, tirs ; l'Afrique commence
        africa = stats.africa_matches(day)
        assert len(africa) == 24
        pronos = fair.make(scan, picks, day, africa=africa)
        brazil = {c["event"] for c in pronos if c["sport_key"].startswith("soccer_brazil")}
        assert brazil >= {"Santos - Flamengo", "Ceará - Criciuma", "Nautico PE - Grêmio Novorizontino", "Atletico Paranaense - Atletico Mineiro"}, brazil
        assert any(c["sport_key"] == "africa" and c["league"].endswith(")") for c in pronos), "matchs africains couverts"
        assert all(c["conf"] > 0 and C.FAIR_P_RANGE[0] <= c["prob"] <= C.FAIR_P_RANGE[1] for c in pronos)
        assert {c["market"] for c in pronos} & set(fair.STAT_MARKETS), "tirs / tirs cadrés proposés"
        # --- les combinés : réels (3 matchs seulement) et à cote juste, séparés
        fair_combos = CB.fair_plan(pronos)
        built = [(t, c) for t, c in fair_combos if c]
        assert len(built) >= 2, fair_combos
        # profils « utilisables » (3 à 5 matchs : Tanzanie, Ouganda) : pronostics seuls, confiance réduite, jamais dans un combiné à cote juste
        short = [c for c in pronos if c["sport_key"] == "africa" and ("Tanzania" in c["league"] or "Uganda" in c["league"])]
        assert short and all(c["conf"] < C.FAIR_COMBO_MIN_Q for c in short), [(c["event"], c["conf"]) for c in short]
        assert not {leg["id"] for _, combo in built for leg in combo[0]} & {c["id"] for c in short}
        good = [c for c in pronos if c["sport_key"] == "africa" and c not in short]
        assert good and all(c["conf"] >= C.FAIR_COMBO_MIN_Q for c in good)
        from kelly import monde as M
        world = M.build(scan, day, datetime(2026, 10, 8, 8, tzinfo=UTC), paid=monde["paid"], africa=africa)
        msgs, sent_picks, sent_combos = app.build_day(picks, day, world, pronos, None, fair_combos)
        text = [fmt.plain(m) for m in msgs]
        fair_msgs = [m for m in msgs if m.startswith("<b>🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET</b>")]
        assert len(fair_msgs) == len(built) >= 2
        head = text[0]
        assert "3 avec des cotes réelles" in head and f"{len(built)} combinés à vérifier chez 1xBet" in head, head
        assert all(c["tier"] in [t for t, _, _ in C.COMBOS] for c in sent_combos)
        assert not any("paie au moins" in t or "— mini" in t for t in text if "COMBINÉ À VÉRIFIER" not in t and "Pronostics Kelly sans cote" not in t)
        real_ids = {c["id"] for c in sent_picks}
        assert real_ids <= {p["id"] for p in picks}                                      # les combinés réels n'ont que des jambes à vraies cotes
        for fm in fair_msgs:
            assert "mise conseillée" not in fm and "Mise indicative" in fm
        # --- enregistrement : fichier à part, jamais dans bets.json ni dans les mises de la bankroll
        n_rec = fair.record_combos(built, day)
        rows = store.load("fair_combos.json", [])
        assert n_rec == len(rows) == len(built) and all(r["result"] is None and len(r["legs"]) >= 2 for r in rows)
        assert fair.record_combos(built, day) == 0, "pas de doublon"
        assert store.load("bets.json", {"combos": []})["combos"] == [] and bankroll.load()["bankroll"] == 100.0
        assert store.size("fair_combos.json") < 30_000
        # --- /combi 25 : cotes réelles impossibles -> combiné à cote juste, clairement annoncé
        keep = pronos[:40] + [c for c in pronos[40:] if CB.fair_eligible(c)][:40]
        store.save("today.json", {"day": str(day), "picks": picks, "pronos": [{k: c[k] for k in c if k != "score"} for c in keep]})
        reply = app.handle_command("/combi 25")
        assert isinstance(reply, telegram.Html) and "ne permettent pas d'atteindre la cote 25" in reply, reply
        assert "🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET" in reply and "Cote mini <b>2" in reply and "à vérifier chez 1xBet" in reply
        mini = float(re.search(r"Cote mini <b>(\d+,\d+)", reply).group(1).replace(",", "."))
        assert 25 <= mini <= 25 * (1 + C.CUSTOM_TOLERANCE) and "Joue si 1xBet paie au moins" in reply and "ise indicative" in reply
        assert "SUR MESURE" not in reply and "mise conseillée" not in reply
        # --- une cote hors de portée de TOUS les pronostics solides du jour : impossible, on dit précisément ce qui manque
        huge = int(CB.fair_shortfall(keep, 25)["best_total"] * 5)
        reply = app.handle_command(f"/combi {huge}")
        assert reply.startswith(f"Impossible de monter un combiné à la cote {huge} aujourd'hui.\n• Avec les cotes réelles, les 3 paris de qualité") \
            and f"ne permettent pas d'atteindre la cote {huge}" in reply, reply
        assert "Pronostics Kelly sans cote" in reply and "il en faudrait environ" in reply and "équipes déjà couvertes" in reply, reply
        # --- sans aucune cote réelle : /combi 25 propose quand même un combiné à cote juste
        store.save("today.json", {"day": str(day), "picks": [], "pronos": [{k: c[k] for k in c if k != "score"} for c in keep]})
        reply = app.handle_command("/combi 25")
        assert reply.startswith("Pas de cotes réelles aujourd'hui : voici un combiné bâti sur les pronostics de Kelly") and "COMBINÉ À VÉRIFIER" in reply, reply
        store.save("today.json", {"day": str(day), "picks": [], "pronos": keep[:2]})
        assert app.handle_command("/combi 25").startswith("Pas de cotes réelles aujourd'hui et pas assez de pronostics solides")
        # --- /stats, /analyse : le Brésil a ses chiffres, avec la source
        st = app.handle_command("/stats Nautico")
        assert "Nautico Recife" in st and "(API-Football, Brasileirão Série B)" in st and "Tirs cadrés" in st and "Corners" in st, st
        store.save("today.json", {"day": str(day), "picks": picks, "pronos": [{k: c[k] for k in c if k != "score"} for c in pronos]})
        an = app.handle_command("/analyse Santos")
        assert "Santos - Flamengo" in an and "Chiffres d'avant-match</b>\n<i>API-Football" in an and "• Tirs cadrés : <b>" in an, an
    print("rejeu du 8 octobre : OK")


def test_run_daily_world():
    """Le matin complet (run_daily) avec un faux API-Football et un faux The Odds API : profils remplis avant l'achat des cotes,
    pronostics et combinés à cote juste envoyés, suivis à part, réglés gratuitement le lendemain et résumés dans /bilan."""
    from kelly import apistats, fair
    entries, monde, today, leagues = real_world()
    with Sandbox() as sb:
        C.SPORTS = list(C.LEAGUE_KEYS)
        sb.set_now(datetime(2026, 10, 8, 8, tzinfo=UTC))
        bankroll.save({"bankroll": 100.0, "initial": 100.0})
        api = FakeFootballAPI(entries, today=WDAY)
        odds_fake = FakeOdds(500, leagues, sports=list(leagues))
        accumulated_world(api, entries)

        order = []                                                      # journal des appels, tous services confondus

        def net(url, **kw):
            order.append(("football", url.split("api-sports.io")[1], kw.get("params") or {}) if "api-sports.io" in url
                         else ("odds", url.split("/v4")[1]))
            return api(url, **kw) if "api-sports.io" in url else odds_fake(url, **kw)
        sb.serve(net)
        os.environ["STATS_API_KEY"] = "test-stats-key"
        stats._calls, stats._halted = 0, False
        stats._refreshed.clear()
        features.reload()
        buf = io.StringIO()
        sent = []
        real = telegram.send
        telegram.send = lambda text, chat_id=None: sent.append(text) or True
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
                app.run_daily()
        finally:
            telegram.send = real
        log = buf.getvalue()
        q = store.load("stats_quota.json", {})
        assert q["n"] == len(api.calls) and q["aps"] <= C.APISTATS_DAILY and stats.left_today() >= C.FAIR_API_RESERVE, q
        assert api.refused == [] and api.count("/fixtures/players") == 0, api.refused       # forfait gratuit : jamais « last », « ids », ni date interdite
        assert not any(p.keys() & {"last", "ids", "team"} for o in order if o[0] == "football" for p in [o[2]])
        assert "🌍 Stats mondiales (accumulation jour après jour, forfait gratuit) : 3 matchs d'hier ajoutés (5 requêtes)" in log, log
        assert "couverture des matchs du jour : 30/30" in log and "30 gardées pour le règlement" in log, log
        # les profils précèdent l'achat des cotes : les ligues payées sont celles où Kelly a des profils (ici le Brésil compte)
        last_profile = max(i for i, o in enumerate(order) if o[0] == "football" and o[1] == "/fixtures/statistics")
        first_paid = min(i for i, o in enumerate(order) if o[0] == "odds" and o[1].endswith("/odds"))
        assert last_profile < first_paid, "les profils sont remplis avant l'achat des cotes"
        fair_msgs = [m for m in sent if m.startswith("<b>🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET</b>")]
        assert fair_msgs and all("Joue si 1xBet paie au moins" in m for m in fair_msgs), [m[:60] for m in sent]
        header = next(m for m in sent if m.startswith("<b>🔥 Kelly"))
        assert "combiné" in header and "à vérifier chez 1xBet" in header, header
        rows = store.load("fair_combos.json", [])
        shot_msgs = [m for m in sent if m.startswith("<b>🎯 COMBINÉ TIRS")]                       # combinés « spécial tirs »
        assert rows and len(rows) == len(fair_msgs) + len(shot_msgs) and all(r["tier"] in (5, 12, 25, "tc5", "tv5") for r in rows)
        bets = store.load("bets.json", {"combos": []})
        assert all(c["tier"] in [t for t, _, _ in C.COMBOS] for c in bets["combos"])                  # jamais de combiné à cote juste ici
        today_ = store.load("today.json", {})
        assert today_["pronos"] and {c["sport_key"] for c in today_["pronos"]} & {"africa", "soccer_brazil_campeonato"}
        assert any(m.startswith("<b>🎲 Pronostics Kelly sans cote") for m in sent) and any("🌍 Tour du monde" in m for m in sent)
        for m in sent:
            assert m.count("<b>") == m.count("</b>") and m.count("<i>") == m.count("</i>"), m
        plain_all = "\n".join(fmt.plain(m) for m in sent).lower()
        for word in fmt.JARGON:
            assert word not in plain_all, (word, plain_all[:2000])
        assert not re.search(r"\d\.\d", plain_all.replace("2026", "")), re.findall(r".{20}\d\.\d.{10}", plain_all)[:3]
        # --- /combi 25 depuis le cache du matin : aucune requête réseau
        sb.serve(lambda url, **kw: (_ for _ in ()).throw(AssertionError(f"requête inattendue : {url}")))
        assert "COMBINÉ" in app.handle_command("/combi 25")
        # --- le lendemain matin : les matchs sont joués ; règlement gratuit des combinés à cote juste, résultats et /bilan
        sb.serve(net)
        for e in entries:
            api.results[e["id"]] = (2, 1)
        api.new_day()
        sb.set_now(datetime(2026, 10, 9, 8, tzinfo=UTC))                          # l'horloge d'API-Football suit celle du test
        d = store.load("fixtures_day.json", {})
        d["_fetched"] = {k: t - 48 * 3600 for k, t in d.get("_fetched", {}).items()}
        store.save("fixtures_day.json", d)
        stats._refreshed.clear()
        stats._neg.clear()
        stats._calls, stats._halted = 0, False
        msgs = app.fair_combos_report(datetime(2026, 10, 9, 8, tzinfo=UTC))
        assert api.refused == [], api.refused                                       # la liste d'hier (8 octobre) est dans la fenêtre du forfait gratuit
        rows = store.load("fair_combos.json", [])
        assert all(r["result"] in ("win", "loss", None) for r in rows) and any(r["result"] for r in rows), rows
        assert msgs and msgs[0].startswith("<b>🎲 Combinés à vérifier chez 1xBet — résultats</b>")
        assert "Combiné à cote mini" in msgs[0] and ("✅" in msgs[0] or "❌" in msgs[0]) and "sans mise, hors bankroll" in msgs[0], msgs[0]
        assert all(r.get("reported") for r in rows if r["result"] in ("win", "loss")) and app.fair_combos_report(datetime(2026, 10, 9, 9, tzinfo=UTC)) == []
        bil = app.handle_command("/bilan")
        assert "Combinés à vérifier chez 1xBet : " in bil and " sur " in bil, bil
        assert "Combinés à vérifier chez 1xBet" in app.handle_command("/bilan détail")
        assert bankroll.load()["bankroll"] == 100.0, "aucun combiné à cote juste dans la bankroll"
        assert not any("fair" in c.get("tier", "") for c in store.load("bets.json", {"combos": []})["combos"])
    print("matin complet avec stats mondiales et combinés à cote juste : OK")


def test_paid_ranking_favours_profiles():
    """À rang égal, les ligues payées en cotes réelles sont celles où Kelly a des profils (football-data ou API-Football) : leurs
    corners et cartons sont comparables aux cotes."""
    from kelly import apistats, catalogue
    counts = {"soccer_usa_mls": 4, "soccer_argentina_primera_division": 1, "soccer_epl": 3}
    assert catalogue.prioritise(counts) == ["soccer_epl", "soccer_usa_mls", "soccer_argentina_primera_division"]
    assert catalogue.prioritise(counts, covered={"soccer_argentina_primera_division"}) == \
        ["soccer_epl", "soccer_argentina_primera_division", "soccer_usa_mls"]
    assert catalogue.prioritise({"soccer_epl": 1, "soccer_usa_mls": 9}, covered={"soccer_usa_mls"})[0] == "soccer_epl"   # la région passe avant
    with Sandbox():
        rows = [[261001 + k, 0, k % 2, 1, 1, 12, 11, 4, 4, 5, 5, 13, 13, 2, 2] for k in range(8)]           # 8 matchs (format de apistats.record)
        apistats.save({"teams": {str(i): {"nm": n, "sk": ["soccer_argentina_primera_division"], "d": stats._today(), "lg": "Primera argentine",
                                          "m": rows} for i, n in ((1, "Boca Juniors"), (2, "River Plate"))}, "lg": ["Primera argentine"]})
        features.reload()
        ev = lambda k, h, a: {"id": h, "sport_key": k, "home_team": h, "away_team": a, "commence_time": f"{WDAY}T22:00:00Z"}     # noqa: E731
        scan = {"leagues": {"soccer_argentina_primera_division": [ev("soccer_argentina_primera_division", "Boca Juniors", "River Plate")],
                            "soccer_usa_mls": [ev("soccer_usa_mls", "Inter Miami", "LA Galaxy")], "soccer_epl": [{"id": "x"}]}}
        assert odds.covered_leagues(scan) == {"soccer_argentina_primera_division"}
        apistats.save({"teams": {str(i): {"nm": n, "sk": ["soccer_argentina_primera_division"], "d": stats._today(), "lg": "Primera argentine",
                                          "m": rows[:4]} for i, n in ((1, "Boca Juniors"), (2, "River Plate"))}, "lg": ["Primera argentine"]})
        features.reload()
        assert odds.covered_leagues(scan) == set(), "4 matchs : profil utilisable mais pas assez bon pour dépenser des crédits"
    features.reload()
    print("classement des ligues payées : OK")


class saved_files:
    """Sauvegarde puis restaure des fichiers de data/ autour d'un test qui les réécrit."""

    def __init__(self, *names):
        self.names = names

    def __enter__(self):
        self.old = {n: store.load(n, None) for n in self.names}

    def __exit__(self, *exc):
        for n, v in self.old.items():
            if v is None:
                with contextlib.suppress(OSError):
                    store.remove(n)
            else:
                store.save(n, v)
        calib._memo.clear()


def test_played_bets():
    """/joue : note la cote et la mise obtenues chez 1xBet ; la bankroll passe en mode réel (seuls les paris notés comptent),
    le bilan donne le résultat réel et l'écart de cote 1xBet face à la cote typique."""
    files = ("bets.json", "tracked.json", "fair_combos.json", "bankroll.json", "today.json")
    with saved_files(*files):
        day = "2026-10-08"
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        store.save("today.json", {"day": day, "picks": [], "pronos": []})
        sur = {"tier": C.COMBOS[0][0], "keys": ["k1", "k2"], "odds": 5.0, "prob": 0.22, "stake": 10.0, "date": day, "result": None}
        costaud = {"tier": C.COMBOS[1][0], "keys": ["k4"], "odds": 13.0, "prob": 0.08, "stake": 5.0, "date": "2026-10-01",
                   "result": "loss"}
        store.save("bets.json", {"picks": [], "combos": [sur, costaud]})
        row = lambda k, sent, odds_, prob: {"key": k, "id": k, "date": day, "sent": sent, "odds": odds_, "prob": prob,  # noqa: E731
                                            "pick": f"Pari {k}", "result": None, "market": "h2h"}
        track.save([row("k1", True, 2.0, 0.5), row("k2", True, 2.5, 0.44), row("k3", True, 1.9, 0.55), row("k5", False, 3.0, 0.3)])
        legs = [{"key": "f1", "min_odds": 2.3, "result": None}, {"key": "f2", "min_odds": 2.3, "result": None}]
        store.save("fair_combos.json", [{"date": day, "tier": 5, "min_odds": 5.29, "prob": 0.2, "keys": ["f1", "f2"], "legs": legs,
                                         "result": None}])
        todo = app.handle_command("/joue")
        assert "/joue sur <cote>" in todo and "/joue pari <cote>" in todo and "/joue v5 <cote>" in todo and "pas noté" in todo, todo
        m = app.handle_command("/joue sûr 5,40")
        assert "Noté" in m and "5,40" in m and "+8 %" in m and "ne suit plus que les paris notés" in m, m
        assert bankroll.load()["real"] and store.load("bets.json", {})["combos"][0]["played"]["stake"] == 10.0
        m = app.handle_command("/joue sur 5,40 3")
        assert "mise 3 u" in m and "ne suit plus" not in m, m
        assert "Noté" in app.handle_command("/joue pari 2")
        assert next(r for r in track.load() if r["key"] == "k3")["played"] == {"odds": 2.0, "stake": 10.0,
                                                                               "at": next(r for r in track.load() if r["key"] == "k3")["played"]["at"]}
        assert "moins que la cote mini" in app.handle_command("/joue v5 5,0")
        assert "inconnu" in app.handle_command("/joue foot 2")
        assert "Pas de combiné Costaud" in app.handle_command("/joue costaud 13")     # celui du 1er octobre est déjà réglé
        assert "invalide" in app.handle_command("/joue sur abc") and "supérieure à 1" in app.handle_command("/joue sur 0,9")
        assert "✅" in app.handle_command("/joue")
        # règlement : seuls les paris notés font bouger la bankroll, à la cote de 1xBet
        bets = store.load("bets.json", {})
        bets["combos"][0] |= {"result": "win", "odds_final": 5.0}
        store.save("bets.json", bets)
        rows = track.load()
        next(r for r in rows if r["key"] == "k3")["result"] = "loss"
        track.save(rows)
        fc = fair.load_combos()
        fc[0]["result"] = "win"
        for leg_ in fc[0]["legs"]:
            leg_["result"] = "win"
        store.save("fair_combos.json", fc)
        bankroll.apply_results(bets["combos"])
        rows = track.load()
        bankroll.apply_results([r for r in rows if r.get("played")], played_only=True)
        bankroll.apply_results(fc, played_only=True)
        expected = 1000 + 3 * (5.4 - 1) - 10 + 1.0 * (5.0 - 1)
        assert abs(bankroll.load()["bankroll"] - round(expected, 2)) < 1e-6, bankroll.load()
        bankroll.apply_results(bets["combos"])
        assert abs(bankroll.load()["bankroll"] - round(expected, 2)) < 1e-6, "un pari n'est compté qu'une fois"
        store.save("bets.json", bets)
        assert "déjà réglé" in app.handle_command("/joue sur 6")
        res = app.combo_result(bets["combos"][0], {})
        assert "cote 5,40 chez 1xBet" in res and "+13,2 u" in res, res
        simple = settle.bilan(bets, rows=rows)
        assert "Joués chez 1xBet : 2 gagnés sur 3" in simple and "Cotes de 1xBet : +6,6 % en moyenne" in simple, simple
        detail = settle.bilan(bets, rows=rows, detail=True)
        assert "Écart de cote 1xBet par palier : Sûr +8 % (1), Pari du jour" in detail, detail
        assert "réglée" in app.handle_command("/bank 500") and bankroll.load()["real"]
        assert "paris notés avec /joue seulement" in bankroll.summary([])
    print("paris joués chez 1xBet (/joue) : OK")


def test_calibration_tracking():
    """Pronostics sans cote : les C.FAIR_TRACK meilleurs + jusqu'à C.FAIR_TRACK_FREE de marchés réglés au score, en silence ;
    cumul de fiabilité par famille gardé pour toujours, et correction des probas d'une famille trop sûre d'elle."""
    with saved_files("fair.json", "calib.json"):
        with contextlib.suppress(OSError):
            store.remove("calib.json")
        calib._memo.clear()
        assert calib.factor("alternate_totals_corners") == 1.0
        rows = [{"market": "alternate_totals_corners", "prob": 0.7, "p0": 0.7, "result": "win" if i < 55 else "loss"}
                for i in range(100)] + [{"market": "totals", "prob": 0.65, "result": "win"} for _ in range(10)]
        assert calib.add(rows) == 110 and calib.add(rows) == 0          # jamais compté deux fois
        calib._memo.clear()                                             # relu depuis le disque
        k = calib.factor("alternate_totals_corners")
        assert abs(k - 0.75) < 1e-9 and calib.factor("totals") == 1.0, k  # 10 pronostics : trop peu pour corriger
        assert abs(calib.calibrate("alternate_totals_corners", 0.7) - 0.65) < 1e-9
        assert calib.lines() == ["corners : 55 % réalisés pour 70 % annoncés (100)"], calib.lines()
        assert "correction k = 0.75" in calib.lines(detail=True)[0]
        ev = {"id": "e1", "home_team": "A", "away_team": "B", "sport_key": "soccer_epl", "commence_time": "2026-10-08T15:00:00Z"}
        c = fair.make_pick(ev, "alternate_totals_corners", "Over", 9.5, 0.7, "tag", "src")
        assert c["p0"] == 0.7 and abs(c["prob"] - 0.65) < 1e-9 and c["min_odds"] > round(1 / 0.7 * 1.05, 2), c
        assert fair.make_pick(ev, "totals", "Over", 2.5, 0.7, "tag", "src")["prob"] == 0.7
        store.save("fair.json", [])
        pronos = [{"key": f"p{i}", "id": f"e{i}", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-08T15:00:00Z",
                   "market": "alternate_totals_corners" if i % 2 else "totals", "name": "Over", "point": 2.5, "prob": 0.6, "p0": 0.6}
                  for i in range(40)]
        assert fair.record(pronos, "2026-10-08") == C.FAIR_TRACK + 14
        got = fair.load()
        assert len([r for r in got if not r.get("x")]) == C.FAIR_TRACK and all(r["market"] == "totals" for r in got if r.get("x"))
        text = settle.bilan({"picks": [], "combos": []}, rows=[{"result": "win", "sent": True, "odds": 2.0, "prob": 0.5,
                                                                 "date": "2026-10-08", "market": "h2h", "key": "z"}], detail=True)
        assert "Fiabilité par marché" in text and "corners : 55 % réalisés pour 70 % annoncés (100), correction k = 0.75" in text, text
    print("suivi élargi et fiabilité par marché : OK")


def test_combo_expectation():
    """Un combiné dont l'espérance (proba x cote) est sous COMBO_MIN_EV est rebâti avec les seules jambes de valeur positive,
    sinon le palier saute et le message du matin le dit."""
    tiers = [("🛡️ SÛR (cote 5 à 7)", 5.0, 7.0)]
    safe = [mkleg(i, 1.35, 0.70, edge=-0.055) for i in range(3)]        # jambes sûres payées avec la marge
    v1 = mkleg(10, 3.0, 0.35, edge=0.05)
    v2 = mkleg(11, 2.2, 0.475, edge=0.045)
    best = CB.best_combo(safe + [v1, v2], 5.0, 7.0)
    assert CB.ev(best) < 1 and "e10" in {c["id"] for c in best[0]}       # le plus probable : 2 sûres + v1, espérance 0,94
    rejected = []
    (_, combo), = CB.plan(safe + [v1, v2], tiers, rejected=rejected)
    assert combo and {c["id"] for c in combo[0]} == {"e10", "e11"} and CB.ev(combo) >= 1 and not rejected, combo
    rejected = []
    (_, combo), = CB.plan(safe + [v1], tiers, rejected=rejected)
    assert combo is None and rejected == [tiers[0][0]]
    assert "espérance négative" in CB.ev_line(rejected) and "Sûr" in CB.ev_line(rejected)
    old = C.COMBO_MIN_EV
    C.COMBO_MIN_EV = 0
    (_, combo), = CB.plan(safe + [v1], tiers)
    C.COMBO_MIN_EV = old
    assert combo and CB.ev(combo) < 1                                   # 0 = filtre désactivé
    msg = fmt.combo(tiers[0][0], combo[0], combo[1], combo[2], 2.0, hint="/joue sur <cote>")
    assert "Espérance −6,2 %" in msg and "/joue sur &lt;cote&gt;" in msg, msg
    print("espérance des combinés : OK")


def test_started_matches_excluded():
    """/combi et /top n'utilisent que les matchs pas encore commencés ; la suggestion de cote est atteignable et plus basse que la demande."""
    now = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
    items = [{"commence": "2026-10-08T17:00:00Z"}, {"commence": "2026-10-08T18:00:00Z"}, {"commence": "2026-10-08T20:30:00Z"}]
    assert _real_upcoming(items, now) == items[2:]
    with saved_files("bets.json"):
        store.save("bets.json", {"picks": [], "combos": []})
        legs = [mkleg(i, 1.8, 0.62, edge=0.05, commence="2026-10-08T20:00:00Z") for i in range(3)]
        msg = app.custom_combo(legs, [], 30, "2026-10-08")
        assert "jouables" in msg and "/combi 5,8" in msg and "/combi 10" not in msg, msg
        msg = app.custom_combo([], [], 10, "2026-10-08", started=True)
        assert msg.startswith("Plus de cotes réelles jouables") and "Réessaie demain : aujourd'hui" in msg, msg
    print("matchs commencés écartés de /combi et /top : OK")


class FakeTelegram:
    """Remplace telegram.api : enregistre les appels, renvoie des messages numérotés et les mises à jour `updates` à getUpdates."""

    def __init__(self, updates=()):
        self.calls, self.updates, self.n = [], list(updates), 100

    def __call__(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def test_buttons_and_free_text():
    """Menu fixe, bouton « ✅ Je l'ai joué » (appui -> question de cote -> « 5,40 2000 » -> noté, bouton remplacé), texte libre."""
    files = ("bets.json", "tracked.json", "fair_combos.json", "bankroll.json", "today.json", "pending.json", "telegram.json")
    old_api, old_env = telegram.api, {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    with saved_files(*files):
        day = "2026-10-08"
        bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
        store.save("today.json", {"day": day, "picks": [mkleg(1, 1.8, 0.6, home="Santos", away="Flamengo", event="Santos - Flamengo",
                                                                  commence="2026-10-08T22:30:00Z")], "pronos": []})
        store.save("bets.json", {"picks": [], "combos": [{"tier": C.COMBOS[0][0], "keys": ["k1"], "odds": 5.0, "prob": 0.22, "stake": 10.0,
                                                          "date": day, "result": None}]})
        track.save([])
        store.save("fair_combos.json", [])
        store.save("pending.json", {})
        # --- texte libre
        assert chat.route("📊 Mes résultats") == ("/bilan", None) and chat.route("🔝 Paris du jour") == ("/top", None)
        assert chat.route("🧾 À noter") == ("/joue", None) and chat.route("Bilan") == ("/bilan", None)
        assert chat.route("bilan détail 7")[0].startswith("/bilan détail 7") and chat.route("Combi 20") == ("/combi 20", None)
        assert chat.route("joué sûr 5,40") == ("/joue sûr 5,40", None) and chat.route("paris du jour") == ("/top", None)
        assert chat.route("/top") == ("/top", None) and chat.route("flamengo") == ("/analyse flamengo", None)
        cmd, reply = chat.route("🎯 Un combiné")
        assert cmd is None and [b["callback_data"] for b in reply.markup["inline_keyboard"][0]] == [f"combi|{t}" for t in chat.COMBI_TARGETS]
        cmd, reply = chat.route("blablabla xyz")
        assert cmd is None and "pas compris" in reply and reply.markup["keyboard"]
        assert "Appuie d'abord" in chat.route("5,40")[1]                    # une cote sans pari en attente
        aide = app.handle_command("aide")
        assert "mode d'emploi" in aide and [[b["text"] for b in row] for row in aide.markup["keyboard"]] == chat.MENU
        assert app.handle_command("bilan").markup["inline_keyboard"][0][0]["callback_data"] == "bilan|detail"
        todo = app.handle_command("🧾 À noter")
        assert todo.markup["inline_keyboard"][0][0]["callback_data"] == f"joue|sur|{day}" and "Sûr" in todo
        # --- bouton « ✅ Je l'ai joué » puis la cote, par le vrai circuit Telegram (poll)
        os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="42")
        fake = FakeTelegram([{"update_id": 1, "callback_query": {"id": "cb1", "data": f"joue|sur|{day}",
                                                                 "message": {"message_id": 7, "chat": {"id": 42}}}},
                             {"update_id": 2, "callback_query": {"id": "cb2", "data": f"joue|sur|{day}",
                                                                 "message": {"message_id": 8, "chat": {"id": 666}}}}])
        telegram.api = fake
        telegram.poll(app.handle_command, on_callback=chat.on_callback)
        assert [p["callback_query_id"] for m, p in fake.calls if m == "answerCallbackQuery"] == ["cb1", "cb2"]   # toujours acquittés
        assert len(fake.sent()) == 1 and "À quelle cote" in fake.sent()[0]["text"]          # le clic d'un autre chat est ignoré
        assert chat.get_pending()["msg"] == 7
        fake.updates = [{"update_id": 3, "message": {"text": "5,40 2000", "chat": {"id": 42}}}]
        telegram.poll(app.handle_command, on_callback=chat.on_callback)
        assert "Noté" in fake.sent()[-1]["text"] and chat.get_pending() is None
        c = store.load("bets.json", {})["combos"][0]
        assert c["played"]["odds"] == 5.4 and c["played"]["stake"] == 2000
        edit = [p for m, p in fake.calls if m == "editMessageReplyMarkup"]
        assert edit and edit[0]["message_id"] == 7 and edit[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Joué à 5,40"
        # --- attente expirée
        chat.set_pending("sur", day, 9)
        p = store.load("pending.json", {})
        p["at"] = (datetime.now(timezone.utc) - timedelta(minutes=chat.PENDING_MINUTES + 1)).isoformat(timespec="seconds")
        store.save("pending.json", p)
        assert chat.get_pending() is None and "Appuie d'abord" in chat.route("6")[1]
        # --- les messages du matin portent les boutons
        legs = [mkleg(i, 1.9, 0.6, edge=0.06) for i in range(6)]
        msgs, _, combos = app.build_day(legs, date(2026, 10, 8))
        assert msgs[0].markup["keyboard"] == chat.menu()["keyboard"]                       # en-tête : le menu fixe
        datas = [getattr(m, "markup", {}).get("inline_keyboard", [[{}]])[0][0].get("callback_data") for m in msgs[1:]]
        assert "joue|pari|2026-10-08" in datas and any(d and d.startswith("joue|sur|") for d in datas), datas
    telegram.api = old_api
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    print("boutons, menu et texte libre : OK")


def main():
    bankroll.save({"bankroll": 1000.0, "initial": 1000.0})
    events = app.load_sample()
    day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()

    # --- Journée 1 : envoi + suivi (envoyés + fantômes)
    msgs = app.run_daily(sample=True)
    assert any("Kelly —" in m for m in msgs) and any("mise conseillée" in m for m in msgs)
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
    text = settle.bilan(bets, detail=True)
    assert "Envoyés" in text and "Suivis en silence" in text and "Brier" in text and "CLV" in text, text
    simple = settle.bilan(bets)
    assert "Bilan" in simple and "Combinés</b> :" in simple and not re.search(r"Brier|CLV|rendement", simple), simple
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
    assert "Bilan" in app.handle_command("/bilan") and "BILAN" in app.handle_command("/bilan détail")
    assert "PSG" in app.handle_command("/match psg")
    # cote 3 et non plus 30 : la dé-marge de Shin retire plus de marge aux longshots que la proportionnelle, et ces données
    # synthétiques ont une marge proportionnelle (sans biais favori-outsider) : il n'y reste que 4 jambes de qualité (max 3,6)
    assert "COMBINÉ SUR MESURE" in app.handle_command("/combi 3")
    assert "meilleurs paris du jour" in app.handle_command("/top")
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
    test_catalogue_and_priority()
    test_budget_with_many_leagues()
    test_fair_pronostics()
    test_morning_always_useful()
    test_africa_and_fair_settlement()
    test_typical_price()
    test_adaptive_disagreement()
    test_quality_floor()
    test_poor_day_and_tiers()
    test_leg_reuse()
    test_fixed_odds_optimiser()
    test_odds_cache_and_zero_paid_calls()
    test_sources_cache_counters()
    test_messages_human()
    test_fair_stat_markets()
    test_fair_combos()
    test_played_bets()
    test_calibration_tracking()
    test_combo_expectation()
    test_started_matches_excluded()
    test_buttons_and_free_text()
    test_paid_ranking_favours_profiles()
    test_replay_oct8()
    test_run_daily_world()
    shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
    print("OK : tout le flux passe hors ligne.")


if __name__ == "__main__":
    main()
