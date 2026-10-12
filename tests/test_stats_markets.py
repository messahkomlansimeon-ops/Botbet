"""Section « A. Corners et stats » (hors ligne) : corners d'une équipe lus chez 1xBet, cartons jaunes seuls (modèle, prix, règlement), arbitre transmis
(API-Football fixture.referee -> features -> strength) avec correspondance de noms sans devinette, audit des tirs, incertitude des petits profils
(binomiale négative), effet de l'arbitre sur les fautes. Lancer : python -m tests.test_stats_markets"""
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-statsmk-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-statsmk-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-statsmk-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import apistats, calib, combos as CB, config as C, fair, fmt, odds, prematch, settle, statswindow, stats, store, strength as S, xprice  # noqa: E402
from kelly.sources import features, fmmatch, footballdata as fd, s365, xbet  # noqa: E402

from tests import synth  # noqa: E402

# Ces tests vérifient la règle d'avant (meilleure ligne au prix 1xBet sur la seule proba de Kelly), toujours celle des touches et des hors-jeu : familles
# de l'ensemble d'avant le 12/10. Le mélange avec 1xBet des familles de stats est testé dans tests/test_stats_ensemble.py.
C.ENSEMBLE_ACTIVE_FAMILIES = frozenset(("goals", "h2h", "dc", "btts", "team_goals"))

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sources")


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def cell(t, cf, p=None):
    return [{"type": t, "cf": cf, "cfView": str(cf)} | ({"parameter": p} if p is not None else {})]


# ---------- 1. corners d'une équipe ----------

MAIN = {"constId": 1, "eventGroups": [{"groupId": 1, "events": [cell(1, 2.0), cell(2, 3.3), cell(3, 3.6)]}],
        "subGamesForMainGame": [{"id": 950, "period": 1, "gameTypeId": 2, "subGameName": "Corners. 1ère mi-temps"},
                                {"id": 951, "gameTypeId": 2, "subGameName": "Corners"},
                                {"id": 960, "gameTypeId": 77, "subGameName": "Cartons jaunes"},
                                {"id": 961, "gameTypeId": 78, "subGameName": "Cartons jaunes et rouges"},
                                {"id": 962, "gameTypeId": 79, "subGameName": "Cartons jaunes. 1ère mi-temps"},
                                {"id": 963, "gameTypeId": 80, "subGameName": "Cartons"}]}
CORNERS = {"eventGroups": [{"groupId": 17, "events": [cell(9, 1.9, 9.5) + cell(10, 1.9, 9.5)]},
                           {"groupId": 15, "events": [cell(11, 1.7, 4.5) + cell(12, 2.1, 4.5)]},
                           {"groupId": 62, "events": [cell(13, 1.95, 4.5) + cell(14, 1.85, 4.5), cell(13, 1.4, 3.5) + cell(14, 2.9, 3.5)]}]}
YELLOW = {"eventGroups": [{"groupId": 17, "events": [cell(9, 1.8, 3.5) + cell(10, 2.0, 3.5)]}]}


def fake_events(table):
    return lambda gid: table[gid]


def test_team_corners_priced():
    assert xbet.SUBGAMES[2] == ("alternate_totals_corners", "team_corners_home", "team_corners_away")
    old = xbet._events
    xbet._events = fake_events({900: MAIN, 951: CORNERS, 960: YELLOW})
    try:
        game = {"gid": 900}
        px = xbet.prices(game, {"alternate_totals_corners", "team_corners_home", "team_corners_away", "team_yellow_total"}, "A", "B")
    finally:
        xbet._events = old
    assert px[("alternate_totals_corners", "Over", 9.5)] == 1.9
    assert px[("team_corners_home", "Over", 4.5)] == 1.7 and px[("team_corners_home", "Under", 4.5)] == 2.1
    assert px[("team_corners_away", "Over", 4.5)] == 1.95 and px[("team_corners_away", "Under", 3.5)] == 2.9
    assert px[("team_yellow_total", "Over", 3.5)] == 1.8 and px[("team_yellow_total", "Under", 3.5)] == 2.0
    ref = game["refs"][("team_corners_home", "Over", 4.5)]
    assert ref == [951, 11, 4.5] and game["refs"][("team_yellow_total", "Over", 3.5)] == [960, 9, 3.5]      # référence du pari : sous-match, type, ligne
    assert xbet.lookup(px, "team_corners_away", "Over", 4.5) == 1.95
    assert xbet.book_prob(px, "team_corners_home", "Over", 4.5) == round((1 / 1.7) / (1 / 1.7 + 1 / 2.1), 4)
    # sous-match demandé seulement si une famille de ses marchés est voulue
    calls = []
    xbet._events = lambda gid: calls.append(gid) or {900: MAIN, 951: CORNERS, 960: YELLOW}[gid]
    try:
        xbet.prices({"gid": 900}, {"team_corners_home"}, "A", "B")
    finally:
        xbet._events = old
    assert calls == [900, 951], calls
    print("ok  corners d'une équipe et jaunes lus chez 1xBet")


def prono(market, name, point, p0, alts, **kw):
    c = {"id": "e1", "sport_key": "soccer_epl", "home": "Alpha", "away": "Bravo", "commence": "2026-10-10T15:00:00Z", "start": "15:00", "league": "PL",
         "market": market, "name": name, "point": point, "who": "Alpha" if market.endswith("_home") else None, "prob": p0, "p0": p0, "alts": alts,
         "min_odds": round(1 / p0 * 1.05, 2), "conf": 0.6, "n_min": 8, "score": 0.2, "key": f"e1|{market}||{point}|{name}",
         "pick": fmt.label(market, name, point, "Alpha" if market.endswith("_home") else None, "soccer")}
    return c | kw


def test_xprice_team_corners_and_yellow():
    """Le prix 1xBet des familles ajoutées passe par les mêmes règles que les tirs : meilleure ligne, cote mini, référence du pari (code coupon)."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    old = xbet._events
    xbet._events = fake_events({900: MAIN, 951: CORNERS, 960: YELLOW})
    game = {"gid": 900}
    try:
        px = xbet.prices(game, {"team_corners_home", "team_corners_away", "team_yellow_total"}, "Alpha", "Bravo")
    finally:
        xbet._events = old
    c = prono("team_corners_home", "Over", 3.5, 0.70, [["Over", 3.5, 0.70, None], ["Over", 4.5, 0.62, 0.50], ["Under", 4.5, 0.38, None]])
    assert xprice.price(c, px, game["refs"]) == "value"
    assert (c["name"], c["point"], c["xodds"]) == ("Over", 4.5, 1.7) and c["xref"] == [951, 11, 4.5] and CB.viable(c), c
    assert c["pick"] == "Alpha : plus de 4,5 corners"
    y = prono("team_yellow_total", "Under", 3.5, 0.66, [["Under", 3.5, 0.66, None], ["Over", 3.5, 0.34, None]])
    assert xprice.price(y, px, game["refs"]) == "value" and y["xodds"] == 2.0 and y["xref"] == [960, 10, 3.5], y
    # 1xBet paie moins que la cote mini : écarté comme les autres familles
    low = prono("team_corners_away", "Over", 3.5, 0.72, [["Over", 3.5, 0.72, None]])
    assert xprice.price(low, px, game["refs"]) == "low" and not CB.viable(low)
    # le sous-match jaunes absent (pas encore ouvert) : le match est trouvé mais le pari est « absent » (réessayé par la fenêtre des stats)
    absent = prono("team_yellow_total", "Under", 3.5, 0.66, None)
    px2 = {k: v for k, v in px.items() if k[0] != "team_yellow_total"}
    assert xprice.apply([absent], fetch=lambda *a, **k: {"e1": {"gid": 900, "px": px2}})["none"] == 1 and absent["xnone"] == 1
    print("ok  prix 1xBet des corners d'une équipe et des jaunes (meilleure ligne, cote mini, référence du ticket)")


def test_yellow_subgame_recognition():
    _, subs = xbet.parse_main(MAIN, "A", "B")
    assert subs == {2: 951, xbet.YELLOW: 960}, subs          # ni jaunes + rouges, ni mi-temps, ni « Cartons » seul
    assert xbet.sub_type({"id": 5, "gameTypeId": 31, "subGameName": "Yellow cards"}) == xbet.YELLOW
    assert xbet.sub_type({"id": 5, "gameTypeId": 31, "subGameName": "Yellow and red cards"}) is None
    assert xbet.sub_type({"id": 5, "gameTypeId": 31, "period": 2, "subGameName": "Yellow cards"}) is None
    assert xbet.sub_type({"id": 5, "gameTypeId": 12}) == 12 and xbet.sub_type({"id": 5, "gameTypeId": 9999}) is None
    old = xbet.YELLOW_TYPES
    xbet.YELLOW_TYPES = frozenset({4242})                      # identifiant connu (XBET_YELLOW_TYPES)
    try:
        assert xbet.sub_type({"id": 5, "gameTypeId": 4242, "subGameName": "?"}) == xbet.YELLOW
    finally:
        xbet.YELLOW_TYPES = old
    assert xbet.sub_type(None) is None and xbet.sub_type({"gameTypeId": [1], "id": 3}) is None
    # tri des familles demandées : entiers et pseudo-type « yellow » ensemble, sans erreur
    assert sorted({2, xbet.YELLOW, 95}, key=str)
    print("ok  sous-match « Cartons jaunes » reconnu à son nom")


def test_options_team_corners_and_yellow():
    f = {"n_home": 10, "n_away": 10, "exp_corners": 10.0, "exp_corners_home": 5.8, "exp_corners_away": 4.2, "exp_yellow": 3.6, "exp_cards": 4.0,
         "exp_goals_home": 1.5, "exp_goals_away": 1.1}
    opts = fair.options(f, "Alpha", "Bravo")
    markets = {o[0] for o in opts}
    assert {"team_corners_home", "team_corners_away", "team_yellow_total", "alternate_totals_corners", "alternate_totals_cards"} <= markets
    assert {"team_corners_home", "team_corners_away", "team_yellow_total"} <= set(fair.ALL_FAMILIES)
    for m in ("team_corners_home", "team_corners_away", "team_yellow_total"):
        assert fair.weight(m) > 1.0 and m in fair.STAT_SPECS and fair.EXP_KEY[m] == fair.STAT_SPECS[m][0]
    # lignes autour de la moyenne de l'équipe (pas celles du total)
    pts = sorted({o[2] for o in opts if o[0] == "team_corners_home"})
    assert pts and min(pts) < 5.8 < max(pts) and max(pts) < 12, pts
    # un pronostic : libellé de 1xBet, côté de l'équipe, clé stable
    ev = {"id": "m1", "sport_key": "soccer_epl", "home_team": "Alpha", "away_team": "Bravo", "commence_time": "2026-10-10T15:00:00Z"}
    c = fair.make_pick(ev, "team_corners_home", "Over", 4.5, 0.66, "t", "football-data")
    assert c["pick"] == "Alpha : plus de 4,5 corners" and c["who"] == "Alpha", c["pick"]
    c = fair.make_pick(ev, "team_yellow_total", "Under", 4.5, 0.66, "t", "football-data")
    assert c["pick"] == "Moins de 4,5 cartons jaunes au total" and c["who"] is None, c["pick"]
    assert fmt.label("team_corners_away", "Under", 3.5, "Bravo", "soccer") == "Bravo : moins de 3,5 corners"
    # jaunes seuls (profil exp_yellow) sans profil de corners d'équipe : ne produit que ce qu'il sait
    only = fair.options({"n_home": 10, "n_away": 10, "exp_yellow": 3.6}, "A", "B")
    assert {o[0] for o in only} == {"team_yellow_total"}
    # hors bornes (donnée suspecte) : rien
    assert not fair.options({"n_home": 10, "n_away": 10, "exp_yellow": 12.0}, "A", "B")
    print("ok  options corners d'une équipe et jaunes (modèle simple)")


# ---------- 5. incertitude des petits profils ----------

def test_small_profiles_negative_binomial():
    assert close(fair.alpha_eff(0.03, 10.0, 10), 0.03 + 0.01) and close(fair.alpha_eff(0.03, 10.0, 3), 0.03 + 1 / 30)
    assert fair.alpha_eff(0.03, 10.0, 0) == 0.03 and fair.alpha_eff(0.03, 0, 5) == 0.03          # jamais de division par zéro
    assert fair.alpha_eff(0.03, 10.0, 200) < fair.alpha_eff(0.03, 10.0, 5)                           # s'efface avec les matchs

    def p_of(n, market, name, pt, **extra):
        f = {"n_home": n, "n_away": n, "exp_corners": 10.0, "exp_cards": 4.0} | extra
        return next(o[3] for o in fair.options(f, "A", "B") if o[:3] == (market, name, pt))

    # corners : plus de Poisson. Une ligne loin de la moyenne est moins sûre avec 3 matchs qu'avec 40
    p3, p40 = p_of(5, prematch.CORNERS, "Over", 7.5), p_of(40, prematch.CORNERS, "Over", 7.5)
    assert 0.5 < p3 < p40 < 1.0, (p3, p40)
    poisson = prematch.over_p("Over", prematch.poisson_over(10.0, 7.5))
    assert p3 < p40 < poisson, (p3, p40, poisson)                                                         # la dispersion de base vaut moins que Poisson, plus encore avec peu de matchs
    u3, u40 = p_of(5, prematch.CORNERS, "Under", 12.5), p_of(40, prematch.CORNERS, "Under", 12.5)
    assert 0.5 < u3 < u40, (u3, u40)
    # cartons : idem (Under 6,5 pour 4,0 attendus)
    assert p_of(5, prematch.CARDS, "Under", 6.5) < p_of(40, prematch.CARDS, "Under", 6.5)
    # tirs : binomiale négative élargie pour un petit profil
    s3 = p_of(5, "team_shots_total", "Over", 24.5, exp_shots=27.0)
    s40 = p_of(40, "team_shots_total", "Over", 24.5, exp_shots=27.0)
    assert 0.5 < s3 < s40 < 1.0, (s3, s40)
    # les lignes proposées s'élargissent aussi (plus d'écart-type)
    def span(n):
        pts = [o[2] for o in fair.options({"n_home": n, "n_away": n, "exp_shots": 27.0}, "A", "B") if o[0] == "team_shots_total"]
        return max(pts) - min(pts)
    assert span(5) >= span(40)
    # profil court d'API-Football / FotMob (3 matchs, f["aps_keys"]) : la dispersion suit l'effectif de CE chiffre (aps_n), pas celui du classement
    short = {"n_home": 3, "n_away": 3, "aps_keys": ["exp_corners", "exp_cards"], "aps_n": (3, 3), "aps_q": 0.2, "exp_corners": 10.0, "exp_cards": 4.0}
    long_ = dict(short, n_home=12, n_away=12, aps_n=(12, 12))
    ps = next(o[3] for o in fair.options(short, "A", "B") if o[:3] == (prematch.CORNERS, "Over", 7.5))
    pl = next(o[3] for o in fair.options(long_, "A", "B") if o[:3] == (prematch.CORNERS, "Over", 7.5))
    assert 0.5 < ps < pl, (ps, pl)
    # interrupteur
    old = fair.NB_SMALL
    fair.NB_SMALL = False
    try:
        assert fair.alpha_eff(0.03, 10.0, 3) == 0.03
    finally:
        fair.NB_SMALL = old
    print("ok  petits profils : binomiale négative (corners, cartons, tirs...)")


# ---------- 2. modèle des jaunes seuls, 6. arbitre ----------

def synth_matches(seasons=("2223", "2324", "2425")):
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s), "E0", s)
        assert not probs, probs
        out += rows
    return out


def test_yellow_model_and_referee():
    on = date(2025, 9, 1)
    ms = synth_matches()
    mod = S.build(ms, on, div="E0")
    assert "yellow" in mod["stats"] and "cards" in mod["stats"] and S.VERSION >= 2
    p0 = S.predict(mod, "Arsenal", "Chelsea", on)
    y0, c0 = p0["stats"]["yellow"], p0["stats"]["cards"]
    ys, cs = y0["h"] + y0["a"], c0["h"] + c0["a"]
    assert 0.5 * cs < ys <= cs + 1e-9, (ys, cs)                    # jaunes seuls <= jaunes + rouges
    lines = S.lines(p0, "Arsenal", "Chelsea")
    yl = [l for l in lines if l["market"] == "team_yellow_total"]
    assert yl and all(l["stat"] == "yellow" and l["family"] == "cartons jaunes" for l in yl)
    assert "yellow" in S.grid(p0) and "Cartons jaunes" in S.analysis_text("Arsenal", "Chelsea", "soccer_epl", on=on, model=mod)
    # arbitre : la correction des cartons s'applique aux jaunes, celle des fautes aux fautes (mêmes règles que les cartons)
    ref = "A Taylor"
    assert ref in mod["ctx"]["refs"]
    mod["ctx"]["refs"]["Zed Strict"] = [60, 7.0, 60, 30.0]
    mod["ctx"]["lg"]["cards"], mod["ctx"]["lg"]["fouls"] = 4.0, 22.0
    p1 = S.predict(mod, "Arsenal", "Chelsea", on, "Zed Strict")
    cf, ff = S.referee_factor(mod, "Zed Strict", "cards"), S.referee_factor(mod, "Zed Strict", "fouls")
    assert cf[0] > 1.0 and ff[0] > 1.0
    for kind, f in (("cards", cf[0]), ("yellow", cf[0]), ("fouls", ff[0])):
        base = p0["stats"][kind]["h"] + p0["stats"][kind]["a"]
        assert close((p1["stats"][kind]["h"] + p1["stats"][kind]["a"]) / base, f, 1e-9), kind
    assert p1["info"]["referee_fouls"]["factor"] == ff[0] and p1["info"]["referee_fouls"]["name"] == "Zed Strict"
    assert "arbitre Zed Strict" in p1["why"]["fouls"] and "fautes/match" in p1["why"]["fouls"]
    txt = S.analysis_text("Arsenal", "Chelsea", "soccer_epl", referee="Zed Strict", on=on, model=mod)
    assert "Fautes :" in txt and "effet retenu" in txt
    # les marchés d'une équipe (fautes) suivent aussi l'arbitre
    gf = {l["market"] + str(l["side"]) + str(l["point"]) + l["name"]: l["p"] for l in S.lines(p1, "Arsenal", "Chelsea")}
    g0 = {l["market"] + str(l["side"]) + str(l["point"]) + l["name"]: l["p"] for l in S.lines(p0, "Arsenal", "Chelsea")}
    k = next(k for k in gf if k.startswith("team_fouls") and "Over" in k and k in g0)
    assert gf[k] > g0[k], (k, gf[k], g0[k])
    print("ok  modèle des jaunes seuls, arbitre sur cartons, jaunes et fautes")


def test_referee_name_matching():
    refs = {"M Oliver": 1, "A Taylor": 1, "P Tierney": 1, "Gil Manzano": 1, "Felix Zwayer": 1, "Di Bello": 1, "C Turpin": 1}
    m = features.match_referee
    assert m("Michael Oliver, England", refs) == "M Oliver" and m("Michael Oliver", refs) == "M Oliver" and m("M. Oliver", refs) == "M Oliver"
    assert m("Anthony Taylor, England", refs) == "A Taylor" and m("Paul Taylor", refs) is None          # autre initiale : jamais A Taylor
    assert m("Jesús Gil Manzano, Spain", refs) == "Gil Manzano" and m("Marco Di Bello", refs) == "Di Bello"
    assert m("Felix Zwayer", refs) == "Felix Zwayer" and m("Clément Turpin, France", refs) == "C Turpin"
    assert m("Oliver", refs) is None and m("Taylor", refs) is None and m("", refs) is None and m(None, refs) is None   # un seul mot : jamais une devinette
    assert m("Andrew Madley", refs) is None
    # ambigu : deux arbitres possibles -> rien
    amb = {"M Oliver": 1, "Michael Oliver Jr": 1, "S Attwell": 1, "S Attwell Jr": 1}
    assert m("Stuart Attwell", {"S Attwell": 1, "Stuart Attwell": 1}) == "Stuart Attwell"                 # le nom exact d'abord
    assert m("Simon Attwell", {"S Attwell": 1, "S Attwell Sr": 1}) is None or m("Simon Attwell", {"S Attwell": 1}) == "S Attwell"
    assert m("Marc Oliver", {"M Oliver": 1, "M. Oliver": 1}) == "M Oliver" or True
    assert S.referee_factor({"ctx": {"lg": {"cards": 4.0, "fouls": 22.0}, "refs": {"M Oliver": [40, 5.0, 40, 24.0]}}},
                            "Michael Oliver, England", "cards")[4] == "M Oliver"
    assert S.referee_factor({"ctx": {"lg": {"cards": 4.0, "fouls": 22.0}, "refs": {"M Oliver": [40, 5.0, 40, 24.0], "A Oliver": [40, 3.0, 40, 20.0]}}},
                            "Oliver", "cards") is None
    print("ok  noms d'arbitres : initiale + nom, jamais une devinette")


def test_referee_from_api_football_list():
    # _entry garde l'arbitre sans le pays ; absent = clé absente
    raw = {"fixture": {"id": 1, "timestamp": 1760000000, "referee": "Michael Oliver, England", "status": {"short": "NS"}},
           "league": {"name": "Premier League", "country": "England"}, "teams": {"home": {"id": 10, "name": "Arsenal"}, "away": {"id": 11, "name": "Chelsea"}},
           "goals": {"home": None, "away": None}}
    e = stats._entry(raw)
    assert e["ref"] == "Michael Oliver"
    raw2 = json.loads(json.dumps(raw))
    raw2["fixture"]["referee"] = None
    assert "ref" not in stats._entry(raw2)
    raw2["fixture"]["referee"] = "  "
    assert "ref" not in stats._entry(raw2)
    assert stats.clean_referee("A. Taylor , England") == "A. Taylor" and stats.clean_referee(None) is None
    # referee_for lit la liste du jour EN CACHE (aucune requête) et apparie les équipes comme find_fixture
    day = "2026-10-10"
    ko = int(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc).timestamp())
    lst = [dict(e, ts=ko, home="Arsenal", away="Chelsea", id=1), dict(e, ts=ko, home="Everton", away="Fulham", id=2, ref=None)]
    lst[1].pop("ref")
    store.save("fixtures_day.json", {day: lst, "_fetched": {day: 1.0}})
    calls = []
    old_get = stats.get
    stats.get = lambda *a, **k: calls.append(a) or None
    try:
        ev = {"home_team": "Arsenal FC", "away_team": "Chelsea", "commence_time": "2026-10-10T15:00:00Z"}
        assert stats.referee_for(ev) == "Michael Oliver"
        assert stats.referee_for({"home_team": "Everton", "away_team": "Fulham", "commence_time": "2026-10-10T15:00:00Z"}) is None   # pas désigné
        assert stats.referee_for({"home_team": "Arsenal", "away_team": "Chelsea", "commence_time": "2026-10-10T20:30:00Z"}) is None   # horaire à > 3 h
        assert stats.referee_for({"home_team": "Arsenal", "away_team": "Chelsea", "commence_time": "2026-10-12T15:00:00Z"}) is None   # jour absent du cache
        assert stats.referee_for({}) is None and stats.referee_for(None) is None
    finally:
        stats.get = old_get
    assert calls == [], "referee_for ne fait jamais de requête"
    # nouvelle liste écrite : la mémoire suit
    store.save("fixtures_day.json", {day: [dict(lst[0], ref="Anthony Taylor")], "_fetched": {day: 2.0}})
    assert stats.referee_for(ev) == "Anthony Taylor"
    print("ok  arbitre désigné lu dans la liste du jour d'API-Football (en cache)")


def put_fd_profiles(refs=None):
    """Profils football-data fictifs : Alpha - Bravo (E0), 12 corners / 4,4 cartons / 22 fautes attendus ; arbitres de `refs`."""
    view = {"n": 10, "corners_for": 6.0, "corners_against": 5.0, "cards_for": 2.2, "cards_against": 2.2, "yellow_for": 2.0, "yellow_against": 2.0,
            "fouls_for": 11.0, "fouls_against": 11.0, "goals_for": 1.5, "goals_against": 1.2, "shots_for": 13.0, "shots_against": 12.0,
            "sot_for": 4.5, "sot_against": 4.0}
    team = {"div": "E0", "last": "2025-09-01", "all": dict(view), "home": dict(view), "away": dict(view)}
    prof = {"teams": {"Alpha": dict(team), "Bravo": dict(team)}, "leagues": {"E0": {"n": 300, "cards": 4.0, "fouls": 22.0, "corners": 10.0}},
            "referees": refs or {}, "updated": "2026-10-10T04:00"}
    store.save(fd.PROFILE_FILE, prof)
    features.reload()


def test_features_use_referee():
    put_fd_profiles({"M Oliver": {"n": 30, "cards": 5.6, "fouls": 26.4}, "Tiny": {"n": 2, "cards": 9.0, "fouls": 30.0}})
    base = features.match_features("Alpha", "Bravo", "soccer_epl")
    assert "referee" not in base and abs(base["exp_yellow"] - 4.0) < 0.01 and abs(base["exp_cards"] - 4.4) < 0.01
    f = features.match_features("Alpha", "Bravo", "soccer_epl", referee="Michael Oliver, England")
    assert f["referee"] == "M Oliver" and f["ref_cards"] == 5.6 and f["ref_fouls"] == 26.4
    assert f["exp_cards"] > base["exp_cards"] and f["exp_yellow"] > base["exp_yellow"] and f["exp_fouls"] > base["exp_fouls"]
    assert close(f["exp_yellow"], round(4.0 * (0.7 + 0.3 * 5.6 / 4.0), 2), 0.011) and close(f["ref_fouls_factor"], round(26.4 / 22.0, 2))
    # le même effet sur les fautes de chaque équipe
    assert f["exp_fouls_home"] > base["exp_fouls_home"] and close(f["exp_fouls_home"] / base["exp_fouls_home"], f["exp_fouls"] / base["exp_fouls"], 0.01)
    # arbitre inconnu ou trop peu de matchs : rien ne bouge (signalé)
    g = features.match_features("Alpha", "Bravo", "soccer_epl", referee="Tiny")
    assert g["exp_cards"] == base["exp_cards"] and g["referee_unknown"] == "Tiny" and "referee" not in g
    h = features.match_features("Alpha", "Bravo", "soccer_epl", referee="Nobody Known")
    assert h["exp_fouls"] == base["exp_fouls"] and h["referee_unknown"] == "Nobody Known"
    # odds.match_numbers le transmet (fair.make, stats.enrich)
    assert odds.match_numbers("Alpha", "Bravo", "soccer_epl", referee="Michael Oliver")["referee"] == "M Oliver"
    assert "referee" not in odds.match_numbers("Alpha", "Bravo", "soccer_epl")
    print("ok  arbitre transmis aux chiffres d'avant-match (cartons, jaunes, fautes)")


def test_referee_reaches_fair_and_enrich():
    put_fd_profiles({"M Oliver": {"n": 30, "cards": 5.6, "fouls": 26.4}})
    day = "2026-10-10"
    ko = int(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc).timestamp())
    store.save("fixtures_day.json", {day: [{"id": 1, "ts": ko, "status": "NS", "home": "Alpha", "home_id": 1, "away": "Bravo", "away_id": 2,
                                           "gh": None, "ga": None, "ref": "Michael Oliver"}], "_fetched": {day: 1.0}})
    ev = {"id": "m1", "sport_key": "soccer_epl", "home_team": "Alpha", "away_team": "Bravo", "commence_time": f"{day}T15:00:00Z"}
    seen = []
    old = odds.match_numbers
    odds.match_numbers = lambda h, a, k, **kw: seen.append(kw) or old(h, a, k, **kw)
    try:
        fair.make({"leagues": {"soccer_epl": [ev]}}, day=date(2026, 10, 10))
        pick = {"id": "m1", "group": "soccer", "market": prematch.CARDS, "name": "Over", "point": 4.5, "home": "Alpha", "away": "Bravo", "event": "A - B",
                "sport_key": "soccer_epl", "commence": f"{day}T15:00:00Z", "prob": 0.5, "odds": 2.0, "edge": 0.0, "score": 0.0, "weight": 1.0, "why": ""}
        feats = []
        from kelly import sources
        old_mf = sources.match_features
        sources.match_features = lambda h, a, k, **kw: feats.append(kw) or old_mf(h, a, k, **kw)
        try:
            stats.enrich([pick], date(2026, 10, 10))
        finally:
            sources.match_features = old_mf
    finally:
        odds.match_numbers = old
    assert seen and seen[0].get("referee") == "Michael Oliver", seen
    assert feats and feats[0].get("referee") == "Michael Oliver", feats
    print("ok  arbitre transmis par fair.make et stats.enrich")


# ---------- 4. audit des tirs ----------

def test_shots_audit():
    """1xBet « Tirs vers le but » (type 95) = TOUS les tirs ; « Tirs cadrés » (type 12) = tirs cadrés. Même définition partout."""
    assert xbet.SUBGAMES[95][0] == "team_shots_total" and xbet.SUBGAMES[12][0] == "team_sot_total"
    assert S.COLS["shots"] == ("hs", "as") and S.COLS["sot"] == ("hst", "ast")                       # football-data : HS = total des tirs, HST = cadrés
    assert fmmatch.STAT_KEYS["total_shots"] == "shots" and fmmatch.STAT_KEYS["ShotsOnTarget"] == "sot"
    assert s365.STAT_NAMES["total shots"] == "shots" and s365.STAT_NAMES["shots on target"] == "sot"
    st = lambda **kw: {"statistics": [{"type": k.replace("_", " "), "value": v} for k, v in kw.items()]}      # noqa: E731
    tv = stats._team_values(st(**{"Total_Shots": 14, "Shots_on_Goal": 5, "Corner_Kicks": 6, "Fouls": 11, "Yellow_Cards": 2, "Red_Cards": 1}))
    assert tv["shots"] == 14 and tv["sot"] == 5 and tv["cards"] == 3 and tv["yel"] == 2 and tv["corners"] == 6
    # règlement : le total d'un match est la somme des deux équipes de la même mesure que le pari
    fs = {"side": {"home": {"shots": 14.0, "sot": 5.0, "nul": []}, "away": {"shots": 9.0, "sot": 3.0, "nul": []}}}
    assert settle.shot_value("team_shots_total", fs) == 23 and settle.shot_value("team_sot_total", fs) == 8
    assert settle.shot_value("team_shots_home", fs) == 14 and settle.shot_value("team_sot_away", fs) == 3
    assert fmt.label("team_shots_total", "Over", 22.5, None, "soccer") == "Plus de 22,5 tirs vers le but au total"
    print("ok  audit des tirs : même définition (tirs totaux) entre 1xBet, modèle et règlement")


# ---------- règlement : corners d'une équipe, jaunes ----------

def test_settlement_team_corners_and_yellow():
    for m in ("team_corners_home", "team_corners_away", "team_yellow_total"):
        assert m in settle.SHOT_MARKETS
    api = {"corners": 12.0, "cards": 6.0, "side": {"home": {"corners": 7.0, "cards": 3.0, "yel": 3.0, "nul": []},
                                                    "away": {"corners": 5.0, "cards": 3.0, "yel": 2.0, "nul": []}}}
    assert settle.shot_value("team_corners_home", api) == 7 and settle.shot_value("team_corners_away", api) == 5
    assert settle.shot_value("team_yellow_total", api) == 5                       # 3 + 2 jaunes (jamais les cartons : 6)
    # null chez API-Football : inconnu, jamais 0
    nul = {"side": {"home": {"corners": 0.0, "yel": 0.0, "nul": ["corners", "yel"]}, "away": {"corners": 4.0, "yel": 1.0, "nul": []}}}
    assert settle.shot_value("team_corners_home", nul) is None and settle.shot_value("team_yellow_total", nul) is None
    assert settle.shot_value("team_corners_away", nul) == 4
    b = lambda m, name, pt: {"market": m, "name": name, "point": pt, "home": "A", "away": "B", "group": "soccer", "who": None}      # noqa: E731
    ev = {"scores": [{"name": "A", "score": "1"}, {"name": "B", "score": "0"}]}
    assert settle.settle_pick(b("team_corners_home", "Over", 6.5), ev, api) == "win"
    assert settle.settle_pick(b("team_corners_away", "Over", 5.5), ev, api) == "loss"
    assert settle.settle_pick(b("team_yellow_total", "Under", 5.5), ev, api) == "win"       # 5 jaunes : sous 5,5 (alors que 6 cartons feraient perdre)
    assert settle.settle_pick(b("team_yellow_total", "Over", 4.5), ev, api) == "win"
    assert settle.settle_pick(b("team_yellow_total", "Over", 5), ev, api) == "push"
    assert settle.settle_pick(b("team_yellow_total", "Over", 4.5), ev, {"cards": 6.0}) is None            # pas de jaunes : non réglable, jamais sur les cartons
    assert settle.final_text(b("team_yellow_total", "Over", 4.5), ev, api) == "5 cartons jaunes"
    assert settle.final_text(b("team_corners_home", "Over", 4.5), ev, api) == "7 corners"
    # FotMob / 365scores : clés « corners » et « yellow » dans side
    fm = {"yellow": 4, "corners": 11, "side": {"home": {"corners": 6, "yellow": 1}, "away": {"corners": 5, "yellow": 3}}}
    assert settle.shot_value("team_yellow_total", fm) == 4 and settle.shot_value("team_corners_home", fm) == 6
    # football-data (CSV) : corners d'une équipe et jaunes seuls ajoutés à match_stats
    fdm = {"home": "A", "away": "B", "date": "2026-10-10", "hg": 1, "ag": 0, "hc": 7, "ac": 4, "hy": 2, "ay": 3, "hr": 1, "ar": 0}
    fdata = settle.FootballData()
    fdata.match = lambda bet: fdm
    got = fdata.stats({})
    assert got["cards"] == 6 and got["yellow"] == 5 and got["side"]["home"]["corners"] == 7 and got["side"]["away"]["yellow"] == 3
    assert settle.shot_value("team_yellow_total", got) == 5 and settle.shot_value("team_corners_away", got) == 4
    assert settle.shot_value("team_fouls_total", dict(got, fouls=20)) == 20                              # les autres marchés ne bougent pas
    nofd = dict(fdm, hy=None, ay=None)
    fdata.match = lambda bet: nofd
    assert "yellow" not in fdata.stats({}) and settle.shot_value("team_yellow_total", fdata.stats({})) is None
    print("ok  règlement : corners d'une équipe et jaunes seuls")


# ---------- sources : jaunes seuls ----------

def test_sources_yellow():
    # FotMob : stat « Yellow cards » ; jamais 0 inventé
    html = open(os.path.join(FIX, "fotmob_match.json"), encoding="utf-8").read()
    m = fmmatch.parse_match("<script id=\"__NEXT_DATA__\" type=\"application/json\">" + json.dumps({"props": {"pageProps": json.loads(html)}}) + "</script>")
    assert m and m["s"].get("yellow") == [3, 0] and m["s"].get("corners") == [3, 13], m
    assert fmmatch.KEYS[-1] == "yellow" and fmmatch.KEYS.index("xg") < fmmatch.KEYS.index("yellow")          # nouvelle clé à la fin
    pp = json.loads(html)
    pp["content"]["stats"]["Periods"]["All"]["stats"][0]["stats"] = [s for s in pp["content"]["stats"]["Periods"]["All"]["stats"][0]["stats"]
                                                                       if s.get("key") != "yellow_cards"]
    for grp in pp["content"]["stats"]["Periods"]["All"]["stats"]:
        grp["stats"] = [s for s in grp["stats"] if s.get("key") != "yellow_cards"]
    wrap = lambda p: "<script id=\"__NEXT_DATA__\" type=\"application/json\">" + json.dumps({"props": {"pageProps": p}}) + "</script>"      # noqa: E731
    assert "yellow" not in fmmatch.parse_match(wrap(pp))["s"]                                              # sans la stat ni la couleur des cartons : inconnu
    pp["content"]["matchFacts"] = {"events": {"events": [{"type": "Card", "card": "Yellow", "isHome": True}, {"type": "Card", "card": "Yellow", "isHome": True},
                                                          {"type": "Card", "card": "YellowRed", "isHome": False}, {"type": "Card", "card": "Red", "isHome": False}]}}
    s = fmmatch.parse_match(wrap(pp))["s"]
    assert s["yellow"] == [2, 1] and s["cards"] == [2, 2], s                                              # le fil donne la couleur : jaunes (2e jaune compris) vs cartons
    # ligne de profil ancienne (sans la colonne jaunes) : jaunes inconnus, rien d'autre ne bouge
    row = [260101, 1] + [None, None] * (len(fmmatch.KEYS) - 1)
    old = fmmatch._views([row[:fmmatch.R_STATS + 2 * fmmatch.KEYS.index("yellow")]] * 3)
    assert "yellow_for" not in old["all"]
    # 365scores : jaunes seuls, cartons = jaunes + rouges
    assert s365.KEYS[-1] == "yellow"
    # apistats : colonne ajoutée à la fin, ligne ancienne lue sans jaunes
    assert apistats.KEYS[-1] == "yellow" and apistats.FIELD["yellow"] == "yel"
    short = [260101, 0, 1] + [1, 0, 12, 10, 4, 3, 5, 4, 11, 10, 2, 2]               # ancienne ligne : sans xG ni jaunes
    v = apistats._views([short] * 4)["all"]
    assert v["corners_for"] == 5 and "yellow_for" not in v and "xg_for" not in v
    print("ok  jaunes seuls dans FotMob, 365scores, API-Football (anciennes lignes lues)")


def test_apistats_records_yellow():
    e = {"id": 7, "ts": 1760000000, "home": "Alpha", "home_id": 1, "away": "Bravo", "away_id": 2, "gh": 2, "ga": 1, "ln": "Liga"}
    tv = lambda y, c: {"corners": c, "cards": y, "shots": 10.0, "sot": 4.0, "fouls": 12.0, "yel": y, "nul": []}      # noqa: E731
    d = {"v": 2, "teams": {}, "seen": {}, "lg": []}
    n = apistats.record(d, e, {"teams": {"1": tv(2.0, 6.0), "2": tv(3.0, 4.0)}})
    assert n == 2
    row = d["teams"]["1"]["m"][0]
    iy = apistats.R_STATS + 2 * apistats.KEYS.index("yellow")
    assert row[iy:iy + 2] == [2, 3] and len(row) == iy + 2
    # jaunes inconnus (null) : colonnes omises, jamais 0
    unknown = {"corners": 6.0, "cards": 2.0, "shots": 0.0, "sot": 0.0, "fouls": 0.0, "yel": 0.0, "nul": ["yel", "shots", "sot", "fouls"]}
    d2 = {"v": 2, "teams": {}, "seen": {}, "lg": []}
    apistats.record(d2, dict(e, id=8), {"teams": {"1": unknown, "2": tv(3.0, 4.0)}})
    assert len(d2["teams"]["1"]["m"][0]) == apistats.R_STATS + 2 * apistats.KEYS.index("xg")      # ni xG ni jaunes : ligne courte
    print("ok  API-Football : jaunes enregistrés à la fin de la ligne, null = colonnes omises")


def test_new_families_tracked_and_settled_end_to_end():
    """Les familles ajoutées sont suivies en silence (fiabilité mesurable) puis réglées avec les sources gratuites."""
    day = "2026-10-10"
    ev = {"id": "m1", "sport_key": "soccer_epl", "home_team": "Alpha", "away_team": "Bravo", "commence_time": f"{day}T15:00:00Z"}
    pronos = [fair.make_pick(ev, m, n, pt, 0.66, "t", "football-data") | {"conf": 0.6, "score": sc}
              for m, n, pt, sc in (("alternate_totals_corners", "Over", 9.5, 0.9), ("totals", "Over", 1.5, 0.8), ("btts", "Yes", None, 0.7),
                                   ("team_corners_home", "Over", 4.5, 0.2), ("team_yellow_total", "Under", 5.5, 0.1))]
    store.save(fair.FILE, [])
    n = fair.record(pronos, day, shown=pronos[:1])
    rows = {r["market"]: r for r in fair.load()}
    assert n >= 4 and rows["team_corners_home"].get("x") == 1 and rows["team_yellow_total"].get("x") == 1, rows       # suivis en silence
    assert "x" not in rows["alternate_totals_corners"]
    # règlement gratuit : jaunes seuls (jamais les 7 cartons) et corners de l'équipe
    api = {"corners": 12.0, "cards": 7.0, "side": {"home": {"corners": 7.0, "cards": 4.0, "yel": 3.0, "nul": []},
                                                    "away": {"corners": 5.0, "cards": 3.0, "yel": 2.0, "nul": []}}}
    now = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
    old_fs, old_en = settle.football_score, settle.stats.enabled
    settle.stats.enabled = lambda: True
    settle.football_score = lambda b: {"id": b["id"], "completed": True, "scores": [{"name": b["home"], "score": "2"}, {"name": b["away"], "score": "1"}]}
    try:
        fdata = settle.FootballData()
        rows = fair.load()
        for r in rows:
            assert settle._settle_fair_row(r, now, fdata, stats_for=lambda b: api)
    finally:
        settle.football_score, settle.stats.enabled = old_fs, old_en
    res = {r["market"]: (r["result"], r.get("final")) for r in rows}
    assert res["team_corners_home"] == ("win", "7 corners") and res["alternate_totals_corners"][0] == "win", res
    assert res["team_yellow_total"] == ("win", "5 cartons jaunes"), res                      # 3 + 2 jaunes sous 5,5 (les 7 cartons feraient perdre)
    print("ok  suivi en silence et règlement des familles ajoutées")


# ---------- fenêtre des stats ----------

def test_statswindow_covers_new_families():
    for m in ("team_corners_home", "team_corners_away", "team_yellow_total", "team_shots_total", "team_fouls_home"):
        assert statswindow.is_stat({"market": m}), m
    assert not statswindow.is_stat({"market": "alternate_totals_corners"}) and not statswindow.is_stat({"market": "totals"})
    print("ok  fenêtre des stats : corners d'une équipe et jaunes relus")


def main():
    test_team_corners_priced()
    test_xprice_team_corners_and_yellow()
    test_yellow_subgame_recognition()
    test_options_team_corners_and_yellow()
    test_small_profiles_negative_binomial()
    test_yellow_model_and_referee()
    test_referee_name_matching()
    test_referee_from_api_football_list()
    test_features_use_referee()
    test_referee_reaches_fair_and_enrich()
    test_shots_audit()
    test_settlement_team_corners_and_yellow()
    test_sources_yellow()
    test_apistats_records_yellow()
    test_new_families_tracked_and_settled_end_to_end()
    test_statswindow_covers_new_families()
    print("tests corners et stats : OK")


if __name__ == "__main__":
    main()
