"""Combinés LIVE (« 🔴 Live », « live 20 ») : à la demande, sur les matchs en cours entre la C.LIVE_MIN_MINUTE et la C.LIVE_MAX_MINUTE minute.

Pas de suivi en continu : un appui = une lecture de la liste live de 1xBet (LiveFeed Get1x2_VZip, via le proxy et le plafond de 1xBet, ~130 Ko
pour 40 matchs, gardée C.LIVE_TTL s) qui donne pour chaque match le score, la minute, les cartons rouges et les cotes live (mêmes types que
l'avant-match : 1N2, double chance, total de buts, les 2 marquent).

Probabilités : seulement les matchs des championnats où Kelly a son modèle de force (strength.market_grid, données de qualité suffisante) ; les
buts attendus d'avant-match de chaque équipe sont ramenés au temps qui reste (part des buts d'un match encore à jouer, 45 % en 1re mi-temps,
55 % en 2e, temps additionnel compris), corrigés des cartons rouges, puis combinés au score du moment (loi de Poisson du reste du match).
Un pari est candidat avec au moins C.LIVE_MIN_P de chances, une espérance d'au moins C.LIVE_MIN_EV à la cote 1xBet, une cote d'au moins
C.LIVE_MIN_ODDS, et jamais plus de C.LIVE_MAX_GAP d'écart avec la probabilité de 1xBet (au-delà, c'est presque toujours une information que le
modèle n'a pas : blessure, domination, changement tactique). Le combiné est le plus probable dont la cote tombe dans la fourchette demandée
(combos.best_combo, un pari par match), avec son code coupon et « ✅ Je le joue » (suivi comme un combi sur mesure, src « live »).

Apprentissage À PART de l'avant-match : chaque pari candidat évalué est noté dans data/live.json avec sa tranche de minutes (C.LIVE_TRANCHES :
30-45, 45-60, 60-75, 75-90+), réglé le lendemain matin comme un pronostic (settle._settle_fair_row) ; la fiabilité est cumulée par famille ET
par tranche dans data/live_calib.json (même formule que kelly/calib.py) et corrige les probabilités live de cette famille dans cette tranche.
Rien n'est mélangé à la calibration d'avant-match. Ne lève jamais vers l'utilisateur : une panne donne un message clair."""
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from . import calib, combos as CB, config as C, fmt, store, strength
from .sources import xbet
from .telegram import Html

JOURNAL = "live.json"           # [{key, id, date, tranche, minute, score, home, away, sport_key, commence, market, name, point, p0, prob, px, xodds, result...}]
CALIB_FILE = "live_calib.json"  # {"<famille>|<tranche>": [n, gagnés, somme des probas brutes]}
KEEP_DAYS = 30
FIRST_HALF, SECOND_HALF = 0.45, 0.55          # part des buts d'un match par mi-temps
HALF_1_LEN, HALF_2_LEN = 47.0, 49.0           # minutes jouées, temps additionnel compris
RED_SELF, RED_OTHER = 0.70, 1.20              # buts attendus d'une équipe réduite à 10 (par carton rouge), et de son adversaire


def _now():
    return datetime.now(timezone.utc)


def tranche(minute):
    """« 60-75 » : la tranche de minutes (C.LIVE_TRANCHES) ; None hors tranches."""
    for lo, hi in C.LIVE_TRANCHES:
        if lo <= minute < hi:
            return f"{lo}-{hi if hi < 200 else '90+'}"
    return None


def remaining(minute, period):
    """Part des buts d'un match encore à marquer à la minute `minute` (période 1 ou 2 ; mi-temps : 45 en période 1)."""
    if period == 1:
        first = FIRST_HALF * max(0.0, HALF_1_LEN - minute) / HALF_1_LEN
        return first + SECOND_HALF
    return SECOND_HALF * max(0.0, 45.0 + HALF_2_LEN - max(minute, 45.0)) / HALF_2_LEN


# ---------- lecture de 1xBet ----------

def feed_url():
    return xbet.BASE + "/service-api/LiveFeed/Get1x2_VZip?" + urlencode(
        {"sports": 1, "count": C.LIVE_COUNT, "lng": "en", "mode": 4, "country": 96, "partner": 286, "getEmpty": "true"})


def fetch(get=None):
    """Matchs de football en cours chez 1xBet -> (liste brute, None) ou ([], raison). `get(url)` : injectable pour les tests."""
    if get is not None:
        data = get(feed_url())
    else:
        ses = xbet._Session()
        if ses.why:
            return [], ses.why
        try:
            with xbet.live_mode():                  # une commande Telegram est « hors ligne » : seul 1xBet est relu, comme pour « combi »
                data = ses.call(xbet._get_json, feed_url(), C.LIVE_TTL)
        except xbet.XbetError as e:
            return [], str(e)
        except Exception as e:                      # noqa: BLE001
            return [], f"{type(e).__name__}: {e}"
        finally:
            ses.close("liste live")
    value = data.get("Value") if isinstance(data, dict) else None
    return (value if isinstance(value, list) else []), None


def _cells(g):
    """Cotes d'un match live : E (principales) + AE[].ME (lignes alternatives), sans doublon (type, ligne)."""
    seen, out = set(), []
    for c in list(g.get("E") or []) + [x for ae in g.get("AE") or [] for x in ae.get("ME") or []]:
        k = (c.get("T"), c.get("P"))
        if c.get("C") and k not in seen and not c.get("B"):
            seen.add(k)
            out.append(c)
    return out


def _stat(sc, sid):
    """Statistique live `sid` (71 : carton rouge) -> (domicile, extérieur)."""
    for block in sc.get("ST") or []:
        for s in block.get("Value") or []:
            if s.get("ID") == sid:
                try:
                    return int(s.get("S1") or 0), int(s.get("S2") or 0)
                except ValueError:
                    return 0, 0
    return 0, 0


def parse(g):
    """Un match live de 1xBet -> {gid, league, home, away, minute, period, score (h, a), reds (h, a), start, odds {(marché, côté, ligne): (cote, type, param)}} ;
    None sans minute ou sans équipes."""
    sc = g.get("SC") or {}
    ts, home, away = sc.get("TS"), g.get("O1E") or g.get("O1"), g.get("O2E") or g.get("O2")
    if ts is None or not home or not away:
        return None
    period = sc.get("CP") or (1 if ts < 45 * 60 else 2)
    fs = sc.get("FS") or {}
    try:
        h, a = int(fs.get("S1") or 0), int(fs.get("S2") or 0)
    except ValueError:
        return None
    odds = {}
    for c in _cells(g):
        t, p, cf = c.get("T"), c.get("P"), float(c["C"])
        if t in xbet.T_1X2:
            odds[("h2h", {"home": home, "draw": "Draw", "away": away}[xbet.T_1X2[t]], None)] = (cf, t, 0)
        elif t in xbet.T_DC:
            name = {"1X": f"{home} or Draw", "12": f"{home} or {away}", "X2": f"Draw or {away}"}[xbet.T_DC[t]]
            odds[("double_chance", name, None)] = (cf, t, 0)
        elif t in (9, 10) and p is not None and float(p) % 1:
            odds[("totals", xbet.T_TOTAL[t][1], float(p))] = (cf, t, float(p))
        elif t in xbet.T_BTTS:
            odds[("btts", xbet.T_BTTS[t], None)] = (cf, t, 0)
    return {"gid": g.get("I"), "league": g.get("LE") or g.get("L") or "", "home": home, "away": away, "minute": int(ts // 60),
            "period": int(period), "score": (h, a), "reds": _stat(sc, 71), "start": g.get("S"), "odds": odds}


def league_key(name):
    """Clé Kelly du championnat 1xBet `name` (nom anglais de xbet.LEAGUES : The Odds API et championnats 1xBet suivis) ; None sinon."""
    return {v.lower(): key for key, v in xbet.LEAGUES.items()}.get(str(name).lower())


def model(m, key):
    """Buts attendus d'avant-match du match live `m` -> {"pred": {lh, la, q, n, src}} ou None, de la source la plus solide à la plus légère :
    1. le modèle de force (strength.market_grid) des championnats football-data ;
    2. les profils FotMob / API-Football / 365scores des autres championnats suivis (features.match_features : exp_goals_home / away), qualité
       au plus 0,6, avis mélangé à celui de 1xBet (C.LIVE_W_PROFILES) ;
    3. les équipes préparées à l'avance sur 365scores (livedata.pred_for), qualité au plus 0,5, avis mélangé à moitié avec 1xBet (C.LIVE_W_LIGHT),
       jamais en coupe ni en amical (deux divisions différentes), hors jeunes / réserves / régional.
    Aucune requête (caches seulement)."""
    from . import livedata
    from .sources import features, footballdata as fd
    if key in fd.SPORT_TO_DIV:
        got = strength.market_grid(m["home"], m["away"], key)
        if got and got.get("pred"):
            return {"pred": got["pred"] | {"src": "modèle"}}
    if livedata.excluded(m["league"], m["home"], m["away"]):
        return None
    if key:
        f = features.match_features(m["home"], m["away"], key) or {}
        lh, la, n = f.get("exp_goals_home"), f.get("exp_goals_away"), [f.get("n_home") or 0, f.get("n_away") or 0]
        if lh and la:
            return {"pred": {"lh": lh, "la": la, "n": n, "q": round(0.6 * min(1.0, min(n) / 10), 3), "src": "profils"}}
    if livedata.cup(m["league"]):                  # coupe / amical : des résultats de championnats différents ne se comparent pas
        return None
    pred = livedata.pred_for(m["home"], m["away"], start=m.get("start"))
    return {"pred": pred} if pred else None


# ---------- probabilités ----------

def _acc(data, fam, tr):
    return (data or {}).get(f"{fam}|{tr}")


LIGHT = ("profils", "365scores")      # sources dont la fiabilité est aussi mesurée à part (« src|365scores »)


def factor(market, tr, data=None, src=None):
    """Correction live de la famille du marché dans la tranche `tr` (même formule que calib._k, cumul À PART), multipliée par celle de la
    SOURCE des données (profils, données légères) dès qu'elle a assez de résultats ; 1 sans assez de résultats."""
    data = data if data is not None else store.load(CALIB_FILE, {})
    k = calib._k(_acc(data, calib.family(market), tr))
    k = 1.0 if k is None else k
    ks = calib._k((data or {}).get(f"src|{src}")) if src in LIGHT else None
    return max(calib.K_RANGE[0], min(calib.K_RANGE[1], k * (ks or 1.0)))


def probs(m, pred, data=None):
    """Probabilités brutes {(marché, côté, ligne): p} des paris de buts du match live `m` d'après le modèle `pred` (strength.predict)."""
    rem = remaining(m["minute"], m["period"])
    rh, ra = m["reds"]
    lh = pred["lh"] * rem * (RED_SELF ** rh) * (RED_OTHER ** ra)
    la = pred["la"] * rem * (RED_SELF ** ra) * (RED_OTHER ** rh)
    mat = strength.score_matrix(lh, la, rho=0.0, cap=10)
    h0, a0 = m["score"]
    ph = pd = pboth = 0.0
    tot = [0.0] * 30
    for i, row in enumerate(mat):
        for j, x in enumerate(row):
            fh, fa = h0 + i, a0 + j
            ph += x * (fh > fa)
            pd += x * (fh == fa)
            pboth += x * (fh > 0 and fa > 0)
            if i + j < len(tot):
                tot[i + j] += x
    pa = 1 - ph - pd
    home, away = m["home"], m["away"]
    out = {("h2h", home, None): ph, ("h2h", "Draw", None): pd, ("h2h", away, None): pa,
           ("double_chance", f"{home} or Draw", None): ph + pd, ("double_chance", f"{home} or {away}", None): ph + pa,
           ("double_chance", f"Draw or {away}", None): pd + pa, ("btts", "Yes", None): pboth, ("btts", "No", None): 1 - pboth}
    for (mk, side, point) in m["odds"]:
        if mk == "totals":
            need = point - (h0 + a0)                       # buts encore nécessaires au-dessus de la ligne
            over = sum(x for k, x in enumerate(tot) if k > need)
            out[("totals", "Over", point)], out[("totals", "Under", point)] = over, 1 - over
    return out


def market_probs(m):
    """Probabilités de 1xBet sans marge : par groupe (1N2, plus / moins d'une même ligne, les 2 marquent) ; double chance tirée du 1N2."""
    o = m["odds"]
    out = {}

    def norm(keys):
        inv = {k: 1 / o[k][0] for k in keys if k in o}
        s = sum(inv.values())
        if len(inv) == len(keys) and s:
            out.update({k: v / s for k, v in inv.items()})
    norm([("h2h", m["home"], None), ("h2h", "Draw", None), ("h2h", m["away"], None)])
    norm([("btts", "Yes", None), ("btts", "No", None)])
    for (mk, side, point) in list(o):
        if mk == "totals" and side == "Over":
            norm([("totals", "Over", point), ("totals", "Under", point)])
    hh, dd, aa = (out.get(("h2h", m["home"], None)), out.get(("h2h", "Draw", None)), out.get(("h2h", m["away"], None)))
    if None not in (hh, dd, aa):
        out[("double_chance", f"{m['home']} or Draw", None)] = hh + dd
        out[("double_chance", f"{m['home']} or {m['away']}", None)] = hh + aa
        out[("double_chance", f"Draw or {m['away']}", None)] = dd + aa
    return out


def legs(raw, now=None, data=None, model_for=None):
    """Paris live évaluables -> (candidats retenus, tous les paris évalués pour le journal, compte {live, fenêtre, connus}). `model_for(m, key)` :
    injectable (tests) ; par défaut strength.market_grid."""
    now = now or _now()
    data = data if data is not None else store.load(CALIB_FILE, {})
    keep, seen, count = [], [], {"live": 0, "window": 0, "known": 0}
    for g in raw:
        m = parse(g)
        if not m:
            continue
        count["live"] += 1
        if not (C.LIVE_MIN_MINUTE <= m["minute"] <= C.LIVE_MAX_MINUTE):
            continue
        count["window"] += 1
        key = league_key(m["league"])
        try:
            got = model_for(m, key) if model_for else model(m, key)
        except Exception:                           # noqa: BLE001 - un match illisible n'empêche pas les autres
            got = None
        pred = (got or {}).get("pred")
        if not pred or pred.get("q", 0) < C.VIABLE_MIN_Q or min(pred.get("n") or [0]) < C.VIABLE_MIN_N:
            continue
        count["known"] += 1
        tr = tranche(m["minute"])
        p_raw, p_x = probs(m, pred), market_probs(m)
        start = datetime.fromtimestamp(m["start"], tz=timezone.utc) if m.get("start") else now
        for k, (cf, t, param) in m["odds"].items():
            if k not in p_raw:
                continue
            mk, side, point = k
            p0 = p_raw[k]
            w = {"profils": C.LIVE_W_PROFILES, "365scores": C.LIVE_W_LIGHT}.get(pred.get("src"), 1.0)
            if w < 1 and k in p_x:                  # données moins solides : l'avis de Kelly mélangé à celui du marché (comme l'ensemble d'avant-match)
                p0 = w * p0 + (1 - w) * p_x[k]
            p = 0.5 + (p0 - 0.5) * factor(mk, tr, data, pred.get("src"))
            leg = {"key": f"lv{m['gid']}|{mk}||{'' if point is None else point}|{side}", "id": f"lv{m['gid']}", "home": m["home"], "away": m["away"],
                   "sport_key": key or "soccer_live_other", "league": m["league"], "commence": start.isoformat().replace("+00:00", "Z"), "start": start.strftime("%H:%M"),
                   "market": mk, "name": side, "point": point, "who": None, "group": "soccer", "p0": round(p0, 4), "prob": round(p, 4),
                   "px": round(p_x[k], 4) if k in p_x else None, "odds": cf, "xodds": cf, "xref": [m["gid"], t, param], "xgid": m["gid"],
                   "min_odds": round(1 / max(p, 1e-6) * (1 + C.FAIR_MARGIN), 2), "pick": fmt.label(mk, side, point, None, "soccer"),
                   "minute": m["minute"], "score": f"{m['score'][0]}-{m['score'][1]}", "tranche": tr, "live": True, "conf": pred.get("q"),
                   "src": pred.get("src") or "modèle"}
            if 0.03 < p < 0.97:
                seen.append(leg)
            gap = abs(p - leg["px"]) if leg["px"] is not None else 1.0
            if (p >= C.LIVE_MIN_P and cf >= C.LIVE_MIN_ODDS and p * cf - 1 >= C.LIVE_MIN_EV and gap <= C.LIVE_MAX_GAP):
                keep.append(leg)
    return keep, seen, count


# ---------- journal et apprentissage à part ----------

def record(rows, now=None):
    """Note les paris live évalués (un par match, marché, ligne, côté et tranche, une fois par jour) pour l'apprentissage live. -> nombre ajouté."""
    now = now or _now()
    day = str(now.date())
    log = store.load(JOURNAL, [])
    log = log if isinstance(log, list) else []
    have = {(r.get("date"), r.get("key"), r.get("tranche")) for r in log}
    cut = str((now - timedelta(days=KEEP_DAYS)).date())
    log = [r for r in log if str(r.get("date")) >= cut or r.get("result") is None]
    n = 0
    for r in rows:
        k = (day, r["key"], r["tranche"])
        if k in have:
            continue
        have.add(k)
        log.append({x: r.get(x) for x in ("key", "id", "tranche", "minute", "score", "home", "away", "sport_key", "commence", "market", "name",
                                          "point", "who", "group", "p0", "prob", "px", "xodds", "src")} | {"date": day})
        n += 1
    if n:
        store.save(JOURNAL, log, compact=True)
    return n


def settle(now=None, fdata=None, stats_for=None):
    """Règle le journal live (comme un pronostic : score final) et cumule la fiabilité par famille et par tranche dans live_calib.json, à part de
    l'avant-match. -> (réglés, appris)."""
    from . import settle as S
    now = now or _now()
    log = store.load(JOURNAL, [])
    if not isinstance(log, list) or not log:
        return 0, 0
    fdata = fdata or S.FootballData()
    data = store.load(CALIB_FILE, {})
    settled = learned = 0
    for r in log:
        if r.get("result") is None:
            S._settle_fair_row(r, now, fdata, stats_for)
            settled += r.get("result") is not None
        if r.get("result") in ("win", "loss") and not r.get("calib"):
            keys = [f"{calib.family(r['market'])}|{r['tranche']}"] + ([f"src|{r['src']}"] if r.get("src") in LIGHT else [])
            for k in keys:                          # par famille et tranche, et par source de données (profils, données légères)
                acc = data.setdefault(k, [0, 0, 0.0])
                acc[0] += 1
                acc[1] += r["result"] == "win"
                acc[2] = round(acc[2] + (r.get("p0") or r["prob"]), 4)
            r["calib"] = True
            learned += 1
    store.save(JOURNAL, log, compact=True)
    if learned:
        store.save(CALIB_FILE, data, compact=True)
    return settled, learned


def calib_lines(data=None):
    """Fiabilité live par famille et tranche (bilan détaillé, status) : « buts 60-75 : 34 réglés, 71 % gagnés pour 66 % annoncés »."""
    data = data if data is not None else store.load(CALIB_FILE, {})
    out = []
    for k in sorted(data):
        n, w, s = data[k]
        fam, tr = k.split("|")
        what = ({"profils": "données moyennes", "365scores": "données légères"}.get(tr, tr) if fam == "src" else f"{calib.LABELS.get(fam, fam)} {tr}")
        out.append(f"{what} : {n} réglé{'s' if n > 1 else ''}, {w / n:.0%} gagnés pour {s / n:.0%} annoncés")
    return out


# ---------- le combiné ----------

def _hms():
    return _now().strftime("%H:%M:%S")


def combo(target, day, get=None, model_for=None, now=None, nlegs=None):
    """Réponse à « live <cote> » : le combiné live le plus probable dans [cote ; cote x (1 + C.CUSTOM_TOLERANCE)], ou ce qui manque."""
    from . import app, custom, money
    t = fmt.trim(target, 1)
    raw, why = fetch(get)
    if why:
        return f"🔴 1xBet live injoignable en ce moment ({why}). Réessaie dans quelques minutes."
    cands, seen, count = legs(raw, now=now, model_for=model_for)
    try:
        record(seen, now)
    except Exception as e:                          # noqa: BLE001 - le journal ne bloque jamais la réponse
        print(f"[live] journal non écrit : {type(e).__name__}: {e}", file=sys.stderr)
    lo, hi = target, target * (1 + C.CUSTOM_TOLERANCE)
    from . import chat
    n_max, exact = nlegs or (None, False)
    mx, mn = chat.legs_bounds(n_max, exact)
    best = CB.best_combo(cands, lo, hi, max_legs=mx, min_legs=mn)
    win = f"entre {C.LIVE_MIN_MINUTE}e et {C.LIVE_MAX_MINUTE}e"
    if not best:
        per = {}
        for c in cands:
            per[c["id"]] = max(per.get(c["id"], 1.0), c["odds"])
        top = sorted(per.values(), reverse=True)[:mx or None]
        reach = math.prod(top) if len(top) >= 2 else None
        lab = " " + chat.legs_label(n_max, exact) if n_max else ""
        return (f"🔴 Pas de combiné live à la cote {t}{lab}.\n"
                f"📡 {count['live']} matchs en cours · {count['window']} {win} · {count['known']} que Kelly connaît\n"
                + (f"📉 {len(per)} avec un bon pari : au plus {fmt.trim(reach, 1)}\n" if reach else f"📉 {len(per)} avec un bon pari\n")
                + fmt.i("🕒 Plus de choix le week-end après-midi et le soir."))
    legs_, total, prob = best
    ev = total * prob
    stake = app._budget_stake(prob, total) if money.active() else None
    code = None
    try:
        code = xbet.save_coupon(legs_)
    except Exception as e:                          # noqa: BLE001
        print(f"[live] code coupon impossible : {type(e).__name__}: {e}", file=sys.stderr)
    lines = [fmt.b(f"🔴 COMBINÉ LIVE — cote {fmt.odds_txt(total)}"),
             f"🎯 {len(legs_)} paris · {fmt.pct(prob)} de chances · {fmt.expect(prob, total)}",
             (f"💰 Mise : {fmt.b(fmt.esc(app._stake_words(stake)))}" if stake is not None else fmt.no_stake_line()), ""]
    for i, c in enumerate(legs_, 1):
        light = {"profils": " ⚠️ données moyennes", "365scores": " ⚠️ données légères"}.get(c.get("src"), "")
        lines += [fmt.b(f"{i}. {fmt.esc(c['home'] + ' – ' + c['away'])}") + f" · {c['score']}, {c['minute']}e" + fmt.i(fmt.esc(light)),
                  f"   {fmt.esc(c['pick'])} · {fmt.b(fmt.odds_txt(c['odds']))}",
                  "   " + fmt.i(f"Kelly {fmt.pct(c['prob'])}" + (f" · 1xBet {fmt.pct(c['px'])}" if c.get("px") is not None else ""))]
    lines.append("")
    lines.append(f"🎟️ Code : {fmt.code(code)}" if code else fmt.i("🎟️ Code indisponible : ajoute ces paris à la main."))
    lines.append(fmt.i(f"⚡ Cotes de {_hms()} : elles bougent vite, valide tout de suite."
                       + (" Espérance négative : pari au prix du marché." if ev < 1 else "")))
    msg = Html("\n".join(lines))
    cid = custom.remember(day, target, lo, hi, best, stake, money.cur() if stake is not None else None, code, _now().strftime("%H:%M"), src="live",
                          nlegs=nlegs)
    return custom.with_button(msg, cid, day, code, another=False)
