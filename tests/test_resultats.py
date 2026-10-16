"""Vues séparées « combis » et « saison résultats » (kelly/resultats.py), sur données FICTIVES dans un dossier temporaire. Lancer : python -m tests.test_resultats"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-res-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-res-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import app, fair, resultats, store  # noqa: E402


def main():
    out = str(resultats.combis())
    assert "Aucun combiné réglé" in out, out
    out = str(resultats.saison())
    assert "aucun combiné réglé" in out and "rien de réglé" in out, out
    store.save("bets.json", {"picks": [], "combos": [
        {"date": "2026-10-10", "tier": "🛡️ SÛR", "result": "win", "odds": 2.5, "stake": 1, "keys": []},
        {"date": "2026-10-11", "tier": "🎯", "result": "loss", "odds": 8.0, "stake": 1, "keys": [], "played": {"stake": 1000, "odds": 8.0}}]})
    store.save(fair.COMBOS_FILE, [
        {"date": "2026-10-12", "tier": "ss10", "result": "win", "min_odds": 9.5, "legs": [{"x": 1}], "played": {"stake": 500, "odds": 9.5}},
        {"date": "2026-10-13", "tier": "ss3", "result": "loss", "min_odds": 3.1, "legs": [{"x": 1}]}])
    c, s = str(resultats.combis()), str(resultats.saison())
    assert "2 réglés : 1 gagné" in c and "ss10" not in c and "ss3" not in c, c
    assert "Combinés saison — 2 réglés : 1 gagné" in s and "cote 9,5" in s and "cote 2,5" not in s, s
    assert "2026-10-10" not in str(resultats.combis(1)), "filtre en jours"
    print("ok  vues combis / saison : séparées (ss.. à part), argent des seuls paris notés, filtre en jours, vides lisibles")


if __name__ == "__main__":
    main()
