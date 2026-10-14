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
    """Évalue et écrit la coupure automatique, LE LUNDI MATIN SEULEMENT, et ne la prononce que si la condition tient sur DEUX LUNDIS CONSÉCUTIFS (à 7 jours exactement : un lundi
    manqué casse la chaîne). Même règle pour la proposition de retour d'une ligue (`back_confirmed`). Ne fait rien si STRATEGY_STATS n'est pas « on ». Une voie coupée ne revient qu'à
    la main. -> liste des coupures décidées cette fois (texte brut) ; ne lève jamais."""
    from datetime import date, timedelta
    from . import config as C, lanes
    try:
        if C.STRATEGY_STATS != "on":
            return []
        day = date.fromisoformat(str(today or date.today())[:10])
        if day.weekday() != 0:
            return []
        doc = dict(lanes.state())
        st = dict(doc.get("stats") or {})
        prev = st.get("watch") if isinstance(st.get("watch"), dict) else {}
        week_ago, iso = str(day - timedelta(days=7)), str(day)
        watch = {}

        def twice(key):
            watch[key] = iso
            return prev.get(key) == week_ago

        cut = st.get("mode") == "ghost"
        ev = evaluate()
        new = []
        n, m, h, v = ev["global"]
        if not cut and v == "neg" and twice("global"):
            tot = len([r for r in _eligible(journal_rows()) if not lanes.league_ghost(r.get("sport_key")) and r.get("sport_key")])
            st.update(mode="ghost", since=iso, reason=f"CLV 1xBet {_pc(m)} ± {_pc(h, False)} sur {n} lignes avec relevé 1xBet ≤ {LEAD_OK_MIN} min avant le match (sur {tot} réglées), "
                      "constaté deux lundis de suite", n=n, mean=round(m, 4), half=round(h, 4))
            new.append(f"voie stats coupée : {st['reason']}")
            cut = True
        if not cut:
            leagues = dict(st.get("leagues") or {})
            for sk, (n, m, h, v) in sorted(ev["leagues"].items()):
                if v == "neg" and twice("league:" + sk):
                    leagues[sk] = {"since": iso, "n": n, "mean": round(m, 4), "half": round(h, 4)}
                    new.append(f"ligue {sk} en fantôme : CLV 1xBet {_pc(m)} ± {_pc(h, False)} sur {n} lignes, constaté deux lundis de suite")
            if new:
                st["leagues"] = leagues
        back = dict(st.get("back_confirmed") or {}) if isinstance(st.get("back_confirmed"), dict) else {}
        for sk, (n, m, h, v) in sorted(ev["back"].items()):
            if v == "pos" and twice("back:" + sk):
                back[sk] = {"since": back.get(sk, {}).get("since", iso) if isinstance(back.get(sk), dict) else iso, "n": n, "mean": round(m, 4), "half": round(h, 4)}
            elif v != "pos":
                back.pop(sk, None)
        st["back_confirmed"] = back
        st["watch"] = watch
        if st != dict(doc.get("stats") or {}):
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
        back = st.get("back_confirmed") if isinstance(st.get("back_confirmed"), dict) else {}
        for sk, d in sorted(back.items()):                               # confirmé deux lundis de suite (apply) ; plus rien à évaluer ici
            if lanes.league_ghost(sk) and isinstance(d, dict):
                how = f"« voies {sk} on »" if sk in (st.get("leagues") or {}) else f"STATS_ON_LEAGUES={sk} (ou retire-la de STATS_GHOST_LEAGUES)"
                out.append(f"✅ {sk} : CLV 1xBet fantôme {_pc(d.get('mean') or 0)} ± {_pc(d.get('half') or 0, False)} sur {d.get('n')} lignes réglées, deux lundis de suite. "
                           f"Retour proposé, à faire à la main : {how}.")
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] lignes de coupure illisibles : {type(e).__name__}: {e}", file=sys.stderr)
    return out


def unlabeled(today=None):
    """(sans étiquette, total) des lignes du jour qui portent un prix 1xBet : jambes des combinés du jour (hors jambes LIVE : leur voie n'est pas définie, à étiqueter plus tard) +
    lignes du journal d'ensemble du jour. Une ligne créée après le lot des étiquettes complètes en a toujours une (fill à défaut) ; seules les lignes d'avant (legacy) peuvent
    en manquer. Documents abîmés : les éléments illisibles sont ignorés."""
    from datetime import date
    from . import fair
    day = str(today or date.today())[:10]
    legs = []
    for combos in (fair.load_combos(), (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])):
        for c in combos if isinstance(combos, list) else []:
            if isinstance(c, dict) and str(c.get("date") or "")[:10] == day:
                legs += [l for l in c.get("legs") or [] if isinstance(l, dict) and l.get("xodds") and not l.get("live")]
    rows = [r for r in journal_rows() if isinstance(r, dict) and r.get("date") == day and r.get("xodds")]
    allx = legs + rows
    return sum(1 for x in allx if not x.get("strategy")), len(allx)


def unlabeled_line(today=None):
    """« jambes sans étiquette : n / total (lignes du jour) » ; '' ensemble éteint (aucune étiquette n'existe alors) ou document illisible."""
    from . import config as C
    if not C.ENSEMBLE_ENABLED:
        return ""
    try:
        n, total = unlabeled(today)
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] compteur d'étiquettes illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return ""
    return f"jambes sans étiquette : {n} / {total} (lignes du jour)"


# ---------- CLV par jambe des combinés ----------

def leg_clv_rows(days=None, today=None):
    """[(jambe, combiné, CLV 1xBet ou None)] des jambes des combinés (à vérifier + notés) de la période : CLV = log(prix 1xBet pris / dernier prix 1xBet connu) de la ligne du journal
    d'ensemble ayant la même clé (relevé ≤ 90 min avant le match, voir x_clv) ; None si la jambe n'a pas de ligne au journal ou pas de relevé assez proche. Une jambe présente dans
    plusieurs combinés du jour compte une fois par combiné (c'est le combiné qui la porte)."""
    from . import fair
    cut = _since(days, today)
    by_key = {r.get("key"): r for r in journal_rows() if r.get("key")}
    out = []
    for combos in (fair.load_combos(), (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])):
        for c in combos if isinstance(combos, list) else []:
            if not isinstance(c, dict) or (c.get("date") or "") < cut:
                continue
            for leg in c.get("legs") or []:
                if isinstance(leg, dict) and leg.get("key"):
                    row = by_key.get(leg["key"])
                    out.append((leg, c, x_clv(row) if row else None))
    return out


def _tier_name(c):
    from . import fmt
    try:
        return fmt.tier_name(c.get("tier"))
    except Exception:                           # noqa: BLE001
        return str(c.get("tier"))


def leg_clv_lines(days=None, today=None):
    """Lignes du bilan détaillé (texte brut) : CLV 1xBet par jambe des combinés, par voie — les jambes `fill` à part — et part des jambes à CLV négatif par palier.
    La couverture (jambes mesurées / jambes) est toujours dite : une jambe sans relevé proche n'est pas comptée comme neutre. [] sans combiné."""
    rows = leg_clv_rows(days, today)
    if not rows:
        return []
    out = [f"jambes mesurées : {sum(1 for _, _, v in rows if v is not None)} / {len(rows)} (prix 1xBet pris contre dernier prix connu, relevé ≤ {LEAD_OK_MIN} min avant le match)"]
    for lane in ("sharpgap", "stats", "fill", None):
        mine = [(l, c, v) for l, c, v in rows if (l.get("strategy") or None) == lane and v is not None]
        if not mine:
            continue
        n, m, h = stats([v for _, _, v in mine], [l.get("id") or c.get("date") for l, c, _ in mine])
        pos = sum(1 for _, _, v in mine if v > 0)
        name = {"sharpgap": "sharpgap", "stats": "stats", "fill": "fill (à part : jambes sans valeur)", None: "avant les étiquettes"}[lane]
        out.append(f"{name} : {n} jambes · CLV {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" · {round(pos / n * 100)} % battent le dernier prix"
                   + ("" if n >= 30 else " (trop peu pour conclure)"))
    tiers = {}
    for l, c, v in rows:
        if v is not None and (l.get("strategy") or None) != "fill":
            tiers.setdefault(_tier_name(c), []).append(v)
    for t, vs in tiers.items():
        neg = sum(1 for v in vs if v < 0)
        out.append(f"palier {t} : {round(neg / len(vs) * 100)} % des jambes à valeur à CLV négatif ({neg}/{len(vs)})")
    return out
