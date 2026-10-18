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


def _old_season_auto_plan(CB, C, legs, usage, limit):
    """season_auto_plan d'AVANT A13 (arrêt à la première jambe qui ne passe pas), pour le comparatif ; mêmes règles que kelly/combos.py par ailleurs."""
    ranked = sorted((c for c in usage.filter(legs) if c.get("sval") or C.FILL_LEGS), key=lambda c: (1 if c.get("sval") else 0, c["prob"] * c["odds"]), reverse=True)
    out, used = [], set()
    while len(out) < limit:
        free = [c for c in ranked if CB.leg_id(c) not in used]
        taken, ev_prod, odds, p_all, matches = [], 1.0, 1.0, 1.0, set()
        for c in free:
            if c["id"] in matches:
                continue
            if len(taken) >= C.MAX_LEGS or not CB._auto_ok(ev_prod * c["prob"] * c["odds"], odds * c["odds"], p_all * c["prob"]):
                break
            taken.append(c)
            matches.add(c["id"])
            ev_prod *= c["prob"] * c["odds"]
            odds *= c["odds"]
            p_all *= c["prob"]
        if len(taken) < 2:
            break
        used |= {CB.leg_id(c) for c in taken}
        out.append((taken, odds))
    return out


def a13():
    """A13 — season_auto_plan : par jour, plan d'avant (break) et plan d'après (jambe sautée), reconstruits depuis le journal de la voie saison (lignes à prix 1xBet, proba retenue `pe`)."""
    from kelly import combos as CB, config as C, ensemble, lanes, seasonlane, store
    log = [r for r in store.load(seasonlane.LOG, []) if isinstance(r, dict) and r.get("xodds") and (r.get("sval") or C.FILL_LEGS)]
    print("A13 — plan saison automatique, avant / après")
    if not log:
        print("  Aucune ligne du journal saison dans cette base (season_log.json absent ou vide) : rien à rejouer.")
        return
    days = defaultdict(list)
    for r in log:
        days[str(r.get("date"))[:10]].append(r | {"odds": r["xodds"], "prob": r.get("pe") or r.get("prob") or 0.0, "lane": "saison"})
    old_ok, old_unsent = lanes.ticket_ok, ensemble.unsent
    lanes.ticket_ok, ensemble.unsent = (lambda c: True), (lambda legs: False)
    print(f"  source : season_log.json de la copie, {len(days)} jour(s) du {min(days)} au {max(days)}")
    changed = empty_to_some = 0
    try:
        for d, legs in sorted(days.items()):
            before = _old_season_auto_plan(CB, C, legs, CB.Usage(), C.SEASON_AUTO_MAX_COMBOS)
            after = [(x[0], x[1]) for _, x in CB.season_auto_plan(legs, CB.Usage())]
            same = [([c["key"] for c in a], round(o, 2)) for a, o in before] == [([c["key"] for c in a], round(o, 2)) for a, o in after]
            changed += not same
            empty_to_some += (not before and bool(after))
            print(f"    {d}: {len(legs)} jambes ; avant {len(before)} combiné(s) {[round(o, 2) for _, o in before]} -> après {len(after)} {[round(o, 2) for _, o in after]}{'' if same else '  <- change'}")
    finally:
        lanes.ticket_ok, ensemble.unsent = old_ok, old_unsent
    print(f"  jours où le plan change : {changed}/{len(days)} ; jours où il passe de [] à au moins un combiné : {empty_to_some}")


def a15():
    """A15 — (a) horizon réel de production (jour du match moins jour de calcul) : de combien le repos était sous-estimé ; écart de calibration (réalisé - annoncé) des lignes de buts par sens
    (plus / moins) selon l'horizon ; (b) paires (id, marché) vues à deux dates ; (c) cumul de means (n, rapport réel / attendu) avec la clé d'avant (jour de suivi) et la clé d'après (jour du match).
    Le rejeu de la saison passée passe déjà le jour du match à predict : il ne montre RIEN sur le repos, d'où cette mesure sur le journal de production."""
    from datetime import date as D
    from kelly import calib, lanestats as LS, means, store
    fair = [r for r in store.load("fair.json", []) if isinstance(r, dict)]
    ens = LS.journal_rows()
    print("A15 — un match = un point (means) ; repos jusqu'au jour du match")
    print(f"  source : fair.json {window(fair)} ({len(fair)} lignes) ; journal d'ensemble {window(ens)} ({len(ens)} lignes)")

    def hz(r):
        try:
            return (D.fromisoformat(str(r["commence"])[:10]) - D.fromisoformat(str(r["date"])[:10])).days
        except (KeyError, ValueError, TypeError):
            return None
    for name, rows in (("fair.json", fair), ("journal d'ensemble", ens)):
        c = defaultdict(int)
        for r in rows:
            h = hz(r)
            if h is not None:
                c[min(max(h, -1), 4)] += 1
        tot = sum(c.values())
        print(f"  (a) horizon {name} (jours, -1 = veille Kelly, 4 = 4 et plus) : " + ", ".join(f"{k:+d}: {v}" for k, v in sorted(c.items())) + (f" — {sum(v for k, v in c.items() if k >= 1)}/{tot} lignes à horizon >= 1 jour" if tot else ""))
    print("      repos sous-estimé de `horizon` jours pour ces lignes ; barème de strength._rest_adj : <= 3 j : -3 %, 4 j : -1,5 %, plus : 0 (effet seulement pour les équipes dont le vrai repos dépasse 3 j - horizon).")
    print("  (a') calibration (réalisé - annoncé) par FAMILLE et par SENS, selon l'horizon — format de l'Étape 1 (step1_ledger : calibration par sens, lignes générées / choisies) ; ici toutes les lignes réglées de fair.json :")
    fams = sorted({calib.family(r.get("market") or "") for r in fair if r.get("result") in ("win", "loss")})
    for fam in fams:
        for sd in ("plus", "moins"):
            cells = []
            for h_lbl, ok in (("h<=0", lambda h: h is not None and h <= 0), ("h>=1", lambda h: h is not None and h >= 1)):
                g = [r for r in fair if r.get("result") in ("win", "loss") and calib.family(r.get("market") or "") == fam and calib.side(r) == sd and ok(hz(r))]
                if g:
                    pw = sum(1 for r in g if r["result"] == "win") / len(g)
                    pa = sum(float(r.get("p0") or r.get("prob") or 0) for r in g) / len(g)
                    cells.append(f"{h_lbl} n {len(g):>4} écart {pw - pa:+.3f}")
                else:
                    cells.append(f"{h_lbl} n    0")
            print(f"      {fam:<11} {sd:<5} " + " | ".join(cells))
    seen = defaultdict(set)
    for r in fair + ens:
        seen[(r.get("id"), r.get("market"), r.get("name"), r.get("point"))].add(str(r.get("date"))[:10])
    twice = {k: v for k, v in seen.items() if len(v) > 1}
    print(f"  (b) paires (id, marché, côté, ligne) vues à plusieurs dates : {len(twice)}/{len(seen)}")
    rows = [r for r in fair + ens if r.get("mu") and r.get("val") is not None and r.get("result") in ("win", "loss", "push") and calib.family(r.get("market") or "") in means.FAMILIES]
    for lbl, keyf in (("clé d'avant (jour de suivi)", lambda r: f"{str(r.get('date') or r.get('commence') or '')[:10]}|{r.get('id')}|{r.get('market')}"),
                      ("clé d'après (jour du match)", lambda r: f"{str(r.get('commence') or r.get('date') or '')[:10]}|{r.get('id')}|{r.get('market')}")):
        done, acc = set(), defaultdict(lambda: [0, 0.0, 0.0])
        for r in rows:
            k = keyf(r)
            if k in done:
                continue
            done.add(k)
            a = acc[calib.family(r["market"])]
            a[0] += 1
            a[1] += float(r["val"])
            a[2] += float(r["mu"])
        print(f"  (c) means, {lbl} : " + (" ; ".join(f"{f} n {a[0]} réel/attendu {a[1] / a[2]:.3f}" for f, a in sorted(acc.items())) or "aucune ligne avec mu et val"))


def a2():
    """A2 — matchs terminés après prolongation / tirs au but (AET, PEN) dans les listes du jour d'API-Football (fixtures_day.json) et lignes de stats réglées dessus (journal d'ensemble, fair.json) :
    celles-là auraient été réglées sur les stats de 120 minutes par la voie du journal (_cached_fstats). fixtures.json NE sert PAS (statut figé au premier passage : « NS » pour 148 matchs)."""
    from kelly import lanestats as LS, settle, store
    days = store.load("fixtures_day.json", {})
    et = {}
    total = 0
    for day, lst in (days.items() if isinstance(days, dict) else []):
        for f in lst if isinstance(lst, list) else []:
            total += 1
            if f.get("status") in ("AET", "PEN"):
                et[(day, f.get("home"), f.get("away"))] = f["status"]
    print("A2 — prolongation / tirs au but")
    print(f"  source : fixtures_day.json de la copie : {total} matchs sur {len(days) if isinstance(days, dict) else 0} jour(s) ; AET / PEN : {len(et)}")
    stat_mk = set(settle.FD_MARKETS) | set(settle.SHOT_MARKETS)
    for name, rows in (("fair.json", store.load("fair.json", [])), ("journal d'ensemble", LS.journal_rows())):
        hit = [r for r in rows if isinstance(r, dict) and r.get("market") in stat_mk and (str(r.get("commence"))[:10], r.get("home"), r.get("away")) in et]
        done = [r for r in hit if r.get("result") in ("win", "loss", "push")]
        print(f"  {name} : lignes de stats sur un match AET / PEN : {len(hit)} dont réglées {len(done)} (win/loss/push)" + "".join(f"\n      {r.get('home')} - {r.get('away')} {r.get('market')} {r.get('name')} {r.get('point')} -> {r.get('result')} ({r.get('final')})" for r in done[:8]))
    print("  (noms d'équipes comparés à l'identique : un nom différent entre la ligne et la liste du jour n'est pas compté)")


def pinnacle():
    """Complément — voie sharpgap : combien de lignes changeraient de statut si la référence était Pinnacle seul quand il est présent ? Le prix de Pinnacle seul n'est PAS gardé par ligne (prob_book / p_sharp
    = SHARP_WEIGHT x moyenne des books de référence + le reste x moyenne des autres books) : le comptage exact n'est pas reconstructible ; l'outil donne la répartition des écarts par source et le poids."""
    from kelly import config as C, track
    rows = [r for r in track.load() if isinstance(r, dict) and (r.get("sharpgap") or (r.get("mark") or {}).get("sharpgap"))]
    print("Complément — référence Pinnacle seul")
    by = defaultdict(int)
    for r in rows:
        g = r.get("gap") or {}
        by[(g.get("src"), g.get("n_sharp"))] += 1
    print(f"  source : tracked.json de la copie, {len(rows)} écarts sharp, fenêtre {window(rows)}")
    print(f"  SHARP_WEIGHT = {C.SHARP_WEIGHT} (part des books de référence dans la proba ; le reste vient des books mous)")
    print("  écarts par (source, nombre de books de référence) : " + (", ".join(f"{k}: {v}" for k, v in sorted(by.items(), key=str)) or "aucun"))
    print("  NON reconstructible : le prix de Pinnacle seul n'est pas enregistré par ligne ; pour la suite, il faudrait journaliser p_pinnacle (une décision du superviseur).")


POINTS = {"pinnacle": pinnacle, "a3": a3, "a1": a1, "a4": a4, "a13": a13, "a15": a15, "a2": a2}


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
