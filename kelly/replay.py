"""Rejeu de la saison passée : Kelly refait, match par match, ses pronostics de stats sur les CSV football-data déjà en cache (aucune requête), pour que la
correction de fiabilité (kelly/calib.py) ne parte pas de zéro sur les corners, cartons, tirs, tirs cadrés, fautes, corners d'une équipe et jaunes.

Pour chaque championnat aux CSV complets (fd.STATS_SPORTS, 18 championnats), les matchs des C.REPLAY_DAYS jours avant le premier rejeu sont pris dans
l'ordre. Le modèle approfondi (strength.build) est ajusté tous les C.REPLAY_STEP_DAYS jours sur les SEULS matchs d'avant (jamais l'avenir), puis chaque
match est prédit comme en direct : strength.predict, mêmes chiffres d'avant-match (features.fill_deep), mêmes options (fair._options : modèle approfondi,
sinon méthode simple), même choix de ligne par marché (fair.best_per_family). Chaque pronostic est réglé avec les stats réelles du CSV
(settle.fd_stats, settle.settle_pick) et compté comme un vrai (calib.accumulate : famille, championnat, sens, tranche).

data/calib_boot.json : {v, from, to, built, divs: {div: {matches, picks, acc}}, acc (somme des championnats)}. Repris là où il s'est arrêté
(championnat par championnat) ; --force le refait. calib.factor part de la correction qu'il mesure (au lieu de « pas de correction ») ; les vrais
résultats s'en écartent dès qu'ils sont assez nombreux. Ils ne sont jamais mélangés.

Moyennes et dispersion (kelly/means.py) : pour chaque match et chaque famille de stats, le nombre que le modèle attendait (brut) contre le réel du CSV,
et de quoi mesurer la dispersion (le modèle croit-il les matchs plus réguliers qu'ils ne le sont ?) : divs[div]["means"] = {famille: [n, Σ réel, Σ attendu,
Σ réel², Σ réel x attendu, Σ attendu², Σ dispersion du modèle x attendu²]}, et doc["means"] la somme. Point de départ de la correction des moyennes
(par famille et par championnat) et de la dispersion. Un championnat rejoué avant (sans « means ») est refait au prochain rejeu ; en attendant, son
ancien résultat reste compté.

Limites : pas d'xG, pas de cotes 1xBet (la ligne est celle que Kelly choisit seule), pas les buts (déjà mesurés sur les vrais résultats). Le rejeu est un
point de départ ; les vrais résultats prennent le dessus à mesure qu'ils arrivent.
"""
import sys
import time
from datetime import date, timedelta

from . import calib, config as C, fair, fmt, settle, store, strength
from .sources import features, footballdata as fd, http, state

FILE = calib.BOOT_FILE
VERSION = 1
MIN_MATCHES = 100          # en dessous, les CSV du championnat manquaient : il sera refait
# marchés réglables avec les seuls CSV football-data
MARKETS = frozenset(("alternate_totals_corners", "alternate_totals_cards", "team_corners_home", "team_corners_away", "team_yellow_total",
                     "team_shots_total", "team_shots_home", "team_shots_away", "team_sot_total", "team_sot_home", "team_sot_away",
                     "team_fouls_total", "team_fouls_home", "team_fouls_away"))


def divs():
    """Divisions football-data rejouées (CSV avec corners, cartons, tirs, fautes), dans un ordre fixe."""
    return sorted({fd.SPORT_TO_DIV[k] for k in fd.STATS_SPORTS})


def load():
    d = store.load(FILE, {})
    return d if isinstance(d, dict) and d.get("v") == VERSION else {}


ACTUAL = {"corners": ("hc", "ac"), "yellow": ("hy", "ay"), "shots": ("hs", "as"), "sot": ("hst", "ast"), "fouls": ("hf", "af")}


def actual(m, fam):
    """Nombre réel d'une famille de stats dans le match `m` (CSV football-data) : total des deux équipes ; cartons = jaunes + rouges ; None s'il manque."""
    if fam == "cards":
        vals = [m.get(k) for k in ("hy", "ay", "hr", "ar")]
        return float(sum(vals)) if all(v is not None for v in vals) else None
    cols = ACTUAL.get(fam)
    if not cols or m.get(cols[0]) is None or m.get(cols[1]) is None:
        return None
    return float(m[cols[0]] + m[cols[1]])


def mean_stats(pred, m, acc):
    """Ajoute à `acc` ({famille: [n, Σ réel, Σ attendu, Σ réel², Σ réel x attendu, Σ attendu², Σ dispersion x attendu²]}) les familles de stats du match :
    attendu = moyenne BRUTE du modèle (h + a), dispersion = celle du total (« at ») ; réel = le CSV."""
    from . import means
    for fam, st in (pred.get("stats") or {}).items():
        if fam not in means.FAMILIES:
            continue
        val, mu = actual(m, fam), (st.get("h") or 0) + (st.get("a") or 0)
        if val is None or mu <= 0:
            continue
        a = acc.setdefault(fam, [0.0] * 7)
        for i, x in enumerate((1, val, mu, val * val, val * mu, mu * mu, float(st.get("at") or 0) * mu * mu)):
            a[i] = round(a[i] + x, 4)


def picks(model, m, sport_key, on, means_acc=None, pred=None, fix=None):
    """Pronostics de stats du match `m` (CSV football-data) avec le modèle `model` (ajusté avant `on`), réglés : [(pronostic, gagné)]. Push et non
    réglables ignorés. `pred` : prévision déjà faite ; `fix` : (facteurs des moyennes, de la dispersion) à lui appliquer (rejeu en deux temps)."""
    from . import means
    pred = pred or (strength.predict(model, m["home"], m["away"], on, m.get("referee")) if model else None)
    if not pred:
        return []
    if means_acc is not None:
        mean_stats(pred, m, means_acc)
    if fix:
        pred = means.apply(pred, *fix)
    f = {"sources": []}
    features.fill_deep(f, pred)
    home, away = pred["home"], pred["away"]
    opts, bases = fair._options(f, home, away)
    fst = settle.fd_stats(m)
    ev = {"scores": [{"name": home, "score": str(m["hg"])}, {"name": away, "score": str(m["ag"])}]}
    out = []
    for market, name, point, p, _ in fair.best_per_family(opts, MARKETS, bases):
        r = {"market": market, "name": name, "point": point, "p0": round(p, 4), "prob": round(p, 4), "sport_key": sport_key,
             "home": home, "away": away, "who": None}
        res = settle.settle_pick(r, ev, fst)
        if res in ("win", "loss"):
            out.append((r, res == "win"))
    return out


def replay_div(div, matches, frm, to, deadline=None):
    """Rejoue les matchs de `div` joués dans [frm, to[ (« AAAA-MM-JJ »). -> {matches, picks, acc}, ou None si `deadline` (time.monotonic) est
    dépassé avant la fin (le championnat sera repris en entier la fois suivante)."""
    sk = fd.DIV_TO_SPORT.get(div)
    acc, n_m, n_p, macc = {}, 0, 0, {}
    hl = strength.hl_for(div)
    upper = strength.upper_matches(div)                               # division du dessus : les relégués n'ont pas l'a priori des promus
    model, start = None, None
    todo = sorted((m for m in matches if frm <= m["date"] < to and m.get("hg") is not None and m.get("ag") is not None),
                  key=lambda m: (m["date"], m["home"]))
    from . import means
    preds = []
    for m in todo:                                  # 1er temps : prévisions (le modèle ne voit que le passé) et moyennes attendu / réel
        if deadline is not None and time.monotonic() > deadline:
            return None
        d = date.fromisoformat(m["date"])
        if start is None or d >= start + timedelta(days=C.REPLAY_STEP_DAYS):
            start = d
            model = strength.build(matches, d, hl, div=div, upper=upper)          # matchs joués AVANT d seulement
        pred = strength.predict(model, m["home"], m["away"], d, m.get("referee")) if model else None
        if pred:
            one = {}
            mean_stats(pred, m, one)
            _add_means(macc, one)
            preds.append((m, d, pred, one))
    # 2e temps : les pronostics avec les moyennes et la dispersion corrigées comme en direct, pour que la fiabilité rejouée soit celle des chances
    # corrigées (sinon le direct serait corrigé deux fois). Corrections mesurées sur les seuls matchs des JOURS D'AVANT (jamais l'avenir).
    run, pending, day = {}, {}, None
    with means.off():
        for m, d, pred, one in preds:
            if d != day:
                _add_means(run, pending)
                pending, day = {}, d
            fix = ({f: a[1] / a[2] for f, a in run.items() if a[0] >= means.BOOT_MIN and a[2] > 0}, {f: means.disp_of(a) for f, a in run.items()})
            got = picks(None, m, sk, d, pred=pred, fix=fix)
            _add_means(pending, one)
            if got:
                n_m += 1
            for r, won in got:
                calib.accumulate(acc, r, won)
                n_p += 1
    return {"matches": n_m, "picks": n_p, "acc": acc, "means": macc}


def _add_means(dst, src):
    """Ajoute les cumuls de moyennes `src` ({famille: [7 sommes]}) à `dst`."""
    for f, a in src.items():
        have = dst.setdefault(f, [0.0] * len(a))
        dst[f] = [round(x + y, 4) for x, y in zip(have, a)]


def _total(per_div):
    """Somme des cumuls des championnats rejoués."""
    out = {}

    def add(dst, src):
        for k, v in src.items():
            if calib._acc_list(v):
                have = dst.setdefault(k, [0, 0, 0.0])
                for i in range(3):
                    have[i] = round(have[i] + v[i], 4)
            elif isinstance(v, dict):
                add(dst.setdefault(k, {}), v)
    for v in per_div.values():
        add(out, v.get("acc") or {})
    return out


def complete(res):
    """Un championnat est-il bien rejoué (au moins MIN_MATCHES matchs) ? Sinon ses CSV manquaient : il sera refait."""
    return bool(res) and res.get("matches", 0) >= MIN_MATCHES


def missing(doc=None):
    """Championnats à (re)jouer : pas encore bien rejoués, ou rejoués avant la mesure des moyennes (sans « means » : leur ancien résultat reste compté
    en attendant)."""
    doc = load() if doc is None else doc
    return [d for d in divs() if not complete((doc.get("divs") or {}).get(d)) or "means" not in (doc.get("divs") or {}).get(d, {})]


def _total_means(per_div):
    """Moyennes et dispersion du rejeu : {"fam": {famille: somme}, "lg": {famille: {championnat: cumul}}}."""
    fam, lg = {}, {}
    for div, v in per_div.items():
        sk = fd.DIV_TO_SPORT.get(div, div)
        for f, a in (v.get("means") or {}).items():
            have = fam.setdefault(f, [0.0] * len(a))
            fam[f] = [round(x + y, 4) for x, y in zip(have, a)]
            lg.setdefault(f, {})[sk] = list(a)
    return {"fam": fam, "lg": lg}


def done(doc=None):
    """Le rejeu est-il complet (tous les championnats) ?"""
    doc = load() if doc is None else doc
    return bool(doc) and not missing(doc)


def matches(div):
    """Matchs du championnat sur les trois saisons que lit le modèle (fd.default_seasons) : CSV en cache, et pour une saison absente du cache, un
    téléchargement (une seule fois : les saisons passées sont gardées pour toujours). Certains championnats ne sont pas rafraîchis le matin."""
    out = []
    for season in fd.default_seasons():
        res = http.cached(fd.url(div, season))
        rows = fd.parse_csv(res.text, div, season)[0] if res else []
        if not rows:
            rows, errs = fd.fetch_season(div, season)
            for e in errs:
                print(f"[replay] {e}", file=sys.stderr)
        out += rows
    return out


def run(seconds=None, force=False, today=None, loader=None, log=None):
    """Rejoue les championnats pas encore faits, dans la limite de `seconds` (None : sans limite). Écrit data/calib_boot.json après chaque
    championnat. `loader(div)` : matchs du championnat (matches par défaut : CSV en cache, saisons manquantes téléchargées). Un championnat de moins
    de MIN_MATCHES matchs (CSV absents) est gardé mais refait la fois suivante. -> le document."""
    log = log or (lambda s: None)
    deadline = None if seconds is None else time.monotonic() + seconds
    doc = {} if force else load()
    if not doc:
        to = today or state.now().date()
        doc = {"v": VERSION, "from": str(to - timedelta(days=C.REPLAY_DAYS)), "to": str(to), "divs": {}}
    loader = loader or matches
    for div in missing(doc):
        t0 = time.monotonic()
        try:
            res = replay_div(div, loader(div), doc["from"], doc["to"], deadline)
        except Exception as e:                                   # noqa: BLE001 - un championnat illisible ne bloque pas les autres
            print(f"[replay] {div} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
            res = {"matches": 0, "picks": 0, "acc": {}, "error": type(e).__name__}
        if res is None:
            log(f"temps écoulé : reprise au prochain passage ({len(divs()) - len(missing(doc))}/{len(divs())} championnats faits)")
            break
        cur = load()                                   # un autre rejeu (réglage du dimanche, « kelly rejeu ») a pu en finir d'autres entre-temps
        if cur.get("from") == doc["from"] and cur.get("to") == doc["to"]:
            doc["divs"] = {d: v for d, v in (cur.get("divs") or {}).items() if complete(v)} | {d: v for d, v in doc["divs"].items() if complete(v) or d not in cur.get("divs", {})}
        doc["divs"][div] = res
        doc["acc"] = _total(doc["divs"])
        doc["means"] = _total_means(doc["divs"])
        doc["built"] = str(today or state.now().date())
        store.save(FILE, doc, compact=True)
        log(f"{div} ({calib.league_name(fd.DIV_TO_SPORT.get(div, div))}) : {res['matches']} matchs, {res['picks']} pronostics "
            f"({time.monotonic() - t0:.0f} s)")
    return doc


def totals(doc=None):
    """(matchs, pronostics) rejoués."""
    doc = load() if doc is None else doc
    ds = (doc.get("divs") or {}).values()
    return sum(d.get("matches", 0) for d in ds), sum(d.get("picks", 0) for d in ds)


def status_line(doc=None):
    """« 📚 Rejeu de la saison passée : 5 400 matchs, 61 200 pronostics » ; « … · en cours (7/18 championnats) » ; '' sans rejeu."""
    doc = load() if doc is None else doc
    if not doc.get("divs"):
        return ""
    n_m, n_p = totals(doc)
    head = f"📚 Rejeu de la saison passée : {fmt.thousands(n_m)} matchs, {fmt.thousands(n_p)} pronostics"
    if not done(doc):
        return head + f" · {len(divs()) - len(missing(doc))}/{len(divs())} championnats"
    return head


def family_lines(doc=None):
    """Ce que le rejeu a mesuré par famille, du plus suivi au moins suivi, et la correction qu'il donne : « 🔴 Tirs · 4 100 · trop confiante
    (52 % / 66 %) · ×0,73 »."""
    from . import status
    doc = load() if doc is None else doc
    acc = doc.get("acc") or {}
    out = []
    for f, (n, w, s) in sorted(((f, a) for f, a in acc.items() if not f.startswith("_") and calib._acc_list(a) and a[0]), key=lambda x: -x[1][0]):
        mood = status._mood(w / n, s / n)
        k = calib.factor(calib.rep_market(f), acc, base=1.0)
        out.append(f"{'🔴' if mood == 'trop confiante' else '🟢'} {calib.LABELS.get(f, f).capitalize()} · {fmt.thousands(round(n))} · {mood} "
                   f"({fmt.pct(w / n)} / {fmt.pct(s / n)})" + (f" · ×{fmt.num(k)}" if abs(k - 1) >= 0.005 else ""))
    return out


def message(doc=None):
    """Message Telegram de fin de rejeu (HTML) ; les championnats sans données sont nommés."""
    doc = load() if doc is None else doc
    n_m, n_p = totals(doc)
    miss = missing(doc)
    lines = ["📚 <b>Rejeu de la saison passée terminé</b>",
             fmt.esc(f"{fmt.thousands(n_m)} matchs de {len(divs()) - len(miss)} championnat{'s' if len(divs()) - len(miss) > 1 else ''}, "
                     f"{fmt.thousands(n_p)} pronostics de stats réglés.")]
    if miss:
        names = ", ".join(calib.league_name(fd.DIV_TO_SPORT.get(d, d)) for d in miss)
        lines.append(fmt.esc(f"⚠️ Pas assez de données pour : {names} (réessayé au prochain « kelly rejeu » et le dimanche)."))
    lines.append("")
    lines += [fmt.esc(x) for x in family_lines(doc)]
    lines += ["", fmt.i(fmt.esc("Kelly part de ces corrections (×) tant qu'il n'a pas assez de vrais résultats ; dès qu'il en a, ce sont eux qui "
                                "comptent, sans être mélangés au rejeu. Détail : « bilan détail »."))]
    return fmt.Html("\n".join(lines))


def main(argv=()):
    """`python bot.py --replay [--force]` : rejoue tout (sans limite de temps), affiche le résumé, prévient sur Telegram. Code 0, 1 si échec."""
    try:
        doc = run(force="--force" in argv, log=lambda s: print("[replay] " + s, flush=True))
        print(status_line(doc) or "[replay] rien à rejouer : aucun CSV football-data en cache")
        for x in family_lines(doc):
            print("  " + x)
        if doc.get("divs") and "--sample" not in argv:
            from . import telegram
            if telegram.creds()[0]:
                telegram.send(message(doc))
        return 0
    except Exception as e:                                       # noqa: BLE001
        print(f"[replay] échec : {type(e).__name__}: {e}", file=sys.stderr)
        return 1
