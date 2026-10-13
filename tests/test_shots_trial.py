"""Tirs vers le but « plus de » (13/10, choix de l'utilisateur) : sens séparés refaits pour les marchés comptés avant la séparation ; essai qui reprend
les « plus de » dans les combinés (chance corrigée par LEURS résultats, hors Grèce / Bundesliga 2 / Liga, au plus 1 par combiné, arrêt automatique) ;
les « moins de » restent écartés. Lancer : python -m tests.test_shots_trial"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-trial-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, combos as CB, config as C, status, store  # noqa: E402


def row(i, side, won, p0=0.65, mid=None, sk="soccer_epl"):
    name = "Over" if side == "plus" else "Under"
    return {"key": f"e{i}|team_shots_home||14.5|{name}", "id": mid or f"e{i}", "date": "2026-10-10", "sport_key": sk, "home": "A", "away": "B",
            "market": "team_shots_home", "name": name, "point": 14.5, "p0": p0, "prob": p0, "result": "win" if won else "loss", "calib": True}


def real_like():
    """Comme la vraie base du 13/10 : tirs « plus de » 60 % pour 65 %, « moins de » 32 % ; famille comptée sans cases de sens (avant la séparation)."""
    rows = [row(i, "plus", i < 16) for i in range(26)] + [row(100 + i, "moins", i < 8) for i in range(25)]
    n = len(rows)
    data = {"shots": [n, sum(r["result"] == "win" for r in rows), 0.65 * n], "corners": [40, 28, 26.0], "_cell": {"corners|plus": [20, 15, 13.0]}}
    return data, rows


def test_rebuild_cells():
    data, rows = real_like()
    calib._migrate(data, rows)
    cells = data["_cell"]
    assert cells["shots|plus"][:2] == [26, 16] and cells["shots|moins"][:2] == [25, 8], cells
    assert cells["corners|plus"] == [20, 15, 13.0] and "corners|moins" not in cells         # une famille déjà séparée n'est jamais refaite
    print("ok  sens refaits (« plus de » / « moins de ») pour les marchés comptés avant la séparation ; les autres intacts")


def test_trial():
    data, rows = real_like()
    calib._migrate(data, rows)
    store.save(calib.FILE, data)
    calib.reset()
    p_plus = calib.calibrate("team_shots_home", 0.70, side="plus")
    p_moins = calib.calibrate("team_shots_home", 0.70, side="moins")
    assert abs(p_plus - (0.5 + 0.2 * (16 / 26 - 0.5) / 0.15)) < 1e-6 and p_moins < 0.60, (p_plus, p_moins)     # « plus de » : SA correction ; « moins de » : la famille
    over = row(1, "plus", True, p0=0.70) | {"prob": p_plus, "conf": 0.9, "n_min": 10}
    under = row(2, "moins", True, p0=0.70) | {"prob": p_moins, "conf": 0.9, "n_min": 10}
    assert calib.trial_ok(over) and CB.data_ok(over), "« plus de » repris"
    assert not calib.trial_ok(under) and not CB.data_ok(under), "« moins de » toujours écarté"
    assert not calib.trial_ok(over | {"sport_key": "soccer_greece_super_league"})                 # championnat faible au rejeu : non
    assert calib.trial_ok(over | {"sport_key": "soccer_brazil_campeonato"}) and calib.trial_ok(over | {"sport_key": "soccer_germany_bundesliga"})
    assert any(x.startswith("🧪 Essai tirs vers le but « plus de » : 26 matchs · 62 % / 65 %") or x.startswith("🧪 Essai tirs vers le but")
               for x in status.learning_lines()), status.learning_lines()
    # au plus 1 « plus de » tirs par combiné
    legs = [row(10 + i, "plus", True, p0=0.75, mid=f"m{i}") | {"prob": 0.66, "odds": 1.9} for i in range(4)]
    legs += [{"id": f"g{i}", "key": f"g{i}|totals||1.5|Over", "market": "totals", "name": "Over", "point": 1.5, "prob": 0.70, "odds": 1.5,
              "sport_key": "soccer_epl", "commence": "2026-10-14T19:00:00Z"} for i in range(4)]
    for x in legs:
        x.setdefault("commence", "2026-10-14T19:00:00Z")
    combo = CB.best_combo(legs, 5.0, 12.0)
    assert combo and sum(1 for c in combo[0] if c["market"].startswith("team_shots")) <= C.SHOTS_TRIAL_MAX_LEGS, combo
    # arrêt automatique : 30 matchs sous 53 %
    data["_cell"]["shots|plus"] = [30, 15, 19.5]
    store.save(calib.FILE, data)
    calib.reset()
    assert not calib.trial_ok(over) and not CB.data_ok(over) and "ARRÊTÉ" in calib.trial_line()
    print("ok  essai tirs « plus de » : chance corrigée par leurs résultats, repris (Brésil, Bundesliga oui ; Grèce non), « moins de » écarté,"
          " au plus 1 par combiné, arrêt automatique sous 53 % après 30 matchs")


def test_leagues():
    data, rows = real_like()
    store.save(calib.FILE, data)
    calib.reset()
    # Brésil : 10 matchs de tirs « plus de », 9 gagnés ; Grèce (écartée d'avance) : 10 matchs à 80 % ; Écosse : 10 matchs à 30 %
    br = [row(500 + i, "plus", i < 9, sk="soccer_brazil_campeonato") for i in range(10)]
    gr = [row(600 + i, "plus", i < 8, sk="soccer_greece_super_league") for i in range(10)]
    sc = [row(700 + i, "plus", i < 3, sk="soccer_spl") for i in range(10)]
    for r in br + gr + sc:
        r.pop("calib")
    calib.add(rows + br + gr + sc)
    d = calib.load()
    assert d["_ls"]["soccer_brazil_campeonato"]["shots|plus"][:2] == [10, 9] and "shots|moins" in d["_ls"]["soccer_epl"], d["_ls"]
    over = row(1, "plus", True, p0=0.70)
    assert calib.trial_ok(over | {"sport_key": "soccer_greece_super_league"}), "repris : SES résultats (80 %) passent avant la liste de départ"
    assert not calib.trial_ok(over | {"sport_key": "soccer_spl"}), "écarté : SES résultats (30 %)"
    assert calib.trial_ok(over | {"sport_key": "soccer_brazil_campeonato"})
    line = calib.trial_line()
    assert "par championnat :" in line and "⛔" in line and "✅" in line, line
    # tous les marchés : un championnat avec assez de résultats DANS UN SENS a sa propre correction pour ce sens
    store.save(calib.FILE, {"corners": [200, 130, 130.0], "_cell": {"corners|plus": [100, 65, 65.0], "corners|moins": [100, 65, 65.0]},
                            "_ls": {"soccer_brazil_campeonato": {"corners|plus": [80, 60, 52.0]}}})
    calib.reset()
    k_br = calib.factor("alternate_totals_corners", sport_key="soccer_brazil_campeonato", side="plus", base=1.0)
    k_br_moins = calib.factor("alternate_totals_corners", sport_key="soccer_brazil_campeonato", side="moins", base=1.0)
    assert k_br > 1.05 and abs(k_br_moins - 1.0) < 0.02, (k_br, k_br_moins)       # Brésil « plus de » corrigé à part ; « moins de » : comme partout
    print("ok  championnats : chaque marché, dans chaque sens, jugé aussi par championnat ; l'essai des tirs écarte ou reprend un championnat d'après SES résultats")


def main():
    test_rebuild_cells()
    test_trial()
    test_leagues()
    print("tests essai tirs vers le but : OK")


if __name__ == "__main__":
    main()
