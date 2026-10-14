"""Étape 0a, lecture UNIQUE de la saison réservée, avec pré-déclaration : le poids de 12 % du modèle sur les buts se justifie-t-il face à l'ouverture de Pinnacle ?

    python tools/audit_final.py --write-prereg                 # 1) écrit la pré-déclaration dans audit_reserve_ledger.json (à commiter AVANT de lire)
    python tools/audit_final.py --final                        # 2) lit la saison réservée UNE fois, applique la règle, écrit le résultat dans le même fichier

Une seule procédure, fixée par l'utilisateur le 2026-10-14 avant toute lecture de la saison réservée (2025-07-01 → fin). Rien d'autre n'est demandé à ces données.
  - Configurations comparées au prix d'OUVERTURE de Pinnacle : p_ens(12 %, contexte éteint) et p_ens(12 %, contexte actuel).
  - Marchés décidés SÉPARÉMENT : 1N2 et plus/moins 2,5 buts. Métriques : distance à la clôture démarginée (KL) et log-loss contre résultats ; agrégat des 5 ligues,
    écart apparié PAR MATCH, intervalle à 95 %.
  - Règle par marché : le poids 12 % n'est conservé que si au moins une des deux configurations bat l'ouverture sur les DEUX métriques avec un intervalle qui exclut zéro
    (borne haute de l'écart < 0). Si les deux passent : celle à la plus petite distance à la clôture. Si une seule : celle-là. Si aucune : poids 0 sur ce marché et sur
    ses dérivés, et `sharpgap` seul.
  - Dérivés, choisis par l'utilisateur le 2026-10-14 : double chance ← 1N2 ; les deux équipes marquent et les buts par équipe ← plus/moins 2,5 (rattachement par parent).
  - 2 configurations × 2 marchés × 2 métriques = 8 comparaisons. Tout chiffre inattendu est noté « observé, non testé », sans décision.
Le fichier de trace est la seule chose écrite ; le cache des sources n'est que lu ; DATA_DIR est un dossier temporaire."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_clv as A  # noqa: E402
import audit_replay as R  # noqa: E402

LEDGER = os.path.join(ROOT, "audit_reserve_ledger.json")
POOL = "ouverture + 12 % modèle"
OPEN = "ouverture Pinnacle"
CONFIGS = {"contexte éteint": False, "contexte actuel": True}
MARKETS = ("1N2", "plus/moins 2,5")
METRICS = ("log-loss", "distance à la clôture")
DERIVED = {"1N2": ["double_chance"], "plus/moins 2,5": ["btts", "team_totals_home", "team_totals_away"]}        # décidé par l'utilisateur le 2026-10-14
TEAM_TOTALS = ("les buts par équipe (team_totals_home/away) suivent le plus/moins 2,5 (mêmes λ domicile/extérieur que le total) ; ils restent dans la collecte du dernier prix "
               "1xBet (décision 3) et pourront faire l'objet d'une décision propre plus tard, sur leurs données, si le CLV 1xBet les sauve")
MIN_N = 30                                    # matchs notés au minimum pour qu'une comparaison compte


def declaration():
    """Le texte pré-déclaré, tel qu'il est empreint (sha256) dans le fichier avant la lecture."""
    return {
        "declared_by": "utilisateur", "declared_on": "2026-10-14",
        "reserved": {"from": R.RESERVED_FROM, "to": R.RESERVED_TO, "divs": list(R.DEFAULT_DIVS), "read": "UNE seule fois"},
        "compared_to": "prix d'ouverture Pinnacle (PSH/PSD/PSA, P>2.5/P<2.5)",
        "configs": {"contexte éteint": "strength.predict(ctx=False)", "contexte actuel": "strength.predict(ctx=True)", "poids du modèle": 0.12},
        "markets": list(MARKETS), "metrics": list(METRICS), "interval": "95 %, écart apparié par match, agrégat 5 ligues",
        "rule": ("poids 12 % conservé sur un marché seulement si au moins une configuration bat l'ouverture sur les DEUX métriques avec une borne haute d'écart < 0 ; "
                 "les deux passent : la plus petite distance à la clôture ; une seule : celle-là ; aucune : poids 0 sur ce marché et ses dérivés, sharpgap seul"),
        "min_matches": MIN_N, "derived": DERIVED, "team_totals": TEAM_TOTALS, "comparisons": len(CONFIGS) * len(MARKETS) * len(METRICS),
        "what_it_measures": ("un PROXY de la production : strength.walk au réglage par défaut (demi-vie 110 j, sans réglage hebdomadaire), sans mélange xG (fair.with_xg), sans "
                             "calib.calibrate, avec un poids FIXE de 12 % au lieu des poids appris par famille (ensemble.json), mis en commun avec l'ouverture de Pinnacle seulement ; "
                             "cinq ligues football-data seulement, 1N2 et plus/moins 2,5 seulement. Le verdict ne peut pas être lu comme plus que cela."),
        "other_reading": "aucune ; tout chiffre inattendu est noté « observé, non testé », sans décision",
    }


def sha(decl):
    return hashlib.sha256(json.dumps(decl, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_atomic(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def metric_vals(recs, market):
    """({métrique: écarts appariés par match 'ouverture + 12 % modèle' - 'ouverture'}, KL moyenne du mélange)."""
    if market == "1N2":
        ll = [R.ll3(r["pred"][POOL][0], r["k3"]) - R.ll3(r["pred"][OPEN][0], r["k3"]) for r in recs]
        kl = [R.kl3(r["q_c3"], r["pred"][POOL][0]) - R.kl3(r["q_c3"], r["pred"][OPEN][0]) for r in recs]
        klm = [R.kl3(r["q_c3"], r["pred"][POOL][0]) for r in recs]
    else:
        ll = [R.ll2(r["pred"][POOL][1], r["over"]) - R.ll2(r["pred"][OPEN][1], r["over"]) for r in recs]
        kl = [R.kl2(r["q_co"], r["pred"][POOL][1]) - R.kl2(r["q_co"], r["pred"][OPEN][1]) for r in recs]
        klm = [R.kl2(r["q_co"], r["pred"][POOL][1]) for r in recs]
    return {METRICS[0]: ll, METRICS[1]: kl}, (sum(klm) / len(klm) if klm else None)


def compare(runs):
    """{marché: {config: {'n', 'log-loss': (m, h), 'distance à la clôture': (m, h), 'passes': bool, 'kl_mix': moyenne}}} : les 8 comparaisons."""
    out = {}
    for market in MARKETS:
        out[market] = {}
        for name, recs in runs.items():
            vals, klm = metric_vals(recs, market)
            cell = {"n": len(recs), "kl_mix": klm}
            ok = len(recs) >= MIN_N
            for metric in METRICS:
                n, m, h = A.stats(vals[metric])
                cell[metric] = (m, h)
                ok = ok and h is not None and m + h < 0
            cell["passes"] = ok
            out[market][name] = cell
    return out


def decide(table):
    """Applique la règle pré-déclarée -> {marché: {'weight': 0.12 | 0.0, 'config': nom | None, 'why': texte}}."""
    out = {}
    for market, cells in table.items():
        ok = [n for n, c in cells.items() if c["passes"]]
        if not ok:
            out[market] = {"weight": 0.0, "config": None, "why": "aucune configuration ne bat l'ouverture sur les deux métriques : poids 0, sharpgap seul",
                           "derived_weight_zero": DERIVED[market]}
        elif len(ok) == 1:
            out[market] = {"weight": 0.12, "config": ok[0], "why": "seule configuration qui bat l'ouverture sur les deux métriques", "derived_weight_zero": []}
        else:
            best = min(ok, key=lambda n: cells[n]["kl_mix"])
            out[market] = {"weight": 0.12, "config": best, "why": "les deux passent : plus petite distance à la clôture", "derived_weight_zero": []}
    return out


def report(table, decision, counts):
    out = ["=" * 100, "SAISON RÉSERVÉE — LECTURE UNIQUE (règle pré-déclarée du 2026-10-14)", "=" * 100,
           f"   {R.RESERVED_FROM} → {R.RESERVED_TO}, 5 ligues ; matchs notés par configuration : " + ", ".join(f"{k} {v}" for k, v in counts.items()),
           f"   {len(CONFIGS) * len(MARKETS) * len(METRICS)} comparaisons pré-déclarées : « ouverture + 12 % modèle » moins « ouverture » (négatif = le modèle améliore)", ""]
    for market, cells in table.items():
        out.append(f"   {market}")
        for name, c in cells.items():
            parts = []
            for metric in METRICS:
                m, h = c[metric]
                parts.append(f"{metric} {m:+.5f} ± {h:.5f}" if m is not None and h is not None else f"{metric} n/a")
            out.append(f"     {name:<18} n={c['n']:<5} " + " | ".join(parts) + f"   -> {'PASSE (bat l’ouverture sur les deux)' if c['passes'] else 'ne passe pas'}")
        d = decision[market]
        out.append(f"     DÉCISION : poids {d['weight']:.2f}" + (f", configuration « {d['config']} »" if d["config"] else "") + f" — {d['why']}"
                   + (f" ; poids 0 aussi sur : {', '.join(d['derived_weight_zero'])}" if d["derived_weight_zero"] else ""))
        out.append("")
    out += ["   La décision s'applique telle quelle (relecture adverse, sans autre modification dans le même commit). Tout autre chiffre ci-dessus est « observé, non testé »."]
    return "\n".join(out)


def main(argv=None, loader=None, ledger=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-prereg", action="store_true")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--cache-dir")
    args = ap.parse_args(argv)
    if args.write_prereg == args.final:
        print("Choisir exactement un mode : --write-prereg ou --final.", file=sys.stderr)
        return 2
    path = ledger or LEDGER                         # chemin FIXE à la racine du dépôt : pas d'option pour en choisir un autre (ce serait une seconde lecture)
    if args.write_prereg:
        if os.path.exists(path):
            print(f"Pré-déclaration déjà écrite ({path}) : refus de la réécrire.", file=sys.stderr)
            return 2
        decl = declaration()
        write_atomic(path, {"prereg": decl, "prereg_sha": sha(decl), "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        print(f"Pré-déclaration écrite dans {path} (sha256 {sha(decl)[:16]}…). À commiter AVANT de lancer --final.")
        return 0
    # --final
    if not os.path.exists(path):
        print("Pas de pré-déclaration : lancer d'abord --write-prereg et la commiter.", file=sys.stderr)
        return 2
    doc = json.load(open(path))
    if "final" in doc:
        print(f"La saison réservée a déjà été lue le {doc['final'].get('at')} : une saison réservée ne se relit pas.", file=sys.stderr)
        return 2
    if sha(doc.get("prereg") or {}) != doc.get("prereg_sha") or doc["prereg"] != declaration():
        print("La pré-déclaration du fichier diffère de celle du code : refus (elle ne se modifie pas après coup).", file=sys.stderr)
        return 2
    lock = path + ".lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        print(f"Une lecture est déjà en cours ou a été interrompue ({lock}) : vérifier puis supprimer ce verrou à la main.", file=sys.stderr)
        return 2
    try:
        return _final(args, path, doc, loader)
    finally:
        os.remove(lock)


def _final(args, path, doc, loader):
    R._isolate_env(args.cache_dir)
    by_matches = (loader or R.load_matches)(list(R.DEFAULT_DIVS))
    runs, counts = {}, {}
    for name, ctx in CONFIGS.items():
        by_div, cnt = R.run(by_matches, R.RESERVED_FROM, R.RESERVED_TO, None, ctx)
        runs[name] = [r for d in by_matches for r in by_div.get(d, [])]
        counts[name] = len(runs[name])
        if any(c.get("timeout") for c in cnt.values()):
            print("Rejeu interrompu : résultat partiel, rien n'est écrit.", file=sys.stderr)
            return 3
    ids = [[r["id"] for r in v] for v in runs.values()]
    if any(i != ids[0] for i in ids) or not ids[0]:
        print("Les deux configurations ne notent pas les mêmes matchs (ou aucun match) : rien n'est écrit.", file=sys.stderr)
        return 3
    table = compare(runs)
    decision = decide(table)
    txt = report(table, decision, counts)
    by_league = {}
    for r in next(iter(runs.values())):
        by_league[r["div"]] = by_league.get(r["div"], 0) + 1
    doc["final"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "matches": len(ids[0]), "by_league": by_league,
                    "ids_sha": hashlib.sha256("\n".join(sorted(ids[0])).encode()).hexdigest(), "decision": decision,
                    "table": {mk: {n: {k: (list(v) if isinstance(v, tuple) else v) for k, v in c.items()} for n, c in cells.items()} for mk, cells in table.items()},
                    "report_sha": hashlib.sha256(txt.encode()).hexdigest()}
    write_atomic(path, doc)                          # la trace d'ABORD : le verdict n'est affiché qu'une fois la lecture enregistrée comme faite
    print(txt)
    print(f"\nTrace écrite dans {path} : À COMMITER.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
