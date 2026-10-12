"""Réglage automatique de Kelly sur l'historique football-data : choisir au lieu de deviner.

Deux paramètres écrits à la main sont mesurés ici, ligue par ligue (python bot.py --tune, hebdomadaire) :

1. Demi-vie du modèle de force (strength.HALF_LIFE = 110 j). Pour chaque candidate de config.TUNE_HALF_LIVES, évaluation
   pas à pas (strength.walk : le modèle est réajusté sur le passé seulement) sur la dernière saison ; score = moyenne des
   log-loss du 1N2 et du plus de 2,5 buts, sur les matchs prédits par TOUTES les candidates (comparaison appariée). On garde
   110 j sauf si une candidate fait mieux d'au moins config.TUNE_MARGIN (anti-bruit). La demi-vie se CHOISIT sur le début de la fenêtre de
   test (config.TUNE_HL_SHARE, la moitié) ; le poids des buts (2.) se MESURE sur le reste, avec la demi-vie retenue : jamais les deux sur les mêmes
   matchs (sinon le poids est optimiste, la demi-vie ayant déjà été choisie parce qu'elle allait bien sur ces matchs).
2. Poids des buts face aux books (config.MODEL_WEIGHT["goals"] = 0,2). Proba mélangée = (1 - w) x books + w x modèle, avec
   la cote d'OUVERTURE du CSV (Pinnacle, sinon moyenne) sans marge (proportionnelle) comme proba des books et la proba du
   modèle walk-forward ; w de la grille 0, 0,05 ... 0,5 qui minimise le log-loss (1N2 + plus de 2,5 buts). Mesuré par ligue
   puis rétréci vers le poids global (tous matchs confondus) selon le nombre de matchs. Ce poids est celui des BUTS (plus/moins) : il ne
   s'applique ni aux 2 marquent (aucune cote historique de ce marché : poids de config, prematch.btts_weight) ni aux corners et cartons. Corners et cartons : AUCUNE cote
   historique dans les CSV, leurs poids de config sont inchangés (impossible à mesurer ici).

Résultat dans data/tuning.json : {built, half_life: {div: jours}, goal_weight: {div: w, "_global": w}, report: {div: {...}}}.
strength.hl_for / strength.goal_weight le lisent (mémoire, relu si le fichier change) avec repli sur les valeurs de config.

Budget de temps config.TUNE_SECONDS : ligues les plus importantes d'abord (E0, SP1, I1, D1, F1, puis les autres), fichier
réécrit après chaque ligue ; au passage suivant on reprend par les ligues jamais réglées puis les plus anciennes (reprise).
Les championnats en année civile (CSV « new ») gardent footballdata.NEW_KEEP_DAYS = 800 jours (400 auparavant : jamais assez pour les 540 jours de
config.TUNE_MIN_SPAN_DAYS, d'où « historique trop court » sur les 32 ligues du 09/10) ; les championnats européens lisent 3 saisons (footballdata.default_seasons).
Au 12/10 : saison en cours depuis août + 2 saisons pleines = plus de 780 jours, assez pour 540 j d'historique et 365 j de test. Le réglage utilise le modèle de force
complet (ancrage marché compris, strength.build) : demi-vie choisie sur la 1re moitié de la fenêtre de test, poids des buts mesuré sur la 2e (jamais les deux sur les mêmes matchs).
"""
import math
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as C, store, strength
from .sources import footballdata as fd, state

FILE = strength.TUNING_FILE
PRIORITY = ("E0", "SP1", "I1", "D1", "F1")
FRESH_DAYS = 5                                   # ligue réglée depuis moins de N jours : ignorée (sauf force)
clock = time.monotonic                           # remplaçable (tests)


class Timeout(Exception):
    """Budget de temps épuisé au milieu d'une ligue : son résultat partiel est abandonné."""


def grid():
    """Poids candidats du modèle de buts : 0, 0,05 ... 0,5."""
    n = round(C.TUNE_WEIGHT_MAX / C.TUNE_WEIGHT_STEP)
    return [round(i * C.TUNE_WEIGHT_STEP, 4) for i in range(n + 1)]


def divisions():
    """Ligues à régler, les plus importantes d'abord."""
    rest = sorted(d for d in dict.fromkeys(fd.SPORT_TO_DIV.values()) if d not in PRIORITY)
    return list(PRIORITY) + rest


def book_probs(m):
    """Probas des books à l'OUVERTURE, sans marge (proportionnelle) : ([H, D, A] ou None, P(plus de 2,5) ou None).
    Pinnacle d'abord, sinon la moyenne des books."""
    o = m.get("odds") or {}
    p3 = po = None
    for book in ("ps", "avg"):
        x = ((o.get("1x2") or {}).get("open") or {}).get(book)
        if x and p3 is None:
            p3 = fd.no_vig(x)
        y = ((o.get("ou25") or {}).get("open") or {}).get(book)
        if y and po is None:
            v = fd.no_vig(y)
            po = v[0] if v else None
    return p3, po


def _ll(p, k):
    return -math.log(max(p[k], 1e-9))


def _llo(po, over):
    return -math.log(max(po if over else 1 - po, 1e-9))


def evaluate(matches, test_from, hl, deadline=None, step_days=None, div=None):
    """Évaluation pas à pas pour une demi-vie : liste (une entrée par match de test, dans l'ordre) de
    (k1x2, plus de 2,5 (0/1), p1x2 modèle, P(plus de 2,5) modèle, p1x2 books | None, P(plus de 2,5) books | None) ou None
    quand le modèle ne prédit pas le match. `deadline` : instant (clock) à ne pas dépasser, sinon Timeout."""
    out = []
    for m, p3, po in strength.walk(matches, test_from, None, hl, step_days or C.TUNE_STEP_DAYS, True, ("goals",), div):
        if deadline is not None and clock() > deadline:
            raise Timeout()
        if p3 is None:
            out.append(None)
            continue
        b3, bo = book_probs(m)
        out.append(("HDA".index(m["ftr"]), 1 if m["hg"] + m["ag"] > 2.5 else 0, p3, po, b3, bo))
    return out


def score_table(recs_by_hl):
    """{hl: {n, ll_1x2, ll_o25, ll}} sur les matchs prédits par toutes les demi-vies (ll = moyenne des deux log-loss)."""
    size = min(len(r) for r in recs_by_hl.values())
    keep = [i for i in range(size) if all(r[i] is not None for r in recs_by_hl.values())]
    table = {}
    for hl, recs in recs_by_hl.items():
        if not keep:
            table[hl] = {"n": 0, "ll_1x2": 0.0, "ll_o25": 0.0, "ll": 0.0}
            continue
        a = sum(_ll(recs[i][2], recs[i][0]) for i in keep) / len(keep)
        b = sum(_llo(recs[i][3], recs[i][1]) for i in keep) / len(keep)
        table[hl] = {"n": len(keep), "ll_1x2": a, "ll_o25": b, "ll": (a + b) / 2}
    return table


def pick_half_life(table, base=strength.HALF_LIFE, margin=None):
    """(demi-vie retenue, gain de log-loss sur `base`, meilleure candidate) : `base` sauf si une candidate fait mieux d'au
    moins `margin` (config.TUNE_MARGIN)."""
    margin = C.TUNE_MARGIN if margin is None else margin
    best = min(table, key=lambda h: (table[h]["ll"], abs(h - base)))
    gain = table[base]["ll"] - table[best]["ll"]
    return (best if best != base and gain >= margin else base), (gain if best != base and gain >= margin else 0.0), best


def weight_curves(recs):
    """Somme des log-loss du mélange (1 - w) x books + w x modèle pour chaque poids de la grille, sur les matchs qui ont des
    cotes d'ouverture. -> {n1, no, ll1: [...], llo: [...]} (sommes, pour pouvoir les additionner entre ligues)."""
    ws = grid()
    ll1, llo, n1, no = [0.0] * len(ws), [0.0] * len(ws), 0, 0
    for r in recs:
        if r is None:
            continue
        k, over, p3, po, b3, bo = r
        if b3:
            n1 += 1
            for i, w in enumerate(ws):
                ll1[i] += -math.log(max((1 - w) * b3[k] + w * p3[k], 1e-9))
        if bo is not None:
            no += 1
            for i, w in enumerate(ws):
                q = (1 - w) * bo + w * po
                llo[i] += -math.log(max(q if over else 1 - q, 1e-9))
    return {"n1": n1, "no": no, "ll1": [round(x, 4) for x in ll1], "llo": [round(x, 4) for x in llo]}


def best_weight(curves, on=None):
    """(poids, log-loss moyen du mélange, log-loss des books seuls) minimisant la moyenne des log-loss du 1N2 et du plus de
    2,5 buts (config.TUNE_WEIGHT_ON : both | ou25 | 1x2) pour une ou plusieurs courbes (additionnées) ; None si aucune cote."""
    on = on or C.TUNE_WEIGHT_ON
    ws = grid()
    curves = [c for c in curves if c.get("n1") or c.get("no")]
    n1 = sum(c["n1"] for c in curves) if on != "ou25" else 0
    no = sum(c["no"] for c in curves) if on != "1x2" else 0
    if not n1 and not no:
        return None
    tot = []
    for i in range(len(ws)):
        a = sum(c["ll1"][i] for c in curves) / n1 if n1 else None
        b = sum(c["llo"][i] for c in curves) / no if no else None
        tot.append(sum(x for x in (a, b) if x is not None) / sum(1 for x in (a, b) if x is not None))
    i = min(range(len(ws)), key=lambda j: (tot[j], ws[j]))          # égalité : le plus petit poids
    return ws[i], tot[i], tot[0]


def _n(c):
    """Matchs avec cotes d'ouverture d'une courbe, pour le marché qui fixe le poids."""
    on = C.TUNE_WEIGHT_ON
    return c["n1"] if on == "1x2" else c["no"] if on == "ou25" else min(c["n1"], c["no"])


def solve_weights(report):
    """(poids global, {div: poids rétréci}) d'après les courbes du rapport. Poids d'une ligue = (n x brut + K x global) /
    (n + K), n = matchs avec cotes (min 1N2 / plus de 2,5), K = config.TUNE_SHRINK_N ; ligue sous TUNE_MIN_TEST matchs : non mesurée."""
    cur = {d: r["gw"] for d, r in report.items() if isinstance(r, dict) and isinstance(r.get("gw"), dict)}
    g = best_weight(list(cur.values()))
    if not g:
        return None, {}
    out = {}
    for d, c in cur.items():
        n = _n(c)
        b = best_weight([c])
        if not b or n < C.TUNE_MIN_TEST:
            continue
        w = (n * b[0] + C.TUNE_SHRINK_N * g[0]) / (n + C.TUNE_SHRINK_N)
        out[d] = round(max(0.0, min(C.TUNE_WEIGHT_MAX, w)), 3)
    return g[0], out


def tune_div(div, matches, deadline=None):
    """Règle une ligue : -> entrée de rapport (voir run). {"skipped": raison} si l'historique ne suffit pas. Lève Timeout."""
    t0 = clock()
    ms = sorted((m for m in matches if m.get("hg") is not None), key=lambda m: m["date"])
    if not ms:
        return {"skipped": "aucun historique en cache"}
    first, last = datetime.fromisoformat(ms[0]["date"]).date(), datetime.fromisoformat(ms[-1]["date"]).date()
    if (last - first).days < C.TUNE_MIN_SPAN_DAYS:
        return {"skipped": f"historique trop court ({(last - first).days} j < {C.TUNE_MIN_SPAN_DAYS})"}
    tf = last - timedelta(days=C.TUNE_TEST_DAYS)
    tf = max(tf, first + timedelta(days=150))                      # au moins 150 jours d'apprentissage avant le test
    cands = sorted(set(C.TUNE_HALF_LIVES) | {strength.HALF_LIFE}, key=lambda h: (h != strength.HALF_LIFE, h))
    try:
        recs = {h: evaluate(ms, tf.isoformat(), h, deadline, div=div) for h in cands}
    finally:
        strength._names.clear()                                   # noms d'équipes résolus : propres à cette ligue
    size = min(len(r) for r in recs.values())
    cut = max(1, min(size - 1, round(size * C.TUNE_HL_SHARE)))     # début du test : choix de la demi-vie ; fin : mesure du poids des buts
    table = score_table({h: r[:cut] for h, r in recs.items()})
    n = score_table(recs)[strength.HALF_LIFE]["n"]                # matchs prédits par toutes les demi-vies, test entier
    if n < C.TUNE_MIN_TEST:
        return {"skipped": f"trop peu de matchs de test prédits ({n} < {C.TUNE_MIN_TEST})"}
    chosen, gain, best = pick_half_life(table)
    return {"n": n, "n_hl": table[strength.HALF_LIFE]["n"], "test_from": tf.isoformat(), "chosen": chosen, "gain": round(gain, 5), "best": best,
            "gain_best": round(table[strength.HALF_LIFE]["ll"] - table[best]["ll"], 5),
            "hl": {str(h): {k: round(v, 5) if k != "n" else v for k, v in t.items() if k != "n"} for h, t in table.items()},
            "gw": weight_curves(recs[chosen][cut:]), "secs": round(clock() - t0, 1)}


def _stamp():
    return state.now().isoformat(timespec="seconds")


def _read():
    old = store.load(FILE, {})
    old = old if isinstance(old, dict) else {}
    rep = old.get("report")
    return {"half_life": dict(old.get("half_life") or {}), "goal_weight": dict(old.get("goal_weight") or {}),
            "report": dict(rep) if isinstance(rep, dict) else {}}


def _age_days(iso):
    try:
        return (state.now() - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def order(divs, report, force=False):
    """Ligues à régler dans l'ordre : jamais réglées d'abord (priorité d'importance), puis les plus anciennes ; les ligues
    réglées depuis moins de FRESH_DAYS jours sont omises (sauf force)."""
    pri = {d: i for i, d in enumerate(divs)}
    todo = [d for d in divs if force or _age_days((report.get(d) or {}).get("tuned")) >= FRESH_DAYS]
    return sorted(todo, key=lambda d: ((report.get(d) or {}).get("tuned") or "", pri[d]))


def run(divs=None, loader=None, seconds=None, force=False, log=None):
    """Règle les ligues dans la limite de `seconds` (config.TUNE_SECONDS), écrit data/tuning.json après chaque ligue.
    -> {"done": [div], "skipped": {div: raison}, "pending": [div], "seconds": s, "result": contenu du fichier}.
    `loader(div)` -> matchs (par défaut les CSV en cache, strength.load_matches) ; `log(texte)` : suivi en direct."""
    t0 = clock()
    budget = C.TUNE_SECONDS if seconds is None else seconds
    deadline = t0 + budget
    cur = _read()
    divs = list(divs or divisions())
    todo = order(divs, cur["report"], force)
    done, skipped, left = [], {}, list(todo)
    for div in todo:
        if clock() >= deadline:
            break
        try:
            r = tune_div(div, (loader or strength.load_matches)(div), deadline)
        except Timeout:
            break
        except Exception as e:                                   # noqa: BLE001 - une ligue illisible ne bloque pas les autres
            skipped[div] = f"erreur {type(e).__name__}: {e}"
            left.remove(div)
            print(f"[tune] {div} ignoré : {skipped[div]}", file=sys.stderr)
            continue
        left.remove(div)
        r["tuned"] = _stamp()
        prev = cur["report"].get(div)
        if "skipped" in r and isinstance(prev, dict) and "skipped" not in prev and div in cur["half_life"]:
            # historique momentanément insuffisant (CSV purgés, changement de saison, trop peu de matchs prédits) : le DERNIER réglage mesuré est
            # conservé tel quel (demi-vie, courbes du poids des buts, date de mesure : la ligue sera retentée au prochain passage) au lieu d'être effacé
            cur["report"][div] = prev | {"last_skip": r["skipped"], "last_skip_at": r["tuned"]}
            skipped[div] = r["skipped"] + " (dernier réglage conservé)"
        else:
            cur["report"][div] = r
            if "skipped" in r:
                skipped[div] = r["skipped"]
                cur["half_life"].pop(div, None)
            else:
                cur["half_life"][div] = r["chosen"]
                done.append(div)
        _write(cur)
        if log:
            log(f"{div} : " + (r.get("skipped") or f"demi-vie {r['chosen']} j"))
    res = _write(cur)
    return {"done": done, "skipped": skipped, "pending": left, "seconds": round(clock() - t0, 1), "result": res}


def _write(cur):
    """Recalcule les poids (global + rétrécis) d'après tous les rapports et écrit data/tuning.json (atomique)."""
    g, per = solve_weights(cur["report"])
    cur["goal_weight"] = ({"_global": round(g, 3), **per} if g is not None else {})
    out = {"built": _stamp(), "half_life": cur["half_life"], "goal_weight": cur["goal_weight"], "report": cur["report"]}
    try:
        store.save(FILE, out)
    except OSError as e:
        print(f"[tune] {FILE} non écrit : {e}", file=sys.stderr)
    strength._tuned.clear()                                      # relu au prochain accès (la date du fichier peut ne pas avoir bougé)
    return out


def _f(x, n=4):
    return f"{x:.{n}f}".replace(".", ",")


def format_report(res):
    """Rapport court en français d'un passage de run()."""
    out = res["result"]
    rep = out.get("report") or {}
    L = [f"Réglage Kelly en {res['seconds']:.0f} s : {len(res['done'])} ligue(s) réglée(s)"
         + (f", {len(res['pending'])} restante(s) (budget épuisé, reprise au prochain passage)" if res["pending"] else "") + "."]
    for d in sorted(out.get("half_life") or {}, key=lambda d: (d not in PRIORITY, PRIORITY.index(d) if d in PRIORITY else 0, d)):
        r = rep.get(d) or {}
        h = out["half_life"][d]
        base = (r.get("hl") or {}).get(str(strength.HALF_LIFE), {}).get("ll")
        cur = (r.get("hl") or {}).get(str(h), {}).get("ll")
        if h == strength.HALF_LIFE:
            gap = f", {r['best']} j meilleure mais gain {_f(r.get('gain_best', 0))} < marge" if r.get("best") not in (None, h) else ""
            line = f"  {d} : demi-vie {h} j gardée{gap}"
        else:
            line = f"  {d} : demi-vie {h} j au lieu de {strength.HALF_LIFE}" + \
                (f", log-loss -{_f(base - cur)}" if base is not None and cur is not None else "")
        w = (out.get("goal_weight") or {}).get(d)
        c = r.get("gw") or {}
        b = best_weight([c]) if c else None
        if w is not None:
            w1, wo = best_weight([c], "1x2"), best_weight([c], "ou25")
            line += (f" ; poids des buts {_f(w, 2)} (brut {_f(b[0], 2)} sur {_n(c)} matchs ; 1N2 seul {_f(w1[0], 2)}, "
                     f"plus de 2,5 seul {_f(wo[0], 2)})" if b and w1 and wo else f" ; poids des buts {_f(w, 2)}")
        L.append(line)
    if (out.get("goal_weight") or {}).get("_global") is not None:
        L.append(f"Poids global des buts face aux books : {_f(out['goal_weight']['_global'], 3)} (config : {_f(C.MODEL_WEIGHT['goals'], 2)}). "
                 "Corners et cartons : poids de config inchangés (pas de cotes historiques).")
    for d, why in sorted(res["skipped"].items()):
        L.append(f"  {d} ignorée : {why}")
    if res["pending"]:
        L.append("  À régler au prochain passage : " + ", ".join(res["pending"]))
    return "\n".join(L)


def _replay():
    """Rejeu de la saison passée (kelly/replay.py) poursuivi C.REPLAY_TUNE_SECONDS s s'il n'est pas fini. Ne lève jamais."""
    try:
        from . import replay
        if C.REPLAY_TUNE_SECONDS > 0 and not replay.done():
            replay.run(seconds=C.REPLAY_TUNE_SECONDS, log=lambda s: print("[replay] " + s, flush=True))
    except Exception as e:                                       # noqa: BLE001 - facultatif
        print(f"[replay] ignoré : {type(e).__name__}: {e}", file=sys.stderr)


def main(argv=()):
    """Mode `python bot.py --tune [--force]` : règle, affiche le rapport. Ne lève jamais ; code de sortie 0 (même partiel), 1 si échec."""
    try:
        res = run(force="--force" in argv, log=lambda s: print("[tune] " + s, flush=True))
        print(format_report(res))
        _replay()
        return 0
    except Exception as e:                                       # noqa: BLE001
        print(f"[tune] échec : {type(e).__name__}: {e}", file=sys.stderr)
        return 1
