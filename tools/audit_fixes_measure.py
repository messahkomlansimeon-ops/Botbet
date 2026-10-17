"""Mesures avant / après des correctifs du lot A de l'audit du 17/10 (voir audit_fixes_ledger.json), en LECTURE SEULE sur une COPIE de kelly.db.

    python tools/audit_fixes_measure.py --data-dir /dossier/avec/kelly.db [a3] [a1] [a4] [a13] [a15]     (sans point : tous)

Ne touche jamais au dossier donné : kelly.db est copié dans un dossier temporaire (DATA_DIR du processus), aucun réseau, aucune écriture dans data/. Chaque mesure dit sa SOURCE
(le dossier, la plus ancienne et la plus récente date lues) pour que l'amendement du ledger puisse la citer. Sur la sauvegarde du 13/10 (8-13/10) il n'y a pas encore de `xclose`."""
import argparse
import math
import os
import shutil
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def setup(data_dir):
    tmp = tempfile.mkdtemp(prefix="kelly-measure-")
    src = os.path.join(data_dir, "kelly.db")
    if not os.path.exists(src):
        sys.exit(f"kelly.db introuvable dans {data_dir}")
    shutil.copy(src, os.path.join(tmp, "kelly.db"))
    os.environ["DATA_DIR"] = tmp
    for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "XBET_PROXY_FILE"):
        os.environ.pop(k, None)
    return tmp


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def f(x, nd=4):
    return "—" if x is None else f"{x:+.{nd}f}"


def window(rows):
    ds = sorted(str(r.get("date"))[:10] for r in rows if r.get("date"))
    return f"{ds[0]} -> {ds[-1]}" if ds else "aucune ligne"


def old_x_clv(r, taken=None):
    """x_clv d'AVANT le correctif A3 (sans la vérification de la prise)."""
    from kelly import lanestats as LS
    xc = r.get("xclose")
    price = taken if taken else r.get("xodds")
    if (r.get("xgone") or not price or not isinstance(xc, dict) or not xc.get("odds") or xc.get("lead_min") is None
            or not 0 <= xc["lead_min"] <= LS.LEAD_OK_MIN or xc.get("susp")):
        return None
    try:
        return math.log(float(price) / float(xc["odds"]))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def a3():
    from kelly import ensemble, lanestats as LS, oddsonly, seasonlane, cupstats, store
    pools = {"journal d'ensemble (+ archive)": LS.journal_rows(), "voie saison": [], "coupes": []}
    try:
        log = store.load(seasonlane.LOG, [])
        pools["voie saison"] = [r for r in log if isinstance(r, dict)] + [r for r in ensemble.archive_rows(prefix=seasonlane.ARCH) if r.get("key") not in {x.get("key") for x in log if isinstance(x, dict)}]
    except Exception as e:                              # noqa: BLE001
        print(f"  voie saison illisible : {type(e).__name__}")
    try:
        pools["coupes"] = cupstats.journal_rows()
    except Exception as e:                              # noqa: BLE001
        print(f"  coupes illisibles : {type(e).__name__}")
    try:
        pools["voie cotes (oddsonly)"] = [r for r in store.load(oddsonly.LOG_FILE, []) if isinstance(r, dict)]
    except Exception as e:                              # noqa: BLE001
        print(f"  voie cotes illisible : {type(e).__name__}")
    print("A3 — CLV contre sa propre lecture")
    tot = defaultdict(int)
    for name, rows in pools.items():
        rs = [r for r in rows if LS.settled(r) or r.get("result") in ("win", "loss")]
        with_x = [r for r in rs if isinstance(r.get("xclose"), dict)]
        print(f"  {name} : {len(rows)} lignes, {len(rs)} réglées, {len(with_x)} avec xclose — fenêtre {window(rows)}")
        by = defaultdict(list)
        for r in with_x:
            by[r.get("strategy") or "sans étiquette"].append(r)
        for lane, g in sorted(by.items()):
            before = [old_x_clv(r) for r in g]
            after = [LS.x_clv(r) for r in g]
            zero_to_none = sum(1 for b, a in zip(before, after) if b is not None and a is None)
            true_zero_lost = sum(1 for r, b, a in zip(g, before, after) if b is not None and a is None and ensemble.take_ts(r) is None and b == 0.0
                                 and (ensemble.close_read_ts(r.get("xclose")) or 0) > (ensemble.ts_of(r.get("at")) or 0) + 60)
            print(f"    voie {lane:<14} lignes à xclose {len(g):>4} | CLV mesuré avant {sum(b is not None for b in before):>4} -> après {sum(a is not None for a in after):>4} | "
                  f"passent de mesuré à None {zero_to_none:>4} (dont CLV exactement 0 : {sum(1 for b, a in zip(before, after) if b == 0.0 and a is None)}, "
                  f"vrais zéros écartés par la règle de repli : {true_zero_lost}) | moyenne avant {f(mean(before))} après {f(mean(after))}")
            tot["avant"] += sum(b is not None for b in before)
            tot["apres"] += sum(a is not None for a in after)
    if not tot["avant"]:
        print("  Aucune ligne à xclose dans cette base : rien à mesurer (le relevé de clôture est postérieur à cette sauvegarde).")


POINTS = {"a3": a3}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("points", nargs="*")
    a = ap.parse_args()
    tmp = setup(a.data_dir)
    print(f"Source : {os.path.abspath(a.data_dir)}/kelly.db (copie en lecture seule)")
    try:
        for p in (a.points or list(POINTS)):
            if p not in POINTS:
                print(f"(point {p} : mesure pas encore écrite)")
                continue
            POINTS[p]()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
