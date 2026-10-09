"""Tests hors ligne de kelly/sources/s365.py (365scores : stats de match). Aucun réseau : les réponses sont des enregistrements réels
(tests/fixtures/s365/, sondés le 9 octobre 2026, seulement raccourcis). Lancer : python -m tests.test_s365"""
import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="kelly-s365-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
for _k in ("S365_ENABLED", "S365_HOURS", "S365_LEAGUES", "S365_DAILY", "S365_GAMES_PER_RUN", "S365_LISTS_PER_RUN", "S365_SECONDS",
           "S365_WINDOW_DAYS", "S365_LIST_HOURS", "KELLY_OWN_LOCK"):
    os.environ.pop(_k, None)

import requests  # noqa: E402

from kelly import cache as kcache, config as C, sources, store  # noqa: E402
from kelly.sources import features, fmmatch, http, names, s365, state  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "s365")
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600
UR, MX, ALG = "soccer_xb_uruguay", "soccer_mexico_ligamx", "soccer_xb_algeria"
ROBOTS = "https://webws.365scores.com/robots.txt"
PREV_UR = ("https://webws.365scores.com/web/games/?langId=1&timezoneId=0&userCountryId=21&apptype=5&competitions=617&games=1"
           "&aftergame=4861907&direction=-1")
_C0 = {k: getattr(C, k) for k in dir(C) if k.startswith("S365_")}


def fx(name):
    with open(os.path.join(FX, name), "rb") as f:
        return f.read()


def fxj(name):
    return json.loads(fx(name))


# --- faux réseau et horloge ----------------------------------------------------

class Clock:
    def __init__(self):
        self.t = T0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}


class Net:
    """requests.get factice : routes = {url: Resp | [Resp, ...] (séquence) | exception} ; journal (url, en-têtes, instant, options)."""

    def __init__(self, clock):
        self.clock, self.routes, self.log, self.inside = clock, {}, [], None

    def get(self, url, headers=None, timeout=None, **kw):
        assert not self.inside or not self.inside(), "téléchargement sous le verrou des données"
        self.log.append((url, dict(headers or {}), self.clock.t, tuple(sorted(kw))))
        self.clock.t += 0.2                                   # latence
        r = self.routes.get(url)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else Resp(404, b"not found")

    def data(self):
        return [e for e in self.log if not e[0].endswith("robots.txt")]


CLOCK, NET = Clock(), None
_orig_get = requests.get


def fresh(serve=True):
    """Données, cache, horloge, réglages et faux réseau neufs."""
    global NET
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    store.DATA_DIR = os.path.join(TMP, "data")
    os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
    for k, v in _C0.items():
        setattr(C, k, v)
    os.environ.pop("KELLY_OWN_LOCK", None)
    http.reset()
    http.WIRE.clear()
    features.reload()
    kcache.OFFLINE = False
    s365._settle["n"] = 0
    CLOCK.t = T0
    state.clock, http._sleep = CLOCK, CLOCK.sleep
    NET = Net(CLOCK)
    requests.get = NET.get
    NET.routes[ROBOTS] = Resp(404)                            # le vrai webws.365scores.com/robots.txt répond 404 : rien d'interdit
    if serve:
        serve_uruguay()
    return NET


def synth_stats(g):
    """Réponse /game/stats/ du match `g` (objet de la liste) bâtie sur un vrai enregistrement dont on remplace les identifiants : pour les
    matchs de la page plus ancienne, dont la sonde n'a pas gardé les statistiques."""
    p = fxj("stats_uruguay_4861907.json")
    gm = p["games"][0]
    remap = {gm["homeCompetitor"]["id"]: g["homeCompetitor"]["id"], gm["awayCompetitor"]["id"]: g["awayCompetitor"]["id"]}
    for st in p["statistics"]:
        st["competitorId"] = remap[st["competitorId"]]
    gm["id"], gm["homeCompetitor"]["id"], gm["awayCompetitor"]["id"] = g["id"], g["homeCompetitor"]["id"], g["awayCompetitor"]["id"]
    return Resp(200, json.dumps(p).encode())


def serve_uruguay():
    NET.routes[s365.list_url(UR)] = Resp(200, fx("results_uruguay.json"))
    NET.routes[PREV_UR] = Resp(200, fx("results_uruguay_prev.json"))
    for g in fxj("results_uruguay_prev.json")["games"]:
        NET.routes[s365.stats_url(g["id"])] = synth_stats(g)
    for gid in (4867341, 4856756, 4861907):
        NET.routes[s365.stats_url(gid)] = Resp(200, fx(f"stats_uruguay_{gid}.json"))
    empty = fxj("stats_uruguay_4856756.json")
    empty["statistics"], empty["games"][0]["id"] = [], 4862065      # match listé avec statistiques mais que l'API ne publie pas
    NET.routes[s365.stats_url(4862065)] = Resp(200, json.dumps(empty).encode())


def hits(needle):
    return [e for e in NET.log if needle in e[0]]


# --- lecture des listes et des statistiques --------------------------------------

def test_parse_results():
    games, prev = s365.parse_results(fxj("results_uruguay.json"), UR)
    assert [g["id"] for g in games] == ["4867341", "4862065", "4856756", "4861907"] and all(g["st"] for g in games)
    g = games[0]
    assert (g["d"], g["hid"], g["hn"], g["aid"], g["an"]) == ("2026-10-09", "12675", "Montevideo City Torque", "8611", "Danubio FC")
    assert prev == PREV_UR
    # Liga MX : heures de Paris (+02:00) ramenées en UTC (00:50 à Paris = la veille 22:50 UTC) ; le match suspendu (score -1) est écarté
    games, prev = s365.parse_results(fxj("results_liga_mx.json"), MX)
    assert [(g["id"], g["d"]) for g in games] == [("4735674", "2026-09-28"), ("4735675", "2026-09-28"), ("4735676", "2026-09-27"),
                                                  ("4735678", "2026-09-26"), ("4735692", "2026-09-13")]
    assert "Suspended" in json.dumps(fxj("results_liga_mx.json")) and "4735703" not in [g["id"] for g in games]
    # reportés : écartés, terminés gardés
    games, _ = s365.parse_results(fxj("results_algeria_postponed.json"), ALG)
    assert [g["id"] for g in games] == ["4856771", "4856772"]
    # un autre championnat, ou un autre sport (38 = basket en 365scores) : rien
    assert s365.parse_results(fxj("results_liga_mx.json"), UR)[0] == []
    bad = fxj("results_uruguay.json")
    for g in bad["games"]:
        g["sportId"] = 2
    assert s365.parse_results(bad, UR)[0] == []
    # prolongation / tirs au but, score absent, mêmes équipes, nom manquant, hasStats faux : écartés ou signalés
    d = fxj("results_uruguay.json")
    d["games"][0]["statusText"] = "After Penalties"
    d["games"][1]["homeCompetitor"]["score"] = -1.0
    d["games"][2]["awayCompetitor"]["id"] = d["games"][2]["homeCompetitor"]["id"]
    d["games"][3]["hasStats"] = False
    got, _ = s365.parse_results(d, UR)
    assert [(g["id"], g["st"]) for g in got] == [("4861907", False)]
    # réponses abîmées : jamais d'exception
    for junk in (None, {}, {"games": "x"}, {"games": [1, None, {"id": 1}]}, {"games": [{"id": 5, "sportId": 1}], "paging": 3}):
        assert s365.parse_results(junk, UR) == ([], None)


def test_previous_page_url_is_checked():
    ok = "/web/games/?langId=1&competitions=617&games=1&aftergame=4861907&direction=-1"
    assert s365.prev_url(ok, 617) == "https://webws.365scores.com" + ok
    for bad in (None, 5, "https://evil.example/web/games/?competitions=617&direction=-1", "/api/games/?competitions=617&direction=-1",
                "/web/games/?competitions=618&direction=-1", "/web/games/?competitions=617&direction=1", "/web/games/?direction=-1",
                "/web/games/?competitions=617&competitions=618&direction=-1"):
        assert s365.prev_url(bad, 617) is None, bad


def test_parse_stats():
    got = s365.parse_stats(fxj("stats_liga_mx_4735674.json"))
    assert got["id"] == "4735674" and (got["hid"], got["aid"]) == ("1263", "1255")
    assert got["s"] == {"goals": [2, 4], "shots": [15, 21], "sot": [6, 10], "corners": [3, 5], "fouls": [16, 6], "throws": [27, 18],
                        "offsides": [1, 2], "cards": [3, 3]}
    # championnat « léger » (Uruguay) : pas de fautes ; la date de la liste prime sur l'heure (au décalage faux) de /game/stats/
    got = s365.parse_stats(fxj("stats_uruguay_4856756.json"), day="2026-09-30")
    assert got["d"] == "2026-09-30" and "fouls" not in got["s"]
    assert got["s"] == {"goals": [1, 1], "shots": [11, 4], "sot": [4, 2], "corners": [0, 5], "throws": [23, 27], "offsides": [2, 0], "cards": [1, 3]}
    assert s365.parse_stats(fxj("stats_uruguay_4856756.json"))["d"] == "2026-09-30"        # sans la liste : jour lu dans l'heure du match
    # Écosse 2 : ni touches ni hors-jeu
    got = s365.parse_stats(fxj("stats_scotland_4741091.json"))
    assert "throws" not in got["s"] and "offsides" not in got["s"] and got["s"]["fouls"] == [7, 6] and got["s"]["cards"] == [0, 2]
    # matchs non suivis : cartons seuls (Iran), corners et cartons à zéro (Guatemala sans hasStats) : rien d'exploitable
    assert s365.parse_stats(fxj("stats_iran_4855687.json")) is None
    assert s365.parse_stats(fxj("stats_guatemala_4744276.json")) is None
    base = fxj("stats_liga_mx_4735674.json")

    def edit(fn):
        d = copy.deepcopy(base)
        fn(d)
        return s365.parse_stats(d)

    def setv(name, cid, value):
        def fn(d):
            for s in d["statistics"]:
                if s["name"] == name and s["competitorId"] == cid:
                    s["value"] = value
        return fn

    assert edit(setv("Red Cards", 1263, "1"))["s"]["cards"] == [4, 3]                      # cartons = jaunes + rouges
    s = edit(setv("Throw-Ins", 1263, "0"))["s"]
    assert "throws" in s                                                                   # 0 d'un côté seulement : vrai zéro
    d2 = copy.deepcopy(base)
    for st in d2["statistics"]:
        if st["name"] in ("Throw-Ins", "Total Shots", "Fouls"):
            st["value"] = "0"
    s = s365.parse_stats(d2)["s"]
    assert "throws" not in s and "shots" not in s and "fouls" not in s and s["corners"] == [3, 5]       # 0-0 = non suivi, pas un zéro
    assert edit(setv("Corners", 1263, "3/8 (38%)"))["s"].get("corners") is None             # valeur non entière ignorée
    assert edit(lambda d: d["games"][0].update(statusText="Live")) is None                 # pas terminé
    assert edit(lambda d: d["games"][0].update(sportId=2)) is None
    assert edit(lambda d: d["games"][0]["homeCompetitor"].update(score=-1.0)) is None
    assert edit(lambda d: d.update(games=[])) is None
    assert edit(lambda d: d.update(statistics=[s for s in d["statistics"] if s["competitorId"] == 1263])) is None    # une seule équipe : pas de paire
    assert edit(lambda d: d["statistics"].append({"name": "Corners", "competitorId": 999, "value": "40"})) ["s"]["corners"] == [3, 5]
    for junk in (None, {}, {"games": "x"}, {"games": [None]}, {"games": [{"statusText": "Ended"}]}):
        assert s365.parse_stats(junk) is None
    assert s365._num(float("nan")) is None and s365._num(True) is None and s365._num("53%") is None and s365._num(" 7 ") == 7


def test_competition_map():
    from kelly import config as cfg
    assert set(cfg.XB_KEYS) <= set(s365.COMPETITIONS), set(cfg.XB_KEYS) - set(s365.COMPETITIONS)       # tout championnat 1xBet a son id 365
    known = set(cfg.LEAGUE_KEYS) | set(cfg.XB_KEYS)
    assert set(s365.COMPETITIONS) <= known, set(s365.COMPETITIONS) - known
    ids = list(s365.COMPETITIONS.values())
    assert len(ids) == len(set(ids)) and all(isinstance(i, int) and i > 0 for i in ids)
    assert 38 not in ids                                                                   # 38 = LNB Élite (basket), pas la Ligue 1 (35)
    cat = fxj("search_excerpt.json")
    assert s365.verify_catalogue(cat) == []
    old = dict(s365.COMPETITIONS)
    try:
        s365.COMPETITIONS["soccer_bogus"] = 38
        s365.COMPETITIONS["soccer_dup"] = 617
        s365.COMPETITIONS["soccer_missing"] = 99999
        probs = " | ".join(s365.verify_catalogue(cat))
        assert "soccer_bogus" in probs and "pas du football" in probs and "soccer_dup" in probs and "déjà pris" in probs
        assert "soccer_missing" in probs and "absent" in probs
    finally:
        s365.COMPETITIONS.clear()
        s365.COMPETITIONS.update(old)
    # les adresses : paramètres du site, heure UTC, un seul id par requête
    u = s365.list_url(UR)
    assert u == ("https://webws.365scores.com/web/games/results/?appTypeId=5&langId=1&timezoneName=UTC&userCountryId=21&competitions=617")
    assert s365.stats_url("4856756").endswith("&games=4856756") and s365.list_url("soccer_epl") is None


def test_scope_and_hours():
    fresh(serve=False)
    assert "soccer_epl" not in s365.COMPETITIONS and "soccer_xb_peru" in s365.scope()
    assert s365.scope() == [k for k in s365.scope() if k in s365.COMPETITIONS]
    assert not s365.FD_FULL & set(s365.scope())                       # ligues aux statistiques football-data complètes : pas ici
    s365.COMPETITIONS["soccer_epl"] = 7                                   # une ligue aux statistiques football-data complètes, si on l'ajoutait
    try:
        assert "soccer_epl" not in s365.scope() and "soccer_xb_peru" in s365.scope()
        C.S365_LEAGUES = ["all"]
        assert "soccer_epl" in s365.scope()                              # ... reste permise à la demande
        C.S365_LEAGUES = []
    finally:
        del s365.COMPETITIONS["soccer_epl"]
    C.S365_LEAGUES = ["soccer_xb_peru", "soccer_epl", "soccer_xb_colombia"]
    assert s365.scope() == ["soccer_xb_colombia", "soccer_xb_peru"] or set(s365.scope()) == {"soccer_xb_colombia", "soccer_xb_peru"}
    C.S365_LEAGUES = ["all"]
    assert len(s365.scope()) == len(s365.COMPETITIONS)
    C.S365_LEAGUES = []
    at = lambda h: datetime(2026, 10, 10, h, 30, tzinfo=timezone.utc)      # noqa: E731
    assert s365.in_hours(at(3)) and s365.in_hours(at(23))                  # vide : toujours
    C.S365_HOURS = "0-6,22-23"
    assert [h for h in range(24) if s365.in_hours(at(h))] == [0, 1, 2, 3, 4, 5, 6, 22, 23]
    C.S365_HOURS = "22-5"                                                  # passe minuit
    assert [h for h in range(24) if s365.in_hours(at(h))] == [0, 1, 2, 3, 4, 5, 22, 23]
    C.S365_HOURS = "7, x, 9-9"
    assert [h for h in range(24) if s365.in_hours(at(h))] == [7, 9]


# --- stockage et profils -----------------------------------------------------

def stats_of(c=(6, 4), sh=(14, 10), sot=(5, 3), fo=(12, 15), th=(22, 25), off=(2, 1), cards=(2, 3), goals=(1, 1)):
    s = {"goals": list(goals), "shots": list(sh), "sot": list(sot), "corners": list(c), "cards": list(cards), "throws": list(th),
         "offsides": list(off)}
    if fo:
        s["fouls"] = list(fo)
    return s


def game(gid, day, h, a, s=None, hn=None, an=None, key=UR):
    return {"id": str(gid), "d": day, "hid": str(h), "hn": hn or f"Team {h}", "aid": str(a), "an": an or f"Team {a}",
            "s": s or stats_of(), "k": key}


def seed(key=UR, n=4, one="Team One", two="Team Two", fouls=True, long_one=None):
    """Équipe 1 : n matchs À DOMICILE ; équipe 2 : n matchs À L'EXTÉRIEUR (adversaires d'un seul match, donc sans profil)."""
    d = s365.load()
    for i in range(n):
        day = (datetime(2026, 10, 1) + timedelta(days=i)).date().isoformat()
        g1 = game(100 + i, day, 1, 10 + i, stats_of(fo=(12, 15) if fouls else None), hn=one, an=f"Rival {10 + i}", key=key)
        g1["hl"] = long_one
        g2 = game(200 + i, day, 20 + i, 2, stats_of(fo=(12, 15) if fouls else None), hn=f"Rival {20 + i}", an=two, key=key)
        assert s365.record(d, g1, key) and s365.record(d, g2, key)
    s365.save(d)
    features.reload()
    return d


def test_record_rows_and_dedup():
    fresh(serve=False)
    d = s365.load()
    m = game(1, "2026-10-01", 1, 2, stats_of(fo=None))
    assert s365.record(d, m, UR) and not s365.record(d, m, UR) and not s365.record(d, {}, UR) and not s365.record(d, None, UR)
    home, away = d["teams"]["1"], d["teams"]["2"]
    assert home["m"] == [[261001, 1, 1, 1, 14, 10, 5, 3, 6, 4, None, None, 2, 3, 22, 25, 2, 1]]       # domicile : (pour, contre)
    assert away["m"] == [[261001, 0, 1, 1, 10, 14, 3, 5, 4, 6, None, None, 3, 2, 25, 22, 1, 2]]       # extérieur : renversé ; fautes absentes
    assert d["seen"]["1"] == 261001 and home["sk"] == [UR] and home["nm"] == "Team 1"
    # plus de S365_KEEP_MATCHES matchs : les plus anciens sont oubliés ; un nom long est gardé
    C.S365_KEEP_MATCHES = 3
    for i in range(2, 7):
        s365.record(d, dict(game(i, f"2026-10-0{i}", 1, 10 + i), hl="Club Team One"), UR)
    assert [r[0] for r in d["teams"]["1"]["m"]] == [261004, 261005, 261006] and d["teams"]["1"]["ln"] == "Club Team One"
    # un match vu n'est jamais relu, même s'il revient dans une liste
    games, _ = s365.parse_results(fxj("results_uruguay.json"), UR)
    d2 = s365.load()
    d2["seen"]["4867341"] = 261009
    games[3]["st"] = False                                       # hasStats faux (Iran, Danemark 2...) : jamais demandé
    s365.note_list(d2, UR, games, None, "first", 6, 1.0)
    assert "4867341" not in d2["pending"] and set(d2["pending"]) == {"4862065", "4856756"}
    assert d2["pending"]["4856756"] == {"k": UR, "d": "2026-09-30", "h": "12675", "hn": "Montevideo City Torque", "a": "7766", "an": "Peñarol"}


def test_profiles_gates_and_aliases():
    fresh(serve=False)
    seed(n=4, long_one="Club Team One")
    d = s365.load()
    s365.record(d, game(300, "2026-10-05", 3, 4, hn="Team Three", an="Team Four"), UR)       # une seule fois : pas de profil
    s365.record(d, game(301, "2026-10-06", 3, 4, hn="Team Three", an="Team Four"), UR)       # deux fois : pas de profil non plus
    s365.save(d)
    prof = s365.profiles()["leagues"][UR]["teams"]
    assert set(prof) == {"Team One", "Club Team One", "Team Two"}, set(prof)
    assert prof["Team One"] is prof["Club Team One"] and prof["Team One"]["id"] == "1"
    one, two = prof["Team One"], prof["Team Two"]
    assert one["all"]["n"] == 4 and one["home"]["n"] == 4 and one["away"]["n"] == 0 and two["away"]["n"] == 4
    assert one["home"]["corners_for"] == 6 and one["home"]["corners_against"] == 4 and two["away"]["corners_for"] == 4
    assert one["all"]["throws_for"] == 22 and one["all"]["offsides_against"] == 1 and one["all"]["fouls_for"] == 12
    assert "goals_for" in one["all"] and one["sk"] == [UR] and one["last"] == "2026-10-04"
    # trois matchs : seuil APISTATS_MIN_N atteint
    d = s365.load()
    s365.record(d, game(302, "2026-10-07", 3, 4, hn="Team Three", an="Team Four"), UR)
    s365.save(d)
    assert {"Team Three", "Team Four"} <= set(s365.profiles()["leagues"][UR]["teams"])
    # deux clubs du même nom dans deux pays restent séparés (un profil par championnat)
    d = s365.load()
    for i in range(3):
        s365.record(d, game(400 + i, f"2026-10-0{i + 1}", 50, 51 + i, hn="Nacional", an=f"Other {i}", key="soccer_xb_paraguay"), "soccer_xb_paraguay")
    s365.save(d)
    prof = s365.profiles()["leagues"]
    assert "Nacional" in prof["soccer_xb_paraguay"]["teams"] and "Nacional" not in prof[UR]["teams"]
    # un nom long ne prend jamais la place du nom court d'une autre équipe de la ligue
    d = s365.load()
    d["teams"]["3"]["ln"] = "Team One"
    s365.save(d)
    assert s365.profiles()["leagues"][UR]["teams"]["Team One"]["id"] == "1"
    # équipe oubliée (pas jouée depuis S365_KEEP_DAYS) et match en attente hors fenêtre : effacés à l'écriture
    d = s365.load()
    d["teams"]["3"]["d"] = str(state.now().date() - timedelta(days=C.S365_KEEP_DAYS + 1))
    d["pending"] = {"9001": {"k": UR, "d": "2026-01-01", "h": "1", "hn": "x", "a": "2", "an": "y"},
                    "9002": {"k": UR, "d": "2026-10-09", "h": "1", "hn": "x", "a": "2", "an": "y"}}
    d["lists"]["soccer_epl"] = {"at": 1.0}
    s365.save(d)
    d = s365.load()
    assert "3" not in d["teams"] and "1" in d["teams"] and set(d["pending"]) == {"9002"} and "soccer_epl" not in d["lists"]
    sm = s365.summary()
    assert sm["tracked"] == len(d["teams"]) and sm["usable"] >= 2 and sm["good"] == 0 and sm["pending"] == 1


# --- choix des listes et des matchs --------------------------------------------

def test_due_lists_order():
    fresh(serve=False)
    keys = ["soccer_xb_peru", "soccer_xb_colombia", UR, "soccer_xb_iran"]
    d = s365.load()
    assert s365.due_lists(d, keys) == [(k, "first") for k in keys]                    # jamais listés : ordre du catalogue
    now = state.clock()
    d["lists"] = {"soccer_xb_peru": {"at": now - 30 * HOUR, "new": "2026-10-09", "old": "2026-09-01", "prev": None},
                  "soccer_xb_colombia": {"at": now - 25 * HOUR, "new": "2026-10-09", "old": "2026-10-01", "prev": PREV_UR},
                  UR: {"at": now - 2 * HOUR, "new": "2026-10-09", "old": "2026-09-01", "prev": PREV_UR}}
    due = s365.due_lists(d, keys)
    # Iran jamais listé d'abord ; puis la page plus ancienne qui manque à la fenêtre (Colombie) ; puis la liste périmée (Pérou) ;
    # l'Uruguay (listé il y a 2 h, fenêtre remplie) n'est pas dû
    assert due == [("soccer_xb_iran", "first"), ("soccer_xb_colombia", "prev"), ("soccer_xb_peru", "first")], due
    assert s365.due_lists(d, keys, failed={"soccer_xb_iran", "soccer_xb_peru"}) == [("soccer_xb_colombia", "prev")]
    # Pérou listé 30 h plus tôt mais sans match depuis plus de 2 semaines : 3 x 20 h
    d["lists"]["soccer_xb_peru"]["new"] = "2026-09-20"
    assert ("soccer_xb_peru", "first") not in s365.due_lists(d, keys)
    d["lists"]["soccer_xb_peru"]["at"] = now - 61 * HOUR
    assert ("soccer_xb_peru", "first") in s365.due_lists(d, keys)
    # championnat sans aucun match avec statistiques (Iran) : une fois par semaine
    d["lists"]["soccer_xb_iran"] = {"at": now - 3 * 24 * HOUR, "new": "2026-10-09", "old": "2026-09-01", "prev": None, "n": 59, "ns": 0}
    assert ("soccer_xb_iran", "first") not in s365.due_lists(d, keys)
    d["lists"]["soccer_xb_iran"]["at"] = now - 8 * 24 * HOUR
    assert ("soccer_xb_iran", "first") in s365.due_lists(d, keys)
    d["lists"]["soccer_xb_iran"] |= {"prev": PREV_UR, "old": "2026-10-01", "at": now - HOUR}      # inutile de remonter le temps sans statistiques
    assert not [x for x in s365.due_lists(d, keys) if x[0] == "soccer_xb_iran"]
    # championnat déjà couvert par FotMob : une liste par semaine, plus de pages plus anciennes
    cov = {"soccer_xb_colombia"}
    assert ("soccer_xb_colombia", "prev") not in s365.due_lists(d, keys, covered=cov)
    # fenêtre de remplissage : 28 jours par défaut ; plus courte, la chaîne s'arrête plus tôt
    C.S365_WINDOW_DAYS = 5
    assert ("soccer_xb_colombia", "prev") not in s365.due_lists(d, keys)


def test_targets_order():
    fresh(serve=False)
    today = state.now().date()
    d = s365.load()
    day = lambda n: str(today - timedelta(days=n))                                    # noqa: E731

    def team(tid, n):
        d["teams"][str(tid)] = {"nm": f"T{tid}", "sk": [UR], "d": str(today), "m": [[261001 + i, 1] + [1] * 16 for i in range(n)]}

    def pend(gid, key, d_, h, a):
        d["pending"][str(gid)] = {"k": key, "d": d_, "h": str(h), "hn": f"T{h}", "a": str(a), "an": f"T{a}"}

    for tid, n in ((1, 0), (2, 5), (3, 8), (4, 8), (5, 2), (6, 20), (7, 20)):
        team(tid, n)
    pend(1, UR, day(3), 1, 2)             # une équipe sans profil (0 match) : tout en tête
    pend(2, UR, day(10), 3, 4)            # deux équipes à 8 matchs (= S365_TARGET_N) et match de plus d'une semaine : ignoré
    pend(3, UR, day(2), 3, 4)             # même équipes mais match de moins d'une semaine : lu (en dernier)
    pend(4, UR, day(5), 5, 2)             # min 2 (< 3) : en tête aussi ; plus ancien que le 1
    pend(5, UR, day(20), 2, 3)            # min 5 (< 6) : seconde catégorie
    pend(6, UR, day(40), 5, 2)            # hors fenêtre de 28 jours : ignoré
    pend(7, "soccer_xb_peru", day(1), 6, 7)   # autre championnat, équipes bien fournies mais match d'hier : lu
    pend(8, "soccer_xb_peru", day(4), 1, 6)   # autre championnat : tourniquet avec l'Uruguay
    pend(9, "soccer_xb_colombia", day(1), 1, 6)   # championnat couvert par FotMob : jamais
    d["seen"]["10"] = 261001
    pend(10, UR, day(1), 1, 2)            # déjà lu
    keys = [UR, "soccer_xb_peru", "soccer_xb_colombia"]
    got = s365.targets(d, keys, covered={"soccer_xb_colombia"}, today=today)
    assert got == ["1", "8", "4", "7", "5", "3"], got
    assert s365.targets(d, [UR], covered=(), today=today, tried={"1", "4"}) == ["5", "3"]
    assert s365.targets(d, ["soccer_xb_peru"], covered=(), today=today) == ["8", "7"]
    assert s365.targets(d, [], covered=(), today=today) == []
    C.S365_TARGET_N = 30                   # équipes jamais « rassasiées » : le match de 10 jours revient
    assert "2" in s365.targets(d, [UR], covered=(), today=today)


# --- remplissage : réseau poli --------------------------------------------------

def test_fill_pipeline_polite_and_idempotent():
    net = fresh()
    rep = s365.fill(max_lists=1, max_games=10, leagues=[UR])
    assert rep["errors"] == [] and rep["added"] == 3 and rep["lists"] == 1 and rep["games"] == 4, rep
    # robots.txt (404 = tout permis) lu une fois, une liste, quatre statistiques ; que webws.365scores.com
    assert len(net.log) == 6 and net.log[0][0] == ROBOTS and rep["requests"] == 6
    assert all(e[0].startswith("https://webws.365scores.com/") for e in net.log)
    assert [e[0] for e in net.data()] == [s365.list_url(UR), s365.stats_url(4867341), s365.stats_url(4856756), s365.stats_url(4861907),
                                          s365.stats_url(4862065)] or sorted(e[0] for e in net.data()[1:]) == sorted(
        s365.stats_url(g) for g in (4867341, 4856756, 4861907, 4862065))
    # politesse : identité KellyBot déclarée, aucun en-tête de navigateur, aucune option (proxy, empreinte TLS...) ; >= 3 s entre deux requêtes
    for url, headers, t, opts in net.log:
        assert headers == {"User-Agent": http.UA, "Accept": "*/*"} and headers["User-Agent"].startswith("KellyBot"), headers
        assert opts == (), opts
    assert all(b[2] - a[2] >= 3.0 for a, b in zip(net.log, net.log[1:])), [e[2] for e in net.log]
    assert http.delay_for("webws.365scores.com") >= 3.0 and "webws.365scores.com" not in http.PROXIES
    d = s365.load()
    assert set(d["seen"]) == {"4867341", "4862065", "4856756", "4861907"} and d["pending"] == {}
    t = d["teams"]["12675"]                                     # Montevideo City Torque : deux matchs à domicile lus (le troisième sans stats)
    assert t["nm"] == "Montevideo City Torque" and len(t["m"]) == 2 and t["sk"] == [UR]
    assert d["teams"]["8611"]["m"][0][:2] == [261009, 0] and d["teams"]["7766"]["nm"] == "Peñarol"
    assert d["lists"][UR]["nt"] == 6 and d["lists"][UR]["new"] == "2026-10-09" and d["lists"][UR]["old"] == "2026-09-26"
    assert d["req"] == {"day": "2026-10-10", "n": 6}
    assert state.health("s365")["ok"] is True and state.health("s365")["rows"] == 4
    assert "4862065" in d["seen"]                                                       # le match sans statistiques n'est plus demandé
    assert not any(http.is_cached(s365.stats_url(g), None) for g in (4867341, 4862065, 4856756, 4861907))     # réponses brutes jamais gardées
    assert http.is_cached(s365.list_url(UR), None)
    done = {s365.stats_url(g) for g in (4867341, 4862065, 4856756, 4861907)}
    # une heure plus tard : la chaîne de pages plus anciennes avance (la fenêtre de 28 jours n'est pas remplie) et lit les matchs de la
    # fenêtre qu'elle a trouvés ; les quatre déjà lus ne sont JAMAIS redemandés
    CLOCK.t += HOUR
    n = len(net.log)
    rep = s365.fill(leagues=[UR])
    new = [e[0] for e in net.log[n:]]
    assert new[0] == PREV_UR and rep["requests"] == 4 and rep["added"] == 3 and rep["errors"] == [], (rep, new)
    assert not done & set(new) and len([u for u in new if "/game/stats/" in u]) == 3
    # puis plus rien à lire : la liste n'est relue qu'après S365_LIST_HOURS (20 h), la fenêtre est pleine
    CLOCK.t += HOUR
    n = len(net.log)
    rep = s365.fill(leagues=[UR])
    assert len(net.log) == n and rep["requests"] == 0 and rep["added"] == 0 and rep["errors"] == [], rep
    # le lendemain : une requête (la liste), aucun des matchs déjà lus
    CLOCK.t += 21 * HOUR
    rep = s365.fill(leagues=[UR])
    new = [e[0] for e in net.log[n:]]
    assert new == [s365.list_url(UR)] and rep["added"] == 0 and rep["requests"] == 1, new


def test_fill_follows_the_window_and_the_chain():
    net = fresh()
    rep = s365.fill(backfill=True, max_requests=3, leagues=[UR])      # robots + première page + page plus ancienne : puis le plafond
    assert rep["requests"] == 3 and rep["lists"] == 2 and rep["games"] == 0, rep
    assert [e[0] for e in net.data()] == [s365.list_url(UR), PREV_UR]
    d = s365.load()
    # fenêtre de 28 jours depuis le 10/10 : le 12/09 ; la page plus ancienne (6 au 21/09) a des matchs hors fenêtre, écartés des lectures
    assert d["lists"][UR]["old"] == "2026-09-06" and d["lists"][UR]["prev"].endswith("aftergame=4826564&direction=-1")
    inwin = {g for g, p in d["pending"].items() if p["d"] >= "2026-09-12"}
    assert {"4843614", "4856760", "4856753", "4867341", "4861907"} <= inwin
    todo = s365.targets(d, [UR], (), state.now().date())
    assert set(todo) == {"4867341", "4862065", "4856756", "4861907", "4843614", "4856760", "4856753"}, todo
    assert not {"4826564", "4826561"} & set(d["pending"])                  # 6 septembre : hors fenêtre, jamais gardés en attente
    # la fenêtre est remplie (old < 12/09) : la chaîne ne continue pas, même s'il existe une page encore plus ancienne
    rep = s365.fill(backfill=True, max_requests=1, leagues=[UR])
    assert [e for e in net.data() if "aftergame=4826564" in e[0]] == []
    assert s365.due_lists(s365.load(), [UR]) == []


def test_fill_caps_budget_and_order():
    net = fresh()
    rep = s365.fill(max_lists=1, max_games=2, leagues=[UR])
    assert rep["games"] == 2 and len(net.data()) == 3 and rep["left"] == 2, rep
    assert set(s365.load()["pending"]) == {g for g in ("4867341", "4862065", "4856756", "4861907")} - set(s365.load()["seen"])
    # plafond du jour : robots + liste + statistiques comptent ; le suivant (même jour) ne fait rien, le lendemain reprend
    fresh()
    C.S365_DAILY = 4
    rep = s365.fill(max_lists=1, max_games=10, leagues=[UR])
    assert rep["requests"] == 4 and s365.load()["req"] == {"day": "2026-10-10", "n": 4}
    n = len(NET.log)
    CLOCK.t += 2 * HOUR
    rep = s365.fill(max_games=10, leagues=[UR])
    assert len(NET.log) == n and rep["requests"] == 0
    CLOCK.t += 24 * HOUR
    rep = s365.fill(max_games=10, leagues=[UR])
    assert rep["requests"] > 0 and s365.load()["req"]["day"] == "2026-10-11"
    # durée épuisée : aucune requête
    fresh()
    rep = s365.fill(seconds=0, leagues=[UR])
    assert NET.log == [] and rep["requests"] == 0
    # plafonds par défaut de la configuration (passage horaire)
    fresh()
    C.S365_GAMES_PER_RUN, C.S365_LISTS_PER_RUN = 1, 1
    rep = s365.fill(leagues=[UR])
    assert rep["lists"] == 1 and rep["games"] == 1
    fresh()
    C.S365_GAMES_PER_RUN = C.S365_LISTS_PER_RUN = 0
    assert s365.fill(leagues=[UR]).get("off") and NET.log == []


def test_fill_retries_fresh_games_without_stats():
    net = fresh()
    C.S365_WINDOW_DAYS = 1                                                 # pas de page plus ancienne : seul le match d'hier compte ici
    d = fxj("results_uruguay.json")
    d["games"] = [g for g in d["games"] if g["id"] == 4867341]            # match d'hier (9/10), statistiques pas encore publiées
    net.routes[s365.list_url(UR)] = Resp(200, json.dumps(d).encode())
    empty = fxj("stats_uruguay_4867341.json")
    empty["statistics"] = []
    net.routes[s365.stats_url(4867341)] = Resp(200, json.dumps(empty).encode())
    for expect_r in (1, 2):
        s365.fill(max_lists=1, max_games=5, leagues=[UR])
        p = s365.load()["pending"].get("4867341")
        assert p and p["r"] == expect_r and "4867341" not in s365.load()["seen"], (expect_r, p)
    s365.fill(max_lists=1, max_games=5, leagues=[UR])                    # troisième essai : abandonné
    assert "4867341" not in s365.load()["pending"] and "4867341" in s365.load()["seen"]
    assert s365.load()["teams"] == {}
    # les statistiques arrivent : le match d'hier est lu à l'essai suivant
    net = fresh()
    C.S365_WINDOW_DAYS = 1
    net.routes[s365.list_url(UR)] = Resp(200, json.dumps(d).encode())
    net.routes[s365.stats_url(4867341)] = [Resp(200, json.dumps(empty).encode())]
    s365.fill(max_lists=1, max_games=5, leagues=[UR])
    assert s365.load()["pending"]["4867341"]["r"] == 1
    net.routes[s365.stats_url(4867341)] = Resp(200, fx("stats_uruguay_4867341.json"))
    rep = s365.fill(max_games=5, leagues=[UR])
    assert rep["added"] == 1 and "12675" in s365.load()["teams"]


def test_blocked_sources_stop_everything():
    # 403 sur la liste : source bloquée 24 h, plus aucune requête (même rejouée), reprise ensuite
    net = fresh()
    net.routes[s365.list_url(UR)] = Resp(403, b"forbidden")
    rep = s365.fill(leagues=[UR])
    assert rep.get("blocked") and state.is_blocked("s365") and len(net.data()) == 1, rep
    assert "bloquée par le site" in state.health("s365")["last_error"] and "HTTP 403" in state.health("s365")["last_error"]
    n = len(net.log)
    for dt in (HOUR, 20 * HOUR):
        CLOCK.t += dt
        rep = s365.fill(leagues=[UR])
        assert rep.get("blocked") and len(net.log) == n, rep
    CLOCK.t += 4 * HOUR + 60                                               # 25 h après le blocage
    net.routes[s365.list_url(UR)] = Resp(200, fx("results_uruguay.json"))
    rep = s365.fill(max_games=0, leagues=[UR])
    assert not rep.get("blocked") and len(net.log) > n and not state.is_blocked("s365")
    # 429 sur une statistique : bloqué, les autres matchs restent en attente, la liste lue est gardée
    fresh()
    NET.routes[s365.stats_url(4856756)] = Resp(429, b"slow down")
    rep = s365.fill(max_lists=1, max_games=10, leagues=[UR])
    assert rep.get("blocked") and state.is_blocked("s365") and "HTTP 429" in state.health("s365")["last_error"]
    d = s365.load()
    assert UR in d["lists"] and "4856756" in d["pending"] and "4861907" in d["pending"] and len(NET.data()) == 4
    # page de vérification anti-robot (200 HTML) : bloqué
    fresh()
    NET.routes[s365.list_url(UR)] = Resp(200, b"<html><head><title>Just a moment...</title></head><body>cf_chl_opt</body></html>")
    rep = s365.fill(leagues=[UR])
    assert rep.get("blocked") and "anti-robot" in state.health("s365")["last_error"]
    # Cloudflare : bloqué une semaine
    fresh()
    NET.routes[s365.list_url(UR)] = Resp(503, b"", {"cf-mitigated": "challenge"})
    s365.fill(leagues=[UR])
    until = datetime.fromisoformat(state.health("s365")["blocked_until"])
    assert until - state.now() > timedelta(days=6)
    # robots.txt qui répond 403 : bloqué, aucune donnée demandée
    fresh()
    NET.routes[ROBOTS] = Resp(403)
    rep = s365.fill(leagues=[UR])
    assert rep.get("blocked") and NET.data() == []


def test_robots_disallow_is_respected():
    for rules in (b"User-agent: *\nDisallow: /web/\n", b"User-agent: KellyBot\nDisallow: /\n", b"User-agent: *\nDisallow: /\n"):
        net = fresh()
        net.routes[ROBOTS] = Resp(200, rules)
        rep = s365.fill(leagues=[UR])
        assert net.data() == [] and not state.is_blocked("s365") and any("robots.txt interdit" in e for e in rep["errors"]), (rules, rep)
        assert rep["added"] == 0 and rep["requests"] == 1
    # robots.txt de www.365scores.com (pages du site) : autre hôte, jamais lu pour l'API
    net = fresh()
    net.routes["https://www.365scores.com/robots.txt"] = Resp(200, b"User-agent: *\nDisallow: *date=*\nDisallow: /\n")
    rep = s365.fill(max_lists=1, max_games=0, leagues=[UR])
    assert rep["lists"] == 1 and not hits("www.365scores.com") and rep["errors"] == []
    # règle qui ne concerne pas /web/ : permis
    net = fresh()
    net.routes[ROBOTS] = Resp(200, b"User-agent: *\nDisallow: /private/\n")
    assert s365.fill(max_lists=1, max_games=0, leagues=[UR])["lists"] == 1 and len(net.data()) == 1
    # Crawl-delay supérieur à 3 s : respecté
    net = fresh()
    net.routes[ROBOTS] = Resp(200, b"User-agent: *\nCrawl-delay: 7\n")
    s365.fill(max_lists=1, max_games=2, leagues=[UR])
    assert all(b[2] - a[2] >= 7.0 for a, b in zip(net.log, net.log[1:])), [e[2] for e in net.log]


def test_errors_do_not_block_and_stop_after_three():
    net = fresh()
    for gid in (4867341, 4862065, 4856756, 4861907):
        net.routes[s365.stats_url(gid)] = Resp(504, b"gateway")
    rep = s365.fill(max_lists=1, max_games=10, leagues=[UR])
    assert not state.is_blocked("s365") and not rep.get("blocked") and rep["games"] == 3 and len(rep["errors"]) == 3, rep
    d = s365.load()
    assert d["seen"] == {} and len(d["pending"]) == 4 and d["teams"] == {}                    # rien d'oublié : repris au passage suivant
    # coupure réseau, JSON illisible, liste sans « games » : erreurs signalées, jamais d'exception
    net = fresh()
    net.routes[s365.list_url(UR)] = requests.ConnectionError("boom")
    rep = s365.fill(leagues=[UR])
    assert rep["added"] == 0 and any("réseau indisponible" in e for e in rep["errors"]), rep
    assert len(hits("/games/results/")) == 2 and rep["lists"] == 1                  # un seul essai (+ le nouvel essai de http), pas de boucle
    net = fresh()
    net.routes[s365.list_url(UR)] = Resp(200, b"<html>maintenance</html>")
    rep = s365.fill(leagues=[UR])
    assert any("illisible" in e for e in rep["errors"]) and "soccer_xb_uruguay" not in s365.load()["lists"], rep
    net = fresh()
    net.routes[s365.stats_url(4867341)] = Resp(200, b'{"statistics": "nope"}')
    rep = s365.fill(max_lists=1, max_games=1, leagues=[UR])
    assert any("illisible" in e for e in rep["errors"]) and "4867341" in s365.load()["pending"]
    # statistiques d'un autre match que celui demandé : écartées (jamais rangées sous le mauvais match)
    net = fresh()
    net.routes[s365.stats_url(4856756)] = Resp(200, fx("stats_uruguay_4861907.json"))
    s365.fill(max_lists=1, max_games=4, leagues=[UR])
    d = s365.load()
    assert "4856756" in d["seen"] and "7766" not in d["teams"]
    net = fresh()
    other = fxj("stats_uruguay_4861907.json")
    other["games"][0]["id"] = 4856756                                      # bon numéro de match mais d'autres équipes que celles de la liste
    net.routes[s365.stats_url(4856756)] = Resp(200, json.dumps(other).encode())
    s365.fill(max_lists=1, max_games=4, leagues=[UR])
    d = s365.load()
    assert "4856756" in d["seen"] and "7766" not in d["teams"] and len(d["teams"]["12675"]["m"]) == 1 and len(d["teams"]["10191"]["m"]) == 1 and "12675" in d["teams"] and len(d["teams"]["12675"]["m"]) == 1
    net = fresh()
    other = fxj("stats_uruguay_4856756.json")
    other["games"][0]["id"] = 999                                          # mêmes équipes mais autre match que celui demandé
    net.routes[s365.stats_url(4856756)] = Resp(200, json.dumps(other).encode())
    s365.fill(max_lists=1, max_games=4, leagues=[UR])
    d = s365.load()
    assert "4856756" in d["seen"] and "7766" not in d["teams"]
    net = fresh()
    other = fxj("stats_uruguay_4861907.json")
    other["games"][0]["id"] = 4856756                                      # bon numéro de match mais d'autres équipes que celles de la liste
    net.routes[s365.stats_url(4856756)] = Resp(200, json.dumps(other).encode())
    s365.fill(max_lists=1, max_games=4, leagues=[UR])
    d = s365.load()
    assert "4856756" in d["seen"] and "7766" not in d["teams"] and len(d["teams"]["12675"]["m"]) == 1 and len(d["teams"]["10191"]["m"]) == 1


def test_disabled_hours_offline_and_lock():
    net = fresh()
    C.S365_ENABLED = False
    assert s365.fill(leagues=[UR]).get("off") and net.log == []
    C.S365_ENABLED = True
    C.S365_HOURS = "20-23"                                        # il est 06:00 UTC
    rep = s365.fill(leagues=[UR])
    assert rep.get("off") and "heures" in rep["why"] and net.log == []
    assert "désactivé" in s365.report_line(rep)
    rep = s365.fill(backfill=True, max_requests=2, leagues=[UR])      # le remplissage manuel ignore les heures
    assert rep["requests"] == 2
    C.S365_HOURS = ""
    kcache.OFFLINE = True                                            # commandes Telegram : aucun téléchargement
    n = len(net.log)
    assert s365.fill(leagues=[UR]).get("off") and len(net.log) == n
    kcache.OFFLINE = False
    # le verrou des données n'est pas tenu pendant les téléchargements (mode KELLY_OWN_LOCK), seulement pour fusionner
    net = fresh()
    os.environ[fmmatch.OWN_LOCK_ENV] = "1"
    state_ = {"held": False, "enters": 0}

    @contextlib.contextmanager
    def fake_lock(wait=None, on_busy=None):
        state_["held"] = True
        state_["enters"] += 1
        try:
            yield
        finally:
            state_["held"] = False

    real = store.data_lock
    store.data_lock = fake_lock
    net.inside = lambda: state_["held"]
    try:
        rep = s365.fill(max_lists=1, max_games=5, leagues=[UR])
    finally:
        store.data_lock = real
        net.inside = None
        os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
    assert rep["added"] == 3 and state_["enters"] == 2 and len(net.log) == 6, (rep, state_)


def test_covered_by_fotmob_is_skipped():
    net = fresh()
    d = s365.load()
    d["lists"][UR] = {"at": state.clock() - 30 * HOUR, "new": "2026-10-09", "old": "2026-09-01", "prev": None, "nt": 16, "n": 20, "ns": 20}
    s365.save(d)
    row = lambda i: [261001 + i, 1] + [1, 1, 10, 8, 4, 3, 5, 4, 9, 9, 2, 3, 20, 20, 1, 1, None, None, None, None]      # noqa: E731
    fm = fmmatch.load()
    for t in range(16):                                       # 16 équipes de la ligue, toutes avec 6 matchs FotMob (tirs ET corners)
        fm["teams"][str(500 + t)] = {"nm": f"F{t}", "sk": [UR], "d": "2026-10-09", "m": [row(i) for i in range(6)]}
    fmmatch.save(fm)
    assert UR in s365._covered(s365.load())
    n = len(net.log)
    rep = s365.fill(leagues=[UR])
    assert len(net.log) == n and rep["requests"] == 0, rep                     # liste vue il y a 30 h, mais couverte : 7 jours
    # tirs absents chez FotMob (ligue mal couverte) : pas « couverte »
    fm = fmmatch.load()
    for t in fm["teams"].values():
        t["m"] = [r[:4] + [None] + r[5:] for r in t["m"]]
    fmmatch.save(fm)
    assert UR not in s365._covered(s365.load())
    # moins de 90 % des équipes : pas couverte
    fm = fmmatch.load()
    for t in list(fm["teams"].values())[:3]:
        t["m"] = [row(0)]
    for t in fm["teams"].values():
        if len(t["m"]) > 1:
            t["m"] = [row(i) for i in range(6)]
    fmmatch.save(fm)
    assert UR not in s365._covered(s365.load())


# --- fusion dans features --------------------------------------------------------

def fm_profile(key, n=6, corners=7.0, shots=None):
    """Profil FotMob (format fmmatch.profiles) pour Team One / Team Two."""
    v = {"n": n, "corners_for": corners, "corners_against": corners, "cards_for": 2.0, "cards_against": 2.0}
    if shots:
        v |= {"shots_for": shots, "shots_against": shots}
    return {"teams": {nm: {"id": nm, "nm": nm, "sk": [key], "all": dict(v), "home": dict(v), "away": dict(v)} for nm in ("Team One", "Team Two")}}


def test_features_fill_gaps_only():
    fresh(serve=False)
    seed()
    f = features.match_features("Team One", "Team Two", UR)
    assert f["sources"] == ["365scores"], f["sources"]
    # équipe 1 toujours à domicile, équipe 2 toujours à l'extérieur : les moyennes sont celles de la synthèse (6-4 corners, 14-10 tirs...)
    assert (f["exp_corners_home"], f["exp_corners_away"], f["exp_corners"]) == (6.0, 4.0, 10.0)
    assert (f["exp_shots_home"], f["exp_shots_away"], f["exp_shots"]) == (14.0, 10.0, 24.0)
    assert (f["exp_sot"], f["exp_fouls"], f["exp_throws"], f["exp_offsides"], f["exp_cards"]) == (8.0, 27.0, 47.0, 3.0, 5.0)
    assert f["exp_cards_team"] == 5.0 and f["exp_fouls_home"] == 12.0 and f["exp_throws_away"] == 25.0
    assert not any(k.startswith("exp_goals") for k in f)                              # les buts gardent leurs sources
    assert f["aps_n"] == (4, 4) and f["aps_q"] == features.aps_quality(4) == 0.25 and (f["n_home"], f["n_away"]) == (4, 4)
    assert set(f["aps_keys"]) == set(f["s365_keys"]) and "exp_corners" in f["aps_keys"] and "exp_throws_home" in f["s365_keys"]
    assert (f["home_team"], f["away_team"]) == ("Team One", "Team Two")
    # championnat « léger » (sans fautes) : pas d'exp_fouls inventé
    fresh(serve=False)
    seed(fouls=False)
    f = features.match_features("Team One", "Team Two", UR)
    assert "exp_fouls" not in f and "exp_fouls_home" not in f and f["exp_corners"] == 10.0
    # nom long, accents et casse
    fresh(serve=False)
    seed(long_one="Club Atlético One")
    assert features.match_features("Club Atletico One", "team two", UR)["sources"] == ["365scores"]
    # autre championnat, équipe inconnue, une seule équipe connue : rien
    for h, a, k in (("Team One", "Team Two", "soccer_xb_peru"), ("Team One", "Nobody", UR), ("Nobody", "Team Two", UR), ("Team One", "Team One", UR)):
        assert features.match_features(h, a, k)["sources"] == [], (h, a, k)
    # moins de APISTATS_MIN_N matchs : pas de profil, pas de chiffre
    fresh(serve=False)
    seed(n=2)
    assert features.match_features("Team One", "Team Two", UR)["sources"] == []


def test_features_priority_over_365():
    # FotMob (matchs) passe avant 365scores : ses corners, tirs et cartons restent ; 365scores ne donne que ce que FotMob n'a pas
    fresh(serve=False)
    seed()
    features._cache["fmm"] = fm_profile(UR, n=6, corners=7.0, shots=11.0)
    f = features.match_features("Team One", "Team Two", UR)
    assert f["sources"] == ["FotMob (matchs)", "365scores"], f["sources"]
    assert f["exp_corners"] == 14.0 and f["exp_shots"] == 22.0 and f["exp_cards"] == 4.0                # FotMob
    assert f["exp_throws"] == 47.0 and f["exp_fouls"] == 27.0 and f["exp_offsides"] == 3.0 and f["exp_sot"] == 8.0      # 365scores
    assert set(f["s365_keys"]) < set(f["aps_keys"]) and "exp_corners" not in f["s365_keys"] and "exp_shots" not in f["s365_keys"]
    assert f["aps_n"] == (4, 4) and f["aps_q"] == 0.25                  # l'effectif et la qualité les plus prudents des deux profils courts
    fresh(serve=False)
    seed()
    features._cache["fmm"] = fm_profile(UR, n=3, corners=7.0, shots=11.0)                  # FotMob sur 3 matchs, 365scores sur 4
    f = features.match_features("Team One", "Team Two", UR)
    assert f["aps_n"] == (3, 3) and f["aps_q"] == features.aps_quality(3) == 0.20
    fresh(serve=False)
    seed()
    features._cache["fmm"] = fm_profile(UR, n=12, corners=7.0, shots=11.0)                 # FotMob sur 12 matchs : 365scores (4) reste le plus prudent
    f = features.match_features("Team One", "Team Two", UR)
    assert f["aps_n"] == (4, 4) and f["aps_q"] == 0.25
    # tout est déjà là (FotMob avec tout) : 365scores n'ajoute ni clé ni source
    fresh(serve=False)
    seed()
    full = fm_profile(UR, n=8, corners=7.0, shots=11.0)
    for t in full["teams"].values():
        for v in (t["all"], t["home"], t["away"]):
            v |= {k + sfx: 3.0 for k in ("sot", "fouls", "throws", "offsides", "goals") for sfx in ("_for", "_against")}
    features._cache["fmm"] = full
    f = features.match_features("Team One", "Team Two", UR)
    assert f["sources"] == ["FotMob (matchs)"] and "s365_keys" not in f and f["exp_throws"] == 6.0
    # API-Football passe avant lui aussi
    fresh(serve=False)
    seed()
    features._cache["aps"] = fm_profile(UR, n=7, corners=9.0)
    f = features.match_features("Team One", "Team Two", UR)
    assert f["sources"] == ["API-Football", "365scores"] and f["exp_corners"] == 18.0 and "exp_corners" not in f["s365_keys"]
    # football-data (CSV « new » du Mexique : buts et cartons) passe avant : ses cartons restent, 365scores complète le reste
    fresh(serve=False)
    seed(key=MX)
    fd = {"teams": {nm: {"div": "MEX", "last": "2026-10-04", "all": {"n": 10, "goals_for": 1.4, "goals_against": 1.2, "cards_for": 2.2,
                                                                      "cards_against": 2.0},
                         "home": {"n": 5, "goals_for": 1.4, "goals_against": 1.2, "cards_for": 2.2, "cards_against": 2.0},
                         "away": {"n": 5, "goals_for": 1.4, "goals_against": 1.2, "cards_for": 2.2, "cards_against": 2.0}}
                   for nm in ("Team One", "Team Two")}, "leagues": {}, "referees": {}}
    features._cache["fd"] = fd
    f = features.match_features("Team One", "Team Two", MX)
    assert f["sources"][0] == "football-data" and f["sources"][-1] == "365scores", f["sources"]
    assert f["exp_cards"] == 4.2 and "exp_cards" not in f["s365_keys"] and f["exp_corners"] == 10.0 and f["exp_goals"] == 2.6
    assert not any(k.startswith("exp_goals") for k in f["s365_keys"])
    # stats d'un championnat africain (le chemin « africa_* » d'API-Football)
    fresh(serve=False)
    seed(key="africa_egypt_premier_league")
    f = features.match_features("Team One", "Team Two", "africa_egypt_premier_league")
    assert f["sources"] == ["365scores"] and f["exp_corners"] == 10.0


def test_features_name_matching_is_safe():
    fresh(serve=False)
    d = s365.load()
    clubs = {1: "Club América", 2: "Club América U20", 3: "América (W)", 4: "Atlético Nacional", 5: "Nacional", 6: "Cerro", 7: "Cerro Largo",
             8: "Atlético San Luis"}
    for tid, nm in clubs.items():
        for i in range(3):                                   # chacun joue 3 matchs à domicile contre « Rival » (qui en joue 24 à l'extérieur)
            s365.record(d, game(tid * 100 + i, f"2026-10-0{i + 1}", tid, 99, hn=nm, an="Rival", key=MX), MX)
    s365.save(d)
    features.reload()
    prof = s365.profiles()["leagues"][MX]["teams"]
    assert {"Club América", "Club América U20", "América (W)", "Atlético Nacional", "Nacional", "Cerro", "Cerro Largo"} <= set(prof)
    assert names.best_team("Club America", prof) == "Club América"
    assert names.best_team("Club America U20", prof) == "Club América U20"                 # les marqueurs jeunes / féminin ne se mélangent pas
    assert names.best_team("America W", prof) == "América (W)"
    assert names.best_team("Atletico Nacional", prof) == "Atlético Nacional" and names.best_team("Nacional", prof) == "Nacional"
    assert names.best_team("Cerro", prof) == "Cerro" and names.best_team("Cerro Largo", prof) == "Cerro Largo"
    assert names.best_team("Atletico San Luis", prof) == "Atlético San Luis"
    # « Chivas » (365scores) = « Guadalajara » ; « Guadalajara » n'est jamais « Atlas Guadalajara » (autre club) faute de profil de Chivas
    both, atlas = {"Atlas Guadalajara": 1, "Chivas": 2}, {"Atlas Guadalajara": 1}
    assert names.best_team("Guadalajara", both) == "Chivas" and names.best_team("Guadalajara Chivas", both) == "Chivas"
    assert names.best_team("Atlas", both) == "Atlas Guadalajara" and names.best_team("Guadalajara", atlas) is None
    assert names.best_team("Atlas Guadalajara", atlas) == "Atlas Guadalajara"
    # un club sans profil ne devient pas un homonyme : « Club América Premier » n'est ni « Club América » ni « Club América U20 »
    f = features.match_features("Club América Premier", "Rival", MX)
    assert f["sources"] == [], f
    f = features.match_features("Club América", "Atlético San Luis", MX)
    assert f["sources"] == ["365scores"] and f["home_team"] == "Club América" and f["away_team"] == "Atlético San Luis"
    f = features.match_features("América (W)", "Club América", MX)
    assert f["home_team"] == "América (W)" and f["away_team"] == "Club América"


def test_profiles_reload_on_store_change():
    fresh(serve=False)
    seed()
    assert features.match_features("Team One", "Team Two", UR)["sources"] == ["365scores"]
    d = s365.load()
    d["teams"] = {}
    s365.save(d)
    features._checked.clear()
    assert features.match_features("Team One", "Team Two", UR)["sources"] == []          # le bot qui tourne voit le nouveau document


def test_end_to_end_pronostics_from_365():
    """Un match d'un championnat 1xBet sans aucune autre source : sans 365scores aucun pronostic ; avec, des pronostics corners / cartons / tirs /
    fautes / touches / hors-jeu avec leur effectif honnête (4 matchs)."""
    from kelly import fair
    fresh(serve=False)
    assert fair.options(features.match_features("Team One", "Team Two", UR), "Team One", "Team Two") == []
    seed()
    f = features.match_features("Team One", "Team Two", UR)
    opts = fair.options(f, "Team One", "Team Two")
    markets = {o[0] for o in opts}
    assert {"alternate_totals_corners", "alternate_totals_cards", "team_shots_total", "team_sot_total", "team_fouls_total", "team_throws_total",
            "team_offsides_total", "team_throws_home", "team_fouls_away"} <= markets, markets
    assert opts and all("(4 derniers matchs seulement)" in o[4] for o in opts)
    assert fair.stat_n(f, "team_throws_total") == 4 and fair.stat_n(f, "alternate_totals_corners") == 4
    assert "365scores" in f["sources"] and fair.simple_q(f, "alternate_totals_corners") == f["aps_q"] == 0.25
    # moins de APISTATS_MIN_N matchs : aucun pronostic
    fresh(serve=False)
    seed(n=2)
    assert fair.options(features.match_features("Team One", "Team Two", UR), "Team One", "Team Two") == []


# --- rapport, ligne de commande, câblage ----------------------------------------------

def test_report_and_cli():
    fresh()
    assert "365scores" not in sources.report()                       # jamais lancé : le rapport ne bouge pas
    rep = s365.fill(max_lists=1, max_games=10, leagues=[UR])
    line = s365.report_line(rep)
    assert "3 match(s) ajouté(s) en 6 requête(s)" in line and "1 liste(s), 4 match(s) lus" in line and "5 équipes suivies" in line, line
    assert "✅ 365scores (stats de match) : 4 matchs lus" in sources.report()
    # bloqué : ligne ⛔ du rapport
    NET.routes[s365.list_url("soccer_xb_peru")] = Resp(403)
    s365.fill(leagues=["soccer_xb_peru"])
    assert state.is_blocked("s365") and "⛔ 365scores" in sources.report() and "bloqué" in s365.report_line(s365.fill())
    # fill_safe n'imprime que sur stderr et ne lève jamais
    fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert s365.fill_safe(max_lists=1, max_games=1, leagues=[UR])["added"] == 1
        real = s365.fill
        s365.fill = lambda **kw: 1 / 0
        try:
            assert s365.fill_safe() is None
        finally:
            s365.fill = real
    assert "[s365] 365scores (stats de match" in err.getvalue() and "[s365] ignoré : ZeroDivisionError" in err.getvalue()
    # ligne de commande
    fresh()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert s365._cli(["status"]) == 0
        assert s365._cli(["bogus"]) == 2
        NET.routes[s365._url("/search/")] = Resp(200, fx("search_excerpt.json"))
        assert s365._cli(["verify"]) == 0
        assert s365._cli(["fill", "--leagues=" + UR, "--requests=3", "--seconds=60"]) == 0
        assert s365._cli(["backfill", "--leagues=" + UR, "--requests=2"]) == 0
    assert f"{len(s365.COMPETITIONS)} championnats vérifiés" in out.getvalue() and "0 équipes" in out.getvalue()
    os.environ.pop(fmmatch.OWN_LOCK_ENV, None)
    assert not http.is_cached(s365._url("/search/"), None)                 # le catalogue (16 Mo) n'est pas gardé
    NET.routes[s365._url("/search/")] = Resp(200, b"not json")
    with contextlib.redirect_stdout(io.StringIO()):
        assert s365._cli(["verify"]) == 1


def test_sources_untouched_and_no_hot_dependency():
    fresh(serve=False)
    # 365scores n'entre pas dans SOURCES ni dans le rafraîchissement du matin : les requêtes du matin et health_all ne changent pas
    assert "s365" not in [m.NAME for m in sources.SOURCES] and "s365" not in sources.health_all()
    assert state.LABELS["s365"] == "365scores" and not C.S365_HOURS and C.S365_ENABLED is True
    assert 0 < C.S365_GAMES_PER_RUN <= 30 and 0 < C.S365_LISTS_PER_RUN <= 6 and C.S365_DAILY <= 300 and C.S365_SECONDS <= 150
    assert http.delay_for(s365.HOST) >= 3.0


# --- règlement des paris -------------------------------------------------------------

def bet(home, away, day, key=UR, market="team_throws_total"):
    return {"home": home, "away": away, "commence": f"{day}T20:00:00Z", "sport_key": key, "market": market, "group": "soccer"}


def test_match_stats_for_settlement():
    from kelly import settle
    net = fresh()
    s365.fill(max_lists=1, max_games=10, leagues=[UR])
    want = s365.parse_stats(fxj("stats_uruguay_4867341.json"))["s"]
    n = len(net.log)
    got = s365.match_stats(bet("Montevideo City Torque", "Danubio", "2026-10-09"))
    assert len(net.log) == n and got["throws"] == sum(want["throws"]) and got["corners"] == 13 and got["cards"] == sum(want["cards"])
    assert got["side"]["home"]["throws"] == want["throws"][0] and got["side"]["away"]["shots"] == want["shots"][1] and got["players"] == {}
    assert "goals" not in got and "goals" not in got["side"]["home"]
    # le format est celui que consomme settle : total du match et une équipe
    assert settle.shot_value("team_throws_total", got) == sum(want["throws"]) and settle.shot_value("team_shots_home", got) == want["shots"][0]
    assert settle.shot_value("team_offsides_away", got) == want["offsides"][1] and settle.shot_value("team_fouls_total", got) is None
    # un jour d'écart (fuseaux), noms approchés ; mais ni un autre jour, ni une autre ligue, ni une autre équipe, ni un club de jeunes
    assert s365.match_stats(bet("Montevideo City Torque FC", "Danubio FC", "2026-10-10"))["corners"] == 13
    assert s365.match_stats(bet("Montevideo City Torque", "Danubio", "2026-10-12")) is None
    assert s365.match_stats(bet("Montevideo City Torque", "Danubio", "2026-10-09", key="soccer_xb_peru")) is None
    assert s365.match_stats(bet("Montevideo City Torque", "Nacional", "2026-10-09")) is None
    assert s365.match_stats(bet("Montevideo City Torque U20", "Danubio", "2026-10-09")) is None
    assert s365.match_stats(dict(bet("Montevideo City Torque", "Danubio", "2026-10-09"), sport_key=None))["corners"] == 13      # ligue inconnue du pari
    assert s365.match_stats({"home": "x"}) is None
    # match listé mais pas encore lu : UNE requête, une seule fois (puis il est dans les matchs récents)
    net = fresh()
    s365.fill(max_lists=1, max_games=0, leagues=[UR])
    assert "4856756" in s365.load()["pending"] and net.data() == [net.data()[0]]
    b = bet("Montevideo City Torque", "Penarol", "2026-09-30")
    got = s365.match_stats(b)
    assert got and got["throws"] == 50 and got["offsides"] == 2 and [e[0] for e in net.data()[1:]] == [s365.stats_url(4856756)]
    n = len(net.log)
    assert s365.match_stats(b) == got and len(net.log) == n and "4856756" in s365.load()["seen"] and "4856756" not in s365.load()["pending"]
    assert not http.is_cached(s365.stats_url(4856756), None)
    # plafond par exécution, hors ligne, source bloquée, source coupée : aucune requête
    for setup in (lambda: setattr(C, "S365_SETTLE_REQUESTS", 0), lambda: setattr(kcache, "OFFLINE", True),
                  lambda: setattr(C, "S365_ENABLED", False), lambda: state.block("s365", "HTTP 403")):
        net = fresh()
        s365.fill(max_lists=1, max_games=0, leagues=[UR])
        setup()
        n = len(net.log)
        assert s365.match_stats(b) is None and len(net.log) == n and s365._settle["n"] == 0       # pas même tenté
    kcache.OFFLINE = False
    net = fresh()                                                       # un 429 pendant le règlement bloque la source et rend None
    s365.fill(max_lists=1, max_games=0, leagues=[UR])
    net.routes[s365.stats_url(4856756)] = Resp(429)
    assert s365.match_stats(b) is None and state.is_blocked("s365")
    net = fresh()                                                       # réponse illisible : None, pari laissé en attente
    s365.fill(max_lists=1, max_games=0, leagues=[UR])
    net.routes[s365.stats_url(4856756)] = Resp(200, b"<html>")
    assert s365.match_stats(b) is None and "4856756" in s365.load()["pending"]
    # les matchs récents ne sont gardés que S365_RECENT_DAYS jours
    net = fresh()
    s365.fill(max_lists=1, max_games=10, leagues=[UR])
    assert set(s365.load()["recent"]) == {"4867341", "4856756"}        # 10 jours depuis le 10/10 : le match du 26/09 n'y est déjà plus
    assert s365.match_stats(bet("Montevideo City Torque", "Peñarol", "2026-09-30"))
    CLOCK.t += 6 * 86400                                               # 16 octobre : le match du 30/09 est trop vieux aussi
    s365.save(s365.load())
    assert set(s365.load()["recent"]) == {"4867341"}
    assert s365.match_stats(bet("Montevideo City Torque", "Peñarol", "2026-09-30")) is None


def test_proxy_and_monthly_cap():
    """S365_PROXY : tout passe par le proxy Webshare de 1xBet (même fichier XBET_PROXY), compté dans data/s365_usage.json, plafond du mois ;
    sans proxy, rien n'est lu (jamais en direct) ; le proxy est retiré après le passage. Lecture simulée (_fill remplacé) : aucun réseau."""
    from kelly.sources import xbet
    fresh()
    seen = []

    def fake_fill(*a, **kw):
        seen.append(http.PROXIES.get(s365.HOST))
        w = http.WIRE.setdefault(s365.HOST, {"bytes": 0, "requests": 0})
        w["bytes"] += 3000
        w["requests"] += 2
        return {"added": 1, "games": 1, "lists": 1, "requests": 2, "left": 0, "errors": []}
    old = (s365._fill, xbet.proxy, C.S365_PROXY, C.S365_MONTH_MB, kcache.OFFLINE)
    try:
        kcache.OFFLINE = False
        s365._fill, C.S365_PROXY = fake_fill, True
        xbet.proxy = lambda: "http://user-GB-20:pw@proxy.test:80"
        rep = s365.fill(max_lists=1)
        assert seen == ["http://user-GB-20:pw@proxy.test:80"] and s365.HOST not in http.PROXIES, seen
        u = s365.usage()
        assert u["bytes"] == 3000 and u["requests"] == 2 and rep["added"] == 1, u
        assert "pw" not in s365.usage_line()                            # le mot de passe n'est jamais affiché
        xbet.proxy = lambda: None                                       # pas de proxy : rien n'est lu, jamais en direct
        rep = s365.fill(max_lists=1)
        assert rep.get("off") and "proxy introuvable" in rep["why"] and len(seen) == 1, rep
        xbet.proxy = lambda: "http://user-GB-20:pw@proxy.test:80"
        C.S365_MONTH_MB = 0.002                                         # plafond du mois atteint (3 000 octets >= 2 000) : rien lu
        rep = s365.fill(max_lists=1)
        assert rep.get("off") and "plafond" in rep["why"] and len(seen) == 1, rep
        C.S365_PROXY = False                                            # S365_PROXY=0 : connexion directe, pas de compteur
        s365.fill(max_lists=1)
        assert seen[-1] is None and s365.usage()["bytes"] == 3000
    finally:
        s365._fill, xbet.proxy, C.S365_PROXY, C.S365_MONTH_MB, kcache.OFFLINE = old


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
    finally:
        requests.get = _orig_get
        for k, v in _C0.items():
            setattr(C, k, v)
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests)} tests 365scores : OK")


if __name__ == "__main__":
    main()
