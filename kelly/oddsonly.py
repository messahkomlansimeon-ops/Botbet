"""Voie « cotes » : des probabilités déduites des seules cotes 1xBet, sans aucune statistique d'équipe ni modèle de force. FANTÔME : calculée et journalisée (oddsonly_log.json), jamais choisie ;
aucune jambe de combiné, aucune mise, aucun code coupon, aucun apprentissage, aucune relecture 1xBet en plus. Règles pré-déclarées dans oddsonly_ledger.json (AVANT ce code).

Mécanismes (fonctions pures, partie haute du module) :
  1a  1N2 <-> double chance (identité exacte, deux sens)
  1b  totaux d'équipe -> total du match et les 2 marquent (deux Poisson indépendantes)
  1c  lignes successives d'un même marché : une loi binomiale négative ajustée SANS la ligne testée (leave-one-out)
  2   retard sur les autres books (The Odds API) : buts (Pinnacle / sharp) et corners (consensus d'au moins 2 books)
  3   mouvement (observation seulement, jamais une valeur)
Partie basse : collecte (observe), journal, durée de vie des incohérences."""
import math
import sys

from . import config as C, ensemble, lanes, strength

EDGE = C.ODDSONLY_EDGE
MAX_EDGE = C.ODDSONLY_MAX_EDGE
MOVE_MIN = 0.03                                   # mécanisme 3 : variation (log) de la cote 1xBet journalisée au moins
MIN_PAIRS = 4                                     # 1c : paires en .5 minimales pour tester une ligne (3 pour ajuster)
MIN_TEAM_PAIRS = 2                                # 1b : paires en .5 minimales par équipe
MULTS = (0.5, 1.0, 2.0)                           # 1c : dispersion r essayée = multiple x la dispersion de repli de la famille
FALLBACK_R = {"goals": 100.0, "team_goals": 100.0, "corners": 60.0, "cards": 25.0, "yellow": 25.0, "shots": 40.0, "sot": 25.0, "fouls": 100.0}     # FIGÉES, non apprises (ledger)
MECHS = ("1a", "1b", "1c", "2", "3")
GOAL_FAMILIES = frozenset(("goals", "team_goals", "btts", "dc", "h2h"))
LADDER_MARKETS_EXCLUDED = frozenset(("btts", "h2h", "double_chance"))


def mode():
    """« ghost » ou « off » : « on » est refusé dans ce lot (la voie ne joue rien, ledger)."""
    return "off" if C.STRATEGY_ODDSONLY == "off" else "ghost"


# ---------- démarginage ----------

def pair_prob(px, market, point):
    """Proba (sans marge, Shin) du « Over » / « Yes » d'une paire à deux issues d'une MÊME ligne en .5 ; None si une cote manque, si la ligne est entière ou si l'overround sort de la plage."""
    if point is None or abs(point * 2 - round(point * 2)) > 1e-9 or abs(point - round(point)) < 1e-9:
        return None
    a, b = px.get((market, "Over", point)), px.get((market, "Under", point))
    ps = ensemble.devig([a, b])
    return ps[0] if ps else None


# ---------- lois ----------

def _over_nb(mu, r, point):
    """P(X > point) d'une binomiale négative de moyenne `mu` et de taille `r` (variance mu + mu^2 / r) ; r = None : Poisson."""
    k = int(math.floor(point))
    pmf = strength.nb_pmf(mu, alpha=0.0 if r is None else 1.0 / r, cap=max(k + 2, int(mu + 10 * math.sqrt(mu + (0 if r is None else mu * mu / r))) + 6))
    return max(0.0, min(1.0, 1.0 - sum(pmf[: k + 1])))


def _sse(mu, r, pairs):
    kmax = int(math.floor(max(L for L, _ in pairs)))
    pmf = strength.nb_pmf(mu, alpha=0.0 if r is None else 1.0 / r, cap=kmax + 2)       # une seule loi pour toutes les lignes
    cum, tot = [], 0.0
    for v in pmf[: kmax + 1]:
        tot += v
        cum.append(tot)
    return sum((max(0.0, min(1.0, 1.0 - cum[int(math.floor(L))])) - p) ** 2 for L, p in pairs)


def fit_mu(pairs, r):
    """Moyenne mu qui minimise l'écart quadratique entre P(Over) de la loi (taille r ; None = Poisson) et les probabilités démarginées `pairs` [(ligne, p_over)] ; (mu, écart) ou (None, None)."""
    if not pairs:
        return None, None
    top = max(L for L, _ in pairs) * 2.5 + 3
    grid = [0.2 * (top / 0.2) ** (i / 39) for i in range(40)]
    best = min(grid, key=lambda m: _sse(m, r, pairs))
    lo, hi = best / (top / 0.2) ** (1 / 39), min(top, best * (top / 0.2) ** (1 / 39))
    for _ in range(14):                                          # raffinement par sections d'or
        a, b = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
        if _sse(a, r, pairs) < _sse(b, r, pairs):
            hi = b
        else:
            lo = a
    mu = (lo + hi) / 2
    return mu, _sse(mu, r, pairs)


def fit_nb(pairs, family):
    """(mu, r) retenus : au moins 3 paires -> r parmi MULTS x dispersion de repli de la famille, au moindre écart ; moins -> la dispersion de repli seule."""
    base = FALLBACK_R.get(family, 40.0)
    if len(pairs) < 3:
        mu, _ = fit_mu(pairs, base)
        return mu, base
    best = None
    for m in MULTS:
        mu, e = fit_mu(pairs, base * m)
        if mu is not None and (best is None or e < best[2]):
            best = (mu, base * m, e)
    return (best[0], best[1]) if best else (None, None)


# ---------- mécanismes ----------

def _row(mech, market, name, point, odds, p, **extra):
    return {"mech": mech, "market": market, "name": name, "point": point, "family": ensemble.family(market), "xodds": odds, "p_impl": round(p, 5),
            "edge": round(p * odds - 1, 5)} | extra


def mech_1a(px, home, away):
    """1N2 <-> double chance : toutes les sélections évaluables, avec la proba implicite déduite de l'AUTRE marché. []  si l'un des deux marchés n'est pas complet ou sort de l'overround."""
    names = {"1": home, "X": "Draw", "2": away}
    h = [px.get(("h2h", names[k], None)) for k in ("1", "X", "2")]
    dcn = {"1X": f"{home} or Draw", "12": f"{home} or {away}", "X2": f"Draw or {away}"}
    d = [px.get(("double_chance", dcn[k], None)) for k in ("1X", "12", "X2")]
    out = []
    ph = ensemble.devig(h)
    pd = ensemble.devig_mass(d)
    if ph and all(h):
        p1, px_, p2 = ph
        for k, p in (("1X", p1 + px_), ("12", p1 + p2), ("X2", px_ + p2)):
            o = px.get(("double_chance", dcn[k], None))
            if o:
                out.append(_row("1a", "double_chance", dcn[k], None, o, p, sens="1N2->DC"))
    if pd and all(d):
        a, b, c = pd
        for k, p in (("1", a + b - 1), ("X", a + c - 1), ("2", b + c - 1)):
            o = px.get(("h2h", names[k], None))
            if o and 0 < p < 1:
                out.append(_row("1a", "h2h", names[k], None, o, p, sens="DC->1N2"))
    return out


def _team_pairs(px, market):
    pts = sorted({k[2] for k in px if k[0] == market and k[1] == "Over" and k[2] is not None})
    return [(L, p) for L in pts for p in [pair_prob(px, market, L)] if p is not None]


def mech_1b(px):
    """Totaux d'équipe (buts) -> total du match et les 2 marquent, par deux Poisson indépendantes. [] sans au moins MIN_TEAM_PAIRS paires par équipe."""
    ph, pa = _team_pairs(px, "team_totals_home"), _team_pairs(px, "team_totals_away")
    if len(ph) < MIN_TEAM_PAIRS or len(pa) < MIN_TEAM_PAIRS:
        return []
    lh, _ = fit_mu(ph, None)
    la, _ = fit_mu(pa, None)
    if lh is None or la is None:
        return []
    out = []
    for (market, name, point), o in px.items():
        if market == "totals" and name in ("Over", "Under") and point is not None and abs(point - round(point)) > 1e-9 and o:
            over = _over_nb(lh + la, None, point)
            out.append(_row("1b", market, name, point, o, over if name == "Over" else 1 - over, lam=[round(lh, 3), round(la, 3)]))
        elif market == "btts" and name in ("Yes", "No") and o:
            yes = (1 - math.exp(-lh)) * (1 - math.exp(-la))
            out.append(_row("1b", market, name, None, o, yes if name == "Yes" else 1 - yes, lam=[round(lh, 3), round(la, 3)]))
    return out


def mech_1c(px):
    """Lignes successives d'un même marché : p implicite d'une ligne = loi binomiale négative ajustée SANS elle (leave-one-out). Familles de voie seulement."""
    out = []
    for market in sorted({k[0] for k in px} - LADDER_MARKETS_EXCLUDED):
        if not lanes.ticket_ok({"market": market}):
            continue
        fam = ensemble.family(market)
        pairs = _team_pairs(px, market)
        if len(pairs) < MIN_PAIRS:
            continue
        for L, p_obs in pairs:
            mu, r = fit_nb([q for q in pairs if q[0] != L], fam)
            if mu is None:
                continue
            over = _over_nb(mu, r, L)
            for name, p in (("Over", over), ("Under", 1 - over)):
                o = px.get((market, name, L))
                if o:
                    out.append(_row("1c", market, name, L, o, p, nb_mu=round(mu, 3), nb_r=round(r, 1), n_lines=len(pairs) - 1))
    return out


SYNC_MIN = 10                                     # mécanisme 2 / 3 : les probas des books ne peuvent pas être plus récentes que la lecture 1xBet de plus de SYNC_MIN minutes (sinon le « retard » serait déjà comblé)


def books_age_min(c, when):
    """Minutes écoulées entre le relevé des books du pari `c` (c["sharp_at"]) et l'instant `when` (SIGNÉ : négatif = les books sont plus récents que `when`) ; None si inconnu."""
    from . import sharpgap
    t = sharpgap._dt(c.get("sharp_at")) if c.get("sharp_at") else None
    return None if t is None else (when - t).total_seconds() / 60


def mech_2(px, picks, now, history=None):
    """Retard sur les autres books : `picks` = paris réels du MÊME match (The Odds API). Buts : proba d'un book de référence (ensemble.sharp_of) ; corners : consensus d'au moins 2 books.
    Âge du relevé des books connu, <= ENSEMBLE_SHARP_MAX_AGE_MIN et pas plus récent que la lecture 1xBet (`now`) de plus de SYNC_MIN minutes."""
    from .sources import xbet
    out = []
    for c in picks or ():
        fam = ensemble.family(c.get("market") or "")
        if not lanes.ticket_ok(c):
            continue
        if fam in GOAL_FAMILIES:
            sh = ensemble.sharp_of(c)
            p = sh[0] if sh else None
        elif fam == "corners" and c.get("market") == "alternate_totals_corners" and (c.get("n_books") or 0) >= 2 and c.get("prob_book") is not None:
            p = c["prob_book"]
        else:
            continue
        age = books_age_min(c, now)
        if p is None or age is None or age > C.ENSEMBLE_SHARP_MAX_AGE_MIN or age < -SYNC_MIN:
            continue
        k = xbet.resolve(px, c["market"], c["name"], c.get("point"))
        o = px.get(k) if k else None
        if o and o > 1:
            out.append(_row("2", k[0], k[1], k[2], o, p, overlap_sharpgap=fam in GOAL_FAMILIES, age_min=round(age)))
    return out


def evaluate(px, home, away, picks=None, now=None, history=None):
    """Toutes les sélections évaluables d'un relevé (mécanismes 1a, 1b, 1c, 2), écart compris, quel que soit son niveau : [dict]. Aucune famille hors voies. Ne lève jamais."""
    out = []
    for fn, args in ((mech_1a, (px, home, away)), (mech_1b, (px,)), (mech_1c, (px,)), (mech_2, (px, picks, now, history))):
        try:
            out += fn(*args)
        except Exception as e:                  # noqa: BLE001 - un mécanisme en échec ne casse ni les autres ni le relevé
            print(f"[cotes] mécanisme {fn.__name__} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return [r for r in out if lanes.ticket_ok({"market": r["market"]}) and r["xodds"] and r["xodds"] > 1]


def status_of(edge):
    """« value » (EDGE <= écart <= MAX_EDGE), « suspect » (> MAX_EDGE), sinon None."""
    if edge is None:
        return None
    return "suspect" if edge > MAX_EDGE else "value" if edge >= EDGE else None


def movement(px_now, px_prev, ref_now=None, ref_prev=None):
    """Mécanisme 3 (observation) : {(marché, côté, ligne): {"mv": log(cote / cote précédente), "mv_ref": log(p / p précédente) ou None}} pour les sélections dont |mv| >= MOVE_MIN."""
    out = {}
    for k, o in px_now.items():
        old = px_prev.get(k)
        if not o or not old or o <= 1 or old <= 1:
            continue
        mv = math.log(o / old)
        if abs(mv) < MOVE_MIN:
            continue
        a, b = (ref_now or {}).get(k), (ref_prev or {}).get(k)
        out[k] = {"mv": round(mv, 4), "mv_ref": round(math.log(a / b), 4) if a and b else None}
    return out


# ---------- collecte : les relevés 1xBet déjà faits ----------

LOG_FILE = "oddsonly_log.json"
STATE_FILE = "oddsonly_state.json"
COVER_FILE = "oddsonly_cover.json"                # couverture par jour : matchs lus / calculables, sélections évaluées par mécanisme et famille
ARCH_DAYS = 90                                    # lignes de plus de 90 jours retirées du journal (le règlement les archive avant : settle_log)
LEAD_OK_MIN = 90                                  # xclose : relevé à 0-90 minutes du coup d'envoi (comme lanestats.x_clv)
STATE_DAYS = 2                                    # un instantané de match plus vieux que son coup d'envoi + 2 jours est oublié
MV_TOP = 5                                        # mécanisme 3 : au plus 5 mouvements journalisés par match et par relevé (les plus forts) ...
MV_MAX = 20                                       # ... et 20 lignes de mouvement par match


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes")


def _ko_ts(commence):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(commence).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def sel_id(market, name, point):
    return f"{market}|{name}|{'' if point is None else point}"


def row_key(date, mid, mech, market, name, point):
    return f"{date}|{mid}|{mech}|{sel_id(market, name, point)}"


def read_time(found_match):
    """Heure (timestamp) de la lecture 1xBet RÉELLE d'un match : la plus ancienne des heures de cache de ses GameId (xbet.odds_time) ; None si le cache n'en dit rien (le match est alors SAUTÉ : jamais de
    lecture fabriquée). Un résultat servi par le cache à la même heure n'est pas une nouvelle lecture."""
    from .sources import xbet
    gids = {found_match.get("gid")} | {r[0] for r in (found_match.get("refs") or {}).values() if isinstance(r, (list, tuple)) and r}
    return xbet.odds_time(sorted(g for g in gids if g))


def _picks_by_id():
    from . import store
    t = store.load("today.json", {})
    out = {}
    for c in (t.get("picks") or []) if isinstance(t, dict) else []:
        if isinstance(c, dict) and c.get("id"):
            out.setdefault(c["id"], []).append(c)
    return out


def _ref_probs(picks, when):
    """{sel_id: prob_book} des paris réels dont le relevé des books est connu et pas plus récent que `when` de plus de SYNC_MIN minutes (mécanisme 3)."""
    out = {}
    for c in picks or ():
        age = books_age_min(c, when)
        if c.get("prob_book") and age is not None and age >= -SYNC_MIN:
            out[sel_id(c["market"], c["name"], c.get("point"))] = c["prob_book"]
    return out


def _new_row(r, mid, ev, sport_key, date, at):
    return {"key": row_key(date, mid, r["mech"], r["market"], r["name"], r["point"]), "id": mid, "home": ev.get("home_team"), "away": ev.get("away_team"),
            "sport_key": sport_key, "commence": ev.get("commence_time"), "date": date, "market": r["market"], "name": r["name"], "point": r["point"],
            "family": r["family"], "mech": r["mech"], "status": r.get("status"), "xodds": r["xodds"], "xref": r.get("xref"), "p_impl": r["p_impl"], "edge": r["edge"],
            "edge_last": r["edge"], "pe": r["p_impl"], "went_suspect": False, "first_at": at, "last_seen_at": at, "gone_at": None, "reads": 1, "censored": None, "xclose": None, "xgone": False,
            "result": None, "strategy": "cotes"} | {k: r[k] for k in ("overlap_sharpgap", "sens", "nb_mu", "nb_r", "n_lines", "lam", "age_min") if k in r}


def censor_past(log, now_ts):
    """Lignes encore ouvertes (ni disparues ni censurées) dont le coup d'envoi est passé : censurées « coup d'envoi » (mécanisme 3 exclu : pas de survie)."""
    for row in log:
        if isinstance(row, dict) and row.get("mech") != "3" and row.get("censored") is None and row.get("gone_at") is None and (_ko_ts(row.get("commence")) or 1e18) <= now_ts:
            row["censored"] = "coup d'envoi"


def observe(found, events, now=None):
    """Appelée après chaque xbet.fetch_day : pour chaque match relu (lecture AUTHENTIQUE, avant le coup d'envoi), évalue les mécanismes sur ses cotes, journalise les incohérences (valeur / suspect) à leur première
    détection, met à jour la survie des lignes ouvertes, le xclose (0-90 min avant le match) et le mouvement. Aucun appel réseau ; écrit seulement oddsonly_log.json puis oddsonly_state.json (store.update :
    atomique ; l'état n'avance qu'une fois le journal écrit) ; ne lève jamais."""
    import time
    from datetime import datetime, timezone
    from . import store
    try:
        if mode() == "off" or not found:
            return
        now_ts = time.time() if now is None else now.timestamp()
        evmap = {ev["id"]: (sk, ev) for sk, ev in events or () if isinstance(ev, dict) and ev.get("id")}
        state = store.load(STATE_FILE, {})
        prev_ms = (state.get("matches") if isinstance(state, dict) else None) or {}
        work = []                                   # [(mid, sk, ev, m, read_ts, ko)] des matchs dont la lecture est nouvelle
        for mid, m in found.items():
            sk_ev = evmap.get(mid)
            if not sk_ev or not m.get("px"):
                continue
            ko = _ko_ts(sk_ev[1].get("commence_time"))
            rt = read_time(m)
            if rt is None or ko is None or ko <= rt or (prev_ms.get(mid) or {}).get("t") == rt:       # cache muet, match commencé ou lecture déjà vue (cache) : rien
                continue
            work.append((mid, sk_ev[0], sk_ev[1], m, rt, ko))
        if not work:
            _sweep(now_ts)                          # aucune lecture nouvelle : seulement la censure des lignes dont le coup d'envoi est passé (écrit seulement s'il y en a)
            return
        picks = _picks_by_id()
        evals = {mid: evaluate(m["px"], ev.get("home_team"), ev.get("away_team"), picks.get(mid), datetime.fromtimestamp(rt, timezone.utc)) for mid, sk, ev, m, rt, ko in work}
        _write(work, evals, picks, prev_ms, now_ts)
    except Exception as e:                          # noqa: BLE001 - la voie fantôme ne casse jamais un relevé
        print(f"[cotes] observation ignorée : {type(e).__name__}: {e}", file=sys.stderr)


def _sweep(now_ts):
    from . import store
    log = store.load(LOG_FILE, [])
    if not any(isinstance(r, dict) and r.get("mech") != "3" and r.get("censored") is None and r.get("gone_at") is None and (_ko_ts(r.get("commence")) or 1e18) <= now_ts for r in log if isinstance(log, list)):
        return

    def fn(log):
        log = log if isinstance(log, list) else []
        censor_past(log, now_ts)
        return log, None
    store.update(LOG_FILE, fn, [])


def _update_row(row, r, at):
    """Survie d'une ligne ouverte à un relevé authentique où son mécanisme se recalcule (`r`)."""
    row["reads"] += 1
    row["edge_last"] = r["edge"]
    if r["edge"] > MAX_EDGE:
        row["went_suspect"] = True                  # le statut reste celui de la première détection ; une ligne qui devient suspecte est exclue du CLV
    if r["edge"] < EDGE:
        row["gone_at"] = at                         # disparue : l'écart est repassé sous EDGE (last_seen_at = dernier relevé où il y était)
    else:
        row["last_seen_at"] = at


def _close(row, m, sel, at, lead):
    """xclose : dernier prix NON suspendu de la sélection lu à 0-90 min du match ; cellule retirée alors que son marché est lu : xgone ; suspendue : on garde le dernier bon prix."""
    o = m["px"].get(sel)
    if o:
        row["xclose"] = {"odds": o, "read": at, "lead_min": round(lead), "susp": False}
    elif sel in (m.get("blocked") or ()):
        if row["xclose"] is None:
            row["xclose"] = {"odds": row["xodds"], "read": at, "lead_min": round(lead), "susp": True}
    elif any(k[0] == sel[0] for k in m["px"]):
        row["xgone"] = True


def _write(work, evals, picks, prev_ms, now_ts):
    from datetime import datetime, timedelta, timezone
    from . import store

    def upd_log(log):
        log = log if isinstance(log, list) else []
        cut = (datetime.fromtimestamp(now_ts, timezone.utc) - timedelta(days=ARCH_DAYS)).date().isoformat()
        log = [r for r in log if isinstance(r, dict) and (r.get("date") or "9") >= cut]
        idx = {r.get("key"): r for r in log}
        for mid, sk, ev, m, rt, ko in work:
            at = _iso(rt)
            date = datetime.fromtimestamp(ko, timezone.utc).date().isoformat()
            lead = (ko - rt) / 60
            markets = {k[0] for k in m["px"]}
            cur = {}
            for r in evals.get(mid, ()):
                r["status"] = status_of(r["edge"])
                r["xref"] = (m.get("refs") or {}).get((r["market"], r["name"], r["point"]))
                cur[row_key(date, mid, r["mech"], r["market"], r["name"], r["point"])] = r
            for k, r in cur.items():
                row = idx.get(k)
                if row is None and r["status"] in ("value", "suspect"):
                    row = idx[k] = _new_row(r, mid, ev, sk, date, at)
                    log.append(row)
                elif row is not None and row["censored"] is None and row["gone_at"] is None and row["mech"] != "3":
                    _update_row(row, r, at)
            for row in log:
                if row.get("id") != mid or row.get("mech") == "3" or row["date"] != date:
                    continue
                sel = (row["market"], row["name"], row["point"])
                if row["censored"] is None and row["gone_at"] is None and row["key"] not in cur and row["market"] in markets:
                    row["censored"] = "cellule" if sel not in m["px"] else "non_calculable"      # cellule retirée / suspendue, ou mécanisme qui ne se recalcule plus (< 4 paires, books trop vieux...) : censurée, jamais « disparue »
                if 0 <= lead <= LEAD_OK_MIN and not row.get("xgone") and row["market"] in markets:
                    _close(row, m, sel, at, lead)
            # mécanisme 3 : mouvement entre les deux derniers relevés authentiques (observation seulement)
            old = prev_ms.get(mid) or {}
            if old.get("px"):
                when = datetime.fromtimestamp(rt, timezone.utc)
                sels = {sel_id(*k): k for k in m["px"]}
                mv = movement({i: o for i, o in ((i, m["px"][k]) for i, k in sels.items()) if i in old["px"]}, old["px"], _ref_probs(picks.get(mid), when), old.get("ref"))
                have = sum(1 for r in log if r.get("id") == mid and r.get("mech") == "3")
                for sid, v in sorted(mv.items(), key=lambda kv: -abs(kv[1]["mv"]))[:MV_TOP]:
                    market, name, point = sels[sid]
                    k = row_key(date, mid, "3", market, name, point)
                    if k in idx or have >= MV_MAX or not lanes.ticket_ok({"market": market}) or not m["px"].get(sels[sid]):
                        continue
                    have += 1
                    row = idx[k] = {"key": k, "id": mid, "home": ev.get("home_team"), "away": ev.get("away_team"), "sport_key": sk, "commence": ev.get("commence_time"),
                                    "date": date, "market": market, "name": name, "point": point, "family": ensemble.family(market), "mech": "3", "status": "watch",
                                    "xodds": m["px"][sels[sid]], "first_at": at, "last_seen_at": at, "reads": 1, "censored": None, "gone_at": None, "xclose": None,
                                    "xgone": False, "result": None, "strategy": "cotes"} | v
                    log.append(row)
        censor_past(log, now_ts)
        return log, None

    store.update(LOG_FILE, upd_log, [])               # le journal d'abord : si l'écriture échoue, l'état n'avance pas et la lecture sera revue

    def upd_state(state):
        state = state if isinstance(state, dict) else {}
        ms = dict(state.get("matches") or {})
        for mid, sk, ev, m, rt, ko in work:
            old = ms.get(mid) or {}
            if (old.get("t") or 0) > rt:               # un relevé plus récent est déjà là (course entre processus) : on ne recule pas
                continue
            when = datetime.fromtimestamp(rt, timezone.utc)
            ms[mid] = {"t": rt, "px": {sel_id(*k): o for k, o in m["px"].items()}, "ref": _ref_probs(picks.get(mid), when), "ko": ko}
        return {"matches": {k: v for k, v in ms.items() if (v.get("ko") or 0) + STATE_DAYS * 86400 > now_ts}}, None

    store.update(STATE_FILE, upd_state, {})

    def upd_cover(doc):
        """Couverture par jour : matchs lus, matchs calculables (au moins un marché évalué par 1a, 1b, 1c ou 2), sélections évaluées par mécanisme et famille (dénominateurs du rapport)."""
        doc = doc if isinstance(doc, dict) else {}
        for mid, sk, ev, m, rt, ko in work:
            day = datetime.fromtimestamp(ko, timezone.utc).date().isoformat()
            d = doc.setdefault(day, {"read": [], "comp": [], "ev": {}})
            if mid not in d["read"]:
                d["read"].append(mid)
            evs = [r for r in evals.get(mid, ()) if r["mech"] in ("1a", "1b", "1c", "2")]
            if evs and mid not in d["comp"]:
                d["comp"].append(mid)
            for r in evs:
                k = f"{r['mech']}|{r['family']}"
                d["ev"][k] = d["ev"].get(k, 0) + 1
        keep = sorted(doc)[-(ARCH_DAYS + 3):]
        return {k: doc[k] for k in keep}, None

    store.update(COVER_FILE, upd_cover, {})


# ---------- règlement et mesure ----------

MIN_LIFETIME_N = 20                               # médiane de durée de vie : au moins 20 incohérences disparues
RETURN_N, RETURN_MATCHES = 300, 30                # règle de retour (ledger) : lignes de valeur à CLV mesuré et matchs distincts


def load_rows(days=None, today=None):
    """Lignes du journal de la période (liste vide si illisible)."""
    from datetime import date, timedelta
    from . import store
    log = store.load(LOG_FILE, [])
    log = [r for r in log if isinstance(r, dict)] if isinstance(log, list) else []
    if days:
        cut = str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days))
        log = [r for r in log if (r.get("date") or "") >= cut]
    return log


def value_rows(rows):
    """Lignes de VALEUR au sens du ledger : mécanismes 1 et 2, statut « value » à la première détection, jamais devenues suspectes."""
    return [r for r in rows if r.get("mech") in ("1a", "1b", "1c", "2") and r.get("status") == "value" and not r.get("went_suspect")]


def lifetime(rows):
    """(disparues, censurées, ouvertes, médiane basse, médiane haute) en minutes : intervalle [dernier relevé où l'écart était là ; premier relevé où il a disparu], depuis la première détection. Médianes seulement avec MIN_LIFETIME_N disparues."""
    from datetime import datetime

    def mins(a, b):
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60
    gone = [r for r in rows if r.get("gone_at")]
    lo = sorted(mins(r["first_at"], r["last_seen_at"]) for r in gone)
    hi = sorted(mins(r["first_at"], r["gone_at"]) for r in gone)
    cens = sum(1 for r in rows if r.get("censored") and not r.get("gone_at"))
    opened = sum(1 for r in rows if not r.get("censored") and not r.get("gone_at"))

    def med(v):
        return None if len(v) < MIN_LIFETIME_N else (v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2)
    return len(gone), cens, opened, med(lo), med(hi)


def _pc(x, signed=True):
    return (f"{x * 100:+.1f}" if signed else f"{x * 100:.1f}").replace(".", ",").replace("-", "−") + " %"


def stats_block(rows, cover=None):
    """Chiffres du rapport (dict) : matchs lus / calculables / avec incohérence, lignes par mécanisme, suspects, durée de vie, CLV par mécanisme, réalisé / annoncé."""
    from . import lanestats as LS
    val = value_rows(rows)
    out = {"mech": {}, "suspect": sum(1 for r in rows if r.get("status") == "suspect" or r.get("went_suspect")), "moves": sum(1 for r in rows if r.get("mech") == "3")}
    for m in ("1a", "1b", "1c", "2"):
        mine = [r for r in val if r["mech"] == m]
        done = [r for r in mine if LS.settled(r)]
        n, mean, h, gone = LS.clv(done)
        w = sum(1 for r in done if r["result"] == "win")
        ann = [r["p_impl"] for r in done if r.get("p_impl") is not None]
        out["mech"][m] = {"lines": len(mine), "matches": len({r["id"] for r in mine}), "settled": len(done), "clv_n": n, "clv": mean, "h": h, "gone": gone,
                          "real": w / len(done) if done else None, "ann": sum(ann) / len(ann) if ann else None,
                          "clv_matches": len({r["id"] for r in done if LS.x_clv(r) is not None})}
    out["life"] = lifetime([r for r in rows if r.get("mech") in ("1a", "1b", "1c", "2") and r.get("status") in ("value", "suspect")])
    cov = cover or {}
    out["read"] = sum(len(d.get("read", ())) for d in cov.values())
    out["comp"] = sum(len(d.get("comp", ())) for d in cov.values())
    out["hit"] = len({(r["date"], r["id"]) for r in val})
    out["ev"] = {}
    for d in cov.values():
        for k, n in (d.get("ev") or {}).items():
            out["ev"][k] = out["ev"].get(k, 0) + n
    all_val = [r for r in val if LS.settled(r)]
    n, mean, h, _ = LS.clv(all_val)
    out["all_clv"] = (n, mean, h, len({r["id"] for r in all_val if LS.x_clv(r) is not None}))
    return out


def load_cover(days=None, today=None):
    from datetime import date, timedelta
    from . import store
    doc = store.load(COVER_FILE, {})
    doc = doc if isinstance(doc, dict) else {}
    if days:
        cut = str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days))
        doc = {k: v for k, v in doc.items() if k >= cut}
    return doc


def lane_line(days=None, today=None):
    """Ligne « cotes » du bilan par voie (texte brut) ; '' sans rien de journalisé. Ne lève jamais."""
    try:
        rows = load_rows(days, today)
        if not rows:
            return ""
        b = stats_block(rows, load_cover(days, today))
        tot = sum(v["lines"] for v in b["mech"].values())
        txt = f"cotes (fantôme, aucun pari) : {tot} incohérence{'s' if tot > 1 else ''} ≥ {_pc(EDGE, False)} sur {b['hit']} match{'s' if b['hit'] > 1 else ''}"
        txt += " (" + ", ".join(f"{m} {v['lines']}" for m, v in b["mech"].items() if v["lines"]) + ")" if tot else ""
        n, mean, h, k = b["all_clv"]
        if n:
            txt += f" · prix pris contre le dernier prix 1xBet {_pc(mean)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" sur {n} ({k} matchs)"
        elif tot:
            txt += " · pas encore de CLV (clôture et règlement à venir)"
        g, c, o, lo, hi = b["life"]
        if g:
            txt += f" · disparues {g}, censurées {c}, ouvertes {o}" + (f", durée médiane entre {lo:.0f} et {hi:.0f} min" if lo is not None else "")
        return txt
    except Exception as e:                          # noqa: BLE001
        print(f"[cotes] ligne du bilan illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return ""


INACTIVE2 = {"cards": "1xBet ne cote pas « tous les cartons »", "yellow": "The Odds API ne cote pas les cartons jaunes", "shots": "The Odds API ne cote pas les tirs",
             "sot": "The Odds API ne cote pas les tirs cadrés", "fouls": "The Odds API ne cote pas les fautes"}


def report(args=None, today=None):
    """Commande « incohérences [N] » : le rapport de la voie cotes en texte brut."""
    from . import fmt
    try:
        days = next((int(a) for a in (args or ()) if str(a).isdigit() and len(str(a)) <= 3), None)
        rows = load_rows(days, today)
        cover = load_cover(days, today)
        head = f"🧮 Voie cotes (fantôme : aucune jambe, aucune mise){f' · {days} derniers jours' if days else ''}"
        if not rows and not cover:
            return fmt.Html(fmt.b(fmt.esc(head)) + "\n\nRien collecté pour l'instant : la voie lit les relevés 1xBet déjà faits (matin, 15 h, passages horaires), aucune requête de plus.")
        b = stats_block(rows, cover)
        L = [f"Matchs lus : {b['read']} · calculables : {b['comp']} · avec au moins une incohérence ≥ {_pc(EDGE, False)} non suspecte : {b['hit']}" + (f" ({_pc(b['hit'] / b['comp'], False)})" if b["comp"] else "")]
        L.append(f"Suspectes (écart > {_pc(MAX_EDGE, False)}, jamais une valeur) : {b['suspect']} · mouvements de cote journalisés (observation seulement) : {b['moves']}")
        for m, name in (("1a", "1N2 ↔ double chance"), ("1b", "totaux d'équipe → total / les 2 marquent (Poisson : à part)"), ("1c", "lignes successives"), ("2", "retard sur les autres books")):
            v = b["mech"][m]
            line = f"• {m} {name} : {v['lines']} ligne{'s' if v['lines'] > 1 else ''} sur {v['matches']} match{'s' if v['matches'] > 1 else ''}"
            if v["clv_n"]:
                line += f" · CLV {_pc(v['clv'])}" + (f" ± {_pc(v['h'], False)}" if v["h"] is not None else "") + f" sur {v['clv_n']} ({v['clv_matches']} matchs)"
            elif v["lines"]:
                line += " · pas encore de CLV"
            if v["real"] is not None:
                line += f" · réalisé {_pc(v['real'], False)} pour {_pc(v['ann'], False)} annoncé"
            if m == "2":
                line += " (buts : recouvre sharpgap, preuve non indépendante)"
            L.append(line)
        g, c, o, lo, hi = b["life"]
        L.append(f"Durée de vie : {g} disparue{'s' if g > 1 else ''}, {c} censurée{'s' if c > 1 else ''}, {o} ouverte{'s' if o > 1 else ''}"
                 + (f" · médiane entre {lo:.0f} et {hi:.0f} min (bornes basse et haute)" if lo is not None else f" · pas de médiane avant {MIN_LIFETIME_N} disparues")
                 + " · relevés environ horaires : lire « disparue en moins d'environ une heure » ou « encore là », pas des minutes ; première vue ≠ début réel (censure à gauche)")
        L.append("Mécanisme 2 inactif : " + " ; ".join(f"{f} ({why})" for f, why in INACTIVE2.items()))
        ev = ", ".join(f"{k.replace('|', '/')} {n}" for k, n in sorted(b["ev"].items())) or "rien"
        L.append(f"Sélections évaluées (mécanisme/famille) : {ev}")
        n, mean, h, k = b["all_clv"]
        L.append(f"Règle de retour (nécessaire, jamais suffisante, décision du superviseur) : CLV ≥ 0 borne basse > 0 sur {RETURN_N} lignes et {RETURN_MATCHES} matchs : {n}/{RETURN_N} lignes, {k}/{RETURN_MATCHES} matchs"
                 + (f", CLV {_pc(mean)}" if n else "") + ". Cases mécanisme × famille : aucune correction de comparaisons multiples.")
        return fmt.Html(fmt.b(fmt.esc(head)) + "\n\n" + "\n".join(fmt.esc(x) for x in L))
    except Exception as e:                          # noqa: BLE001
        print(f"[cotes] rapport illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return "Rapport de la voie cotes illisible pour l'instant (voir /erreurs)."
