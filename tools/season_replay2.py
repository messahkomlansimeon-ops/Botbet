"""Rejeu pré-déclaré v2 de la voie `saison` (season_ledger.json, section « v2 », commitée AVANT le code) : a priori de marché (b), ajustement itératif (c) ou les deux (d) contre la voie actuelle (a).

    python tools/season_replay2.py [--cache-dir DOSSIER] [--divs E0,SP1,...] [--max-seconds 21600] [--jobs 8] [--smoke] [--final]

Même protocole que tools/season_replay.py : cache disque des CSV football-data seulement (aucun réseau, DATA_DIR temporaire, cache jamais écrit), matchs datés AVANT 2025-07-01
(la fenêtre réservée n'est jamais lue), un match est noté à partir de la 8e journée des deux équipes, lots de 3 jours, tous les modèles réajustés sur les seuls matchs antérieurs au lot.
Modèles : a (season.fit, k = 12), b (prior market), c (fit iterative), d (les deux), strength_hl110 (référence). Critère : log-vraisemblance binomiale négative hors échantillon, écart
apparié par match, IC à 95 %, par famille (corners, jaunes, tirs, tirs cadrés, fautes) et en AGRÉGAT (somme des cinq familles, matchs où les cinq sont observées). `--final` écrit dans v2.final,
UNE fois, refuse si la pré-déclaration v2 a changé ; la règle de décision est CALCULÉE et rapportée, jamais appliquée (aucun réglage n'est modifié par cet outil)."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_clv as A  # noqa: E402
import season_replay as R1  # noqa: E402

LEDGER = R1.LEDGER
RESERVED_FROM = R1.RESERVED_FROM
STEP_DAYS = R1.STEP_DAYS
MIN_PRIOR = R1.MIN_PRIOR
FAMILIES = R1.FAMILIES
K = 12.0
MODELS = ("a", "b", "c", "d", "strength_hl110")
CONFIGS = {"a": ("flat", "moments"), "b": ("market", "moments"), "c": ("flat", "iterative"), "d": ("market", "iterative")}
DECISION = (("b", "a"), ("c", "a"), ("d", "a"))
REFERENCE = (("a", "strength_hl110"), ("b", "strength_hl110"), ("c", "strength_hl110"), ("d", "strength_hl110"))
KEYS = FAMILIES + ("agg",)
MIN_MATCHES = 30
SEASONS = R1.SEASONS


def sha(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fit_models(past, obs, d, sk, div, upper):
    from kelly import strength as S, season
    stats = ("goals",) + FAMILIES
    out = {"strength_hl110": S.build(past, d, 110, stats, div, 0.0, upper)}
    base = out["strength_hl110"]
    fb = None
    if base:
        fb = {st: {"alpha": s.get("alpha", 0.0), "atot": s.get("atot", 0.0)} for st, s in base["stats"].items() if st != "goals"}
        fb["kappa"] = S.kappa_of(base["stats"]["goals"])
    for name, (prior, mode) in CONFIGS.items():
        out[name] = season.fit(obs, d, sk, k=K, fallback=fb, prior=prior, fit_mode=mode)
    return out


def score_match(models, m, d):
    """{famille: {modèle: nll}} du match, ou None ; une famille n'est notée que si TOUS les modèles la prédisent."""
    from kelly import strength as S, season
    preds = {}
    for name in MODELS:
        mod = models.get(name)
        if not mod:
            return None
        p = S.predict(mod, m["home"], m["away"], d, None, False) if name.startswith("strength") else season.predict(mod, m["home"], m["away"])
        if not p:
            return None
        preds[name] = p
    out = {}
    for fam in FAMILIES:
        obs = S._pair(m, fam)
        if obs is None:
            continue
        row = {}
        for name, p in preds.items():
            s = p["stats"].get(fam)
            if not s:
                row = None
                break
            row[name] = R1.nll_side(obs, s["h"], s["a"], s["al"])
        if row:
            out[fam] = row
    return out or None


def replay(matches, div, deadline=None, step_days=STEP_DAYS):
    """Notes de tous les modèles sur les matchs de la division datés avant RESERVED_FROM. -> (enregistrements, comptes)."""
    from kelly import strength as S, season
    from kelly.sources import footballdata as fd
    sk = fd.DIV_TO_SPORT.get(div, "soccer_epl")
    past = [m for m in matches if m["date"] < RESERVED_FROM]
    obs = season._from_matches(past, market=True)
    recs, cnt = [], {"scored": 0, "no_model": 0, "too_early": 0, "timeout": 0, "errors": 0}
    upper = S.upper_matches(div)
    batch_start, models, seen = None, None, {}
    for m in sorted(past, key=lambda x: x["date"]):
        d = date.fromisoformat(m["date"])
        key = season.season_start(sk, d).isoformat()
        n_h, n_a = seen.get((key, m["home"]), 0), seen.get((key, m["away"]), 0)
        for t in (m["home"], m["away"]):
            seen[(key, t)] = seen.get((key, t), 0) + 1
        if min(n_h, n_a) < MIN_PRIOR:
            cnt["too_early"] += 1
            continue
        if deadline is not None and time.time() > deadline:
            cnt["timeout"] = 1
            break
        if batch_start is None or d >= batch_start + timedelta(days=step_days):
            batch_start = d
            models = fit_models(past, obs, d, sk, div, upper)
        try:
            row = score_match(models, m, d)
        except Exception as e:                      # noqa: BLE001 - un match en erreur est écarté pour TOUS les modèles et compté
            cnt["errors"] += 1
            cnt.setdefault("first_error", f"{div} {m['date']} {m['home']}-{m['away']} : {type(e).__name__}: {e}")
            continue
        if row is None:
            cnt["no_model"] += 1
            continue
        cnt["scored"] += 1
        recs.append({"id": f"{div}|{m['date']}|{m['home']}|{m['away']}", "date": m["date"], "div": div, "nll": row})
    return recs, cnt


def _div_job(args):
    div, deadline, cache = args
    R1._isolate_env(cache)
    from kelly.sources import footballdata as fd
    matches = fd.cached_history([div], SEASONS)
    if len(matches) < 60:
        return div, [], {"scored": 0, "no_model": 0, "too_early": 0, "timeout": 0, "errors": 0}
    recs, cnt = replay(matches, div, deadline)
    return div, recs, cnt


def with_aggregate(recs):
    """Ajoute à chaque enregistrement dont les CINQ familles sont notées la famille « agg » : somme des nll par modèle."""
    for r in recs:
        if all(f in r["nll"] for f in FAMILIES):
            r["nll"] = dict(r["nll"], agg={mdl: sum(r["nll"][f][mdl] for f in FAMILIES) for mdl in MODELS})
    return recs


def table(recs):
    """{clé: {« X - Y »: {n, mean, half}}} pour les contrastes de décision et de référence."""
    out = {}
    for key in KEYS:
        rows = [r for r in recs if key in r["nll"]]
        out[key] = {}
        for x, y in DECISION + REFERENCE:
            xs = [r["nll"][key][x] - r["nll"][key][y] for r in rows]
            n, mean, half = A.stats(xs, [r["id"] for r in rows])
            out[key][f"{x} - {y}"] = {"n": n, "mean": mean, "half": half}
    return out


def decide(tab):
    """Règle du ledger v2 : X remplace (a) si l'agrégat est strictement meilleur (borne haute < 0) ET qu'aucune famille (30 matchs et plus) n'est strictement pire (borne basse > 0).
    Plusieurs : la plus petite moyenne d'agrégat, puis la plus simple (b, c, d). -> (liste des configurations qui passent, choisie ou None, détail par configuration)."""
    detail, passed = {}, []
    for x, _ in DECISION:
        agg = tab["agg"][f"{x} - a"]
        better = agg["half"] is not None and agg["n"] >= MIN_MATCHES and agg["mean"] + agg["half"] < 0
        worse = [f for f in FAMILIES if tab[f][f"{x} - a"]["half"] is not None and tab[f][f"{x} - a"]["n"] >= MIN_MATCHES and tab[f][f"{x} - a"]["mean"] - tab[f][f"{x} - a"]["half"] > 0]
        detail[x] = {"agg_better": better, "families_worse": worse, "passes": bool(better and not worse)}
        if better and not worse:
            passed.append(x)
    chosen = min(passed, key=lambda x: (tab["agg"][f"{x} - a"]["mean"], "bcd".index(x))) if passed else None
    return passed, chosen, detail


def report(tab, counts, divs, passed, chosen, detail):
    lines = ["Rejeu `saison` v2 (fenêtre de développement, matchs avant " + RESERVED_FROM + ")",
             f"divisions : {', '.join(divs)} ; matchs notés : {counts['scored']} (trop tôt : {counts['too_early']}, sans modèle : {counts['no_model']})"
             + (" ; ARRÊTÉ PAR LE TEMPS" if counts["timeout"] else "") + (f" ; {counts['errors']} match(s) écarté(s) sur erreur" if counts.get("errors") else ""),
             "log-vraisemblance binomiale négative par match (négatif = le premier est meilleur) :"]
    for key, d in tab.items():
        for c, v in d.items():
            if v["n"]:
                tag = "" if v["half"] is None or v["n"] < MIN_MATCHES else "  <- meilleur" if v["mean"] + v["half"] < 0 else "  <- pire" if v["mean"] - v["half"] > 0 else "  (indécis)"
                lines.append(f"  {key:<8}{c:<24}n={v['n']:<5}{v['mean']:+.4f}" + (f" ± {v['half']:.4f}" if v["half"] is not None else "") + tag)
    lines.append("règle : " + (f"passent {passed}, retenue : {chosen}" if passed else "aucune configuration ne passe, (a) reste") + f" ; détail {detail}")
    return "\n".join(lines)


def write_final(tab, counts, divs, recs, ledger_path=None, now=None):
    """Écrit v2.final dans le ledger, UNE fois ; refuse si v2.final existe ou si la pré-déclaration v2 ne correspond plus à son empreinte."""
    path = ledger_path or LEDGER
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    v2 = doc.get("v2") or {}
    if not v2:
        raise SystemExit("season_ledger.json : pas de section v2 (pré-déclaration absente).")
    if v2.get("final"):
        raise SystemExit("season_ledger.json : le résultat v2 est déjà écrit (une seule lecture). Une nouvelle question demande une nouvelle pré-déclaration.")
    if sha(v2["prereg"]) != v2.get("prereg_sha"):
        raise SystemExit("season_ledger.json : la pré-déclaration v2 a changé depuis son empreinte : refus.")
    passed, chosen, detail = decide(tab)
    v1_ids = (doc.get("final") or {}).get("ids_sha")
    ids_sha = hashlib.sha256("\n".join(sorted(r["id"] for r in recs)).encode()).hexdigest()
    v2["final"] = {"at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "divs": list(divs), "counts": counts, "ids_sha": ids_sha, "same_set_as_v1": ids_sha == v1_ids,
                   "table": tab, "decision_comparisons": sum(1 for k in KEYS for x, y in DECISION if tab[k][f"{x} - {y}"]["n"]),
                   "reference_comparisons": sum(1 for k in KEYS for x, y in REFERENCE if tab[k][f"{x} - {y}"]["n"]),
                   "rule": {"passed": passed, "chosen": chosen, "detail": detail, "applied": False}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir")
    ap.add_argument("--divs")
    ap.add_argument("--max-seconds", type=int, default=21600)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="essai de bon fonctionnement : UNE division, 120 s, n'affiche que des comptes (jamais la table) et n'écrit rien")
    args = ap.parse_args(argv)
    if args.final and (args.divs or args.smoke):
        raise SystemExit("--final se lance sur toutes les divisions, sans --divs ni --smoke (une seule lecture).")
    R1._isolate_env(args.cache_dir)
    from kelly.sources import footballdata as fd
    divs = [d for d in (args.divs.split(",") if args.divs else [x for x in fd.SPORT_TO_DIV.values() if x not in fd.NEW_DIVS]) if d]
    if args.smoke:
        divs, args.max_seconds, args.jobs = divs[:1], 120, 1
    deadline = time.time() + args.max_seconds
    jobs = [(d, deadline, args.cache_dir) for d in divs]
    recs, counts, used, first_error = [], {"scored": 0, "no_model": 0, "too_early": 0, "timeout": 0, "errors": 0}, [], None
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            results = list(ex.map(_div_job, jobs))
    else:
        results = [_div_job(j) for j in jobs]
    for div, r, c in results:
        if r:
            used.append(div)
        recs += r
        for k in counts:
            counts[k] += c.get(k, 0)
        first_error = first_error or c.get("first_error")
        print(f"[saison v2] {div} : {c['scored']} match(s) noté(s)", file=sys.stderr)
    if first_error:
        counts["first_error"] = first_error
    if args.smoke:
        print(f"essai : {counts} (aucune table affichée, rien écrit)")
        return
    recs = with_aggregate(recs)
    tab = table(recs)
    passed, chosen, detail = decide(tab)
    print(report(tab, counts, used, passed, chosen, detail))
    if args.final:
        missing = [x for x in divs if x not in used]
        if missing:
            raise SystemExit(f"division(s) sans match noté (données absentes ou insuffisantes) : {', '.join(missing)} : le résultat n'est pas écrit (mêmes 18 divisions que v1 exigées).")
        if counts["timeout"]:
            raise SystemExit("rejeu interrompu par le temps : le résultat n'est pas écrit (relance avec --max-seconds plus grand).")
        write_final(tab, counts, used, recs)
        print("résultat écrit dans season_ledger.json (v2.final).")


if __name__ == "__main__":
    main()
