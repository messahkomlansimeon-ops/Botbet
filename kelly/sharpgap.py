"""Écarts « sharp » : 1xBet paie plus que la cote juste des books de référence.

Pour les matchs et marchés couverts par The Odds API, les paris réels du jour (data/today.json, "picks") portent la proba « vraie » des books de
référence (Pinnacle pesé, marge de Shin retirée : c["prob_book"], c["prob_src"] « pinnacle » ou « sharp », c["n_sharp"], voir odds.consensus) et,
une fois remis au prix 1xBet (xprice.price_picks), la cote 1xBet c["xodds"] et sa référence c["xref"] (de quoi enregistrer un coupon). L'écart est

    edge = cote 1xBet x proba sharp - 1

C'est la seule source de valeur que l'audit n'a pas démentie : 1xBet est un book mou, en retard sur les books de référence ; ce n'est PAS un
avis du modèle de Kelly. Un candidat doit passer ces filtres (chacun compté dans scan()["rejected"]) :
  - no_sharp / few_sharp : proba venant d'un book de référence, Pinnacle présent ou au moins C.SHARPGAP_MIN_SHARP books de référence ;
  - no_xodds : le pari a une cote 1xBet (c["xodds"], sinon celle du même pari chez un pronostic relevé par xprice.apply) ;
  - soon : le match commence dans plus de C.SHARPGAP_SOON_MIN minutes ;
  - age_unknown / stale : le relevé sharp a un âge connu (c["sharp_at"], sinon la date du pari dans odds_history.json, voir sharp_age) d'au plus
    C.SHARPGAP_MAX_AGE_MIN minutes : les cotes du matin sont périmées l'après-midi (un book de référence bouge, 1xBet aussi) ;
  - x_stale : relevé 1xBet pas plus vieux que C.SHARPGAP_MAX_X_AGE_MIN minutes (âge inconnu : accepté, la cote est revérifiée en direct avant
    d'envoyer un coupon) ;
  - edge : écart >= C.SHARPGAP_EDGE ; suspect : écart > C.SHARPGAP_MAX_EDGE (cote en retard ou pari mal apparié : écarté, pas annoncé).
Les candidats sont classés par écart décroissant, un seul par match et marché (per_match) pour ne pas empiler des paris corrélés. Chaque candidat
est un dict prêt à afficher (describe()), à enregistrer en coupon (xref, xodds, xgid) et à suivre (key, id, market...). Les refus pour âge ou
cote 1xBet périmée qui auraient eu un écart suffisant sont gardés dans scan()["refused"] (de quoi dire « 3 écarts refusés : relevé sharp trop vieux »).

Phase 2 (paris suivis) : le matin (app.run_daily) la section « 💰 1xBet paie plus que le marché » annonce les C.SHARPGAP_MAX_SHOWN plus gros écarts, chacun avec
son code coupon et son bouton « ✅ Je l'ai joué » ; aux relevés (15:00, mise à jour manuelle) et aux passages horaires (clôture), xalerts en annonce de nouveaux
APRÈS avoir relu le prix 1xBet en direct et recalculé l'écart à ce prix (check). Chaque pari annoncé est numéroté « g<n> » dans data/sharpgap_sent.json (dédoublonné
par clé et par jour : jamais deux fois le même pari, au plus C.SHARPGAP_MAX_DAY par jour) et suivi comme les autres paris (track.record_gap : note, argent,
clôture, règlement, bilan). Sa clôture mesure l'écart contre Pinnacle : r["gap_edge_close"] = cote prise x proba de clôture sans marge - 1 (track.close,
freeclose.fill : « CLV moyen contre Pinnacle », summary_line).
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config as C, ensemble, fmt, store

REASONS = ("no_sharp", "few_sharp", "no_xodds", "soon", "age_unknown", "stale", "x_stale", "edge", "suspect")
SENT_FILE = "sharpgap_sent.json"                 # {"day", "n", "batches", "items": {clé du pari: {"n", "batch", "xodds", "edge", "at", "via"}}}


def _dt(iso):
    """Instant (aware, UTC) d'un texte ISO, « Z » compris ; None si illisible."""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def sharp_age(c, now, history=None):
    """Âge en minutes du relevé sharp du pari `c` : c["sharp_at"] (heure ISO du relevé The Odds API, posée par odds.build_picks en phase 2), sinon
    la date de la dernière entrée du pari dans odds_history.json (`history` : {clé: {"t": ISO}}, snapshot() à chaque relevé) ; None si inconnue."""
    t = _dt(c.get("sharp_at")) if c.get("sharp_at") else None
    if t is None:
        h = (history or {}).get(c.get("key"))
        t = _dt(h.get("t")) if isinstance(h, dict) and h.get("t") else None
    return None if t is None else max(0.0, (now - t).total_seconds() / 60)


def x_age(today, c, now):
    """Âge en minutes du relevé 1xBet du pari : c["x_at"] (ISO) ou l'heure « HH:MM » (UTC) de today["picks_at"] / today["xbet_at"] ; None si inconnu."""
    t = _dt(c.get("x_at")) if c.get("x_at") else None
    if t is None:
        hhmm = today.get("picks_at") or today.get("xbet_at")
        try:
            h, m = (int(x) for x in str(hhmm).split(":")[:2])
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                t -= timedelta(days=1)
        except (TypeError, ValueError):
            t = None
    return None if t is None else max(0.0, (now - t).total_seconds() / 60)


def _xprices(pronos):
    """{identité du pari: (cote, référence, id 1xBet)} des pronostics relevés chez 1xBet : le même pari que le pari réel, au même prix."""
    out = {}
    for c in pronos or []:
        if c.get("xodds"):
            out[ensemble._kid(c)] = (c["xodds"], c.get("xref"), c.get("xgid"))
    return out


def judge(c, today, now, history, xp=None, price=None, min_edge=None, max_age=None, x_fresh=True):
    """Le pari réel `c` est-il un écart sharp ? -> (candidat | None, raison) ; raison '' = retenu. Les raisons de REASONS ; pour « edge », « age_unknown »,
    « stale », « x_stale » et « suspect » le candidat est rendu aussi (écart calculé) : scan en tire les refus. `price` : (cote, référence, id 1xBet) lus
    en direct (check), à la place de ceux du pari ; `xp` : cotes des pronostics (_xprices) à défaut. `x_fresh` faux : l'âge du relevé 1xBet ne compte pas (le prix
    sera relu en direct avant tout envoi : xalerts.gap_items)."""
    min_edge = C.SHARPGAP_EDGE if min_edge is None else min_edge
    max_age = C.SHARPGAP_MAX_AGE_MIN if max_age is None else max_age
    s = ensemble.sharp_of(c)
    if not s:
        return None, "no_sharp"
    p, src, n_sharp = s
    if not (src == "pinnacle" or n_sharp >= C.SHARPGAP_MIN_SHARP):
        return None, "few_sharp"
    if price is not None:
        xodds, xref, xgid = price
    else:
        xodds, xref, xgid = c.get("xodds"), c.get("xref"), c.get("xgid")
        if not xodds and xp:
            xodds, xref, xgid = xp.get(ensemble._kid(c), (None, None, None))
    if not xodds or xodds <= 1:
        return None, "no_xodds"
    start = _dt(c.get("commence"))
    mins = None if start is None else (start - now).total_seconds() / 60
    if mins is None or mins < C.SHARPGAP_SOON_MIN:
        return None, "soon"
    edge = xodds * p - 1
    age, xage = sharp_age(c, now, history), x_age(today, c, now)
    cand = {"key": c["key"], "id": c["id"], "sport_key": c.get("sport_key"), "group": c.get("group", "soccer"), "event": c.get("event"),
            "home": c.get("home"), "away": c.get("away"), "league": c.get("league"), "start": c.get("start"), "commence": c["commence"],
            "market": c["market"], "who": c.get("who"), "name": c["name"], "point": c.get("point"), "pick": c.get("pick"),
            "p_sharp": round(p, 4), "fair_odds": round(1 / p, 2), "src": src, "n_sharp": n_sharp, "n_books": c.get("n_books"),
            "xodds": xodds, "xref": xref, "xgid": xgid, "xcid": c.get("xcid"), "edge": round(edge, 4),
            "kelly_f": round(edge / (xodds - 1), 4), "age_min": None if age is None else round(age),
            "x_age_min": None if xage is None else round(xage), "mins_to_start": round(mins), "book": c.get("book"),
            "best_odds": c.get("best_odds"), "typ_odds": c.get("typ_odds"), "sharp_at": c.get("sharp_at"), "sharp_src": c.get("sharp_src")}
    why = ("age_unknown" if age is None else "stale" if age > max_age
           else "x_stale" if x_fresh and xage is not None and xage > C.SHARPGAP_MAX_X_AGE_MIN else "")
    if edge < min_edge:
        return cand, "edge"
    if why:
        return cand, why
    if edge > C.SHARPGAP_MAX_EDGE:
        return cand, "suspect"
    return cand, ""


def scan(today, now=None, history=None, min_edge=None, max_age=None, per_match=True, use_pronos=True, x_fresh=True):
    """Écarts sharp des paris réels de `today` (data/today.json) à l'instant `now` -> {"cands": [...], "rejected": Counter par raison,
    "refused": [candidats d'écart suffisant refusés pour âge ou relevé 1xBet périmé, "why" en plus], "n_picks": nombre de paris examinés}.
    `x_fresh` faux : le relevé 1xBet d'un pari peut être vieux (relevé de l'après-midi : le prix est relu en direct avant l'envoi)."""
    now = now or datetime.now(timezone.utc)
    history = store.load("odds_history.json", {}) if history is None else history
    xp = _xprices(today.get("pronos")) if use_pronos else {}
    out, rej, refused = [], Counter(), []
    picks = today.get("picks") or []
    for c in picks:
        cand, why = judge(c, today, now, history, xp, None, min_edge, max_age, x_fresh)
        if why in ("age_unknown", "stale", "x_stale"):
            rej[why] += 1
            refused.append(cand | {"why": why})
        elif why:
            rej[why] += 1
        else:
            out.append(cand)
    out.sort(key=lambda x: (-x["edge"], -(x["n_sharp"] or 0), x["age_min"]))
    if per_match:
        seen, keep = set(), []
        for x in out:
            k = (x["id"], x["market"])
            if k not in seen:
                seen.add(k)
                keep.append(x)
        out = keep
    refused.sort(key=lambda x: -x["edge"])
    return {"cands": out, "rejected": rej, "refused": refused, "n_picks": len(picks)}


def check(c, xodds, xref, xgid, now=None, history=None, min_edge=None, max_age=None):
    """Re-vérifie le pari réel `c` (avec ses probas sharp) au prix 1xBet lu EN DIRECT `xodds` (référence `xref`, id `xgid`) : l'écart est recalculé à ce prix et
    refusé s'il est sous le seuil, si le relevé sharp est périmé ou si le match est imminent -> (candidat | None, raison '' si retenu)."""
    now = now or datetime.now(timezone.utc)
    cand, why = judge(c | {"x_at": now.isoformat()}, {}, now, {} if history is None else history, None, (xodds, xref, xgid), min_edge, max_age)
    return (cand if not why else None), why


def find(today, now=None, history=None, **kw):
    """Candidats d'écart sharp, du plus grand écart au plus petit (voir scan pour les filtres et les options)."""
    return scan(today, now, history, **kw)["cands"]


def _n(x, d=2):
    return f"{x:.{d}f}".replace(".", ",")


def describe(x):
    """Textes prêts à afficher (sans HTML) : {"title", "line", "age"}. title : le pari ; line : « 1xBet 1,95 · juste 1,81 (Pinnacle) · +7,7 % » ;
    age : « relevé sharp il y a 12 min · match dans 95 min »."""
    book = "Pinnacle" if x["src"] == "pinnacle" else f"{x['n_sharp']} books de référence"
    age = "âge du relevé sharp inconnu" if x.get("age_min") is None else f"relevé sharp il y a {x['age_min']} min"
    return {"title": x.get("pick") or x["name"],
            "line": f"1xBet {_n(x['xodds'])} · juste {_n(x['fair_odds'])} ({book}) · {x['edge'] * 100:+.1f} %".replace(".", ","),
            "age": f"{age} · match dans {x['mins_to_start']} min"}


# ---------- paris annoncés : numéros « g<n> », dédoublonnage par jour ----------

def load_sent(day):
    """Document des paris annoncés le jour `day` (vide si un autre jour y est écrit) : {"day", "n", "batches", "items": {clé: {...}}}."""
    d = store.load(SENT_FILE, {})
    day = str(day)[:10]
    if isinstance(d, dict) and d.get("day") == day and isinstance(d.get("items"), dict):
        return d | {"n": max(d["n"] if isinstance(d.get("n"), int) else 0, len(d["items"])),
                    "batches": d["batches"] if isinstance(d.get("batches"), int) else 0}
    return {"day": day, "n": 0, "batches": 0, "items": {}}


def sent_keys(day):
    """Clés des paris déjà annoncés aujourd'hui (matin, relevés, alertes) : un pari n'est annoncé qu'une fois par jour."""
    return set(load_sent(day)["items"])


def room(day):
    """Combien de paris peuvent encore être annoncés aujourd'hui (C.SHARPGAP_MAX_DAY au plus par jour)."""
    return max(0, C.SHARPGAP_MAX_DAY - len(load_sent(day)["items"]))


def reserve(cands, day, via, now=None):
    """Numérote (« g<n> », stable toute la journée) et retient les candidats `cands` annoncés ensemble dans UN message (`via` : « matin », « relevé »,
    « alerte »). Un pari déjà connu garde son numéro et son message (relance du matin). Les nouveaux sont limités par jour (room). -> les candidats retenus,
    copies avec "n" (numéro) et "batch" (message) en plus."""
    doc = load_sent(day)
    out, batch = [], None
    at = (now or datetime.now(timezone.utc)).strftime("%H:%M")
    for x in cands:
        old = doc["items"].get(x["key"])
        if old is None:
            if len(doc["items"]) >= C.SHARPGAP_MAX_DAY:
                continue
            if batch is None:
                doc["batches"] += 1
                batch = f"b{doc['batches']}"
            doc["n"] += 1
            old = {"n": doc["n"], "batch": batch, "via": via}
        doc["items"][x["key"]] = old | {"xodds": x["xodds"], "edge": x["edge"], "at": at}
        out.append(x | {"n": old["n"], "batch": old["batch"]})
    if out:
        store.save(SENT_FILE, doc)
    return out


def unreserve(keys, day):
    """Reprend les numéros réservés pour `keys` quand le message n'a pas pu être bâti (rien n'a été annoncé : ces paris restent à annoncer). Ne touche pas
    aux paris déjà suivis (ligne de suivi existante ne se défait pas : seul l'enregistrement dans le document du jour)."""
    doc = load_sent(day)
    gone = [k for k in keys if k in doc["items"]]
    for k in gone:
        doc["items"].pop(k)
    if gone:
        store.save(SENT_FILE, doc)


# ---------- clôture : l'écart contre Pinnacle ----------

def close_edge(xodds, p_close):
    """Écart de la cote prise contre la cote finale sans marge d'un book de référence : cote x proba de clôture - 1 (> 0 : la cote 1xBet battait la clôture)."""
    return round(xodds * p_close - 1, 4)


def summary_line(rows, days=None, today=None):
    """« Paris 1xBet en retard : 7 suivis (3 joués), CLV moyen contre Pinnacle +2,1 % (5 mesurés) » (texte brut) ; '' sans pari suivi. CLV : moyenne de
    r["gap_edge_close"] (close_edge) des paris dont la clôture vient de Pinnacle (r["gap_close_src"] « pinnacle »)."""
    mine = [r for r in rows if r.get("sharpgap") and r.get("sent")]
    if days:
        cut = str((datetime.fromisoformat(str(today)) if today else datetime.now(timezone.utc)).date() - timedelta(days=days))
        mine = [r for r in mine if str(r.get("date") or "") >= cut]
    if not mine:
        return ""
    played_ = sum(1 for r in mine if r.get("played") and not r.get("skipped"))
    m = [r["gap_edge_close"] for r in mine if r.get("gap_edge_close") is not None and r.get("gap_close_src") == "pinnacle"]
    txt = f"Paris 1xBet en retard : {len(mine)} suivi{'s' if len(mine) > 1 else ''} ({played_} joué{'s' if played_ > 1 else ''})"
    if m:
        v = sum(m) / len(m) * 100
        txt += (f", CLV moyen contre Pinnacle {'+' if v >= 0 else '−'}{fmt.num(abs(v), 1)} % "
                f"(cote prise face à la cote finale de Pinnacle, {len(m)} mesuré{'s' if len(m) > 1 else ''})")
    return txt
