"""Démonstration hors ligne de l'ensemble (kelly/ensemble.py) et des écarts sharp (kelly/sharpgap.py) sur une COPIE des données de Kelly.

    python tools/ensemble_demo.py --data-dir /chemin/vers/un/dossier/avec/kelly.db [--now 2026-10-09T18:03:00+00:00] [--examples 8]

Ne touche jamais au dossier donné : seul `kelly.db` en est copié dans un dossier temporaire (aucun réseau, aucune écriture dans data/). Lit
today.json (pronostics du jour et paris réels), combi_sent.json (combinés sur mesure du jour), odds_history.json (âge des relevés sharp),
ensemble.json (poids appris, s'il existe). Affiche :
  1. les 💎 d'aujourd'hui avec la proba du modèle (liste actuelle) et avec l'ensemble (liste de l'ensemble), par famille, avec exemples
     « 1xBet : X % · Sharp : Y % · Kelly : Z % · retenu : W % » ;
  2. ce que deviendraient les combinés sur mesure du jour (m1, m2, m8...) : probabilité et espérance avec la proba retenue ;
  3. les écarts sharp (sharpgap.scan) des paris réels du jour.
Limites de la copie : today.json ne garde que la cote 1xBet de la ligne choisie (pas toutes les cotes du match) -> l'ensemble n'y est évalué que sur
cette ligne (en production xprice.choose évalue toutes les lignes c["alts"] avec les cotes complètes) ; la proba 1xBet sans marge d'une double chance
n'y est pas calculable (les trois cotes manquent) : approchée par 1 / cote / 1,05 (marquée « ≈ »).
"""
import argparse
import copy
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR"), help="dossier contenant kelly.db (copié, jamais modifié)")
ap.add_argument("--now", help="instant de référence ISO (défaut : heure d'écriture de today.json)")
ap.add_argument("--examples", type=int, default=8)
args = ap.parse_args()
if not args.data_dir or not os.path.isfile(os.path.join(args.data_dir, "kelly.db")):
    sys.exit("--data-dir : dossier avec kelly.db requis (utilise une copie du snapshot)")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.realpath(args.data_dir) == os.path.realpath(os.path.join(ROOT, "data")):
    sys.exit("refus : c'est le dossier data/ du bot ; donne une copie")
tmp = tempfile.mkdtemp(prefix="kelly-ens-demo-")
shutil.copy2(os.path.join(args.data_dir, "kelly.db"), os.path.join(tmp, "kelly.db"))
os.environ["DATA_DIR"] = tmp
for k in ("SOURCES_CACHE_DIR", "ODDS_CACHE_DIR"):
    os.environ[k] = tempfile.mkdtemp(prefix="kelly-ens-demo-cache-")
os.environ["XBET_PROXY_FILE"] = os.path.join(tmp, "pas-de-fichier.env")
os.environ.pop("TELEGRAM_TOKEN", None)
sys.path.insert(0, ROOT)

from kelly import calib, combos as CB, config as C, ensemble as E, sharpgap, store  # noqa: E402


def pc(p):
    return "  — " if p is None else f"{p * 100:4.0f} %"


def label(c):
    return f"{c.get('event', '?')} | {c.get('pick', c['name'])}"


def opinions_line(parts, approx=False):
    bits = []
    if parts["p_x"] is not None:
        bits.append(f"1xBet : {parts['p_x'] * 100:.0f} %" + (" (≈)" if approx else ""))
    if parts["p_sharp"] is not None:
        bits.append(f"Sharp : {parts['p_sharp'] * 100:.0f} %")
    if parts["p_model"] is not None:
        bits.append(f"Kelly : {parts['p_model'] * 100:.0f} %")
    p = E.pool(parts["p_model"], parts["p_mkt"], parts["wm"])
    return " · ".join(bits) + (f" · retenu : {p * 100:.0f} %" if p is not None else "")


def ensemble_view(c, now):
    """Le pronostic `c` (état actuel de la copie : prob = proba du modèle) vu par l'ensemble, à sa cote 1xBet c["xodds"] : décision et copie
    « comme si l'ensemble était actif » (prob = p_ens, p_model = ancienne prob), ou None sans cote 1xBet."""
    price = c.get("xodds")
    if not price:
        return None
    pm = c["prob"]
    ps = E.sharp_for(c, c["name"], c["point"], now)
    px = c.get("xp")
    approx = px is None and E.family(c["market"]) == "dc"
    px = E.x_approx(price) if approx else px
    v = E.value_of(c["market"], pm, px, ps, price, x_approx_used=approx)
    c2 = dict(c)
    if v["p"] is not None:
        c2.update(prob=v["p"], p_model=pm, p_x=px, p_sharp=ps, min_odds=E.min_price(v["p"]), xev=v["ev"])
    return v, c2, approx


def main():
    now = (datetime.fromisoformat(args.now) if args.now else None)
    ver = store.version("today.json")
    if now is None:
        now = datetime.strptime(ver[1], "%Y-%m-%dT%H:%M:%S%z") if ver else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = store.load("today.json", {})
    pronos = copy.deepcopy(today.get("pronos") or [])
    picks = copy.deepcopy(today.get("picks") or [])
    history = store.load("odds_history.json", {})
    if not pronos:
        sys.exit("today.json sans pronostics")
    print(f"=== Démo ensemble — jour {today.get('day')} — référence {now:%Y-%m-%d %H:%M} UTC (today.json écrit à cette heure) ===")
    print(f"{len(pronos)} pronostics, {sum(1 for c in pronos if c.get('xodds'))} avec cote 1xBet, {sum(1 for c in pronos if c.get('xp'))} avec proba 1xBet sans marge, "
          f"{len(picks)} paris réels (The Odds API), relevé 1xBet de {today.get('xbet_at')}.")
    learned = E.lines()
    print("Poids appris :", "; ".join(learned) if learned else "aucun résultat appris (poids = a priori : buts/1N2/2 marquent/double chance modèle 12 %, "
          "corners/cartons/tirs/fautes/touches/hors-jeu modèle 50 %, sharp 75 % face à 1xBet)")
    n_s, n_m = E.attach(pronos, picks, history)
    print(f"Rapprochement pronostics / paris réels : {n_s} pronostic(s) avec un avis sharp (Pinnacle ou ≥ {C.SHARPGAP_MIN_SHARP} books de référence), "
          f"{n_m} pari(s) réel(s) avec un avis du modèle. (Les matchs payés par The Odds API n'ont de pronostic que sur corners et cartons, où Pinnacle "
          "ne cote presque jamais : l'avis sharp sert surtout aux paris réels, voir 3.)")

    # ---------- 1. les 💎 ----------
    cur = CB.best_per_match(pronos)
    views = {}
    for c in pronos:
        w = ensemble_view(c, now)
        if w:
            views[c["key"]] = w
    ens_pool = []
    why = Counter()
    value_only = Counter()
    for c in pronos:
        w = views.get(c["key"])
        if not w:
            continue
        v, c2, approx = w
        b = c.get("base")
        if not v["ok"]:
            why[{"price": "1xBet paie trop peu", "veto": "veto (écart modèle-marché)", "no_market": "pas d'avis du marché"}.get(v["why"], v["why"])] += 1
            continue
        value_only[E.family(c["market"])] += 1
        if v["p"] < C.VIABLE_MIN_P:
            why[f"valeur, mais proba retenue < {C.VIABLE_MIN_P * 100:.0f} % (seuil de viabilité)"] += 1
            continue
        if b is not None and v["parts"]["p_model"] < b:
            why["modèle sous un match moyen"] += 1
            continue
        c3 = {k: x for k, x in c2.items() if k != "base"}                      # la référence se compare à la proba du MODÈLE (fait ci-dessus)
        if not CB.viable(c3):
            why["autre critère de viabilité (données, qualité, calibration)"] += 1
            continue
        ens_pool.append(c2)
    ens = CB.best_per_match(ens_pool)
    print("\n--- 1. 💎 du jour : proba du modèle (actuel) contre proba retenue (ensemble) ---")
    print(f"💎 actuels (un par match, viables à leur cote 1xBet, proba du modèle) : {len(cur)}")
    print(f"💎 avec l'ensemble (cote x proba retenue >= {1 + C.ENSEMBLE_MARGIN:.2f}, veto > {C.ENSEMBLE_MAX_DISAGREE:.2f}, un par match) : {len(ens)}")
    print("Pronostics cotés chez 1xBet écartés par l'ensemble, par raison :", dict(why) or "aucun")
    cur_keys = {c["key"] for c in cur}
    ens_keys = {c["key"] for c in ens}
    print(f"Communs aux deux listes : {len(cur_keys & ens_keys)} ; seulement dans l'actuelle : {len(cur_keys - ens_keys)} ; seulement avec l'ensemble : {len(ens_keys - cur_keys)}")
    fam_cur, fam_ens = Counter(E.family(c["market"]) for c in cur), Counter(E.family(c["market"]) for c in ens)
    priced = Counter(E.family(c["market"]) for c in pronos if c.get("xodds"))
    print("\n  famille            cotés 1xBet   💎 actuels   valeur (ensemble, avant seuil de proba)   💎 ensemble")
    for fam in sorted(set(fam_cur) | set(fam_ens) | set(priced), key=lambda f: -priced[f]):
        print(f"  {calib.LABELS.get(fam, fam):18s} {priced[fam]:11d} {fam_cur[fam]:12d} {value_only[fam]:20d} {fam_ens[fam]:30d}")
    ev_cur = [c["prob"] * c["xodds"] - 1 for c in cur]
    print(f"\n  EV annoncée moyenne des 💎 actuels (proba du modèle) : {sum(ev_cur) / len(ev_cur) * 100:+.1f} %" if cur else "")
    if cur:
        ev_if = []
        for c in cur:
            v, _, _ = views[c["key"]]
            ev_if.append(v["ev"])
        print(f"  EV des MÊMES paris avec la proba retenue : {sum(ev_if) / len(ev_if) * 100:+.1f} %  (la différence est l'optimisme du modèle sur ces paris)")
    print(f"\n  Exemples — 💎 actuels, vus par l'ensemble (jusqu'à {args.examples}) :")
    for c in cur[:args.examples]:
        v, c2, approx = views[c["key"]]
        verdict = "VALEUR" if v["ok"] else {"price": "écarté : 1xBet paie trop peu", "veto": "écarté : veto (écart modèle-marché trop grand)",
                                            "no_market": "écarté : pas d'avis du marché", "no_prob": "écarté"}.get(v["why"], v["why"])
        print(f"   • {label(c)}  @ {c['xodds']:.2f}")
        print(f"       {opinions_line(v['parts'], approx)}  →  EV {v['ev'] * 100:+.1f} % (avant : {(c['prob'] * c['xodds'] - 1) * 100:+.1f} %)  [{verdict}]")
    if ens:
        print(f"\n  Exemples — 💎 de l'ensemble (jusqu'à {args.examples}) :")
        for c in ens[:args.examples]:
            v, _, approx = views[c["key"]]
            print(f"   • {label(c)}  @ {c['xodds']:.2f}\n       {opinions_line(v['parts'], approx)}  →  EV {v['ev'] * 100:+.1f} %")
    else:
        print("\n  Aucun 💎 avec l'ensemble.")
    gaps = sorted(((views[c["key"]][0]["gap"], c) for c in pronos if c["key"] in views and views[c["key"]][0]["gap"] is not None), key=lambda x: -x[0])
    print("\n  Plus grands écarts modèle-marché parmi les pronostics cotés :")
    for g, c in gaps[:4]:
        v, _, approx = views[c["key"]]
        print(f"   • {label(c)} : écart {g * 100:.0f} points — {opinions_line(v['parts'], approx)}")

    # ---------- 2. combinés sur mesure ----------
    sent = store.load("combi_sent.json", {})
    by_key = {c["key"]: c for c in pronos}
    print("\n--- 2. combinés sur mesure du jour (combi_sent.json) : proba du modèle contre proba retenue ---")
    for cid in ("m1", "m2", "m8"):
        item = (sent.get("items") or {}).get(cid)
        if not item:
            print(f"{cid} : absent de la copie")
            continue
        p_old, p_new, total, miss, rows = 1.0, 1.0, 1.0, 0, []
        for leg in item["legs"]:
            odd = leg.get("xodds") or leg.get("odds")
            total *= odd
            c = by_key.get(leg["key"])
            w = ensemble_view(c | {"xodds": odd}, now) if c else None
            if not w:
                p_old *= leg["prob"]
                p_new *= leg["prob"]
                miss += 1
                rows.append((leg, odd, None, None))
                continue
            v, _, approx = w
            p_old *= leg["prob"]
            p_new *= v["p"]
            rows.append((leg, odd, v, approx))
        print(f"{cid}  cote cible {item['target']:g}, {len(item['legs'])} jambes, cote totale {total:.1f}, coupon {item.get('coupon')}")
        print(f"     proba du combiné : modèle {p_old * 100:.3f} % → ensemble {p_new * 100:.3f} %   "
              f"EV : modèle {(p_old * total - 1) * 100:+.0f} % → ensemble {(p_new * total - 1) * 100:+.0f} %"
              + (f"   ({miss} jambe(s) sans match dans pronos : proba du modèle gardée)" if miss else ""))
        for leg, odd, v, approx in rows[:3 if cid != "m8" else 8]:
            if v:
                print(f"       - {leg['pick']} @ {odd:.2f} : {opinions_line(v['parts'], approx)}")
        if len(rows) > 3 and cid != "m8":
            print(f"       … {len(rows) - 3} autres jambes")
    # ---------- 3. écarts sharp ----------
    print("\n--- 3. écarts sharp des paris réels du jour (sharpgap.scan) ---")
    today2 = today | {"pronos": pronos, "picks": picks}
    res = sharpgap.scan(today2, now, history)
    print(f"{res['n_picks']} paris réels examinés, {len(res['cands'])} candidat(s) (écart >= {C.SHARPGAP_EDGE * 100:.0f} %, relevé sharp <= {C.SHARPGAP_MAX_AGE_MIN} min, "
          f"Pinnacle ou >= {C.SHARPGAP_MIN_SHARP} books de référence, match dans > {C.SHARPGAP_SOON_MIN} min). Refus : {dict(res['rejected']) or 'aucun'}")
    for x in res["cands"]:
        d = sharpgap.describe(x)
        print(f"   ✔ {x['event']} | {d['title']} — {d['line']} ({d['age']}) coupon: xref={x['xref']}")
    for x in res["refused"][:5]:
        d = sharpgap.describe(x)
        print(f"   ✖ {x['event']} | {d['title']} — {d['line']} ({d['age']}) refusé : {x['why']}")
    # scénario illustratif RÉEL côté sharp : « et si 1xBet payait la meilleure cote vue chez les autres books ? »
    illus = [dict(c, xodds=c["best_odds"], xref=None, xgid=None) for c in picks if E.sharp_of(c) and c.get("best_odds") and not c.get("xodds")]
    if illus:
        ill = sharpgap.scan(today | {"picks": illus, "pronos": []}, now, history, min_edge=-1.0, max_age=10 ** 6)
        top = sorted(ill["cands"] + ill["refused"], key=lambda x: -x["edge"])[:5]
        print("   Illustration (les paris réels n'ont pas de cote 1xBet dans la copie) : si 1xBet payait la MEILLEURE cote vue chez les autres books :")
        for x in top:
            print(f"      {x['event']} | {x['pick']} : meilleure cote {x['xodds']:.2f} ({x['book']}) contre juste {x['fair_odds']:.2f} → écart {x['edge'] * 100:+.1f} % "
                  f"(sharp {x['src']}, relevé de {x['age_min']} min)")
    # format d'affichage : exemple SYNTHÉTIQUE (cote 1xBet inventée = cote juste sharp + 5 %), pour montrer ce que le message contiendrait
    syn = [dict(c, xodds=round(1 / E.sharp_of(c)[0] * 1.05, 2), xref=[0, 0, 0], xgid=0) for c in picks if E.sharp_of(c)][:3]
    if syn:
        sres = sharpgap.scan(today | {"picks": syn, "pronos": []}, now, history, max_age=10 ** 6)
        print("   Exemple de FORMAT (SYNTHÉTIQUE : cote 1xBet inventée = cote juste sharp + 5 %, âge du relevé ignoré) :")
        for x in sres["cands"]:
            d = sharpgap.describe(x)
            print(f"      ✔ {x['event']} | {d['title']} — {d['line']} ({d['age']})")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
