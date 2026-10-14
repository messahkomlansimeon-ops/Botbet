"""Ensemble adaptatif (kelly/ensemble.py), écarts sharp (kelly/sharpgap.py) et chemin « ensemble » de xprice, hors ligne : devig 2 et 3 issues,
renormalisation des poids, apprentissage lent avec rappel vers l'a priori, veto et règle de valeur, journal des avis, écarts sharp (filtres,
fraîcheur), et chemin d'origine de xprice STRICTEMENT inchangé quand C.ENSEMBLE_ENABLED est faux (le mode mixte par défaut : tests/test_ensemble_wiring.py).
Lancer : python -m tests.test_ensemble"""
import copy
import math
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-ensemble-")
os.environ["ENSEMBLE_FROZEN_MODEL"] = "none"       # ces tests fixent le comportement D'AVANT le gel du poids du modèle (audit du 14/10) : un ensemble vide le reproduit exactement
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ensemble-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ensemble-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")

from kelly import calib, combos as CB, config as C, ensemble as E, fair, sharpgap, store, xprice  # noqa: E402
from kelly.sources import xbet  # noqa: E402

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


def fresh():
    """État propre : ni calibration, ni poids appris, ni journal."""
    for name in (calib.FILE, E.FILE, E.LOG_FILE):
        store.remove(name)
    calib._memo.clear()
    E.reset()


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ---------- devig ----------

def test_devig():
    # 2 issues sans marge : 50 / 50 ; avec marge : somme 1, ordre conservé, Shin retire plus de marge à l'outsider
    assert E.devig([2.0, 2.0]) == [0.5, 0.5]
    p = E.devig([1.5, 2.8])
    assert close(sum(p), 1.0, 1e-9) and p[0] > p[1] and abs(p[0] - 0.6548) < 5e-4, p                  # valeur de l'audit (shin_t)
    assert p[0] < 1 / 1.5 and p[1] < 1 / 2.8, "la marge est retirée aux deux issues"
    # 3 issues (1N2) : valeurs de l'audit
    p3 = E.devig([2.1, 3.3, 3.6])
    assert close(sum(p3), 1.0, 1e-9) and all(abs(a - b) < 5e-4 for a, b in zip(p3, [0.4555, 0.2847, 0.2598])), p3
    # cotes douteuses : refusées
    assert E.devig([2.0]) is None and E.devig([2.0, None]) is None and E.devig([1.0, 3.0]) is None
    assert E.devig([1.2, 1.2]) is None, "somme des 1/cote = 1,67 : une cellule manque ou le marché est suspendu"
    assert E.devig([1.4, 1.4, 1.4]) is None and E.devig([]) is None
    # x_fair : plus/moins, oui/non (paire de la MÊME ligne)
    px = {("totals", "Over", 2.5): 1.85, ("totals", "Under", 2.5): 1.95, ("totals", "Over", 3.5): 3.0, ("btts", "Yes", None): 1.8,
          ("btts", "No", None): 1.95}
    a = E.x_fair(px, "totals", "Over", 2.5)
    assert close(a, E.devig([1.85, 1.95])[0]) and 0.5 < a < 1 / 1.85 + 1e-9
    assert close(E.x_fair(px, "totals", "Under", 2.5), 1 - a, 1e-9)
    assert E.x_fair(px, "totals", "Over", 3.5) is None, "l'autre issue de la ligne 3,5 manque : pas de devig"
    assert E.x_fair(px, "totals", "Over", 4.5) is None and E.x_fair(px, "corners_x", "Over", 2.5) is None
    assert close(E.x_fair(px, "btts", "Yes", None), E.devig([1.8, 1.95])[0])
    # x_fair : 1N2 (trio complet obligatoire)
    px |= {("h2h", "Málaga", None): 2.9, ("h2h", "Draw", None): 3.25, ("h2h", "Espanyol", None): 2.7}
    h = [E.x_fair(px, "h2h", n, None) for n in ("Málaga", "Draw", "Espanyol")]
    assert close(sum(h), 1.0, 1e-9) and all(abs(a - b) < 1e-9 for a, b in zip(h, E.devig([2.9, 3.25, 2.7])))
    no_draw = {k: v for k, v in px.items() if k != ("h2h", "Draw", None)}
    assert E.x_fair(no_draw, "h2h", "Málaga", None) is None
    two_way = {("h2h", "Málaga", None): 1.9, ("h2h", "Espanyol", None): 1.9}            # 1N2 à deux issues seulement : incomplet, jamais dé-margé comme une paire
    assert E.x_fair(two_way, "h2h", "Málaga", None) is None
    # double chance : somme des deux issues du 1N2 sans marge quand les trois cotes du 1N2 sont là...
    px |= {("double_chance", "Málaga or Draw", None): 1.55, ("double_chance", "Draw or Espanyol", None): 1.5,
           ("double_chance", "Málaga or Espanyol", None): 1.4}
    dc = E.x_fair(px, "double_chance", "Málaga or Draw", None, "Málaga", "Espanyol")
    assert close(dc, h[0] + h[1], 1e-9)
    assert close(E.x_fair(px, "double_chance", "Draw or Málaga", None, "Málaga", "Espanyol"), dc), "ordre des équipes sans importance"
    # ... sinon les trois cotes de double chance ramenées à une masse de 2
    only_dc = {k: v for k, v in px.items() if k[0] == "double_chance"}
    ds = [E.x_fair(only_dc, "double_chance", n, None, "Málaga", "Espanyol") for n in ("Málaga or Draw", "Draw or Espanyol", "Málaga or Espanyol")]
    assert close(sum(ds), 2.0, 1e-9) and all(0 < x < 1 for x in ds), ds
    assert E.x_fair({k: v for k, v in only_dc.items() if k[1] != "Málaga or Espanyol"}, "double_chance", "Málaga or Draw", None) is None
    assert close(E.x_approx(1.5, 0.05), 1 / 1.5 / 1.05) and E.x_approx(None) is None and E.x_approx(1.0) is None
    print("ok  devig 2 et 3 issues (Shin), 1N2, double chance, refus des marchés incomplets")


# ---------- poids et combinaison ----------

def test_pool_and_renormalisation():
    fresh()
    # a priori : buts / 1N2 / 2 marquent / double chance = 0,12 ; corners, cartons, tirs... = 0,25 (0,5 avant l'audit du 13/10) ; sharp 0,75
    for m in ("totals", "btts", "double_chance", "h2h"):
        assert E.weights(E.family(m))["wm"] == 0.12, m
    for m in ("alternate_totals_corners", "alternate_totals_cards", "team_shots_total", "team_sot_home", "team_fouls_away",
              "team_throws_total", "team_offsides_total"):
        assert E.weights(E.family(m))["wm"] == 0.25, m
    assert {E.family(m) for m in ("team_shots_total", "team_shots_home", "team_shots_away")} == {"shots"}
    # trois avis : log-cotes, poids effectifs de somme 1
    p, parts = E.combine("goals", 0.70, 0.55, 0.60)
    mkt = E.expit(0.75 * E.logit(0.60) + 0.25 * E.logit(0.55))
    assert close(parts["p_mkt"], mkt) and close(p, E.expit(0.12 * E.logit(0.70) + 0.88 * E.logit(mkt)))
    assert close(sum(parts["w"].values()), 1.0) and close(parts["w"]["model"], 0.12) and close(parts["w"]["sharp"], 0.88 * 0.75)
    assert parts["src"] == "sharp+x+model" and 0.55 < p < 0.70
    # sharp absent : 1xBet porte tout le poids du marché, le modèle garde 0,12 (il ne grimpe PAS à 0,3)
    p2, parts = E.combine("goals", 0.70, 0.55, None)
    assert close(parts["w"]["model"], 0.12) and close(parts["w"]["x"], 0.88) and parts["w"]["sharp"] == 0.0 and parts["src"] == "x+model"
    assert close(p2, E.expit(0.12 * E.logit(0.70) + 0.88 * E.logit(0.55)))
    # 1xBet absent, sharp présent
    p3, parts = E.combine("goals", 0.70, None, 0.60)
    assert close(parts["w"]["sharp"], 0.88) and parts["w"]["x"] == 0.0 and close(p3, E.expit(0.12 * E.logit(0.70) + 0.88 * E.logit(0.60)))
    # aucun avis de marché : le modèle seul ; pas de modèle : le marché seul ; rien : None
    assert E.combine("goals", 0.70, None, None)[0] == 0.70 and E.combine("goals", 0.70)[1]["w"] == {"model": 1.0, "sharp": 0.0, "x": 0.0}
    assert close(E.combine("goals", None, 0.55, 0.60)[0], E.pool_market(0.60, 0.55, 0.75)[0])
    assert E.combine("goals", None, None, None)[0] is None
    # famille non mesurée : le modèle pèse 25 %, 1xBet 75 %
    pc, parts = E.combine("corners", 0.70, 0.50, None)
    assert close(pc, E.expit(0.25 * E.logit(0.70))) and close(parts["w"]["x"], 0.75)
    # avis identiques : la proba retenue est cette proba ; la combinaison reste entre les avis
    assert close(E.combine("goals", 0.6, 0.6, 0.6)[0], 0.6)
    for pm, px_, ps in ((0.9, 0.2, 0.5), (0.2, 0.9, 0.5), (0.5, 0.5, 0.9)):
        assert min(pm, px_, ps) <= E.combine("corners", pm, px_, ps)[0] <= max(pm, px_, ps)
    print("ok  combinaison en log-cotes, renormalisation par étage (le modèle garde son poids sans sharp)")


# ---------- apprentissage ----------

def rows_for(fam_market, n, model_good, seed, noise_m=0.10, noise_x=0.08, start=0):
    """Résultats synthétiques : p vraie ~ U(0,45 ; 0,8). Modèle exact et marché bruité (model_good) ou l'inverse."""
    rng = random.Random(seed)
    out = []
    for i in range(start + n):                        # le même flux de tirages : start=500 donne la suite des 500 premiers
        pt = rng.uniform(0.45, 0.8)
        pm, px = (pt, pt + rng.gauss(0, noise_x)) if model_good else (pt + rng.gauss(0, noise_m), pt)
        out.append({"key": f"{fam_market}|{seed}|{i}", "id": f"g{seed}x{i}", "date": "2026-10-01", "market": fam_market, "result": "win" if rng.random() < pt else "loss",
                    "pm": min(0.95, max(0.05, pm)), "px": min(0.95, max(0.05, px)), "ps": None})
    return out[start:]


def test_learning_is_slow_and_shrunk():
    fresh()
    corners = "alternate_totals_corners"
    # sous ENSEMBLE_MIN_N_OTHER matchs (300 : famille sans a priori mesuré) : le poids est EXACTEMENT l'a priori
    rows = rows_for(corners, 60, True, 1)
    assert E.learn(rows) == 60
    w = E.weights("corners")
    assert w["wm"] == 0.25 and 59 < w["n_m"] <= 60, w
    # idempotent : les mêmes lignes (ou leurs clés) ne comptent pas deux fois ; ligne non réglée, sans modèle ou sans marché : rien
    assert E.learn(rows) == 0 and all(r.get("ens") for r in rows)
    clones = [dict(r) for r in rows]
    for r in clones:
        r.pop("ens")
    assert E.learn(clones) == 0, "clés déjà apprises (ensemble.json) : pas de double compte, même depuis un autre document"
    assert E.learn([{"key": "n1", "market": corners, "result": None, "pm": 0.6, "px": 0.5}, {"key": "n2", "market": corners, "result": "win", "pm": None, "px": 0.5},
                    {"key": "n3", "market": corners, "result": "win", "pm": 0.6}, {"key": "n4", "market": corners, "result": "void", "pm": 0.6, "px": 0.5}]) == 0
    E.learn(rows_for(corners, 50, True, 1, start=60))
    assert E.weights("corners")["wm"] == 0.25, "110 matchs : sous 300, toujours l'a priori"
    # juste au-dessus du minimum, le passage au posterior est progressif : poids = a priori + (n - MIN) / MIN x (posterior - a priori)
    E.learn(rows_for(corners, 230, True, 1, start=110))
    acc = E.load()["fam"]["corners"]["m"]
    post = E._post_mean(acc, 0.25, C.ENSEMBLE_PRIOR_SD_OTHER)
    ramp = (acc["n"] - C.ENSEMBLE_MIN_N_OTHER) / C.ENSEMBLE_MIN_N_OTHER
    assert 0 < ramp < 0.5 and abs(post - 0.25) > 0.005 and close(E.weights("corners")["wm"], 0.25 + ramp * (post - 0.25)), (ramp, post, E.weights("corners"))
    # modèle réellement meilleur : le poids monte, lentement, et reste loin de 1 ; l'inverse : il baisse
    E.learn(rows_for(corners, 160, True, 1, start=340))
    w500 = E.weights("corners")["wm"]
    assert 0.25 < w500 < 0.6, w500
    E.learn(rows_for(corners, 1500, True, 1, start=500))
    w2000 = E.weights("corners")["wm"]
    assert w500 < w2000 < 0.9, (w500, w2000)
    fresh()
    E.learn(rows_for(corners, 700, False, 2))
    assert 0.05 < E.weights("corners")["wm"] < 0.25
    # lignes d'un même match : corrélées, elles comptent pour UN match (effectif et vraisemblance)
    fresh()
    same = [r | {"id": f"g7x{i // 4}"} for i, r in enumerate(rows_for(corners, 400, True, 7))]
    assert E.learn(same) == 400 and 95 < E.weights("corners")["n_m"] <= 100, E.weights("corners")       # 100 matchs (oubli léger compris)
    # famille où l'audit a mesuré (buts) : l'a priori serré (0,12) bouge à peine, même avec un modèle exact
    fresh()
    E.learn(rows_for("totals", 500, True, 3))
    assert 0.12 <= E.weights("goals")["wm"] < 0.2, E.weights("goals")
    fresh()
    E.learn(rows_for("totals", 800, False, 3))
    assert 0.05 < E.weights("goals")["wm"] <= 0.12
    # persistance : un autre processus relit les poids
    w = E.weights("goals")["wm"]
    E.reset()
    assert E.weights("goals")["wm"] == w and store.exists(E.FILE)
    # sharp face à 1xBet : appris à part (ws), seulement avec les deux avis ; `learn` marque les lignes apprises : chaque cas repart de lignes neuves
    def sharp_rows():
        rng = random.Random(5)
        out = []
        for i in range(400):
            pt = rng.uniform(0.45, 0.8)
            out.append({"key": f"s{i}", "date": "2026-10-01", "market": "totals", "result": "win" if rng.random() < pt else "loss", "pm": pt,
                        "ps": min(0.95, max(0.05, pt + rng.gauss(0, 0.02))), "px": min(0.95, max(0.05, pt + rng.gauss(0, 0.10)))})
        return out
    fresh()
    assert E.learn(sharp_rows()) == 400
    w = E.weights("goals")
    assert w["ws"] > 0.75 and w["n_s"] > 300 and w["n_m"] > 300, w
    fresh()
    assert E.learn([r | {"ps": None} for r in sharp_rows()]) == 400
    w = E.weights("goals")
    assert w["ws"] == 0.75 and w["n_s"] == 0 and w["n_m"] > 300, "sans les deux avis, le poids du sharp n'apprend pas"
    # un pari réel (sharp et 1xBet, sans avis du modèle) apprend le poids du sharp seulement
    fresh()
    assert E.learn([r | {"pm": None} for r in sharp_rows()]) == 400
    w = E.weights("goals")
    assert w["ws"] > 0.75 and w["n_s"] > 300 and w["n_m"] == 0 and w["wm"] == 0.12, w
    fresh()
    assert E.learn([r | {"pm": None, "ps": None} for r in sharp_rows()]) == 0 and E.learn([r | {"px": None, "ps": None} for r in sharp_rows()]) == 0, "un seul avis : rien à apprendre"
    # le poids du modèle est appris avec l'avis du marché au ws COURANT (déjà déplacé par les résultats sharp / 1xBet), pas au ws a priori
    fresh()
    assert E.learn(sharp_rows()) == 400
    ws_now = E.weights("goals")["ws"]
    assert ws_now > 0.75
    lam = 0.5 ** (1 / C.ENSEMBLE_HALF_LIFE)
    before = list(E.load()["fam"]["goals"]["m"]["ll"])
    one = {"key": "w1", "date": "2026-10-02", "market": "totals", "result": "win", "pm": 0.7, "px": 0.5, "ps": 0.6}
    assert E.learn([one]) == 1
    after = E.load()["fam"]["goals"]["m"]["ll"]
    p_mkt = E.expit(ws_now * E.logit(0.6) + (1 - ws_now) * E.logit(0.5))
    for g, b, a in zip(E.GRID, before, after):
        assert close(a, lam * b + C.ENSEMBLE_TEMP * math.log(E.expit(g * E.logit(0.7) + (1 - g) * E.logit(p_mkt))), 1e-9), g
    # un autre processus réécrit le document (règlement du soir, remise à zéro) : le poids est relu sans reset()
    fresh()
    E.learn(rows_for(corners, 600, True, 4))
    assert E.weights("corners")["wm"] > 0.25
    store.save(E.FILE, {"v": 2, "fam": {}, "done": {}})
    assert E.weights("corners")["wm"] == 0.25 and E.weights("corners")["n_m"] == 0
    # état de la version 1 (effectif en lignes, a priori 0,5) : le poids appris du modèle des familles non mesurées est oublié, les buts restent
    store.save(E.FILE, {"fam": {"corners": {"m": {"ll": [0.0] * 40, "n": 900}, "s": {"ll": [0.0] * 40, "n": 5}}, "goals": {"m": {"ll": [0.0] * 40, "n": 700}}},
                        "done": {}})
    st = E.load()
    assert st["v"] == 2 and "m" not in st["fam"]["corners"] and st["fam"]["corners"]["s"]["n"] == 5 and st["fam"]["goals"]["m"]["n"] == 700, st
    # oubli : l'effectif ne dépasse jamais ~ 1 / (1 - lambda)
    fresh()
    old = C.ENSEMBLE_HALF_LIFE
    C.ENSEMBLE_HALF_LIFE = 50
    try:
        E.learn(rows_for(corners, 600, True, 9))
        assert E.weights("corners")["n_m"] < 80, E.weights("corners")
    finally:
        C.ENSEMBLE_HALF_LIFE = old
    fresh()
    print("ok  apprentissage : a priori sous le minimum, mouvement lent, rappel fort sur les buts, idempotent, oubli")


# ---------- règle de valeur et veto ----------

def test_value_rule_and_veto():
    fresh()
    # buts, AVEC un avis sharp (Pinnacle, Betfair...) : marché 0,55 ; modèle 0,60 -> retenu ~ 0,556. Cote 1,95 : 0,556 x 1,95 = 1,084 >= 1,03 : valeur
    v = E.value_of("totals", 0.60, 0.55, 0.55, 1.95)
    assert v["ok"] and v["why"] == "" and 0.55 < v["p"] < 0.60 and close(v["ev"], v["p"] * 1.95 - 1) and close(v["gap"], 0.05)
    # cote 1,80 : 0,556 x 1,80 = 1,0 < 1,03 : le modèle seul (0,60 x 1,8 = 1,08) aurait validé, pas l'ensemble
    v = E.value_of("totals", 0.60, 0.55, 0.55, 1.80)
    assert not v["ok"] and v["why"] == "price" and 0.60 * 1.80 > 1.05
    # SANS avis sharp, même cote et mêmes avis : jamais « valeur » (1xBet seul rend 0,93-0,94 : le modèle ne doit pas décider seul) ; trop peu probable pour être « probable »
    v = E.value_of("totals", 0.60, 0.55, None, 1.95)
    assert not v["ok"] and v["why"] == "not_probable" and v["kind"] is None and close(v["ev_x"], 0.55 * 1.95 - 1)
    # marge réglable
    assert E.value_of("totals", 0.60, 0.55, 0.55, 1.90, margin=0.0)["ok"] and not E.value_of("totals", 0.60, 0.55, 0.55, 1.90, margin=0.20)["ok"]
    # frontière exacte : cote x p_ens == 1 + marge est valide
    p = E.combine("goals", 0.60, 0.55)[0]
    assert E.value_of("totals", 0.60, 0.55, 0.55, (1 + C.ENSEMBLE_MARGIN) / p + 1e-9)["ok"]
    assert not E.value_of("totals", 0.60, 0.55, 0.55, (1 + C.ENSEMBLE_MARGIN) / p - 1e-3)["ok"]
    # « probable » : sans avis sharp, proba retenue >= ENSEMBLE_PROBABLE_MIN_P (0,60) et 1xBet pas plus de 10 % sous elle ; ev_x : l'espérance au prix 1xBet sans marge
    v = E.value_of("totals", 0.66, 0.62, None, 1.55)
    assert v["ok"] and v["kind"] == "probable" and v["why"] == "" and 0.62 < v["p"] < 0.66 and v["ev"] < 0 and close(v["ev_x"], 0.62 * 1.55 - 1) and v["ev_x"] < 0
    assert E.value_of("totals", 0.66, 0.62, 0.62, 1.55)["why"] == "price" and E.value_of("totals", 0.66, 0.62, 0.62, 1.70)["kind"] == "value"        # avec sharp : la règle de valeur
    assert E.value_of("totals", 0.66, 0.62, None, 1.30)["why"] == "price", "1xBet bradé (plus de 10 % sous la proba retenue) : ni valeur ni probable"
    assert E.value_of("totals", 0.66, 0.62, None, 1.95)["kind"] == "probable", "sans sharp, même une cote généreuse n'est jamais une « valeur »"
    C.ENSEMBLE_PROBABLE = False
    try:
        assert E.value_of("totals", 0.66, 0.62, None, 1.55)["why"] == "no_sharp"
    finally:
        C.ENSEMBLE_PROBABLE = True
    # veto : famille où le marché pèse >= 0,7 et écart modèle-marché > 0,12, même avec une cote très généreuse
    v = E.value_of("totals", 0.75, 0.55, None, 3.0)
    assert not v["ok"] and v["why"] == "veto" and close(v["gap"], 0.20) and v["ev"] > 0.5
    assert E.value_of("totals", 0.67, 0.55, 0.55, 3.0)["ok"], "écart de 0,12 pile : pas de veto"
    assert E.value_of("totals", 0.55 + 0.1201, 0.55, None, 3.0)["why"] == "veto"
    # le veto se mesure au marché total (sharp + 1xBet)
    v = E.value_of("btts", 0.30, 0.52, 0.52, 3.0)
    assert v["why"] == "veto"
    # corners (marché à 0,75, mais jamais mesuré meilleur que le modèle : VETO_FAMILIES) : pas de veto, un grand écart reste permis
    v = E.value_of("alternate_totals_corners", 0.75, 0.55, None, 2.0)
    assert v["ok"] and v["why"] == "" and 0.55 < v["p"] < 0.75
    # famille de marché lourd sans aucun avis de marché : rien n'est validé
    v = E.value_of("totals", 0.70, None, None, 3.0)
    assert not v["ok"] and v["why"] == "no_market" and v["p"] == 0.70
    # ailleurs aussi, sans avis de marché : rien n'est validé (valeur jugée sur le seul modèle) ; sauf réglage explicite
    v = E.value_of("alternate_totals_corners", 0.70, None, None, 1.6)
    assert not v["ok"] and v["why"] == "no_market" and v["p"] == 0.70
    C.ENSEMBLE_REQUIRE_MARKET = False
    try:
        assert E.value_of("alternate_totals_corners", 0.70, None, None, 1.6)["ok"]
        assert E.value_of("totals", 0.70, None, None, 3.0)["why"] == "no_market", "familles à marché lourd : jamais sans avis de marché"
    finally:
        C.ENSEMBLE_REQUIRE_MARKET = True
    assert E.value_of("totals", None, None, None, 2.0)["why"] == "no_prob"
    # pari sans avis du modèle (pari réel) : l'avis sharp décide, comme un écart sharp
    v = E.value_of("h2h", None, 0.40, 0.45, 2.4)
    assert v["ok"] and close(v["p"], E.pool_market(0.45, 0.40, 0.75)[0]) and v["gap"] is None
    # seuil de veto réglable, et poids appris qui rendent le marché moins lourd
    old = C.ENSEMBLE_VETO_WEIGHT
    C.ENSEMBLE_VETO_WEIGHT = 0.95
    try:
        assert E.value_of("totals", 0.75, 0.55, 0.55, 3.0)["ok"], "poids du marché 0,88 < 0,95 : plus de veto"
    finally:
        C.ENSEMBLE_VETO_WEIGHT = old
    # value(c, price) lit le pronostic : p_model, xp (ou p_x), psh via p_sharp
    c = {"market": "totals", "prob": 0.60, "xp": 0.55, "p_sharp": 0.55, "xodds": 1.95}
    assert E.value(c)["ok"] and close(E.value(c)["p"], E.value_of("totals", 0.60, 0.55, 0.55, 1.95)["p"])
    assert not E.value(c, 1.80)["ok"] and E.value(c | {"p_x": 0.50})["parts"]["p_x"] == 0.50
    # double chance sans proba 1xBet : refusée tant que l'approximation n'est pas demandée
    d = {"market": "double_chance", "prob": 0.80, "xodds": 1.40}
    assert E.value(d)["why"] == "no_market"
    va = E.value(d | {"p_sharp": 0.68}, approx=True)
    assert va["parts"]["x_approx"] and close(va["parts"]["p_x"], 1 / 1.40 / 1.05) and va["why"] == "price" and va["gap"] < C.ENSEMBLE_MAX_DISAGREE
    va = E.value(d, approx=True)                      # même double chance sans avis sharp : « probable » (proba retenue ~ 0,69, 1xBet pas plus de 10 % sous elle)
    assert va["ok"] and va["kind"] == "probable" and va["gap"] < C.ENSEMBLE_MAX_DISAGREE
    print("ok  règle de valeur (marge 3 %), veto de désaccord, famille sans marché, marché peu lourd")


# ---------- attach, sharp_for, journal ----------

def test_attach_and_log():
    fresh()
    prono = {"key": "e1|totals||2.5|Over", "id": "e1", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-09T19:00:00Z",
             "market": "totals", "name": "Over", "point": 2.5, "prob": 0.6, "p0": 0.6, "xodds": 1.9, "xp": 0.52,
             "alts": [["Over", 2.5, 0.6, None], ["Under", 2.5, 0.4, None], ["Over", 3.5, 0.3, None]]}
    dc = {"key": "e1|double_chance|||A or Draw", "id": "e1", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-09T19:00:00Z",
          "market": "double_chance", "name": "A or Draw", "point": None, "prob": 0.7, "p0": 0.7, "xodds": 1.4,
          "alts": [["A or Draw", None, 0.7, None]]}
    pick_t = {"key": "e1|totals||2.5|Over", "id": "e1", "market": "totals", "name": "Over", "point": 2.5, "prob": 0.5, "prob_book": 0.5,
              "prob_src": "pinnacle", "n_sharp": 1, "sharp_at": "2026-10-09T14:30:00+00:00"}
    pick_dc = {"key": "e1|double_chance|||Draw or A", "id": "e1", "market": "double_chance", "name": "Draw or A", "point": None, "prob": 0.66,
               "prob_book": 0.66, "prob_src": "sharp", "n_sharp": 2}
    pick_cons = {"key": "e1|totals||3.5|Over", "id": "e1", "market": "totals", "name": "Over", "point": 3.5, "prob": 0.3, "prob_book": 0.3,
                 "prob_src": "consensus", "n_sharp": 0}
    assert E.sharp_of(pick_cons) is None and E.sharp_of(pick_t) == (0.5, "pinnacle", 1)
    n_s, n_m = E.attach([prono, dc], [pick_t, pick_dc, pick_cons])
    assert (n_s, n_m) == (2, 1 + 1 + 1), (n_s, n_m)                    # 2 pronostics couverts ; 3 paris réels avec une ligne du modèle
    assert prono["psh"] == {"over|2.5": [0.5, "pinnacle", 1, "2026-10-09T14:30:00+00:00"]}, prono["psh"]
    assert list(dc["psh"]) == ["a or draw|None"], "double chance : ordre des équipes sans importance"
    assert pick_t["p_model"] == 0.6 and pick_cons["p_model"] == 0.3
    assert E.sharp_for(prono, "Over", 2.5) == 0.5 and E.sharp_for(prono, "Under", 2.5) is None and E.sharp_for(prono, "Over", 3.5) is None
    # un relevé sharp trop vieux sort de l'ensemble ; heure inconnue : accepté
    assert E.sharp_for(prono, "Over", 2.5, datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)) == 0.5
    assert E.sharp_for(prono, "Over", 2.5, datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)) is None
    assert E.sharp_for(dc, "A or Draw", None, datetime(2030, 1, 1, tzinfo=timezone.utc)) == 0.66
    # un pari réel avec p_model compte son modèle ; sans, sa proba « prob » est celle des books : pas un avis du modèle
    assert E.opinions(pick_t) == (0.6, None, 0.5, False) and E.opinions(pick_cons | {"p_model": None})[0] is None
    assert E.opinions({"market": "totals", "prob_book": 0.4, "prob": 0.4})[0] is None

    # journal : seulement modèle + avis de marché exact ; mise à jour tant que non réglé ; match commencé non mis à jour ; ROW comme fair.json
    assert E.ROW == fair.ROW
    prono["p_sharp"] = 0.5
    future = NOW + timedelta(hours=4)
    prono["commence"] = dc["commence"] = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    no_market = prono | {"key": "e2|totals||2.5|Over", "xp": None, "p_sharp": None, "psh": None}
    approx = prono | {"key": "e3|totals||2.5|Over", "x_approx": True, "xp": 0.5, "p_x": 0.5, "p_sharp": None, "psh": None}
    real = {"key": "e4|h2h|||A", "id": "e4", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": prono["commence"], "market": "h2h", "name": "A",
            "point": None, "prob": 0.5, "prob_book": 0.5, "prob_src": "pinnacle", "n_sharp": 1, "p_x": 0.48}
    assert E.record([prono, dc, no_market, approx, real], "2026-10-09", NOW) == 2, "dc : pas de proba 1xBet exacte ; no_market : rien ; approx (marquée) : jamais ; pari réel sharp + 1xBet : oui"
    assert [r["pm"] for r in store.load(E.LOG_FILE, []) if r["key"] == real["key"]] == [None]
    store.save(E.LOG_FILE, [r for r in store.load(E.LOG_FILE, []) if r["key"] != real["key"]])
    log = store.load(E.LOG_FILE, [])
    assert len(log) == 1 and all(k in log[0] for k in fair.ROW) and log[0]["pm"] == 0.6 and log[0]["px"] == 0.52 and log[0]["ps"] == 0.5
    assert log[0]["result"] is None and log[0]["date"] == "2026-10-09" and log[0]["prob"] == 0.6
    prono["xp"], prono["xodds"] = 0.50, 2.0                                        # relevé plus récent avant le match : ajouté à part (« late »), la lecture de la décision reste
    assert E.record([prono], "2026-10-09", NOW + timedelta(hours=1)) == 0
    log = store.load(E.LOG_FILE, [])
    assert len(log) == 1 and log[0]["px"] == 0.52 and log[0]["ps"] == 0.5 and log[0]["late"]["px"] == 0.5 and "xodds" not in log[0]["late"], log[0]
    prono["xp"] = 0.4                                                              # match commencé : le journal n'est plus touché
    assert E.record([prono], "2026-10-09", future + timedelta(minutes=5)) == 0 and store.load(E.LOG_FILE, [])[0]["late"]["px"] == 0.5
    # les lignes de plus de ENSEMBLE_LOG_DAYS jours sortent
    E.record([prono | {"key": "e9|totals||2.5|Over", "xp": 0.5}], "2026-10-25", NOW)
    assert [r["key"] for r in store.load(E.LOG_FILE, [])] == ["e9|totals||2.5|Over"]
    # règlement du journal : settle_log applique le règlement fourni (phase 2 : settle._settle_fair_row) puis apprend
    fresh()
    E.record([prono | {"key": f"k{i}|totals||2.5|Over", "xp": 0.5 + i / 100, "prob": 0.6} for i in range(5)], "2026-10-09", NOW)
    done = {"k0|totals||2.5|Over": "win", "k1|totals||2.5|Over": "loss", "k2|totals||2.5|Over": "void"}

    def settle_one(r):
        r["result"] = done.get(r["key"])
        return r["result"] is not None
    settled, learned = E.settle_log(settle_one)
    assert len(settled) == 3 and learned == 2, (len(settled), learned)             # le « void » ne compte pas
    assert E.settle_log(settle_one) == ([], 0), "déjà réglé / appris : rien de plus"
    assert all(r.get("ens") for r in store.load(E.LOG_FILE, []) if r["result"] in ("win", "loss"))
    assert any("buts" in line for line in E.lines())
    fresh()
    print("ok  attach (sharp, modèle, fraîcheur), journal des avis (champs de fair.json), règlement différé")


# ---------- chemin d'origine de xprice inchangé ----------

def legacy_choose(c, px):
    """choose() tel qu'il était avant le chemin ensemble (copie du code d'origine)."""
    best, key = None, None
    for name, point, p0, base in c.get("alts") or [[c["name"], c["point"], c.get("p0", c["prob"]), None]]:
        price = xbet.lookup(px, c["market"], name, point)
        if not price:
            continue
        p = calib.calibrate(c["market"], p0, sport_key=c.get("sport_key"))
        b = None if base is None else calib.calibrate(c["market"], base, sport_key=c.get("sport_key"))
        if p < C.VIABLE_MIN_P or (b is not None and p < b) or price < round(1 / p * (1 + C.FAIR_MARGIN), 2):
            continue
        ev = p * price - 1
        rank = (p >= C.FAIR_COMBO_MIN_P, ev * p)
        if key is None or rank > key:
            best, key = (name, point, p, p0, b, price, ev), rank
    return best


def legacy_price(c, px, refs=None):
    """price() tel qu'il était avant le chemin ensemble (copie du code d'origine)."""
    pick = legacy_choose(c, px)
    if pick:
        name, point, p, p0, b, odd, ev = pick
        moved = (name, point) != (c["name"], c["point"])
        if moved:
            c.update(name=name, point=point, key=f'{c["id"]}|{c["market"]}||{point if point is not None else ""}|{name}',
                     pick=xprice.odds.label(c["market"], name, point, c.get("who"), "soccer"))
            if b is not None:
                c["base"] = round(b, 4)
        c.update(prob=round(p, 4), p0=round(p0, 4), fair=round(1 / p, 2), min_odds=round(1 / p * (1 + C.FAIR_MARGIN), 2),
                 xodds=odd, xev=round(ev, 4), xp=xbet.book_prob(px, c["market"], name, None if point is None else float(point)))
        if moved or c.get("xodds0") is None:
            c["xodds0"] = odd
        ref = (refs or {}).get((c["market"], name, None if point is None else float(point)))
        if ref:
            c["xref"] = ref
        return "value"
    odd = xbet.lookup(px, c["market"], c["name"], c["point"])
    if odd:
        c.update(xodds=odd, xev=round(c["prob"] * odd - 1, 4),
                 xp=xbet.book_prob(px, c["market"], c["name"], None if c["point"] is None else float(c["point"])))
        ref = (refs or {}).get((c["market"], c["name"], None if c["point"] is None else float(c["point"])))
        if ref:
            c["xref"] = ref
        return "weak" if xprice._weak(c) else "low"
    return None


def scenario(rng):
    """Un pronostic aux lignes variées et les cotes 1xBet d'un match, tirés au hasard."""
    market = rng.choice(["totals", "btts", "alternate_totals_corners", "team_shots_total", "double_chance"])
    lines = [("Over", 1.5), ("Over", 2.5), ("Under", 2.5), ("Under", 3.5)] if market in ("totals", "alternate_totals_corners", "team_shots_total") else \
        [("Yes", None), ("No", None)] if market == "btts" else [("A or Draw", None), ("Draw or B", None)]
    alts = [[n, pt, round(rng.uniform(0.5, 0.85), 4), rng.choice([None, round(rng.uniform(0.4, 0.7), 4)])] for n, pt in lines if rng.random() < 0.8]
    name, point = rng.choice(lines)
    px = {(market, n, pt): round(rng.uniform(1.15, 3.2), 2) for n, pt in lines if rng.random() < 0.85}
    c = {"key": f"s|{market}||{point}|{name}", "id": "s", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-09T19:00:00Z",
         "market": market, "name": name, "point": point, "who": None, "prob": round(rng.uniform(0.5, 0.85), 4), "p0": round(rng.uniform(0.5, 0.85), 4),
         "min_odds": 1.8, "conf": 0.6, "n_min": 8, "score": 0.2, "pick": "x"}
    if alts:
        c["alts"] = alts
    refs = {k: [900, 1 + i, 0] for i, k in enumerate(px)}
    return c, px, refs


def test_xprice_switched_off_is_identical():
    """ENSEMBLE_ENABLED=0 (interrupteur général, par l'environnement) : le chemin d'origine de xprice, strictement. Le défaut est « mixte » (voir
    test_mixed_defaults_and_inactive_families dans tests/test_ensemble_wiring.py)."""
    fresh()
    C.ENSEMBLE_ENABLED = False
    try:
        rng = random.Random(2026)
        seen = {"value": 0, "low": 0, "weak": 0, None: 0}
        for i in range(400):
            c, px, refs = scenario(rng)
            a, b = copy.deepcopy(c), copy.deepcopy(c)
            assert xprice.choose(a, px) == legacy_choose(b, px)
            ra, rb = xprice.price(a, px, refs), legacy_price(b, px, refs)
            assert ra == rb and a == b, (i, ra, rb, a, b)
            seen[ra] += 1
            assert not ({"p_model", "p_ens", "p_x", "p_sharp", "xveto", "xwhy", "ew", "esrc"} & set(a)), "aucun champ d'ensemble éteint"
            assert a.get("min_odds") == b.get("min_odds") and a.get("prob") == b.get("prob")
        assert all(seen[k] > 20 for k in ("value", "low", "weak")) and seen[None] > 5, seen
        # apply() : même résultat, sans clé « veto », aucune écriture de l'ensemble
        c, px, refs = scenario(random.Random(7))
        found = {"s": {"gid": 900, "px": px, "refs": refs}}
        out = xprice.apply([copy.deepcopy(c)], fetch=lambda *a, **k: found, picks=[{"id": "x"}])
        assert set(out) == {"matches", "value", "low", "weak", "none"} and not store.exists(E.FILE) and not store.exists(E.LOG_FILE)
        assert E.explain_c({"market": "totals", "p_x": 0.5, "p_model": 0.6}) == "" and E.legend() == "" and not E.logging_on() and not E.is_active("totals")
    finally:
        C.ENSEMBLE_ENABLED = True
    print("ok  ENSEMBLE_ENABLED=0 : chemin d'origine de xprice identique à l'ancien (400 scénarios), aucun champ ni fichier d'ensemble")


def sharp_free_kind(c, px, refs):
    xprice.price(c, px, refs)
    return c.get("xkind")


def test_xprice_opt_in_path():
    """Le chemin « ensemble » de xprice dans TOUTES les familles (phase 1) : ici ENSEMBLE_ACTIVE_FAMILIES les contient toutes ; le mode mixte par défaut
    (buts, 1N2, double chance, les 2 marquent seulement) est testé dans tests/test_ensemble_wiring.py."""
    fresh()
    old_active = C.ENSEMBLE_ACTIVE_FAMILIES
    C.ENSEMBLE_ENABLED = True
    C.ENSEMBLE_ACTIVE_FAMILIES = frozenset({"goals", "btts", "dc", "h2h", "corners", "cards", "shots", "sot", "fouls", "throws", "offsides"})
    try:
        base = {"key": "m|totals||1.5|Over", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-09T19:00:00Z",
                "market": "totals", "name": "Over", "point": 1.5, "who": None, "prob": 0.76, "p0": 0.76, "min_odds": 1.4, "conf": 0.6, "n_min": 8,
                "score": 0.2, "pick": "Plus de 1,5 buts", "alts": [["Over", 1.5, 0.76, None], ["Under", 1.5, 0.24, None]]}
        refs = {("totals", "Over", 1.5): [9, 9, 1.5]}
        sharp = {"key": "m|totals||1.5|Over", "id": "m", "market": "totals", "name": "Over", "point": 1.5, "prob": 0.74, "prob_book": 0.74,
                 "prob_src": "pinnacle", "n_sharp": 1}
        # --- buts, avec un avis sharp : valeur (la proba retenue suit le marché, le modèle ne pèse que 12 %) ---
        px = {("totals", "Over", 1.5): 1.45, ("totals", "Under", 1.5): 2.9}
        p_x = E.x_fair(px, "totals", "Over", 1.5)
        c = copy.deepcopy(base)
        found = {"m": {"gid": 900, "px": px, "refs": refs}}
        out = xprice.apply([c], fetch=lambda *a, **k: found, picks=[sharp])
        assert out == {"matches": 1, "value": 1, "low": 0, "weak": 0, "none": 0, "veto": 0}, out
        p_ens = E.combine("goals", 0.76, p_x, 0.74)[0]
        assert c["prob"] == c["p_ens"] == round(p_ens, 4) and c["p_model"] == 0.76 and c["p_x"] == c["xp"] == round(p_x, 4) and c["p_sharp"] == 0.74
        assert c["esrc"] == "sharp+x+model" and c["ew"] == 0.12 and c["p0"] == 0.76 and sharp["p_model"] == 0.76
        assert min(p_x, 0.74) < c["prob"] < 0.76 and abs(c["prob"] - 0.7285) < 2e-4
        assert c["xodds"] == 1.45 and abs(c["xev"] - (p_ens * 1.45 - 1)) < 1e-4 and c["xref"] == [9, 9, 1.5] and c["xodds0"] == 1.45
        assert abs(c["min_odds"] - (1 + C.ENSEMBLE_MARGIN) / p_ens) < 0.006 and c["xodds"] >= c["min_odds"] and c["fair"] == round(1 / p_ens, 2)
        assert "xveto" not in c and "xwhy" not in c and CB.viable(c)
        assert E.explain_c(c) == "1xBet : 67 % · Sharp : 74 % · Kelly : 76 % · retenu : 73 %" and E.explain_c(base) == "", E.explain_c(c)
        assert E.explain(E.prob(c)[1]) == E.explain_c(c), "même texte depuis les avis et la proba retenue"
        # relevé de l'après-midi sans paris réels : les probas sharp du matin restent ; modèle repris de c["alts"], pas de c["prob"] (= p_ens)
        again = copy.deepcopy(c)
        assert xprice.apply([again], fetch=lambda *a, **k: found)["value"] == 1
        assert again["prob"] == c["prob"] and again["p_model"] == 0.76 and again["p_sharp"] == 0.74 and "psh" in again
        # --- même pari, sans avis sharp : le marché seul porte 88 % -> jamais « valeur » (c'est voulu sur les buts) ; « probable » (🎯) : proba retenue ~ 0,68 >= 0,60 et
        # 1xBet pas plus de 10 % sous elle ; son espérance au prix 1xBet sans marge (xev_x) est négative : aucune mise (combos.stake_prob) ---
        low = copy.deepcopy(base)
        assert xprice.price(low, px, refs) == "value" and low["xkind"] == "probable" and "xveto" not in low and "p_sharp" not in low and "xwhy" not in low
        assert low["xodds"] == 1.45 and low["min_odds"] <= low["xodds"] and CB.viable(low) and low["p_model"] == 0.76 and 0.60 <= low["prob"] < 0.76
        assert low["xev_x"] < 0 and CB.probable(low)
        assert abs(CB.stake_prob([low], low["prob"]) - low["xp"]) < 1e-3 and low["xp"] * low["xodds"] < 1, "mise calculée sur la proba de 1xBet sans marge : espérance négative"
        assert sharp_free_kind(copy.deepcopy(base), px, refs) == "probable" and xprice.price(copy.deepcopy(c), px, refs) == "value" and c["xkind"] == "value"
        cheap = copy.deepcopy(base) | {"prob": 0.70, "p0": 0.70, "alts": [["Over", 1.5, 0.70, None]]}                 # marge de 27 % chez 1xBet (1,30 / 2,00) : plus de 10 % sous la proba retenue, ni valeur ni probable
        assert xprice.price(cheap, {("totals", "Over", 1.5): 1.30, ("totals", "Under", 1.5): 2.0}, refs) == "low" and cheap["xwhy"] == "price" and "xkind" not in cheap and not CB.viable(cheap)
        legacy = copy.deepcopy(base)
        C.ENSEMBLE_ENABLED = False
        assert xprice.price(legacy, px, refs) == "value", "le modèle seul aurait validé ce pari (0,76 x 1,45 = 1,10) : l'ensemble ne le valide pas"
        C.ENSEMBLE_ENABLED = True
        # --- veto : modèle 0,86 contre un marché à ~0,67 -> jamais « value », même à cote généreuse ---
        far = copy.deepcopy(base) | {"prob": 0.86, "p0": 0.86, "alts": [["Over", 1.5, 0.86, None]]}
        out = xprice.apply([far], fetch=lambda *a, **k: found)
        assert out["veto"] == 1 and out["low"] == 1 and out["value"] == 0, out
        assert far["xveto"] == 1 and far["xwhy"] == "veto" and far["min_odds"] > far["xodds"] and not CB.viable(far) and far["p_model"] == 0.86
        # un relevé suivant, sans veto, efface la marque
        ok = copy.deepcopy(far) | {"alts": [["Over", 1.5, 0.76, None]]}
        xprice.apply([ok], fetch=lambda *a, **k: found, picks=[sharp])
        assert "xveto" not in ok and ok.get("xwhy") != "veto" and ok["p_model"] == 0.76
        # ligne actuelle absente de c["alts"] (ligne déplacée) : son modèle vient de c["p0"], jamais de c["prob"] (= p_ens d'un passage précédent)
        off = copy.deepcopy(base) | {"prob": 0.70, "p0": 0.76, "alts": [["Under", 1.5, 0.24, None]]}
        assert xprice.price(off, px, refs) == "low" and off["p_model"] == 0.76 and off["xodds"] == 1.45 and off["xwhy"] == "line"       # sans sharp la ligne serait « probable » : hors c["alts"], elle n'est pas proposée
        # --- corners (non mesuré : modèle 25 %) : sans avis sharp, la valeur est possible, sur un désaccord net (Kelly 85 %, 1xBet 60 %) ---
        pxc = {("alternate_totals_corners", "Under", 10.5): 1.55, ("alternate_totals_corners", "Over", 10.5): 2.45}
        cor = {"key": "m|alternate_totals_corners||10.5|Under", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B",
               "commence": "2026-10-09T19:00:00Z", "market": "alternate_totals_corners", "name": "Under", "point": 10.5, "who": None, "prob": 0.85,
               "p0": 0.85, "min_odds": 1.4, "conf": 0.6, "n_min": 8, "score": 0.2, "pick": "Moins de 10,5 corners",
               "alts": [["Under", 10.5, 0.85, None]]}
        px_c = E.x_fair(pxc, "alternate_totals_corners", "Under", 10.5)
        assert xprice.price(cor, pxc) == "value"
        assert cor["ew"] == 0.25 and cor["esrc"] == "x+model" and cor["p_model"] == 0.85 and abs(cor["prob"] - E.expit(0.25 * E.logit(0.85) + 0.75 * E.logit(px_c))) < 1e-4
        assert px_c < cor["prob"] < 0.85 and cor["xodds"] == 1.55 and abs(cor["xev"] - (cor["prob"] * 1.55 - 1)) < 1e-4 and CB.viable(cor)
        assert "xveto" not in cor, "famille où le marché n'a jamais été mesuré meilleur : un écart de 0,25 n'est pas écarté"
        mild = copy.deepcopy(cor) | {"prob": 0.75, "p0": 0.75, "alts": [["Under", 10.5, 0.75, None]]}
        assert xprice.price(mild, pxc) == "low" and not CB.viable(mild), "Kelly 75 % contre 1xBet 60 % : à 25 %, plus assez pour une valeur"
        # sous C.VIABLE_MIN_P, la proba retenue rend la ligne « faible » : ici 0,62 de modèle sur des plus/moins à 2,1 / 1,75 -> ~0,53
        pxw = {("alternate_totals_corners", "Over", 9.5): 2.1, ("alternate_totals_corners", "Under", 9.5): 1.75}
        thin = copy.deepcopy(cor) | {"name": "Over", "point": 9.5, "prob": 0.62, "p0": 0.62, "alts": [["Over", 9.5, 0.62, None]]}
        assert xprice.price(thin, pxw) == "weak" and thin["prob"] < C.VIABLE_MIN_P and not CB.viable(thin)
        # la base (modèle sous un match moyen) écarte toujours la ligne : « weak » (le modèle dit « moins bon qu'un match moyen »)
        weak = copy.deepcopy(cor) | {"alts": [["Under", 10.5, 0.75, 0.80]]}
        assert xprice.price(weak, pxc) == "weak" and not CB.viable(weak)
        # --- plusieurs lignes : choose retient une ligne qui passe la règle, jamais une ligne sous VIABLE_MIN_P ou en dessous de 1 + marge ---
        multi = copy.deepcopy(cor) | {"alts": [["Under", 10.5, 0.85, None], ["Over", 10.5, 0.15, None], ["Under", 11.5, 0.93, None]]}
        pxm = pxc | {("alternate_totals_corners", "Under", 11.5): 1.30, ("alternate_totals_corners", "Over", 11.5): 3.5}
        got = xprice.choose(copy.deepcopy(multi), pxm)
        assert got is not None and got[2] >= C.VIABLE_MIN_P and got[5] * got[2] >= 1 + C.ENSEMBLE_MARGIN - 1e-9 and (got[0], got[1]) in (("Under", 10.5), ("Under", 11.5))
        # --- double chance : la proba 1xBet vient des trois cotes (ici sans le 1N2) ---
        dcpx = {("double_chance", "A or Draw", None): 1.45, ("double_chance", "Draw or B", None): 1.75, ("double_chance", "A or B", None): 1.40}
        dcp = {"key": "m|double_chance|||A or Draw", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-09T19:00:00Z",
               "market": "double_chance", "name": "A or Draw", "point": None, "who": None, "prob": 0.72, "p0": 0.72, "min_odds": 1.5, "conf": 0.6,
               "n_min": 8, "score": 0.2, "pick": "A ou nul", "alts": [["A or Draw", None, 0.72, None]]}
        xdc = E.x_fair(dcpx, "double_chance", "A or Draw", None, "A", "B")
        r = xprice.price(dcp, dcpx)
        assert dcp["p_x"] == round(xdc, 4) and r in ("value", "low", "weak") and dcp["p_model"] == 0.72
        # --- pari réel remis au prix 1xBet : ses avis (p_x, p_sharp) sont posés pour le journal ---
        real = sharp | {"odds": 1.3, "best_odds": 1.35, "edge": 0.0, "score": 0.1, "typ_odds": None, "weight": 1.0, "home": "A", "away": "B"}
        assert xprice._pick_price(real, px, refs) and real["p_x"] == round(p_x, 4) and real["p_sharp"] == 0.74
    finally:
        C.ENSEMBLE_ENABLED = False
        C.ENSEMBLE_ACTIVE_FAMILIES = old_active
        fresh()
    # retour à l'option désactivée : plus rien d'ensemble
    c = copy.deepcopy(base)
    assert xprice.price(c, px, refs) in ("value", "low") and "p_model" not in c and "p_ens" not in c
    C.ENSEMBLE_ENABLED = True
    print("ok  chemin ensemble de xprice : p_ens dans prob, p_model à part, veto, min_odds cohérent, re-pricing stable, sharp attaché")


# ---------- écarts sharp ----------

def pick_sharp(n, **kw):
    c = {"key": f"e{n}|h2h|||Home{n}", "id": f"e{n}", "sport_key": "soccer_epl", "group": "soccer", "event": f"Home{n} - Away{n}", "home": f"Home{n}",
         "away": f"Away{n}", "league": "EPL", "start": "19:00", "commence": (NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "market": "h2h", "who": None, "point": None, "name": f"Home{n}", "pick": f"Home{n} gagne", "prob": 0.52, "prob_book": 0.50, "prob_src": "pinnacle",
         "n_sharp": 1, "odds": 1.9, "best_odds": 2.0, "book": "Betfair", "n_books": 18, "xodds": 2.1, "xref": [900 + n, 1, 0], "xgid": 900 + n,
         "sharp_at": (NOW - timedelta(minutes=20)).isoformat()}
    return c | kw


def test_sharpgap():
    fresh()
    today = {"day": "2026-10-09", "xbet_at": "14:40", "picks": [], "pronos": []}
    ok = pick_sharp(1)                                                          # 2,1 x 0,50 - 1 = +5 %
    assert abs(2.1 * 0.50 - 1 - 0.05) < 1e-9
    today["picks"] = [ok]
    res = sharpgap.scan(today, NOW, history={})
    assert [x["key"] for x in res["cands"]] == [ok["key"]] and res["n_picks"] == 1 and not res["rejected"]
    x = res["cands"][0]
    assert x["edge"] == 0.05 and x["p_sharp"] == 0.5 and x["fair_odds"] == 2.0 and x["xodds"] == 2.1 and x["xref"] == [901, 1, 0] and x["xgid"] == 901
    assert x["age_min"] == 20 and x["x_age_min"] == 20 and x["mins_to_start"] == 180 and x["src"] == "pinnacle" and x["key"] == ok["key"]
    assert abs(x["kelly_f"] - 0.05 / 1.1) < 1e-4
    d = sharpgap.describe(x)
    assert d["title"] == "Home1 gagne" and d["line"] == "1xBet 2,10 · juste 2,00 (Pinnacle) · +5,0 %" and "il y a 20 min" in d["age"] and "dans 180 min" in d["age"], d
    assert sharpgap.find(today, NOW, history={}) == res["cands"]
    # filtres : chacun compté sous sa raison
    cases = {
        "no_sharp": pick_sharp(2, prob_src="consensus", n_sharp=0),
        "few_sharp": pick_sharp(3, prob_src="sharp", n_sharp=1),
        "no_xodds": pick_sharp(4, xodds=None),
        "soon": pick_sharp(5, commence=(NOW + timedelta(minutes=C.SHARPGAP_SOON_MIN - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "started": pick_sharp(6, commence=(NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "stale": pick_sharp(7, sharp_at=(NOW - timedelta(minutes=C.SHARPGAP_MAX_AGE_MIN + 1)).isoformat()),
        "age_unknown": pick_sharp(8, sharp_at=None),
        "edge": pick_sharp(9, xodds=2.05),                                      # +2,5 % < 3 %
        "suspect": pick_sharp(10, xodds=3.0),                                   # +50 %
        "x_stale": pick_sharp(11, x_at=(NOW - timedelta(minutes=C.SHARPGAP_MAX_X_AGE_MIN + 1)).isoformat()),
    }
    today["picks"] = list(cases.values())
    res = sharpgap.scan(today, NOW, history={})
    assert res["cands"] == [] and res["n_picks"] == len(cases)
    got = dict(res["rejected"])
    assert got == {"no_sharp": 1, "few_sharp": 1, "no_xodds": 1, "soon": 2, "stale": 1, "age_unknown": 1, "edge": 1, "suspect": 1, "x_stale": 1}, got
    assert {r["why"] for r in res["refused"]} == {"stale", "age_unknown", "x_stale"} and all(r["edge"] >= C.SHARPGAP_EDGE for r in res["refused"])
    # frontières : écart pile au seuil, âge pile au maximum, ≥ 2 books de référence sans Pinnacle
    edge_ok = pick_sharp(12, xodds=round(1.03 / 0.5, 6))
    age_ok = pick_sharp(13, sharp_at=(NOW - timedelta(minutes=C.SHARPGAP_MAX_AGE_MIN)).isoformat())
    two = pick_sharp(14, prob_src="sharp", n_sharp=2)
    today["picks"] = [edge_ok, age_ok, two]
    assert {x["key"] for x in sharpgap.find(today, NOW, history={})} == {edge_ok["key"], age_ok["key"], two["key"]}
    # l'âge vient de odds_history.json quand le pari n'a pas de « sharp_at »
    old = pick_sharp(15, sharp_at=None)
    hist = {old["key"]: {"t": (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M+00:00")}}
    today["picks"] = [old]
    assert sharpgap.find(today, NOW, history=hist)[0]["age_min"] == 30 and sharpgap.find(today, NOW, history={}) == []
    hist[old["key"]]["t"] = (NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M+00:00")     # relevé du matin, l'après-midi
    res = sharpgap.scan(today, NOW, history=hist)
    assert res["cands"] == [] and res["rejected"]["stale"] == 1 and res["refused"][0]["age_min"] == 720
    # cote 1xBet absente du pari : reprise du même pari chez un prono relevé par xprice (même identité, ordre des équipes sans importance)
    dc = pick_sharp(16, market="double_chance", name="Draw or Home16", key="e16|double_chance|||Draw or Home16", xodds=None, xref=None, xgid=None)
    prono = {"id": "e16", "market": "double_chance", "name": "Home16 or Draw", "point": None, "xodds": 2.2, "xref": [7, 4, 0], "xgid": 7}
    today["picks"], today["pronos"] = [dc], [prono]
    got = sharpgap.find(today, NOW, history={})
    assert len(got) == 1 and got[0]["xodds"] == 2.2 and got[0]["xref"] == [7, 4, 0] and abs(got[0]["edge"] - 0.1) < 1e-9
    assert sharpgap.find(today, NOW, history={}, use_pronos=False) == []
    # classement par écart, un seul pari par match et marché
    a = pick_sharp(17, xodds=2.2)
    b = pick_sharp(18, xodds=2.1)
    same = pick_sharp(17, key="e17|h2h|||Draw", name="Draw", xodds=2.08, pick="Nul")
    other = pick_sharp(17, key="e17|totals||2.5|Over", market="totals", name="Over", point=2.5, xodds=2.1, pick="Plus de 2,5 buts")
    today["picks"], today["pronos"] = [b, same, a, other], []
    got = sharpgap.find(today, NOW, history={})
    assert [x["key"] for x in got] == [a["key"], b["key"], other["key"]], [x["key"] for x in got]       # +10 %, puis deux +5 % dans l'ordre d'entrée
    assert [x["edge"] for x in got] == sorted((x["edge"] for x in got), reverse=True)
    assert {(x["id"], x["market"]) for x in got} == {("e17", "h2h"), ("e18", "h2h"), ("e17", "totals")} and len(got) == 3
    assert len(sharpgap.find(today, NOW, history={}, per_match=False)) == 4
    # seuils réglables
    assert len(sharpgap.find(today, NOW, history={}, min_edge=0.08)) == 1 and sharpgap.find(today, NOW, history={}, max_age=5) == []
    print("ok  écarts sharp : filtres (Pinnacle ou 2 books, cote 1xBet, imminent, âge, 1xBet périmé, suspect), âge via historique, classement")


def main():
    test_devig()
    test_pool_and_renormalisation()
    test_learning_is_slow_and_shrunk()
    test_value_rule_and_veto()
    test_attach_and_log()
    test_xprice_switched_off_is_identical()
    test_xprice_opt_in_path()
    test_sharpgap()
    print("tests ensemble : OK")


if __name__ == "__main__":
    main()
