"""Voie `saison`, commit A (hors ligne, données fictives) : modèle des matchs de la saison en cours seulement (kelly/season.py) — valeurs attendues calculées à la main,
régression vers la moyenne (poids n/(n+k)), minimum de matchs, dispersion (plancher 0, repli sous 30 matchs de ligue), exclusion des matchs d'avant la saison, lecture
des lignes stockées, une seule source par ligue. Jamais le vrai cache ni le vrai .env. Lancer : python -m tests.test_season"""
import os
import random
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-season-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, season as S, strength  # noqa: E402

ON = date(2026, 10, 14)
SK = "soccer_epl"


def row(team, d, home, **stats):
    return {"team": team, "d": d, "home": home, "s": {k: v for k, v in stats.items()}}


def league(n_teams=8, rounds=4, seed=1, mean_h=1.6, mean_a=1.2, corners=(5.5, 4.5), disp=0.0):
    """Ligue fictive : chaque équipe reçoit `rounds` fois et visite `rounds` fois (matchs de la saison, aout -> octobre), toutes égales (attaque 1)."""
    rnd, obs = random.Random(seed), []
    teams = [f"T{i}" for i in range(n_teams)]

    def draw(mean):
        if disp > 0:                                           # binomiale négative par mélange gamma-Poisson
            mean *= rnd.gammavariate(1 / disp, disp)
        x, p, L = 0, 1.0, pow(2.718281828, -mean)
        while True:
            p *= rnd.random()
            if p <= L:
                return x
            x += 1
    day = date(2026, 8, 5)
    for r in range(rounds):
        for i, h in enumerate(teams):
            a = teams[(i + 1 + r) % n_teams]
            day += timedelta(days=1)
            hg, ag, hc, ac = draw(mean_h), draw(mean_a), draw(corners[0]), draw(corners[1])
            d = day.isoformat()
            obs.append(row(h, d, 1, goals=(hg, ag), corners=(hc, ac)))
            obs.append(row(a, d, 0, goals=(ag, hg), corners=(ac, hc)))
    return obs


def test_hand_values():
    obs = [row("A", "2026-09-01", 1, goals=(2, 0), corners=(6, 3)), row("B", "2026-09-01", 0, goals=(0, 2), corners=(3, 6)),
           row("A", "2026-09-08", 0, goals=(1, 1), corners=(4, 4)), row("B", "2026-09-08", 1, goals=(1, 1), corners=(4, 4))]
    m = S.fit(obs, ON, SK, k=6)
    g = m["stats"]["goals"]
    # côté « domicile » d'un match : pour de l'équipe qui reçoit, contre de celle qui visite (2, 2, 1, 1) ; côté « extérieur » (0, 0, 1, 1)
    assert abs(g["lg_h"] - (2 + 1 + 2 + 1) / 4) < 1e-9 and abs(g["lg_a"] - (0 + 1 + 0 + 1) / 4) < 1e-9, g
    A = g["t"]["A"]
    assert A["nh"] == 1 and A["na"] == 1 and A["n"] == 2
    w = 1 / (1 + 6)
    assert abs(A["ah"] - (w * 2 + (1 - w) * g["lg_h"])) < 1e-9, A                     # attaque à domicile : 1 match de 2 buts, rétréci avec n/(n+k)
    assert abs(A["dh"] - (w * 0 + (1 - w) * g["lg_a"])) < 1e-9, A
    assert abs(A["aa"] - (w * 1 + (1 - w) * g["lg_a"])) < 1e-9 and abs(A["da"] - (w * 1 + (1 - w) * g["lg_h"])) < 1e-9
    print("ok  valeurs à la main : moyennes de ligue domicile/extérieur, attaque et défense rétrécies avec n/(n+k)")


def test_regression_to_mean_and_expectation():
    obs = league(8, 4)
    # une équipe très prolifique : 5 matchs à domicile à 5 buts
    for i in range(5):
        obs.append(row("HOT", f"2026-09-{10 + i:02d}", 1, goals=(5, 1), corners=(9, 3)))
        obs.append(row(f"T{i}", f"2026-09-{10 + i:02d}", 0, goals=(1, 5), corners=(3, 9)))
    m = S.fit(obs, ON, SK, k=6)
    g = m["stats"]["goals"]
    ah = g["t"]["HOT"]["ah"]
    assert g["lg_h"] < ah < 5.0 and abs(ah - (5 / 11 * 5 + 6 / 11 * g["lg_h"])) < 1e-9, (ah, g["lg_h"])      # n = 5 : poids 5/11
    k0 = S.fit(obs, ON, SK, k=0)["stats"]["goals"]["t"]["HOT"]["ah"]
    assert abs(k0 - 5.0) < 1e-9, "k = 0 : aucune régression"
    p = S.predict(m, "HOT", "T7")
    t7 = g["t"]["T7"]
    assert abs(p["lh"] - g["t"]["HOT"]["ah"] * t7["da"] / g["lg_h"]) < 1e-9 and abs(p["la"] - t7["aa"] * g["t"]["HOT"]["dh"] / g["lg_a"]) < 1e-9, p
    assert p["lh"] > g["lg_h"] and p["rho"] == 0.0 and p["lane"] == "saison"
    # pas d'ancrage marché ni demi-vie : le même modèle un jour plus tard sans nouveau match est identique
    again = S.fit(obs, ON + timedelta(days=1), SK, k=6)["stats"]["goals"]["t"]["HOT"]["ah"]
    assert abs(again - ah) < 1e-12, "aucune pondération par l'âge des matchs"
    print("ok  régression vers la moyenne de la ligue (n=5, k=6 : poids 5/11), k=0 sans régression, attente = attaque x défense / moyenne, sans demi-vie")


def test_minimum_matches():
    obs = league(8, 4)
    obs.append(row("NEW", "2026-09-20", 1, goals=(1, 1), corners=(5, 5)))
    obs.append(row("T0", "2026-09-20", 0, goals=(1, 1), corners=(5, 5)))
    m = S.fit(obs, ON, SK)
    assert S.predict(m, "NEW", "T1") is None, "1 match : pas de prévision"
    for i in range(1, 3):
        obs.append(row("NEW", f"2026-09-{20 + i}", i % 2, goals=(1, 1), corners=(5, 5)))
    assert S.predict(S.fit(obs, ON, SK), "NEW", "T1") is None, "3 matchs : pas encore"
    obs.append(row("NEW", "2026-09-30", 0, goals=(1, 2), corners=(5, 5)))
    p = S.predict(S.fit(obs, ON, SK), "NEW", "T1")
    assert p is not None and p["n"][0] == 4 and "corners" in p["stats"]
    assert S.predict(m, "Inconnu FC", "T1") is None and S.predict(None, "T0", "T1") is None
    print("ok  minimum 4 matchs par équipe : 1 et 3 matchs sans prévision, 4 matchs avec ; équipe inconnue : None")


def test_season_boundary():
    pre = [row("A", "2026-06-30", 1, goals=(9, 9), corners=(20, 20)), row("B", "2026-06-30", 0, goals=(9, 9), corners=(20, 20))]
    obs = league(8, 4) + pre
    a = S.fit(obs, ON, SK)["stats"]["goals"]
    b = S.fit(league(8, 4), ON, SK)["stats"]["goals"]
    assert a["lg_h"] == b["lg_h"] and a["t"]["T0"]["n"] == b["t"]["T0"]["n"] and "A" not in a["t"], "un match du 30 juin n'entre jamais dans la saison 2026-27"
    on_jan = date(2027, 2, 1)
    assert S.season_start("soccer_brazil_campeonato", on_jan) == date(2027, 1, 1) and S.season_start(SK, on_jan) == date(2026, 7, 1)
    assert S.season_start(SK, date(2026, 6, 15)) == date(2025, 7, 1)
    for k, want in (("soccer_xb_northern_ireland", date(2026, 7, 1)), ("soccer_league_of_ireland", date(2027, 1, 1)), ("soccer_xb_usl", date(2027, 1, 1)),
                    ("soccer_conmebol_copa_libertadores", date(2027, 1, 1)), ("soccer_usa_mls", date(2027, 1, 1)), ("soccer_germany_bundesliga", date(2026, 7, 1))):
        assert S.season_start(k, on_jan) == want, (k, S.season_start(k, on_jan))
    future = [row("T0", "2026-10-14", 1, goals=(9, 9)), row("T1", "2026-10-15", 1, goals=(9, 9))]
    assert S.fit(league(8, 4) + future, ON, SK)["stats"]["goals"]["lg_h"] == b["lg_h"], "le jour même et après : jamais"
    print("ok  un match daté avant le 1er juillet (ou le jour même / après) n'entre pas ; année civile pour le Brésil")


def test_dispersion():
    pois = S.fit(league(10, 6, seed=3, disp=0.0), ON, SK)["stats"]["corners"]
    over = S.fit(league(10, 6, seed=3, disp=0.30), ON, SK)["stats"]["corners"]
    assert pois["matches"] >= 30 and pois["alpha_src"] == "ligue" and pois["alpha"] >= 0.0, pois["alpha"]
    assert pois["alpha"] < 0.08, ("Poisson : dispersion près de 0", pois["alpha"])
    assert over["alpha"] > pois["alpha"] + 0.1 and 0.12 < over["alpha"] < 0.6, ("dispersion mesurée ~0,3", over["alpha"])
    assert over["atot"] >= 0.0
    # moins de 30 matchs de ligue : le repli du rejeu
    small = S.fit(league(8, 2, seed=2, disp=0.3), ON, SK, fallback={"corners": {"alpha": 0.123, "atot": 0.045}, "kappa": 0.07})
    c = small["stats"]["corners"]
    assert c["matches"] == 16 < C.SEASON_ALPHA_MIN_MATCHES and c["alpha_src"] == "repli" and c["alpha"] == 0.123 and c["atot"] == 0.045, c
    assert small["kappa"] == 0.07, "buts : kappa de repli sous 30 matchs"
    big = S.fit(league(10, 6, seed=3, disp=0.30), ON, SK)
    assert big["kappa"] == strength.kappa_of({"atot": big["stats"]["goals"]["atot"]}) and 0.0 <= big["kappa"] <= C.GOALS_KAPPA_MAX
    print("ok  dispersion : plancher 0 (Poisson), ~0,3 retrouvé sur données surdispersées, repli sous 30 matchs de ligue, kappa des buts de la ligue")


def test_pred_feeds_strength_lines():
    m = S.fit(league(10, 6, seed=5, disp=0.2), ON, SK)
    p = S.predict(m, "T1", "T4")
    assert p
    g = strength.grid(p)
    assert abs(sum(g["1x2"].values()) - 1) < 1e-9 and abs(g["dc"]["1X"] - (g["1x2"]["H"] + g["1x2"]["D"])) < 1e-9
    assert "corners" in g and abs(g["corners"]["mean"] - (p["stats"]["corners"]["h"] + p["stats"]["corners"]["a"])) < 1e-9
    ls = strength.lines(p, p["home"], p["away"])
    fams = {x["market"] for x in ls}
    assert {"h2h", "double_chance", "totals", "btts", "team_totals", "alternate_totals_corners"} <= fams, fams
    assert all(0.0 <= x["p"] <= 1.0 and 0.0 <= x["base"] <= 1.0 for x in ls)
    print("ok  la prévision a la forme de strength.predict : grille et lignes (1N2, double chance, totaux, 2 marquent, corners) lisibles telles quelles")


def test_collect_sources():
    obs8 = league(8, 4)
    only_b = {"football-data": lambda: [], "fotmob": lambda: [o for o in obs8 if o["team"] != "T7"][:6], "365scores": lambda: obs8, "api-football": lambda: [o | {"team": "Z" + o["team"]} for o in obs8]}
    got, src = S.collect(SK, ON, only_b)
    assert src == "365scores" and got is obs8, src                                     # fotmob n'a pas assez d'équipes : la source suivante, UNE seule par ligue
    got, src = S.collect(SK, ON, {"football-data": lambda: obs8, "fotmob": lambda: obs8})
    assert src == "football-data"
    got, src = S.collect(SK, ON, {"football-data": lambda: league(5, 4), "fotmob": lambda: [], "365scores": lambda: [], "api-football": lambda: []})
    assert (got, src) == ([], None), "moins de 8 équipes à 4 matchs : ligue non couverte"
    def boom():
        raise OSError("disque")
    assert S.collect(SK, ON, {"football-data": boom, "fotmob": lambda: obs8})[1] == "fotmob", "une source illisible passe à la suivante"
    m = S.get(SK, ON, {"football-data": lambda: obs8})
    assert m and m["source"] == "football-data" and m["rows"] == len(obs8)
    print("ok  une seule source par ligue (football-data > FotMob > 365scores > API-Football), couverture = 8 équipes à 4 matchs, source illisible ignorée")


def test_stored_rows():
    # FotMob : [jour AAMMJJ, domicile, (pour, contre) pour goals, shots, sot, corners, fouls, cards, throws, offsides, xg, npxg, yellow]
    from kelly.sources import fmmatch
    pair = lambda f, a: [f, a]                                 # noqa: E731
    mk = lambda day, home, goals, corners: [day, home] + pair(*goals) + pair(None, None) * 2 + pair(*corners) + pair(None, None) * 7   # noqa: E731
    doc = {"1": {"nm": "Alpha", "sk": [SK], "m": [mk(260920, 1, (2, 1), (7, 3)), mk(260913, 0, (0, 0), (4, 6)), mk(250501, 1, (9, 9), (9, 9))]},
           "2": {"nm": "Autre ligue", "sk": ["soccer_x"], "m": [mk(260920, 1, (3, 3), (3, 3))]}}
    obs = S._from_team_rows(doc, SK, fmmatch.KEYS, fmmatch.R_STATS)
    assert len(obs) == 3 and {o["team"] for o in obs} == {"Alpha"}
    o = next(o for o in obs if o["d"] == "2026-09-20")
    assert o["home"] == 1 and o["s"]["goals"] == (2, 1) and o["s"]["corners"] == (7, 3) and "shots" not in o["s"], o
    m = S.fit(obs, ON, SK)                                     # le match de mai 2025 est d'une autre saison
    assert m is None or m["stats"]["goals"]["t"]["Alpha"]["n"] == 2
    print("ok  lignes stockées FotMob lues (jour AAMMJJ, domicile, paires pour/contre) ; autre ligue et saison passée écartées")


def test_api_football_league_label():
    from kelly import apistats
    row = lambda day, lg, home, g: [day, lg, home, g[0], g[1]] + [None, None] * (len(apistats.KEYS) - 1)      # noqa: E731
    teams = {}
    for i in range(8):
        league_rows = [row(260901 + j, 0, j % 2, (1, 1)) for j in range(5)]
        cup = [row(260920 + j, 1, 1, (9, 9)) for j in range(2)] if i == 0 else []      # Alpha joue une coupe EN DERNIER : son t["lg"] est celui de la coupe
        old_lg = [row(260301 + j, 2, j % 2, (4, 4)) for j in range(10)] if i == 1 else []     # T1 vient d'une autre division : 10 matchs d'avant la saison, 5 de la nouvelle
        teams[str(i)] = {"nm": "Alpha" if i == 0 else f"T{i}", "lg": "Coupe" if i == 0 else "Premier League", "sk": [SK], "m": old_lg + league_rows + cup}
    doc = {"lg": ["Premier League", "Coupe", "Championship"], "seen": {}, "teams": teams}
    real = apistats.load
    apistats.load = lambda: doc
    try:
        none = lambda: []                                                       # noqa: E731
        obs, src = S.collect(SK, ON, {"football-data": none, "fotmob": none, "365scores": none})
        assert src == "api-football", src
        alpha = [o for o in obs if o["team"] == "Alpha"]
        assert len(alpha) == 5 and all(o["s"]["goals"] == (1, 1) for o in alpha), alpha          # les 2 matchs de coupe (9-9) sont écartés
        t1 = [o for o in obs if o["team"] == "T1"]
        assert len(t1) == 5 and all(o["s"]["goals"] == (1, 1) for o in t1), t1                  # promue : ses 5 matchs de la saison, pas les 10 de l'ancienne division
    finally:
        apistats.load = real
    print("ok  API-Football : la compétition d'une équipe est la plus jouée (jamais la dernière, qui peut être une coupe)")


def main():
    for t in (test_hand_values, test_regression_to_mean_and_expectation, test_minimum_matches, test_season_boundary, test_dispersion,
              test_pred_feeds_strength_lines, test_collect_sources, test_stored_rows, test_api_football_league_label):
        t()
    print("tests voie saison (modèle) : OK")


if __name__ == "__main__":
    main()
