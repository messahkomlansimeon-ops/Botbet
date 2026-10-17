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
            nosrc = sum(1 for r, b in zip(g, before) if b == 0.0 and not (r.get("xclose") or {}).get("src") and ensemble.take_ts(r) is None)
            print(f"    voie {lane:<14} lignes à xclose {len(g):>4} | CLV mesuré avant {sum(b is not None for b in before):>4} -> après {sum(a is not None for a in after):>4} | "
                  f"passent de mesuré à None {zero_to_none:>4} (dont CLV exactement 0 : {sum(1 for b, a in zip(before, after) if b == 0.0 and a is None)}, "
                  f"vrais zéros écartés par la règle de repli : {true_zero_lost} ; zéros SANS source gardés (hors règle) : {nosrc}) | moyenne avant {f(mean(before))} après {f(mean(after))}")
            tot["avant"] += sum(b is not None for b in before)
            tot["apres"] += sum(a is not None for a in after)
    if not tot["avant"]:
        print("  Aucune ligne à xclose dans cette base : rien à mesurer (le relevé de clôture est postérieur à cette sauvegarde).")


def a1(days=14):
    """A1 — alertes 💰 (écarts sharp) sur la double chance. L'instant de lecture du 1X2 d'où vient la proba n'était pas enregistré : pas de compte exact des alertes qui n'auraient pas dû partir,
    une BORNE (toutes les alertes 💰 double chance de la fenêtre) ; les alertes écrites à partir du correctif portent gap.sharp_src == « h2h » et la bonne heure."""
    from kelly import track
    rows = [r for r in track.load() if isinstance(r, dict)]
    gaps = [r for r in rows if r.get("sharpgap") or (r.get("mark") or {}).get("sharpgap")]
    dates = sorted(str(r.get("date") or r.get("commence"))[:10] for r in gaps)
    print("A1 — alertes 💰 double chance")
    print(f"  source : tracked.json de la copie, fenêtre des écarts sharp {dates[0] if dates else '—'} -> {dates[-1] if dates else '—'} ({len(gaps)} écarts, {len(rows)} paris suivis)")
    dc = [r for r in gaps if r.get("market") == "double_chance"]
    print(f"  écarts sharp sur la double chance : {len(dc)} (borne haute des alertes à tort ; l'âge réel du 1X2 n'est pas enregistré dans les anciennes lignes)")
    fixed = [r for r in dc if (r.get("gap") or {}).get("sharp_src") == "h2h"]
    print(f"  dont déjà écrites avec sharp_src == h2h (après correctif) : {len(fixed)}")
    for r in dc[:10]:
        g = r.get("gap") or {}
        print(f"    {str(r.get('commence'))[:16]} {r.get('event')} {r.get('pick')} edge {g.get('edge')} sharp_at {g.get('sharp_at')}")


def a4():
    """A4 — probabilités corrigées hors de [0,01 ; 0,99] dans les journaux (fair.json, journal d'ensemble + archive, tracked.json) et lignes qui seraient refusées avec la borne (espérance
    prob x cote - 1 positive avant, négative après)."""
    from kelly import calib, lanestats as LS, store
    srcs = {"fair.json": store.load("fair.json", []), "journal d'ensemble": LS.journal_rows(), "tracked.json": store.load("tracked.json", [])}
    print("A4 — probabilités corrigées bornées")
    for name, rows in srcs.items():
        rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("prob"), (int, float))]
        out01 = [r for r in rows if not 0 <= r["prob"] <= 1]
        outb = [r for r in rows if not calib.P_MIN <= r["prob"] <= calib.P_MAX]
        refused = [r for r in outb if r.get("xodds") and r["prob"] * r["xodds"] - 1 >= 0 > calib.bound(r["prob"]) * r["xodds"] - 1]
        print(f"  {name} : {len(rows)} lignes à prob — fenêtre {window(rows)} ; hors de [0,1] : {len(out01)} ; hors de [{calib.P_MIN},{calib.P_MAX}] : {len(outb)} ; "
              f"espérance positive avant et négative après la borne : {len(refused)}")
        for r in outb[:5]:
            print(f"    {r.get('key') or r.get('id')} prob {r['prob']} xodds {r.get('xodds')}")


POINTS = {"a3": a3, "a1": a1, "a4": a4}


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
