"""Choix des lignes de stats à la cote 1xBet (hors ligne) : corners, cartons, jaunes, tirs, tirs cadrés et fautes passent par l'ensemble (proba retenue =
mélange de Kelly et de 1xBet sans marge, poids appris) au lieu de la seule proba de Kelly ; la ligne où Kelly s'écarte le plus de 1xBet n'est plus
choisie d'office ; les poids appris (tirs : 1xBet bien meilleur) se répercutent ; touches et hors-jeu gardent la règle d'avant.
Lancer : python -m tests.test_stats_ensemble"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-statsens-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, combos as CB, config as C, ensemble, fmt, store, xprice  # noqa: E402


def prono(market, name, point, p0, alts):
    return {"id": "e1", "sport_key": "soccer_epl", "home": "Alpha", "away": "Bravo", "commence": "2026-10-12T15:00:00Z", "start": "15:00", "league": "PL",
            "market": market, "name": name, "point": point, "who": None, "prob": p0, "p0": p0, "alts": alts, "min_odds": round(1 / p0 * 1.05, 2),
            "conf": 0.6, "n_min": 8, "score": 0.2, "key": f"e1|{market}||{point}|{name}", "pick": fmt.label(market, name, point, None, "soccer")}


def reset():
    store.save(calib.FILE, {})
    store.save(ensemble.FILE, {})
    calib._memo.clear()
    ensemble.reset()


# cotes 1xBet des tirs du match : 1xBet voit plus de 24,5 à 55 %, plus de 22,5 à 70 % (marge ~6 %)
SHOTS = {("team_shots_total", "Over", 22.5): 1.36, ("team_shots_total", "Under", 22.5): 3.10,
         ("team_shots_total", "Over", 24.5): 1.72, ("team_shots_total", "Under", 24.5): 2.10,
         ("team_shots_total", "Over", 26.5): 2.30, ("team_shots_total", "Under", 26.5): 1.58}


def test_families():
    assert {"corners", "cards", "yellow", "shots", "sot", "fouls"} <= C.ENSEMBLE_ACTIVE_FAMILIES
    assert all(ensemble.is_active(m) for m in ("alternate_totals_corners", "team_corners_home", "alternate_totals_cards", "team_yellow_total",
                                               "team_shots_total", "team_sot_away", "team_fouls_home"))
    assert not ensemble.is_active("team_throws_total") and not ensemble.is_active("team_offsides_total")      # touches, hors-jeu : règle d'avant
    print("ok  corners, cartons, jaunes, tirs, tirs cadrés, fautes : décidés par l'ensemble ; touches et hors-jeu : règle d'avant")


def test_blend_and_line_choice():
    reset()
    # Kelly voit plus de 24,5 à 70 % (15 points au-dessus de 1xBet) et plus de 22,5 à 80 % (10 points au-dessus)
    alts = [["Over", 22.5, 0.80, None], ["Over", 24.5, 0.70, None], ["Over", 26.5, 0.52, None]]
    old = C.ENSEMBLE_ACTIVE_FAMILIES
    C.ENSEMBLE_ACTIVE_FAMILIES = old - {"shots"}
    try:
        legacy = prono("team_shots_total", "Over", 24.5, 0.70, alts)
        assert xprice.price(legacy, SHOTS) == "value" and legacy["point"] == 24.5 and legacy["prob"] == 0.7       # avant : le plus gros écart, 70 % annoncés
    finally:
        C.ENSEMBLE_ACTIVE_FAMILIES = old
    c = prono("team_shots_total", "Over", 24.5, 0.70, alts)
    assert xprice.price(c, SHOTS) == "value", c
    px = ensemble.x_fair(SHOTS, "team_shots_total", c["name"], c["point"])
    assert px < c["prob"] < c["p_model"], c                        # proba retenue entre 1xBet et Kelly : plus jamais 70 % sur la seule foi de Kelly
    assert c["p0"] in (0.70, 0.80) and abs(c["prob"] * c["xodds"] - 1 - c["xev"]) < 1e-3 and c["xodds"] * c["prob"] >= 1 + C.ENSEMBLE_MARGIN, c
    # Kelly à peine au-dessus de 1xBet : plus aucune valeur une fois mélangé (avant : joué si la cote >= 1 / proba x 1,05)
    thin = prono("team_shots_total", "Over", 24.5, 0.64, [["Over", 24.5, 0.64, None]])
    assert xprice.price(thin, SHOTS) in ("low", "weak") and not CB.viable(thin), thin
    print("ok  tirs : proba retenue entre Kelly et 1xBet, ligne choisie sur le mélange, petit écart plus joué")


def test_learned_weights():
    reset()
    alts = [["Over", 24.5, 0.70, None]]
    before = prono("team_shots_total", "Over", 24.5, 0.70, alts)
    assert xprice.price(before, SHOTS) == "value"
    # résultats : 1xBet bien meilleur que Kelly sur les tirs (poids du modèle appris très bas)
    ll = [-((i - 0) ** 2) / 4.0 for i in range(40)]
    store.save(ensemble.FILE, {"fam": {"shots": {"m": {"ll": ll, "n": 600}}}, "done": {}})
    ensemble._memo.clear()
    w = ensemble.weights("shots")["wm"]
    assert w < 0.3, w
    after = prono("team_shots_total", "Over", 24.5, 0.70, alts)
    assert xprice.price(after, SHOTS) in ("low", "weak") and not CB.viable(after), after      # Kelly écoutée moins : plus de valeur
    print(f"ok  poids appris (Kelly {round(w * 100)} % sur les tirs) : le désaccord seul ne fait plus un pari")


def test_no_probable_path_for_stats():
    """Poids appris très bas (le marché pèse plus de 70 %) : le chemin « probable » (sans sharp, -10 % toléré) est celui des buts ; une ligne de stats
    doit toujours valoir C.ENSEMBLE_MARGIN au prix 1xBet."""
    reset()
    store.save(ensemble.FILE, {"fam": {"shots": {"m": {"ll": [-((i - 0) ** 2) / 4.0 for i in range(40)], "n": 600}}}, "done": {}})
    ensemble._memo.clear()
    assert 1 - ensemble.weights("shots")["wm"] >= C.ENSEMBLE_VETO_WEIGHT
    c = prono("team_shots_total", "Over", 22.5, 0.72, [["Over", 22.5, 0.72, None]])       # 1xBet 1,36 : espérance au mélange -4 %
    assert xprice.price(c, SHOTS) == "low" and c.get("xkind") is None and not CB.viable(c), c
    v = ensemble.value_of("totals", 0.72, 0.70, None, 1.36)                                  # buts sans sharp : « probable » comme avant
    assert v["ok"] and v["kind"] == "probable", v
    print("ok  stats : jamais « probable » sans valeur au prix 1xBet, même quand Kelly pèse peu ; buts : inchangé")


def main():
    test_families()
    test_blend_and_line_choice()
    test_learned_weights()
    test_no_probable_path_for_stats()
    print("tests choix des lignes de stats : OK")


if __name__ == "__main__":
    main()
