"""Mesures par voie (sharpgap / stats / fill), partagées par le bilan, le hebdo, les objectifs et la coupure automatique de la voie stats.

CLV 1xBet d'une ligne du journal d'ensemble = log(cote prise / dernier prix 1xBet connu) ; « connu » = relevé à `LEAD_OK_MIN` minutes ou moins du coup d'envoi (au-delà,
ce n'est pas une clôture). Une ligne dont le pari a été retiré par 1xBet (`xgone`) n'a pas de clôture : exclue, et comptée à part. Marges à 95 % PAR MATCH (erreur-type
par grappes : 7 lignes d'un même match ne comptent pas comme 7 preuves). Aucune écriture ici, rien ne lève : un document illisible donne des listes vides."""
import math
import sys
from collections import defaultdict

from . import config as C, ensemble, store

LEAD_OK_MIN = 90
LANES = ("sharpgap", "stats", "fill")
OBJ_MIN_N = 300                                # objectif 1 : lignes réglées minimales avant de conclure
CUT_MIN_N = 300                                # coupure de la voie stats entière
CUT_MIN_N_LEAGUE = 200                         # coupure et retour d'une ligue
_memo = {"key": None, "rows": []}


def stats(xs, groups=None):
    """(n, moyenne, demi-marge à 95 %) ; (0, None, None) sans valeur ; demi-marge None sous deux grappes."""
    pairs = [(x, (groups[i] if groups is not None else i)) for i, x in enumerate(xs) if x is not None]
    n = len(pairs)
    if not n:
        return 0, None, None
    m = sum(x for x, _ in pairs) / n
    clusters = defaultdict(float)
    for x, g in pairs:
        clusters[g] += x - m
    k = len(clusters)
    if k < 2:
        return n, m, None
    se = math.sqrt(k / (k - 1) * sum(v * v for v in clusters.values())) / n
    return n, m, 1.96 * se


def journal_rows():
    """Journal d'ensemble + archive compacte (le journal, plus complet, l'emporte). [] si illisible. Mémoïsé tant que les documents ne changent pas."""
    try:
        key = (store.version(ensemble.LOG_FILE), tuple(store.version(n) for n in store.names(ensemble.ARCH_PREFIX)))
        if _memo["key"] == key:
            return _memo["rows"]
        log = store.load(ensemble.LOG_FILE, [])
        log = [r for r in log if isinstance(r, dict)] if isinstance(log, list) else []
        seen = {r.get("key") for r in log}
        rows = log + [r for r in ensemble.archive_rows() if r.get("key") not in seen]
        _memo.update(key=key, rows=rows)
        return rows
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] journal illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return []


def settled(r):
    return r.get("result") in ("win", "loss")


def x_clv(r):
    """log(cote prise / dernier prix 1xBet connu) d'une ligne réglée, ou None (pas de prix pris, pas de relevé assez proche, pari retiré par 1xBet)."""
    xc = r.get("xclose")
    if (r.get("xgone") or not r.get("xodds") or not isinstance(xc, dict) or not xc.get("odds") or xc.get("lead_min") is None
            or not 0 <= xc["lead_min"] <= LEAD_OK_MIN or xc.get("susp")):
        return None                                 # relevé avant le match seulement (pas en jeu), prix jouable (pas suspendu), pari non retiré
    try:
        return math.log(float(r["xodds"]) / float(xc["odds"]))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def clv(rows):
    """(n, moyenne, demi-marge, nombre de lignes retirées par 1xBet) sur des lignes réglées."""
    xs = [x_clv(r) for r in rows]
    n, m, h = stats(xs, [r.get("id") or i for i, r in enumerate(rows)])
    return n, m, h, sum(1 for r in rows if r.get("xgone"))


def lane_rows(lane, rows=None, sport_key=None):
    out = [r for r in (journal_rows() if rows is None else rows) if r.get("strategy") == lane and settled(r)]
    return [r for r in out if r.get("sport_key") == sport_key] if sport_key else out


def verdict(n, m, h, min_n):
    """'neg' : CLV négatif, intervalle à 95 % entièrement sous zéro, au moins `min_n` lignes ; 'pos' : entièrement au-dessus ; sinon None."""
    if n < min_n or m is None or h is None:
        return None
    return "neg" if m + h < 0 else "pos" if m - h > 0 else None


def objective_lines(plain=False):
    """Les deux objectifs (texte brut) : (1) CLV contre le dernier prix 1xBet connu positif, intervalle à 95 % excluant zéro, sur au moins OBJ_MIN_N lignes de valeur réglées
    (sharpgap + stats) ; (2) capital actuel ≥ capital de départ. Aucun montant n'est un objectif."""
    from . import money
    out = []
    word = "Prix pris contre le dernier prix 1xBet" if plain else "CLV 1xBet"          # le bilan simple n'emploie pas le mot CLV
    try:
        from . import lanes
        rows = [r for r in lane_rows("sharpgap") + lane_rows("stats") if r.get("strategy") == "sharpgap" or not lanes.league_ghost(r.get("sport_key"))]      # voies jouées seulement
        n, m, h, _ = clv(rows)
        if m is None or not n:
            out.append(f"🎯 {word} : pas encore de mesure (0/{OBJ_MIN_N} lignes)")
        else:
            v = verdict(n, m, h, OBJ_MIN_N)
            tail = ("atteint ✅" if v == "pos" else "négatif, intervalle sous zéro ⚠️" if v == "neg" else
                    f"en cours ({n}/{OBJ_MIN_N} lignes)" if n < OBJ_MIN_N else "zéro dans l'intervalle : pas de preuve")
            out.append(f"🎯 {word} {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" · {tail}")
    except Exception as e:                      # noqa: BLE001
        out.append(f"🎯 {word} : illisible ({type(e).__name__})")
    st = money.capital_state()
    if st:
        ok = st["capital"] >= st["base"]
        out.append(f"🏁 Capital {money.money(round(st['capital']))} {'≥' if ok else '<'} départ {money.money(round(st['base']))}" + (" ✅" if ok else ""))
    return out


def _pc(x, signed=True):
    """Pourcentage à la française : « −3,1 % » (virgule, vrai signe moins)."""
    t = f"{x * 100:+.1f}" if signed else f"{x * 100:.1f}"
    return t.replace("-", "−").replace(".", ",") + " %"


def _since(days, today=None):
    from datetime import date, timedelta
    if not days:
        return ""
    return str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days))


def leg_results(rows, days=None, today=None):
    """{clé de jambe: (voie, résultat)} des jambes RÉGLÉES : paris suivis envoyés + jambes des combinés (à vérifier et notés). Une jambe qui est à la fois un pari et la
    jambe d'un combiné ne compte qu'une fois. Voie « legacy » : ligne d'avant les étiquettes."""
    from . import fair
    cut = _since(days, today)
    out = {}

    def put(key, lane, res, day):
        if key and res in ("win", "loss") and (day or "") >= cut and key not in out:
            out[key] = (lane if lane in LANES else "legacy", res)

    for r in rows:
        if r.get("sent"):
            put(r.get("key"), r.get("strategy"), r.get("result"), r.get("date"))
    docs = [fair.load_combos(), (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])]
    for combos in docs:
        for c in combos if isinstance(combos, list) else []:
            for leg in c.get("legs") or []:
                put(leg.get("key"), leg.get("strategy"), leg.get("result"), c.get("date"))
    return out


def combo_money(days=None, today=None):
    """{"avec valeur": (gain, nombre, mise), "sans valeur": ..., "avant étiquettes": ...} des combinés NOTÉS et réglés : l'argent d'un combiné n'est jamais réparti
    entre voies (il mélange leurs jambes), il est ventilé par `has_value`."""
    from . import fair, money
    bets = store.load_doc("bets.json", {"combos": []})
    rows = store.load_doc("tracked.json", [])
    out = {}
    for x, g in money._counted(bets, rows if isinstance(rows, list) else [], fair.load_combos(), days, today):
        if not (x.get("legs") is not None or x.get("keys") is not None):
            continue                                    # un pari simple n'est pas un combiné
        k = "avec valeur" if x.get("has_value") is True else "sans valeur" if x.get("has_value") is False else "avant étiquettes"
        a = out.get(k, (0.0, 0, 0.0))
        out[k] = (a[0] + g, a[1] + 1, a[2] + float((x.get("played") or {}).get("stake") or 0))
    return out


def lane_lines(rows, days=None, today=None, plain=False):
    """Lignes du bilan par voie (texte brut) : jambes réglées (gagnées sur jouées) et CLV 1xBet par voie, puis l'argent des combinés par `has_value`. [] sans rien de réglé."""
    from . import money
    out = []
    legs = leg_results(rows, days, today)
    cut = _since(days, today)
    log = [r for r in journal_rows() if (r.get("date") or "") >= cut]
    for lane in LANES + ("legacy",):
        mine = [res for ln, res in legs.values() if ln == lane]
        if not mine:
            continue
        w = sum(1 for res in mine if res == "win")
        txt = f"{'avant les étiquettes' if lane == 'legacy' else lane} : {w} gagnée{'s' if w > 1 else ''} sur {len(mine)} ({round(w / len(mine) * 100)} %)"
        if lane in ("sharpgap", "stats"):
            n, m, h, gone = clv([r for r in log if r.get("strategy") == lane and settled(r)])
            if n:
                txt += f" · {'prix pris contre le dernier prix 1xBet' if plain else 'CLV 1xBet'} {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" (sur {n} lignes du journal)"
        out.append(txt)
    try:
        cm = combo_money(days, today)
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] argent des combinés illisible : {type(e).__name__}: {e}", file=sys.stderr)
        cm = {}
    for k in ("avec valeur", "sans valeur", "avant étiquettes"):
        if k in cm:
            g, n, staked = cm[k]
            out.append(f"combinés {k} : {money.signed(round(g))} sur {n} noté{'s' if n > 1 else ''}" + (f" · misé {money.money(round(staked))}" if staked else ""))
    return out


# ---------- coupure automatique de la voie stats (et retour proposé) ----------

MIN_MATCHES = 30                               # une coupure exige aussi 30 matchs distincts : 200 lignes de 8 matchs ne sont pas 200 preuves


def _since_ok():
    from . import lanes
    return str((lanes.state().get("stats") or {}).get("since_ok") or "")


def _returned(sk):
    from . import lanes
    return str(((lanes.state().get("stats") or {}).get("returned") or {}).get(sk) or "")


def _eligible(rows, since=""):
    """Lignes `stats` réglées depuis la dernière remise à la main (voie entière) et, par ligue, depuis son retour : ce qui a précédé n'est jamais re-jugé."""
    out = []
    for r in rows:
        if settled(r) and r.get("strategy") == "stats" and (r.get("date") or "") >= max(since, _since_ok(), _returned(r.get("sport_key"))):
            out.append(r)
    return out


def _matches(rows):
    return len({r.get("id") for r in rows if x_clv(r) is not None})


def evaluate(rows=None):
    """{"global": verdict de la voie jouée, "leagues": {ligue: verdict} des ligues JOUÉES, "back": {ligue: verdict} des ligues en fantôme} ; chaque verdict = (n, moyenne, demi-marge,
    'neg' | 'pos' | None). La voie entière est jugée sur les lignes `stats` réglées des ligues jouées (celles déjà en fantôme n'y comptent pas), ≥ CUT_MIN_N lignes ; une ligue sur ses
    propres lignes, ≥ CUT_MIN_N_LEAGUE ; seules les lignes avec un relevé ≤ 90 min avant le match comptent, les paris retirés par 1xBet sont exclus. Lignes depuis la remise à la main."""
    from . import lanes
    rows = journal_rows() if rows is None else rows
    elig = _eligible(rows)
    played = [r for r in elig if not lanes.league_ghost(r.get("sport_key")) and r.get("sport_key")]
    n, m, h, _ = clv(played)
    out = {"global": (n, m, h, verdict(n, m, h, CUT_MIN_N) if _matches(played) >= MIN_MATCHES else None), "leagues": {}, "back": {}}
    by = defaultdict(list)
    for r in elig:
        if r.get("sport_key"):
            by[r["sport_key"]].append(r)
    leagues = (lanes.state().get("stats") or {}).get("leagues") or {}
    for sk, rs in by.items():
        if sk in leagues:
            rs = [r for r in rs if (r.get("date") or "") >= str((leagues[sk] or {}).get("since") or "")]      # après la coupure : l'échantillon indépendant
        n, m, h, _ = clv(rs)
        v = (n, m, h, verdict(n, m, h, CUT_MIN_N_LEAGUE) if _matches(rs) >= MIN_MATCHES else None)
        out["back" if lanes.league_ghost(sk) else "leagues"][sk] = v
    return out


def apply(today=None):
    """Évalue et écrit la coupure automatique. Ne fait rien si l'interrupteur STRATEGY_STATS n'est pas « on » ou si la voie est déjà coupée. Une voie coupée ne revient qu'à la main.
    -> liste des coupures décidées cette fois (texte brut) ; ne lève jamais."""
    from datetime import date
    from . import config as C, lanes
    try:
        if C.STRATEGY_STATS != "on":
            return []
        doc = dict(lanes.state())
        st = dict(doc.get("stats") or {})
        if st.get("mode") == "ghost":
            return []
        ev = evaluate()
        day = str(today or date.today())
        new = []
        n, m, h, v = ev["global"]
        if v == "neg":
            tot = len([r for r in _eligible(journal_rows()) if not lanes.league_ghost(r.get("sport_key")) and r.get("sport_key")])
            st.update(mode="ghost", since=day, reason=f"CLV 1xBet {_pc(m)} ± {_pc(h, False)} sur {n} lignes avec relevé 1xBet ≤ {LEAD_OK_MIN} min avant le match (sur {tot} réglées)",
                      n=n, mean=round(m, 4), half=round(h, 4))
            new.append(f"voie stats coupée : {st['reason']}")
        else:
            leagues = dict(st.get("leagues") or {})
            for sk, (n, m, h, v) in sorted(ev["leagues"].items()):
                if v == "neg":
                    leagues[sk] = {"since": day, "n": n, "mean": round(m, 4), "half": round(h, 4)}
                    new.append(f"ligue {sk} en fantôme : CLV 1xBet {_pc(m)} ± {_pc(h, False)} sur {n} lignes")
            if new:
                st["leagues"] = leagues
        if new:
            lanes.write_state(doc | {"stats": st})
        return new
    except Exception as e:                      # noqa: BLE001 - la coupure ne bloque jamais le message du matin
        print(f"[lanes] coupure automatique ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return []


def cut_lines():
    """Ce que le bilan du matin doit dire en PREMIER sur la voie stats : coupure en cours (pourquoi, comment la remettre), ligues coupées, et les ligues en fantôme dont le CLV
    fantôme est devenu positif (intervalle excluant zéro, ≥ 200 lignes) = retour PROPOSÉ, jamais appliqué automatiquement. [] sinon."""
    from . import lanes
    out = []
    try:
        st = lanes.state().get("stats") or {}
        if st.get("mode") == "ghost":
            out.append(f"🛑 Voie stats coupée depuis le {st.get('since')} : {st.get('reason')}. Elle continue en fantôme (mesure seule). Pour la remettre : « voies stats on ».")
        for sk, d in sorted((st.get("leagues") or {}).items()):
            out.append(f"🛑 {sk} en fantôme depuis le {(d or {}).get('since')} : CLV 1xBet {_pc((d or {}).get('mean') or 0)} sur {(d or {}).get('n')} lignes. "
                       f"Pour la remettre : « voies {sk} on ».")
        for sk, (n, m, h, v) in sorted(evaluate()["back"].items()):
            if v == "pos":
                how = f"« voies {sk} on »" if sk in (st.get("leagues") or {}) else f"STATS_ON_LEAGUES={sk} (ou retire-la de STATS_GHOST_LEAGUES)"
                out.append(f"✅ {sk} : CLV 1xBet fantôme {_pc(m)} ± {_pc(h, False)} sur {n} lignes réglées. Retour proposé, à faire à la main : {how}.")
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] lignes de coupure illisibles : {type(e).__name__}: {e}", file=sys.stderr)
    return out
