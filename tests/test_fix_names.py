"""Tests hors ligne des correctifs de noms d'équipes et de statistiques (aucun réseau). Lancer : python -m tests.test_fix_names

1. ligues sans division football-data : plus de profils pris dans les autres ligues ; mots génériques (« Atlético », « Inter »...) ;
2. mauvaise équipe dans la bonne ligue (best_team : égalités, mots courts) ;
3. id FotMob du Championship anglais ;
4. cartons et corners null d'API-Football = 0 ;
5. remplaçant jamais entré = pas de stats joueur (pari annulé, pas gagné à 0 tir).
"""
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="kelly-fixnames-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["STATS_CACHE_DIR"] = os.path.join(TMP, "apistats")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ.pop("STATS_API_KEY", None)
os.environ.pop("STATS_DAILY_CAP", None)

from kelly import apistats, stats, store  # noqa: E402
from kelly.sources import features, fotmob, names  # noqa: E402

PIN = "2026-10-09"


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._d


def team(n=10, **kw):
    """Profil football-data minimal : `n` matchs, 1,5 but marqué et encaissé, 5 corners, 2 cartons par match."""
    v = {"n": n, "goals_for": 1.5, "goals_against": 1.5, "corners_for": 5.0, "corners_against": 5.0,
         "cards_for": 2.0, "cards_against": 2.0}
    return {"all": v, "home": dict(v), "away": dict(v)}


def only_fd(teams):
    """Vide les caches de sources, ne garde que football-data (`teams` : {nom: (division, profil)}) ; _deep écrirait des fichiers."""
    features._cache.clear()
    features._cache["fd"] = {"teams": {t: dict(p, div=d) for t, (d, p) in teams.items()}, "referees": {}, "leagues": {}}
    for k in ("us", "fm", "fb", "fmm", "aps"):
        features._cache[k] = {}
    features._deep = lambda *a, **k: None


def best(name, cands):
    return names.best_team(name, cands)


# --- 1. ligues sans division football-data --------------------------------------------

def test_football_data_needs_a_division():
    """« Atlético-GO » (Brésil Série B) n'est pas « Ath Madrid », « Juventude » n'est pas « Juventus », « Barcelona SC » (Équateur) n'est
    pas « Barcelona » : une ligue sans division football-data n'utilise aucun profil football-data."""
    only_fd({"Ath Madrid": ("SP1", team()), "Juventus": ("I1", team()), "Barcelona": ("SP1", team()), "Sp Lisbon": ("P1", team()),
             "Everton": ("E0", team()), "Arsenal": ("E0", team()), "Chelsea": ("E0", team())})
    for h, a, sk in (("Atlético-GO", "Juventude", "soccer_brazil_serie_b"),
                     ("Barcelona SC", "Sporting Cristal", "soccer_conmebol_copa_libertadores"),
                     ("Everton CD", "Atlético Madrid", "soccer_chile_campeonato")):
        f = features.match_features(h, a, sk)
        assert "football-data" not in f["sources"] and "home_team" not in f and "exp_goals" not in f, (h, a, f)
    f = features.match_features("Arsenal", "Chelsea", "soccer_epl")               # une division connue marche toujours
    assert f["sources"] == ["football-data"] and (f["home_team"], f["away_team"]) == ("Arsenal", "Chelsea"), f
    f = features.match_features("Arsenal", "Atlético Madrid", "soccer_uefa_champs_league")   # coupes d'Europe : clubs de toutes les divisions
    assert f["sources"] == ["football-data"] and f["away_team"] == "Ath Madrid", f


def test_generic_words_never_carry_a_match():
    s = names.team_sim
    assert s("Atlético-GO", "Ath Madrid") < 0.8 and s("Atlético Goianiense", "Ath Madrid") < 0.8
    assert s("Atlético Mineiro", "Ath Madrid") < 0.8 and s("Atletico", "Atletico Madrid") < 0.8
    assert s("Juventude", "Juventus") < 0.8 and s("Sporting Cristal", "Sp Lisbon") < 0.8 and s("Sporting Cristal", "Sporting") < 0.8
    assert s("Independiente", "Independiente Rivadavia") < 0.8 and s("Independiente del Valle", "Independiente") < 0.8
    assert s("Inter Miami", "Inter") < 0.8 and s("Union Berlin", "Union") < 0.8 and s("Real Madrid", "Real Betis") < 0.8
    assert s("Manchester City", "Manchester United") < 0.8 and s("Nacional", "Atlético Nacional") < 0.8
    assert s("America", "Club America") < 0.8 or s("America", "America MG") < 0.8
    # ce qui doit continuer à marcher
    assert s("Atlético Madrid", "Ath Madrid") >= 0.8 and s("Atletico de Madrid", "Atlético Madrid") >= 0.8
    assert s("Athletic Club", "Ath Bilbao") >= 0.8 and s("Inter Milan", "Inter") >= 0.8
    assert s("Tottenham Hotspur", "Tottenham") >= 0.85 and s("Wolves", "Wolverhampton Wanderers") >= 0.8
    assert s("Atlético-GO", "Atletico GO") >= 0.8 and s("Atlético-GO", "Atlético-MG") < 0.8
    assert s("Newcastle United", "Newcastle Utd") >= 0.8 and s("Newell's Old Boys", "Newells Old Boys") >= 0.8
    assert s("Independiente Rivadavia", "Ind. Rivadavia") >= 0.8 and s("Deportivo Riestra", "Dep. Riestra") >= 0.8
    assert s("Gimnasia LP", "Gimnasia L.P.") >= 0.8 and s("Hamburger SV", "Hamburg") >= 0.8


# --- 2. mauvaise équipe dans la bonne ligue ---------------------------------------------

def test_best_team_ambiguity_and_short_names():
    assert best("Tokyo Verdy", ["FC Tokyo", "Verdy", "Gamba Osaka"]) is None            # FC Tokyo ou Verdy : on ne devine pas
    assert best("Tokyo Verdy", ["FC Tokyo", "Tokyo Verdy", "Verdy"]) == "Tokyo Verdy"    # le nom exact lève l'ambiguïté
    assert best("Independiente Rivadavia", ["Independiente", "Ind. Rivadavia", "Boca Juniors"]) == "Ind. Rivadavia"
    assert best("Independiente Rivadavia", ["Independiente", "Boca Juniors"]) is None
    assert best("Gremio", ["Remo", "Bahia"]) is None and best("Grêmio", ["Remo", "Grêmio"]) == "Grêmio"   # un mot court n'est pas dans un autre
    assert best("UTS Rabat", ["FAR Rabat", "FUS Rabat"]) is None
    assert best("AL Nasr SC", ["Al Masry SC", "Al Ahly SC"]) is None and best("Al Nasr", ["Al Masry SC", "Al Nasr SC"]) == "Al Nasr SC"
    assert best("Estudiantes", ["Estudiantes L.P.", "Estudiantes Rio Cuarto"]) is None
    assert best("Manchester United", ["Man United", "Man City"]) == "Man United"
    assert best("Sheffield United", ["Sheffield Weds", "Sheffield Utd"]) == "Sheffield Utd"
    # plusieurs noms d'une même équipe (même profil) ne sont pas une ambiguïté
    p = {"nm": "Sao Paulo"}
    assert best("Sao Paulo", {"Sao Paulo": p, "São Paulo FC": p, "Santos": {"nm": "Santos"}}) in ("Sao Paulo", "São Paulo FC")
    assert best("x", []) is None


# --- 3. FotMob : Championship anglais -----------------------------------------------------

def test_fotmob_championship_id():
    """123 est le Championship ÉCOSSAIS (Stenhousemuir, Raith Rovers...) ; 48 est l'anglais (vérifié sur /leagues/48 : ENG, Championship)."""
    assert fotmob.SPORT_TO_LEAGUE["soccer_efl_champ"] == 48 and 123 not in fotmob.SPORT_TO_LEAGUE.values()
    store.save("src_fotmob.json", {"leagues": {
        "soccer_efl_champ": {"id": 123, "teams": {"Raith Rovers": {"n": 8}}, "matches": []},      # profil écrit avec l'ancien id : écarté
        "soccer_epl": {"id": 47, "teams": {"Arsenal": {"n": 8}}, "matches": []},
        "soccer_england_league1": {"teams": {"Barnsley": {"n": 8}}, "matches": []}}})              # ancien format sans id : servi
    lg = fotmob.profiles()["leagues"]
    assert "soccer_efl_champ" not in lg and set(lg) == {"soccer_epl", "soccer_england_league1"}


# --- 4. cartons et corners null ----------------------------------------------------------

def stat_block(tid, **vals):
    return {"team": {"id": tid}, "statistics": [{"type": k.replace("_", " "), "value": v} for k, v in vals.items()]}


def test_null_cards_and_corners_are_zero():
    """API-Football envoie null pour zéro : une équipe sans carton avait sa paire corners/cartons écartée (moyennes gonflées)."""
    home = stat_block(1, Corner_Kicks=None, Total_Shots=9, Fouls=12, Yellow_Cards=None, Red_Cards=None)
    away = stat_block(2, Corner_Kicks=4, Total_Shots=8, Fouls=10, Yellow_Cards=3, Red_Cards=None)
    e = stats._entry_from([home, away])
    h, a = e["teams"]["1"], e["teams"]["2"]
    assert h["corners"] == 0 and h["cards"] == 0 and h["yel"] == 0 and h["nul"] == [], h
    assert a["cards"] == 3 and a["nul"] == [] and e["corners"] == 4 and e["cards"] == 3
    # tirs inconnus (null) = toujours une donnée absente, pas un zéro
    h2 = stat_block(1, Corner_Kicks=5, Total_Shots=None, Fouls=None, Yellow_Cards=None, Red_Cards=None)
    t = stats._entry_from([h2, away])["teams"]["1"]
    assert t["nul"] == ["shots", "fouls", "sot", "cards", "yel"] and t["corners"] == 5, t     # aucune autre stat : les cartons manquent
    t = stats._entry_from([stat_block(1, Corner_Kicks=None, Total_Shots=None, Yellow_Cards=None, Red_Cards=None), away])["teams"]["1"]
    assert {"corners", "cards", "yel", "shots"} <= set(t["nul"]), t                              # liste vide de stats : jamais 0
    # le profil de l'équipe garde le match (avant : paire corners/cartons perdue)
    stats._today = lambda: PIN
    d = apistats.load()
    ev = {"id": 77, "ts": 1791000000, "home": "A", "home_id": 1, "away": "B", "away_id": 2, "gh": 1, "ga": 0, "lk": "soccer_brazil_campeonato",
          "ln": "Brasileirão"}
    apistats.record(d, ev, e)
    row = d["teams"]["1"]["m"][0]
    ic, ik = apistats.R_STATS + 2 * apistats.KEYS.index("corners"), apistats.R_STATS + 2 * apistats.KEYS.index("cards")
    assert row[ic:ic + 2] == [0, 4] and row[ik:ik + 2] == [0, 3], row
    # ancienne entrée du cache disque (avant le correctif) : « nul » sur cartons/corners avec des tirs connus = zéro
    old = {"teams": {"1": {"corners": 0.0, "cards": 0.0, "yel": 0.0, "shots": 9.0, "sot": 0.0, "fouls": 12.0, "nul": ["corners", "cards", "yel"]},
                     "2": {"corners": 4.0, "cards": 3.0, "yel": 3.0, "shots": 8.0, "sot": 0.0, "fouls": 10.0, "nul": []}}}
    apistats.record(d, dict(ev, id=78), old)
    assert d["teams"]["1"]["m"][1][ik:ik + 2] == [0, 3]
    gone = {"teams": {"1": {"corners": 0.0, "cards": 0.0, "yel": 0.0, "shots": 0.0, "sot": 0.0, "fouls": 0.0,
                            "nul": ["corners", "cards", "yel", "shots", "sot", "fouls"]}, "2": old["teams"]["2"]}}
    assert apistats.record(d, dict(ev, id=79), gone) == 0                                       # rien de connu d'un côté : match sans stats utilisables
    assert len(d["teams"]["1"]["m"]) == 2 and len(d["teams"]["2"]["m"]) == 2


# --- 5. remplaçant jamais entré -----------------------------------------------------------

def test_unused_substitute_has_no_player_stats():
    """Un remplaçant sans minute (games.minutes null) était réglé à 0 tir : le « Moins de » gagnait. Maintenant il n'a pas de stats."""
    stats._today = lambda: PIN
    os.environ["STATS_API_KEY"] = "test-key"
    stats._calls, stats._halted = 0, False
    stats._neg.clear()
    played = {"player": {"name": "K. Mbappé"}, "statistics": [{"games": {"minutes": 90}, "shots": {"total": None, "on": None}, "fouls": {}}]}
    shooter = {"player": {"name": "V. Osimhen"}, "statistics": [{"games": {"minutes": 71}, "shots": {"total": 3, "on": 1}, "fouls": {"committed": 2}}]}
    bench = {"player": {"name": "A. Benched"}, "statistics": [{"games": {"minutes": None}, "shots": {"total": None, "on": None}, "fouls": {}}]}
    zero = {"player": {"name": "B. Zero"}, "statistics": [{"games": {"minutes": 0}, "shots": {"total": None}, "fouls": {}}]}

    def fake(url, p):
        if url.endswith("/fixtures/statistics"):
            return Resp({"errors": [], "response": [stat_block(10, Corner_Kicks=5, Total_Shots=9), stat_block(20, Corner_Kicks=3, Total_Shots=7)]})
        return Resp({"errors": [], "response": [{"players": [played, shooter, bench, zero]}]})
    orig = stats.requests.get
    stats.requests.get = lambda url, **kw: fake(url, kw.get("params"))
    try:
        st = stats.fixture_stats(4242)
    finally:
        stats.requests.get = orig
        os.environ.pop("STATS_API_KEY", None)
    assert st["players"]["K. Mbappé"] == {"shots": 0.0, "shots_on_target": 0.0, "fouls": 0.0}      # a joué sans tir : vrai zéro
    assert st["players"]["V. Osimhen"]["shots"] == 3.0
    assert "A. Benched" not in st["players"] and "B. Zero" not in st["players"]
    assert stats.player_stat(st, "A. Benched", "shots") is None and stats.player_stat(st, "Benched", "shots") is None   # ni 0 ni gagné
    assert stats.player_stat(st, "Mbappe", "shots") == 0.0


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests fix_names : OK")


if __name__ == "__main__":
    main()
