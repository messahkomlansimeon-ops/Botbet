"""Voie cotes, mesure : règlement gratuit sans apprentissage, durée de vie (médiane avec bornes, seulement avec 20 disparues), CLV par mécanisme (lignes devenues suspectes exclues), couverture, ligne du bilan par voie, rapport
(commande « incohérences »), carte (cinquième voie). Journaux FICTIFS en dossier temporaire. Lancer : python -m tests.test_oddsonly_report"""
import math
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-oddsonly-rep-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-oddsonly-rep-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import app, edgemap, ensemble, lanestats, oddsonly as O, settle, store  # noqa: E402

DAY = "2026-10-17"


def row(i, mech="1a", status="value", result="win", gone=None, seen=None, censored=None, clv=0.03, went=False, mid=None, market="alternate_totals_corners", name="Over", **kw):
    """Ligne du journal : prise à 2,00, clôture telle que le CLV vaille `clv`, première détection 12:00."""
    return {"key": f"{DAY}|{mid or 'm' + str(i)}|{mech}|{market}|{name}|{i}", "id": mid or f"m{i}", "home": "H", "away": "A", "sport_key": "soccer_epl",
            "commence": f"{DAY}T20:00:00Z", "date": DAY, "market": market, "name": name, "point": 9.5, "family": "corners", "mech": mech, "status": status, "xodds": 2.0,
            "p_impl": 0.55, "pe": 0.55, "edge": 0.06, "edge_last": 0.06, "went_suspect": went, "first_at": f"{DAY}T12:00+00:00", "last_seen_at": seen or f"{DAY}T12:00+00:00",
            "gone_at": gone, "reads": 2, "censored": censored, "xclose": {"odds": 2.0 / math.exp(clv), "read": f"{DAY}T19:00+00:00", "lead_min": 60, "susp": False} if clv is not None else None,
            "xgone": False, "result": result, "strategy": "cotes"} | kw


def test_lifetime_and_clv():
    rows = []
    for i in range(25):                              # 25 disparues : médiane basse 60 min (dernier relevé à 13:00), haute 120 (disparue à 14:00)
        rows.append(row(i, gone=f"{DAY}T14:00+00:00", seen=f"{DAY}T13:00+00:00", clv=0.02 + (i % 2) * 0.02))
    rows += [row(100 + i, mech="1c", censored="cellule") for i in range(3)] + [row(200, mech="2", result=None)]
    rows += [row(300, status="suspect", clv=None), row(301, went=True, clv=-0.5)]           # suspecte et devenue suspecte : hors CLV
    g, c, o, lo, hi = O.lifetime(rows)
    assert (g, c) == (25, 3), (g, c, o)
    assert lo == 60 and hi == 120, (lo, hi)
    few = O.lifetime(rows[:5])
    assert few[0] == 5 and few[3] is None and few[4] is None, "pas de médiane avant 20 disparues"
    b = O.stats_block(rows, {DAY: {"read": [f"m{i}" for i in range(40)], "comp": [f"m{i}" for i in range(30)], "ev": {"1a|h2h": 12, "1c|corners": 40}}})
    assert b["suspect"] == 2 and b["read"] == 40 and b["comp"] == 30 and b["hit"] >= 25
    assert b["mech"]["1a"]["clv_n"] == 25 and abs(b["mech"]["1a"]["clv"] - 0.03) < 0.011 and b["mech"]["1a"]["real"] == 1.0 and abs(b["mech"]["1a"]["ann"] - 0.55) < 1e-9
    assert all(r["key"] != f"{DAY}|m301|1a|alternate_totals_corners|Over|301" for r in O.value_rows(rows)), "devenue suspecte : exclue du CLV"
    print("ok  durée de vie (médiane avec bornes seulement avec 20 disparues, censure), CLV par mécanisme sans les lignes devenues suspectes")


def test_report_lane_line_and_command():
    rows = [row(i, gone=f"{DAY}T14:00+00:00", seen=f"{DAY}T13:00+00:00") for i in range(22)] + [row(50, mech="1b"), row(51, mech="2", overlap_sharpgap=True)]
    store.save(O.LOG_FILE, rows)
    store.save(O.COVER_FILE, {DAY: {"read": [f"m{i}" for i in range(30)], "comp": [f"m{i}" for i in range(26)], "ev": {"1a|h2h": 12}}})
    txt = str(O.report([], DAY))
    for want in ("fantôme", "Matchs lus : 30", "calculables : 26", "1b totaux d'équipe", "à part", "recouvre sharpgap", "médiane entre 60 et 120", "Mécanisme 2 inactif", "yellow", "Règle de retour", "censure à gauche"):
        assert want in txt, (want, txt)
    ll = O.lane_line(None, DAY)
    assert ll.startswith("cotes (fantôme, aucun pari) : 24 incohérences ≥ 3,0 %") and "prix pris contre le dernier prix 1xBet" in ll, ll
    assert any(x.startswith("cotes (fantôme") for x in lanestats.lane_lines([], None, DAY, plain=True)), "ligne du bilan par voie"
    assert "Voie cotes" in str(app._handle_command("/incoherences 7"))
    store.save(O.LOG_FILE, [])
    store.save(O.COVER_FILE, {})
    assert "Rien collecté" in str(O.report([], DAY)) and O.lane_line(None, DAY) == ""
    print("ok  rapport, ligne du bilan par voie, commande « incohérences »")


def test_settle_no_learning_and_merge():
    open_rows = [row(1, result=None), row(2, result=None, mid="m2"), row(3, result="win")]
    store.save(O.LOG_FILE, open_rows)
    old = settle._settle_fair_row
    seen_ens = store.load(ensemble.LOG_FILE, [])
    calls = []

    def fake(b, now, fdata, stats_for=None):
        calls.append(b["key"])
        if b["id"] == "m1":
            b["result"], b["final"] = "loss", "1-0"
            return True
        return False
    settle._settle_fair_row = fake
    try:
        n = settle.settle_oddsonly(datetime(2026, 10, 18, 9, tzinfo=timezone.utc))
    finally:
        settle._settle_fair_row = old
    log = {r["key"]: r for r in store.load(O.LOG_FILE, [])}
    assert n == 1 and log[open_rows[0]["key"]]["result"] == "loss" and log[open_rows[1]["key"]]["result"] is None and open_rows[2]["key"] not in calls, (n, calls)
    assert log[open_rows[0]["key"]]["censored"] in (None, "coup d'envoi") and log[open_rows[1]["key"]]["censored"] == "coup d'envoi", "le règlement censure aussi les lignes ouvertes"
    assert store.load(ensemble.LOG_FILE, []) == seen_ens, "rien dans le journal d'ensemble, aucun apprentissage"
    print("ok  règlement : fusion par clé, lignes réglées intactes, censure du coup d'envoi, rien dans le journal d'ensemble")


def test_carte_fifth_voie():
    store.save(O.LOG_FILE, [row(i) for i in range(5)] + [row(90, status="suspect", clv=None), row(91, went=True), row(92, mech="3", status="watch")])
    out = edgemap.voie_rows()
    assert len(out["cotes"]) == 5 and edgemap.VOIES[-1] == "cotes" and edgemap.VOIE_LABEL["cotes"] == "cotes (fantôme)", out["cotes"]
    store.save(O.LOG_FILE, [])
    print("ok  carte : cinquième voie « cotes » (valeur, réglée, jamais devenue suspecte)")


if __name__ == "__main__":
    test_lifetime_and_clv()
    test_report_lane_line_and_command()
    test_settle_no_learning_and_merge()
    test_carte_fifth_voie()
    print("tests voie cotes (mesure) : OK")
