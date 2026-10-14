"""Archive compacte du journal d'ensemble (90 jours, un document par jour « arch/ens/<jour> ») : les lignes RÉGLÉES y sont copiées une seule fois au règlement (avant que
le journal ne les oublie), avec ce qu'il faut aux audits (modèle, avis de marché, prix 1xBet de la prise et dernier prix connu avec son avance, fantôme du modèle) ; les jours
de plus de 90 jours sont effacés ; elle n'entre pas dans le miroir lisible ; une panne d'écriture ne bloque jamais le règlement ; seuls les outils d'audit la lisent (audit_clv
fusionne journal et archive sans doublon). Hors ligne. Lancer : python -m tests.test_ensemble_archive"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-archive-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-archive-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from kelly import config as C, ensemble as E, store  # noqa: E402

import audit_clv as A  # noqa: E402

TODAY = date(2026, 10, 14)


def line(i, day="2026-10-13", **kw):
    return {"key": f"m{i}|totals||2.5|Over", "id": f"m{i}", "market": "totals", "name": "Over", "point": 2.5, "commence": f"{day}T19:00:00Z", "date": day, "pm": 0.6,
            "px": 0.5, "ps": 0.52, "xodds": 1.9, "prob": 0.6, "result": None} | kw


def settle_all(r):
    r["result"] = "win" if int(r["id"][1:]) % 2 else "loss"
    r["final"] = "2-1"
    return True


def test_archive_once_and_fields():
    store.save(E.LOG_FILE, [line(1), line(2, xref=[7, 3, 2.5], xclose={"odds": 2.1, "read": "x", "read_ts": 1.0, "lead_min": 70, "susp": True}, xgone={"lead_min": 40},
                                    shadow={"label": "x", "ev": 0.08, "model_value": True, "old_ok": True, "old_kind": "value", "old_p": 0.7}),
                                line(3, "2026-10-12"), line(4, "2026-10-12", market="team_shots_home")])
    settled, learned = E.settle_log(settle_all)
    assert len(settled) == 4
    assert E.store.names(E.ARCH_PREFIX) == [E.ARCH_PREFIX + "2026-10-12", E.ARCH_PREFIX + "2026-10-13"]
    rows = {r["key"]: r for r in E.archive_rows()}
    assert set(rows) == {l["key"] for l in [line(1), line(2), line(3)]} | {line(4)["key"]}
    r2 = rows[line(2)["key"]]
    assert r2["xclose"] == {"odds": 2.1, "lead_min": 70, "susp": True} and r2["xref"] == [7, 3, 2.5] and r2["xgone"] == {"lead_min": 40} and r2["result"] == "loss" and r2["date"] == "2026-10-13", r2
    assert r2["shadow"] == {"ev": 0.08, "model_value": True, "old_ok": True, "old_kind": "value", "old_p": 0.7} and r2["pm"] == 0.6 and r2["xodds"] == 1.9
    assert "xclose" not in rows[line(1)["key"]] and "shadow" not in rows[line(1)["key"]] and "xref" not in rows[line(1)["key"]]
    old_row = E.unpack_archive_row("k", "2026-10-01", ["totals", "m", "Over", 2.5, "c", 0.6, 0.5, None, 1.9, [2.0, 80], None, "win", None, None])          # archivée avant la colonne xref / susp
    assert old_row["xclose"] == {"odds": 2.0, "lead_min": 80, "susp": False} and "xref" not in old_row
    doc = store.load(E.ARCH_PREFIX + "2026-10-13", None)
    assert doc["cols"] == list(E.ARCH_COLS) and isinstance(doc["rows"][line(1)["key"]], list) and len(doc["rows"]) == 2, doc
    # une seule copie : un 2e règlement ne recopie rien, les lignes sont marquées
    assert all(r.get("arch") for r in store.load(E.LOG_FILE, [])) and E.archive(store.load(E.LOG_FILE, [])) == 0
    n_before = len(E.archive_rows())
    E.settle_log(settle_all)
    assert len(E.archive_rows()) == n_before
    print("ok  archive : lignes réglées copiées une fois, compactes (colonnes + valeurs), xclose / xgone / fantôme conservés, lignes marquées")


def test_retention_and_mirror():
    old_day = str(TODAY - timedelta(days=C.ENSEMBLE_ARCHIVE_DAYS + 5))
    keep_day = str(TODAY - timedelta(days=C.ENSEMBLE_ARCHIVE_DAYS - 5))
    for d in (old_day, keep_day):
        store.save(E.ARCH_PREFIX + d, {"cols": list(E.ARCH_COLS), "rows": {f"k{d}": [None] * len(E.ARCH_COLS)}})
    rows = [line(9, "2026-10-13", result="win")]
    assert E.archive(rows, today=TODAY) == 1
    names = store.names(E.ARCH_PREFIX)
    assert E.ARCH_PREFIX + old_day not in names and E.ARCH_PREFIX + keep_day in names and C.ENSEMBLE_ARCHIVE_DAYS == 90, names
    dest = tempfile.mkdtemp(prefix="kelly-archive-mirror-")
    store.export(dest)
    assert not os.path.exists(os.path.join(dest, "arch")) and os.path.exists(os.path.join(dest, "ensemble_log.json")), "l'archive n'entre pas dans le miroir lisible"
    print("ok  rétention 90 jours (le plus vieux jour effacé), archive absente du miroir lisible")


def test_failure_never_blocks():
    store.save(E.LOG_FILE, [line(21), line(22)])
    real = store.save_many
    store.save_many = lambda docs: (_ for _ in ()).throw(OSError("disque plein"))
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            settled, learned = E.settle_log(settle_all)
    finally:
        store.save_many = real
    log = store.load(E.LOG_FILE, [])
    assert len(settled) == 2 and all(r["result"] for r in log) and not any(r.get("arch") for r in log), "le règlement passe, les lignes ne sont pas marquées copiées"
    assert "archive ignorée" in err.getvalue()
    E.settle_log(settle_all)                          # au règlement suivant elles sont copiées
    assert {l["key"] for l in [line(21), line(22)]} <= {r["key"] for r in E.archive_rows()}
    old = C.ENSEMBLE_ARCHIVE
    C.ENSEMBLE_ARCHIVE = False
    try:
        assert E.archive([line(30, result="win")]) == 0
    finally:
        C.ENSEMBLE_ARCHIVE = old
    print("ok  panne d'écriture : le règlement passe, copie au passage suivant ; interrupteur ENSEMBLE_ARCHIVE")


def test_audit_reads_both():
    src = tempfile.mkdtemp(prefix="kelly-archive-src-")
    con = sqlite3.connect(os.path.join(src, "kelly.db"))
    con.execute("create table docs (name text primary key, body text, updated text, version integer)")
    con.execute("create table meta (k text, v text)")
    live = [line(1, result="win", pm=0.61)]
    arch_rows = {line(1)["key"]: E._pack(line(1, result="win", pm=0.5)), line(2)["key"]: E._pack(line(2, result="loss"))}
    for nm, body in (("tracked.json", []), ("ensemble_log.json", live), (E.ARCH_PREFIX + "2026-10-13", {"cols": list(E.ARCH_COLS), "rows": arch_rows})):
        con.execute("insert into docs values (?,?,?,?)", (nm, json.dumps(body), "2026-10-14", 1))
    con.commit()
    con.close()
    rows, log = A.load(src)
    by = {r["key"]: r for r in log}
    assert set(by) == {line(1)["key"], line(2)["key"]} and len(log) == 2, "sans doublon"
    assert by[line(1)["key"]]["pm"] == 0.61 and not by[line(1)["key"]].get("archived"), "le journal (plus complet) l'emporte"
    assert by[line(2)["key"]]["archived"] is True and by[line(2)["key"]]["result"] == "loss"
    print("ok  audit_clv : journal + archive fusionnés sans doublon, le journal l'emporte")


def main():
    test_archive_once_and_fields()
    test_retention_and_mirror()
    test_failure_never_blocks()
    test_audit_reads_both()
    print("tests archive du journal d'ensemble : OK")


if __name__ == "__main__":
    main()
