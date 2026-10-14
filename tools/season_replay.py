"""Rejeu pré-déclaré de la voie `saison` (season_ledger.json, commité AVANT tout code) : la saison seule (kelly/season.py, k = 3 / 6 / 12) prédit-elle mieux les statistiques
d'équipe que le modèle `strength` actuel (demi-vie 110) ou qu'une variante à demi-vie 45 ?

    python tools/season_replay.py [--cache-dir DOSSIER] [--divs E0,SP1,...] [--max-seconds 10800] [--final]

Lit seulement le cache disque déjà rempli des CSV football-data (aucun réseau ; DATA_DIR est un dossier temporaire ; le cache n'est jamais écrit). La fenêtre est
celle du ledger : matchs datés AVANT 2025-07-01 (la fenêtre réservée de audit_reserve_ledger.json n'est jamais lue). Un match est noté si les deux équipes ont déjà joué au
moins 7 matchs de la saison. Chaque lot de trois jours (comme strength.walk) réajuste TOUS les modèles sur les seuls matchs antérieurs au lot : même information pour chacun.
Critère : log-vraisemblance binomiale négative hors échantillon, par équipe-côté (domicile + extérieur), somme par match ; écart apparié par match, moyenne et intervalle à 95 %
(erreur-type par match), par famille. `--final` écrit le résultat dans season_ledger.json (champ « final »), UNE seule fois ; sans lui, rapport seulement.

Dispersion (ledger) : chaque modèle la sienne, sans élargissement alpha_eff ; saison : alpha de moments de la ligue-saison avant le lot (plancher 0), repli sous 30 matchs de
ligue = l'alpha ajusté du modèle strength_hl110 à la même date. Aucun means.factor, aucun calib, aucun contexte, aucun ancrage marché (mkt = 0)."""
import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_clv as A  # noqa: E402

LEDGER = os.path.join(ROOT, "season_ledger.json")
RESERVED_FROM = "2025-07-01"                  # la fenêtre réservée (audit_reserve_ledger.json) n'est JAMAIS lue ici
STEP_DAYS = 3
MIN_PRIOR = 7                                 # matchs déjà joués cette saison par CHAQUE équipe (donc le 8e match ou plus tard)
FAMILIES = ("corners", "yellow", "shots", "sot", "fouls")
MODELS = ("saison_k3", "saison_k6", "saison_k12", "strength_hl110", "strength_hl45")
KS = {"saison_k3": 3.0, "saison_k6": 6.0, "saison_k12": 12.0}
CONTRASTS = (("saison_k3", "strength_hl110"), ("saison_k6", "strength_hl110"), ("saison_k12", "strength_hl110"), ("saison_k6", "strength_hl45"), ("strength_hl45", "strength_hl110"))
MIN_MATCHES = 30
SEASONS = ("2223", "2324", "2425")           # saisons lues si elles sont en cache (la fenêtre < 2025-07-01 ne contient que 2024-25 ; ne dépend pas de l'horloge)


def _isolate_env(cache_dir=None):
    """À appeler AVANT tout import de kelly : pas d'envoi, pas de proxy, données temporaires ; le cache des sources n'est lu qu'en lecture."""
    for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
        os.environ.pop(k, None)
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-season-replay-data-")
    if cache_dir:
        os.environ["SOURCES_CACHE_DIR"] = cache_dir


def nb_logpmf(k, mu, alpha):
    """log P(X = k) d'une binomiale négative de moyenne `mu` et variance mu + alpha x mu^2 (alpha < 1e-6 : Poisson)."""
    mu = max(mu, 1e-9)
    if alpha < 1e-6:
        return k * math.log(mu) - mu - math.lgamma(k + 1)
    r = 1.0 / alpha
    return math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu))


def nll_side(obs, h, a, alpha):
    """Log-vraisemblance négative des deux comptes observés (domicile `obs[0]`, extérieur `obs[1]`) pour les attentes (h, a) et la dispersion `alpha`."""
    return -(nb_logpmf(obs[0], h, alpha) + nb_logpmf(obs[1], a, alpha))


def replay(matches, div, deadline=None, step_days=STEP_DAYS):
    """Notes de tous les modèles sur les matchs de la division `div` datés avant RESERVED_FROM. -> (enregistrements [{id, date, div, nll: {famille: {modèle: nll}}}], comptes)."""
    from kelly import strength as S, season
    from kelly.sources import footballdata as fd
    sk = fd.DIV_TO_SPORT.get(div, "soccer_epl")
    past = [m for m in matches if m["date"] < RESERVED_FROM]               # physiquement rien de la fenêtre réservée
    obs = season._from_matches(past)
    stats = ("goals",) + FAMILIES
    recs, cnt = [], {"scored": 0, "no_model": 0, "too_early": 0, "timeout": 0, "errors": 0}
    upper = S.upper_matches(div)
    batch_start, models = None, None
    ordered = sorted(past, key=lambda m: m["date"])
    # effectifs de la saison, mis à jour au fil des matchs (un seul passage)
    seen = {}
    for m in ordered:
        d = date.fromisoformat(m["date"])
        key = (season.season_start(sk, d).isoformat(), )
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
            models = fit_models(past, obs, d, sk, div, stats, upper)
        try:
            row = score_match(models, m, d)
        except Exception as e:                      # noqa: BLE001 - une donnée dégénérée ne doit pas perdre des heures de calcul : le match est écarté pour TOUS les modèles
            cnt["errors"] = cnt.get("errors", 0) + 1
            cnt.setdefault("first_error", f"{div} {m['date']} {m['home']}-{m['away']} : {type(e).__name__}: {e}")
            continue
        if row is None:
            cnt["no_model"] += 1
            continue
        cnt["scored"] += 1
        recs.append({"id": f"{div}|{m['date']}|{m['home']}|{m['away']}", "date": m["date"], "div": div, "nll": row})
    return recs, cnt


def fit_models(past, obs, d, sk, div, stats, upper):
    """Les cinq modèles ajustés sur les matchs antérieurs au jour `d` : {nom: modèle strength ou saison}."""
    from kelly import strength as S, season
    out = {"strength_hl110": S.build(past, d, 110, stats, div, 0.0, upper), "strength_hl45": S.build(past, d, 45, stats, div, 0.0, upper)}
    base = out["strength_hl110"]
    fb = None
    if base:
        fb = {st: {"alpha": s.get("alpha", 0.0), "atot": s.get("atot", 0.0)} for st, s in base["stats"].items() if st != "goals"}
        fb["kappa"] = S.kappa_of(base["stats"]["goals"])
    for name, k in KS.items():
        out[name] = season.fit(obs, d, sk, k=k, fallback=fb)
    return out


def score_match(models, m, d):
    """{famille: {modèle: nll}} du match `m` (jour `d`), ou None si un modèle ne peut pas le prédire pour AUCUNE famille. Une famille n'est notée que si TOUS les modèles la prédisent
    (écarts appariés)."""
    from kelly import strength as S, season
    preds = {}
    for name in MODELS:
        mod = models.get(name)
        if not mod:
            return None
        if name.startswith("strength"):
            p = S.predict(mod, m["home"], m["away"], d, None, False)
        else:
            p = season.predict(mod, m["home"], m["away"])
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
            row[name] = nll_side(obs, s["h"], s["a"], s["al"])
        if row:
            out[fam] = row
    return out or None


def table(recs):
    """{famille: {« A - B »: {n, moyenne, demi-marge}}} : écarts appariés par match de la log-vraisemblance négative (négatif = A meilleur que B)."""
    out = {}
    for fam in FAMILIES:
        rows = [r for r in recs if fam in r["nll"]]
        out[fam] = {}
        for a, b in CONTRASTS:
            xs = [r["nll"][fam][a] - r["nll"][fam][b] for r in rows]
            n, m, h = A.stats(xs, [r["id"] for r in rows])
            out[fam][f"{a} - {b}"] = {"n": n, "mean": m, "half": h}
    return out


def report(tab, counts, divs):
    lines = ["Rejeu `saison` (fenêtre de développement, matchs avant " + RESERVED_FROM + ")", f"divisions : {', '.join(divs)} ; matchs notés : {counts['scored']} "
             f"(trop tôt dans la saison : {counts['too_early']}, sans modèle : {counts['no_model']})" + (" ; ARRÊTÉ PAR LE TEMPS" if counts["timeout"] else "") + (f" ; {counts['errors']} match(s) écarté(s) sur erreur" if counts.get("errors") else ""),
             "log-vraisemblance binomiale négative par match (négatif = le premier modèle est meilleur) :"]
    for fam, d in tab.items():
        for k, v in d.items():
            if v["n"]:
                tag = "" if v["half"] is None or v["n"] < MIN_MATCHES else "  <- meilleur" if v["mean"] + v["half"] < 0 else "  <- pire" if v["mean"] - v["half"] > 0 else "  (indécis)"
                lines.append(f"  {fam:<8}{k:<36}n={v['n']:<5}{v['mean']:+.4f}" + (f" ± {v['half']:.4f}" if v["half"] is not None else "") + tag)
    return "\n".join(lines)


def write_final(tab, counts, divs, recs, ledger_path=None, now=None):
    """Écrit le résultat dans le ledger (champ « final »), UNE seule fois ; refuse si « final » existe déjà. -> le document écrit."""
    path = ledger_path or LEDGER
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("final"):
        raise SystemExit("season_ledger.json : le résultat est déjà écrit (une seule lecture). Une nouvelle question demande une nouvelle pré-déclaration.")
    doc["final"] = {"at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "divs": list(divs), "counts": counts,
                    "ids_sha": hashlib.sha256("\n".join(sorted(r["id"] for r in recs)).encode()).hexdigest(), "table": tab,
                    "comparisons": sum(1 for fam in tab.values() for v in fam.values() if v["n"])}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                          # atomique : un arrêt en cours d'écriture ne corrompt jamais le ledger
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir")
    ap.add_argument("--divs")
    ap.add_argument("--max-seconds", type=int, default=10800)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="essai de bon fonctionnement : UNE division, 90 s, n'affiche que des comptes (jamais la table) et n'écrit rien")
    args = ap.parse_args(argv)
    if args.final and (args.divs or args.smoke):
        raise SystemExit("--final se lance sur toutes les divisions, sans --divs ni --smoke (une seule lecture).")
    _isolate_env(args.cache_dir)
    from kelly.sources import footballdata as fd
    divs = [d for d in (args.divs.split(",") if args.divs else [x for x in fd.SPORT_TO_DIV.values() if x not in fd.NEW_DIVS]) if d]
    if args.smoke:
        divs, args.max_seconds = divs[:1], 90
    deadline = time.time() + args.max_seconds
    recs, counts, used = [], {"scored": 0, "no_model": 0, "too_early": 0, "timeout": 0, "errors": 0}, []
    first_error = None
    for div in divs:
        matches = fd.cached_history([div], SEASONS)
        if len(matches) < 60:
            continue
        r, c = replay(matches, div, deadline)
        if r:
            used.append(div)
        recs += r
        for k in counts:
            counts[k] += c.get(k, 0)
        first_error = first_error or c.get("first_error")
        print(f"[saison] {div} : {c['scored']} match(s) noté(s)", file=sys.stderr)
        if c["timeout"]:
            break
    if first_error:
        counts["first_error"] = first_error
    if args.smoke:
        print(f"essai : {counts} (aucune table affichée, rien écrit)" + (f" ; première erreur : {first_error}" if first_error else ""))
        return
    tab = table(recs)
    print(report(tab, counts, used))
    if args.final:
        if counts["timeout"]:
            raise SystemExit("rejeu interrompu par le temps : le résultat n'est pas écrit (relance avec --max-seconds plus grand).")
        write_final(tab, counts, used, recs)
        print("résultat écrit dans season_ledger.json (champ « final »).")


if __name__ == "__main__":
    main()
