"""Étape 0a, point 1 : quelle configuration des facteurs de contexte du modèle de buts se rapproche le plus du prix d'ouverture de Pinnacle ?

    python tools/audit_ctx.py [--cache-dir DOSSIER] [--divs E0,SP1,I1,D1,F1] [--max-seconds 900]

FENÊTRE DE DÉVELOPPEMENT SEULEMENT (voir tools/audit_replay.py : échauffement jamais noté, saison réservée jamais lue ici). Même rejeu, mêmes verrous
contre la fuite d'information : modèle ajusté sur le passé strict, marché mis en commun = ouverture Pinnacle, clôture démarginée = cible.

Cinq configurations des facteurs de contexte des buts (strength.CTX_PARTS = forme, split domicile/extérieur, repos, face-à-face, enjeu) :
  1. ctx complet (production) ; 2. sans face-à-face ; 3. sans forme ; 4. sans face-à-face ni forme ; 5. ctx entièrement éteint.
(« forme » = l'écart buts réels / attendus des 5 derniers matchs ; le split domicile/extérieur est un facteur à part, gardé en 2, 3, 4.)
Pour chaque configuration et chaque marché (1N2, plus/moins 2,5), sur l'agrégat des ligues : log-loss contre les résultats et distance (KL) à la clôture,
du modèle seul puis mis en commun à 12 % avec l'ouverture, avec l'écart apparié PAR MATCH à l'ouverture de Pinnacle et à la configuration actuelle.
Règle de choix, fixée avant de regarder : la configuration retenue pour la suite est celle dont le modèle seul a la plus petite somme des deux distances
moyennes (1N2 + plus/moins 2,5) à la clôture. Ce choix est fait sur le développement et sera testé UNE fois sur la saison réservée."""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_clv as A  # noqa: E402
import audit_replay as R  # noqa: E402

CONFIGS = (
    ("ctx complet (production)", True),
    ("sans face-à-face", ("form", "split", "rest", "stakes")),
    ("sans forme", ("split", "rest", "h2h", "stakes")),
    ("sans face-à-face ni forme", ("split", "rest", "stakes")),
    ("ctx éteint", False),
)
POOL = "ouverture + 12 % modèle"


def collect(by_matches, test_from, test_to, seconds):
    """{nom de configuration: liste d'enregistrements (même ordre, mêmes matchs)}. Les cinq rejeux notent exactement les mêmes matchs ou le rapport refuse."""
    out = {}
    for name, ctx in CONFIGS:
        by_div, _ = R.run(by_matches, test_from, test_to, seconds, ctx)
        out[name] = [r for div in by_matches for r in by_div.get(div, [])]
    ids = [[r["id"] for r in recs] for recs in out.values()]
    if any(i != ids[0] for i in ids):
        raise RuntimeError("les configurations ne notent pas les mêmes matchs : comparaison impossible")
    return out


def vals(recs, pred, market):
    """(log-loss par match, KL à la clôture par match) du prédicteur `pred` sur `market` ('1x2' ou 'ou25')."""
    if market == "1x2":
        return ([R.ll3(r["pred"][pred][0], r["k3"]) for r in recs], [R.kl3(r["q_c3"], r["pred"][pred][0]) for r in recs])
    return ([R.ll2(r["pred"][pred][1], r["over"]) for r in recs], [R.kl2(r["q_co"], r["pred"][pred][1]) for r in recs])


def diff(a, b):
    return [x - y for x, y in zip(a, b)]


def report(runs, label="développement"):
    first = next(iter(runs.values()))
    out = ["=" * 110, f"CONFIGURATIONS DU CONTEXTE DES BUTS ({label}) — {len(first)} matchs, mêmes matchs pour les cinq configurations", "=" * 110]
    if not first:
        return "\n".join(out + ["   Aucun match noté."])
    opening = {m: vals(first, "ouverture Pinnacle", m) for m in ("1x2", "ou25")}
    base = {(m, pr): vals(runs[CONFIGS[0][0]], pr, m) for m in ("1x2", "ou25") for pr in ("modèle seul", POOL)}      # chaque prédicteur contre LUI-MÊME en ctx actuel
    n_cmp = 0
    scores = {}
    for market, title in (("1x2", "1N2"), ("ou25", "Plus/moins 2,5 buts")):
        out += ["", f"   {title}", f"     {'configuration':<30}{'prédicteur':<26}{'log-loss':<18}{'KL à la clôture':<20}{'log-loss vs ouverture':<30}{'KL vs ouverture':<28}KL vs ctx actuel"]
        o_ll, o_kl = opening[market]
        out.append(f"     {'(prix d’ouverture Pinnacle)':<30}{'':<26}{R._fmt(o_ll):<18}{R._fmt(o_kl):<20}")
        for name, _ in CONFIGS:
            recs = runs[name]
            for pred in ("modèle seul", POOL):
                ll, kl = vals(recs, pred, market)
                n_cmp += 3 if name != CONFIGS[0][0] else 2
                vs_cur = "" if name == CONFIGS[0][0] else R._delta(diff(kl, base[(market, pred)][1]))
                out.append(f"     {name:<30}{pred:<26}{R._fmt(ll):<18}{R._fmt(kl):<20}{R._delta(diff(ll, o_ll)):<30}{R._delta(diff(kl, o_kl)):<28}{vs_cur}")
                if pred == "modèle seul":
                    scores[name] = scores.get(name, 0.0) + A.stats(kl)[1]
    best = min(scores, key=scores.get)
    out += ["", "   Somme des deux distances moyennes à la clôture, modèle seul (plus petit = plus proche) :"]
    for name, sc in sorted(scores.items(), key=lambda kv: kv[1]):
        out.append(f"     {name:<30}{sc:.4f}" + ("   <- configuration retenue par la règle (à tester une fois sur la saison réservée)" if name == best else ""))
    ref = sum(A.stats(opening[m][1])[1] for m in ("1x2", "ou25"))
    out += ["", f"   Pour mémoire, le prix d'ouverture est à {ref:.4f} de la clôture (il en est naturellement le plus proche) ; aucune configuration du modèle seul n'est",
            "   « compétitive » tant que ses écarts « vs ouverture » restent marqués « pire ».",
            f"   {n_cmp} comparaisons faites : à 95 %, ≈ {n_cmp * 0.05:.0f} peuvent paraître significatives par hasard ; seule la saison réservée tranche.",
            "   Limites : 1N2 et plus/moins 2,5 uniquement ; ligues football-data les plus efficientes ; pas de 1xBet ; réglage par défaut du modèle ; le choix de",
            "   configuration se fait sur le développement et n'est PAS une décision de production."]
    return "\n".join(out)


def main(argv=None, loader=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir")
    ap.add_argument("--divs", default=",".join(R.DEFAULT_DIVS))
    ap.add_argument("--max-seconds", type=int, default=900)
    ap.add_argument("--warmup-end", default=R.WARMUP_END)
    ap.add_argument("--reserved-from", default=R.RESERVED_FROM)
    args = ap.parse_args(argv)
    R._isolate_env(args.cache_dir)
    divs = [d for d in args.divs.split(",") if d]
    by_matches = (loader or R.load_matches)(divs)
    runs = collect(by_matches, args.warmup_end, args.reserved_from, args.max_seconds)
    print(report(runs, f"{args.warmup_end} → {args.reserved_from}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
