"""Pronostics viables : un seul par match, le meilleur, et seulement s'il est solide (chances, données, mieux qu'un match moyen,
marché où Kelly n'est pas trop sûre d'elle). Les combinés à vérifier n'utilisent que des pronostics viables.
Lancer : python -m tests.test_viable"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-viable-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-viable-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-viable-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import calib, combos as CB, config as C, fair, fmt, store  # noqa: E402


def prono(match, market, p, score, **kw):
    return {"key": f"{match}|{market}|{p}", "id": match, "sport_key": "soccer_epl", "home": f"H{match}", "away": f"A{match}",
            "commence": "2026-10-10T15:00:00Z", "start": "15:00", "league": "L", "market": market, "name": "Over", "point": 2.5,
            "prob": p, "p0": p, "min_odds": round(1 / p * 1.05, 2), "conf": 0.5, "n_min": 8, "score": score,
            "pick": f"{market} {p}"} | kw


def test_viable_rules():
    calib._memo.clear()
    store.save(calib.FILE, {})
    calib._memo.clear()
    ok = prono("m", "totals", 0.62, 0.2)
    assert CB.viable(ok)
    assert not CB.viable(ok | {"prob": 0.57})                         # pas assez de chances
    assert not CB.viable(ok | {"n_min": 3})                           # pas assez de matchs de données
    assert not CB.viable(ok | {"conf": 0.2})                          # données trop pauvres
    assert not CB.viable(ok | {"base": 0.66})                         # moins bien qu'un match moyen du championnat
    assert CB.viable(ok | {"base": 0.55})
    # marché où Kelly s'est montrée trop sûre d'elle (corrigée fortement) : écarté
    store.save(calib.FILE, {"corners": [400, 200, 280.0]})            # 50 % réalisés pour 70 % annoncés
    calib._memo.clear()
    assert calib.factor("alternate_totals_corners") < C.VIABLE_MIN_K
    assert not CB.viable(prono("m", "alternate_totals_corners", 0.70, 0.3)) and CB.viable(ok)
    store.save(calib.FILE, {})
    calib._memo.clear()
    print("ok  règles de viabilité")


def test_one_best_per_match():
    pronos = [prono("a", "totals", 0.62, 0.10), prono("a", "alternate_totals_corners", 0.66, 0.30),
              prono("a", "team_sot_total", 0.70, 0.40, n_min=2),       # meilleur score mais pas viable (2 matchs)
              prono("b", "btts", 0.56, 0.50), prono("b", "double_chance", 0.64, 0.05),
              prono("c", "totals", 0.55, 0.60)]                        # aucun pronostic viable pour ce match
    best = CB.best_per_match(pronos)
    assert [(c["id"], c["market"]) for c in best] == [("a", "alternate_totals_corners"), ("b", "double_chance")], best
    text = fmt.pronos(best)
    assert text.count("👉") == 2 and "66 % de chances · données solides" in text, text
    assert "Aucun pronostic assez solide" in fmt.pronos([])
    # combinés à vérifier : seulement des jambes viables
    legs = CB.fair_legs(pronos)
    assert legs and all(CB.viable(c) for c in legs) and not any(c["market"] == "team_sot_total" for c in legs)
    # suivi : les montrés ne sont pas « en silence », d'autres le sont
    store.save("fair.json", [])
    fair.record(pronos, "2026-10-10", shown=best)
    rows = {r["key"]: r for r in store.load("fair.json", [])}
    assert all(not rows[c["key"]].get("x") for c in best)
    print("ok  un seul pronostic par match, le meilleur viable")


def main():
    test_viable_rules()
    test_one_best_per_match()
    print("tests pronostics viables : OK")


if __name__ == "__main__":
    main()
