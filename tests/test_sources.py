"""Tests hors ligne de kelly/sources (aucun réseau). Lancer : python -m tests.test_sources"""
import contextlib
import io
import json
import math
import os
import shutil
import tempfile
import time
from datetime import date, datetime, timezone

TMP = tempfile.mkdtemp(prefix="kelly-sources-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ.pop("FBREF_MAX_PAGES", None)

import requests  # noqa: E402

from kelly import bootstrap, config as C, learn, sources, store  # noqa: E402
from kelly.sources import betexplorer, fbref, features, fmmatch, footballdata as fd, fotmob, http, names, state, understat  # noqa: E402

from tests import synth  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sources")
T0 = datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


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
    """requests.get factice : routes = {url: Resp | [Resp, ...] (séquence) | callable(headers)}."""

    def __init__(self, clock):
        self.clock, self.routes, self.log = clock, {}, []

    def get(self, url, headers=None, timeout=None, **kw):
        self.log.append((url, dict(headers or {}), self.clock.t))
        self.clock.t += 0.2                                   # latence
        r = self.routes.get(url)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(headers or {})
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else Resp(404, b"not found")

    def hits(self, needle):
        return [e for e in self.log if needle in e[0]]

    def data_hits(self):
        return [e for e in self.log if not e[0].endswith("robots.txt")]


CLOCK, NET = Clock(), None
_orig_get = requests.get
ROBOTS_OK = Resp(200, b"User-agent: *\nDisallow: /private/\n")
FD, US, FB = "football-data.co.uk", "understat.com", "fbref.com"
API_US = "https://understat.com/getLeagueData/EPL/2025"
EPL_FD = "https://football-data.co.uk/mmz4281/2526/E0.csv"
EPL_US = "https://understat.com/league/EPL/2025"
EPL_FB = "https://fbref.com/en/comps/9/{}/Premier-League-Stats"

def corrupt(name):
    """Document illisible dans la base (JSON cassé), comme un ancien fichier abîmé."""
    with store._db() as con:
        con.execute("INSERT OR REPLACE INTO docs (name, body, updated) VALUES (?, '{pas du json', 'x')", (name,))



def fresh(serve=True):
    """Données, cache, horloge et faux réseau neufs."""
    global NET
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    store.DATA_DIR = os.path.join(TMP, "data")
    os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
    http.reset()
    features.reload()
    CLOCK.t = T0
    state.clock, http._sleep = CLOCK, CLOCK.sleep
    NET = Net(CLOCK)
    requests.get = NET.get
    if serve:
        for d in (FD, US, FB, FM, BX):
            NET.routes[f"https://{d}/robots.txt"] = ROBOTS_OK
        NET.routes[EPL_FD] = Resp(200, fixture("E0_2526.csv"), {"ETag": '"abc"'})
        NET.routes["https://football-data.co.uk/mmz4281/2425/E0.csv"] = Resp(404)
        NET.routes[API_US] = Resp(200, fixture("understat_api_epl_2025.json"), {"Content-Type": "text/javascript;charset=UTF-8"})
        NET.routes[EPL_FB.format("shooting")] = Resp(200, fixture("fbref_epl_shooting.html"))
        NET.routes[EPL_FB.format("misc")] = Resp(200, fixture("fbref_epl_misc.html"))
        for kid in set(fotmob.SPORT_TO_LEAGUE.values()) | set(fotmob.EXTRA_LEAGUES.values()):
            NET.routes[f"https://{FM}/leagues/{kid}"] = Resp(200, _fotmob_html())
        for path in set(betexplorer.PATHS.values()) | set(betexplorer.EXTRA_PATHS.values()):
            NET.routes[f"https://{BX}/football/{path}/results/"] = Resp(200, fixture("betexplorer_ci_results.html"))
    return NET


def close(a, b, tol=0.02):
    return abs(a - b) <= tol


# --- football-data ------------------------------------------------------------

def test_csv_parsing():
    text = fixture("E0_2526.csv").decode("utf-8-sig")
    rows, probs = fd.parse_csv(text, "E0", "2526")
    assert len(rows) == 8 and probs == [], (len(rows), probs)       # non joué et ligne vide ignorés
    m = rows[0]
    assert (m["date"], m["home"], m["away"], m["hg"], m["ag"], m["ftr"]) == ("2025-08-16", "Arsenal", "Chelsea", 2, 1, "H")
    assert (m["hs"], m["as"], m["hst"], m["ast"], m["hf"], m["af"], m["hc"], m["ac"]) == (15, 9, 6, 3, 10, 12, 7, 3)
    assert (m["hy"], m["ay"], m["hr"], m["ar"], m["referee"]) == (1, 3, 0, 0, "M Oliver")
    o = m["odds"]
    assert o["1x2"]["open"]["avg"] == [1.9, 3.6, 4.2] and o["1x2"]["close"]["avg"] == [1.85, 3.7, 4.4]
    assert o["1x2"]["close"]["ps"] == [1.87, 3.75, 4.45] and o["1x2"]["close"]["max"] == [1.9, 3.9, 4.7]
    assert o["ou25"]["open"]["avg"] == [1.8, 2.05] and o["ou25"]["close"]["avg"] == [1.75, 2.1]
    assert o["ou25"]["close"]["ps"] == [1.77, 2.12]
    assert o["ah"]["open"]["line"] == -0.5 and o["ah"]["close"]["line"] == -0.75
    assert o["ah"]["close"]["avg"] == [2.04, 1.87] and o["ah"]["open"]["ps"] == [1.96, 1.97]
    assert rows[1]["odds"] == {}                                    # pas de cotes : dict vide, pas d'erreur
    p = fd.no_vig(o["1x2"]["close"]["avg"])
    assert p and close(sum(p), 1, 1e-9) and p[0] > p[1] > p[2]
    # BOM tolérée même si le texte arrive brut
    assert fd.parse_csv("﻿" + text, "E0", "2526")[0][0]["home"] == "Arsenal"

    # latin-1 : accents conservés, colonnes de stats absentes tolérées
    res = http.Result("u", fixture("SP1_2526_latin1.csv"))
    assert "Muñiz Ruiz" in res.text
    sp, probs = fd.parse_csv(res.text, "SP1", "2526")
    assert len(sp) == 4 and sp[0]["home"] == "Alavés" and sp[0]["referee"] == "Muñiz Ruiz"
    assert sp[0]["hf"] is None and sp[0]["hc"] is None and sp[0]["hs"] == 7 and sp[0]["hy"] == 3
    assert any("colonnes de stats absentes" in p for p in probs)
    assert sp[0]["odds"] == {"1x2": {"open": {"b365": [5.5, 3.9, 1.6]}}}      # seules les cotes présentes ressortent
    assert fd.parse_csv(res.text)[0][0]["div"] == "SP1"

    # vrais fichiers football-data (sonde GitHub Actions) : BOM UTF-8, ~150 colonnes, bookmakers en plus
    raw = fixture("E0_2526_real.csv")
    assert raw.startswith(b"\xef\xbb\xbfDiv,Date,Time,HomeTeam")
    for text_ in (http.Result("u", raw).text, raw.decode("latin-1")):          # BOM bien lue ou lue en latin-1 (« ï»¿Div »)
        real, probs = fd.parse_csv(text_, "E0", "2526")
        assert len(real) == 24 and probs == [], (len(real), probs)
    m = real[0]
    assert (m["date"], m["home"], m["away"], m["hg"], m["ag"], m["referee"]) == ("2025-08-15", "Liverpool", "Bournemouth", 4, 2, "A Taylor")
    assert (m["hs"], m["as"], m["hst"], m["ast"], m["hf"], m["af"], m["hc"], m["ac"], m["hy"], m["ay"]) == (19, 10, 10, 3, 7, 10, 6, 7, 1, 2)
    assert m["odds"]["1x2"]["open"]["avg"] == [1.31, 5.96, 8.31] and m["odds"]["1x2"]["close"]["avg"] == [1.29, 6.02, 8.68]
    assert m["odds"]["1x2"]["close"]["ps"] == [1.29, 6.55, 9.75] and m["odds"]["ou25"]["close"]["avg"] == [1.36, 3.05]
    assert m["odds"]["ah"]["open"]["line"] == -1.5 and m["odds"]["ah"]["close"]["line"] == -1.75
    assert m["odds"]["ah"]["close"]["avg"] == [1.94, 1.76]
    old, _ = fd.parse_csv(http.Result("u", fixture("E0_2425_real.csv")).text, "E0", "2425")
    assert len(old) == 24 and old[0]["home"] == "Man United" and old[0]["referee"] == "R Jones"
    assert all(x["odds"]["1x2"]["close"]["avg"] for x in real if x["odds"].get("1x2", {}).get("close"))

    # minimal : dd/mm/yy, lignes vides, virgule finale, date illisible
    i1, probs = fd.parse_csv(fixture("I1_2425_min.csv").decode())
    assert [r["date"] for r in i1] == ["2024-08-17", "2024-08-18", "2024-08-25"], i1
    assert any("illisible" in p for p in probs)
    assert fd.parse_date("1/9/99") == "1999-09-01" and fd.parse_date("2025-10-04") == "2025-10-04"
    assert fd.parse_date("31/02/2025") is None and fd.parse_date("") is None

    # alias « nouveau format » (Home/Away/HG/AG)
    new, _ = fd.parse_csv("Country,League,Season,Date,Time,Home,Away,HG,AG,Res\nUSA,MLS,2025,01/03/2025,01:00,A,B,1,0,H\n")
    assert new[0]["home"] == "A" and new[0]["hg"] == 1

    # structure modifiée : jamais d'exception, message clair
    for bad in ("", "<html><body>Maintenance</body></html>", "Foo,Bar\n1,2\n"):
        rows, probs = fd.parse_csv(bad)
        assert rows == [] and probs, bad
    assert "structure" in fd.parse_csv("<html>x</html>")[1][0]
    assert "colonnes manquantes" in fd.parse_csv("Foo,Bar\n1,2\n")[1][0]


def test_seasons_and_mapping():
    assert fd.current_season(date(2025, 12, 1)) == "2526" and fd.current_season(date(2026, 10, 8)) == "2627"
    assert fd.current_season(date(2026, 3, 1)) == "2526" and fd.current_season(date(2026, 7, 1)) == "2627"
    assert fd.previous_season("2526") == "2425" and fd.previous_season("0001") == "9900"
    assert fd.url("E0", "2526") == "https://football-data.co.uk/mmz4281/2526/E0.csv"      # sans www (pas de redirection)
    assert fd.SPORT_TO_DIV["soccer_epl"] == "E0" and fd.SPORT_TO_DIV["soccer_spain_la_liga"] == "SP1"
    assert fd.SPORT_TO_DIV["soccer_italy_serie_a"] == "I1" and fd.SPORT_TO_DIV["soccer_germany_bundesliga"] == "D1"
    assert fd.SPORT_TO_DIV["soccer_france_ligue_one"] == "F1"
    assert fd.divs_for(["soccer_epl", "tennis_atp", "soccer_epl"]) == ["E0"]
    assert understat.SPORT_TO_LEAGUE["soccer_epl"] == "EPL" and understat.SPORT_TO_LEAGUE["soccer_russia_premier_league"] == "RFPL"
    assert len(understat.LEAGUES) == 6


def test_team_profiles():
    rows, _ = fd.parse_csv(fixture("E0_2526.csv").decode("utf-8-sig"), "E0", "2526")
    old = {"date": "2023-01-01", "div": "E0", "home": "Vieux FC", "away": "Arsenal", "hg": 0, "ag": 0, "hs": 1, "as": 1,
           "hst": 0, "ast": 0, "hf": 1, "af": 1, "hc": 1, "ac": 1, "hy": 0, "ay": 0, "hr": 0, "ar": 0, "referee": None, "odds": {}}
    assert "Vieux FC" not in fd.team_profiles(rows + [old])["teams"]    # plus aucun match récent : écarté
    prof = fd.team_profiles(rows)
    ars = prof["teams"]["Arsenal"]
    assert ars["div"] == "E0" and ars["last"] == "2025-11-22"
    a, h, w = ars["all"], ars["home"], ars["away"]
    # Arsenal : domicile #1, #5, #8 ; extérieur #4, #7 (corners 7,5,6 / 2,4 ; contre 3,6,5 / 8,7)
    assert (a["n"], h["n"], w["n"]) == (5, 3, 2), (a["n"], h["n"], w["n"])
    assert close(a["corners_for"], 4.8) and close(a["corners_against"], 5.8)
    assert close(h["corners_for"], 6.0) and close(h["corners_against"], 4.67)
    assert close(w["corners_for"], 3.0) and close(w["corners_against"], 7.5)
    assert close(a["cards_for"], 2.0) and close(a["shots_for"], 10.6) and close(a["sot_for"], 4.6)
    assert close(a["fouls_for"], 10.8) and close(a["goals_for"], 1.6) and close(a["goals_against"], 1.0)
    # dernière fenêtre : last_n=3 -> #5, #7, #8 (corners 5, 4, 6)
    p3 = fd.team_profiles(rows, last_n=3)["teams"]["Arsenal"]
    assert p3["all"]["n"] == 3 and close(p3["all"]["corners_for"], 5.0)
    refs = prof["referees"]
    assert refs["M Oliver"]["n"] == 4 and close(refs["M Oliver"]["cards"], 4.5) and close(refs["M Oliver"]["fouls"], 21.25)
    assert refs["A Taylor"]["n"] == 3 and close(refs["A Taylor"]["cards"], 3.67) and close(refs["A Taylor"]["fouls"], 19.0)
    assert refs["S Attwell"]["n"] == 1
    lg = prof["leagues"]["E0"]
    assert lg["n"] == 8 and close(lg["corners"], 11.0) and close(lg["cards"], 4.25)
    # colonnes absentes : pas de clé, pas d'erreur
    sp, _ = fd.parse_csv(fixture("SP1_2526_latin1.csv").decode("latin-1"))
    sprof = fd.team_profiles(sp)
    assert "corners_for" not in sprof["teams"]["Barcelona"]["all"] and "shots_for" in sprof["teams"]["Barcelona"]["all"]
    assert fd.team_profiles([]) == {"teams": {}, "referees": {}, "leagues": {}}
    assert len(json.dumps(prof)) < 20000


# --- Understat -----------------------------------------------------------------

def test_understat_extraction():
    assert understat.decode_js(r"caf\xE9 \u0141\u00f3d\u017A \x22q\x22 it\'s \\ ok") == 'café Łódź "q" it\'s \\ ok'
    assert json.loads(understat.decode_js(r"\x22a\/b\x22")) == "a/b"                    # \/ toléré par json
    assert understat.decode_js(r"\xC3\xA9t\xC3\xA9") == "été"                      # UTF-8 lu comme latin-1
    vs, probs = understat.extract_vars("var a = JSON.parse('\\x5B1\\x2C2\\x5D'); var b=JSON.parse(\"\\u007B\\u0022k\\u0022:3\\u007D\"); var c = JSON.parse('\\x7Bpas json')")
    assert vs["a"] == [1, 2] and vs["b"] == {"k": 3} and "c" not in vs and probs == ["variable c illisible"]
    html = fixture("understat_epl_2025.html").decode()
    data, probs = understat.parse_league(html)
    assert probs == [] and sorted(data["teams"]) == ["Arsenal", "Chelsea", "Liverpool", "Manchester City"]
    assert len(data["teams"]["Arsenal"]) == 4 and len(data["players"]) == 6 and len(data["dates"]) == 1
    assert any(p["player_name"] == "Martin Ødegaard" for p in data["players"])     # \xD8 décodé
    built = understat.build_league(data)
    ars = built["teams"]["Arsenal"]
    assert ars["all"]["n"] == 4 and close(ars["all"]["xg"], 1.875) and close(ars["all"]["xga"], 1.375)
    assert close(ars["home"]["xg"], 2.5) and close(ars["away"]["xg"], 1.25) and close(ars["home"]["xga"], 0.75)
    assert close(ars["all"]["ppda"], 9.75) and ars["home"]["n"] == 2
    last2 = understat.team_form(data["teams"]["Arsenal"], last_n=2)["all"]       # 2 derniers : 3.0/1.0 et 1.5/2.5
    assert last2["n"] == 2 and close(last2["xg"], 2.25) and close(last2["xga"], 1.75)
    pl = built["players"]
    assert close(pl["Manchester City"]["Erling Haaland"]["shots90"], 4.0, 1e-9)
    assert close(pl["Arsenal"]["Bukayo Saka"]["shots90"], 30 * 90 / 1020) and close(pl["Arsenal"]["Bukayo Saka"]["xg90"], 5.1 * 90 / 1020)
    assert "Joueur Remplaçant" not in pl["Arsenal"]                                # < 450 minutes
    # vrai extrait de /getLeagueData/EPL/2025 (sonde) : teams seul, joueurs absents signalés
    real, probs = understat.parse_league_json(fixture("understat_api_league_real.json").decode())
    assert list(real["teams"]) == ["Aston Villa"] and len(real["teams"]["Aston Villa"]) == 19 and probs == ["playersData absent (pas de stats joueurs)"]
    av = understat.build_league(real)["teams"]["Aston Villa"]
    assert av["all"]["n"] == 6 and av["home"]["n"] == 6 and av["all"]["xg"] > 0 and av["all"]["ppda"] > 5 and real["players"] == []
    # clés alternatives teamsData / playersData dans la réponse JSON
    alt, _ = understat.parse_league_json(json.dumps({"teamsData": {"1": {"title": "A", "history": [{"xG": 1, "xGA": 1, "h_a": "h", "date": "2025-01-01"}]}}}))
    assert alt and list(alt["teams"]) == ["A"]
    api, probs = understat.parse_league_json(fixture("understat_api_epl_2025.json").decode())
    assert probs == [] and understat.build_league(api) == understat.build_league(data)
    mt, probs = understat.parse_match(fixture("understat_match_9001.html").decode())
    assert probs == [] and mt["shots"]["h"][0]["player"] == "Bukayo Saka" and mt["shots"]["h"][0]["player_assisted"] == "Łukasz Fabiański"
    assert mt["rosters"]["a"]["21"]["yellow_card"] == "1"
    # structure modifiée
    none, probs = understat.parse_league("<html><script>var x = 1;</script></html>")
    assert none is None and "structure de la page modifiée" in probs[0] and "teamsData" in probs[0]
    none, probs = understat.parse_league("var teamsData = JSON.parse('\\x7B\\x7D');")
    assert none is None and "aucune équipe" in probs[0]
    bad = "var teamsData = JSON.parse('" + json.dumps({"1": {"title": "A", "history": [{"foo": 1}]}}).replace('"', "\\x22") + "');"
    none, probs = understat.parse_league(bad)
    assert none is None and "champs" in probs[0]
    assert understat.parse_match("<html></html>")[0] is None
    api, probs = understat.parse_league_json(json.dumps({"teams": {"1": {"title": "A", "history": [
        {"xG": 1, "xGA": 1, "h_a": "h", "date": "2025-01-01"}]}}, "players": [], "dates": []}))
    assert api and list(api["teams"]) == ["A"] and any("playersData" in p for p in probs)
    assert understat.parse_league_json("pas du json")[0] is None


# --- FBref ---------------------------------------------------------------------

def test_fbref_tables():
    shoot = fbref.parse_tables(fixture("fbref_epl_shooting.html").decode())
    misc = fbref.parse_tables(fixture("fbref_epl_misc.html").decode())
    assert set(shoot) == {"stats_squads_shooting_for", "stats_squads_shooting_against", "stats_shooting"}   # joueurs : commentaire
    assert set(misc) == {"stats_squads_misc_for", "stats_squads_misc_against", "stats_misc"}                 # tout en commentaire
    sq = shoot["stats_squads_shooting_for"]
    assert len(sq) == 4 and sq[0]["team"] == "Arsenal" and sq[0]["shots"] == "140"           # en-têtes répétés et espaceurs écartés
    assert shoot["stats_squads_shooting_against"][0]["team"] == "vs Arsenal"
    pl = shoot["stats_shooting"]
    assert pl[0]["player"] == "Bukayo Saka" and pl[0]["_id"] == "bc7dc64d" and pl[0]["team"] == "Arsenal"
    assert fbref.num("1,234") == 1234.0 and fbref.num("45.5%") == 45.5 and fbref.num("") is None
    pages = {"shooting": shoot, "misc": misc}
    players = fbref.build_players(pages)
    s = players["Arsenal"]["Bukayo Saka"]
    assert (s["shots90"], s["sot90"], s["fouls90"], s["fouled90"], s["cards90"], s["yc90"]) == (3.0, 1.2, 1.0, 2.5, 0.2, 0.2)
    assert close(players["Chelsea"]["Cole Palmer"]["cards90"], 3 / 9.5) and close(players["Chelsea"]["Cole Palmer"]["fouls90"], 2.0)
    assert "Martin Ødegaard" in players["Arsenal"] and "Remplaçant Un" not in players["Arsenal"]   # < 3 matchs
    assert players["Manchester City"]["Erling Haaland"]["shots90"] == 4.0
    squads = fbref.build_squads(pages)
    a = squads["Arsenal"]
    assert a["games"] == 10.0 and a["for"]["shots_pm"] == 14.0 and a["for"]["sot_pm"] == 5.0 and a["for"]["fouls_pm"] == 10.0
    assert close(a["for"]["cards_pm"], 1.7) and a["against"]["shots_pm"] == 9.0 and a["against"]["fouled_pm"] == 10.0
    assert fbref.check_structure("shooting", shoot) == [] and fbref.check_structure("misc", misc) == []
    # structure modifiée
    assert "structure de la page modifiée" in fbref.check_structure("misc", {})[0]
    no_col = fbref.parse_tables('<table id="stats_misc"><tbody><tr><td data-stat="player">X</td><td data-stat="team">Y</td></tr></tbody></table>')
    assert "fouls" in fbref.check_structure("misc", no_col)[0]
    assert "absents" in fbref.check_structure("misc", {"stats_other": [{"a": "1"}]})[0]
    # HTML cassé : pas d'exception
    assert fbref.parse_tables("<table id=x><tr><td data-stat=a>1</td></tr><div><!-- <table id=y><tr><td") is not None
    assert fbref.parse_tables("") == {} and fbref.parse_tables(None) == {}


def test_names():
    assert names.team_sim("Nott'm Forest", "Nottingham Forest") >= 0.8
    assert names.team_sim("Man United", "Manchester United") >= 0.8 and names.team_sim("Ath Madrid", "Atletico Madrid") >= 0.8
    assert names.team_sim("Paris SG", "Paris Saint-Germain") >= 0.8 and names.team_sim("Borussia M.Gladbach", "Borussia Mönchengladbach") >= 0.8
    assert names.team_sim("Arsenal", "Arsenal W") == 0 and names.team_sim("Arsenal", "Chelsea") < 0.6
    assert names.best_team("Man City", ["Arsenal", "Manchester City", "Chelsea"]) == "Manchester City"
    assert names.best_team("Brentford", ["Arsenal", "Chelsea"]) is None
    assert names.player_match("Martin Odegaard", ["Bukayo Saka", "Martin Ødegaard"]) == "Martin Ødegaard"
    assert names.player_match("Heung-Min Son", ["Son Heung-Min", "Ben Davies"]) == "Son Heung-Min"
    assert names.player_match("B. Saka", ["Bukayo Saka"]) == "Bukayo Saka"
    assert names.player_match("J. Evans", ["Jonny Evans", "Jonathan Evans"]) is None          # ambigu
    assert names.player_match("Michael Oliver", ["M Oliver", "A Taylor"]) == "M Oliver"


# --- téléchargement poli ---------------------------------------------------------

def test_robots_and_user_agent():
    net = fresh()
    net.routes[f"https://{FD}/robots.txt"] = Resp(200, b"User-agent: *\nDisallow: /mmz4281/\n")
    r = http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT)
    assert not r.ok and "robots.txt interdit" in r.error and not r.blocked
    assert net.data_hits() == [], "aucune requête de données ne doit partir"
    assert not state.is_blocked(fd.NAME)
    h = fd.refresh(["soccer_epl"])                                    # robots interdit tout : erreur claire, pas d'exception
    assert h["ok"] is False and "robots.txt interdit" in h["last_error"]
    assert net.data_hits() == []
    # une règle plus fine laisse passer
    fresh()
    NET.routes[f"https://{FD}/robots.txt"] = Resp(200, b"User-agent: KellyBot\nDisallow: /private/\n\nUser-agent: *\nDisallow: /\n")
    assert http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT).ok
    assert all(e[1]["User-Agent"] == http.UA for e in NET.log), NET.log
    # robots.txt injoignable (5xx) : on ne télécharge pas
    fresh()
    NET.routes[f"https://{FD}/robots.txt"] = Resp(503)
    r = http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT)
    assert not r.ok and "injoignable" in r.error and NET.data_hits() == []
    # robots.txt absent (404) : tout est permis
    fresh()
    NET.routes[f"https://{FD}/robots.txt"] = Resp(404)
    assert http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT).ok
    # robots.txt en cache disque pour 24 h
    n = len(NET.hits("robots.txt"))
    http.fetch("https://football-data.co.uk/mmz4281/2526/SP1.csv", fd.NAME, http.TTL_CSV_CURRENT)
    assert len(NET.hits("robots.txt")) == n


def test_rate_limit():
    net = fresh()
    pages = [EPL_FB.format(k) for k in ("shooting", "misc", "passing_types", "standard")]
    for p in pages:
        net.routes[p] = Resp(200, b"<html>ok</html>")
    for p in pages:
        assert http.fetch(p, fbref.NAME, http.TTL_FBREF).ok
    ts = [e[2] for e in net.log]                                      # robots.txt compris
    assert len(ts) == 5 and all(b - a >= 6.0 for a, b in zip(ts, ts[1:])), ts
    fresh()
    for u in ("https://football-data.co.uk/mmz4281/2526/SP1.csv", "https://football-data.co.uk/mmz4281/2526/I1.csv"):
        NET.routes[u] = Resp(200, b"Div,Date\n")
    http.fetch("https://football-data.co.uk/mmz4281/2526/SP1.csv", fd.NAME, 0)
    http.fetch("https://football-data.co.uk/mmz4281/2526/I1.csv", fd.NAME, 0)
    ts = [e[2] for e in NET.log]
    assert len(ts) == 3 and all(b - a >= 3.0 for a, b in zip(ts, ts[1:])), ts
    # le délai ne peut pas descendre sous le plancher, même si on le configure
    http.MIN_DELAY["fbref.com"] = 0.5
    try:
        assert http.delay_for("fbref.com") >= 6.0 and http.delay_for("understat.com") >= 3.0
    finally:
        http.MIN_DELAY["fbref.com"] = 6.0
    # Crawl-delay de robots.txt plus grand : respecté
    fresh()
    NET.routes[f"https://{FD}/robots.txt"] = Resp(200, b"User-agent: *\nCrawl-delay: 10\n")
    for u in ("SP1", "I1"):
        NET.routes[f"https://{FD}/mmz4281/2526/{u}.csv"] = Resp(200, b"Div,Date\n")
        http.fetch(f"https://{FD}/mmz4281/2526/{u}.csv", fd.NAME, 0)
    ts = [e[2] for e in NET.log]
    assert ts[2] - ts[1] >= 10.0, ts


def test_cache_conditional_retry():
    net = fresh()
    r = http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT)
    assert r.ok and not r.from_cache and r.text.startswith("﻿") is False
    assert os.path.isdir(os.environ["SOURCES_CACHE_DIR"]) and len(net.hits("E0.csv")) == 1
    assert http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT).from_cache and len(net.hits("E0.csv")) == 1   # TTL 12 h
    CLOCK.t += 13 * HOUR
    net.routes[EPL_FD] = lambda h: Resp(304) if h.get("If-None-Match") == '"abc"' else Resp(200, b"x")
    r = http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT)
    assert r.ok and r.from_cache and r.content == fixture("E0_2526.csv") and len(net.hits("E0.csv")) == 2
    assert net.hits("E0.csv")[-1][1]["If-None-Match"] == '"abc"'
    assert http.fetch(EPL_FD, fd.NAME, http.TTL_CSV_CURRENT).from_cache and len(net.hits("E0.csv")) == 2      # fraîcheur relancée
    # saisons passées : jamais rafraîchies
    CLOCK.t += 400 * 24 * HOUR
    assert http.fetch(EPL_FD, fd.NAME, http.FOREVER).from_cache and len(net.hits("E0.csv")) == 2
    # Last-Modified
    fresh()
    NET.routes[EPL_FD] = Resp(200, b"a", {"Last-Modified": "Mon, 01 Dec 2025 10:00:00 GMT"})
    http.fetch(EPL_FD, fd.NAME, 60)
    CLOCK.t += 120
    NET.routes[EPL_FD] = lambda h: Resp(304) if h.get("If-Modified-Since") else Resp(200, b"b")
    assert http.fetch(EPL_FD, fd.NAME, 60).content == b"a"
    # un seul nouvel essai sur 5xx
    fresh()
    NET.routes[EPL_FD] = [Resp(500), Resp(200, b"ok")]
    r = http.fetch(EPL_FD, fd.NAME, 60)
    assert r.ok and len(NET.hits("E0.csv")) == 2
    fresh()
    NET.routes[EPL_FD] = [Resp(502)]
    r = http.fetch(EPL_FD, fd.NAME, 60)
    assert not r.ok and "HTTP 502" in r.error and len(NET.hits("E0.csv")) == 2 and not state.is_blocked(fd.NAME)
    fresh()
    NET.routes[EPL_FD] = [requests.Timeout("lent"), Resp(200, b"ok")]
    assert http.fetch(EPL_FD, fd.NAME, 60).ok and len(NET.hits("E0.csv")) == 2
    fresh()
    NET.routes[EPL_FD] = requests.ConnectionError("coupé")
    r = http.fetch(EPL_FD, fd.NAME, 60)
    assert not r.ok and "réseau" in r.error and len(NET.hits("E0.csv")) == 2
    # 404 : erreur simple, pas de blocage ; contenu périmé servi si le réseau échoue
    fresh()
    assert "404" in http.fetch("https://football-data.co.uk/mmz4281/2526/ZZ.csv", fd.NAME, 60).error
    assert not state.is_blocked(fd.NAME)
    fresh()
    http.fetch(EPL_FD, fd.NAME, 60)
    CLOCK.t += 3600
    NET.routes[EPL_FD] = Resp(500)
    r = http.fetch(EPL_FD, fd.NAME, 60)
    assert r.ok and r.stale and "HTTP 500" in r.error


def test_blocked_403_and_isolation():
    net = fresh()
    net.routes[API_US] = Resp(403, b"Forbidden")
    out = sources.refresh_all(["soccer_epl"])
    assert out["understat"]["ok"] is False and out["understat"]["blocked_until"]
    assert "bloquée" in out["understat"]["last_error"] and "403" in out["understat"]["last_error"]
    until = datetime.fromisoformat(out["understat"]["blocked_until"])
    assert abs((until - datetime.fromtimestamp(CLOCK.t, timezone.utc)).total_seconds() - 24 * HOUR) < 1800   # pages FotMob lues après
    # les autres sources ont tourné normalement
    assert out["footballdata"]["ok"] is True and out["footballdata"]["rows"] > 0
    assert out["fbref"]["ok"] is True and out["fbref"]["rows"] > 0
    assert store.load("src_footballdata.json", {})["teams"]["Arsenal"] and store.load("src_fbref.json", {})["leagues"]
    # blocage persistant (état dans data/) : plus aucune requête vers Understat pendant 24 h
    n = len(net.hits("getLeagueData"))
    assert n == 1                                                      # 403 : on s'arrête net, pas de repli HTML
    http.reset()
    out2 = sources.refresh_all(["soccer_epl"])
    assert len(net.hits("getLeagueData")) == n and out2["understat"]["blocked_until"]
    CLOCK.t += 23 * HOUR
    sources.refresh_all(["soccer_epl"])
    assert len(net.hits("getLeagueData")) == n
    # après 24 h : nouvel essai
    CLOCK.t += 2 * HOUR
    net.routes[API_US] = Resp(200, fixture("understat_api_epl_2025.json"))
    out3 = sources.refresh_all(["soccer_epl"])
    assert len(net.hits("getLeagueData")) == n + 1 and out3["understat"]["ok"] is True and not out3["understat"]["blocked_until"]
    # 429 et page anti-robot : même traitement
    fresh()
    NET.routes[EPL_FB.format("shooting")] = Resp(429, b"Too Many Requests")
    out = sources.refresh_all(["soccer_epl"])
    assert out["fbref"]["blocked_until"] and len(NET.hits("fbref.com/en/comps")) == 1        # arrêt net après le refus
    assert out["footballdata"]["ok"] and out["understat"]["ok"]
    fresh()
    NET.routes[API_US] = Resp(200, b"<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>")
    out = sources.refresh_all(["soccer_epl"])
    assert out["understat"]["blocked_until"] and "anti-robot" in out["understat"]["last_error"]
    # défi Cloudflare réel (sonde FBref : HTTP 403 + Cf-Mitigated: challenge) : désactivée 7 jours, rien d'autre ne casse
    fresh()
    NET.routes[EPL_FB.format("shooting")] = Resp(403, b"<html>challenge</html>", {"Cf-Mitigated": "challenge", "Server": "cloudflare"})
    out = sources.refresh_all(["soccer_epl"])
    fb = out["fbref"]
    assert fb["ok"] is False and "FBref bloque les robots (Cloudflare) : source désactivée" in fb["last_error"]
    until = datetime.fromisoformat(fb["blocked_until"])
    assert abs((until - datetime.fromtimestamp(CLOCK.t, timezone.utc)).total_seconds() - 7 * 24 * HOUR) < 1800
    assert len(NET.hits("fbref.com/en/comps")) == 1 and out["footballdata"]["ok"] and out["understat"]["ok"]
    assert "⛔ FBref bloque les robots (Cloudflare)" in sources.report()
    f = sources.match_features("Arsenal", "Man City", "soccer_epl")
    assert f["sources"] == ["football-data", "understat"] and "exp_corners" in f and "exp_xg_total" in f
    CLOCK.t += 3 * 24 * HOUR
    sources.refresh_all(["soccer_epl"])
    assert len(NET.hits("fbref.com/en/comps")) == 1                    # toujours désactivée après 3 jours
    CLOCK.t += 5 * 24 * HOUR
    sources.refresh_all(["soccer_epl"])
    assert len(NET.hits("fbref.com/en/comps")) == 2                    # un seul nouvel essai au bout d'une semaine
    fresh()
    os.environ["FBREF_ENABLED"] = "0"
    try:
        sources.refresh_all(["soccer_epl"])
        assert not NET.hits("fbref.com") and "⚪ FBref : désactivée" in sources.report()
    finally:
        os.environ.pop("FBREF_ENABLED", None)
    # robots.txt qui répond 403 : bloqué aussi
    fresh()
    NET.routes[f"https://{FB}/robots.txt"] = Resp(403)
    out = sources.refresh_all(["soccer_epl"])
    assert out["fbref"]["blocked_until"] and out["footballdata"]["ok"]
    txt = sources.report()
    assert "⛔ FBref" in txt and "✅ football-data" in txt


def test_structure_change_reports_errors():
    net = fresh()
    out = sources.refresh_all(["soccer_epl"])
    assert all(h["ok"] for h in out.values()), out
    good = store.load("src_understat.json", {})
    # le site change : plus de teamsData, plus d'API JSON ; CSV remplacé par du HTML ; tableaux FBref disparus
    CLOCK.t += 30 * HOUR
    net.routes[API_US] = Resp(200, b'{"foo": 1}')
    net.routes[EPL_US] = Resp(200, b"<html><script>window.__NEXT_DATA__ = {}</script></html>")
    net.routes["https://understat.com/getLeagueData/EPL/2024"] = Resp(404)
    net.routes["https://understat.com/league/EPL/2024"] = Resp(200, b"<html></html>")
    net.routes[EPL_FD] = Resp(200, b"<html><body>Nouveau site</body></html>")
    for k in ("shooting", "misc"):
        net.routes[EPL_FB.format(k)] = Resp(200, b"<html><body><p>Les donnees avancees ne sont plus disponibles</p></body></html>")
    out = sources.refresh_all(["soccer_epl"])
    assert out["understat"]["ok"] is False and "structure de la page modifiée" in out["understat"]["last_error"]
    assert out["fbref"]["ok"] is False and "structure de la page modifiée" in out["fbref"]["last_error"]
    assert out["footballdata"]["ok"] is False and "structure" in out["footballdata"]["last_error"]
    assert all(not h["blocked_until"] for h in out.values())
    assert store.load("src_understat.json", {}) == good, "les anciens profils sont conservés"
    assert "football-data" in sources.report() and "⚠️" in sources.report()
    assert features.match_features("Arsenal", "Manchester City", "soccer_epl")["sources"], "anciennes données toujours utilisables"
    # le fichier HTML invalide n'est pas gardé en cache : une réparation est vue au prochain passage
    net.routes[EPL_FD] = Resp(200, fixture("E0_2526.csv"))
    CLOCK.t += 13 * HOUR
    assert fd.refresh(["soccer_epl"])["ok"] is True
    # point d'accès JSON en échec : repli sur l'ancienne page JSON.parse
    fresh()
    NET.routes[API_US] = Resp(404)
    NET.routes[EPL_US] = Resp(200, fixture("understat_epl_2025.html"))
    h = understat.refresh(["soccer_epl"])
    assert h["ok"] and store.load("src_understat.json", {})["leagues"]["EPL"]["teams"]["Arsenal"]["all"]["xg"] == 1.88
    # requête principale : en-têtes du XHR du site + User-Agent de navigateur
    fresh()
    understat.refresh(["soccer_epl"])
    hdr = NET.hits("getLeagueData/EPL/2025")[0][1]
    assert hdr["X-Requested-With"] == "XMLHttpRequest" and hdr["Referer"] == "https://understat.com/league/EPL"
    assert hdr["User-Agent"] == http.UA and not NET.hits("understat.com/league")
    # jamais d'exception, même sur une réponse absurde
    fresh()
    NET.routes[API_US] = Resp(200, b"\xff\xfe\x00 {")
    NET.routes[EPL_US] = Resp(200, b"\xff\xfe\x00 var teamsData = JSON.parse('")
    assert understat.refresh(["soccer_epl"])["ok"] is False
    NET.routes[EPL_FD] = Resp(200, b"\x00\x01\x02")
    assert fd.refresh(["soccer_epl"])["ok"] is False


def test_fbref_page_budget():
    net = fresh()
    os.environ["FBREF_MAX_PAGES"] = "1"
    try:
        h = fbref.refresh(["soccer_epl"])
        assert len(net.hits("fbref.com/en/comps")) == 1                    # plafond de pages par exécution
        assert h["ok"] and "fouls90" not in store.load("src_fbref.json", {})["leagues"]["soccer_epl"]["players"]["Arsenal"]["Bukayo Saka"]
        os.environ["FBREF_MAX_PAGES"] = "6"
        h = fbref.refresh(["soccer_epl"])
        assert h["ok"] and len(net.hits("fbref.com/en/comps")) == 2       # la page déjà en cache ne recompte pas
        assert store.load("src_fbref.json", {})["leagues"]["soccer_epl"]["players"]["Arsenal"]["Bukayo Saka"]["fouls90"] == 1.0
        assert fbref.refresh(["soccer_epl"])["ok"] and len(net.hits("fbref.com/en/comps")) == 2
    finally:
        os.environ.pop("FBREF_MAX_PAGES", None)


# --- bilan, profils, fonctions d'avant-match ---------------------------------------

def test_health_report_and_features():
    net = fresh()
    assert sources.health_all()["fbref"] == {"ok": None, "last_success": None, "last_error": None, "rows": 0, "blocked_until": None}
    assert "pas encore lancée" in sources.report()
    out = sources.refresh_all(["soccer_epl", "tennis_atp"])
    for name, h in out.items():
        assert set(h) == {"ok", "last_success", "last_error", "rows", "blocked_until"} and h["ok"] is True and h["rows"] > 0, (name, h)
    assert out["footballdata"]["last_error"] and "HTTP 404" in out["footballdata"]["last_error"]       # 2425/E0 absent : avertissement
    txt = sources.report()
    assert txt.count("✅") == 5 and all(n in txt for n in ("football-data", "Understat", "FBref", "FotMob", "BetExplorer")), txt
    assert len(txt.splitlines()) == 6
    total = sum(store.size(f) for f in store.names("src_"))
    assert 0 < total < 400_000, total            # ~60 ligues FotMob avec les championnats 1xBet (C.XB_LEAGUES)
    # le délai de 6 s est tenu entre les pages FBref
    ts = [e[2] for e in net.log if "fbref.com" in e[0]]
    assert all(b - a >= 6.0 for a, b in zip(ts, ts[1:]))

    f = sources.match_features("Arsenal", "Man City", "soccer_epl")
    assert f["sources"] == ["football-data", "understat", "fbref"], f["sources"]
    assert f["home_team"] == "Arsenal" and f["away_team"] == "Man City"
    # corners : Arsenal à domicile (6.0 pour, 4.67 contre) face à City dehors (6.0 pour, 5.0 contre)
    assert close(f["exp_corners_home"], 5.5) and close(f["exp_corners_away"], 5.33) and close(f["exp_corners"], 10.83, 0.03)
    assert close(f["exp_cards_team"], 4.17, 0.03) and f["exp_cards"] == f["exp_cards_team"]
    assert "exp_shots_home" in f and "exp_sot_away" in f and "exp_goals_home" in f and "exp_fouls" in f
    assert close(f["xg_home"], 1.88) and close(f["xga_away"], 1.23) and close(f["exp_xg_home"], 1.55, 0.03)
    assert close(f["exp_xg_total"], f["exp_xg_home"] + f["exp_xg_away"], 0.02)
    fr = sources.match_features("Arsenal", "Man City", "soccer_epl", referee="Anthony Taylor")      # A Taylor : 3.67 cartons
    assert fr["referee"] == "A Taylor" and close(fr["ref_cards"], 3.67) and close(fr["exp_cards"], 0.7 * 4.17 + 0.3 * 3.67, 0.03)
    assert close(fr["ref_fouls"], 19.0) and fr["ref_cards_factor"] < 1 and fr["exp_cards"] < fr["exp_cards_team"]
    fu = sources.match_features("Arsenal", "Man City", "soccer_epl", referee="Zed Nobody")
    assert fu["referee_unknown"] == "Zed Nobody" and fu["exp_cards"] == fu["exp_cards_team"]
    fo = sources.match_features("Arsenal", "Man City", "soccer_epl", referee="S Attwell")           # 1 seul match : ignoré
    assert "ref_cards" not in fo
    # noms de The Odds API
    assert sources.match_features("Manchester City", "Arsenal FC", "soccer_epl")["home_team"] == "Man City"
    # inconnu / autre ligue : dict vide mais valide
    assert sources.match_features("Real Madrid", "Barcelona", "soccer_spain_la_liga") == {"sources": []}
    assert sources.match_features("Foo", "Bar", "basketball_nba") == {"sources": []}
    # repli FBref quand football-data est absent
    features._cache["fd"] = {}
    g = sources.match_features("Arsenal", "Man City", "soccer_epl")
    assert g["sources"] == ["understat", "fbref"] and close(g["exp_shots_home"], (14.0 + 8.5) / 2)
    assert close(g["exp_cards"], ((1.7 + 2.2) / 2) + ((1.2 + 2.0) / 2), 0.02), g
    features.reload()
    # joueurs
    p = sources.player_features("Bukayo Saka", "Arsenal")
    assert p["sources"] == ["understat", "fbref"] and close(p["shots90"], 2.65) and close(p["sot90"], 1.2)
    assert p["fouls90"] == 1.0 and p["cards90"] == 0.2 and close(p["xg90"], 0.45) and p["minutes"] == 1020
    assert sources.player_features("Martin Odegaard", "Arsenal")["name"] == "Martin Ødegaard"
    assert sources.player_features("Haaland", None, "soccer_epl")["team"] == "Manchester City"
    assert sources.player_features("Haaland", "Chelsea") is None and sources.player_features("Inconnu Total", "Arsenal") is None
    assert sources.player_features("Saka", "Arsenal", "soccer_spain_la_liga") is None


def test_football_data_for_settlement():
    """Règlement gratuit : CSV lus depuis le cache disque SANS requête (même périmés), match retrouvé à ±1 jour."""
    net = fresh()
    assert fd.cached_history(["E0"], ["2526"]) == [] and net.log == []
    fd.fetch_season("E0", "2526")
    n = len(net.log)
    CLOCK.t += 30 * 24 * HOUR                                        # cache périmé : toujours lisible
    ms = fd.cached_history(["E0"], ["2526"])
    assert len(ms) == 8 and len(net.log) == n
    m = fd.find_match(ms, "Manchester City", "Arsenal", "2025-09-20")
    assert m and (m["hg"], m["ag"]) == (2, 2)
    assert fd.find_match(ms, "Manchester City", "Arsenal", "2025-09-21") and fd.find_match(ms, "Manchester City", "Arsenal", "2025-09-19")
    assert fd.find_match(ms, "Manchester City", "Arsenal", "2025-09-23") is None
    assert fd.find_match(ms, "Arsenal", "Manchester City", "2025-09-20") is None          # domicile / extérieur inversés
    assert fd.find_match(ms, "Arsenal FC", "Chelsea", "2025-08-16")["referee"] == "M Oliver"
    assert fd.find_match(ms, "Real Madrid", "Barcelona", "2025-08-16") is None
    st = fd.match_stats(m)
    assert st["corners"] == 10 and st["shots"] == 24 and st["fouls"] == 17 and st["cards"] == 3 and st["players"] == {}
    # cartons = jaunes + rouges ; données absentes = clé absente (jamais réglé sur 0)
    assert fd.match_stats({"hy": 2, "ay": 1, "hr": 1, "ar": None, "hc": 4, "ac": 5}) == {"players": {}, "corners": 9, "cards": 4}
    assert fd.match_stats({"hy": None, "ay": 1, "hc": None, "ac": 5}) == {"players": {}}


def test_team_profile():
    """/stats : équipe retrouvée par nom approché (ou mot unique), ambiguïté signalée, inconnue = None."""
    fresh()
    sources.refresh_all(["soccer_epl"])
    p = sources.team_profile("arsenal")
    assert p["team"] == "Arsenal" and p["div"] == "E0" and p["fd"]["all"]["n"] >= 3 and p["us"]["all"]["xg"] > 0, p
    assert sources.team_profile("man city")["team"] == "Man City" and sources.team_profile("Manchester City")["team"] == "Man City"
    assert sources.team_profile("city")["team"] == "Man City"                           # mot unique
    assert sources.team_profile("Zzz Inconnu") is None
    features._cache["fd"] = {"teams": {"Man City": {"div": "E0"}, "Man United": {"div": "E0"}}}
    assert sources.team_profile("manchester") == {"many": ["Man City", "Man United"]}
    assert sources.team_profile("man utd")["team"] == "Man United"
    features.reload()


def history_net(seasons=("2223", "2324", "2425", "2526")):
    """Faux football-data avec un historique synthétique (E0) pour les saisons demandées."""
    net = fresh()
    for sn in seasons:
        until = date(2025, 11, 29) if sn == "2526" else None
        net.routes[f"https://football-data.co.uk/mmz4281/{sn}/E0.csv"] = Resp(200, synth.csv_for(sn, until).encode())
    return net


def test_bootstrap_rows():
    """Lignes d'amorçage : cote d'ouverture vs clôture du même book, consensus Avg sans marge, variables d'avant-match
    sans fuite, petit fichier, plafond, reconstruction hebdomadaire, jamais d'exception."""
    net = history_net()
    ms = []
    for sn in ("2324", "2425", "2526"):
        ms += fd.parse_csv(synth.csv_for(sn, date(2025, 11, 29) if sn == "2526" else None), "E0", sn)[0]
    obj = bootstrap.build(ms)
    rows = obj["rows"]
    assert 200 < len(rows) <= C.BOOT_MAX_ROWS and obj["cols"][:3] == ["date", "market", "clv"] and obj["seasons"] == ["2324", "2425", "2526"]
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert max(sum(1 for r in rows if r[0] == d) for d in {r[0] for r in rows}) <= C.BOOT_PER_DAY
    assert len(json.dumps(obj)) < 60_000, len(json.dumps(obj))
    # première journée : aucune forme connue => aucune variable d'avant-match (pas de fuite du futur)
    first = [r for r in rows if r[0] == rows[0][0]]
    assert all(r[7] == r[8] == r[9] == 0 for r in first)
    # une ligne recalculée à la main : cote Max d'ouverture / clôture du même book, consensus Avg sans marge
    mx = {"date": "2025-10-01", "season": "2526", "home": "A", "away": "B", "hg": 1, "ag": 0, "odds": {"1x2": {
        "open": {"max": [2.2, 3.5, 3.8], "avg": [2.0, 3.4, 3.6], "ps": [2.04, 3.45, 3.55]},
        "close": {"max": [2.0, 3.5, 4.0], "avg": [1.95, 3.4, 3.7]}}}}
    r0 = bootstrap.match_rows(mx, None)[0]
    pv, ps_ = fd.no_vig(mx["odds"]["1x2"]["open"]["avg"]), fd.no_vig(mx["odds"]["1x2"]["open"]["ps"])
    edge0 = pv[0] * 2.2 - 1
    assert r0[:3] == ["2025-10-01", 0, round(math.log(2.2 / 2.0), 4)] and r0[3] == round(edge0, 4)
    assert r0[4] == round(abs(pv[0] - ps_[0]) / pv[0], 4) and r0[5] == round(math.log(2.2), 4)
    assert r0[6] == round((edge0 + 0.5 * abs(pv[0] - ps_[0]) / pv[0]) * C.MARKET_WEIGHT["h2h"] + (C.MARKET_WEIGHT["h2h"] - 1) * 0.05, 4)
    xs = bootstrap.match_rows(mx, (1.8, 0.9))                       # écart de buts attendus du côté du pari
    assert [r[7] for r in xs] == [0.9, -0.9, -0.9]
    assert any(r[7] != 0 for r in rows) and any(r[9] != 0 for r in rows)             # 1N2 : écart de buts ; buts : proba Poisson
    # le signal fictif existe bien sur l'ensemble des candidats : plus la valeur est grande, plus le CLV l'est
    allr = [r for m in ms for r in bootstrap.match_rows(m, None)]
    xs, ys = [r[3] for r in allr], [r[2] for r in allr]
    mx_, my_ = sum(xs) / len(xs), sum(ys) / len(ys)
    corr = sum((x - mx_) * (y - my_) for x, y in zip(xs, ys)) / math.sqrt(
        sum((x - mx_) ** 2 for x in xs) * sum((y - my_) ** 2 for y in ys))
    assert len(allr) > 3 * len(rows) and corr > 0.5, (len(allr), corr)
    # plafond de lignes : éclaircissement régulier, ordre conservé
    small = bootstrap.build(ms, cap=100)["rows"]
    assert len(small) == 100 and [r[0] for r in small] == sorted(r[0] for r in small)
    # cote aberrante ou sans consensus distinct de la cote prise : ignorée
    odd = dict(ms[200], odds={"1x2": {"open": {"max": [2.0, 3.0, 4.0]}, "close": {"max": [1.9, 3.0, 4.2]}}})
    assert bootstrap.match_rows(odd, None) == []
    # lecture par learn : lignes marquées, poids réduit, variables nommées
    boot = learn.boot_rows(obj)
    assert len(boot) == len(rows) and all(b["boot"] and b["w"] == C.BOOT_WEIGHT and b["features"]["bootstrap"] == 1.0 for b in boot)
    assert any("gap_goals" in b["features"] and b["features"]["has_pm"] == 1.0 for b in boot) and boot[0]["market"] in ("h2h", "totals")
    assert learn.boot_rows({"rows": [["2025-01-01", 5, 0.1], ["x"], None]}) == [] and learn.boot_rows(None) == []

    # ensure() : télécharge les saisons passées une fois, fichier petit dans data/, relecture sans requête
    assert not store.exists("bootstrap.json")
    out = bootstrap.ensure(["soccer_epl", "soccer_uefa_champs_league"])
    assert out["rows"] and out["seasons"] == ["2223", "2324", "2425", "2526"] and out["current"] == "2526", out["seasons"]
    assert store.size("bootstrap.json") < 100_000
    assert {u.split("/")[-2] for u, _, _ in net.log if u.endswith("E0.csv")} == {"2223", "2324", "2425", "2526"}
    n = len(net.log)
    assert bootstrap.ensure(["soccer_epl"])["built"] == out["built"] and len(net.log) == n          # < 7 jours : relu
    CLOCK.t += 8 * 24 * HOUR
    again = bootstrap.ensure(["soccer_epl"])
    assert again["built"] != out["built"]                                                          # reconstruit
    old = store.load("bootstrap.json", {})
    # échec réseau : l'ancien fichier reste ; division inconnue : rien ; jamais d'exception
    CLOCK.t += 8 * 24 * HOUR
    shutil.rmtree(os.path.join(TMP, "cache"), ignore_errors=True)
    http.reset()
    for sn in ("2223", "2324", "2425", "2526", "2627"):
        net.routes[f"https://football-data.co.uk/mmz4281/{sn}/E0.csv"] = Resp(500)
    assert bootstrap.ensure(["soccer_epl"], force=True)["built"] == old["built"]
    assert bootstrap.ensure(["tennis_atp"], force=True)["built"] == old["built"]
    bootstrap.store = None                                                                         # bug interne : avalé
    try:
        assert bootstrap.ensure(["soccer_epl"]) == {}
    finally:
        bootstrap.store = store


def test_probe_sources_report():
    """tools/probe.py --sources : refresh_all + amorçage pour de bon (ici sur un faux réseau), rapport JSON court,
    aucun secret, dossier de données temporaire (data/ du bot intact)."""
    old_env = {k: os.environ.get(k) for k in ("ODDS_API_KEY", "DATA_DIR", "SOURCES_CACHE_DIR")}
    os.environ["ODDS_API_KEY"] = "sekret-odds-key"                                           # avant l'import : probe.SECRETS
    from tools import probe
    net = history_net()
    net.routes[API_US] = Resp(200, fixture("understat_api_epl_2025.json"))
    net.routes[EPL_FB.format("shooting")] = Resp(403, b"", {"Cf-Mitigated": "challenge"})     # FBref : Cloudflare
    outdir = os.path.join(TMP, "probe-out")
    real_out, real_data = probe.OUT, store.DATA_DIR
    probe.OUT = outdir
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert probe.main_sources() == 0
        txt = open(os.path.join(outdir, "sources_report.json"), encoding="utf-8").read()
        rep = json.loads(txt)
        assert store.DATA_DIR != real_data and not os.path.exists(real_data)                  # dossier temporaire
        assert rep["sources"]["footballdata"]["ok"] and rep["sources"]["footballdata"]["rows"] > 100
        assert rep["sources"]["understat"]["ok"] and rep["sources"]["fbref"]["ok"] is False and rep["sources"]["fbref"]["blocked_until"]
        assert "Cloudflare" in rep["sources"]["fbref"]["error"]
        assert rep["profiles"]["src_footballdata.json"]["teams"] == 20 and rep["profiles"]["src_footballdata.json"]["bytes"] > 1000
        assert rep["profiles"]["src_understat.json"]["players"] > 0 and rep["bootstrap"]["rows"] > 100
        assert rep["bootstrap"]["seasons"] == ["2223", "2324", "2425", "2526"]
        assert rep["cache"]["files"] > 5 and rep["leagues"] and rep["seconds"] >= 0
        assert rep["sample_match"]["sources"] and "exp_corners" in rep["sample_match"] and "fd" in rep["sample_team"]
        assert "sekret-odds-key" not in txt and len(txt) < 4000, len(txt)
    finally:
        probe.OUT, store.DATA_DIR = real_out, real_data
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_new_leagues_slim_profiles_and_deadline():
    """Autres pays (fichier « new leagues », résultats seulement) : profil de buts ; divisions secondaires : profil léger ;
    délai maximal : plus aucun téléchargement, sans bloquer la source."""
    net = fresh()
    assert fd.url("BRA", "2627") == "https://football-data.co.uk/new/BRA.csv" and "BRA" in fd.NEW_DIVS
    assert fd.SPORT_TO_DIV["soccer_usa_mls"] == "USA" and fd.DIV_TO_SPORT["BRA"] == "soccer_brazil_campeonato"
    teams = ["Flamengo RJ", "Palmeiras", "Santos", "Corinthians"]
    rows = ["Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PH,PD,PA"]
    for i in range(12):
        d = (date(2025, 9, 1) + (date(2025, 9, 7) - date(2025, 9, 1)) * i).strftime("%d/%m/%Y")
        rows.append(f"Brazil,Serie A,2025,{d},21:00,{teams[i % 4]},{teams[(i + 1) % 4]},{i % 4},{i % 3},H,2.0,3.3,3.6")
    rows += ["Brazil,Serie A,2019,01/05/2019,21:00,Old FC,Santos,1,0,H,2.0,3.3,3.6",
             "Brazil,Serie A,2025,20/11/2025,21:00,Santos,Palmeiras,,,,2.0,3.3,3.6"]               # non joué
    net.routes["https://football-data.co.uk/new/BRA.csv"] = Resp(200, "\n".join(rows).encode())
    e1 = fixture("E0_2526.csv").decode("utf-8-sig").replace("\nE0,", "\nE1,")
    net.routes["https://football-data.co.uk/mmz4281/2526/E1.csv"] = Resp(200, e1.encode())
    net.routes["https://football-data.co.uk/mmz4281/2425/E1.csv"] = Resp(404)
    h = fd.refresh(["soccer_brazil_campeonato", "soccer_efl_champ"])
    assert h["ok"], h
    assert len(net.hits("/new/BRA.csv")) == 1 and not net.hits("/new/BRA.csv")[0][0].endswith("2526")     # un seul fichier par pays
    prof = fd.profiles()
    fla = prof["teams"]["Flamengo RJ"]
    assert fla["div"] == "BRA" and fla["all"]["n"] == 6 and "goals_for" in fla["all"] and "shots_for" not in fla["all"], fla
    assert "Old FC" not in prof["teams"]                                       # plus d'un an : écarté
    # profil léger des divisions hors des 5 grandes : buts, corners, cartons ; les grandes gardent tout
    champ = next(t for t, v in prof["teams"].items() if v["div"] == "E1")
    assert "corners_for" in prof["teams"][champ]["all"] and "shots_for" not in prof["teams"][champ]["all"]
    full = fd.team_profiles(fd.parse_csv(fixture("E0_2526.csv").decode("utf-8-sig"), "E0", "2526")[0])
    assert "shots_for" in full["teams"]["Arsenal"]["all"]                      # les 5 grandes ligues gardent tous les chiffres
    # chiffres d'avant-match : buts attendus (Poisson) pour le Brésil, jamais de corners (le fichier n'en a pas)
    features.reload()
    f = sources.match_features("Flamengo", "Palmeiras", "soccer_brazil_campeonato")
    assert f["sources"] == ["football-data"] and f["n_home"] >= 5 and "exp_goals_home" in f and "exp_corners" not in f, f
    assert sources.match_features("Flamengo", "Palmeiras", "soccer_epl")["sources"] == []              # autre division : écarté
    # règlement gratuit : fichier lu depuis le cache disque, sans requête
    n = len(net.log)
    ms = fd.cached_history(["BRA"])
    assert len(ms) == 12 and len(net.log) == n and fd.find_match(ms, "Flamengo", "Palmeiras", "2025-09-01")
    # délai maximal dépassé : cache lu, aucun nouveau téléchargement, la source n'est pas bloquée
    CLOCK.t += 30 * 24 * HOUR
    http.set_deadline(60)
    http._deadline = time.monotonic() - 1
    try:
        n = len(net.log)
        res = http.fetch("https://football-data.co.uk/new/BRA.csv", "footballdata", http.TTL_CSV_CURRENT)
        assert res.ok and res.stale and "délai dépassé" in res.error and len(net.log) == n
        res = http.fetch("https://football-data.co.uk/mmz4281/2526/D2.csv", "footballdata", http.TTL_CSV_CURRENT)
        assert not res.ok and "délai dépassé" in res.error and len(net.log) == n and not state.is_blocked("footballdata")
    finally:
        http.set_deadline(None)
    assert http._deadline is None


def test_refresh_never_raises():
    fresh(serve=False)                                                  # tout en 404
    out = sources.refresh_all(["soccer_epl"])
    assert all(h["ok"] is False and h["last_error"] for h in out.values()), out
    sources.report()
    fresh()
    requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boum"))   # exception imprévue dans la couche réseau
    out = sources.refresh_all(["soccer_epl"])
    assert all(h["ok"] is False for h in out.values()) and "erreur interne" in out["footballdata"]["last_error"]
    # profils vides ou corrompus : match_features reste valide
    fresh()
    features.reload()
    corrupt("src_footballdata.json")
    assert sources.match_features("Arsenal", "Chelsea", "soccer_epl") == {"sources": []}
    assert sources.soccer_leagues(["soccer_epl", "tennis_atp", "soccer_nope"]) == ["soccer_epl"]
    assert "soccer_spain_la_liga" in sources.soccer_leagues(["soccer_"])


# --- FotMob et BetExplorer ----------------------------------------------------

FM = "www.fotmob.com"
BX = "www.betexplorer.com"


def _fotmob_html():
    data = json.loads(fixture("fotmob_league47.json"))
    inner = json.dumps({"props": {"pageProps": data}}, ensure_ascii=False).replace("</", "<\\/")
    return (f'<html><body><script id="__NEXT_DATA__" type="application/json">{inner}</script>'
            "</body></html>").encode()


def _fm_match_html(m=None, throws=None):
    """Page de match FotMob (fixture réelle Fulham - Man Utd, réduite) ; `m` : match de la page de ligue dont elle prend l'id, les
    équipes et la date ; `throws` : touches à mettre à la place des vraies."""
    data = json.loads(fixture("fotmob_match.json"))
    if m:
        st = m["status"]
        hg, ag = (int(x) for x in st["scoreStr"].replace(" ", "").split("-"))
        data["general"].update(matchId=m["id"], matchTimeUTCDate=st["utcTime"])
        data["header"]["teams"] = [{"name": m["home"]["name"], "id": int(m["home"]["id"]), "score": hg},
                                   {"name": m["away"]["name"], "id": int(m["away"]["id"]), "score": ag}]
    if throws is not None:
        for grp in data["content"]["stats"]["Periods"]["All"]["stats"]:
            for x in grp["stats"]:
                if x["key"] == "player_throws":
                    x["stats"] = throws
    inner = json.dumps({"props": {"pageProps": data}}, ensure_ascii=False).replace("</", "<\\/")
    return f'<html><script id="__NEXT_DATA__" type="application/json">{inner}</script></html>'.encode()


def test_fmmatch_stats():
    """Pages de match FotMob : touches, hors-jeu, fautes... lues par passage horaire, profils, chiffres d'avant-match, pronostics sur
    touches / hors-jeu / fautes, règlement."""
    from kelly import fair, fmt, settle
    m = fmmatch.parse_match(_fm_match_html().decode())
    assert m["home"] == "Fulham" and m["s"]["throws"] == [11, 20] and m["s"]["offsides"] == [1, 2] and m["s"]["fouls"] == [9, 5]
    assert m["s"]["sot"] == [6, 6] and m["s"]["shots"] == [12, 29] and m["s"]["corners"] == [3, 13] and m["s"]["cards"] == [3, 0]
    assert m["s"]["goals"] == [1, 1] and m["d"] == "2026-09-20"
    assert "throws" not in fmmatch.parse_match(_fm_match_html(throws=[0, 0]).decode())["s"]       # 0-0 = non suivi
    assert fmmatch.parse_match("<html>rien</html>") is None
    # cartons : le fil du match fait foi (FotMob laisse parfois la stat à 0-0, au Brésil)
    data = json.loads(fixture("fotmob_match.json"))
    data["content"]["matchFacts"] = {"events": {"events": [{"type": "Card", "card": "Yellow", "isHome": False},
                                                           {"type": "Card", "card": "Red", "isHome": True}, {"type": "Goal", "isHome": True}]}}
    page = '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": data}}) + "</script>"
    assert fmmatch.parse_match(page)["s"]["cards"] == [1, 1]
    net = fresh()
    league = json.loads(fixture("fotmob_league47.json"))["fixtures"]["allMatches"]
    done = [x for x in league if x["status"].get("finished")]
    for x in done:
        net.routes[f"https://{FM}{x['pageUrl'].split('#')[0]}"] = Resp(200, _fm_match_html(x))
    assert fmmatch.fill()["pages"] == 0                               # sans page de ligue en cache : rien à lire
    fotmob.refresh(["soccer_epl"])                                    # le matin : page de la ligue (en cache)
    rep = fmmatch.fill(max_pages=5, seconds=600)
    assert rep["pages"] == 5 and rep["added"] == 5 and rep["left"] > 0, rep
    rep = fmmatch.fill(max_pages=200, seconds=6000)
    assert rep["added"] == len(done) - 5 and rep["left"] == 0, rep
    assert fmmatch.fill(max_pages=200, seconds=6000)["pages"] == 0              # jamais une page deux fois
    assert not os.listdir(os.path.join(TMP, "cache")) or all(
        os.path.getsize(os.path.join(TMP, "cache", f)) < 2_000_000 for f in os.listdir(os.path.join(TMP, "cache")))
    prof = fmmatch.profiles()["teams"]
    ars = prof["Arsenal"]
    assert ars["all"]["n"] == 5 and ars["all"]["throws_for"] == 16.4 and "offsides_for" in ars["all"], ars["all"]
    features.reload()
    f = sources.match_features("Arsenal", "Manchester City", "soccer_epl")
    assert "FotMob (matchs)" in f["sources"] and f["exp_throws"] > 20 and f["exp_offsides"] > 1, f
    assert {"exp_throws", "exp_offsides"} <= set(f["aps_keys"]) and f["aps_q"] > 0
    markets = {o[0] for o in fair.options(f, "Arsenal", "Manchester City")}
    assert {"team_throws_total", "team_offsides_total", "team_fouls_total", "team_sot_total"} <= markets, markets
    assert fmt.label("team_throws_total", "Over", 30.5, None, "soccer") == "Plus de 30,5 touches au total"
    assert fmt.label("team_fouls_home", "Under", 12.5, "Arsenal", "soccer") == "Arsenal : moins de 12,5 fautes"
    # règlement : le match est déjà accumulé (aucune page de plus)
    x = done[-1]
    b = {"home": x["home"]["name"], "away": x["away"]["name"], "commence": x["status"]["utcTime"], "sport_key": "soccer_epl",
         "market": "team_throws_total", "name": "Over", "point": 30.5, "group": "soccer", "who": None, "id": "z"}
    st = fmmatch.match_stats(b)
    assert settle.shot_value("team_throws_total", st) == 31 and settle.shot_value("team_offsides_total", st) == 3
    assert settle.shot_value("team_fouls_home", st) == 9 and settle.final_text(b, {}, st) == "31 touches"
    ev = {"scores": [{"name": b["home"], "score": "1"}, {"name": b["away"], "score": "1"}]}
    assert settle.settle_pick(b, ev, st) == "win" and settle.settle_pick(b | {"point": 31.5}, ev, st) == "loss"
    assert fmmatch.match_stats(b | {"home": "Inconnu FC"}) is None


def test_fotmob_parse_league():
    data, probs = fotmob.parse_league(_fotmob_html().decode())
    assert data and not probs, probs
    t = data["teams"]
    assert t["Arsenal"]["n"] == 5 and t["Arsenal"]["w"] == 4 and close(t["Arsenal"]["xg"], 8.48, 0.02)
    assert t["Manchester City"]["gf"] == 13 and t["Manchester City"]["ga"] == 5 and t["Manchester City"]["xga"]
    ms = data["matches"]
    assert len(ms) >= 40 and any(m["home"] == "Arsenal" and m["hg"] == 3 and m["ag"] == 0 for m in ms)
    assert all(m["d"] and m["hg"] is not None and m["ag"] is not None for m in ms)
    assert fotmob.parse_league("<html>pas de donnees</html>")[0] is None
    assert "__NEXT_DATA__ introuvable" in fotmob.parse_league("<html>x</html>")[1][0]
    # table vide (saison à zéro, ancien et nouveau format) = erreur claire, pas d'exception
    vide = json.dumps({"props": {"pageProps": {"table": [{"data": {"table": {"all": []}}}]}}})
    html = (f'<script id="__NEXT_DATA__">{vide}</script>')
    assert fotmob.parse_league(html)[0] is None and "aucun classement" in fotmob.parse_league(html)[1][0]
    vide2 = json.dumps({"props": {"pageProps": {"table": [{"data": {"table": {"all": [], "xg": []}}}]}}})
    html2 = (f'<script id="__NEXT_DATA__">{vide2}</script>')
    assert fotmob.parse_league(html2)[0] is None and "aucun classement" in fotmob.parse_league(html2)[1][0]


def test_fotmob_refresh_and_features():
    fresh(serve=False)
    NET.routes[f"https://{FM}/robots.txt"] = ROBOTS_OK
    NET.routes["https://www.fotmob.com/leagues/47"] = Resp(200, _fotmob_html())
    for kid in fotmob.EXTRA_LEAGUES.values():                       # ligues africaines toujours actualisées
        NET.routes[f"https://www.fotmob.com/leagues/{kid}"] = Resp(200, _fotmob_html())
    h = fotmob.refresh(["soccer_epl"])
    assert h["ok"] and h["rows"] > 0, h
    prof = fotmob.profiles()
    assert "soccer_epl" in prof["leagues"] and prof["leagues"]["soccer_epl"]["teams"]["Arsenal"]["xg"]
    # le 2e passage relit le cache : mêmes données, aucun appel
    n = len(NET.log)
    assert fotmob.refresh(["soccer_epl"])["ok"]
    assert len(NET.log) == n
    # features : l'xG FotMob complète quand Understat est absent
    features.reload()
    f = features.match_features("Arsenal", "Coventry City", "soccer_epl")
    assert "FotMob" in f["sources"] and "exp_xg_home" in f and "exp_xg_total" in f
    # robots.txt interdit -> aucune requête de données, erreurs explicites, health négatif
    fresh(serve=False)
    NET.routes[f"https://{FM}/robots.txt"] = Resp(200, b"User-agent: *\nDisallow: /\n")
    h = fotmob.refresh(["soccer_epl"])
    assert h["ok"] is False and "robots.txt interdit" in (h["last_error"] or "")
    assert not [e for e in NET.log if "/leagues/" in e[0]]


def test_betexplorer_parse_and_refresh():
    html = fixture("betexplorer_ci_results.html").decode()
    ms = betexplorer.parse_results(html, today=date(2026, 10, 8))
    assert ms, "aucun match parsé"
    m = next(x for x in ms if x["home"] == "ISCA")
    assert m["away"] == "Bouake" and (m["hg"], m["ag"]) == (1, 0)
    assert (m["o1"], m["ox"], m["o2"]) == (2.37, 2.82, 3.03) and m["mid"]
    reporte = [x for x in ms if x["home"] == "Agboville"]
    assert reporte == []                                            # POSTP. sans score : sauté
    dated = [x for x in ms if x["d"]]
    assert dated and dated[0]["d"] in ("2026-10-07", "2026-10-06", "2026-10-05"), dated[:2]
    # date absolue « 06.10. » -> 2026-10-06
    row = ('<tr><td><a href="/football/ivory-coast/ligue-1/zoman-korhogo/AAAA1111/">'
           "<span><strong>Zoman</strong></span> - <span>Korhogo</span></a></td>"
           '<td><a href="/x/">1:1</a></td><td data-odd="2.10"></td><td data-odd="3.20"></td>'
           '<td data-odd="3.30"></td><td>06.10.</td></tr>')
    ms2 = betexplorer.parse_results(row, today=date(2026, 10, 8))
    assert ms2 and ms2[0]["d"] == "2026-10-06" and ms2[0]["hg"] == 1
    # refresh réel avec faux réseau
    fresh(serve=False)
    NET.routes[f"https://{BX}/robots.txt"] = ROBOTS_OK
    NET.routes[f"https://{BX}/football/ivory-coast/ligue-1/results/"] = Resp(200, html.encode())
    h = betexplorer.refresh(["soccer_epl"])                         # epl absent -> erreur, extras actualisés
    prof = betexplorer.profiles()
    assert "africa_ivory_coast_ligue_1" in prof["leagues"], list(prof["leagues"])
    assert prof["leagues"]["africa_ivory_coast_ligue_1"]["matches"][0]["o1"]
    assert any("soccer_epl" in e for e in [h["last_error"] or ""]) or h["ok"] is True
    # sans cotes (page vide) : erreur de structure, cache invalidé
    for path in betexplorer.EXTRA_PATHS.values():
        NET.routes[f"https://{BX}/football/{path}/results/"] = Resp(200, b"<html><table></table></html>")
    h = betexplorer.refresh(["soccer_africa_egypt_premier_league"])
    assert "structure" in (h["last_error"] or "")
    # désactivée par env : aucun appel réseau, health inchangée
    n = len(NET.log)
    os.environ["BETEXPLORER_ENABLED"] = "0"
    try:
        assert betexplorer.refresh() == betexplorer.health() and len(NET.log) == n
    finally:
        os.environ.pop("BETEXPLORER_ENABLED")
    assert "FotMob" in sources.report() and "BetExplorer" in sources.report()


def test_new_sources_wired_in():
    fresh(serve=False)
    for d in (FM, BX, FD, US, FB):
        NET.routes[f"https://{d}/robots.txt"] = ROBOTS_OK
    names = [m.NAME for m in sources.SOURCES]
    assert "fotmob" in names and "betexplorer" in names
    assert sources.soccer_leagues(["soccer_epl"]) == ["soccer_epl"]
    assert fotmob.leagues_for(["soccer_fifa_world_cup_qualifiers_"]) == sorted(
        k for k in fotmob.SPORT_TO_LEAGUE if k.startswith("soccer_fifa_world_cup_qualifiers_")) + \
        [k for k in sorted(fotmob.EXTRA_LEAGUES)]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print(f"ok  {t.__name__}")
    finally:
        requests.get = _orig_get
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests)} tests sources : OK")


if __name__ == "__main__":
    main()
