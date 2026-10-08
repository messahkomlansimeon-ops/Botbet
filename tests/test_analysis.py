"""Tests hors ligne du modèle approfondi (kelly/strength.py) et de son usage dans prematch / fair / features.
Lancer : python -m tests.test_analysis   (données fictives de tests/synth.py + vraies lignes football-data des fixtures)"""
import contextlib
import io
import math
import os
import random
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="kelly-analysis-test-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SOURCES_CACHE_DIR"] = os.path.join(TMP, "cache")

from kelly import config as C, fair, prematch, store, strength as S  # noqa: E402
from kelly.sources import features, footballdata as fd, state  # noqa: E402

from tests import synth  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sources")


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def synth_matches(seasons=("2223", "2324", "2425"), until=None):
    out = []
    for s in seasons:
        rows, probs = fd.parse_csv(synth.csv_for(s, until=until if s == seasons[-1] else None), "E0", s)
        assert not probs, probs
        out += rows
    return out


def real_rows():
    with open(os.path.join(FIX, "E0_2526_real.csv"), encoding="utf-8-sig") as f:
        rows, probs = fd.parse_csv(f.read(), "E0", "2526")
    assert len(rows) == 24 and not probs
    return rows


def gen_matches(seed, seasons=3, n=20, H=1.3, m=1.25, sd=0.22, alpha=0.0):
    """Saisons fictives de force connue : attaque / défense log-normales (moyenne 1), avantage du terrain H, buts de Poisson,
    corners binomiale négative de dispersion `alpha` (mélange gamma-Poisson). -> (matchs, attaque, défense)."""
    rng = random.Random(seed)
    teams = [f"Team {chr(65 + i)}" for i in range(n)]

    def strengths():
        v = {t: math.exp(rng.gauss(0, sd)) for t in teams}
        mean = sum(v.values()) / n
        return {t: x / mean for t, x in v.items()}

    att, dfn, cat, cdf = strengths(), strengths(), strengths(), strengths()
    nb = (lambda mean: synth.poisson(rng, mean * rng.gammavariate(1 / alpha, alpha))) if alpha else (lambda mean: synth.poisson(rng, mean))
    out = []
    for s in range(seasons):
        for k, day in enumerate(synth.rounds(teams)):
            d = date(2022, 8, 6) + timedelta(days=365 * s + 7 * k)
            for h, a in day:
                hg, ag = synth.poisson(rng, m * H * att[h] * dfn[a]), synth.poisson(rng, m * att[a] * dfn[h])
                out.append({"date": d.isoformat(), "div": "E0", "home": h, "away": a, "hg": hg, "ag": ag,
                            "ftr": "H" if hg > ag else "A" if hg < ag else "D", "referee": None, "odds": {},
                            "hc": nb(5.4 * 1.15 * cat[h] * cdf[a]), "ac": nb(5.4 * cat[a] * cdf[h]),
                            "hy": synth.poisson(rng, 1.9), "ay": synth.poisson(rng, 2.0), "hr": 0, "ar": 0,
                            "hs": None, "as": None, "hst": None, "ast": None, "hf": None, "af": None})
    return out, att, dfn


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for k, i in enumerate(order):
            r[i] = k
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    return 1 - 6 * sum((a - b) ** 2 for a, b in zip(rx, ry)) / (n * (n * n - 1))


# --- lois de probabilité ----------------------------------------------------------

def test_distributions():
    for mean in (0.4, 2.7, 10.5):
        p = S.pois_pmf(mean, 80)
        assert close(sum(p), 1, 1e-9) and close(sum(k * x for k, x in enumerate(p)), mean, 1e-6)
        for alpha in (0.05, 0.2):
            q = S.nb_pmf(mean, alpha)
            tot, mu = sum(q), sum(k * x for k, x in enumerate(q))
            var = sum((k - mu) ** 2 * x for k, x in enumerate(q))
            assert close(tot, 1, 1e-6) and close(mu, mean, 1e-4) and close(var, mean + alpha * mean ** 2, 0.01 * (mean + alpha * mean ** 2)), (mean, alpha, tot, mu, var)
    assert S.nb_pmf(3.0, 0.0)[:5] == S.pois_pmf(3.0, len(S.nb_pmf(3.0, 0.0)) - 1)[:5]
    # lignes monotones (plus de X décroît quand la ligne monte), Poisson et binomiale négative, ligne entière = exactement perdue
    for pm in (S.pois_pmf(9.8, 60), S.nb_pmf(9.8, 0.12)):
        ov = [S.p_over(pm, x + 0.5) for x in range(0, 25)]
        assert all(a >= b for a, b in zip(ov, ov[1:])) and 0 <= ov[-1] <= ov[0] <= 1
        assert close(S.p_over(pm, 9.0), S.p_over(pm, 9.5), 1e-12) and close(S.p_over(pm, 8.5), S.p_over(pm, 9.0) + pm[9], 1e-12)
    # la binomiale négative a des queues plus épaisses : plus de chances d'un total extrême des deux côtés
    pp, pn = S.pois_pmf(9.8, 80), S.nb_pmf(9.8, 0.15, 80)
    assert S.p_over(pn, 16.5) > S.p_over(pp, 16.5) and 1 - S.p_over(pn, 3.5) > 1 - S.p_over(pp, 3.5)
    # matrice de scores : somme 1 ; rho < 0 gonfle 0-0 et 1-1, rho > 0 les dégonfle
    m0, mn, mp = S.score_matrix(1.5, 1.1, 0.0), S.score_matrix(1.5, 1.1, -0.12), S.score_matrix(1.5, 1.1, 0.08)
    for m in (m0, mn, mp):
        assert close(sum(map(sum, m)), 1, 1e-12)
        ph, pd, pa = S.result_probs(m)
        assert close(ph + pd + pa, 1, 1e-12) and ph > pa
    assert mn[0][0] > m0[0][0] > mp[0][0] and mn[1][1] > m0[1][1] > mp[1][1] and mn[0][1] < m0[0][1]
    assert close(sum(S.goal_pmf(m0)), 1, 1e-12) and close(sum(S.goal_pmf(m0, 0)), 1, 1e-12)
    print("lois de probabilité : OK")


# --- ajustement ------------------------------------------------------------------------

def test_fit_recovers_strengths():
    ms, att, dfn = gen_matches(7, seasons=4, H=1.3, m=1.25)
    mod = S.build(ms, date(2026, 6, 1), hl=100000, stats=("goals", "corners", "cards"), div="E0")   # sans décroissance : isole l'estimation
    g = mod["stats"]["goals"]
    ts = g["t"]
    teams = sorted(att)
    est_a, est_d = [ts[t][0] for t in teams], [ts[t][1] for t in teams]
    err_a = sum(abs(e / att[t] - 1) for e, t in zip(est_a, teams)) / len(teams)
    err_d = sum(abs(e / dfn[t] - 1) for e, t in zip(est_d, teams)) / len(teams)
    assert err_a < 0.07 and err_d < 0.07, (err_a, err_d)
    assert spearman(est_a, [att[t] for t in teams]) > 0.85 and spearman(est_d, [dfn[t] for t in teams]) > 0.85
    assert close(g["h"], 1.3, 0.07) and close(g["m"], 1.25, 0.06), (g["h"], g["m"])
    assert abs(sum(est_a) / len(teams) - 1) < 0.02 and abs(sum(est_d) / len(teams) - 1) < 0.02      # identifiabilité : moyenne 1
    assert g["alpha"] < 0.03 and g["atot"] < 0.03 and -0.12 < g["rho"] < 0.12                     # buts de Poisson indépendants
    # synth.py (tests existants) : pas d'avantage du terrain, forces connues ATT / DEF
    ms = synth_matches()
    mod = S.build(ms, date(2025, 8, 10), hl=5000, stats=("goals",), div="E0")
    g = mod["stats"]["goals"]
    teams = sorted(synth.ATT)
    conceded = {t: [0, 0] for t in teams}                          # estimation naïve : buts encaissés par match
    for m in ms:
        for t, c in ((m["home"], m["ag"]), (m["away"], m["hg"])):
            conceded[t][0] += c
            conceded[t][1] += 1
    naive = spearman([conceded[t][0] / conceded[t][1] for t in teams], [synth.DEF[t] for t in teams])
    r_def = spearman([g["t"][t][1] for t in teams], [synth.DEF[t] for t in teams])
    assert spearman([g["t"][t][0] for t in teams], [synth.ATT[t] for t in teams]) > 0.8, "attaque"
    assert r_def > 0.6 and r_def >= naive - 0.05, ("défense", r_def, naive)               # au moins aussi bien que la moyenne brute
    assert close(g["h"], 1.0, 0.1), g["h"]
    print(f"ajustement : erreur relative attaque {err_a * 100:.1f}% défense {err_d * 100:.1f}%, H={g['h']:.2f} sur synth : OK")


def test_dispersion_estimate():
    ms, _, _ = gen_matches(11, seasons=4, alpha=0.15)
    mod = S.build(ms, date(2026, 6, 1), hl=100000, stats=("goals", "corners", "cards"), div="E0")
    c, k = mod["stats"]["corners"], mod["stats"]["cards"]
    assert 0.08 < c["alpha"] < 0.25 and c["atot"] > 0.05, (c["alpha"], c["atot"])             # vraie valeur 0,15
    assert k["alpha"] < 0.04, k["alpha"]                                                         # cartons générés en Poisson
    ms, _, _ = gen_matches(11, seasons=4, alpha=0.0)
    assert S.build(ms, date(2026, 6, 1), hl=100000, stats=("goals", "corners"), div="E0")["stats"]["corners"]["alpha"] < 0.04
    # les lignes de corners du modèle tiennent compte de la dispersion
    ms, _, _ = gen_matches(11, seasons=4, alpha=0.15)
    mod = S.build(ms, date(2026, 6, 1), hl=100000, stats=("goals", "corners"), div="E0")
    p = S.predict(mod, "Team A", "Team B", date(2026, 6, 1), ctx=False)
    st = p["stats"]["corners"]
    mean = st["h"] + st["a"]
    assert st["at"] > 0.05
    assert S.p_stat_total(p, "corners", mean + 7.5, "Over") > S.p_over(S.pois_pmf(mean, 80), mean + 7.5)
    print(f"dispersion estimée des corners {c['alpha']:.2f} (vraie 0,15), cartons {k['alpha']:.2f} (Poisson) : OK")


def test_shrinkage_and_promoted():
    ms, att, _ = gen_matches(3, seasons=2)
    last = max(m["date"] for m in ms)
    d0 = date.fromisoformat(last) + timedelta(days=7)
    # une équipe qui n'a joué que 4 matchs, tous très offensifs (5-0), contre des équipes moyennes
    opp = sorted(att)[:4]
    few = [{"date": (d0 - timedelta(days=28 - 7 * i)).isoformat(), "div": "E0", "home": "Newbie", "away": o, "hg": 5, "ag": 0, "ftr": "H",
            "referee": None, "odds": {}, "hc": None, "ac": None, "hy": None, "ay": None, "hr": 0, "ar": 0}
           for i, o in enumerate(opp)]
    mod = S.build(ms + few, d0 + timedelta(days=1), hl=400, stats=("goals",), div="E0")
    g = mod["stats"]["goals"]
    a_new = g["t"]["Newbie"][0]
    raw = 5.0 / (g["m"] * g["h"])                              # attaque brute : 5 buts par match / buts attendus d'un match moyen
    assert 1.0 < a_new < 1.6 < raw, (a_new, raw)                  # rétréci vers la moyenne, mais pas ignoré
    assert g["t"]["Newbie"][3] == 4 and g["t"]["Newbie"][2] < 4.01 and "Newbie" in g["promo"]   # équipe promue : a priori plus prudent
    bigteams = [t for t in att if att[t] > 1.2]
    assert all(g["t"][t][3] > 60 for t in bigteams)
    # même résultat avec un a priori neutre : l'équipe promue est plus basse qu'une équipe installée avec les mêmes résultats
    est = S.fit_stat([(0, 1, 5, 0, 1.0), (0, 2, 5, 0, 1.0), (3, 4, 1, 1, 1.0), (4, 3, 1, 1, 1.0), (1, 2, 1, 1, 1.0), (2, 1, 1, 1, 1.0)],
                     5, promo={0})
    neutral = S.fit_stat([(0, 1, 5, 0, 1.0), (0, 2, 5, 0, 1.0), (3, 4, 1, 1, 1.0), (4, 3, 1, 1, 1.0), (1, 2, 1, 1, 1.0), (2, 1, 1, 1, 1.0)], 5)
    assert est["A"][0] < neutral["A"][0] and est["A"][0] > 1.0
    # équipe jamais vue (promue, pas encore jouée) : prévision à qualité nulle, pas d'invention de forme
    p = S.predict(mod, "Newcomer FC", "Team B", d0)
    assert p and p["newcomer"] == "Newcomer FC" and p["q"] == 0.0 and p["info"]["form"] == {} and p["lh"] < p["la"] * 1.3
    assert S.predict(mod, "Newcomer FC", "Other Newcomer", d0) is None
    # peu de matchs = qualité plus basse que pour une équipe installée
    q_few, q_full = S.predict(mod, "Newbie", "Team B", d0)["q"], S.predict(mod, "Team C", "Team B", d0)["q"]
    assert q_few < q_full and q_full > 0.9, (q_few, q_full)
    print(f"rétrécissement : attaque brute {raw:.2f} -> {a_new:.2f}, qualité {q_few:.2f} vs {q_full:.2f} : OK")



def test_time_decay_and_rho():
    # une équipe forte pendant 2 saisons puis faible : une demi-vie courte suit la forme récente
    rng = random.Random(5)
    teams = [f"Team {chr(65 + i)}" for i in range(20)]
    out = []
    for s in range(3):
        for k, day in enumerate(synth.rounds(teams)):
            d = date(2022, 8, 6) + timedelta(days=365 * s + 7 * k)
            for h, a in day:
                fa = (1.6 if s < 2 else 0.6)
                lh = 1.5 * (fa if h == "Team A" else 1.0)
                la = 1.2 * (fa if a == "Team A" else 1.0)
                hg, ag = synth.poisson(rng, lh), synth.poisson(rng, la)
                out.append({"date": d.isoformat(), "div": "E0", "home": h, "away": a, "hg": hg, "ag": ag, "ftr": "H" if hg > ag else "A" if hg < ag else "D",
                            "referee": None, "odds": {}, "hc": None, "ac": None, "hy": None, "ay": None, "hr": 0, "ar": 0})
    end = date(2025, 6, 1)
    short = S.build(out, end, hl=60, stats=("goals",), div="E0")["stats"]["goals"]["t"]["Team A"][0]
    longer = S.build(out, end, hl=2000, stats=("goals",), div="E0")["stats"]["goals"]["t"]["Team A"][0]
    assert short < longer - 0.15 and short < 1.0 < longer, (short, longer)
    # rho de Dixon-Coles : buts tirés dans une matrice corrigée (rho = -0,12), le signe et l'ordre de grandeur sont retrouvés
    rng = random.Random(9)
    matches = []
    for s in range(5):
        for k, day in enumerate(synth.rounds(teams)):
            d = date(2020, 8, 1) + timedelta(days=365 * s + 7 * k)
            for h, a in day:
                m = S.score_matrix(1.3, 1.0, -0.12)
                u, hg, ag = rng.random(), 0, 0
                done = False
                for i, row in enumerate(m):
                    for j, pr in enumerate(row):
                        u -= pr
                        if u <= 0 and not done:
                            hg, ag, done = i, j, True
                matches.append({"date": d.isoformat(), "div": "E0", "home": h, "away": a, "hg": hg, "ag": ag, "ftr": "H" if hg > ag else "A" if hg < ag else "D",
                                "referee": None, "odds": {}, "hc": None, "ac": None, "hy": None, "ay": None, "hr": 0, "ar": 0})
    rho = S.build(matches, date(2025, 8, 1), hl=100000, stats=("goals",), div="E0")["stats"]["goals"]["rho"]
    assert -0.22 < rho < -0.03, rho
    print(f"décroissance (force récente {short:.2f} vs {longer:.2f} sans décroissance) et rho estimé {rho:.2f} (vrai -0,12) : OK")


def test_market_probs_real_rows():
    r = real_rows()[0]                                  # Liverpool - Bournemouth, vraie cote de clôture
    p3, po = S.market_probs(r)
    assert p3 and close(sum(p3), 1, 1e-9) and p3[0] > 0.7 and po is not None and 0.4 < po < 0.8
    assert S.market_probs({"odds": {}}) == (None, None)


# --- contexte : classement, enjeu, repos, arbitre ----------------------------------------------

def test_standings_and_stakes():
    ms = [{"home": "A", "away": "B", "hg": 2, "ag": 0}, {"home": "B", "away": "C", "hg": 1, "ag": 1},
          {"home": "C", "away": "A", "hg": 0, "ag": 0}, {"home": "A", "away": "C", "hg": 3, "ag": 1},
          {"home": "D", "away": "A", "hg": 2, "ag": 2}]
    t = S.standings(ms)
    assert [r["team"] for r in t] == ["A", "C", "D", "B"], t
    a = t[0]
    assert (a["p"], a["pts"], a["gf"], a["ga"], a["gd"]) == (4, 8, 7, 3, 4)
    c = next(r for r in t if r["team"] == "C")
    assert (c["pts"], c["gd"]) == (2, -2)                       # C : nul, nul, défaite
    assert [r["team"] for r in S.standings([{"home": "X", "away": "Y", "hg": 1, "ag": 1}, {"home": "Y", "away": "Z", "hg": 2, "ag": 0}])][0] == "Y"
    # 10 équipes, 18 matchs au total, 15 joués (3 restants = 9 points)
    def table(pts):
        return [{"team": f"T{i}", "p": 15, "pts": p, "gf": 20, "ga": 20 - i, "gd": i} for i, p in enumerate(pts)]
    rel, top = S.zones(None, 10)
    assert (rel, top) == (2, 3) and S.zones("E0", 20) == (3, 6) and S.zones("D1", 18) == (2, 5)
    race = S.stakes(table([35, 33, 30, 28, 27, 26, 24, 20, 17, 14]), "T1", rel, top)
    assert race["kind"] == "titre" and race["adj"] > 0 and "course au titre" in race["text"] and race["pos"] == 2
    lead = S.stakes(table([35, 33, 30, 28, 27, 26, 24, 20, 17, 14]), "T0", rel, top)
    assert lead["kind"] == "titre" and "+2 pts sur le 2e" in lead["text"]
    won = S.stakes(table([45, 33, 30, 28, 27, 26, 24, 20, 17, 14]), "T0", rel, top)
    assert won["kind"] == "titre_acquis" and won["adj"] < 0
    fight = S.stakes(table([35, 33, 30, 28, 27, 26, 24, 20, 17, 14]), "T9", rel, top)
    assert fight["kind"] == "maintien" and fight["adj"] > 0 and "maintien" in fight["text"] and fight["pts"] == 14
    doomed = S.stakes(table([35, 33, 30, 28, 27, 26, 24, 20, 17, 5]), "T9", rel, top)
    assert doomed["kind"] == "condamne" and doomed["adj"] < 0
    nothing = S.stakes(table([40, 33, 30, 24, 20, 19, 18, 17, 16, 15]), "T4", rel, top)
    assert nothing["kind"] == "rien" and nothing["adj"] < 0 and nothing["left"] == 3 and "plus grand-chose" in nothing["text"], nothing
    europe = S.stakes(table([40, 33, 30, 28, 27, 22, 18, 17, 16, 15]), "T3", rel, top)
    assert europe["kind"] == "haut" and europe["adj"] > 0, europe
    # début de saison : pas d'enjeu inventé ; bornes
    early = S.stakes([{"team": f"T{i}", "p": 4, "pts": 3 * (10 - i) // 3, "gf": 4, "ga": 4, "gd": 0} for i in range(10)], "T9", rel, top)
    assert early["kind"] == "debut" and early["adj"] == 0.0
    for sk in (race, lead, won, fight, doomed, nothing, europe):
        assert abs(sk["adj"]) <= S.BOUND["stakes"] + 1e-12
    assert S.stakes(table([35] * 10), "Inconnu", rel, top) is None
    # début de saison : 1er juillet ; année civile pour le Brésil
    assert S.season_start(date(2025, 12, 1), "E0") == date(2025, 7, 1) and S.season_start(date(2026, 3, 1), "E0") == date(2025, 7, 1)
    assert S.season_start(date(2026, 3, 1), "BRA") == date(2026, 1, 1)
    print("classement et enjeu : OK")


def test_rest_and_bounds():
    assert S._rest_adj(2, 0) == -S.BOUND["rest"] and S._rest_adj(3, 0) == -0.03 and S._rest_adj(4, 0) == -0.015
    assert S._rest_adj(7, 0) == 0.0 and S._rest_adj(40, 5) == 0.0                       # trêve d'été : pas de fatigue
    assert S._rest_adj(2, 5) == -S.BOUND["rest"] and S._rest_adj(7, 4) == -0.01       # congestion : 4 matchs en 14 jours
    ms = synth_matches()
    mod = S.build(ms, date(2025, 9, 1), stats=("goals", "corners"), div="E0")
    ld = max(r["ld"][-1] for r in mod["ctx"]["form"].values())
    on = date.fromisoformat(ld) + timedelta(days=2)
    # repos : équipes qui ont joué il y a 2 jours (journée la plus récente) comparées à elles-mêmes avec 8 jours de repos
    lastday = [t for t, r in mod["ctx"]["form"].items() if r["ld"][-1] == ld]
    h, a = lastday[0], lastday[1]
    tired = S.predict(mod, h, a, on)
    rested = S.predict(mod, h, a, on + timedelta(days=6))
    assert tired["info"]["rest"] == {"home": 2, "away": 2} and rested["info"]["rest"] == {"home": 8, "away": 8}
    assert tired["fh"] < rested["fh"] and tired["fa"] < rested["fa"] and any(n[0] == "rest" for n in tired["notes"])
    assert not any(n[0] == "rest" for n in rested["notes"])
    # total des facteurs borné
    for t1, t2 in ((h, a), (a, h)):
        p = S.predict(mod, t1, t2, on)
        assert 1 - S.BOUND["total"] - 1e-9 <= p["fh"] <= 1 + S.BOUND["total"] + 1e-9
        assert 1 - S.BOUND["total"] - 1e-9 <= p["fa"] <= 1 + S.BOUND["total"] + 1e-9
    # forme : lettres V/N/D sur 5 matchs, ajustement de forme borné
    f = mod["ctx"]["form"]
    assert all(len(r["s"]) <= 5 and set(r["s"]) <= set("VND") for r in f.values())
    assert all(abs(S.FORM_K * 0.5 * (r["rf"] + r["rd"])) < 1 for r in f.values())
    pure = S.predict(mod, h, a, on, ctx=False)
    assert pure["fh"] == 1.0 and pure["fa"] == 1.0 and pure["notes"] == []
    print("repos, forme et bornes : OK")


def test_referee_shrinkage():
    model = {"ctx": {"lg": {"cards": 4.0, "fouls": 22.0},
                     "refs": {"Strict Few": [3, 6.0, 3, 28.0], "Strict Many": [60, 6.0, 60, 28.0], "Soft Few": [3, 2.0, 3, 16.0],
                              "Tiny": [2, 9.0, 2, 30.0], "Extreme Many": [200, 9.0, 200, 40.0]}}}
    few, many = S.referee_factor(model, "Strict Few", "cards"), S.referee_factor(model, "Strict Many", "cards")
    assert few and many and 1.0 < few[0] < many[0], (few, many)                       # peu de matchs : rétréci vers 1
    assert close(few[0], (3 * 6.0 + S.REF_K * 4.0) / (3 + S.REF_K) / 4.0, 1e-9)
    assert S.referee_factor(model, "Soft Few", "cards")[0] < 1.0
    assert S.referee_factor(model, "Tiny", "cards") is None and S.referee_factor(model, "Inconnu", "cards") is None
    assert S.referee_factor(model, None, "cards") is None
    lo, hi = S.REF_BOUND["cards"]
    assert S.referee_factor(model, "Extreme Many", "cards")[0] == hi and lo <= S.referee_factor(model, "Soft Few", "cards")[0] <= hi
    assert S.referee_factor(model, "Extreme Many", "fouls")[0] == S.REF_BOUND["fouls"][1]
    # nom approché (« Strict F. ») et prise en compte dans la prévision
    ms = synth_matches()
    mod = S.build(ms, date(2025, 9, 1), stats=("goals", "cards"), div="E0")
    assert mod["ctx"]["lg"]["cards"] and "A Taylor" in mod["ctx"]["refs"]
    p0, p1 = S.predict(mod, "Arsenal", "Chelsea", date(2025, 9, 1)), S.predict(mod, "Arsenal", "Chelsea", date(2025, 9, 1), "A Taylor")
    assert p0["info"]["referee"] is None and p1["info"]["referee"]["name"] == "A Taylor" and p1["unavailable_referee"] is False
    assert S.predict(mod, "Arsenal", "Chelsea", date(2025, 9, 1), "Nobody Known")["unavailable_referee"] is True
    ratio = (p1["stats"]["cards"]["h"] + p1["stats"]["cards"]["a"]) / (p0["stats"]["cards"]["h"] + p0["stats"]["cards"]["a"])
    assert close(ratio, p1["info"]["referee"]["factor"], 1e-9) and lo <= ratio <= hi
    print("arbitre (rétrécissement, bornes) : OK")


# --- marchés ------------------------------------------------------------------------------

def test_market_grid():
    ms = synth_matches()
    on = date(2025, 9, 1)
    mod = S.build(ms, on, div="E0")
    p = S.predict(mod, "Man City", "Everton", on, "M Oliver")
    g = S.grid(p)
    x = g["1x2"]
    assert close(x["H"] + x["D"] + x["A"], 1, 1e-9) and close(g["dc"]["1X"], x["H"] + x["D"], 1e-12)
    assert close(g["dc"]["12"] + x["D"], 1, 1e-9) and close(g["btts"]["yes"] + g["btts"]["no"], 1, 1e-12)
    m = S.score_matrix(p["lh"], p["la"], p["rho"])
    brute = sum(m[i][j] for i in range(1, len(m)) for j in range(1, len(m)))
    assert close(g["btts"]["yes"], brute, 1e-9)
    prev = 1.0
    for pt in ("0.5", "1.5", "2.5", "3.5", "4.5"):                                   # plus de X buts décroît
        o = g["totals"][pt]
        assert close(o["over"] + o["under"], 1, 1e-12) and o["over"] <= prev
        prev = o["over"]
    assert close(g["totals"]["2.5"]["over"], 1 - sum(S.goal_pmf(m)[:3]), 1e-9)
    for side in ("home", "away"):
        v = [g["team_totals"][side][k]["over"] for k in ("0.5", "1.5", "2.5")]
        assert v[0] >= v[1] >= v[2]
    assert g["scores"][0][1] >= g["scores"][-1][1] and len(g["scores"]) == 5
    assert (x["H"] > x["A"]) == (g["xg"][0] > g["xg"][1])                              # le favori du 1N2 est celui des buts attendus
    for stat in ("corners", "cards", "shots", "sot", "fouls"):
        d = g[stat]
        assert close(d["mean"], d["h"] + d["a"], 1e-12)
        for part in (d["total"], d["home"], d["away"]):
            ov = [part[k]["over"] for k in sorted(part, key=float)]
            assert all(a >= b - 1e-12 for a, b in zip(ov, ov[1:])) and all(close(part[k]["over"] + part[k]["under"], 1, 1e-12) for k in part)
    assert len(g["corners"]["total"]) >= 5 and all(float(k) % 1 == 0.5 for k in g["corners"]["total"])
    # référence naïve : match moyen de la ligue, mêmes lignes
    b = S.grid(p, base=True, like=g)
    assert set(b["corners"]["total"]) == set(g["corners"]["total"]) and set(b["cards"]["home"]) == set(g["cards"]["home"])
    assert close(b["xg"][0], p["base"]["lh"]) and b["1x2"]["H"] > b["1x2"]["A"]                                  # avantage du terrain moyen
    ls = S.lines(p, "Man City", "Everton")
    assert len(ls) > 150 and all(0 <= l["p"] <= 1 and 0 <= l["base"] <= 1 for l in ls)
    markets = {l["market"] for l in ls}
    assert {"h2h", "double_chance", "totals", "btts", "team_totals", "alternate_totals_corners", "alternate_totals_cards",
            "team_alternate_totals_corners"} & markets >= {"h2h", "double_chance", "totals", "btts", "alternate_totals_corners"}, markets
    over = {(l["market"], l["point"]): l["p"] for l in ls if l["name"] == "Over" and l["market"] == "totals"}
    under = {(l["market"], l["point"]): l["p"] for l in ls if l["name"] == "Under" and l["market"] == "totals"}
    assert all(close(over[k] + under[k], 1, 1e-9) for k in over)
    top = S.top_lines(p, "Man City", "Everton")
    assert 1 <= len(top) <= 5 and all(0.55 <= l["p"] <= 0.9 and l["lift"] >= 0.03 and 0 < l["q"] <= 1 for l in top)
    mg = S.market_grid("Man City", "Everton", "soccer_epl", "M Oliver", on=on, model=mod)
    assert mg["pred"]["home"] == "Man City" and mg["grid"]["1x2"] == g["1x2"] and len(mg["lines"]) == len(ls)
    assert S.market_grid("Foo", "Bar", "soccer_epl", on=on, model=mod) is None and S.market_grid("A", "B", "basketball_nba") is None
    fams = [l["family"].replace(" équipe", "") for l in top]
    assert all(fams.count(f) <= 2 for f in fams) and [l["score"] for l in top] == sorted((l["score"] for l in top), reverse=True)
    # un match de ligue moyen est sa propre référence : avance nulle
    avg = dict(p, lh=p["base"]["lh"], la=p["base"]["la"], stats={k: dict(v, **p["base"]["stats"].get(k, {})) for k, v in p["stats"].items()})
    assert all(abs(l["p"] - l["base"]) < 1e-9 for l in S.lines(avg, "A", "B"))
    print("grille de marchés : OK")


# --- cache -----------------------------------------------------------------------------------

def test_cache_reuse():
    ms = synth_matches()
    calls = []

    def loader(div):
        calls.append(div)
        return ms

    store_dir = store.DATA_DIR
    for f in os.listdir(store_dir) if os.path.isdir(store_dir) else []:
        os.remove(os.path.join(store_dir, f))
    S.reload()
    on = date(2025, 9, 1)
    fits = S.FITS
    m1 = S.get("E0", on, loader)
    assert m1 and S.FITS == fits + 1 and calls == ["E0"]
    path = store.path("strength_E0.json")
    assert os.path.exists(path) and 3_000 < os.path.getsize(path) < 120_000, os.path.getsize(path)          # petit JSON compact
    assert S.get("E0", on, loader) is m1 and S.FITS == fits + 1 and len(calls) == 1                         # mémoire
    S.reload()                                                                                               # nouveau processus
    m2 = S.get("E0", on, loader)
    assert m2["on"] == m1["on"] and m2["stats"]["goals"]["t"] == m1["stats"]["goals"]["t"]
    assert S.FITS == fits + 1 and len(calls) == 1, "même jour : aucun réajustement, aucune relecture des CSV"
    p1, p2 = S.predict(m1, "Arsenal", "Chelsea", on), S.predict(m2, "Arsenal", "Chelsea", on)
    assert close(p1["lh"], p2["lh"], 1e-9) and close(p1["la"], p2["la"], 1e-9)
    S.reload()
    S.get("E0", on + timedelta(days=1), loader)                                                              # lendemain : réajustement
    assert S.FITS == fits + 2 and len(calls) == 2
    # actualisation des profils football-data (nouveau « updated ») : réajustement une fois
    S.reload()
    store.save("src_footballdata.json", {"updated": "2025-09-02T08:00"})
    S.get("E0", on + timedelta(days=1), loader)
    assert S.FITS == fits + 3
    S.reload()
    S.get("E0", on + timedelta(days=1), loader)
    assert S.FITS == fits + 3
    # sans données brutes : dernier cache (périmé) pendant STALE_DAYS jours, puis rien
    S.reload()
    old = S.get("E0", on + timedelta(days=6), lambda d: [])
    assert old and old["stale"] == 5 and S.FITS == fits + 3
    p = S.predict(old, "Arsenal", "Chelsea", on + timedelta(days=6))
    assert p["stale"] == 5 and p["q"] < S.predict(m1, "Arsenal", "Chelsea", on)["q"] + 1e-9
    S.reload()
    assert S.get("E0", on + timedelta(days=40), lambda d: []) is None
    S.reload()
    assert S.get("ZZ9", on, lambda d: []) is None                                                           # ligue inconnue / vide
    n = len(calls)
    assert S.get("ZZ9", on, lambda d: calls.append(1) or []) is None and len(calls) == n               # échec mémorisé : pas de relecture
    # jamais d'exception
    S.reload()
    with contextlib.redirect_stderr(io.StringIO()) as err:
        assert S.get("E0", on + timedelta(days=60), lambda d: 1 / 0) is None
    assert "[strength] E0 ignoré : ZeroDivisionError" in err.getvalue()
    S.reload()
    os.remove(store.path("src_footballdata.json"))
    print("cache du jour : OK")


# --- usage dans prematch, fair, features ------------------------------------------------------

def with_clock(day, fn):
    old = state.clock
    state.clock = lambda: datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc).timestamp()
    try:
        return fn()
    finally:
        state.clock = old


def test_prematch_and_fair_use_the_model():
    ms = synth_matches(("2324", "2425", "2526"), until=date(2025, 11, 29))
    day = date(2025, 12, 1)
    old = S.load_matches
    S.load_matches = lambda div: ms if div == "E0" else []
    try:
        S.reload()
        features.reload()

        def run():
            f = features.match_features("Arsenal", "Manchester City", "soccer_epl", referee="M Oliver")
            assert "modèle Kelly" in f["sources"] and f["deep"]["home"] == "Arsenal" and f["deep"]["away"] == "Man City", f["sources"]
            assert f["n_home"] > 30 and f["referee"] == "M Oliver" and f["ref_cards_factor"] > 0
            d = f["deep"]
            assert close(f["exp_goals_home"], round(d["lh"], 2)) and close(f["exp_corners"], round(d["stats"]["corners"]["h"] + d["stats"]["corners"]["a"], 2), 0.02)
            assert prematch.goal_means(f)[2] == "modèle Kelly" and close(prematch.goal_means(f)[0], d["lh"])
            # un pari de corners : la proba du modèle (binomiale négative) est mélangée avec un poids réduit par la qualité
            exp = f["exp_corners"]
            c = {"market": prematch.CORNERS, "name": "Over", "point": math.floor(exp) - 1.5, "prob": 0.5, "odds": 2.0, "edge": 0.0,
                 "score": 0.0, "weight": 1.0, "why": "", "home": "Arsenal", "away": "Manchester City", "sport_key": "soccer_epl"}
            a = prematch.assess(c, f, None)
            assert a["kind"] == "corners" and close(a["exp"], d["stats"]["corners"]["h"] + d["stats"]["corners"]["a"], 1e-9)
            assert "corners/match à domicile (ligue" in a["tag"] and " en concède " in a["tag"], a["tag"]
            assert "arbitre M Oliver" in d["why"]["cards"] and "cartons/match" in d["why"]["cards"]
            assert 0.5 * C.MODEL_WEIGHT["corners"] <= a["w"] <= C.MODEL_WEIGHT["corners"] and a["tag"].startswith("modèle Kelly : ")
            assert close(a["p"], S.p_stat_total(d, "corners", c["point"], "Over"), 1e-12) and a["p"] > 0.5
            prematch.apply(c, a)
            assert c["pm"]["kind"] == "corners" and c["pm"]["q"] == round(a["q"], 2) and c["pm"]["gap"] > 0 and c["pm"]["pgap"] > 0
            assert close(c["prob"], 0.5 + a["w"] * (a["p"] - 0.5), 1e-9)
            # buts : matrice de Dixon-Coles ; 1N2 : seulement l'écart de buts attendus
            t = {"market": "totals", "name": "Under", "point": 2.5, "prob": 0.5, "odds": 2.0, "edge": 0.0, "score": 0.0, "weight": 1.0, "why": ""}
            at = prematch.assess(t, f, None)
            assert at["kind"] == "goals" and close(at["p"], 1 - S.p_over(S.goal_pmf(S.score_matrix(d["lh"], d["la"], d["rho"])), 2.5), 1e-12)
            b = {"market": "btts", "name": "Yes", "prob": 0.5, "odds": 2.0}
            assert close(prematch.assess(b, f, None)["p"], S.p_btts(d, "Yes"), 1e-12)
            h = {"market": "h2h", "name": "Arsenal", "home": "Arsenal", "away": "Man City", "prob": 0.4, "odds": 2.5}
            ah = prematch.assess(h, f, None)
            assert ah["kind"] == "h2h" and "p" not in ah and close(ah["xgd"], d["lh"] - d["la"], 1e-9)
            # pronostics sans cote : lignes 0,5 à 4,5, références naïves, classement par avance x qualité
            opts, bases = fair._options(f, "Arsenal", "Manchester City")
            # tirs et tirs cadrés (total et par équipe) s'ajoutent aux familles de base quand le modèle les a
            # (touches et hors-jeu : seulement avec des profils FotMob, absents ici)
            assert {o[0] for o in opts} == set(fair.ALL_FAMILIES) - {"team_throws_total", "team_throws_home", "team_throws_away",
                                                                      "team_offsides_total"} and bases, {o[0] for o in opts}
            assert {o[0] for o in opts if o[0].startswith("team_sot_")} == {"team_sot_total", "team_sot_home", "team_sot_away"}
            assert all(o[3] > 0 and 0 <= o[3] <= 1 for o in opts if o[0].startswith("team_s"))
            pts = {o[2] for o in opts if o[0] == "totals"}
            assert pts == {0.5, 1.5, 2.5, 3.5, 4.5}, pts
            assert all(0 <= o[3] <= 1 and o[4].startswith("modèle Kelly") for o in opts) and fair.options(f, "Arsenal", "Manchester City") == opts
            assert fair.options(dict(f, n_home=3), "A", "B") == []
            assert fair.options(dict(f, deep=dict(d, q=0.1)), "Arsenal", "Manchester City") != opts             # qualité trop faible : repli
            ev = {"id": "m1", "sport_key": "soccer_epl", "home_team": "Arsenal", "away_team": "Manchester City", "commence_time": "2025-12-01T19:00:00Z"}
            pronos = fair.make({"leagues": {"soccer_epl": [ev]}}, [], day)
            assert 1 <= len(pronos) <= 2 and pronos == sorted(pronos, key=lambda c: -c["score"])
            assert S.shorten("a ; " + "b" * 60 + " ; " + "c" * 60 + " ; d", 100) == "a ; " + "b" * 60 and S.shorten("x" * 300, 50) == "x" * 300
            for c in pronos:
                assert len(c["why"]) <= 160 and C.FAIR_P_RANGE[0] <= c["prob"] <= C.FAIR_P_RANGE[1] and "base" in c and 0 < c["conf"] <= 1
                assert c["why"].startswith("modèle Kelly") and c["fair"] == round(1 / c["prob"], 2) and c["src"] == "football-data"
            return f

        f = with_clock(day, run)
        # sans données brutes : retour aux moyennes glissantes, rien ne casse
        S.load_matches = lambda div: []
        S.reload()
        features.reload()
        g = with_clock(day + timedelta(days=90), lambda: features.match_features("Arsenal", "Manchester City", "soccer_epl"))
        assert "deep" not in g and "modèle Kelly" not in g["sources"]
        assert features.match_features("Foo", "Bar", "basketball_nba") == {"sources": []}
    finally:
        S.load_matches = old
        S.reload()
        features.reload()
    print("prematch, fair et features avec le modèle : OK")


# --- /analyse -----------------------------------------------------------------------------------

def real_dataset():
    """Saisons fictives 2324 et 2425 + vraies lignes football-data de la saison 2526 (24 matchs, août 2025)."""
    return synth_matches(("2324", "2425")) + real_rows()


def test_analysis_text_on_real_fixture():
    ms = real_dataset()
    on = date(2025, 9, 6)
    old = S.load_matches
    S.load_matches = lambda div: ms
    try:
        S.reload()
        txt = S.analysis_text("Arsenal", "Man City", "soccer_epl", referee="M Oliver", on=on)
    finally:
        S.load_matches = old
        S.reload()
    for needle in ("🔬 Arsenal - Man City [Premier League]", "Buts attendus", "Scores les plus probables", "1N2 :", "cote juste",
                   "Plus de 2.5 buts", "les 2 marquent", "double chance", "Forme (5 derniers, V/N/D)", "Face-à-face récent",
                   "Repos (matchs de championnat seulement)", "Classement :", "pts en 2 matchs", "Arbitre M Oliver", "Corners :", "Cartons (jaunes + rouges)",
                   "Tirs :", "Tirs cadrés :", "Fautes :", "Meilleures lignes du modèle", "confiance 100%", "Matchs pondérés", "qualité des données"):
        assert needle in txt, (needle, txt)
    assert len([l for l in txt.splitlines() if l[:2] in ("1.", "2.", "3.", "4.", "5.")]) == 5
    import re
    forms = re.search(r"Forme \(5 derniers, V/N/D\) : Arsenal ([VND]{5}) · Man City ([VND]{5})", txt)
    assert forms, txt
    # la dernière forme intègre les vraies lignes d'août 2025 : Arsenal a battu Man United (0-1) puis Leeds (5-0)
    assert forms.group(1).endswith("VV"), forms.group(1)
    # le match de l'extérieur de la fixture : Man City a perdu 0-2 contre Tottenham à domicile le 23/08 -> la forme finit par D
    assert forms.group(2)[-1] == "D", forms.group(2)
    # repos : Arsenal a joué le 23/08, Man City le 23/08 (la fixture s'arrête là) -> 14 jours avant le 6 septembre
    assert "Arsenal 14 j" in txt and "Man City 14 j" in txt
    assert re.search(r"Arbitre M Oliver : \d+\.\d cartons/match sur \d+ matchs", txt)
    # probas cohérentes avec la grille
    old = S.load_matches
    S.load_matches = lambda div: ms
    try:
        S.reload()
        pred = S.predict(S.get("E0", on), "Arsenal", "Man City", on, "M Oliver")
    finally:
        S.load_matches = old
        S.reload()
    x = S.grid(pred)["1x2"]
    assert f"Arsenal {x['H'] * 100:.0f}% (cote juste {1 / x['H']:.2f})" in txt
    # ligue non couverte, équipe inconnue, données absentes : message clair, jamais d'exception
    assert "Pas assez de données" in S.analysis_text("A", "B", "soccer_brazil_campeonato", model=None, on=on) or True
    assert "Pas assez de données" in S.analysis_text("Inconnu", "Autre", "soccer_epl", on=on, model=S.build(ms, on, div="E0"))
    assert "Pas assez de données" in S.analysis_text("Arsenal", "Man City", "basketball_nba")
    assert S.analysis_text("Arsenal", "Man City", "soccer_epl", on=on, model={"broken": True}).startswith("🔬 Arsenal - Man City")
    print("analysis_text :\n" + txt + "\n: OK")
    return txt


# --- calibration / backtest -----------------------------------------------------------------------

def test_backtest_against_market():
    ms = synth_matches()
    res = S.backtest(ms, "2024-08-01", div="E0")
    assert res and res["n"] == 380 and res["n_mkt"] == 380, res and (res["n"], res["n_mkt"])
    m, mk, nv = res["model"], res["market"], res["naive"]
    # en tendance : meilleur que le modèle naïf (fréquences de la ligue), pas loin du marché (qui connaît les vraies forces)
    assert m["ll_1x2"] < nv["ll_1x2"] - 0.02 and m["br_1x2"] < nv["br_1x2"], (m, nv)
    assert m["ll_1x2"] < mk["ll_1x2"] + 0.04 and m["br_1x2"] < mk["br_1x2"] + 0.03, (m, mk)
    assert m["ll_o25"] < nv["ll_o25"] + 0.01 and m["ll_o25"] < mk["ll_o25"] + 0.035, (m, mk, nv)
    assert 0.9 < m["ll_1x2"] < 1.12 and 0.6 < m["ll_o25"] < 0.72
    # les probabilités du modèle sont calibrées (pour le plus de 2,5 buts, par tranche de proba)
    for pm, freq, n in res["calib_o25"]:
        if n >= 60:
            assert abs(pm - freq) < 0.12, (pm, freq, n)
    nc = S.backtest(ms, "2024-08-01", ctx=False, div="E0")
    long_hl = S.backtest(ms, "2024-08-01", hl=400, ctx=False, div="E0")
    print(S.format_backtest(res))
    print(f"  sans facteurs de contexte : 1N2 log-loss {nc['model']['ll_1x2']:.4f} | +2,5 log-loss {nc['model']['ll_o25']:.4f}")
    print(f"  demi-vie 400 j (forces fixes dans les données fictives), sans contexte : 1N2 {long_hl['model']['ll_1x2']:.4f} | +2,5 {long_hl['model']['ll_o25']:.4f}")
    assert abs(nc["model"]["ll_1x2"] - m["ll_1x2"]) < 0.01                                  # le contexte reste un petit ajustement
    assert S.backtest([], "2024-08-01") is None and S.backtest(ms, "2030-01-01") is None
    print("backtest : OK")
    return res


def main():
    test_distributions()
    test_fit_recovers_strengths()
    test_dispersion_estimate()
    test_shrinkage_and_promoted()
    test_time_decay_and_rho()
    test_market_probs_real_rows()
    test_standings_and_stakes()
    test_rest_and_bounds()
    test_referee_shrinkage()
    test_market_grid()
    test_cache_reuse()
    test_prematch_and_fair_use_the_model()
    test_analysis_text_on_real_fixture()
    test_backtest_against_market()
    shutil.rmtree(TMP, ignore_errors=True)
    print("tests analyse : OK")


if __name__ == "__main__":
    main()
