"""Carte des edges par case (voie, ligue, famille, sens) pour les voies stats et saison. Règles pré-déclarées dans edgemap_ledger.json (commité AVANT ce code).

Voies : stats (journal d'ensemble, lignes `strategy == "stats"`), stats_cup (journal des stats de coupe), saison (jambes à VALEUR `sval == 1`), saison_cup ; championnats et coupes ne se
mélangent jamais. Par case : lignes réglées, lignes à CLV mesuré (lanestats.x_clv : prix pris contre le dernier prix 1xBet connu), CLV moyen et demi-marge à 95 % PAR MATCH, réalisé / annoncé,
gain THÉORIQUE (1 unité au prix `xodds` du journal : ce n'est pas l'argent réellement misé).

Couleur : lisible = au moins 100 lignes à CLV mesuré pour une case (300 pour un agrégat) ET 30 matchs distincts ; verte (CLV - marge > 0), rouge (CLV + marge < 0), grise (indécise ou pas assez de
données). Cascade quand une case est trop mince : (voie, famille, sens) toutes ligues, puis (voie, famille).

Évaluation LE LUNDI seulement (apply, après lanestats.apply) : une couleur ne change que si la même couleur candidate est vue deux lundis consécutifs (7 jours exacts). L'état est dans
data/edgemap_state.json ; le bilan hebdo, la commande `carte` et le mécanisme lisent cet état enregistré. Mécanisme (EDGEMAP_GATE, éteint par défaut) : une jambe à valeur dont la case, ou à défaut
son agrégat lisible le plus proche, est ROUGE passe en fantôme (lanes.usable) ; vert et gris : comportement actuel. Il ne s'active qu'après 4 semaines de carte en lecture seule. Rien n'est appris
de la carte. Aucune écriture hors edgemap_state.json ; rien ne lève : un document illisible donne une carte vide."""
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

from . import calib, config as C, lanestats as LS, store

STATE_FILE = "edgemap_state.json"
VOIES = ("stats", "stats_cup", "saison", "saison_cup")
VOIE_LABEL = {"stats": "stats", "stats_cup": "stats (coupes)", "saison": "saison", "saison_cup": "saison (coupes)"}
CASE_MIN = 100                                 # lignes à CLV mesuré pour lire une case
AGG_MIN = 300                                  # ... pour lire un agrégat (toutes ligues)
MIN_MATCHES = 30                               # matchs distincts (comme la coupure de la voie stats)
STALE_DAYS = 14                                 # état enregistré plus vieux que ça : le mécanisme n'agit plus
GATE_WAIT_DAYS = 28                            # le mécanisme reste éteint tant que 4 semaines ne se sont pas écoulées depuis le premier lundi évalué
SHOW_MIN = 30                                  # affichage seulement : les cases plus minces sont comptées, pas listées
ICON = {"green": "🟢", "red": "🔴", "grey": "⚪"}
_memo = {"t": 0.0, "v": None}


def _today():
    return datetime.now(timezone.utc).date()          # heure d'Abidjan = UTC


def sens_of(name):
    n = str(name or "").strip().lower()
    return "plus" if n == "over" else "moins" if n == "under" else "autre"


def key_of(voie, league, fam, sens):
    return f"{voie}|{league}|{fam}|{sens}"


def voie_of(c):
    """Voie de la carte d'une ligne (pronostic, jambe ou ligne de journal), ou None : la carte ne couvre que les jambes à VALEUR de la voie stats et de la voie saison."""
    if c.get("strategy") == "saison" or c.get("lane") == "saison":
        return ("saison_cup" if c.get("cup") else "saison") if c.get("sval") == 1 else None
    if c.get("strategy") == "stats":
        return "stats_cup" if c.get("cup") else "stats"
    return None


# ---------- mesure ----------

def voie_rows():
    """{voie: lignes RÉGLÉES de la population pré-déclarée}, une même clé une seule fois."""
    from . import cupstats, seasonlane
    out = {v: [] for v in VOIES}
    seen = set()

    def put(voie, r):
        k = (voie, r.get("key"))
        if LS.settled(r) and r.get("key") and k not in seen:
            seen.add(k)
            out[voie].append(r)

    for r in LS.journal_rows():
        if isinstance(r, dict) and r.get("strategy") == "stats" and not r.get("cup"):
            put("stats", r)
    for r in cupstats.journal_rows():
        if isinstance(r, dict) and r.get("strategy") == "stats":
            put("stats_cup", r)
    for r in seasonlane.journal_rows():
        if isinstance(r, dict) and r.get("sval") == 1:
            put("saison_cup" if r.get("cup") else "saison", r)
    return out


def measure(rows):
    """Chiffres d'un groupe de lignes réglées : n (réglées), nc (CLV mesuré), matchs (distincts, parmi les CLV mesurés), clv, h (demi-marge à 95 % par match), réalisé, annoncé, gain théorique."""
    xs = [x if isinstance(x, (int, float)) and math.isfinite(x) else None for x in (LS.x_clv(r) for r in rows)]      # nan / inf = non mesuré
    nc, m, h = LS.stats(xs, [r.get("id") or i for i, r in enumerate(rows)])
    wins = sum(1 for r in rows if r.get("result") == "win")
    ann = [r.get("pe") if r.get("pe") is not None else r.get("pm") for r in rows]
    ann = [a for a in ann if isinstance(a, (int, float)) and not isinstance(a, bool)]
    gain = 0.0
    for r in rows:                                  # seulement les lignes à prix valide (fini, > 1) : ni gagnée ni perdue sans prix
        try:
            o = float(r["xodds"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(o) and o > 1.0:
            gain += (o - 1.0) if r.get("result") == "win" else -1.0
    return {"n": len(rows), "nc": nc, "matches": len({r.get("id") for r, x in zip(rows, xs) if x is not None}), "clv": m, "h": h,
            "real": wins / len(rows) if rows else None, "ann": sum(ann) / len(ann) if ann else None, "gain": round(gain, 2)}


def colour(s, agg):
    """(couleur, lisible) d'une case mesurée : lisible = assez de lignes à CLV mesuré et de matchs ; verte / rouge seulement si l'intervalle à 95 % exclut zéro."""
    need = AGG_MIN if agg else CASE_MIN
    if s["nc"] < need or s["matches"] < MIN_MATCHES or s["clv"] is None or s["h"] is None:
        return "grey", False
    return ("green" if s["clv"] - s["h"] > 0 else "red" if s["clv"] + s["h"] < 0 else "grey"), True


def compute(rows_by_voie=None):
    """{clé de case ou d'agrégat: {"voie","league","fam","sens", chiffres, "colour", "readable", "agg"}} : cases (voie, ligue, famille, sens), agrégats (voie, *, famille, sens) et (voie, *, famille, *)."""
    rows_by_voie = voie_rows() if rows_by_voie is None else rows_by_voie
    groups = {}
    for voie, rows in rows_by_voie.items():
        for r in rows:
            fam, sens = calib.family(r.get("market") or ""), sens_of(r.get("name"))
            lg = r.get("sport_key") or "?"
            for league, s in ((lg, sens), ("*", sens), ("*", "*")):
                groups.setdefault((voie, league, fam, s), []).append(r)
    table = {}
    for (voie, league, fam, sens), rows in groups.items():
        agg = league == "*"
        s = measure(rows)
        col, readable = colour(s, agg)
        table[key_of(voie, league, fam, sens)] = s | {"voie": voie, "league": league, "fam": fam, "sens": sens, "colour": col, "readable": readable, "agg": agg}
    return table


def cascade(voie, league, fam, sens):
    """Les cases à consulter, de la plus fine à la plus large."""
    return [key_of(voie, league, fam, sens), key_of(voie, "*", fam, sens), key_of(voie, "*", fam, "*")]


def resolve(table, voie, league, fam, sens):
    """(clé, case) de la première case lisible de la cascade ; (None, None) sinon (grise : pas assez de données)."""
    for k in cascade(voie, league, fam, sens):
        s = table.get(k)
        if s and s.get("readable"):
            return k, s
    return None, None


# ---------- état du lundi ----------

def state():
    """Document d'état enregistré (relu au plus toutes les 2 s : lanes.usable l'interroge dans des boucles) ; {} sans document ou illisible."""
    now = time.monotonic()
    if _memo["v"] is None or now - _memo["t"] > 2.0:
        d = store.load(STATE_FILE, {})
        _memo.update(t=now, v=d if isinstance(d, dict) else {})
    return _memo["v"]


def reset():
    _memo.update(t=0.0, v=None)


def apply(today=None):
    """Évalue la carte et enregistre l'état, LE LUNDI SEULEMENT. Une couleur committée ne change que si la même couleur candidate est constatée deux lundis consécutifs (7 jours exacts : un lundi manqué
    casse la chaîne) ; la couleur initiale est grise. -> True si l'état a été écrit ; ne lève jamais."""
    try:
        day = date.fromisoformat(str(today or _today())[:10])
        if day.weekday() != 0:
            return False
        doc = state()
        prev = doc.get("colours") if isinstance(doc.get("colours"), dict) else {}
        watch_prev = doc.get("watch") if isinstance(doc.get("watch"), dict) else {}
        week_ago, iso = str(day - timedelta(days=7)), str(day)
        table = compute()
        if not any(s["n"] for s in table.values()):                     # lecture du journal vide ou en échec : on n'écrase pas l'état enregistré
            print("[carte] aucune ligne réglée lue : état conservé", file=sys.stderr)
            return False
        snap, watch = {}, {}
        for k, s in table.items():
            cur = (prev.get(k) or {}).get("c", "grey")
            raw = s["colour"]
            new = cur
            if raw != cur:
                w = watch_prev.get(k)
                if isinstance(w, dict) and w.get("cand") == raw and w.get("on") == week_ago:
                    new = raw                          # constaté deux lundis de suite : la couleur change
                else:
                    watch[k] = {"cand": raw, "on": iso}
            if s["readable"] or new != "grey" or k in watch:
                snap[k] = {"c": new, "raw": raw, "r": s["readable"], "voie": s["voie"], "league": s["league"], "fam": s["fam"], "sens": s["sens"], "agg": s["agg"],
                           "n": s["n"], "nc": s["nc"], "matches": s["matches"], "clv": s["clv"], "h": s["h"], "real": s["real"], "ann": s["ann"], "gain": s["gain"]}
        counts = {v: sum(1 for s in table.values() if s["voie"] == v and s["readable"] and not s["agg"]) for v in VOIES}
        store.save(STATE_FILE, {"first": doc.get("first") or iso, "week": iso, "colours": snap, "watch": watch, "readable_cases": counts, "comparisons": sum(counts.values())})
        reset()
        return True
    except Exception as e:                      # noqa: BLE001 - la carte ne bloque jamais le message du matin
        print(f"[carte] évaluation ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ---------- mécanisme (éteint par défaut) ----------

def gate_active(st=None):
    """Le mécanisme agit-il ? EDGEMAP_GATE allumé ET au moins GATE_WAIT_DAYS jours depuis le premier lundi évalué (règle d'activation pré-déclarée : 4 semaines de lecture seule)."""
    if not C.EDGEMAP_GATE:
        return False
    st = state() if st is None else st
    try:
        t = _today()
        return (bool(st.get("first")) and t >= date.fromisoformat(str(st["first"])) + timedelta(days=GATE_WAIT_DAYS)
                and (t - date.fromisoformat(str(st["week"]))).days <= STALE_DAYS)         # un état périmé (lundis manqués) ne bloque rien
    except (ValueError, KeyError, TypeError):
        return False


def committed(st, voie, league, fam, sens):
    """Couleur committée de la case de la jambe, ou à défaut de son agrégat lisible le plus proche (état enregistré, jamais le journal) ; « grey » sans case lisible."""
    cols = st.get("colours") if isinstance(st.get("colours"), dict) else {}
    for k in cascade(voie, league, fam, sens):
        rec = cols.get(k)
        if isinstance(rec, dict) and rec.get("r"):
            return rec.get("c", "grey")
    return "grey"


def gate_ok(c):
    """La jambe `c` peut-elle être jouée ? Faux seulement quand le mécanisme agit ET que sa case (ou son agrégat) est rouge ; les jambes hors carte (fill, autres voies) : vrai."""
    if not C.EDGEMAP_GATE:
        return True
    voie = voie_of(c)
    if voie is None:
        return True
    st = state()
    if not gate_active(st):
        return True
    return committed(st, voie, c.get("sport_key") or "?", calib.family(c.get("market") or ""), sens_of(c.get("name"))) != "red"


# ---------- affichage ----------

def _pc(x, signed=True):
    return LS._pc(x, signed)


def _chance(n):
    """Ce que le hasard seul donnerait : 2,5 % des cases lisibles sans aucun edge réel passent vertes (idem rouges) à chaque évaluation."""
    return f"par hasard seul : ≈ {n * 0.025:.1f} case(s) verte(s) et autant de rouges".replace(".", ",")


def _line(s):
    where = "toutes ligues" if s["league"] == "*" else calib.league_name(s["league"])
    sens = {"plus": "plus", "moins": "moins", "autre": "", "*": "tous sens"}[s["sens"]]
    head = f"{ICON[s.get('c', s.get('colour'))]} {calib.LABELS.get(s['fam'], s['fam'])}{' · ' + sens if sens else ''} · {where}"
    body = f"{s['n']} lignes ({s['nc']} mesurées, {s['matches']} matchs)"
    if s["clv"] is not None:
        body += f" · CLV {_pc(s['clv'])}" + (f" ± {_pc(s['h'], False)}" if s["h"] is not None else "")
    if s["real"] is not None:
        body += f" · réalisé {round(s['real'] * 100)} %" + (f" / annoncé {round(s['ann'] * 100)} %" if s["ann"] is not None else "")
    body += f" · gain théorique {s['gain']:+.1f} u".replace("-", "−")
    return f"{head} : {body}"


def _entries(st, voie, fresh):
    """Cases à afficher d'une voie : l'état enregistré du lundi (couleur committée) ; sans état, la mesure du moment."""
    if fresh is not None:
        return [s | {"c": s["colour"]} for s in fresh.values() if s["voie"] == voie]
    cols = st.get("colours") if isinstance(st.get("colours"), dict) else {}
    return [v for v in cols.values() if isinstance(v, dict) and v.get("voie") == voie]


def report(args=()):
    """Commande `carte [stats | saison]` : tableau par voie (agrégats d'abord, puis cases lisibles), vertes / rouges / grises avec leurs chiffres. Ne lève jamais."""
    try:
        words = [str(a).lower() for a in (args or ())]
        only = [v for v in VOIES if any(w in v or w.startswith(v.split("_")[0]) for w in words)] if words else list(VOIES)
        st = state()
        fresh = None
        if not st.get("colours"):
            fresh = compute()                       # avant le premier lundi : la mesure du moment, dite telle quelle
        out = ["🗺️ Carte des edges (CLV 1xBet par case : prix pris contre le dernier prix 1xBet connu, intervalle à 95 % par match)"]
        out.append(f"état du lundi {st['week']}, couleurs confirmées deux lundis de suite" if st.get("week") and fresh is None else
                   "pas encore évaluée un lundi : mesure du moment, aucune couleur confirmée")
        out.append(f"🟢 CLV > 0 et intervalle sans zéro · 🔴 CLV < 0 et intervalle sans zéro · ⚪ indécis ou trop peu de données (lecture : {CASE_MIN} lignes mesurées pour une case, {AGG_MIN} pour un agrégat, {MIN_MATCHES} matchs)")
        out.append("mécanisme (EDGEMAP_GATE) : " + ("allumé" + ("" if gate_active(st) else f" mais en attente (lecture seule {GATE_WAIT_DAYS} jours)") if C.EDGEMAP_GATE else "éteint : la carte ne change aucun pari"))
        for voie in only:
            es = _entries(st, voie, fresh)
            cases = [e for e in es if not e.get("agg")]
            readable = [e for e in cases if e.get("r", e.get("readable"))]
            cnt = {col: sum(1 for e in readable if e.get("c") == col) for col in ICON}
            thin = [e for e in cases if not e.get("r", e.get("readable"))]
            block = [f"\n— {VOIE_LABEL[voie]} : {len(readable)} case{'s' if len(readable) > 1 else ''} lisible{'s' if len(readable) > 1 else ''} (🟢 {cnt['green']} · 🔴 {cnt['red']} · ⚪ {cnt['grey']}) · {_chance(len(readable))}"]
            aggs = sorted((e for e in es if e.get("agg") and e.get("nc", 0) >= SHOW_MIN), key=lambda e: (e["fam"], e["sens"]))
            block += [_line(e) + (f" (du moment : {ICON[e['raw']]})" if e.get("raw") not in (None, e.get("c")) else "") for e in aggs]
            block += [_line(e) + (f" (du moment : {ICON[e['raw']]})" if e.get("raw") not in (None, e.get("c")) else "")
                      for e in sorted(cases, key=lambda e: (-e.get("nc", 0))) if e.get("nc", 0) >= SHOW_MIN]
            hidden = sum(1 for e in thin if e.get("nc", 0) < SHOW_MIN)
            if hidden:
                block.append(f"… {hidden} case{'s' if hidden > 1 else ''} avec moins de {SHOW_MIN} lignes mesurées, non listée{'s' if hidden > 1 else ''}")
            if not es:
                block.append("aucune ligne réglée pour l'instant")
            out += block
        return "\n".join(out)
    except Exception as e:                      # noqa: BLE001
        print(f"[carte] affichage illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return "🗺️ La carte est illisible pour l'instant (voir kelly logs)."


def weekly_lines():
    """Bloc du bilan hebdo : l'état ENREGISTRÉ au dernier lundi (jamais un recalcul : le rapport et le mécanisme ne divergent pas). [] sans état."""
    try:
        st = state()
        cols = st.get("colours") if isinstance(st.get("colours"), dict) else {}
        if not cols:
            return []
        out = []
        for voie in VOIES:
            cases = [v for v in cols.values() if isinstance(v, dict) and v.get("voie") == voie and not v.get("agg") and v.get("r")]
            if not cases:
                continue
            cnt = {col: sum(1 for e in cases if e.get("c") == col) for col in ICON}
            out.append(f"{VOIE_LABEL[voie]} : {len(cases)} cases lisibles (🟢 {cnt['green']} · 🔴 {cnt['red']} · ⚪ {cnt['grey']}) · {_chance(len(cases))}")
            for e in sorted((e for e in cases if e.get("c") != "grey"), key=lambda e: e.get("clv") or 0, reverse=True)[:5]:
                out.append("  " + _line(e))
        out.append(f"mécanisme : {'allumé' if C.EDGEMAP_GATE else 'éteint'} · carte évaluée le lundi {st.get('week')} · commande « carte » pour le détail")
        return out if len(out) > 1 else []
    except Exception as e:                      # noqa: BLE001
        print(f"[carte] bloc hebdo illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return []
