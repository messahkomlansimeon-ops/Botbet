"""Étape 1, nettoyage de la chaîne de corrections (step1_ledger.json, extension du 17/10, écrite AVANT le code) : trois couches empiriques s'empilent sur les mêmes résultats
(means : moyennes, calib : probabilités, leg_discount : jambes). Rejeu walk-forward sur les CSV football-data DÉJÀ en cache (aucun réseau, DATA_DIR temporaire, cache jamais écrit,
matchs datés AVANT 2025-07-01 : la fenêtre réservée n'est jamais lue), lots de 3 jours, modèle ajusté sur les seuls matchs antérieurs, match noté à partir de la 8e journée des deux équipes.

    python tools/audit_chain.py [--cache-dir DOSSIER] [--divs E0,SP1,...] [--jobs 8] [--max-seconds 10800] [--smoke]
                                [--no-means] [--no-calib] [--no-leg-discount]

Configurations (une couche à la fois, dans l'ordre) : C0 = p0 et mu BRUTS ; C1 = + means (facteurs réel / attendu et dispersion mesurés sur les seuls JOURS D'AVANT, comme kelly/replay.py) ;
C2 = + calib (kelly/calib.py accumulée sur les seuls jours d'avant, par famille / ligue / sens / tranche). leg_discount : non évaluable en rejeu (aucun combiné rejoué) ; le drapeau est
accepté, sans effet ici, et le rapport le dit. Un drapeau --no-X retire la couche de la chaîne.
Critères (ledger) : means = log-vraisemblance binomiale négative des COMPTES (HC/AC, HY/AY, HS/AS, HST/AST, HF/AF), écart apparié par match C1 - C0 ; calib = log-loss binaire des lignes
choisies par le rejeu (fair.best_per_family sur les chiffres BRUTS, mêmes lignes dans les trois configurations), écart apparié par match C2 - C1 ; IC à 95 % par match, par famille et par sens.
La règle est CALCULÉE et rapportée, jamais appliquée : aucun réglage n'est modifié par cet outil."""
import argparse
import math
import os
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

RESERVED_FROM = "2025-07-01"
STEP_DAYS = 3
MIN_PRIOR = 7
SEASONS = ("2223", "2324", "2425")
FAMILIES = ("corners", "yellow", "shots", "sot", "fouls")
MIN_MATCHES = 30
EPS = 1e-4


def isolate_env(cache_dir=None):
    for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
        os.environ.pop(k, None)
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-audit-chain-")
    if cache_dir:
        os.environ["SOURCES_CACHE_DIR"] = cache_dir


def logloss(p, y):
    p = min(1 - EPS, max(EPS, p))
    return -(math.log(p) if y else math.log(1 - p))


def stats(xs, groups):
    """(n, moyenne, demi-marge à 95 % par grappes de match) ; demi-marge None sous deux grappes."""
    n = len(xs)
    if not n:
        return 0, None, None
    m = sum(xs) / n
    cl = defaultdict(float)
    for x, g in zip(xs, groups):
        cl[g] += x - m
    k = len(cl)
    if k < 2:
        return n, m, None
    return n, m, 1.96 * math.sqrt(k / (k - 1) * sum(v * v for v in cl.values())) / n


def replay_div(args):
    """Rejeu d'une division : -> (enregistrements, comptes). Un enregistrement par match noté : {id, date, div, nll: {fam: {cfg: nll}}, picks: [{fam, sens, y, p: {cfg: p}}]}."""
    div, layers, cache_dir, deadline = args
    if not os.environ.get("DATA_DIR"):
        isolate_env(cache_dir)
    from kelly import calib, fair, means, replay, season, settle, strength as S
    from kelly.sources import features, footballdata as fd
    sk = fd.DIV_TO_SPORT.get(div, "soccer_epl")
    matches = fd.cached_history([div], SEASONS)
    past = sorted((m for m in matches if m["date"] < RESERVED_FROM and m.get("hg") is not None and m.get("ag") is not None), key=lambda m: (m["date"], m["home"]))
    cnt = {"scored": 0, "too_early": 0, "no_model": 0, "timeout": 0, "errors": 0}
    if len(past) < 60:
        return [], cnt
    hl, upper = S.hl_for(div), S.upper_matches(div)
    seen, preds, model, start = {}, [], None, None
    for m in past:                                                     # 1er temps : prévisions (le modèle ne voit que le passé)
        d = date.fromisoformat(m["date"])
        key = season.season_start(sk, d).isoformat()
        n_h, n_a = seen.get((key, m["home"]), 0), seen.get((key, m["away"]), 0)
        for t in (m["home"], m["away"]):
            seen[(key, t)] = seen.get((key, t), 0) + 1
        if min(n_h, n_a) < MIN_PRIOR:
            cnt["too_early"] += 1
            continue
        if time.time() > deadline:
            cnt["timeout"] = 1
            break
        if start is None or d >= start + timedelta(days=STEP_DAYS):
            start = d
            model = S.build(past, d, hl, div=div, upper=upper)
        pred = S.predict(model, m["home"], m["away"], d, m.get("referee")) if model else None
        if not pred:
            cnt["no_model"] += 1
            continue
        one = {}
        replay.mean_stats(pred, m, one)
        preds.append((m, d, pred, one))
    recs, run, pending, day, acc = [], {}, {}, None, {}
    pend_cal = []
    with means.off():
        for m, d, pred, one in preds:
            try:
                if d != day:                                           # les jours d'AVANT seulement : moyennes et calibration mises à jour au changement de jour
                    replay._add_means(run, pending)
                    for r, won in pend_cal:
                        calib.accumulate(acc, r, won)
                    pending, pend_cal, day = {}, [], d
                fix = ({f: a[1] / a[2] for f, a in run.items() if a[0] >= means.BOOT_MIN and a[2] > 0}, {f: means.disp_of(a) for f, a in run.items()})
                preds_by = {"C0": pred, "C1": means.apply(pred, *fix) if "means" in layers else pred}
                rec = {"id": f"{div}|{m['date']}|{m['home']}|{m['away']}", "date": m["date"], "div": div, "nll": {}, "picks": []}
                for fam in FAMILIES:
                    obs = S._pair(m, fam)
                    if obs is None:
                        continue
                    row = {}
                    for cfg, pr in preds_by.items():
                        s = (pr.get("stats") or {}).get(fam)
                        if not s:
                            row = None
                            break
                        row[cfg] = _nll_side(obs, s["h"], s["a"], s["al"])
                    if row:
                        rec["nll"][fam] = row
                # lignes choisies sur les chiffres BRUTS (C0), évaluées aux trois configurations
                opts = {}
                for cfg, pr in preds_by.items():
                    f = {"sources": []}
                    features.fill_deep(f, pr)
                    o, bases = fair._options(f, pr["home"], pr["away"])
                    opts[cfg] = ({(mk, nm, pt): p for mk, nm, pt, p, _ in o}, bases, o)
                if opts["C0"][2]:
                    fst = settle.fd_stats(m)
                    ev = {"scores": [{"name": pred["home"], "score": str(m["hg"])}, {"name": pred["away"], "score": str(m["ag"])}]}
                    for market, name, point, p0, _ in fair.best_per_family(opts["C0"][2], replay.MARKETS, opts["C0"][1]):
                        r = {"market": market, "name": name, "point": point, "home": pred["home"], "away": pred["away"], "sport_key": sk}
                        res = settle.settle_pick(r, ev, settle.fd_stats(m))
                        if res not in ("win", "loss"):
                            continue
                        fam, sd = calib.family(market), calib.side(r)
                        q1 = opts["C1"][0].get((market, name, point))
                        p1 = (fair.shrink(q1) if q1 is not None else p0) if "means" in layers else p0
                        p2 = calib.calibrate(market, p1, data=acc, sport_key=sk, side=sd) if "calib" in layers else p1
                        rec["picks"].append({"fam": fam, "sens": sd, "y": 1 if res == "win" else 0, "p": {"C0": p0, "C1": p1, "C2": p2}})
                        pend_cal.append((dict(r, p0=p1, prob=p1), res == "win"))
                replay._add_means(pending, one)
                recs.append(rec)
                cnt["scored"] += 1
            except Exception as e:                                     # noqa: BLE001 - un match dégénéré est écarté pour TOUTES les configurations
                cnt["errors"] += 1
                cnt.setdefault("first_error", f"{div} {m['date']} {m['home']}-{m['away']} : {type(e).__name__}: {e}")
    return recs, cnt


def _nll_side(obs, h, a, alpha):
    import season_replay as R1
    return R1.nll_side(obs, h, a, alpha)


def table(recs, layers):
    """Écarts appariés par match : count (means : C1 - C0) par famille ; log-loss (means : C1 - C0, calib : C2 - C1, chaîne : C2 - C0) par famille et par sens."""
    out = {"count": {}, "ll": {}}
    for fam in FAMILIES:
        rs = [r for r in recs if fam in r["nll"]]
        xs = [r["nll"][fam]["C1"] - r["nll"][fam]["C0"] for r in rs]
        out["count"][fam] = stats(xs, [r["id"] for r in rs])
    for contrast in (("C1", "C0"), ("C2", "C1"), ("C2", "C0")):
        for fam in FAMILIES + ("all",):
            for sens in ("plus", "moins", "all"):
                xs, gs = [], []
                for r in recs:
                    ps = [p for p in r["picks"] if (fam == "all" or p["fam"] == fam) and (sens == "all" or p["sens"] == sens)]
                    if ps:
                        xs.append(sum(logloss(p["p"][contrast[0]], p["y"]) - logloss(p["p"][contrast[1]], p["y"]) for p in ps) / len(ps))       # un point par match (moyenne de ses lignes)
                        gs.append(r["id"])
                out["ll"][(contrast, fam, sens)] = stats(xs, gs)
    return out


def verdict(n, m, h):
    if h is None or n < MIN_MATCHES:
        return "non lisible"
    return "MIEUX" if m + h < 0 else "PIRE" if m - h > 0 else "indécis"


LIMITS = ("Limites (à lire avec le tableau) : le verdict porte sur une couche idéale apprise sur TOUS les matchs de la division, pas sur la couche de production : means sans rapprochement n / (n + 40), sans bornes 0,80-1,25, sans",
          "demi-vie de 90 jours, sans point de départ du rejeu ni étage championnat, dispersion de la division ; calib avec poids 1 par ligne (la production pèse 1 / k par match), sans vieillissement, par division, sans amorce. C0 « brut » contient",
          "déjà le contexte et l'ancrage marché de strength.predict / build (le nettoyage du contexte vient APRÈS cette chaîne, ledger) ; les lignes sont choisies sur C0 dans la plage de FAIR_P_RANGE : biais du gagnant possible en faveur de C0.",
          "Football-data ne voit pas l'écart de comptage des autres sources ni de 1xBet : calib ne peut être jugée ici que sur la distribution des CSV. Environ 60 verdicts MIEUX / PIRE à 95 % sont émis sans correction de multiplicité :",
          "lire d'abord les lignes « toutes » et « all » (cellules primaires déclarées à l'amendement du 17/10) ; une seule cellule isolée n'est pas une preuve.")


def report(tab, counts, divs, layers, flags):
    off = [f for f in flags if f]
    lines = ["Chaîne de corrections (step1_ledger.json, extension du 17/10) : rejeu walk-forward, matchs avant " + RESERVED_FROM,
             f"divisions : {', '.join(divs)} ; matchs notés : {counts['scored']} (trop tôt : {counts['too_early']}, sans modèle : {counts['no_model']}"
             + (f", écartés sur erreur : {counts['errors']}" if counts["errors"] else "") + ")" + (" ; ARRÊTÉ PAR LE TEMPS" if counts["timeout"] else ""),
             "couches actives : " + (", ".join(sorted(layers)) or "aucune") + (f" ; retirées : {', '.join(off)}" if off else "")
             + " ; leg_discount : non évaluable en rejeu (aucun combiné rejoué), voir calib.json _combo",
             "écart apparié par match, négatif = la couche améliore ; IC à 95 % par match ; verdict MIEUX / PIRE seulement avec 30 matchs et un IC qui exclut zéro", "",
             "1. means (C1 - C0), log-vraisemblance binomiale négative des comptes, par match :"]
    for fam in FAMILIES:
        n, m, h = tab["count"][fam]
        lines.append(f"   {fam:<8} n={n:<5} " + ("n/a" if m is None else f"{m:+.4f}" + (f" ± {h:.4f}" if h is not None else "")) + f"  {verdict(n, m, h) if n else ''}")
    for title, contrast in (("2. means (C1 - C0), log-loss binaire des lignes choisies", ("C1", "C0")), ("3. calib (C2 - C1), log-loss binaire des lignes choisies", ("C2", "C1")),
                            ("4. chaîne (C2 - C0), log-loss binaire des lignes choisies", ("C2", "C0"))):
        lines += ["", title + " :"]
        for fam in FAMILIES + ("all",):
            cells = []
            for sens in ("plus", "moins", "all"):
                n, m, h = tab["ll"][(contrast, fam, sens)]
                cells.append(f"{sens}: " + ("n/a" if m is None else f"n={n} {m:+.4f}" + (f" ± {h:.4f}" if h is not None else "") + f" {verdict(n, m, h)}"))
            lines.append(f"   {'toutes' if fam == 'all' else fam:<8} " + " | ".join(cells))
    return "\n".join(lines + ["", *LIMITS])


def combo_legs_line(data_dir):
    """Information sur leg_discount (non évaluable en rejeu) : jambes de combinés RÉGLÉES de la sauvegarde (calib.json « _combo » = [jambes, gagnées, Σ annoncé], vieilli) -> texte."""
    import json
    import shutil
    import sqlite3
    src = os.path.join(data_dir, "kelly.db")
    if not os.path.exists(src):
        return "leg_discount : sauvegarde absente"
    tmp = tempfile.mkdtemp(prefix="kelly-audit-")
    shutil.copy(src, os.path.join(tmp, "kelly.db"))
    con = sqlite3.connect(f"file:{os.path.join(tmp, 'kelly.db')}?mode=ro", uri=True)
    try:
        row = con.execute("select body from docs where name=?", ("calib.json",)).fetchone()
        acc = (json.loads(row[0]) if row else {}).get("_combo")
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
    if not (isinstance(acc, list) and len(acc) == 3 and acc[0] > 0 and acc[2] > 0):
        return "leg_discount : aucune jambe de combiné réglée comptée"
    n, w, p = acc
    return (f"leg_discount (information, non évaluable en rejeu) : {n:.0f} jambes de combinés réglées (poids vieillis) : {w / n:.1%} gagnées pour {p / n:.1%} annoncées, "
            f"rapport {w / p:.3f} (< 1 : jambes surestimées, d'où une remise ; > 1 : sous-estimées) ; pas d'intervalle (cumul pondéré sans identifiant de match)")


def layers_from(args):
    """(couches actives, drapeaux de retrait) : --no-means, --no-calib retirent la couche de la chaîne ; --no-leg-discount est accepté sans effet (voir la docstring)."""
    layers = {"means", "calib"} - ({"means"} if args.no_means else set()) - ({"calib"} if args.no_calib else set())
    return layers, ["--no-means" if args.no_means else "", "--no-calib" if args.no_calib else "", "--no-leg-discount" if args.no_leg_discount else ""]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir")
    ap.add_argument("--divs")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--max-seconds", type=int, default=10800)
    ap.add_argument("--data-dir", help="dossier qui contient kelly.db (sauvegarde) : ajoute la ligne d'information sur leg_discount")
    ap.add_argument("--smoke", action="store_true", help="essai de bon fonctionnement : UNE division, 120 s, n'affiche que des comptes")
    ap.add_argument("--no-means", action="store_true")
    ap.add_argument("--no-calib", action="store_true")
    ap.add_argument("--no-leg-discount", action="store_true", help="sans effet en rejeu (aucun combiné rejoué) ; accepté pour la chaîne pré-déclarée")
    args = ap.parse_args(argv)
    isolate_env(args.cache_dir)
    from kelly.sources import footballdata as fd
    layers, flags = layers_from(args)
    divs = [d for d in (args.divs.split(",") if args.divs else [x for x in fd.SPORT_TO_DIV.values() if x not in fd.NEW_DIVS]) if d]
    if args.smoke:
        divs, args.max_seconds = divs[:1], 120
    deadline = time.time() + args.max_seconds
    jobs = [(d, layers, args.cache_dir, deadline) for d in divs]
    recs, counts, used, first_error = [], {"scored": 0, "too_early": 0, "no_model": 0, "timeout": 0, "errors": 0}, [], None
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = [(div, ex.submit(replay_div, j)) for div, j in zip(divs, jobs)]
        for div, fut in futs:
            try:
                r, c = fut.result()
            except Exception as e:                                     # noqa: BLE001 - une division en échec ne fait pas perdre les autres
                r, c = [], {"scored": 0, "too_early": 0, "no_model": 0, "timeout": 0, "errors": 1, "first_error": f"{div} : {type(e).__name__}: {e}"}
            if r:
                used.append(div)
            recs += r
            for k in counts:
                counts[k] += c.get(k, 0)
            first_error = first_error or c.get("first_error")
            print(f"[chaîne] {div} : {c['scored']} match(s) noté(s)", file=sys.stderr)
    if first_error:
        print(f"première erreur : {first_error}", file=sys.stderr)
    if args.smoke:
        print(f"essai : {counts} (aucune table affichée)")
        return 0
    print(report(table(recs, layers), counts, used, layers, flags))
    if args.data_dir:
        print("\n5. " + combo_legs_line(args.data_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
