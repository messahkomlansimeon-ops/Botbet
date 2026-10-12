"""Corrections du modèle de paris (hors ligne, données synthétiques) :
  1. porte d'adoption du modèle CLV : test hors échantillon valable (>= 30 lignes sur >= 5 jours), jamais sur l'apprentissage, marge exigée, ridge
     proportionnel au nombre de variables, marchés / books rares regroupés dans « other » ;
  2. calibration de Platt : activée seulement si le Brier s'améliore hors échantillon (validation croisée par blocs de jours) ;
  3. journal des avis de l'ensemble : la lecture de la décision reste, sharp périmé exclu, journal borné ;
  4. raison « stats » d'une jambe de combiné : refusée là où l'ensemble décide (sauf accord) et quand le modèle de buts ne vaut rien dans la ligue ;
     une valeur manquante ne plante plus ;
  5. buts d'une équipe (team_goals) décidés par l'ensemble comme les autres buts ;
  6. réglage : demi-vie choisie sur la 1re moitié du test, poids des buts mesuré sur la 2e, jamais appliqué aux 2 marquent ;
  7. CLV gratuit : on compare Pinnacle à Pinnacle et la moyenne à la moyenne ;
  8. gain d'un combiné à vérifier noté : cote notée payée telle quelle si aucune jambe n'est annulée ;
  9. fenêtre des stats : la relecture 1xBet ne fait que fusionner ses champs (rien d'écrit entre-temps n'est perdu).
Lancer : python -m tests.test_model_fixes"""
import copy
import os
import random
import tempfile
from datetime import date, datetime, timedelta, timezone

from tests import test_stats_window as W      # fixe DATA_DIR (temporaire), neutralise Telegram ; jeu d'essai de la fenêtre des stats

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-modelfix-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import bankroll, calib, combos as CB, config as C, ensemble as E, freeclose, learn, odds, prematch, statswindow, store, strength, tune, xprice  # noqa: E402
from kelly.sources import footballdata as fd  # noqa: E402

UTC = timezone.utc


def close(a, b, eps=1e-9):
    return abs(a - b) <= eps


class Cfg:
    """Réglages de config remplacés le temps d'un bloc."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: getattr(C, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(C, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(C, k, v)


# ---------- 1. modèle CLV ----------

def clv_rows(days, per_day, seed=1, heur_noise=0.08, signal=0.5, rare=0):
    """Paris suivis synthétiques : CLV = signal x valeur + bruit ; le score heuristique est la valeur bruitée. `rare` : marchés différents vus 3 fois."""
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        day = (date(2026, 9, 1) + timedelta(d)).isoformat()
        for i in range(per_day):
            edge = rng.uniform(0, 0.25)
            f = {"edge": edge, "heuristic": edge * 0.8 + rng.gauss(0, heur_noise), "log_odds": 0.7, "market=h2h": 1.0, "group=soccer": 1.0}
            r = {"date": day, "clv": round(signal * edge + rng.gauss(0, 0.02), 4), "sent": False, "features": f}
            rows.append(r)
    for k in range(rare):                                   # chaque marché rare : 3 lignes
        for j in range(3):
            r = rows[(k * 3 + j) * 2 % len(rows)]
            r["features"].pop("market=h2h", None)
            r["features"][f"market=rare{k}"] = 1.0
    return rows


def test_learn_needs_a_valid_out_of_sample_test():
    # 40 lignes sur 2 jours (le cas d'avant : un test de 3 à 5 lignes, voire sur l'apprentissage) : rien n'est adopté
    few = clv_rows(2, 20)
    one_day = [r | {"date": "2026-09-01"} for r in few]
    tr, te = learn._split(one_day)
    assert te == [] and len(tr) == len(one_day), "un seul jour : aucun test (jamais sur l'apprentissage)"
    out = learn.train_clv(few)
    assert not out["adopted"] and "test hors échantillon valable" in out["reason"] and "test_rows" not in out, out
    # 42 lignes sur 7 jours : le test aurait 5 jours mais l'apprentissage seulement 2 : refusé
    assert not learn.train_clv(clv_rows(7, 6))["adopted"]
    # 12 jours de 12 paris : test = les 5 derniers jours (60 lignes) -> adopté, test réellement hors échantillon
    ok = learn.train_clv(clv_rows(12, 12))
    assert ok["adopted"] and ok["oos_source"] == "réel" and ok["test_rows"] >= C.LEARN_MIN_TEST_ROWS and ok["test_days"] >= C.LEARN_MIN_TEST_DAYS, ok
    assert ok["oos_model"] > ok["oos_heuristic"] + C.LEARN_OOS_MARGIN
    tr, te = learn._split(clv_rows(12, 12), C.LEARN_MIN_TEST_DAYS, C.LEARN_MIN_TRAIN_DAYS)
    assert max(r["date"] for r in tr) < min(r["date"] for r in te) and not set(map(id, tr)) & set(map(id, te))
    # test trop petit (5 jours mais 2 lignes par jour = 10 lignes) : refusé même avec assez de jours
    assert not learn.train_clv(clv_rows(14, 4))["adopted"] or learn.train_clv(clv_rows(14, 4))["test_rows"] >= C.LEARN_MIN_TEST_ROWS
    thin = learn.train_clv(clv_rows(40, 1))                   # 40 lignes, 40 jours, mais 8 lignes de test : refusé
    assert not thin["adopted"] and "test hors échantillon valable" in thin["reason"], thin
    # un modèle déjà adopté n'est pas perdu parce que le test est (momentanément) trop petit
    prev = {"adopted": True, "oos_source": "réel", "names": ["edge"], "mean": [0], "std": [1], "w": [1, 0]}
    kept = learn.train_clv(few, prev)
    assert kept["adopted"] and kept["names"] == ["edge"] and "ancien modèle conservé" in kept["reason"], kept
    print("ok  1a test hors échantillon valable exigé, jamais sur l'apprentissage")


def test_learn_margin_and_ridge_and_rare_buckets():
    # heuristique parfaite : le modèle ne fait pas mieux -> pas adopté (avant : « >= » adoptait sur une égalité)
    perfect = learn.train_clv(clv_rows(12, 12, heur_noise=0.0))
    assert not perfect["adopted"] and "marge" in perfect["reason"], perfect
    with Cfg(LEARN_OOS_MARGIN=1.0):                           # marge énorme : même un vrai signal n'est pas adopté
        assert not learn.train_clv(clv_rows(12, 12))["adopted"]
    # ridge proportionnel au nombre de variables
    out = learn.train_clv(clv_rows(12, 12))
    assert close(out["ridge_lambda"], round(C.RIDGE_LAMBDA * len(out["names"]), 2)), out
    # marchés vus 3 fois (< LEARN_RARE_MIN) : regroupés dans « other », pas une variable chacun
    rows = clv_rows(12, 12, rare=10)
    m = learn.train_clv(rows)
    assert "market=other" in m["names"] and not any(n.startswith("market=rare") for n in m["names"]), m["names"]
    assert "market=h2h" in m["names"], "le marché fréquent garde sa variable"
    if m["adopted"]:
        base = {"edge": 0.1, "heuristic": 0.1, "log_odds": 0.7, "group=soccer": 1.0}
        assert close(learn.predict(m, base | {"market=jamais_vu": 1.0}), learn.predict(m, base | {"market=other": 1.0})), "marché inconnu : variable « other »"
        assert learn.predict(m, base | {"market=h2h": 1.0}) != learn.predict(m, base | {"market=other": 1.0})
    # beaucoup de marchés rares + CLV pur bruit : jamais adopté (avant : variables à écart-type minuscule interpolées par le ridge)
    adopted = 0
    for seed in range(12):
        noise = clv_rows(14, 12, seed=seed, signal=0.0, rare=25)
        adopted += learn.train_clv(noise)["adopted"]
    assert adopted <= 3, f"{adopted} modèles de bruit adoptés sur 12"          # ~10 % attendus (avant : plus de la moitié)
    # les books : au plus TOP_BOOKS, les rares dans « other »
    brows = clv_rows(12, 12)
    for i, r in enumerate(brows):
        r["features"][f"book={'B' + str(i % 3) if i % 8 else 'Rare' + str(i)}"] = 1.0
    names = learn.train_clv(brows)["names"]
    assert "book=other" in names and not any(n.startswith("book=Rare") for n in names) and {"book=B0", "book=B1", "book=B2"} <= set(names), names
    print("ok  1b marge, ridge par variable, marchés et books rares regroupés")


# ---------- 2. calibration ----------

def test_platt_needs_out_of_sample_gain():
    rng = random.Random(5)
    # probas justes : Platt gagne un peu sur ses propres lignes (bruit) mais pas hors échantillon -> inactif
    fair_rows = []
    for i in range(400):
        p = rng.uniform(0.35, 0.75)
        fair_rows.append({"market": "h2h", "date": (date(2026, 9, 1) + timedelta(i // 10)).isoformat(), "prob_book": p, "prob": p,
                          "result": "win" if rng.random() < p else "loss"})
    cal = learn.calibrate(fair_rows)["resultat"]
    assert cal["brier_after"] <= cal["brier_before"], cal                      # toujours vrai sur ses propres lignes : l'ancien critère
    assert not cal["active"] and "oos_before" in cal, cal
    # probas trop optimistes : gain réel, aussi hors échantillon -> actif
    off = []
    for i in range(400):
        p = rng.uniform(0.35, 0.75)
        off.append({"market": "h2h", "date": (date(2026, 9, 1) + timedelta(i // 10)).isoformat(), "prob_book": p, "prob": p,
                    "result": "win" if rng.random() < max(0.05, p - 0.12) else "loss"})
    cal = learn.calibrate(off)["resultat"]
    assert cal["active"] and cal["oos_after"] < cal["oos_before"] - C.CALIB_OOS_MARGIN, cal
    # lignes sans date (ordre = chronologie) : même résultat
    nodate = [{k: v for k, v in r.items() if k != "date"} for r in off]
    assert learn.calibrate(nodate)["resultat"]["active"]
    # sous le minimum : inactif
    assert learn.calibrate(off[:C.CALIB_MIN_ROWS - 1])["resultat"] == {"n": C.CALIB_MIN_ROWS - 1, "active": False}
    # un jour seul : pas de blocs de jours -> pas d'activation (rien à valider hors échantillon)
    one_day = [dict(r, date="2026-09-01") for r in off]
    assert not learn.calibrate(one_day)["resultat"]["active"]
    print("ok  2 Platt activé seulement si le Brier baisse hors échantillon")


# ---------- 3. journal des avis de l'ensemble ----------

KO = "2030-01-01T19:00:00Z"
NOW = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)


def op(i=1, **kw):
    """Pronostic de buts avec ses trois avis (modèle, 1xBet, sharp lu à 08:30)."""
    return {"key": f"m{i}|totals||3.5|Under", "id": f"m{i}", "sport_key": "soccer_epl", "home": "H", "away": "A", "commence": KO, "market": "totals",
            "name": "Under", "point": 3.5, "prob": 0.7, "p0": 0.7, "xodds": 1.8, "p_model": 0.7, "p_x": 0.62, "p_sharp": 0.64,
            "psh": {"under|3.5": [0.64, "pinnacle", 1, "2030-01-01T08:30:00Z"]}} | kw


def test_ensemble_log_keeps_the_decision_read_and_drops_stale_sharp():
    store.save(E.LOG_FILE, [])
    E.reset()
    # lecture du matin (09:00) : les trois avis, une heure
    assert E.record([op()], "2030-01-01", NOW) == 1
    row = store.load(E.LOG_FILE, [])[0]
    assert (row["pm"], row["px"], row["ps"]) == (0.7, 0.62, 0.64) and row["at"] == "2030-01-01T09:00+00:00", row
    # relevé du soir (17:00) : 1xBet a bougé, le sharp du matin est resté -> la lecture de la décision reste, la relecture va dans « late »
    evening = datetime(2030, 1, 1, 17, 0, tzinfo=UTC)
    assert E.record([op(p_x=0.50)], "2030-01-01", evening) == 0, "sharp de 08:30 : 8 h, périmé à 17:00"
    row = store.load(E.LOG_FILE, [])[0]
    assert row["px"] == 0.62 and row["ps"] == 0.64 and "late" not in row, row         # sharp périmé : même pas de « late » (avis de moments différents)
    soon = datetime(2030, 1, 1, 10, 0, tzinfo=UTC)
    assert E.record([op(p_x=0.60, p_sharp=0.65)], "2030-01-01", soon) == 0
    row = store.load(E.LOG_FILE, [])[0]
    assert row["px"] == 0.62 and row["ps"] == 0.64 and row["late"]["px"] == 0.6 and row["late"]["ps"] == 0.65, row
    # un nouveau pronostic dont le sharp est périmé à l'enregistrement n'entre pas dans l'apprentissage
    store.save(E.LOG_FILE, [])
    assert E.record([op(2)], "2030-01-01", evening) == 0 and store.load(E.LOG_FILE, []) == []
    assert E.sharp_stale(op(2), evening) and not E.sharp_stale(op(2), NOW)
    # un sharp d'heure inconnue est accepté (comme sharp_for) ; sans sharp du tout, modèle + 1xBet entrent
    nohour = op(3, psh={"under|3.5": [0.64, "pinnacle", 1, None]})
    assert E.record([nohour, op(4, p_sharp=None, psh=None)], "2030-01-01", evening) == 2
    # pari réel : l'heure du sharp est c["sharp_at"]
    real = {"key": "r|h2h|||A", "id": "r", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": KO, "market": "h2h", "name": "A", "point": None,
            "prob": 0.5, "prob_book": 0.5, "prob_src": "pinnacle", "n_sharp": 1, "p_x": 0.48, "p_model": 0.55, "sharp_at": "2030-01-01T08:00:00Z"}
    store.save(E.LOG_FILE, [])
    assert E.record([real], "2030-01-01", NOW) == 1 and E.record([dict(real, key="r2|h2h|||A")], "2030-01-01", evening) == 0
    print("ok  3a journal : lecture de la décision gardée, sharp périmé exclu, relecture tardive à part")


def test_ensemble_log_is_bounded():
    day = date(2030, 1, 20)
    mk = lambda i, d, **kw: {"key": f"k{i}", "date": str(d), "result": None} | kw        # noqa: E731
    log = [mk(0, day - timedelta(days=C.ENSEMBLE_LOG_DAYS + 1)),                          # trop vieille
           mk(1, day - timedelta(days=5), result="win", ens=True),                        # réglée et apprise depuis plus de 2 jours : résumée par ensemble.json
           mk(2, day - timedelta(days=1), result="win", ens=True),                        # apprise hier : gardée
           mk(3, day - timedelta(days=5)),                                                # en attente : gardée
           mk(4, day, result="loss")]                                                     # réglée, pas encore apprise : gardée
    assert [r["key"] for r in E.prune_log(log, day)] == ["k2", "k3", "k4"]
    with Cfg(ENSEMBLE_LOG_MAX_ROWS=3):
        big = [mk(i, day - timedelta(days=i % 3), **({"result": "win", "ens": True} if i % 4 == 0 else {})) for i in range(10)]
        out = E.prune_log(big, day)
        assert len(out) == 3 and all(not r.get("ens") for r in out), out                  # les apprises partent d'abord
        pend = [mk(i, day - timedelta(days=i % 5)) for i in range(10)]
        out = E.prune_log(pend, day)
        assert len(out) == 3 and all(r["date"] >= sorted(p["date"] for p in pend)[-4] for r in out)      # puis les plus vieilles
    # record et settle_log appliquent le plafond
    with Cfg(ENSEMBLE_LOG_MAX_ROWS=2):
        store.save(E.LOG_FILE, [])
        E.record([op(i) for i in range(5)], "2030-01-01", NOW)
        assert len(store.load(E.LOG_FILE, [])) == 2
    store.save(E.LOG_FILE, [])
    print("ok  3b journal borné (âge, lignes apprises, plafond)")


# ---------- 4. raison « stats » des jambes ----------

def leg(i=1, market="totals", edge=0.0, **kw):
    return {"key": f"k{i}", "id": f"e{i}", "odds": 2.3, "prob": 0.45, "edge": edge, "n_books": 14, "weight": 1.0, "score": 0.0, "market": market,
            "name": "Over", "point": 2.5, "why": "", "sport_key": "soccer_epl", "pm": {"kind": "goals", "pgap": 0.07}} | kw


def test_stats_reason_not_in_ensemble_families():
    corners = leg(1, "alternate_totals_corners", pm={"kind": "corners", "pgap": 0.07})
    assert CB.leg_reason(corners) == "stats", "corners : l'ensemble ne décide pas, le signal du modèle reste une raison"
    assert CB.leg_reason(leg(2, "totals")) is None, "buts : le désaccord modèle-marché perd 9 à 16 % -> plus une raison"
    assert CB.leg_reason(leg(3, "btts", pm={"kind": "btts", "pgap": 0.07})) is None
    assert CB.leg_reason(leg(4, "h2h", name="Home", pm={"kind": "h2h", "xgd": 0.9}, prob=0.5, odds=1.9)) is None
    with Cfg(ENSEMBLE_ACTIVE_FAMILIES=frozenset()):                                     # ensemble en fantôme : comme avant
        assert CB.leg_reason(leg(2, "totals")) == "stats"
    with Cfg(ENSEMBLE_ENABLED=False):
        assert CB.leg_reason(leg(2, "totals")) == "stats"
    # l'ensemble valide aussi le pari (cote 1xBet x proba retenue >= 1,03, pas de veto) : la raison tient
    agree = leg(5, "totals", prob_book=0.60, prob_src="pinnacle", n_sharp=1, p_model=0.62, p_x=0.58, xodds=1.85)
    assert E.value(agree)["ok"] and CB.leg_reason(agree) == "stats", E.value(agree)
    cheap = dict(agree, xodds=1.5)                                                       # 1xBet paie trop peu : l'ensemble refuse
    assert not E.value(cheap)["ok"] and CB.leg_reason(cheap) is None
    assert CB.leg_reason({k: v for k, v in agree.items() if k != "xodds"}) is None, "pas de cote 1xBet : pas d'accord possible"
    # le modèle de buts ne vaut pas le marché dans la ligue (poids mesuré 0) : jamais « stats » sur buts / 1N2 / 2 marquent, même hors ensemble
    store.save(tune.FILE, {"half_life": {}, "goal_weight": {"E0": 0.0}, "report": {}})
    strength.reload()
    try:
        assert prematch.goal_weight({"sport_key": "soccer_epl"}) == 0.0
        with Cfg(ENSEMBLE_ACTIVE_FAMILIES=frozenset()):
            assert CB.leg_reason(leg(2, "totals")) is None
            assert CB.leg_reason(leg(6, "totals", sport_key="soccer_spain_la_liga")) == "stats", "ligue non mesurée : poids de config"
            assert CB.leg_reason(corners) == "stats", "corners : le poids des buts n'a rien à y voir"
    finally:
        store.remove(tune.FILE)
        strength.reload()
    # valeur manquante : plus de KeyError ni de 0 inventé
    nopt = {k: v for k, v in leg(7, "alternate_totals_corners", pm={"kind": "corners", "pgap": 0.07}).items() if k != "edge"}
    assert CB.leg_reason(nopt) is None
    assert CB.quality_legs([nopt, corners]) == [corners]
    print("ok  4 « stats » refusée sur les familles de l'ensemble (sauf accord) et là où le modèle de buts ne vaut rien ; edge manquant")


# ---------- 5. buts d'une équipe ----------

def test_team_goals_decided_by_the_ensemble():
    assert "team_goals" in C.ENSEMBLE_ACTIVE_FAMILIES
    for m in ("totals", "btts", "double_chance", "h2h", "team_totals_home", "team_totals_away"):         # tous les marchés de buts de Kelly
        assert calib.family(m) in C.ENSEMBLE_ACTIVE_FAMILIES, (m, calib.family(m))
        assert E.is_active(m), m
    assert E.prior_model("team_goals") == 0.12 and C.ENSEMBLE_PRIOR_SD["team_goals"] == 0.07            # le marché pèse 88 %, comme les autres buts
    assert not E.is_active("alternate_totals_corners") and not E.is_active("team_shots_total")
    # un pronostic de buts d'une équipe passe par l'ensemble : proba retenue p_ens, jamais le modèle seul
    c = {"key": "t|team_totals_home||1.5|Over", "id": "t", "sport_key": "soccer_epl", "home": "H", "away": "A", "commence": KO, "market": "team_totals_home",
         "name": "Over", "point": 1.5, "prob": 0.8, "p0": 0.8, "min_odds": 1.3, "score": 0.1, "alts": [["Over", 1.5, 0.8, None]], "pick": "x", "group": "soccer"}
    px = {("team_totals_home", "Over", 1.5): 1.5, ("team_totals_home", "Under", 1.5): 2.6}
    store.save(E.FILE, {})
    E.reset()
    r = xprice.price(c, px, {k: [9, 1, 0] for k in px})
    assert "p_ens" in c and c["p_ens"] == c["prob"] and c["p_ens"] < 0.8 and c["p_model"] == 0.8, c           # tirée vers le marché (1xBet sans marge ~ 0,63)
    assert r in ("low", "weak"), "1xBet seul ne peut pas donner de valeur : pas de 💎 sans avis sharp"
    print("ok  5 buts d'une équipe décidés par l'ensemble (a priori 12 %)")


# ---------- 6. réglage ----------

def fake_recs(size, good_first):
    """Prédictions walk-forward fabriquées : `good_first` -> la demi-vie est meilleure sur la 1re moitié, pire sur la 2e."""
    recs = []
    for i in range(size):
        first = i < size // 2
        p_true = 0.55 if first == good_first else 0.30
        p3 = [p_true, (1 - p_true) / 2, (1 - p_true) / 2]
        recs.append((0, 1, p3, 0.6, [0.4, 0.3, 0.3], 0.55))
    return recs


def test_tune_separates_half_life_and_weight_samples():
    old_eval = tune.evaluate
    recs = {110: fake_recs(400, good_first=False), 330: fake_recs(400, good_first=True)}      # 330 gagne la 1re moitié ; 110 la 2e
    tune.evaluate = lambda ms, tf, h, deadline=None, step_days=None, div=None: recs[h]
    ms = [{"date": "2022-01-01", "hg": 1, "ag": 0}, {"date": "2024-01-01", "hg": 2, "ag": 1}]
    try:
        with Cfg(TUNE_HALF_LIVES=(110, 330), TUNE_MIN_TEST=100, TUNE_HL_SHARE=0.5):
            r = tune.tune_div("E0", ms)
            assert r["n"] == 400 and r["n_hl"] == 200, r                                   # demi-vie choisie sur 200 matchs
            assert r["chosen"] == 330, r                                                   # la 1re moitié décide (sur les 400 entiers, 110 et 330 se valent)
            assert r["gw"]["n1"] == 200 and r["gw"]["no"] == 200, r["gw"]                  # poids mesuré sur l'AUTRE moitié seulement
            whole = tune.weight_curves(recs[330])
            assert whole["n1"] == 400 and r["gw"] != whole
            # la courbe du poids est exactement celle de la 2e moitié de la demi-vie retenue
            assert r["gw"] == tune.weight_curves(recs[330][200:])
    finally:
        tune.evaluate = old_eval
    # les 2 marquent n'utilisent jamais le poids des buts (plus/moins de 2,5) mesuré
    store.save(tune.FILE, {"half_life": {}, "goal_weight": {"E0": 0.45}, "report": {}})
    strength.reload()
    try:
        assert prematch.goal_weight({"sport_key": "soccer_epl"}) == 0.45
        assert prematch.btts_weight({"sport_key": "soccer_epl"}) == C.MODEL_WEIGHT["goals"]
    finally:
        store.remove(tune.FILE)
        strength.reload()
    print("ok  6 demi-vie choisie sur la 1re moitié, poids des buts mesuré sur la 2e, 2 marquent à la valeur de config")


# ---------- 7. CLV gratuit ----------

def fc_row(key, src, prob, **kw):
    return {"key": key, "id": key, "sport_key": "soccer_epl", "group": "soccer", "home": "Arsenal", "away": "Chelsea", "market": "h2h", "name": "Arsenal",
            "point": None, "commence": "2026-10-11T15:00:00Z", "prob": prob, "prob_book": prob, "prob_src": src, "sent": False, "closed_at": None,
            "clv": None, "result": None} | kw


class FD:
    def __init__(self, **books):
        self.books = books

    def match(self, b):
        return {"odds": {"1x2": {"close": self.books}}}


def test_free_close_compares_like_with_like():
    now = datetime(2026, 10, 12, 8, tzinfo=UTC)
    prices = [1.8, 3.8, 4.6]
    p = odds.shin_probs(prices)[0]
    # marché immobile, même source des deux côtés : CLV 0
    a = fc_row("a", "pinnacle", p)
    assert freeclose.fill([a], now, FD(ps=prices), {}) == 1 and abs(a["clv"]) < 1e-4 and a["close_src"] == "football-data/ps"
    b = fc_row("b", "consensus", p)
    assert freeclose.fill([b], now, FD(avg=prices), {}) == 1 and abs(b["clv"]) < 1e-4 and b["close_src"] == "football-data/avg"
    # sources différentes : sautées (avant : Pinnacle d'abord, sinon la moyenne, sans regarder d'où venait la proba prise)
    c = fc_row("c", "pinnacle", p)
    assert freeclose.fill([c], now, FD(avg=prices), {}) == 0 and c["clv"] is None
    d = fc_row("d", "consensus", p)
    assert freeclose.fill([d], now, FD(ps=prices, avg=[1.5, 4.0, 5.0]), {}) == 1 and d["close_src"] == "football-data/avg", "consensus : la moyenne, pas Pinnacle"
    assert abs(d["close_prob"] - round(odds.shin_probs([1.5, 4.0, 5.0])[0], 4)) < 1e-9
    e = fc_row("e", None, p)
    f = {k: v for k, v in fc_row("f", "pinnacle", p).items() if k != "prob_src"}
    g = fc_row("g", "sharp", p)                                                          # autre book de référence : pas Pinnacle, pas une moyenne
    assert freeclose.fill([e, f, g], now, FD(ps=prices, avg=prices), {}) == 0
    # BetExplorer (moyenne de books) : seulement pour une proba prise au consensus
    bx = {"leagues": {"soccer_epl": {"matches": [{"d": "2026-10-11", "home": "Arsenal", "away": "Chelsea", "o1": 1.8, "ox": 3.8, "o2": 4.6}]}}}
    h, i = fc_row("h", "pinnacle", p), fc_row("i", "consensus", p)
    assert freeclose.fill([h, i], now, type("N", (), {"match": lambda self, b: None})(), bx) == 1 and h["clv"] is None and i["close_src"] == "betexplorer"
    print("ok  7 CLV gratuit : Pinnacle contre Pinnacle, moyenne contre moyenne, sinon ligne sautée")


# ---------- 8. gain d'un combiné à vérifier noté ----------

def fair_combo(results, real=True, odds_=(1.55, 1.55, 1.55, 1.55), played_odds=None):
    legs = [{"key": f"k{i}", "min_odds": m, "xodds": m if real else None, "odds": m if real else None, "result": r} for i, (m, r) in enumerate(zip(odds_, results))]
    if not real:
        for l in legs:
            l.pop("xodds"), l.pop("odds")
    total = round(1.55 ** 4, 2) if real else round(1.55 ** 4, 2)                         # 5,77 : le total arrondi de 5,772...
    return {"result": "win", "min_odds": total, "legs": legs, "played": {"odds": played_odds or total, "stake": 1000}}


def test_played_gain_no_rounding_error_when_all_legs_win():
    for real in (True, False):
        x = fair_combo(("win",) * 4, real=real)
        assert bankroll.void_ratio(x) == 1.0
        assert close(bankroll.played_gain(x), 1000 * (5.77 - 1)), (real, bankroll.played_gain(x))         # avant : 1000 x (5,77 x 5,772/5,77 - 1) = +2,06 FCFA de trop
        x["played"]["odds"] = 5.5
        assert close(bankroll.played_gain(x), 1000 * (5.5 - 1))
    # jambe annulée : cote recalculée sur les cotes exactes des jambes (produit des gagnantes / produit de toutes)
    for real in (True, False):
        x = fair_combo(("win", "win", "void", "win"), real=real)
        assert close(bankroll.void_ratio(x), 1 / 1.55), real
        assert close(bankroll.played_gain(x), 1000 * (5.77 / 1.55 - 1))
    # cotes de jambes différentes (1xBet) : le total de la ligne est arrondi, le ratio ne dépend pas de son arrondi
    x = fair_combo(("win", "void", "win", "win"), odds_=(1.61, 1.37, 2.03, 1.19))
    assert close(bankroll.void_ratio(x), 1 / 1.37) and close(bankroll.played_gain(x), 1000 * (x["played"]["odds"] / 1.37 - 1))
    # perdu, aucune jambe gagnée, push : inchangé
    assert bankroll.played_gain(dict(fair_combo(("win",) * 4), result="loss")) == -1000
    assert bankroll.played_gain(fair_combo(("void",) * 4)) == 0.0
    assert bankroll.played_gain(dict(fair_combo(("win",) * 4), result="void")) == 0.0
    # jambe sans cote exploitable : repli sur le total de la ligne (ancien calcul)
    x = fair_combo(("win", "void", "win", "win"))
    x["legs"][1].pop("min_odds"), x["legs"][1].pop("xodds"), x["legs"][1].pop("odds")
    assert bankroll.played_gain(x) is not None
    print("ok  8 combiné à vérifier : cote notée payée telle quelle sans jambe annulée, ratio exact sinon")


# ---------- 9. fenêtre des stats : fusion des seuls champs relus ----------

def test_merge_read_unit():
    before = {"key": "k", "name": "Under", "point": 9.5, "prob": 0.66, "xnone": 1, "p_sharp": 0.5, "alts": [["Under", 9.5, 0.66, None]]}
    after = {"key": "k", "name": "Under", "point": 9.5, "prob": 0.66, "p_sharp": 0.5, "xodds": 1.85, "xodds0": 1.85, "xev": 0.2, "xat": "17:05", "alts": before["alts"]}
    cur = copy.deepcopy(before) | {"alts": [["Under", 9.5, 0.66, None], ["Under", 10.5, 0.7, None]], "xodds0": 1.7, "marque": True, "p_sharp": 0.55}
    out = statswindow.merge_read(cur, before, after)
    assert out is cur
    assert cur["xodds"] == 1.85 and cur["xev"] == 0.2 and cur["xat"] == "17:05" and "xnone" not in cur, cur                       # champs relus : repris
    assert cur["xodds0"] == 1.7, "cote du matin écrite entre-temps : gardée (ligne inchangée)"
    assert len(cur["alts"]) == 2 and cur["marque"] is True, "lignes alts et marques écrites entre-temps : gardées"
    assert cur["p_sharp"] == 0.55, "proba sharp réécrite entre-temps : gardée, la relecture ne l'a pas changée"
    # la ligne a changé : sa cote du matin est celle de la nouvelle ligne
    moved = after | {"name": "Under", "point": 10.5, "xodds0": 1.95}
    cur = copy.deepcopy(before) | {"xodds0": 1.7}
    statswindow.merge_read(cur, before, moved)
    assert cur["point"] == 10.5 and cur["xodds0"] == 1.95
    # un champ supprimé par la relecture l'est aussi chez le pronostic rechargé ; sans cote du matin, la relecture la pose
    cur = copy.deepcopy(before)
    statswindow.merge_read(cur, before, {k: v for k, v in after.items() if k != "xodds0"} | {"xodds0": 1.85})
    assert cur["xodds0"] == 1.85 and "xnone" not in cur
    print("ok  9a merge_read : seuls les champs relus sont repris")


def test_stats_window_keeps_fields_written_during_the_read():
    W.setup()
    fx = W.Fx()
    key = "M1|team_fouls_total||9.5|Under"

    def fetch(events, deadline=None, wanted=None, **kw):
        out = fx(events, deadline=deadline, wanted=wanted)
        t = store.load("today.json", {})                                  # un autre passage écrit pendant la relecture (sans verrou)
        c = next(c for c in t["pronos"] if c["key"] == key)
        c.update(xodds0=1.70, p_sharp=0.55, alts=[["Under", 9.5, 0.66, None], ["Under", 10.5, 0.74, None]], tag="écrit pendant la relecture")
        store.save("today.json", t)
        return out
    rep, sent, _ = W.with_fakes(lambda: W.app.run_stats_window(W.NOW, fetch=fetch))
    c = W.by_key()[key]
    assert c["xodds"] == 1.85 and c.get("xat") == "17:05" and "xnone" not in c, c               # la relecture est bien là
    assert c["xodds0"] == 1.70 and c["p_sharp"] == 0.55 and len(c["alts"]) == 2 and c["tag"] == "écrit pendant la relecture", c
    print("ok  9b fenêtre des stats : xodds0, alts, p_sharp écrits pendant la relecture ne sont plus perdus")


def main():
    test_learn_needs_a_valid_out_of_sample_test()
    test_learn_margin_and_ridge_and_rare_buckets()
    test_platt_needs_out_of_sample_gain()
    test_ensemble_log_keeps_the_decision_read_and_drops_stale_sharp()
    test_ensemble_log_is_bounded()
    test_stats_reason_not_in_ensemble_families()
    test_team_goals_decided_by_the_ensemble()
    test_tune_separates_half_life_and_weight_samples()
    test_free_close_compares_like_with_like()
    test_played_gain_no_rounding_error_when_all_legs_win()
    test_merge_read_unit()
    test_stats_window_keeps_fields_written_during_the_read()
    print("tests corrections du modèle : OK")


if __name__ == "__main__":
    main()
