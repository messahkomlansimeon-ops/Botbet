"""Prix 1xBet de « clôture » des marchés de stats dans le journal d'ensemble (audit du CLV sans prix sharp) : la référence 1xBet (xref) est gardée à la prise, le
DERNIER prix lu dans le CACHE DISQUE (jamais de réseau) est noté avec son avance sur le coup d'envoi (lead_min), un relevé plus récent remplace l'ancien, un
pari retiré par 1xBet ne remplace pas le dernier prix connu, hors fenêtre ou réglé : rien, une panne ne bloque jamais le passage. Hors ligne.
Lancer : python -m tests.test_xclose_journal"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-xclose-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xclose-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, ensemble as E, store  # noqa: E402
from kelly.sources import xbet  # noqa: E402

NOW = datetime(2030, 1, 1, 18, 0, tzinfo=timezone.utc)
KO = NOW + timedelta(minutes=100)          # le match commence dans 100 min


def prono(key, **kw):
    return {"key": key, "id": "m1", "sport_key": "soccer_epl", "home": "H", "away": "A", "commence": KO.isoformat(), "market": "team_shots_home",
            "name": "Over", "point": 5.5, "prob": 0.6, "p0": 0.6, "xodds": 1.9, "mu": None, "xref": [901, 5, 5.5], "xat": "17:00",
            "model_p": 0.6, "prob_x": 0.52, "x_approx": False} | kw


def test_record_keeps_xref():
    store.save(E.LOG_FILE, [])
    c = prono("k1") | {"p_model": 0.6, "xp": 0.52}                    # le modèle ET l'avis 1xBet : la ligne entre au journal
    assert E.record([c], "2030-01-01", NOW) == 1
    log = store.load(E.LOG_FILE, [])
    assert len(log) == 1 and log[0]["xref"] == [901, 5, 5.5] and log[0]["xodds"] == 1.9, log
    old = prono("k2") | {"p_model": 0.6, "xp": 0.52}
    old.pop("xref")
    assert E.record([old], "2030-01-01", NOW) == 1 and "xref" not in store.load(E.LOG_FILE, [])[1], "sans référence 1xBet à la prise : pas de champ"
    E.record([prono("k2") | {"p_model": 0.6, "xp": 0.52, "xodds": 2.4}], "2030-01-01", NOW)          # relecture : la référence est rattrapée, le prix de la prise reste
    row = {r["key"]: r for r in store.load(E.LOG_FILE, [])}["k2"]
    assert row["xref"] == [901, 5, 5.5] and row["xodds"] == 1.9, row
    print("ok  journal : la ligne garde la référence 1xBet de la prise (hors ROW), rattrapée sur une ligne plus ancienne, sans changer le prix de la prise")


def seed(rows):
    store.save(E.LOG_FILE, rows)


def line(key, **kw):
    return {"key": key, "id": "m", "market": "team_shots_home", "name": "Over", "point": 5.5, "xodds": 1.9, "xref": [901, 5, 5.5], "commence": KO.isoformat(),
            "result": None, "pm": 0.6, "px": 0.5, "ps": None, "prob": 0.6, "date": "2030-01-01"} | kw


def test_mark():
    ts = NOW.timestamp()
    read = {"cell": None}
    old = xbet.cached_cell
    xbet.cached_cell = lambda ref: read["cell"]
    try:
        seed([line("a"), line("settled", result="win"), line("noref", xref=None), line("far", commence=(NOW + timedelta(hours=9)).isoformat()),
              line("past", commence=(NOW - timedelta(hours=2)).isoformat())])
        read["cell"] = xbet.Cell(2.05, ts - 30 * 60, False)                 # relevé il y a 30 min = 130 min avant le coup d'envoi (le match est dans 100 min)
        assert E.mark_x_close(NOW) == 1, "seule la ligne ouverte, avec référence, dans la fenêtre"
        log = {r["key"]: r for r in store.load(E.LOG_FILE, [])}
        x = log["a"]["xclose"]
        assert x["odds"] == 2.05 and x["lead_min"] == 130 and x["susp"] is False and x["read_ts"] == ts - 1800, x
        assert all("xclose" not in log[k] for k in ("settled", "noref", "far", "past")), "réglé, sans référence, hors fenêtre : rien"
        assert log["a"]["xodds"] == 1.9, "le prix de la PRISE ne change jamais"

        assert E.mark_x_close(NOW) == 0, "même relevé : rien de neuf"
        read["cell"] = xbet.Cell(2.20, ts - 10 * 60, False)                 # relevé plus récent : il remplace l'ancien
        assert E.mark_x_close(NOW) == 1
        assert {r["key"]: r for r in store.load(E.LOG_FILE, [])}["a"]["xclose"]["odds"] == 2.20
        read["cell"] = xbet.Cell(1.80, ts - 40 * 60, False)                 # relevé plus ancien : ignoré
        assert E.mark_x_close(NOW) == 0

        read["cell"] = xbet.Cell(None, ts - 5 * 60, False)                  # 1xBet a retiré le pari : on garde le dernier prix et on le dit
        assert E.mark_x_close(NOW) == 1
        a = {r["key"]: r for r in store.load(E.LOG_FILE, [])}["a"]
        assert a["xclose"]["odds"] == 2.20 and a["xgone"]["lead_min"] == 105, a
        assert E.mark_x_close(NOW) == 0, "déjà noté"

        read["cell"] = xbet.Cell(2.40, ts - 2 * 60, False)                  # le pari rouvre (suspendu puis rouvert) : prix plus récent, « retiré » disparaît
        assert E.mark_x_close(NOW) == 1
        a = {r["key"]: r for r in store.load(E.LOG_FILE, [])}["a"]
        assert a["xclose"]["odds"] == 2.40 and a["xclose"]["lead_min"] == 102 and "xgone" not in a, a

        seed([line("naive", commence=(KO.replace(tzinfo=None)).isoformat()), line("ok")])        # une date naïve ne fait pas abandonner la passe
        read["cell"] = xbet.Cell(2.05, ts - 30 * 60, False)
        assert E.mark_x_close(NOW) == 2, "la ligne à date naïve est lue comme UTC, l'autre aussi"

        read["cell"] = None                                                  # match absent du cache
        assert E.mark_x_close(NOW) == 0

        def boom(ref):
            raise RuntimeError("cache illisible")
        xbet.cached_cell = boom
        assert E.mark_x_close(NOW) == 0 and app.mark_xclose(NOW) == 0, "une panne ne bloque jamais"
    finally:
        xbet.cached_cell = old
    print("ok  dernier prix 1xBet du cache : lead_min, relevé plus récent remplace, retiré = garde le dernier prix, hors fenêtre / réglé / sans référence : rien")


def main():
    test_record_keeps_xref()
    test_mark()
    print("tests prix 1xBet de clôture du journal : OK")


if __name__ == "__main__":
    main()
