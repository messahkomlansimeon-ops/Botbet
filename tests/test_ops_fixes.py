"""Corrections d'exploitation (hors ligne) : croissance des fichiers (journal live, équipes du live, s365), files bloquées par un élément en échec (365scores,
FotMob), plafonds de requêtes, coupons 1xBet (blocage, nouveaux essais), alertes (heures calmes, marché suspendu), /status, Telegram (délai de lecture),
horloge, profils. Faux réseau et faux Telegram, DATA_DIR temporaire. Lancer : python -m tests.test_ops_fixes"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from tests import test_s365 as T  # noqa: E402  (faux réseau, horloge simulée et fixtures 365scores ; fixe DATA_DIR et SOURCES_CACHE_DIR)

import requests  # noqa: E402

from kelly import cache as kcache, config as C, live, livedata, quiet, status, store, telegram, xalerts  # noqa: E402
from kelly.sources import features, fmmatch, fotmob, http, s365, state, xbet  # noqa: E402

NOW = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)
DAY = "2026-10-10"
SECRET = "pw-ultra-secret"


def ago(days, now=NOW):
    return str((now - timedelta(days=days)).date())


# ---------- 1. journal live ----------

def test_live_journal_bounded():
    store.save(live.JOURNAL, [])
    log = [{"key": "a", "tranche": "60-75", "date": ago(10), "result": None},             # sans résultat depuis 10 jours : oubliée
           {"key": "b", "tranche": "60-75", "date": ago(2), "result": None},              # sans résultat mais récente : gardée
           {"key": "c", "tranche": "60-75", "date": ago(40), "result": "win", "calib": True},      # réglée il y a plus de 30 jours : oubliée
           {"key": "d", "tranche": "60-75", "date": ago(10), "result": "loss", "calib": True}]    # réglée, dans la fenêtre : gardée
    store.save(live.JOURNAL, log)
    assert live.record([], NOW) == 0
    assert {r["key"] for r in store.load(live.JOURNAL, [])} == {"b", "d"}, store.load(live.JOURNAL, [])
    v = store.version(live.JOURNAL)
    assert live.record([], NOW) == 0 and store.version(live.JOURNAL) == v, "rien à ajouter ni à retirer : le document n'est pas réécrit"
    # ajout puis même ligne le même jour : une seule écriture
    row = {"key": "z", "id": "lv1", "tranche": "60-75", "minute": 63, "score": "1-0", "home": "A", "away": "B", "market": "totals", "name": "Over", "point": 1.5}
    assert live.record([row], NOW) == 1
    v = store.version(live.JOURNAL)
    assert live.record([row], NOW) == 0 and store.version(live.JOURNAL) == v
    # plafond du journal : les plus anciennes lignes d'abord
    old_max, live.MAX_ROWS = live.MAX_ROWS, 3
    try:
        store.save(live.JOURNAL, [{"key": f"k{i}", "tranche": "t", "date": ago(i % 3), "result": None} for i in range(10)])
        live.record([], NOW)
        kept = store.load(live.JOURNAL, [])
        assert len(kept) == 3 and all(r["date"] == ago(0) for r in kept), kept
    finally:
        live.MAX_ROWS = old_max
    # règlement : réécrit seulement si une ligne change
    from kelly import settle as S
    saved = S._settle_fair_row
    S._settle_fair_row = lambda r, now, fdata, stats_for=None: False
    try:
        store.save(live.JOURNAL, [{"key": "q", "tranche": "60-75", "date": ago(1), "result": None, "market": "totals"}])
        v = store.version(live.JOURNAL)
        assert live.settle(NOW, fdata=object()) == (0, 0) and store.version(live.JOURNAL) == v, "rien de réglé : pas de réécriture"
        store.save(live.JOURNAL, [{"key": "q", "tranche": "60-75", "date": ago(9), "result": None, "market": "totals"}])
        live.settle(NOW, fdata=object())
        assert store.load(live.JOURNAL, []) == [], "ligne jamais réglée de plus de 4 jours : élaguée"
    finally:
        S._settle_fair_row = saved
    print("ok  1 journal live élagué (sans résultat, réglé, plafond) et jamais réécrit pour rien")


# ---------- 2. équipes du live ----------

class _Res:
    def __init__(self, obj=None, ok=True, raw=None, url="u"):
        self.ok, self.error, self.stale, self.url = ok, None if ok else "HTTP 500", False, url
        self.content = raw if raw is not None else json.dumps(obj).encode()
        self.status = 200 if ok else 500


class _NoProxy:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def _prep_world(results_ok=True, fetched=None, wire=None):
    """Remplace http.fetch et s365._via_proxy par des faux (liste du jour : 6 matchs de 12 équipes ; résultats de chaque équipe)."""
    games = [{"id": 100 + i, "competitionId": 1, "startTime": "2026-10-10T16:00:00+00:00", "homeCompetitor": {"id": 1000 + 2 * i, "name": f"Home{i}"},
              "awayCompetitor": {"id": 1001 + 2 * i, "name": f"Away{i}"}} for i in range(6)]
    day_list = {"competitions": [{"id": 1, "name": "Liga 2"}], "games": games}
    results = {"games": [{"startTime": f"2026-10-0{i}T15:00:00+00:00", "homeCompetitor": {"id": 1000, "score": 2.0}, "awayCompetitor": {"id": 9, "score": 1.0}}
                         for i in range(1, 9)]}
    calls = [] if fetched is None else fetched

    def fake_fetch(url, name, ttl, **k):
        calls.append(url)
        w = http.WIRE.setdefault(s365.HOST, {"bytes": 0, "requests": 0})
        w["requests"] += 1
        w["bytes"] += 4000
        if "allscores" in url:
            return _Res(day_list, url=url)
        return _Res(results, url=url) if results_ok else _Res(ok=False, url=url)
    return fake_fetch, calls


def test_livedata_bounds():
    T.fresh(serve=False)
    old = (http.fetch, s365._via_proxy, http.invalidate)
    s365._via_proxy = lambda settle=False: _NoProxy()
    try:
        # purge : équipe non relue depuis 60 jours oubliée (date naïve ou non), une récente gardée
        d = {"lists": {}, "teams": {"1": {"name": "Old", "at": "2026-07-01T10:00"}, "2": {"name": "Fresh", "at": "2026-10-09T10:00+00:00"},
                                    "3": {"name": "NoDate"}}, "fail": {}}
        assert livedata.purge(d, NOW) == 2 and set(d["teams"]) == {"2"}
        # un passage : les équipes de la liste sont lues, la vieille équipe est oubliée, puis un second passage ne réécrit RIEN
        fetch, calls = _prep_world()
        http.fetch = fetch
        store.save(livedata.FILE, {"lists": {}, "teams": {"999": {"name": "Old", "comp": "", "at": "2026-07-01T10:00+00:00", "res": [["2026-06-30", 1, 0, 1]] * 5}}})
        rep = livedata.prepare(now=NOW)
        db = livedata.load()
        assert rep["teams"] == 12 and "999" not in db["teams"] and len(db["teams"]) == 12, (rep, sorted(db["teams"]))
        assert db["req"]["day"] == DAY and db["req"]["n"] >= 13, db["req"]
        v, n = store.version(livedata.FILE), len(calls)
        rep2 = livedata.prepare(now=NOW + timedelta(minutes=5))
        assert rep2["teams"] == 0 and len(calls) == n and store.version(livedata.FILE) == v, "rien à faire : ni requête ni écriture"
        # watch : les listes seules, sans effacer des équipes ajoutées entre-temps ; rien d'écrit si les listes n'ont pas changé
        stale = livedata.load()
        fresh_doc = livedata.load()
        fresh_doc["teams"]["5000"] = {"name": "Added meanwhile", "comp": "", "at": "2026-10-10T10:00+00:00", "res": []}
        store.save(livedata.FILE, fresh_doc)
        assert livedata.save_lists(stale) is False, "mêmes listes : pas d'écriture"
        stale["lists"]["2026-10-09"] = {"at": "2026-10-09T10:00+00:00", "games": []}
        assert livedata.save_lists(stale) is True
        after = livedata.load()
        assert "5000" in after["teams"] and "2026-10-09" in after["lists"], "les équipes ajoutées entre-temps sont gardées"
        # disjoncteur : 3 erreurs de suite arrêtent le passage ; les équipes en échec sont mises de côté au passage suivant
        store.save(livedata.FILE, {"lists": {}, "teams": {}})
        fetch, calls = _prep_world(results_ok=False)
        http.fetch = fetch
        rep = livedata.prepare(now=NOW)
        assert rep["teams"] == 0 and "erreurs de suite" in rep.get("stopped", ""), rep
        assert sum("results" in c for c in calls) == livedata.MAX_ERRORS, calls
        n = sum("results" in c for c in calls)
        livedata.prepare(now=NOW + timedelta(minutes=30))
        again = [c for c in calls if "results" in c][n:]
        assert len(again) == livedata.MAX_ERRORS and not set(again) & set(c for c in calls[:n] if "results" in c), "les 3 équipes en échec attendent"
        # une réponse 200 qui n'est pas du JSON est retirée du cache
        dropped = []
        http.invalidate = lambda url, headers=None: dropped.append(url)
        http.fetch = lambda url, name, ttl, **k: _Res(raw=b"<html>maintenance</html>", url=url)
        assert livedata._results(s365, 1234) is None and any("competitors=1234" in u for u in dropped), dropped
        dropped.clear()
        assert livedata._day_list(s365, {"lists": {}}, datetime(2026, 10, 10), NOW) == [] and any("allscores" in u for u in dropped), dropped
        # plafond du jour : requêtes du live comptées avec celles de 365scores
        http.invalidate = old[2]
        store.save(livedata.FILE, {"lists": {}, "teams": {}})
        fetch, calls = _prep_world()
        http.fetch = fetch
        saved_cfg = (C.S365_DAILY, livedata.DAILY_EXTRA)
        C.S365_DAILY, livedata.DAILY_EXTRA = 5, 0
        try:
            rep = livedata.prepare(now=NOW)
        finally:
            C.S365_DAILY, livedata.DAILY_EXTRA = saved_cfg
        assert rep["teams"] == 4 and "plafond du jour" in rep.get("stopped", ""), rep       # 1 liste + 4 équipes = 5 requêtes
        # plafond du mois relu PENDANT le passage
        store.save(livedata.FILE, {"lists": {}, "teams": {}})
        fetch, calls = _prep_world()
        http.fetch = fetch
        s365._BUDGET.update(limit=30_000, base=0, wire0=http.WIRE.get(s365.HOST, {"bytes": 0})["bytes"])
        try:
            rep = livedata.prepare(now=NOW)
        finally:
            s365._BUDGET.clear()
        assert 0 < rep["teams"] < 12 and "plafond mensuel" in rep.get("stopped", ""), rep
    finally:
        http.fetch, s365._via_proxy, http.invalidate = old
    print("ok  2 équipes du live : purge, écritures évitées, disjoncteur, 200 non-JSON invalidé, plafonds du jour et du mois")


def test_livedata_homonyms_and_speed():
    teams = {"1": {"name": "Dinamo", "comp": "Romania. Liga 1", "res": [[1, 0, 1]] * 6}, "2": {"name": "Dinamo", "comp": "Croatia. HNL", "res": [[0, 0, 1]] * 6},
             "3": {"name": "Zeta Rovers", "comp": "Wales. Cymru", "res": [[1, 1, 1]] * 6}}
    assert livedata._team("Dinamo", teams) is None, "deux Dinamo de pays différents : jamais mélangés"
    assert livedata._team("Dinamo", teams, "Croatia. HNL") is teams["2"] and livedata._team("Dinamo", teams, "Romania. Liga 1") is teams["1"]
    assert livedata._team("Dinamo", teams, "Spain. La Liga") is None
    assert livedata._team("Zeta Rovers", teams) is teams["3"] and livedata._team("zeta rovers fc", teams) is teams["3"]
    assert livedata._team("Totally Unknown", teams) is None
    # 10 000 équipes : une recherche ne compare plus tous les noms
    big = {str(i): {"name": f"Club{i} Sporting", "comp": "X", "res": []} for i in range(10000)}
    big["77777"] = {"name": "Real Alpha Zulu", "comp": "Y", "res": []}
    idx = livedata._index(big)
    t0 = time.perf_counter()
    for _ in range(20):
        assert livedata._team("Real Alpha Zulu", big, index=idx) is big["77777"]
        assert livedata._team("Alpha Zulu", big, index=idx) is big["77777"]
        assert livedata._team("Nothing Matches Here", big, index=idx) is None
    per = (time.perf_counter() - t0) / 60
    assert per < 0.05, f"{per:.3f} s par recherche"
    print(f"ok  2b homonymes refusés ou départagés par le championnat, recherche indexée ({per * 1000:.1f} ms avec 10 000 équipes)")


# ---------- 3. s365 : « vus » bornés ----------

def test_s365_seen_bounded():
    T.fresh(serve=False)
    d = s365.load()
    today = s365._today()
    d2i = lambda n: s365._d2i(str(today - timedelta(days=n)))      # noqa: E731
    d["seen"] = {"old": d2i(200), "stale": d2i(90), "window": d2i(20), "today": d2i(0)}
    s365.save(d)
    assert set(s365.load()["seen"]) == {"window", "today"}, "au-delà de la fenêtre + marge, un match vu n'est plus utile (il n'est plus jamais demandé)"
    old_max, s365.MAX_SEEN = s365.MAX_SEEN, 3
    try:
        d = s365.load()
        d["seen"] = {f"g{i}": d2i(i) for i in range(10)}
        s365.save(d)
        assert set(s365.load()["seen"]) == {"g0", "g1", "g2"}
    finally:
        s365.MAX_SEEN = old_max
    # la garantie « jamais redemandé » vaut sur toute la fenêtre : un match lu il y a 20 jours n'est pas redemandé par une liste qui le contient encore
    net = T.fresh()
    s365.fill(max_lists=1, max_games=10, leagues=[T.UR])
    seen_before = set(s365.load()["seen"])
    assert seen_before
    net.log.clear()
    T.CLOCK.t += 21 * T.HOUR
    s365.fill(max_lists=1, max_games=10, leagues=[T.UR])
    asked = {e[0].split("games=")[1].split("&")[0] for e in net.data() if "/game/stats/" in e[0]}
    assert not asked & seen_before, f"match déjà lu redemandé : {asked & seen_before}"
    print("ok  3 s365 : « vus » bornés par âge et par nombre, sans redemander un match de la fenêtre")


# ---------- 4. s365 : élément en échec ----------

def test_s365_poison_pill():
    net = T.fresh()
    s365.fill(max_lists=1, max_games=0, leagues=[T.UR])
    order = s365.targets(s365.load(), [T.UR])
    poison = order[0]
    net.routes[s365.stats_url(int(poison))] = T.Resp(404, b"nope")
    reps = []
    for i in range(3):
        reps.append(s365.fill(max_lists=0, max_games=2, leagues=[T.UR]))
        d = s365.load()
        if i < 2:
            assert d["pending"][poison]["f"] == i + 1, d["pending"][poison]
            net.log.clear()
            again = s365.fill(max_lists=0, max_games=2, leagues=[T.UR])                 # tout de suite : le match en échec attend (écart de 6 h)
            assert not [e for e in net.data() if s365.stats_url(int(poison)) == e[0]], "pas redemandé avant l'écart"
        T.CLOCK.t += 7 * T.HOUR
    d = s365.load()
    assert poison in d["seen"] and poison not in d["pending"], "3 échecs espacés : mis de côté"
    # les autres matchs ont avancé (la file n'est plus bloquée) : plusieurs matchs lus malgré le premier en échec
    assert len(d["seen"]) >= 4 and poison not in d["recent"] and all(t["m"] for t in d["teams"].values()), (d["seen"], d["recent"].keys())
    # panne générale (rien ne répond) : aucun compteur d'échec, la file n'est pas décimée
    net = T.fresh()
    s365.fill(max_lists=1, max_games=0, leagues=[T.UR])
    for g in list(s365.load()["pending"]):
        net.routes[s365.stats_url(int(g))] = requests.ConnectionError("boom")
    for _ in range(4):
        s365.fill(max_lists=0, max_games=3, leagues=[T.UR])
        T.CLOCK.t += 7 * T.HOUR
    d = s365.load()
    assert d["seen"] == {} and all("f" not in p for p in d["pending"].values()), "réseau coupé : rien n'est compté contre les matchs"
    # liste de résultats en échec : pas réessayée tout de suite, jamais abandonnée
    net = T.fresh(serve=False)
    net.routes[s365.list_url(T.UR)] = T.Resp(404, b"nope")
    s365.fill(max_lists=1, max_games=0, leagues=[T.UR])
    n = len(net.data())
    s365.fill(max_lists=1, max_games=0, leagues=[T.UR])
    assert len(net.data()) == n, "liste en échec : attend"
    T.CLOCK.t += 7 * T.HOUR
    net.routes[s365.list_url(T.UR)] = T.Resp(200, T.fx("results_uruguay.json"))
    assert s365.fill(max_lists=1, max_games=0, leagues=[T.UR])["lists"] == 1 and T.UR in s365.load()["lists"]
    print("ok  4 s365 : compteur d'échecs durable par match (espacé, puis mis de côté), panne générale non comptée")


# ---------- 5. FotMob : élément en échec, retard des statistiques, page sans stats ----------

def _league_html(data):
    inner = json.dumps({"props": {"pageProps": data}}, ensure_ascii=False).replace("</", "<\\/")
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{inner}</script></body></html>'.encode()


def test_fotmob_failures_and_negative_cache():
    from tests import test_sources as TS
    net = T.fresh(serve=False)
    league = json.loads(TS.fixture("fotmob_league47.json"))
    done = [x for x in league["fixtures"]["allMatches"] if x["status"].get("finished")]
    today = state.now().date()
    poison, fresh_nostats, old_nostats = done[0], done[1], done[2]
    fresh_nostats["status"]["utcTime"] = f"{today - timedelta(days=1)}T19:00:00.000Z"       # hier : ses statistiques peuvent encore arriver
    net.routes["https://www.fotmob.com/robots.txt"] = T.Resp(404)
    net.routes["https://www.fotmob.com/leagues/47"] = T.Resp(200, _league_html(league))
    url = lambda x: f"https://www.fotmob.com{x['pageUrl'].split('#')[0]}"       # noqa: E731
    for x in done:
        net.routes[url(x)] = T.Resp(200, TS._fm_match_html(x))
    net.routes[url(poison)] = T.Resp(404, b"gone")                                 # ne répond jamais
    net.routes[url(fresh_nostats)] = T.Resp(200, b"<html>pas encore de stats</html>")
    net.routes[url(old_nostats)] = T.Resp(200, b"<html>jamais de stats</html>")
    fotmob.refresh(["soccer_epl"])
    fmmatch._neg.clear()
    cnt = lambda x: len([e for e in net.log if e[0] == url(x)])        # noqa: E731
    rep = fmmatch.fill(max_pages=200, seconds=6000)
    d = fmmatch.load()
    assert rep["added"] == len(done) - 3, rep
    assert cnt(poison) == 1 and d["retry"][str(poison["id"])][0] == 1 and str(poison["id"]) not in d["seen"]
    assert str(fresh_nostats["id"]) not in d["seen"] and d["retry"][str(fresh_nostats["id"])][0] == 1, "match d'hier sans stats : réessayé plus tard"
    assert str(old_nostats["id"]) in d["seen"], "match ancien sans statistiques : vu pour de bon"
    fmmatch.fill(max_pages=200, seconds=6000)
    assert cnt(poison) == 1 and cnt(fresh_nostats) == 1 and cnt(old_nostats) == 1, "pas retéléchargées avant l'écart"
    for i in range(2):
        T.CLOCK.t += 4 * T.HOUR
        fmmatch.fill(max_pages=200, seconds=6000)
    d = fmmatch.load()
    assert cnt(poison) == 3 and cnt(fresh_nostats) == 3, (cnt(poison), cnt(fresh_nostats))
    assert str(poison["id"]) in d["seen"] and str(fresh_nostats["id"]) in d["seen"], "3 essais espacés : mis de côté"
    T.CLOCK.t += 4 * T.HOUR
    fmmatch.fill(max_pages=200, seconds=6000)
    assert cnt(poison) == 3 and cnt(fresh_nostats) == 3
    # match_stats (règlement) : la même page sans statistiques n'est téléchargée qu'une fois pour tous les paris, puis pas avant l'expiration de la marque
    net = T.fresh(serve=False)
    net.routes["https://www.fotmob.com/robots.txt"] = T.Resp(404)
    net.routes["https://www.fotmob.com/leagues/47"] = T.Resp(200, _league_html(league))
    for x in done:
        net.routes[url(x)] = T.Resp(200, TS._fm_match_html(x))
    target = done[3]
    net.routes[url(target)] = T.Resp(200, b"<html>sans stats</html>")
    fotmob.refresh(["soccer_epl"])
    fmmatch._neg.clear()
    fmmatch._settle_pages["n"] = 0
    b = {"home": target["home"]["name"], "away": target["away"]["name"], "commence": target["status"]["utcTime"], "sport_key": "soccer_epl",
         "market": "team_throws_total", "name": "Over", "point": 30.5, "group": "soccer", "who": None, "id": "z"}
    for _ in range(5):                                                        # cinq paris sur le même match
        assert fmmatch.match_stats(b) is None
    assert cnt(target) == 1, cnt(target)
    fmmatch._neg.clear()                                                      # nouvelle exécution : la marque durable garde la page
    fmmatch._settle_pages["n"] = 0
    assert fmmatch.match_stats(b) is None and cnt(target) == 1
    T.CLOCK.t += (fmmatch.RETRY_KEEP_DAYS + 1) * 86400
    fmmatch._neg.clear()
    assert fmmatch.match_stats(b) is None and cnt(target) == 2, "marque expirée : un nouvel essai"
    # parse_stats / parse_match : une page où tout vaut zéro n'est pas un match suivi
    assert fmmatch.parse_match(TS._fm_match_html(done[0]).decode()) is not None
    print("ok  5 FotMob : page en échec et statistiques tardives réessayées (espacées), page sans stats non retéléchargée")


# ---------- 6. 365scores : statistiques toutes à zéro ----------

def test_s365_all_zero_stats_rejected():
    p = T.fxj("stats_uruguay_4861907.json")
    assert s365.parse_stats(p) is not None
    zero = json.loads(json.dumps(p))
    for st in zero["statistics"]:
        st["value"] = "0"
    assert s365.parse_stats(zero) is None, "que des zéros : match non suivi"
    only = json.loads(json.dumps(p))
    only["statistics"] = [s for s in only["statistics"] if s["name"] in ("Corners", "Shots On Target", "Offsides")]
    for st in only["statistics"]:
        st["value"] = "0"
    assert s365.parse_stats(only) is None, "corners, tirs cadrés et hors-jeu à 0-0 : zéros d'un match non suivi"
    only["statistics"][0]["value"] = "4"
    assert s365.parse_stats(only) is not None, "un volume non nul suffit"
    print("ok  6 365scores : un match dont toutes les statistiques valent 0 est refusé")


# ---------- 7 / 8. mots de passe du proxy ----------

def test_proxy_secrets_masked():
    url = f"http://user-GB-20:{SECRET}@proxy.test:80"
    http.PROXIES["webws.365scores.com"] = url
    try:
        assert SECRET not in http.scrub(f"boom {url} fin") and SECRET not in http.err(RuntimeError(f"Cannot connect to proxy {url}"))
        assert "RuntimeError" in http.err(RuntimeError(f"Cannot connect to proxy {url}"))
        assert SECRET not in http.scrub(f"HTTPSConnectionPool: user-GB-20:{SECRET}@proxy.test failed")
        assert http.scrub("réseau indisponible (ReadTimeout)") == "réseau indisponible (ReadTimeout)"
    finally:
        http.PROXIES.pop("webws.365scores.com", None)
    # une erreur interne du remplissage n'écrit jamais le mot de passe dans le rapport
    T.fresh()
    saved = s365.targets

    def boom(*a, **k):
        raise RuntimeError(f"Cannot connect to proxy {url}")
    s365.targets = boom
    try:
        rep = s365.fill(max_lists=0, max_games=1, leagues=[T.UR])
    finally:
        s365.targets = saved
    assert rep["errors"] and SECRET not in " ".join(rep["errors"]) and "erreur interne" in rep["errors"][0], rep["errors"]
    # livedata : la raison d'un échec ne contient jamais le mot de passe
    saved_load = livedata.load

    def bad_load():
        raise RuntimeError(f"proxy {url}")
    livedata.load = bad_load
    old = s365._via_proxy
    s365._via_proxy = lambda settle=False: _NoProxy()
    try:
        rep = livedata.prepare(now=NOW)
    finally:
        livedata.load, s365._via_proxy = saved_load, old
    assert rep["skipped"] and SECRET not in rep["skipped"], rep
    # reserve du règlement : le remplissage s'arrête avant le plafond du mois, le règlement peut aller jusqu'au bout
    T.fresh(serve=False)
    os.environ["XBET_PROXY"] = url
    saved_cfg = (C.S365_PROXY, C.S365_MONTH_MB)
    C.S365_PROXY, C.S365_MONTH_MB = True, 100.0
    try:
        store.save(s365.USAGE, {"month": str(state.now().date())[:7], "bytes": int(90e6), "requests": 5, "days": {}})
        with s365._via_proxy() as why:
            assert why and "plafond mensuel" in why and SECRET not in why, why
        with s365._via_proxy(settle=True) as why:
            assert why is None, "le règlement garde sa réserve"
            assert 9e6 - 1 < s365.month_left() <= 10e6
        assert s365.month_left() is None
    finally:
        C.S365_PROXY, C.S365_MONTH_MB = saved_cfg
        os.environ.pop("XBET_PROXY", None)
        http.PROXIES.pop(s365.HOST, None)
    print("ok  7/8 mots de passe du proxy masqués, réserve du mois pour le règlement, plafond relu pendant le passage")


# ---------- 9 / 10. coupon 1xBet ----------

class _Ses:
    why = None

    def call(self, fn, *a, **k):
        return fn(*a, **k)

    def close(self, what):
        pass


def test_coupon_blocking_and_retries():
    T.fresh(serve=False)
    state.clock = T.CLOCK
    url = xbet.BASE + "/service-api/LiveBet/Open/SaveCoupon"
    posted = []
    answers = []

    def fake_post(u, headers=None, json=None, timeout=None, **kw):
        posted.append(u)
        return answers.pop(0) if len(answers) > 1 else answers[0]
    saved = (requests.post, xbet._Session, xbet.time.sleep)
    requests.post, xbet._Session = fake_post, lambda: _Ses()
    xbet.time.sleep = lambda s: None
    legs = [{"xodds": 1.68, "xref": [1, 10, 3.5]}, {"xodds": 1.36, "xref": [2, 10, 3.5]}]
    try:
        # 403 : la source est coupée 24 h, le code n'est pas redemandé, le journal dit le code HTTP
        answers[:] = [T.Resp(403, b"<html>forbidden</html>")]
        r = http.post(url, {"a": 1})
        assert r.blocked and r.status == 403 and not r.ok and state.is_blocked("xbet"), r
        n = len(posted)
        assert http.post(url, {"a": 1}).blocked and len(posted) == n, "source coupée : plus aucune requête"
        assert xbet.save_coupon(legs) is None and len(posted) == n
        for code in (429,):
            store.save(state.FILE, {})
            answers[:] = [T.Resp(code, b"")]
            assert http.post(url, {"a": 1}).blocked and state.is_blocked("xbet")
        store.save(state.FILE, {})
        answers[:] = [T.Resp(200, b"<html><title>Just a moment...</title></html>")]
        assert http.post(url, {"a": 1}).blocked and state.is_blocked("xbet")
        store.save(state.FILE, {})
        answers[:] = [T.Resp(200, b"{}", {"cf-mitigated": "challenge"})]
        assert http.post(url, {"a": 1}).blocked
        # une réponse JSON normale n'est jamais prise pour une page de vérification
        store.save(state.FILE, {})
        answers[:] = [T.Resp(200, json.dumps({"Success": True, "Value": "OKCODE"}).encode())]
        assert http.post(url, {"a": 1}).ok and not state.is_blocked("xbet")
        # 400 : le code HTTP est dit (et la source n'est pas coupée)
        answers[:] = [T.Resp(400, b"Bad request")]
        r = http.post(url, {"a": 1})
        assert r.status == 400 and r.error == "HTTP 400" and not state.is_blocked("xbet")
        # coupon : 403 -> un seul POST (aucune insistance)
        store.save(state.FILE, {})
        posted.clear()
        answers[:] = [T.Resp(403, b"")]
        assert xbet.save_coupon(legs) is None and len(posted) == 1 and state.is_blocked("xbet")
        # refus : seul le code passager connu est réessayé ; un Success vrai n'est jamais rejoué
        store.save(state.FILE, {})
        posted.clear()
        answers[:] = [T.Resp(200, json.dumps({"Success": False, "Error": "No", "ErrorCode": 99}).encode())]
        assert xbet.save_coupon(legs) is None and len(posted) == 1
        posted.clear()
        answers[:] = [T.Resp(200, json.dumps({"Success": False, "Error": "x", "ErrorCode": 161627}).encode())]
        assert xbet.save_coupon(legs) is None and len(posted) == 1 + len(xbet.COUPON_BACKOFF)
        posted.clear()
        answers[:] = [T.Resp(200, json.dumps({"Success": True, "Value": None}).encode())]
        assert xbet.save_coupon(legs) is None and len(posted) == 1
        posted.clear()
        answers[:] = [T.Resp(200, b"<html>pas du json</html>")]
        assert xbet.save_coupon(legs) is None and len(posted) == 1
    finally:
        requests.post, xbet._Session, xbet.time.sleep = saved
        store.save(state.FILE, {})
    print("ok  9/10 POST coupon : 403 / 429 / vérification coupent la source, nouveaux essais seulement pour le refus passager connu")


# ---------- 11. heures calmes ----------

def test_quiet_drops_perishable_alerts():
    held = []
    saved = (telegram.send, C.QUIET_HOURS)
    sent = []
    telegram.send = lambda msg, *a, **k: sent.append(str(msg)) or True
    store.save(quiet.FILE, [])
    store.save(xalerts.FILE, {})
    items = [xalerts._item(k, f"key-{k}", 2.0, f"texte {k}", rank=1.0) for k in ("lag", "rise", "danger", "good")]
    try:
        C.QUIET_HOURS = "23-7"
        night = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)
        msg = xalerts.send(items, DAY, night)
        held = store.load(quiet.FILE, [])
        assert len(held) == 1 and "texte danger" in held[0]["text"] and "texte good" in held[0]["text"], held
        assert "texte lag" not in held[0]["text"] and "texte rise" not in held[0]["text"], "périmées au réveil : abandonnées"
        assert msg is not None and not sent
        assert not any(k.startswith(("key-lag", "key-rise")) for k in store.load(xalerts.FILE, {}).get("items", {})), "pas retenues comme envoyées"
        # que des alertes périmées la nuit : rien n'est gardé
        store.save(quiet.FILE, [])
        assert xalerts.send(items[:2], DAY, night) is None and store.load(quiet.FILE, []) == []
        # au réveil, les gardées partent ; le jour, tout part tout de suite
        assert quiet.flush(datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)) == 0
        store.save(quiet.FILE, held)
        assert quiet.flush(datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)) == 1 and any("texte danger" in s for s in sent)
        sent.clear()
        store.save(xalerts.FILE, {})
        xalerts.send(items, DAY, datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc))
        assert len(sent) == 1 and all(f"texte {k}" in sent[0] for k in ("lag", "rise", "danger", "good"))
    finally:
        telegram.send, C.QUIET_HOURS = saved
        store.save(quiet.FILE, [])
    print("ok  11 alertes de nuit : 💰 joue maintenant et 📈 abandonnées, les autres gardées pour le réveil")


# ---------- 12. marché suspendu ----------

def test_suspended_market_is_not_gone():
    T.fresh(serve=False)
    state.clock = T.CLOCK
    u = xbet._events_url(88801)
    body = json.dumps({"eventGroups": [{"events": [[{"type": 1, "cf": 1.52, "parameter": 0, "blocked": True}, {"type": 2, "cf": 3.4}]]}]}).encode()
    http._write(http._key(u, xbet.JSON_H), {"url": u, "fetched_at": state.clock(), "status": 200}, body)
    blocked, absent, ok = xbet.cached_cell([88801, 1, 0]), xbet.cached_cell([88801, 3, 0]), xbet.cached_cell([88801, 2, 0])
    assert blocked.blocked and blocked.cf == 1.52 and blocked[0] == 1.52, blocked
    assert absent.cf is None and not absent.blocked, absent
    assert ok.cf == 3.4 and not ok.blocked
    now = NOW
    c = {"key": "k1", "xref": [88801, 1, 0], "market": "h2h", "name": "A", "point": None}
    prices = xalerts.Prices(xalerts.Reader(lambda *a, **k: {}), now)
    assert prices.get(c) is None, "suspendu : inconnu, pas « retiré »"
    assert xalerts.Prices(xalerts.Reader(lambda *a, **k: {}), now).get(c | {"key": "k2", "xref": [88801, 3, 0]})[0] == "gone", "vraiment absent : retiré"
    # relecture en direct : parse_main sépare le suspendu de l'absent
    data = {"eventGroups": [{"events": [[{"type": 1, "cf": 1.9, "blocked": True}, {"type": 2, "cf": 3.1}, {"type": 9, "cf": 1.7, "parameter": 2.5, "blocked": True}]]}]}
    blk = set()
    px, _ = xbet.parse_main(data, "A", "B", {}, 1, blk)
    assert ("h2h", "A", None) in blk and ("totals", "Over", 2.5) in blk and ("h2h", "A", None) not in px and px[("h2h", "Draw", None)] == 3.1, (px, blk)
    m = {"px": px, "refs": {}, "blocked": blk}
    assert xalerts._suspended(m, {"market": "h2h", "name": "A", "point": None}) and not xalerts._suspended(m, {"market": "h2h", "name": "B", "point": None})
    live_prices = xalerts.Prices(type("R", (), {"get": lambda self, c: m})(), now)
    assert live_prices.get({"key": "k3", "market": "h2h", "name": "A", "point": None}) is None
    assert live_prices.get({"key": "k4", "market": "h2h", "name": "B", "point": None})[0] == "gone"
    print("ok  12 marché suspendu ≠ marché retiré (cache et relecture en direct)")


# ---------- 13. /status ----------

def test_status_odds_is_cheap():
    from kelly import app, odds
    saved = (app._check_odds, app._check_stats, app._check_telegram_bot, odds.get, dict(odds.CREDITS), os.environ.get("ODDS_API_KEY"))
    calls = []

    def boom():
        raise AssertionError("app._check_odds (tour des ligues) ne doit pas être appelé par /status")
    app._check_odds = boom
    app._check_stats = lambda: (True, ["✅ API-Football : ok"])
    app._check_telegram_bot = lambda: (True, ["✅ Telegram : ok"], None)
    odds.get = lambda path, **k: calls.append(path) or []
    os.environ["ODDS_API_KEY"] = "fake-key-for-test"
    try:
        odds.CREDITS.update(remaining=412, used=88)
        lines = status.api_lines()
        assert lines[0] == "✅ The Odds API ▰▱▱▱▱▱▱▱▱▱ 88/500 ce mois" and calls == [], (lines, calls)       # 88 utilisés sur 412 + 88
        odds.CREDITS.update(remaining=None, used=None)
        lines = status.api_lines()
        assert calls == ["/sports"], calls                       # UN seul appel, gratuit
        os.environ.pop("ODDS_API_KEY")
        assert "ODDS_API_KEY" in status.odds_line()
    finally:
        app._check_odds, app._check_stats, app._check_telegram_bot, odds.get = saved[:4]
        odds.CREDITS.update(saved[4])
        if saved[5] is None:
            os.environ.pop("ODDS_API_KEY", None)
        else:
            os.environ["ODDS_API_KEY"] = saved[5]
    print("ok  13 /status : crédits connus ou un seul appel /sports, jamais le tour des ligues")


# ---------- 14. Telegram : délai de lecture ----------

def test_telegram_read_timeout_not_requeued():
    saved = (telegram._session.post, os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"), telegram.CONNECT_TIMEOUT)
    os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "123:fake-token-for-test", "42"
    posts = []

    def timeout(*a, **k):
        posts.append(a[0] if a else "")
        raise requests.ReadTimeout("lent")
    try:
        store.save(telegram.OUTBOX, [])
        telegram._session.post = timeout
        assert telegram.send("Bonjour") is False
        assert store.load(telegram.OUTBOX, []) == [] and len(posts) == 1, "délai de lecture : peut-être livré, ni remis en attente ni renvoyé"
        # connexion impossible AVANT l'envoi : remis en attente comme avant
        def noconn(*a, **k):
            raise requests.ConnectTimeout("injoignable")
        telegram._session.post = noconn
        assert telegram.send("Bonjour") is False
        box = store.load(telegram.OUTBOX, [])
        assert len(box) == 1 and box[0]["text"] == "Bonjour", box
        # une entrée déjà en attente dont le renvoi expire : abandonnée (pas de doublon), la suivante continue
        store.save(telegram.OUTBOX, [{"text": "un", "html": False, "markup": None, "chat_id": None, "ts": time.time()},
                                     {"text": "deux", "html": False, "markup": None, "chat_id": None, "ts": time.time()}])
        seq = []

        def mixed(url, json=None, **k):
            seq.append(json["text"])

            class R:
                ok, status_code = True, 200

                def json(self_):
                    return {"ok": True, "result": {"message_id": 7}}
            if json["text"].endswith("un"):
                raise requests.ReadTimeout("lent")
            return R()
        telegram._session.post = mixed
        sent, left = telegram.flush_outbox()
        assert sent == 1 and left == 0 and any(t.endswith("deux") for t in seq) and store.load(telegram.OUTBOX, []) == [], (sent, left, seq)
        # message long : le morceau resté sans réponse n'est pas renvoyé, les suivants (jamais partis) sont mis en attente dans l'ordre
        store.save(telegram.OUTBOX, [])
        long_text = "\n\n".join(f"bloc {i} " + "x" * 3000 for i in range(3))
        n = [0]

        def second_times_out(url, json=None, **k):
            n[0] += 1
            if n[0] == 2:
                raise requests.ReadTimeout("lent")

            class R:
                ok, status_code = True, 200

                def json(self_):
                    return {"ok": True, "result": {"message_id": n[0]}}
            return R()
        telegram._session.post = second_times_out
        assert telegram.send(long_text) is False
        box = store.load(telegram.OUTBOX, [])
        assert len(box) == 1 and box[0]["text"].startswith("bloc 2"), [b["text"][:8] for b in box]
        assert telegram.api and True
        err = telegram.TelegramError("x", 500)
        assert telegram._transient(err) and not telegram._transient(telegram.TelegramError("x", 400)) and not telegram._transient(
            telegram.TelegramError("x", None, maybe_sent=True)) and telegram._transient(telegram.TelegramError("x", None))
    finally:
        telegram._session.post = saved[0]
        for k, v in (("TELEGRAM_TOKEN", saved[1]), ("TELEGRAM_CHAT_ID", saved[2])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        store.save(telegram.OUTBOX, [])
    print("ok  14 Telegram : un délai de lecture sur sendMessage n'est pas remis en attente (pas de doublon)")


# ---------- 15. horloge ----------

def test_wait_clock_jump():
    dom = "jump.test"
    slept = []
    saved = (http._sleep, state.clock)
    http._sleep = lambda s: slept.append(s)
    state.clock = lambda: 1_000_000.0
    try:
        http._last[dom] = 1_000_000.0 + 3600            # horloge reculée d'une heure depuis la dernière requête
        http._wait(dom)
        assert slept and slept[0] <= http.delay_for(dom) + 1e-9, slept
        slept.clear()
        http._last[dom] = 1_000_000.0 - 1.0              # fonctionnement normal : il reste delay - 1 s
        http._wait(dom)
        assert abs(slept[0] - (http.delay_for(dom) - 1.0)) < 1e-6
        slept.clear()
        http._last[dom] = 1_000_000.0 - 100.0            # assez attendu
        http._wait(dom)
        assert not slept
    finally:
        http._sleep, state.clock = saved
        http._last.pop(dom, None)
    print("ok  15 écart entre requêtes : jamais plus que l'écart normal si l'horloge recule")


# ---------- 16. profils ----------

def test_features_do_not_memoise_failures():
    class Mod:
        PROFILE_FILE = "zz_ops_fake.json"
        broken = True

        @classmethod
        def profiles(cls):
            if cls.broken:
                raise RuntimeError("base illisible")
            return {"teams": {"A": {"all": {"n": 5}}}}
    features.reload()
    try:
        store.save(Mod.PROFILE_FILE, {"x": 1})
        assert features._p("zz", Mod) == {} and "zz" not in features._cache, "un échec n'est pas gardé en mémoire"
        Mod.broken = False
        features._clock = lambda: 1e9
        assert features._p("zz", Mod) == {"teams": {"A": {"all": {"n": 5}}}}, "la source revient dès qu'elle répond"
        # lecture ratée de la base (store._unreadable) : pas mémorisée non plus
        features.reload()
        store._unreadable.add(Mod.PROFILE_FILE)
        features._clock = lambda: 2e9
        features._p("zz", Mod)
        assert "zz" not in features._cache
        store._unreadable.discard(Mod.PROFILE_FILE)
        features._clock = lambda: 3e9
        assert features._p("zz", Mod)["teams"]
        # derniers bons profils gardés si une relecture échoue
        Mod.broken = True
        store.save(Mod.PROFILE_FILE, {"x": 2})
        features._clock = lambda: 4e9
        assert features._p("zz", Mod)["teams"], "relecture en échec : on garde les derniers bons profils"
    finally:
        features._clock = time.monotonic
        features.reload()
        store.remove(Mod.PROFILE_FILE)
    print("ok  16 profils : un échec de lecture n'est jamais mémorisé")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
    finally:
        requests.get = T._orig_get
        for k, v in T._C0.items():
            setattr(C, k, v)
        kcache.OFFLINE = False
    print(f"\n{len(tests)} tests corrections d'exploitation : OK")


if __name__ == "__main__":
    main()
    sys.exit(0)
