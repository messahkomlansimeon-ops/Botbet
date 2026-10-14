"""Voie `saison` : jambes, prix 1xBet, étiquette, journal, apprentissage (le modèle est dans kelly/season.py).

Isolement : cette voie ne touche ni aux voies sharpgap / stats, ni au gel de l'ensemble (config.ENSEMBLE_FROZEN_MODEL), ni à leurs documents. Elle a :
  - ses propres pronostics (une ligne par match et par marché, `c["strategy"] = "saison"`, clé terminée par « |saison » : jamais la même clé qu'un pari des autres voies) ;
  - son propre mélange avec le marché : p_ens = expit(wm x logit(p_modèle) + (1 - wm) x logit(p_marché)), p_marché = sharp (si présent et frais) et 1xBet sans marge en log-cotes
    (ws = ENSEMBLE_PRIOR_SHARP) ; wm = SEASON_MODEL_WEIGHT (0,20), puis appris par famille (data/season_weights.json) ; sans avis de marché, aucune jambe (le modèle seul n'est
    jamais jugé) ; le gel ne s'applique pas ici, mais `ensemble.sharp_for` garde sa fraîcheur plus stricte pour les cinq familles figées ;
  - sa propre calibration par famille et sens (data/season_calib.json, les fonctions de kelly/calib.py sur CE document, sans le prior du rejeu) ;
  - son propre journal (data/season_log.json, archive arch/sea/), mêmes champs que le journal d'ensemble (pm, px, ps, xref, xclose, strategy) ;
  - ses propres champs : `sval` = 1 (jambe à valeur : proba retenue x cote >= 1 + SEASON_MARGIN) ou 0 (jambe de remplissage : proba retenue >= SEASON_FILL_MIN_P et espérance au prix du
    marché >= SEASON_FILL_MIN_EV) ; `ref` = 1 sur la ligne de RÉFÉRENCE du marché (la plus proche de FAIR_P_TARGET, choisie sans regarder le prix).
Apprentissage (poids, calibration) : les seules lignes de référence, hors coupes (cup = true : mesurées au bilan, pas encore apprises). Les lignes choisies parce qu'elles valent quelque chose au prix 1xBet sont triées sur leur écart au marché : les
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
STATE_FILE = "season_state.json"
EVENTS_FILE = "season_events.json"
SUFFIX = "|saison"
MARKETS = frozenset(("totals", "btts", "double_chance", "h2h", "team_totals_home", "team_totals_away", prematch.CORNERS, prematch.CARDS)
                    + tuple(m for m in fair.STAT_SPECS if not m.startswith(("team_throws", "team_offsides"))))
FALLBACK_ALPHA = {"corners": (0.04, 0.03), "cards": (0.06, 0.06), "yellow": (0.06, 0.06), "shots": (0.05, 0.03), "sot": (0.05, 0.04), "fouls": (0.05, 0.03)}      # (équipe, total) : constantes de fair.py


def mode():
    return lanes.season_mode()


def month_ok():
    """Le plafond mensuel de données 1xBet (C.XBET_MONTH_MB) laisse de la place à la voie (C.SEASON_MONTH_PCT %) : au-delà, elle ne relit plus 1xBet (le relevé du matin passe avant)."""
    try:
        return xbet.usage()["bytes"] < C.SEASON_MONTH_PCT / 100 * C.XBET_MONTH_MB * 1e6
    except Exception:                          # noqa: BLE001 - compteur illisible : pas de blocage
        return True


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
        return (r.get("ref") and not r.get("cup") and r.get("result") in ("win", "loss") and r.get("key") and r["key"] not in data["done"] and not r.get("ens")
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


def cup_models(today):
    """{clé de ligue: modèle `saison`} de TOUS les championnats suivis (même hors SEASON_LEAGUES : une coupe a besoin du championnat de chaque équipe)."""
    out = {}
    for sk in known_leagues(False):
        m = season.get(sk, today, fallback=fallback_for(sk))
        if m:
            out[sk] = m
    return out


def _why_none(model, models, cup, home, away):
    """Pourquoi aucune prévision pour ce match : ("few", équipes sous C.SEASON_MIN_MATCHES matchs) ou ("unknown", équipes non reconnues ou sans modèle)."""
    few, unknown = set(), set()
    for name in (home, away):
        if cup:
            hit = season.locate(models or {}, name)
            if not hit:
                unknown.add(name)
            elif models[hit[0]]["stats"]["goals"]["t"][hit[1]]["n"] < C.SEASON_MIN_MATCHES:
                few.add(name)
        else:
            t = season.find_team(model, name) if model else None
            if not t:
                unknown.add(name)
            elif season.teams(model)[t]["n"] < C.SEASON_MIN_MATCHES:
                few.add(name)
    return few, unknown


def make(scan, now, today=None, info=None):
    """Pronostics `saison` du jour (sans prix) : un par match et par marché, avec toutes ses lignes plausibles en c["alts"] ([nom, ligne, proba brute, référence]), la ligne par défaut étant
    celle dont la proba est la plus proche de FAIR_P_TARGET (c'est la ligne de RÉFÉRENCE, choisie sans regarder le prix). Seulement les ligues couvertes par le modèle de saison.
    `info` (dict) reçoit le compte : matchs examinés, de coupe, prédits, équipes sous le minimum de matchs, équipes non reconnues."""
    info = info if info is not None else {}
    for k in ("events", "cup_events", "predicted"):
        info.setdefault(k, 0)
    info.setdefault("few", set())
    info.setdefault("unknown", set())
    out = []
    today = today or now.date()
    models = None
    for key, evs in (scan.get("leagues") or {}).items():
        if not league_ok(key):
            continue
        cup = key in C.SEASON_CUPS
        if cup:                                      # coupe : l'attente vient du profil de championnat de chaque équipe (season.cup_predict), jambes marquées cup = true
            models = cup_models(today) if models is None else models
            model = {"source": "coupe : profils de championnat"} if models else None
        else:
            model = season.get(key, today, fallback=fallback_for(key))
        if not model:
            continue
        for ev in sorted(evs, key=lambda e: e["commence_time"]):
            if _ko(ev) <= now:
                continue
            info["events"] += 1
            info["cup_events"] += 1 if cup else 0
            pred = season.cup_predict(models, ev["home_team"], ev["away_team"]) if cup else season.predict(model, ev["home_team"], ev["away_team"])
            if not pred:
                few, unknown = _why_none(model, models, cup, ev["home_team"], ev["away_team"])
                info["few"] |= few
                info["unknown"] |= unknown
                continue
            info["predicted"] += 1
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
                            "mu": None, "n_min": min(pred["n"]), "lane": "saison"} | ({"xgid": ev["xgid"]} if ev.get("xgid") else {}) | ({"cup": True} if cup else {}))
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
    out = {"name": name, "point": point, "p0": p0, "base": base, "odd": odd, "pm": pm, "p_x": p_x, "p_s": p_s, "p_mkt": p_mkt, "wm": wm, "p": None, "ev": None, "ev_mkt": None, "kind": None, "why": ""}
    if p_mkt is None:
        return out
    p = ensemble.pool(pm, p_mkt, wm)
    kind, why = verdict(p, odd, p_mkt, pm)
    out.update(p=p, ev=p * odd - 1, ev_mkt=p_mkt * odd - 1, kind=kind, why=why)
    return out


def verdict(p, odd, p_mkt, p_model=None):
    """-> (« value » | « fill » | None, raison du refus ou ''). Garde-fous copiés des autres voies : « veto » si |proba du modèle - proba du marché| > SEASON_MAX_DISAGREE, « suspect » si l'espérance
    proba retenue x cote - 1 dépasse SEASON_MAX_EDGE (donnée ou modèle faux) ; sinon valeur (p x cote >= 1 + SEASON_MARGIN) ou remplissage (p >= SEASON_FILL_MIN_P et espérance au prix du marché
    >= SEASON_FILL_MIN_EV)."""
    if p_model is not None and abs(p_model - p_mkt) > C.SEASON_MAX_DISAGREE:
        return None, "veto"
    if p * odd - 1 > C.SEASON_MAX_EDGE:
        return None, "suspect"
    if p * odd >= 1 + C.SEASON_MARGIN:
        return "value", ""
    if p >= C.SEASON_FILL_MIN_P and p_mkt * odd - 1 >= C.SEASON_FILL_MIN_EV:
        return "fill", ""
    return None, ""


def _rank(v):
    ev = v["ev"]
    return (v["kind"] == "value", v["p"] >= C.FAIR_COMBO_MIN_P, ev * v["p"] if ev > 0 else ev)


def _apply(c, v, refs, ref_line=False, leg=True):
    """Pose sur une COPIE du pronostic `c` la ligne évaluée `v` (voir _line) : le pari, sa proba retenue, son prix, ses avis, son étiquette `saison`. `sval` : 1 (valeur), 0 (remplissage) ;
    absent pour une ligne de référence sans verdict (`ref_only`, jamais une jambe). -> la copie."""
    d = dict(c)
    name, point = v["name"], v["point"]
    d.update(name=name, point=point, key=_key(c["id"], c["market"], name, point), pick=odds.label(c["market"], name, point, c.get("who"), "soccer"))
    p = v["p"]
    d.update(prob=round(p, 4), p_ens=round(p, 4), p_model=round(v["pm"], 4), p0=round(v["p0"], 4), base=round(v["base"], 4), fair=round(1 / p, 2), xodds=v["odd"], xev=round(v["ev"], 4),
             ew=round(v["wm"], 4), esrc="saison", strategy="saison", lane="saison")      # la jambe de remplissage (sval 0) devient strategy « fill » plus bas ; lane reste « saison »
    if v["kind"] and leg:
        d.update(sval=1 if v["kind"] == "value" else 0, strategy="saison" if v["kind"] == "value" else "fill", min_odds=round((1 + (C.SEASON_MARGIN if v["kind"] == "value" else C.SEASON_FILL_MIN_EV)) / p, 2))
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
    if v.get("why"):
        d["swhy"] = v["why"]                           # raison du refus (« veto », « suspect ») : gardée au journal, comptée au bilan
    d.pop("alts", None)
    return d


def price(c, px, refs, now, wdata=None, cdata=None):
    """Met le pronostic `c` au prix 1xBet de `px` : -> (jambe choisie ou None, ligne de référence ou None, ligne refusée journalisée ou None). La jambe est la meilleure ligne des alternatives du marché (valeur d'abord,
    puis assez probable pour un combiné, puis espérance), None si aucune ne vaut ni la valeur ni le remplissage. La ligne de référence est celle du marché la plus proche de
    FAIR_P_TARGET qui a un prix ET un avis de marché, quel que soit le verdict : la seule dont on apprend (voir le haut du module). Quand la jambe choisie est aussi la référence, elle porte
    ref = 1 et il n'y a pas de seconde ligne ; sinon la référence est rendue à part (sval absent : `ref_only`, jamais une jambe)."""
    wdata = load_weights() if wdata is None else wdata
    cdata = load_calib() if cdata is None else cdata
    evals = []
    for name, point, p0, base in c.get("alts") or []:
        odd = xbet.lookup(px, c["market"], name, point)
        if odd:
            evals.append(_line(c, px, name, point, p0, base, odd, now, wdata, cdata))
    scored = [v for v in evals if v["p"] is not None]
    if not scored:
        return None, None, None
    ref_v = min(scored, key=lambda v: abs(v["p0"] - C.FAIR_P_TARGET))
    good = [v for v in scored if v["kind"]]
    best = max(good, key=_rank) if good else None
    chosen = _apply(c, best, refs, ref_line=best is ref_v) if best else None
    ref = None if best is ref_v else _apply(c, ref_v, refs, ref_line=True, leg=False)       # la référence n'est JAMAIS une jambe (pas de sval) quand une autre ligne a été choisie
    refused = [v for v in scored if v["why"] and v is not ref_v and v is not best]
    extra = None
    if refused:                                       # la ligne refusée la plus tentante du marché (plus grande espérance) : au journal avec sa raison, jamais une jambe ni une référence
        worst = max(refused, key=lambda v: v["ev"])
        extra = _apply(c, worst, refs, leg=False) | {"refused": 1}
    return chosen, ref, extra


def known_leagues(_filtered=True):
    """Clés de toutes les ligues dont une source stockée (football-data, FotMob, 365scores, API-Football) a des équipes."""
    from . import apistats
    from .sources import footballdata as fd, fmmatch, s365
    out = {fd.DIV_TO_SPORT[d] for d in fd.SPORT_TO_DIV.values() if d not in fd.NEW_DIVS and d in fd.DIV_TO_SPORT}
    for doc in (fmmatch.load()["teams"], s365.load()["teams"], apistats.load()["teams"]):
        for t in doc.values():
            if isinstance(t, dict):
                out.update(k for k in t.get("sk") or [] if isinstance(k, str))
    return sorted(k for k in out if k not in C.SEASON_CUPS and (not _filtered or league_ok(k)))


def refresh_state(now, loaders=None):
    """Écrit data/season_state.json : ce que la voie suit exactement aujourd'hui ({"day", "leagues": {clé: {source, teams, opp}}}), lu par `status` (ligues suivies, équipes, équipes avec
    adversaire). Lecture seule des données stockées ; ne lève jamais. -> le document."""
    try:
        doc = {"day": str(now.date()), "leagues": season.snapshot(now.date(), known_leagues(), loaders)}
        store.save(STATE_FILE, doc, compact=True)
        return doc
    except Exception as e:                          # noqa: BLE001
        print(f"[saison] état des ligues suivies ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def state_text():
    """« ligues suivies : 12 · équipes : 230 · avec adversaire : 40 » (data/season_state.json) ; « … pas encore mesuré » sans document."""
    d = store.load(STATE_FILE, {})
    lg = (d.get("leagues") if isinstance(d, dict) else None) or {}
    if not isinstance(lg, dict) or not d:
        return "ligues suivies : pas encore mesuré"
    return (f"ligues suivies : {len(lg)} · équipes : {sum(v.get('teams', 0) for v in lg.values())} · avec adversaire : {sum(v.get('opp', 0) for v in lg.values())}"
            f" (au {d.get('day')})")


def remember_events(scan, day):
    """Garde (data/season_events.json) les matchs du jour des ligues que la voie peut traiter (championnats suivis et coupes) : de quoi reconstruire les jambes À LA DEMANDE (`saison N`) sur les
    matchs pas encore commencés, sans relire la liste des matchs. Ne lève jamais."""
    try:
        keep = set(known_leagues(False)) | set(C.SEASON_CUPS)
        evs = [{k: ev.get(k) for k in ("id", "sport_key", "league", "home_team", "away_team", "commence_time", "xgid") if ev.get(k) is not None}
               | {"sport_key": ev.get("sport_key") or key} for key, lst in (scan.get("leagues") or {}).items() if key in keep for ev in lst]
        store.save(EVENTS_FILE, {"day": str(day), "events": evs}, compact=True)
    except Exception as e:                          # noqa: BLE001
        print(f"[saison] matchs du jour non gardés : {type(e).__name__}: {e}", file=sys.stderr)


def rebuild(now, fetch=None):
    """Jambes `saison` reconstruites À LA DEMANDE (`saison N`) sur les matchs du jour pas encore commencés (championnats suivis ET coupes, LDC comprise), avec les cotes 1xBet RELUES à
    l'instant (cache de moins de C.XBET_COMBI_FRESH_MIN min repris), dans le plafond de la voie (C.SEASON_MAX_MATCHES matchs, C.SEASON_SECONDS s ; le relevé ne remplace pas le compte rendu
    du relevé principal). Les mêmes règles que le matin (modèle, marché, valeur / remplissage, garde-fous) ; les lignes sont journalisées (CLV par jambe). -> (jambes, bilan) ; le bilan :
    {events (non commencés), cup_events, predicted, read (matchs lus chez 1xBet), cup_read, no_price (matchs prédits sans prix), few (équipes sous le minimum), unknown (équipes non reconnues)}.
    Ne lève jamais. `fetch` : injectable (mêmes arguments que xbet.fetch_day)."""
    from datetime import timedelta
    from . import xprice
    info = {"events": 0, "cup_events": 0, "predicted": 0, "read": 0, "cup_read": 0, "no_price": 0, "unread": 0, "few": set(), "unknown": set()}
    day = str(now.date())
    if fetch is None and not month_ok():              # le relevé principal passe avant : la voie se tait au-delà de C.SEASON_MONTH_PCT % du plafond mensuel
        info["capped"] = True
        return [], info
    try:
        doc = store.load(EVENTS_FILE, {})
        kday = (store.load("today.json", {}) or {}).get("day") or day            # le jour KELLY (jusqu'à 04:00 UTC le lendemain), pas la date UTC
        day = str(doc.get("day") or day) if isinstance(doc, dict) else day
        evs = [e for e in (doc.get("events") or [] if isinstance(doc, dict) and day == str(kday) else [])
               if _ko(e) > now + timedelta(minutes=C.XBET_SOON_MIN)]
        scan = {"leagues": {}}
        for e in evs:
            scan["leagues"].setdefault(e["sport_key"], []).append(e)
        pronos = make(scan, now, now.date(), info)
        if not pronos:
            return [], info
        picks = (store.load("today.json", {}) or {}).get("picks") or []
        attach_sharp(pronos, picks, store.load("odds_history.json", {}))
        events, wanted = xprice._events(pronos)[:C.SEASON_MAX_MATCHES], xprice._wanted(pronos)
        kw = dict(deadline=time.monotonic() + C.SEASON_SECONDS, wanted=wanted, live=True, fresh_min=C.XBET_COMBI_FRESH_MIN)
        found = (fetch(events, **kw) if fetch is not None else xbet.fetch_day(events, record=False, **kw)) or {}
    except Exception as e:                          # noqa: BLE001
        print(f"[saison] reconstruction impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return [], info
    cups = {c["id"] for c in pronos if c.get("cup")}
    ids = {c["id"] for c in pronos}
    info["read"] = sum(1 for i in ids if (found.get(i) or {}).get("px"))
    info["cup_read"] = sum(1 for i in cups if (found.get(i) or {}).get("px"))
    wdata, cdata = load_weights(), load_calib()
    legs, rows, priced = [], [], set()
    for c in pronos:
        f = found.get(c["id"])
        if not f or not f.get("px"):
            continue
        chosen, ref, extra = price(c, f["px"], f.get("refs"), now, wdata, cdata)
        for d in (chosen, ref, extra):
            if d is not None:
                d["xgid"] = f["gid"]
                if f.get("cid"):
                    d["xcid"] = f["cid"]
                rows.append(d)
                priced.add(c["id"])
        if chosen is not None:
            legs.append(chosen)
    asked = {e["id"] for _, e in events}
    info["unread"] = len(ids - asked)                  # prédits mais au-delà du plafond de matchs : jamais demandés à 1xBet
    info["no_price"] = len(asked - priced)             # demandés mais AUCUNE ligne n'a de prix ni d'avis de marché (match introuvable chez 1xBet, marché absent, ou relevé arrêté par le délai)
    record(rows, day, now)
    return legs, info


def summary_line(info):
    """Une ligne : ce que la reconstruction a trouvé (« 🔎 12 matchs lus chez 1xBet sur 20 non commencés (dont 4 de coupe) · 5 équipes sous 4 matchs · 3 matchs sans prix · 2 équipes non reconnues »)."""
    s = lambda n, one, many: f"{n} {one if n <= 1 else many}"      # noqa: E731
    out = f"🔎 {s(info['read'], 'match lu', 'matchs lus')} chez 1xBet sur {info['events']} non commencés (dont {info['cup_read']} de coupe)"
    out += f" · {s(len(info['few']), 'équipe', 'équipes')} sous {C.SEASON_MIN_MATCHES} matchs · {s(info['no_price'], 'match sans prix', 'matchs sans prix')}"
    if info["unknown"]:
        out += f" · {s(len(info['unknown']), 'équipe non reconnue', 'équipes non reconnues')}"
    if info.get("unread"):
        out += f" · {s(info['unread'], 'match non relu', 'matchs non relus')} (au-delà du plafond de {C.SEASON_MAX_MATCHES} matchs)"
    return out


def reprice(c, px, refs):
    """La jambe `c` (déjà choisie le matin) remise aux cotes `px` du moment (copie), ou None si 1xBet ne propose plus CETTE ligne. La proba du modèle (p_model), son poids (ew) et l'avis sharp
    du matin (p_sharp) restent ceux du matin ; seul l'avis de 1xBet (p_x) et le prix changent. `sval` est recalculé : 1 (valeur), 0 (remplissage) ou None (la jambe ne vaut plus ni l'un ni l'autre)."""
    odd = xbet.lookup(px, c["market"], c["name"], c["point"])
    if not odd:
        return None
    p_x = ensemble.x_fair(px, c["market"], c["name"], c["point"], c.get("home"), c.get("away"))
    p_mkt = ensemble.pool_market(c.get("p_sharp"), p_x, C.ENSEMBLE_PRIOR_SHARP)[0]
    out = dict(c, xodds=odd, odds=odd)
    if p_mkt is not None and c.get("p_model") is not None:
        p = ensemble.pool(c["p_model"], p_mkt, c.get("ew", C.SEASON_MODEL_WEIGHT))
        kind, why = verdict(p, odd, p_mkt, c["p_model"])
        out.update(prob=round(p, 4), p_ens=round(p, 4), xev=round(p * odd - 1, 4), sval=None if kind is None else 1 if kind == "value" else 0)
        if kind is not None:
            out["strategy"] = "saison" if kind == "value" else "fill"          # voie `saison` : valeur = « saison », remplissage = « fill » (lane reste « saison »)
        if p_x is not None:
            out.update(p_x=round(p_x, 4), xp=round(p_x, 4))
    else:
        out["sval"] = None
    ref = (refs or {}).get((c["market"], c["name"], None if c["point"] is None else float(c["point"])))
    if ref:
        out["xref"] = ref
    return out


def live_check(ids, now=None, fresh_min=None, fetch=None):
    """Relit EN DIRECT chez 1xBet les matchs `ids` des jambes `saison` du matin (data/today.json['season'], id 1xBet connu : lus directement) et les remet au prix du moment, SANS rien écrire :
    -> (jambes encore proposées, remises au prix [reprice], ids relus, heure « HH:MM »). Les matchs qui commencent dans moins de C.XBET_SOON_MIN min sont laissés de côté. Même contrat
    que xprice.live_check pour _ticket_problems / _check_ticket. `fetch` : injectable. Ne lève jamais (1xBet en panne : aucun id relu)."""
    from datetime import timedelta
    from . import xprice
    now = now or datetime.now(timezone.utc)
    legs = store.load("today.json", {}).get("season") or []
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    sel = [c for c in legs if c["id"] in ids and datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > soon]
    found = {}
    if sel:
        try:
            events, wanted = xprice._events(sel), xprice._wanted(sel)
            kw = dict(deadline=time.monotonic() + C.XBET_SECONDS, wanted=wanted, live=True, fresh_min=fresh_min)
            found = (fetch(events, **kw) if fetch is not None else xbet.fetch_day(events, record=False, **kw)) or {}
        except Exception as e:                      # noqa: BLE001
            print(f"[saison] vérification en direct impossible : {type(e).__name__}: {e}", file=sys.stderr)
            found = {}
    pool = []
    for c in sel:
        f = found.get(c["id"])
        r = reprice(c, f["px"], f.get("refs")) if f and f.get("px") else None
        if r is not None:
            pool.append(r)
    return pool, set(found), now.strftime("%H:%M")


def run(scan, picks, day, now, fetch=None, history=None):
    """Passage du matin de la voie : pronostics, avis sharp, prix 1xBet (le cache du relevé principal sert d'abord), choix des lignes, journal. -> (jambes `saison` jouables, mode) ; ([], mode)
    quand la voie est éteinte ou sans jambe. `fetch` : injectable (tests). Le relevé 1xBet de la voie ne remplace pas le compte rendu du relevé principal (record=False) et tient en
    C.SEASON_SECONDS. Ne lève jamais."""
    m = mode()
    if m != "off":
        remember_events(scan, day)                   # les matchs du jour, pour reconstruire les jambes à la demande (`saison N`)
        refresh_state(now)                           # ce que la voie suit (status) : mesuré à chaque passage du matin, même sans relevé 1xBet
    if m == "off" or not C.XBET_ENABLED or (fetch is None and not C.SOURCES_REFRESH):
        return [], m
    if fetch is None and not month_ok():
        print(f"[saison] plafond mensuel de données 1xBet atteint à {C.SEASON_MONTH_PCT:g} % : pas de relevé pour la voie ce matin", file=sys.stderr)
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
            chosen, ref, extra = price(c, f["px"], f.get("refs"), now, wdata, cdata)
            for d in (chosen, ref, extra):
                if d is not None:
                    d["xgid"] = f["gid"]
                    if f.get("cid"):
                        d["xcid"] = f["cid"]
                    rows.append(d)
            if chosen is not None:
                legs.append(chosen | ({"ghost": 1} if m == "ghost" else {}))
        n = record(rows, day, now)
        why = {}
        for d in rows:
            if d.get("swhy"):
                why[d["swhy"]] = why.get(d["swhy"], 0) + 1
        print(f"[saison] {len(pronos)} pronostic(s), {len(found)} match(s) chez 1xBet, {len(legs)} jambe(s) ({sum(1 for c in legs if c.get('sval'))} à valeur), {n} avis au journal"
              + (f", lignes écartées : {', '.join(f'{k} {v}' for k, v in sorted(why.items()))}" if why else ""), file=sys.stderr)
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
