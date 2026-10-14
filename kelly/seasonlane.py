"""Voie `saison` : jambes, prix 1xBet, étiquette, journal, apprentissage (le modèle est dans kelly/season.py).

Isolement : cette voie ne touche ni aux voies sharpgap / stats, ni au gel de l'ensemble (config.ENSEMBLE_FROZEN_MODEL), ni à leurs documents. Elle a :
  - ses propres pronostics (une ligne par match et par marché, `c["strategy"] = "saison"`, clé terminée par « |saison » : jamais la même clé qu'un pari des autres voies) ;
  - son propre mélange avec le marché : p_ens = expit(wm x logit(p_modèle) + (1 - wm) x logit(p_marché)), p_marché = sharp (si présent et frais) et 1xBet sans marge en log-cotes
    (ws = ENSEMBLE_PRIOR_SHARP) ; wm = SEASON_MODEL_WEIGHT (0,35), puis appris par famille (data/season_weights.json) ; sans avis de marché, aucune jambe (le modèle seul n'est
    jamais jugé) ; le gel ne s'applique pas ici, mais `ensemble.sharp_for` garde sa fraîcheur plus stricte pour les cinq familles figées ;
  - sa propre calibration par famille et sens (data/season_calib.json, les fonctions de kelly/calib.py sur CE document, sans le prior du rejeu) ;
  - son propre journal (data/season_log.json, archive arch/sea/), mêmes champs que le journal d'ensemble (pm, px, ps, xref, xclose, strategy) ;
  - ses propres champs : `sval` = 1 (jambe à valeur : proba retenue x cote >= 1 + SEASON_MARGIN) ou 0 (jambe de remplissage : proba retenue >= SEASON_FILL_MIN_P et espérance au prix du
    marché >= SEASON_FILL_MIN_EV) ; `ref` = 1 sur la ligne de RÉFÉRENCE du marché (la plus proche de FAIR_P_TARGET, choisie sans regarder le prix).
Apprentissage (poids, calibration) : les seules lignes de référence. Les lignes choisies parce qu'elles valent quelque chose au prix 1xBet sont triées sur leur écart au marché : les
apprendre fabriquerait le biais du gagnant (les tirs « moins de », 13/10). Elles servent à la mesure (bilan, CLV), jamais à l'apprentissage.
Une jambe par match dans un combiné (combos), comme les autres voies ; ici une ligne par marché."""
import sys
import time
from datetime import datetime, timezone

from . import calib, catalogue, config as C, ensemble, fair, lanes, odds, prematch, season, store, strength
from .sources import xbet

LOG = "season_log.json"
ARCH = "arch/sea/"
WEIGHTS_FILE = "season_weights.json"
CALIB_FILE = "season_calib.json"
SUFFIX = "|saison"
MARKETS = frozenset(("totals", "btts", "double_chance", "h2h", "team_totals_home", "team_totals_away", prematch.CORNERS, prematch.CARDS)
                    + tuple(m for m in fair.STAT_SPECS if not m.startswith(("team_throws", "team_offsides"))))
FALLBACK_ALPHA = {"corners": (0.04, 0.03), "cards": (0.06, 0.06), "yellow": (0.06, 0.06), "shots": (0.05, 0.03), "sot": (0.05, 0.04), "fouls": (0.05, 0.03)}      # (équipe, total) : constantes de fair.py


def mode():
    return lanes.season_mode()


def league_ok(sport_key):
    return not C.SEASON_LEAGUES or sport_key in C.SEASON_LEAGUES


def fallback_for(sport_key):
    """Repli de dispersion d'une ligue de moins de SEASON_ALPHA_MIN_MATCHES matchs : celui du rejeu (strength.fit_stat de la ligue football-data, ses alpha et kappa), sinon les constantes de
    fair.py ; {stat: {alpha, atot}, kappa}."""
    out = {stat: {"alpha": a, "atot": t} for stat, (a, t) in FALLBACK_ALPHA.items()}
    try:
        from .sources import footballdata as fd
        div = fd.SPORT_TO_DIV.get(sport_key)
        model = strength.get(div) if div and div not in fd.NEW_DIVS else None
        if model:
            for stat, s in model["stats"].items():
                if stat != "goals" and stat in out:
                    out[stat] = {"alpha": s.get("alpha", out[stat]["alpha"]), "atot": s.get("atot", out[stat]["atot"])}
            out["kappa"] = strength.kappa_of(model["stats"]["goals"])
    except Exception:                                  # noqa: BLE001 - le repli n'est qu'un repli
        pass
    return out


# ---------- poids et calibration propres à la voie ----------

def load_weights():
    d = store.load(WEIGHTS_FILE, {})
    d = d if isinstance(d, dict) else {}
    d.setdefault("fam", {})
    d.setdefault("done", {})
    return d


def weights(fam, data=None):
    """{"wm", "n", "prior"} : poids du modèle face au marché pour la famille `fam` (SEASON_MODEL_WEIGHT tant que SEASON_MIN_N matchs ne sont pas réglés, puis la moyenne a posteriori)."""
    d = load_weights() if data is None else data
    acc = ((d.get("fam") or {}).get(fam) or {}).get("m")
    return {"wm": ensemble._weight(acc, C.SEASON_MODEL_WEIGHT, C.SEASON_PRIOR_SD, C.SEASON_MIN_N), "n": round((acc or {}).get("n", 0.0), 1), "prior": C.SEASON_MODEL_WEIGHT}


def load_calib():
    d = store.load(CALIB_FILE, {})
    return d if isinstance(d, dict) else {}


def calibrated(market, p, sport_key, side, data):
    """Proba du modèle de saison corrigée par la fiabilité MESURÉE sur la voie (calib.factor sur data/season_calib.json, point de départ 1 : pas de prior du rejeu)."""
    return 0.5 + (p - 0.5) * calib.factor(market, data, sport_key, side, p, base=1.0)


def learn(log):
    """Apprend des lignes de RÉFÉRENCE réglées du journal : poids du modèle par famille (data/season_weights.json) et calibration (data/season_calib.json). Idempotent par clé (« ens » sur la
    ligne). Même effectif que ensemble.learn (un match compte une fois par famille). -> nombre de lignes apprises."""
    data, cal = load_weights(), load_calib()

    def learnable(r):
        return (r.get("ref") and r.get("result") in ("win", "loss") and r.get("key") and r["key"] not in data["done"] and not r.get("ens")
                and r.get("pm") is not None and (r.get("px") is not None or r.get("ps") is not None))
    todo = sorted((r for r in log if learnable(r)), key=lambda r: (str(r.get("date") or ""), str(r.get("key") or "")))
    if not todo:
        return 0
    per = {}
    for r in todo:
        k = (ensemble.family(r["market"]), str(r.get("date") or ""), r.get("id") or r["key"].split("|")[0])
        per[k] = per.get(k, 0) + 1
    cal["_t"] = calib.aged(cal, cal.get("_t"))
    for r in todo:
        fam = ensemble.family(r["market"])
        y = 1 if r["result"] == "win" else 0
        w = 1.0 / per[(fam, str(r.get("date") or ""), r.get("id") or r["key"].split("|")[0])]
        p_mkt = ensemble.pool_market(r.get("ps"), r.get("px"), C.ENSEMBLE_PRIOR_SHARP)[0]
        ensemble._add(data["fam"].setdefault(fam, {}).setdefault("m", {"ll": [0.0] * len(ensemble.GRID), "n": 0.0}),
                      [ensemble._loglik(ensemble.pool(r["pm"], p_mkt, g), y) for g in ensemble.GRID], w)
        calib.accumulate(cal, r | {"prob": r.get("p0") or r["pm"], "p0": r.get("p0") or r["pm"]}, bool(y), w)
        data["done"][r["key"]] = str(r.get("date") or "")
        r["ens"] = True
    keep = sorted(data["done"].values())[-3000:]
    cut = keep[0] if keep else ""
    data["done"] = {k: v for k, v in data["done"].items() if v >= cut}
    store.save(WEIGHTS_FILE, data, compact=True)
    store.save(CALIB_FILE, cal, compact=True)
    return len(todo)


# ---------- jambes ----------

def options(pred, home, away):
    """Lignes de marché du modèle de saison pour un match : [(marché, côté, ligne, proba brute, référence d'un match moyen de la ligue)], marchés de MARKETS, moyennes plausibles."""
    out = []
    for l in strength.lines(pred, home, away):
        mk, pt = l["market"], l["point"]
        if (mk in fair.SIDE_MARKETS or mk == "team_totals") and l.get("side"):
            mk = f"{mk}_{l['side']}"
        if mk not in MARKETS:
            continue
        if mk in fair.STAT_SPECS:
            s = (pred["stats"] or {}).get(l["stat"])
            mean = None if not s else (s["h"] + s["a"] if mk.endswith("_total") else s["h" if mk.endswith("_home") else "a"])
            lo, hi = fair.STAT_SPECS[mk][2]
            if mean is None or not lo <= mean <= hi:
                continue
        out.append((mk, l["name"], pt, l["p"], l["base"]))
    return out


def _key(ev_id, market, name, point):
    return f'{ev_id}|{market}||{point if point is not None else ""}|{name}{SUFFIX}'


def make(scan, now, today=None):
    """Pronostics `saison` du jour (sans prix) : un par match et par marché, avec toutes ses lignes plausibles en c["alts"] ([nom, ligne, proba brute, référence]), la ligne par défaut étant
    celle dont la proba est la plus proche de FAIR_P_TARGET (c'est la ligne de RÉFÉRENCE, choisie sans regarder le prix). Seulement les ligues couvertes par le modèle de saison."""
    out = []
    today = today or now.date()
    for key, evs in (scan.get("leagues") or {}).items():
        if not league_ok(key):
            continue
        model = season.get(key, today, fallback=fallback_for(key))
        if not model:
            continue
        for ev in sorted(evs, key=lambda e: e["commence_time"]):
            if _ko(ev) <= now:
                continue
            pred = season.predict(model, ev["home_team"], ev["away_team"])
            if not pred:
                continue
            by = {}
            for mk, name, pt, p, base in options(pred, ev["home_team"], ev["away_team"]):
                by.setdefault(mk, []).append((name, pt, p, base))
            for mk, lines in by.items():
                lo, hi = C.FAIR_P_RANGE
                alts = [[n, pt, round(p, 4), round(b, 4)] for n, pt, p, b in lines if lo <= p <= hi]
                if not alts:
                    continue
                n0, pt0, p0, b0 = min(alts, key=lambda a: abs(a[2] - C.FAIR_P_TARGET))
                who = fair.side_name(mk, ev)
                out.append({"key": _key(ev["id"], mk, n0, pt0), "id": ev["id"], "sport_key": ev.get("sport_key") or key, "league": ev.get("league") or catalogue.name(key),
                            "event": f'{ev["home_team"]} - {ev["away_team"]}', "home": ev["home_team"], "away": ev["away_team"], "commence": ev["commence_time"],
                            "start": ev["commence_time"][11:16], "market": mk, "name": n0, "point": pt0, "who": who, "group": "soccer",
                            "pick": odds.label(mk, n0, pt0, who, "soccer"), "p0": p0, "base": b0, "alts": alts, "conf": round(pred["qs"].get(_stat_of(mk), pred["q"]), 2), "score": p0 - 0.5, "src": "saison", "why": f"saison en cours ({model.get('source')})",
                            "mu": None, "n_min": min(pred["n"]), "lane": "saison"} | ({"xgid": ev["xgid"]} if ev.get("xgid") else {}))
    return out


def _ko(ev):
    return datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))


def _stat_of(market):
    return {prematch.CORNERS: "corners", prematch.CARDS: "cards"}.get(market) or (market.split("_")[1] if market in fair.STAT_SPECS else "goals")


def attach_sharp(pronos, picks, history=None):
    """Avis sharp des paris réels (The Odds API) posés sur les lignes des pronostics `saison` : c["psh"] (comme ensemble.attach, SANS toucher aux paris réels : ensemble.attach leur
    écrit c["p_model"], ce qui écraserait celui des autres voies). -> nombre de pronostics couverts."""
    sharp = {}
    for c in picks or []:
        s = ensemble.sharp_of(c)
        if s:
            h = (history or {}).get(c.get("key"))
            sharp[ensemble._kid(c)] = s + (c.get("sharp_at") or (h.get("t") if isinstance(h, dict) else None),)
    n = 0
    for c in pronos:
        psh = {}
        for a in c.get("alts") or []:
            kid = ensemble._kid(c, a[0], a[1])
            if kid in sharp:
                psh[f"{kid[2]}|{kid[3]}"] = [round(sharp[kid][0], 4), sharp[kid][1], sharp[kid][2], sharp[kid][3]]
        if psh:
            c["psh"] = psh
            n += 1
        else:
            c.pop("psh", None)
    return n


def _line(c, px, name, point, p0, base, odd, now, wdata, cdata):
    """Avis et décision de la voie sur une ligne du pronostic `c` à la cote 1xBet `odd` -> dict (jamais None) ; "kind" : "value" | "fill" | None. Sans avis de marché : pas de proba retenue."""
    fam = ensemble.family(c["market"])
    sd = calib.side(c | {"name": name})
    pm = calibrated(c["market"], p0, c.get("sport_key"), sd, cdata)
    p_x = ensemble.x_fair(px, c["market"], name, point, c.get("home"), c.get("away"))
    p_s = ensemble.sharp_for(c, name, point, now)
    p_mkt = ensemble.pool_market(p_s, p_x, C.ENSEMBLE_PRIOR_SHARP)[0]
    wm = weights(fam, wdata)["wm"]
    out = {"name": name, "point": point, "p0": p0, "base": base, "odd": odd, "pm": pm, "p_x": p_x, "p_s": p_s, "p_mkt": p_mkt, "wm": wm, "p": None, "ev": None, "ev_mkt": None, "kind": None}
    if p_mkt is None:
        return out
    p = ensemble.pool(pm, p_mkt, wm)
    out.update(p=p, ev=p * odd - 1, ev_mkt=p_mkt * odd - 1)
    if p * odd >= 1 + C.SEASON_MARGIN:
        out["kind"] = "value"
    elif p >= C.SEASON_FILL_MIN_P and out["ev_mkt"] >= C.SEASON_FILL_MIN_EV:
        out["kind"] = "fill"
    return out


def _rank(v):
    ev = v["ev"]
    return (v["kind"] == "value", v["p"] >= C.FAIR_COMBO_MIN_P, ev * v["p"] if ev > 0 else ev)


def _apply(c, v, refs, ref_line=False):
    """Pose sur une COPIE du pronostic `c` la ligne évaluée `v` (voir _line) : le pari, sa proba retenue, son prix, ses avis, son étiquette `saison`. `sval` : 1 (valeur), 0 (remplissage) ;
    absent pour une ligne de référence sans verdict (`ref_only`, jamais une jambe). -> la copie."""
    d = dict(c)
    name, point = v["name"], v["point"]
    d.update(name=name, point=point, key=_key(c["id"], c["market"], name, point), pick=odds.label(c["market"], name, point, c.get("who"), "soccer"))
    p = v["p"]
    d.update(prob=round(p, 4), p_ens=round(p, 4), p_model=round(v["pm"], 4), p0=round(v["p0"], 4), base=round(v["base"], 4), fair=round(1 / p, 2), xodds=v["odd"], xev=round(v["ev"], 4),
             ew=round(v["wm"], 4), esrc="saison", strategy="saison", lane="saison")
    if v["kind"]:
        d.update(sval=1 if v["kind"] == "value" else 0, min_odds=round((1 + (C.SEASON_MARGIN if v["kind"] == "value" else C.SEASON_FILL_MIN_EV)) / p, 2))
    else:
        d.update(ref_only=1, min_odds=round(1 / p, 2))
    for k, val in (("p_x", v["p_x"]), ("p_sharp", v["p_s"])):
        if val is None:
            d.pop(k, None)
        else:
            d[k] = round(val, 4)
    d["xp"] = d.get("p_x")
    ref = (refs or {}).get((c["market"], name, None if point is None else float(point)))
    if ref:
        d["xref"] = ref
    else:
        d.pop("xref", None)
    if ref_line:
        d["ref"] = 1
    d.pop("alts", None)
    return d


def price(c, px, refs, now, wdata=None, cdata=None):
    """Met le pronostic `c` au prix 1xBet de `px` : -> (jambe choisie ou None, ligne de référence ou None). La jambe est la meilleure ligne des alternatives du marché (valeur d'abord,
    puis assez probable pour un combiné, puis espérance), None si aucune ne vaut ni la valeur ni le remplissage. La ligne de référence est celle du marché la plus proche de
    FAIR_P_TARGET qui a un prix ET un avis de marché, quel que soit le verdict : la seule dont on apprend (voir le haut du module). Quand la jambe choisie est aussi la référence, elle porte
    ref = 1 et il n'y a pas de seconde ligne ; sinon la référence est rendue à part (sval absent si elle n'a ni valeur ni remplissage : `ref_only`)."""
    wdata = load_weights() if wdata is None else wdata
    cdata = load_calib() if cdata is None else cdata
    evals = []
    for name, point, p0, base in c.get("alts") or []:
        odd = xbet.lookup(px, c["market"], name, point)
        if odd:
            evals.append(_line(c, px, name, point, p0, base, odd, now, wdata, cdata))
    scored = [v for v in evals if v["p"] is not None]
    if not scored:
        return None, None
    ref_v = min(scored, key=lambda v: abs(v["p0"] - C.FAIR_P_TARGET))
    good = [v for v in scored if v["kind"]]
    best = max(good, key=_rank) if good else None
    chosen = _apply(c, best, refs, ref_line=best is ref_v) if best else None
    ref = None if best is ref_v else _apply(c, ref_v, refs, ref_line=True)
    return chosen, ref


def run(scan, picks, day, now, fetch=None, history=None):
    """Passage du matin de la voie : pronostics, avis sharp, prix 1xBet (le cache du relevé principal sert d'abord), choix des lignes, journal. -> (jambes `saison` jouables, mode) ; ([], mode)
    quand la voie est éteinte ou sans jambe. `fetch` : injectable (tests). Le relevé 1xBet de la voie ne remplace pas le compte rendu du relevé principal (record=False) et tient en
    C.SEASON_SECONDS. Ne lève jamais."""
    m = mode()
    if m == "off" or not C.XBET_ENABLED or (fetch is None and not C.SOURCES_REFRESH):
        return [], m
    try:
        pronos = make(scan, now, day)
        if not pronos:
            return [], m
        attach_sharp(pronos, picks, history if history is not None else store.load("odds_history.json", {}))
        from . import xprice
        events, wanted = xprice._events(pronos)[:C.SEASON_MAX_MATCHES], xprice._wanted(pronos)
        deadline = time.monotonic() + C.SEASON_SECONDS
        found = fetch(events, deadline=deadline, wanted=wanted) if fetch is not None else xbet.fetch_day(events, deadline=deadline, wanted=wanted, record=False)
        wdata, cdata = load_weights(), load_calib()
        legs, rows = [], []
        for c in pronos:
            f = found.get(c["id"])
            if not f or not f.get("px"):
                continue
            chosen, ref = price(c, f["px"], f.get("refs"), now, wdata, cdata)
            for d in (chosen, ref):
                if d is not None:
                    d["xgid"] = f["gid"]
                    if f.get("cid"):
                        d["xcid"] = f["cid"]
                    rows.append(d)
            if chosen is not None:
                legs.append(chosen | ({"ghost": 1} if m == "ghost" else {}))
        n = record(rows, day, now)
        print(f"[saison] {len(pronos)} pronostic(s), {len(found)} match(s) chez 1xBet, {len(legs)} jambe(s) ({sum(1 for c in legs if c.get('sval'))} à valeur), {n} avis au journal", file=sys.stderr)
        return legs, m
    except Exception as e:                          # noqa: BLE001 - la voie ne casse jamais le matin
        print(f"[saison] voie ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return [], m


def record(rows, day, now):
    """Journal de la voie (data/season_log.json) : chaque jambe choisie et chaque ligne de référence, avec leurs avis (pm, px, ps). -> lignes ajoutées."""
    if not ensemble.logging_on() or not rows:
        return 0
    try:
        return ensemble.record(rows, str(day), now, name=LOG, shadow=False)
    except Exception as e:                          # noqa: BLE001
        print(f"[saison] journal ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def journal_rows():
    """Journal + archive de la voie (le journal, plus complet, l'emporte)."""
    log = store.load(LOG, [])
    log = [r for r in log if isinstance(r, dict)] if isinstance(log, list) else []
    seen = {r.get("key") for r in log}
    return log + [r for r in ensemble.archive_rows(prefix=ARCH) if r.get("key") not in seen]
