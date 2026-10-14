"""Voie `saison`, v2 (hors ligne, données fictives) : a priori de marché (cotes de clôture déjà jouées) et ajustement itératif corrigé de l'adversaire, pré-déclarés dans season_ledger.json (v2).
Garanties : défauts inchangés (flat / moments), buts et dispersion jamais touchés, test de fuite à DEUX sens (cotes du match prédit et des suivants sans effet, cotes d'un match antérieur avec effet),
l'équipe qui n'a croisé que des défenses fortes est corrigée par l'ajustement itératif, repli sans adversaire connu.
Lancer : python -m tests.test_season_v2"""
import json
import os
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-v2-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-season-v2-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, season as S  # noqa: E402

SK = "soccer_epl"
ON = date(2026, 10, 14)
FAMS = ("corners", "yellow", "shots", "sot", "fouls")
TEAMS = [f"T{i}" for i in range(8)]
ATT = dict(zip(TEAMS, (1.30, 1.15, 1.10, 1.00, 1.00, 0.90, 0.85, 0.70)))
DEF = dict(zip(TEAMS, (0.85, 0.80, 1.00, 1.00, 1.05, 1.10, 1.15, 1.25)))      # > 1 : encaisse plus


def fit(obs, on, sk, prior="flat", fit_mode="moments", **kw):
    """Les tests nomment leurs options : (a) = flat / moments sauf demande (les défauts de la production sont market / iterative depuis la lecture v2)."""
    return S.fit(obs, on, sk, prior=prior, fit_mode=fit_mode, **kw)


def make(schedule, noise=0.0):
    """Lignes d'observation de matchs sans bruit (valeurs = attentes exactes) : buts et une stat par famille (même structure de force), `xg` = attentes de buts exactes (cotes de clôture parfaites)."""
    obs, day = [], date(2026, 8, 5)
    for h, a in schedule:
        day += timedelta(days=1)
        lh, la = 1.5 * 1.2 * ATT[h] * DEF[a], 1.5 * ATT[a] * DEF[h]
        s_h = {"goals": (lh, la)}
        s_a = {"goals": (la, lh)}
        for i, fam in enumerate(FAMS):
            base = 5.0 + i
            eh, ea = base * 1.2 * ATT[h] ** 0.5 * DEF[a] ** 0.5, base * ATT[a] ** 0.5 * DEF[h] ** 0.5
            s_h[fam], s_a[fam] = (eh, ea), (ea, eh)
        d = day.isoformat()
        obs.append({"team": h, "d": d, "home": 1, "s": s_h, "opp": a, "xg": (lh, la)})
        obs.append({"team": a, "d": d, "home": 0, "s": s_a, "opp": h, "xg": (la, lh)})
    return obs


def round_robin(reps=2):
    return [(h, a) for _ in range(reps) for h in TEAMS for a in TEAMS if h != a]


def test_defaults_unchanged():
    """Réglages par défaut = ceux adoptés à la lecture v2 (market / iterative) ; (a) reste disponible en explicite (retour en arrière) et ne lit jamais les cotes."""
    obs = make(round_robin(1))
    assert C.SEASON_PRIOR == "market" and C.SEASON_FIT == "iterative"
    base = fit(obs, ON, SK, prior="flat", fit_mode="moments")
    explicit = fit(obs, ON, SK, prior="flat", fit_mode="moments")
    default = S.fit(obs, ON, SK)
    assert default["cfg"] == {"prior": "market", "fit": "iterative"} and default["stats"]["corners"] != base["stats"]["corners"] and default["stats"]["goals"] == base["stats"]["goals"]
    strip = lambda m: {k: v for k, v in m.items() if k != "cfg"}      # noqa: E731
    assert json.dumps(strip(base), sort_keys=True) == json.dumps(strip(explicit), sort_keys=True) and base["cfg"] == {"prior": "flat", "fit": "moments"}
    # des `xg` présents n'y changent rien quand l'a priori de marché est éteint
    bare = [{k: v for k, v in o.items() if k != "xg"} for o in obs]
    assert json.dumps(strip(fit(bare, ON, SK, prior="flat", fit_mode="moments")), sort_keys=True) == json.dumps(strip(base), sort_keys=True)
    print("ok  défauts (market / iterative, lecture v2) ; (a) explicite identique avec ou sans cotes, buts du défaut = ceux de (a)")


def test_market_prior_scope():
    obs = make(round_robin(1))
    a = fit(obs, ON, SK, prior="flat")
    for mode in ({"prior": "market"}, {"prior": "market", "fit_mode": "iterative"}, {"fit_mode": "iterative"}):
        b = fit(obs, ON, SK, **mode)
        assert json.dumps(b["stats"]["goals"], sort_keys=True) == json.dumps(a["stats"]["goals"], sort_keys=True), "les buts ne bougent jamais"
        for fam in FAMS:
            assert b["stats"][fam]["alpha"] == a["stats"][fam]["alpha"] and b["stats"][fam]["atot"] == a["stats"][fam]["atot"], f"la dispersion reste celle de (a) : {fam} {mode}"
    b = fit(obs, ON, SK, prior="market")
    t0a, t0b = a["stats"]["corners"]["t"]["T0"], b["stats"]["corners"]["t"]["T0"]
    assert t0b["ah"] != t0a["ah"] and t0b["n"] == t0a["n"], "l'a priori de marché déplace la moyenne d'équipe"
    assert b["stats"]["corners"]["lg_h"] == a["stats"]["corners"]["lg_h"], "mêmes moyennes de ligue"
    print("ok  a priori de marché : moyennes d'équipe des cinq familles seulement, buts et dispersion de (a) intacts")


def test_leak_two_ways():
    obs = make(round_robin(1))
    cut = obs[len(obs) // 2]["d"]
    on = date.fromisoformat(cut)
    def perturbed(pred):
        out = []
        for o in obs:
            if pred(o) and o.get("xg"):
                o = dict(o, xg=(o["xg"][0] * 3.0 + 1.0, o["xg"][1] * 0.2))
            out.append(o)
        return out
    dump = lambda m: json.dumps(m, sort_keys=True)      # noqa: E731
    for mode in ({"prior": "market"}, {"prior": "market", "fit_mode": "iterative"}):
        ref = dump(fit(obs, on, SK, **mode))
        later = dump(fit(perturbed(lambda o: o["d"] >= cut), on, SK, **mode))
        assert later == ref, f"cotes du jour du modèle et des suivants : aucun effet ({mode})"
        earlier = dump(fit(perturbed(lambda o: o["d"] < cut and o["team"] in ("T0", "T1")), on, SK, **mode))
        assert earlier != ref, f"cotes d'un match antérieur : effet attendu, l'a priori est branché ({mode})"
    for mode in ({"prior": "flat"}, {"prior": "flat", "fit_mode": "iterative"}):
        ref = dump(fit(obs, on, SK, **mode))
        assert dump(fit(perturbed(lambda o: o["d"] < cut), on, SK, **mode)) == ref, f"sans a priori de marché, les cotes ne comptent pour rien ({mode})"
    print("ok  fuite à deux sens : cotes du match prédit et des suivants sans effet, cotes antérieures avec effet pour b et d, aucun pour a et c")


def test_iterative_opponent():
    """T0 (forte attaque 1,30) n'a joué que contre T1 et T2 (défenses fortes 0,80 et 0,85) : sa moyenne brute sous-estime sa force, l'ajustement itératif la corrige."""
    sched = [(h, a) for h in TEAMS[1:] for a in TEAMS[1:] if h != a] * 2 + [("T0", "T1"), ("T0", "T2"), ("T1", "T0"), ("T2", "T0")] * 3
    obs = make(sched)
    on = date(2026, 12, 31)
    truth = lambda t, s: s["lg_h"] * ATT[t] ** 0.5     # noqa: E731 - attaque vraie des corners à domicile (relative à une défense moyenne 1)
    mom = fit(obs, on, SK)["stats"]["corners"]
    it = fit(obs, on, SK, fit_mode="iterative")["stats"]["corners"]
    mk = fit(obs, on, SK, prior="market", fit_mode="iterative")["stats"]["corners"]
    ex = lambda s: S._expect(s, "T0", "T7")       # noqa: E731 - attente de T0 à domicile contre une défense faible (1,25)
    true_h = 5.0 * 1.2 * ATT["T0"] ** 0.5 * DEF["T7"] ** 0.5
    e_mom, e_it, e_mk = abs(ex(mom)[0] - true_h), abs(ex(it)[0] - true_h), abs(ex(mk)[0] - true_h)
    assert e_it < e_mom and e_mk < e_mom, (e_mom, e_it, e_mk)
    print(f"ok  ajustement itératif : T0 n'a croisé que des défenses fortes, erreur sur son attente {e_mom:.3f} (moyennes) -> {e_it:.3f} (itératif) / {e_mk:.3f} (avec a priori de marché)")


def test_format_and_fallback():
    obs = make(round_robin(1))
    m = fit(obs, ON, SK, prior="market", fit_mode="iterative")
    p = S.predict(m, "T0", "T3")
    assert p and p["stats"]["corners"]["h"] > 0 and p["lane"] == "saison" and set(FAMS) <= set(p["stats"])
    # sans identifiant d'adversaire (autres sources) : retour aux moyennes
    no_opp = [dict(o, opp=None) for o in obs]
    assert json.dumps(fit(no_opp, ON, SK, fit_mode="iterative")["stats"], sort_keys=True) == json.dumps(fit(no_opp, ON, SK)["stats"], sort_keys=True)
    # sans aucune cote : l'a priori de marché retombe sur la moyenne de ligue
    bare = [{k: v for k, v in o.items() if k != "xg"} for o in obs]
    assert json.dumps(fit(bare, ON, SK, prior="market")["stats"], sort_keys=True) == json.dumps(fit(bare, ON, SK)["stats"], sort_keys=True)
    # relatif d'équipe : rétréci vers 1 avec n / (n + 6), moyenne de ligue du côté en dénominateur
    rel = S.market_rel([o for o in obs if o["d"] < ON.isoformat()])
    n = 7
    assert abs(rel["T0"][1][0] - (n / (n + 6) * (1.5 * 1.2 * ATT["T0"] * sum(DEF[a] for a in TEAMS if a != "T0") / n) / (sum(1.5 * 1.2 * ATT[h] * sum(DEF[a] for a in TEAMS if a != h) for h in TEAMS) / 56) + 6 / (n + 6))) < 1e-9, rel["T0"]
    print("ok  format de season.predict conservé, repli sans adversaire ou sans cotes, relatif de marché rétréci n/(n+6)")


def test_conceded_orientation():
    """Le relatif « encaissé » se divise par la moyenne de ligue du côté ADVERSE (ledger, clarification) et les a priori d'encaissé suivent leur côté."""
    rows = []
    for i in range(10):
        d = f"2026-09-{i + 1:02d}"
        rows.append({"team": "A", "d": d, "home": 1, "s": {}, "xg": (2.0, 1.0)})        # A reçoit : marque 2, encaisse 1
        rows.append({"team": "B", "d": d, "home": 0, "s": {}, "xg": (1.0, 2.0)})
    rel = S.market_rel(rows, shrink=0.0)
    assert abs(rel["A"][1][0] - 1.0) < 1e-9 and abs(rel["A"][1][1] - 1.0) < 1e-9, rel["A"]      # moyenne de ligue domicile = 2 (marqués), extérieur = 1 (encaissés à domicile)
    rows.append({"team": "A", "d": "2026-09-20", "home": 1, "s": {}, "xg": (2.0, 2.0)})
    rel = S.market_rel(rows, shrink=0.0)
    assert abs(rel["A"][1][1] - 12 / 11) < 1e-9, rel["A"]            # encaissé à domicile (12 / 11 en moyenne) / buts implicites à l'extérieur en moyenne de ligue (1), pas / 2
    fs = {"lg_h": 5.0, "lg_a": 4.0}
    pri = S._market_priors({"A": {1: (1.21, 0.81), 0: (0.64, 1.44)}}, fs, 0.5)["A"]
    assert abs(pri[0] - 5.0 * 1.1) < 1e-9 and abs(pri[1] - 4.0 * 0.9) < 1e-9 and abs(pri[2] - 4.0 * 0.8) < 1e-9 and abs(pri[3] - 5.0 * 1.2) < 1e-9, pri      # ah, dh, aa, da
    print("ok  relatif d'encaissé rapporté au côté adverse ; a priori ah / dh / aa / da contre lg_h / lg_a / lg_a / lg_h")


def main():
    for t in (test_defaults_unchanged, test_market_prior_scope, test_leak_two_ways, test_iterative_opponent, test_format_and_fallback, test_conceded_orientation):
        t()
    print("tests voie saison v2 : OK")


if __name__ == "__main__":
    main()
