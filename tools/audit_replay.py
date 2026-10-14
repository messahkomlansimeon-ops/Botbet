"""Étape 0a de l'audit, rejeu historique GRATUIT sur football-data : le modèle bat-il la cote d'ouverture de Pinnacle pour prédire la clôture ?

    python tools/audit_replay.py [--cache-dir DOSSIER] [--divs E0,SP1,I1,D1,F1] [--max-seconds 900]
    python tools/audit_replay.py --final --ledger audit_reserve_ledger.json     # UNE SEULE fois : lit la saison réservée ; la trace reste À LA RACINE du dépôt (à commiter), jamais dans /tmp

Lit seulement le cache disque déjà rempli des CSV football-data (aucun réseau, aucune écriture dans data/ ni dans le cache ; DATA_DIR est un dossier
temporaire). Le marché 1N2 et le plus/moins 2,5 buts sont les SEULS marchés qui ont des cotes historiques : ce rejeu ne dit RIEN des corners, cartons
ni tirs, et rien de la cote 1xBet (pas d'historique) ni du moment réel de la prise (les cotes d'ouverture sont relevées des jours avant le match).

Découpage fixé AVANT la première exécution (jamais ajusté après avoir vu un résultat) :
  - ÉCHAUFFEMENT : matchs avant WARMUP_END (2025-01-01) : servent à ajuster le modèle, ne sont JAMAIS notés ;
  - DÉVELOPPEMENT : de WARMUP_END à RESERVED_FROM (2025-07-01) : la fin de la saison 2024-25, où l'on peut regarder, comparer, se tromper ;
  - RÉSERVÉE : de RESERVED_FROM à la fin : les saisons suivantes. Lue UNE fois, avec --final, qui exige --ledger et refuse de relire si le fichier existe.

Trois verrous contre la fuite d'information :
  1. le modèle de chaque lot est ajusté (strength.walk) sur les matchs STRICTEMENT antérieurs au lot ; la liste donnée au rejeu est coupée à la fin de la
     fenêtre ; l'ancrage marché de l'apprentissage ne lit que les cotes de clôture de matchs DÉJÀ JOUÉS, jamais celle du match prédit ;
  2. le marché mis en commun avec le modèle est le prix d'OUVERTURE de Pinnacle (PSH/PSD/PSA, P>2.5/P<2.5), jamais la clôture ;
  3. la clôture démarginée (PSC*, Shin) ne sert que de CIBLE de mesure.
Deux questions, côte à côte, par marché : (a) le modèle seul est-il plus proche de la clôture que l'ouverture ? (b) mis en commun avec l'ouverture à 12 % /
25 %, améliore-t-il la log-loss sur les résultats et la distance à la clôture ?"""
import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_clv as A  # noqa: E402

WARMUP_END = "2025-01-01"
RESERVED_FROM = "2025-07-01"
RESERVED_TO = "2027-01-01"
STEP_DAYS = 3
WEIGHTS = (0.12, 0.25)              # poids du modèle testés (ceux de l'ensemble : 12 % buts / 25 % stats)
EDGE_MIN = 0.03                     # écart minimal pour qu'un pari simulé soit pris : p x cote d'ouverture - 1
DEFAULT_DIVS = ("E0", "SP1", "I1", "D1", "F1")
MIN_MATCHES = 30
EPS = 1e-9


def _isolate_env(cache_dir=None):
    """À appeler AVANT tout import de kelly : pas d'envoi, pas de proxy, données temporaires ; le cache des sources n'est lu qu'en lecture."""
    for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
        os.environ.pop(k, None)
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-replay-data-")      # jamais celui du shell
    if cache_dir:
        os.environ["SOURCES_CACHE_DIR"] = cache_dir


def pinnacle_pack(m):
    """Cotes Pinnacle d'OUVERTURE et de CLÔTURE du match (1N2 et plus/moins 2,5) et leurs probas sans marge (Shin) ; None s'il en manque une."""
    from kelly import strength as S
    o = m.get("odds") or {}

    def get(mk, ph):
        return ((o.get(mk) or {}).get(ph) or {}).get("ps")
    o3, c3, oo, co = get("1x2", "open"), get("1x2", "close"), get("ou25", "open"), get("ou25", "close")
    if not (o3 and c3 and oo and co):
        return None
    q_o3, q_c3, q_oo, q_co = S._devig(o3), S._devig(c3), S._devig(oo), S._devig(co)
    if not (q_o3 and q_c3 and q_oo and q_co):
        return None
    return {"odds_o3": list(o3), "q_o3": q_o3, "q_c3": q_c3, "odds_oo": list(oo), "q_oo": q_oo[0], "q_co": q_co[0]}


def replay(matches, div, test_from, test_to, deadline=None, ctx=True):
    """Prédit pas à pas (strength.walk, réglage par défaut) les matchs de [test_from, test_to[ et rassemble, pour ceux qui ont toutes les cotes Pinnacle,
    ce qu'il faut pour mesurer. -> (liste d'enregistrements, dict de comptes : prédits, sans modèle, sans cotes, arrêté par le temps)."""
    from kelly import strength as S
    S._xg_memo.clear()                                               # buts implicites mémorisés par (div, date, équipes) : jamais d'un rejeu à l'autre
    past = [m for m in matches if m["date"] < test_to]               # physiquement rien après la fin de la fenêtre
    recs, cnt = [], {"predicted": 0, "no_model": 0, "no_odds": 0, "timeout": 0}
    for m, p3, po in S.walk(past, test_from, test_to, step_days=STEP_DAYS, ctx=ctx, div=div):
        if deadline is not None and time.time() > deadline:
            cnt["timeout"] = 1
            break
        if p3 is None:
            cnt["no_model"] += 1
            continue
        pk = pinnacle_pack(m)
        if pk is None:
            cnt["no_odds"] += 1
            continue
        cnt["predicted"] += 1
        recs.append({"id": f"{div}|{m['date']}|{m['home']}|{m['away']}", "div": div, "date": m["date"], "k3": "HDA".index(m["ftr"]),
                     "over": int(m["hg"] + m["ag"] > 2.5), "p3": list(p3), "po": po} | pk)
        recs[-1]["pred"] = predictors(recs[-1])                      # une seule fois par match
    return recs, cnt


def pool3(p, q, wm):
    """Mise en commun géométrique (= en logit pour deux issues) : wm × modèle + (1 - wm) × marché, renormalisée."""
    w = [(max(a, EPS) ** wm) * (max(b, EPS) ** (1 - wm)) for a, b in zip(p, q)]
    s = sum(w)
    return [x / s for x in w]


def predictors(r):
    """{nom: (probas 1N2, proba du plus de 2,5)} : le modèle seul, le marché d'ouverture, et les mises en commun ; la clôture est la référence."""
    out = {"modèle seul": (r["p3"], r["po"]), "ouverture Pinnacle": (r["q_o3"], r["q_oo"])}
    for wm in WEIGHTS:
        out[f"ouverture + {int(wm * 100)} % modèle"] = (pool3(r["p3"], r["q_o3"], wm), A.pool(r["po"], r["q_oo"], wm))
    return out


def ll3(p, k):
    return -math.log(max(p[k], EPS))


def ll2(p, over):
    return -math.log(max(p if over else 1 - p, EPS))


def kl3(q, p):
    return sum(a * math.log(max(a, EPS) / max(b, EPS)) for a, b in zip(q, p) if a > 0)


def kl2(q, p):
    return kl3([q, 1 - q], [p, 1 - p])


def _fmt(xs, d=4):
    n, m, h = A.stats(xs)
    return "n/a" if n == 0 else f"{m:.{d}f}" + (f" ± {h:.{d}f}" if h is not None else "")


def _delta(xs, d=4):
    n, m, h = A.stats(xs)
    if n == 0:
        return "n/a"
    verdict = "" if h is None or n < MIN_MATCHES else "  <- meilleur" if m + h < 0 else "  <- pire" if m - h > 0 else "  (indécis)"
    return f"{m:+.{d}f}" + (f" ± {h:.{d}f}" if h is not None else "") + verdict


def market_block(recs, label, ll, kl, sel):
    """Lignes de rapport d'un marché : log-loss sur les résultats et distance (KL) à la clôture, par prédicteur, et écarts appariés par match à l'ouverture."""
    names = list(recs[0]["pred"])
    lls = {n: [ll(sel(r["pred"][n]), r) for r in recs] for n in names}
    kls = {n: [kl(sel(r["pred"][n]), r) for r in recs] for n in names}
    ref_ll = [ll(sel((r["q_c3"], r["q_co"])), r) for r in recs]
    out = [f"   {label}  ({len(recs)} matchs)",
           f"     {'prédicteur':<28}{'log-loss (résultats)':<24}{'distance à la clôture (KL)':<30}écart à l'OUVERTURE (appariés par match)"]
    for n in names:
        d_ll = [a - b for a, b in zip(lls[n], lls["ouverture Pinnacle"])]
        d_kl = [a - b for a, b in zip(kls[n], kls["ouverture Pinnacle"])]
        tail = "" if n == "ouverture Pinnacle" else f"log-loss {_delta(d_ll)}   KL {_delta(d_kl)}"
        out.append(f"     {n:<28}{_fmt(lls[n]):<24}{_fmt(kls[n]):<30}{tail}")
    out.append(f"     {'clôture Pinnacle (référence)':<28}{_fmt(ref_ll):<24}{'0 (c’est la cible)':<30}")
    return out


def bets(recs, name, edge_min=EDGE_MIN):
    """Paris simulés au prix d'OUVERTURE de Pinnacle : toute issue dont (proba du prédicteur × cote d'ouverture - 1) >= edge_min (None = TOUTES les issues, sans
    sélection : la ligne de base). CLV = log(cote prise × proba de clôture sans marge) ; gain = cote × gagné - 1. -> liste de (id du match, clv, gain)."""
    out = []
    for r in recs:
        p3, po = r["pred"][name]
        sels = [(p3[i], r["odds_o3"][i], r["q_c3"][i], r["k3"] == i) for i in range(3)]
        sels += [(po, r["odds_oo"][0], r["q_co"], r["over"] == 1), (1 - po, r["odds_oo"][1], 1 - r["q_co"], r["over"] == 0)]
        for p, odd, qc, won in sels:
            if edge_min is None or p * odd - 1 >= edge_min:
                out.append((r["id"], math.log(odd * qc), odd * won - 1))
    return out


def bets_block(recs):
    base = bets(recs, "ouverture Pinnacle", None)
    bn, bm, bh = A.stats([x[1] for x in base], [x[0] for x in base])
    gn, gm, gh = A.stats([x[2] for x in base], [x[0] for x in base])
    out = [f"   Paris simulés au prix d'OUVERTURE de Pinnacle (écart minimal {EDGE_MIN:.0%}) ; CLV contre la clôture Pinnacle démarginée, marges PAR MATCH.",
           "   Le CLV d'un pari pris SANS information vaut environ −(marge de Pinnacle) : la bonne comparaison est la ligne de base « toutes les issues », pas zéro :",
           f"     {'ligne de base : toutes les issues':<34}{len(base):>5} paris  CLV {bm * 100:+.1f} % ± {bh * 100:.1f}   gain par unité {gm * 100:+.1f} % ± {gh * 100:.1f}"]
    for name in recs[0]["pred"]:
        if name == "ouverture Pinnacle":
            continue
        b = bets(recs, name)
        ids = [x[0] for x in b]
        n, m, h = A.stats([x[1] for x in b], ids)
        txt = f"     {name:<34}{len(b):>5} paris  CLV {A.fmt_stats([x[1] for x in b], ids).split('moy')[-1].strip() if n else 'n=0'}"
        if n >= MIN_MATCHES and h is not None:
            diff, marge = (m - bm) * 100, math.sqrt(h * h + bh * bh) * 100
            txt += f"   -> {diff:+.1f} points vs la ligne de base (± {marge:.1f})"
        out.append(txt)
    if any(len(bets(recs, n)) < MIN_MATCHES for n in recs[0]["pred"] if n != "ouverture Pinnacle"):
        out.append(f"   (Moins de {MIN_MATCHES} paris pour une mise en commun : avec 88 % de marché, la proba retenue ne s'écarte presque jamais assez d'un prix qui contient déjà sa marge.)")
    out.append("   Lecture : le CLV est la mesure juste (le gain d'un petit nombre de paris est du bruit). Un CLV AU-DESSUS de la ligne de base veut dire que ce prédicteur")
    out.append("   voit la clôture avant qu'elle n'arrive, contre Pinnacle, au prix d'ouverture de Pinnacle ; EN DESSOUS, que ses « écarts » sont surtout ses erreurs (sélection adverse).")
    out.append("   Ce n'est PAS un CLV 1xBet ni un gain réalisable.")
    return out


def verdict_line(recs, label, kl_model, kl_open):
    """Une phrase par marché : le modèle seul est-il plus proche de la clôture que l'ouverture ? (écart de KL apparié par match)."""
    d = [kl_model(r) - kl_open(r) for r in recs]
    n, m, h = A.stats(d)
    if n < MIN_MATCHES or h is None:
        return f"   Verdict {label} : moins de {MIN_MATCHES} matchs : aucun verdict."
    if m + h < 0:
        return f"   Verdict {label} : le modèle seul est PLUS PROCHE de la clôture que l'ouverture (écart de KL {m:+.4f}, marge exclue de 0) : il voit quelque chose."
    if m - h > 0:
        return (f"   Verdict {label} : le modèle seul est PLUS LOIN de la clôture que l'ouverture (écart de KL {m:+.4f}) : aucune preuve d'avantage contre Pinnacle "
                "sur ce marché, rien ne justifie de lui donner du poids ici, sur ces données.")
    return f"   Verdict {label} : indécis (la marge de l'écart de KL contient 0)."


def report(by_div, counts, label, final=False):
    allr = [r for rs in by_div.values() for r in rs]
    out = ["=" * 100, f"REJEU FOOTBALL-DATA ({label})" + ("  — LECTURE UNIQUE DE LA SAISON RÉSERVÉE" if final else ""), "=" * 100]
    tot = {k: sum(c.get(k, 0) for c in counts.values()) for k in ("predicted", "no_model", "no_odds", "timeout")}
    out.append(f"   {len(allr)} matchs notés ({', '.join(f'{d} {len(rs)}' for d, rs in by_div.items())}) ; sans modèle {tot['no_model']}, sans cotes Pinnacle "
               f"complètes {tot['no_odds']}" + (" ; ARRÊTÉ PAR LE TEMPS : résultat partiel" if tot["timeout"] else "") + ".")
    if not allr:
        out.append("   Aucun match noté.")
        return "\n".join(out)
    out += market_block(allr, "1N2 (log-loss sur H/D/A)", lambda p, r: ll3(p[0], r["k3"]), lambda p, r: kl3(r["q_c3"], p[0]), lambda t: t)
    out.append("")
    out += market_block(allr, "Plus/moins 2,5 buts (sur « plus de 2,5 »)", lambda p, r: ll2(p[1], r["over"]), lambda p, r: kl2(r["q_co"], p[1]), lambda t: t)
    out += ["", verdict_line(allr, "1N2", lambda r: kl3(r["q_c3"], r["pred"]["modèle seul"][0]), lambda r: kl3(r["q_c3"], r["pred"]["ouverture Pinnacle"][0])),
            verdict_line(allr, "plus/moins 2,5", lambda r: kl2(r["q_co"], r["pred"]["modèle seul"][1]), lambda r: kl2(r["q_co"], r["pred"]["ouverture Pinnacle"][1])),
            ""] + bets_block(allr)
    out += ["", "   Limites : seulement le 1N2 et le plus/moins 2,5 (aucun historique de cotes pour corners, cartons, tirs) ; pas de cote 1xBet ; Pinnacle d'ouverture",
            "   relevé des jours avant le match, pas à 08:00 ; pas de limites de mise ; réglage par défaut du modèle (pas le réglage hebdomadaire) ; les 5 ligues",
            "   football-data les plus liquides, où le marché est le plus efficient : « pas d'avantage ici » ne dit RIEN des marchés mous."]
    return "\n".join(out)


def load_matches(divs):
    from kelly.sources import footballdata as fd
    by = {d: [] for d in divs}
    for m in fd.cached_history(list(divs)):
        by.setdefault(m["div"], []).append(m)
    return by


def run(by_div_matches, test_from, test_to, seconds=None, ctx=True):
    deadline = time.time() + seconds if seconds else None
    by_div, counts = {}, {}
    for div, ms in by_div_matches.items():
        recs, cnt = replay(ms, div, test_from, test_to, deadline, ctx)
        by_div[div], counts[div] = recs, cnt
    return by_div, counts


def main(argv=None, loader=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", help="dossier du cache des sources (lecture seule) ; défaut : celui de Kelly")
    ap.add_argument("--divs", default=",".join(DEFAULT_DIVS))
    ap.add_argument("--max-seconds", type=int, default=900)
    ap.add_argument("--final", action="store_true", help="lit la saison RÉSERVÉE, une seule fois (exige --ledger)")
    ap.add_argument("--ledger", help="fichier créé par --final ; si il existe déjà, --final refuse")
    ap.add_argument("--warmup-end", default=WARMUP_END)
    ap.add_argument("--reserved-from", default=RESERVED_FROM)
    args = ap.parse_args(argv)
    if args.final:
        if not args.ledger:
            print("--final exige --ledger FICHIER (trace de l'unique lecture de la saison réservée).", file=sys.stderr)
            return 2
        if os.path.exists(args.ledger):
            print(f"La saison réservée a déjà été lue (trace : {args.ledger}). Refus : une saison réservée ne se relit pas.", file=sys.stderr)
            return 2
    _isolate_env(args.cache_dir)
    divs = [d for d in args.divs.split(",") if d]
    by_matches = (loader or load_matches)(divs)
    if args.final:
        test_from, test_to, label = args.reserved_from, RESERVED_TO, f"saison RÉSERVÉE {args.reserved_from} → fin"
    else:
        test_from, test_to, label = args.warmup_end, args.reserved_from, f"développement {args.warmup_end} → {args.reserved_from} ; échauffement avant, réservée après"
    by_div, counts = run(by_matches, test_from, test_to, args.max_seconds)
    txt = report(by_div, counts, label, args.final)
    print(txt)
    if args.final:
        with open(args.ledger, "x") as f:
            json.dump({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "divs": divs, "from": test_from,
                       "matches": sum(len(v) for v in by_div.values()), "sha": hashlib.sha256(txt.encode()).hexdigest()}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
