"""Mesures par voie (sharpgap / stats / fill), partagées par le bilan, le hebdo, les objectifs et la coupure automatique de la voie stats.

CLV 1xBet d'une ligne du journal d'ensemble = log(cote prise / dernier prix 1xBet connu) ; « connu » = relevé à `LEAD_OK_MIN` minutes ou moins du coup d'envoi (au-delà,
ce n'est pas une clôture). Une ligne dont le pari a été retiré par 1xBet (`xgone`) n'a pas de clôture : exclue, et comptée à part. Marges à 95 % PAR MATCH (erreur-type
par grappes : 7 lignes d'un même match ne comptent pas comme 7 preuves). Aucune écriture ici, rien ne lève : un document illisible donne des listes vides."""
import math
import sys
from collections import defaultdict

from . import config as C, ensemble, store

LEAD_OK_MIN = 90
LANES = ("sharpgap", "stats", "stats_cup", "fill", "live", "saison", "saison_cup")        # saison_cup : les jambes `saison` de COUPE (cup = true), séparées au bilan
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


def x_clv(r, taken=None):
    """log(cote prise / dernier prix 1xBet connu) d'une ligne réglée, ou None (pas de prix pris, pas de relevé assez proche, pari retiré par 1xBet). `taken` : le prix PRIS par
    l'appelant (la jambe d'un combiné bâti plus tard n'a pas le prix de la première lecture du journal) ; à défaut, le prix de la ligne."""
    xc = r.get("xclose")
    price = taken if taken else r.get("xodds")
    if (r.get("xgone") or not price or not isinstance(xc, dict) or not xc.get("odds") or xc.get("lead_min") is None
            or not 0 <= xc["lead_min"] <= LEAD_OK_MIN or xc.get("susp")):
        return None                                 # relevé avant le match seulement (pas en jeu), prix jouable (pas suspendu), pari non retiré
    try:
        return math.log(float(price) / float(xc["odds"]))
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

    def put(key, lane, res, day, cup=False):
        if key and res in ("win", "loss") and (day or "") >= cut and key not in out:
            lane = "saison_cup" if lane == "saison" and cup else "stats_cup" if lane == "stats" and cup else lane
            out[key] = (lane if lane in LANES else "legacy", res)

    for r in rows:
        if r.get("sent"):
            put(r.get("key"), r.get("strategy"), r.get("result"), r.get("date"))
    docs = [fair.load_combos(), (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])]
    for combos in docs:
        for c in combos if isinstance(combos, list) else []:
            for leg in c.get("legs") or []:
                put(leg.get("key"), "saison" if leg.get("lane") == "saison" else leg.get("strategy"), leg.get("result"), c.get("date"), bool(leg.get("cup")))      # un remplissage `saison` (strategy « fill ») reste dans la voie saison
    return out


def combo_money(days=None, today=None):
    """{"avec valeur": (gain, nombre, mise), "sans valeur": ..., "avant étiquettes": ...} des combinés NOTÉS et réglés : l'argent d'un combiné n'est jamais réparti
    entre voies (il mélange leurs jambes), il est ventilé par `has_value`."""
    from . import combos as CB, fair, money
    bets = store.load_doc("bets.json", {"combos": []})
    rows = store.load_doc("tracked.json", [])
    out = {}
    for x, g in money._counted(bets, rows if isinstance(rows, list) else [], fair.load_combos(), days, today):
        if not (x.get("legs") is not None or x.get("keys") is not None):
            continue                                    # un pari simple n'est pas un combiné
        if x.get("legs") and (CB.season_no(x.get("tier")) is not None or all(l.get("lane") == "saison" for l in x["legs"])):
            continue                                    # l'argent de la voie `saison` est sur SA ligne (season_detail), jamais dans « avec / sans valeur » des autres voies
        k = "avec valeur" if x.get("has_value") is True else "sans valeur" if x.get("has_value") is False else "avant étiquettes"
        a = out.get(k, (0.0, 0, 0.0))
        out[k] = (a[0] + g, a[1] + 1, a[2] + float((x.get("played") or {}).get("stake") or 0))
    return out


def cup_rows(days=None, today=None):
    """Lignes RÉGLÉES de la voie `stats` portant le label « stats » (valeur) dans le journal des stats de coupe (data/cup_log.json + archive)."""
    from . import cupstats
    cut = _since(days, today)
    return [r for r in cupstats.journal_rows() if isinstance(r, dict) and r.get("strategy") == "stats" and settled(r) and (r.get("date") or "") >= cut]


def cup_line(days=None, today=None, plain=False):
    """« stats (coupes) » : jambes de stats de coupe (fantôme par défaut), réalisé contre annoncé et CLV 1xBet avec son intervalle à 95 % (marges PAR MATCH) sur les lignes réglées ; la règle de
    retour pré-déclarée (CLV >= 0 sur C.STATS_CUPS_BACK_N lignes -> « on » à la main) y est dite. '' sans rien de réglé. Ne lève jamais."""
    try:
        rows = cup_rows(days, today)
        if not rows:
            return ""
        w = sum(1 for r in rows if r["result"] == "win")
        ann = [r["pm"] for r in rows if r.get("pm") is not None]
        n, m, h, _ = clv(rows)
        txt = f"stats (coupes, {C.STATS_CUPS_MODE}) : {w} gagnée{'s' if w > 1 else ''} sur {len(rows)} ({round(w / len(rows) * 100)} %)" + (f" · annoncé {round(sum(ann) / len(ann) * 100)} %" if ann else "")
        if n:
            txt += f" · {'prix pris contre le dernier prix 1xBet' if plain else 'CLV 1xBet'} {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" (sur {n}/{C.STATS_CUPS_BACK_N} lignes)"
            if n >= C.STATS_CUPS_BACK_N and m >= 0 and C.STATS_CUPS_MODE != "on":
                txt += f" · ✅ règle de retour atteinte : à la main, STATS_CUPS_MODE=on"
        return txt
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] ligne des stats de coupe illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return ""


def season_rows(days=None, today=None):
    """Lignes du journal de la voie `saison` qui sont des JAMBES (valeur ou remplissage : `sval`), de la période ; les lignes de référence sans verdict n'en font pas partie."""
    from . import seasonlane
    cut = _since(days, today)
    return [r for r in seasonlane.journal_rows() if isinstance(r, dict) and r.get("sval") is not None and (r.get("date") or "") >= cut]


def season_rows_all(days=None, today=None):
    """Toutes les lignes du journal de la voie `saison` de la période (jambes, références, lignes refusées)."""
    from . import seasonlane
    cut = _since(days, today)
    return [r for r in seasonlane.journal_rows() if isinstance(r, dict) and (r.get("date") or "") >= cut]


def season_detail(days=None, today=None, plain=False, cup=False):
    """Suite de la ligne « saison » du bilan par voie : réalisé contre annoncé (proba retenue) et CLV 1xBet avec son intervalle à 95 % (marges PAR MATCH) des jambes du journal de la voie,
    séparés valeur / remplissage, puis l'argent des combinés notés par palier de cote (« cote 10 : +1 200 FCFA sur 3 »). '' sans rien de réglé ni de noté. Ne lève jamais."""
    try:
        from . import combos as CB, fair, money
        rows = [r for r in season_rows(days, today) if settled(r) and bool(r.get("cup")) == cup]        # championnats et coupes (cup = true) ne se mélangent jamais
        parts = []
        for label, sel in (("valeur", [r for r in rows if r.get("sval") == 1]), ("remplissage", [r for r in rows if r.get("sval") == 0])):
            if not sel:
                continue
            w = sum(1 for r in sel if r["result"] == "win")
            ann = [r["pe"] for r in sel if r.get("pe") is not None]              # la proba RETENUE annoncée ; une ligne sans `pe` (écrite avant ce champ) n'entre pas dans la moyenne annoncée
            txt = f"{label} : {w}/{len(sel)} (réalisé {round(w / len(sel) * 100)} %" + (f" pour {round(sum(ann) / len(ann) * 100)} % annoncé" if ann else "") + ")"
            n, m, h, gone = clv(sel)
            if n:
                txt += f", {'prix pris contre le dernier prix 1xBet' if plain else 'CLV 1xBet'} {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" sur {n}"
            parts.append(txt)
        why = {}
        for r in season_rows_all(days, today):
            if r.get("swhy") and bool(r.get("cup")) == cup:
                why[r["swhy"]] = why.get(r["swhy"], 0) + 1
        if why:
            parts.append("lignes écartées : " + ", ".join(f"{k} {v}" for k, v in sorted(why.items())))
        if cup:
            return (" · " + " · ".join(parts)) if parts else ""             # l'argent des combinés n'est pas séparable par jambe : il reste sur la ligne « saison »
        bets = store.load_doc("bets.json", {"combos": []})
        trk = store.load_doc("tracked.json", [])
        tiers, custom = {}, (0.0, 0)
        for x, g in money._counted(bets, trk if isinstance(trk, list) else [], fair.load_combos(), days, today):
            if x.get("legs") is None:
                continue
            sn = CB.season_no(x.get("tier"))
            if sn is not None:                  # par tranche de cote RÉELLE du combiné (< 5, 5-15, 15-50, > 50), anciens paliers « ss10 » compris (cote visée si la cote réelle manque)
                bucket = CB.odds_bucket(x.get("min_odds") or x.get("odds") or sn) or CB.odds_bucket(sn)
                a = tiers.get(bucket, (0.0, 0))
                tiers[bucket] = (a[0] + g, a[1] + 1)
            elif x["legs"] and all(l.get("lane") == "saison" or l.get("strategy") == "saison" for l in x["legs"]):       # « saison N » : combiné sur mesure bâti sur les seules jambes de la voie
                custom = (custom[0] + g, custom[1] + 1)
        order = [label for _, label in CB.ODDS_BUCKETS]
        notes = [f"cote {k} : {money.signed(round(v[0]))} sur {v[1]}" for k, v in sorted(tiers.items(), key=lambda kv: order.index(kv[0]))] + ([f"sur mesure : {money.signed(round(custom[0]))} sur {custom[1]}"] if custom[1] else [])
        if notes:
            parts.append("combinés notés " + " ; ".join(notes))
        return (" · " + " · ".join(parts)) if parts else ""
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] détail de la voie saison illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return ""


def fmt_odds(x):
    return str(int(x)) if float(x).is_integer() else f"{x:g}".replace(".", ",")


def lane_lines(rows, days=None, today=None, plain=False):
    """Lignes du bilan par voie (texte brut) : jambes réglées (gagnées sur jouées) et CLV 1xBet par voie, puis l'argent des combinés par `has_value`. [] sans rien de réglé."""
    from . import money
    out = []
    legs = leg_results(rows, days, today)
    cut = _since(days, today)
    log = [r for r in journal_rows() if (r.get("date") or "") >= cut]
    for lane in LANES + ("legacy",):
        mine = [res for ln, res in legs.values() if ln == lane]
        if not mine and lane in ("saison", "saison_cup"):
            det = season_detail(days, today, plain, cup=lane == "saison_cup")
            if det:
                out.append(("saison (coupes)" if lane == "saison_cup" else "saison") + " : aucun combiné réglé" + det)
            continue
        if not mine:
            continue
        w = sum(1 for res in mine if res == "win")
        txt = f"{ {'legacy': 'avant les étiquettes', 'saison_cup': 'saison (coupes)', 'stats_cup': 'stats (coupes, combinés)'}.get(lane, lane) } : {w} gagnée{'s' if w > 1 else ''} sur {len(mine)} ({round(w / len(mine) * 100)} %)"
        if lane in ("sharpgap", "stats"):
            n, m, h, gone = clv([r for r in log if r.get("strategy") == lane and settled(r)])
            if n:
                txt += f" · {'prix pris contre le dernier prix 1xBet' if plain else 'CLV 1xBet'} {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" (sur {n} lignes du journal)"
        if lane == "live":
            txt += " · hors voies (dimensionnement d'avant) ; " + live_calib_text(days, today)
        if lane in ("saison", "saison_cup"):
            txt += season_detail(days, today, plain, cup=lane == "saison_cup")
        out.append(txt)
    cl = cup_line(days, today, plain)
    if cl:
        out.append(cl)
    try:
        from . import oddsonly
        out += oddsonly.lane_lines(days, today)       # voie « cotes » : fantôme, aucun pari ; ventilée par mécanisme et famille
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] ligne cotes illisible : {type(e).__name__}: {e}", file=sys.stderr)
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
        la = live_alert()
        if la:
            out.append(la)
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] lignes de coupure illisibles : {type(e).__name__}: {e}", file=sys.stderr)
    return out


def unlabeled(today=None):
    """(sans étiquette, total) des lignes du jour qui portent un prix 1xBet : jambes des combinés du jour (les jambes `live` ont leur propre étiquette : elles comptent comme étiquetées) +
    lignes du journal d'ensemble du jour. Une ligne créée après le lot des étiquettes complètes en a toujours une (fill à défaut) ; seules les lignes d'avant (legacy) peuvent
    en manquer. Documents abîmés : les éléments illisibles sont ignorés."""
    from datetime import date
    from . import fair
    day = str(today or date.today())[:10]
    legs = []
    for combos in (fair.load_combos(), (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])):
        for c in combos if isinstance(combos, list) else []:
            if isinstance(c, dict) and str(c.get("date") or "")[:10] == day:
                legs += [l for l in c.get("legs") or [] if isinstance(l, dict) and l.get("xodds")]
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
    """[(jambe, combiné, CLV 1xBet ou None)] des jambes des combinés (à vérifier, sur mesure, ET réels de bets.json) de la période : CLV = log(prix 1xBet de la jambe / dernier prix
    1xBet connu de sa ligne). Le dernier prix vient de la ligne du journal d'ensemble de même clé ; le prix de la jambe est celui du COMBINÉ (leg["xodds"], ou x["xodds"] de
    `xlegs` pour un combiné de bets.json) : une jambe sans prix pris n'est PAS mesurée (jamais le prix de la première lecture du journal, que personne n'a pris). None aussi sans
    ligne au journal (clé changée) ou sans relevé assez proche. Une jambe présente dans plusieurs combinés compte une fois par combiné."""
    from . import fair
    cut = _since(days, today)
    from . import seasonlane
    by_key = {r.get("key"): r for r in journal_rows() if r.get("key")}
    by_key |= {r.get("key"): r for r in seasonlane.journal_rows() if r.get("key")}        # clés distinctes (« |saison ») : jamais d'écrasement d'une autre voie
    from . import cupstats
    by_key |= {r.get("key"): r for r in cupstats.journal_rows() if r.get("key") and r.get("key") not in by_key}      # stats de coupe : leur propre journal (ids de coupe, jamais ceux d'un championnat)
    out = []
    bets = (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])
    for combos in (fair.load_combos(), bets, fair.load_ghost()):
        for c in combos if isinstance(combos, list) else []:
            if not isinstance(c, dict) or (c.get("date") or "") < cut or c.get("closed"):
                continue                                        # un combiné remplacé par une reconstruction (jamais joué) ne compte pas comme prix pris
            legs = [l for l in c.get("legs") or [] if isinstance(l, dict)]
            if not legs:                                        # combiné réel de bets.json : ses jambes n'ont que {clé, prix, référence} (xlegs) ; la voie vient du journal
                legs = [{"key": x.get("key"), "xodds": x.get("xodds"), "id": str(x.get("key") or "").split("|")[0],
                         "strategy": (by_key.get(x.get("key")) or {}).get("strategy"), "cup": (by_key.get(x.get("key")) or {}).get("cup")} for x in c.get("xlegs") or [] if isinstance(x, dict)]
            for leg in legs:
                if leg.get("key"):
                    row = by_key.get(leg["key"])
                    out.append((leg, c, x_clv(row, leg.get("xodds")) if row and leg.get("xodds") else None))
    return out


def _tier_name(c):
    """Palier lisible : titre d'un combiné de bets.json (« 🛡️ SÛR »), « cote N » pour un combiné à cote juste (palier numérique)."""
    from . import fmt
    t = c.get("tier")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return f"cote {fmt.trim(t, 1)}"
    try:
        return fmt.tier_name(t)
    except Exception:                           # noqa: BLE001
        return str(t)


def leg_clv_lines(days=None, today=None):
    """Lignes du bilan détaillé (texte brut) : CLV 1xBet par jambe des combinés, par voie — les jambes `fill` à part — et part des jambes à CLV négatif par palier.
    La couverture (jambes mesurées / jambes) est toujours dite : une jambe sans relevé proche n'est pas comptée comme neutre. [] sans combiné."""
    rows = leg_clv_rows(days, today)
    if not rows:
        return []
    live = [r for r in rows if (r[0].get("strategy") or None) == "live"]
    is_sea = lambda l: l.get("lane") == "saison" or l.get("strategy") == "saison"      # noqa: E731 - les remplissages de la voie portent strategy « fill » et lane « saison »
    sea = [r for r in rows if is_sea(r[0])]
    rows = [r for r in rows if (r[0].get("strategy") or None) != "live" and not is_sea(r[0])]          # les jambes live (hors voies, jamais au journal d'ensemble) et `saison` (journal à part) ne gonflent pas la couverture : elles ont leur ligne
    out = [f"jambes mesurées : {sum(1 for _, _, v in rows if v is not None)} / {len(rows)} (prix 1xBet pris contre dernier prix connu, relevé ≤ {LEAD_OK_MIN} min avant le match ; "
           "une jambe sans prix pris, sans ligne au journal ou sans relevé proche n'est pas mesurée : seules les lignes restées les mêmes sont vues)"]
    for lane in ("sharpgap", "stats", "stats_cup", "fill", None):
        mine = [(l, c, v) for l, c, v in rows if (l.get("strategy") or None) == ("stats" if lane == "stats_cup" else lane) and v is not None
                and (lane not in ("stats", "stats_cup") or bool(l.get("cup")) == (lane == "stats_cup"))]      # seule la voie stats sépare les coupes
        if not mine:
            continue
        n, m, h = stats([v for _, _, v in mine], [l.get("id") or c.get("date") for l, c, _ in mine])
        pos = sum(1 for _, _, v in mine if v > 0)
        name = {"sharpgap": "sharpgap", "stats": "stats", "stats_cup": "stats (coupes, à part)", "fill": "fill (à part : jambes sans valeur)", None: "avant les étiquettes"}[lane]
        out.append(f"{name} : {n} jambes · CLV {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "") + f" · {round(pos / n * 100)} % battent le dernier prix"
                   + ("" if n >= 30 else " (trop peu pour conclure)"))
    for label, grp in (("saison (à part : voie propre)", [x for x in sea if not x[0].get("cup")]), ("saison, coupes (à part)", [x for x in sea if x[0].get("cup")])):
        if not grp:                             # la voie `saison` à part (championnats puis coupes) : couverture et CLV ne se mélangent à ceux d'aucune autre voie
            continue
        meas = [(l, v) for l, _, v in grp if v is not None]
        if meas:
            n, m, h = stats([v for _, v in meas], [l.get("id") for l, _ in meas])
            pos = sum(1 for _, v in meas if v > 0)
            out.append(f"{label} : {len(meas)} mesurées sur {len(grp)} jambes · CLV {_pc(m)}" + (f" ± {_pc(h, False)}" if h is not None else "")
                       + f" · {round(pos / n * 100)} % battent le dernier prix" + ("" if n >= 30 else " (trop peu pour conclure)"))
        else:
            out.append(f"{label} : {len(grp)} jambes, aucune mesurée")
    if live:                                    # toujours dite, mesurée ou non : le live est visible à part, hors voies
        meas = [v for _, _, v in live if v is not None]
        out.append(f"live (à part : hors voies) : {len(live)} jambes, " + (f"{len(meas)} mesurées" if meas else live_calib_text(days, today)))
    try:
        from . import fair
        g = [r for r in fair.load_ghost() if (r.get("date") or "") >= _since(days, today)]
        if g:
            out.append(f"{len(g)} combiné{'s' if len(g) > 1 else ''} sans valeur suivi{'s' if len(g) > 1 else ''} en fantôme (non envoyé{'s' if len(g) > 1 else ''}) : leurs jambes sont comptées ci-dessus")
    except Exception:                           # noqa: BLE001
        pass
    tiers = {}
    for l, c, v in rows:
        if v is not None and (l.get("strategy") or None) not in ("fill", "live", "saison") and l.get("lane") != "saison":
            tiers.setdefault(_tier_name(c), []).append(v)
    for t, vs in tiers.items():
        neg = sum(1 for v in vs if v < 0)
        out.append(f"palier {t} : {round(neg / len(vs) * 100)} % des jambes à valeur à CLV négatif ({neg}/{len(vs)})")
    return out


LIVE_ALERT_N = 200                             # jambes live réglées avant de rendre un verdict


def live_calib_rows(days=None, today=None):
    """[(id du match, chance annoncée, 1 si gagnée sinon 0)] des jambes `live` RÉGLÉES (paris suivis envoyés + jambes des combinés), une fois par jambe (clé). Aucune requête : les
    résultats existent déjà. Une jambe sans chance annoncée n'est pas comptée."""
    from . import fair
    cut = _since(days, today)
    out, seen = [], set()

    def put(leg, day):
        k = leg.get("key")
        p = leg.get("prob")
        if (k and k not in seen and leg.get("strategy") == "live" and leg.get("result") in ("win", "loss") and (day or "") >= cut
                and isinstance(p, (int, float)) and not isinstance(p, bool) and 0 < p < 1):
            seen.add(k)
            out.append((leg.get("id") or str(k).split("|")[0], float(p), 1 if leg["result"] == "win" else 0))

    for r in store.load_doc("tracked.json", []):
        if isinstance(r, dict) and r.get("sent"):
            put(r, r.get("date"))
    docs = [fair.load_combos(), (store.load_doc("bets.json", {"combos": []}) or {}).get("combos", [])]
    for combos in docs:
        for c in combos if isinstance(combos, list) else []:
            for leg in (c.get("legs") or []) if isinstance(c, dict) else []:
                if isinstance(leg, dict):
                    put(leg, c.get("date"))
    return out


def live_calib(days=None, today=None):
    """(n, taux annoncé, taux réalisé, écart réalisé − annoncé, demi-marge à 95 % de l'écart) des jambes live réglées ; marges PAR MATCH (les jambes d'un même match sont corrélées).
    (0, None, None, None, None) sans jambe."""
    rows = live_calib_rows(days, today)
    if not rows:
        return 0, None, None, None, None
    n, d, h = stats([y - p for _, p, y in rows], [i for i, _, _ in rows])
    return n, sum(p for _, p, _ in rows) / n, sum(y for _, _, y in rows) / n, d, h


def live_calib_text(days=None, today=None):
    """« CLV non mesurable (pas de clôture relevée), calibration n/200 » (+ annoncé / réalisé / écart dès qu'il y a des jambes réglées)."""
    n, pa, pr, d, h = live_calib(days, today)
    txt = f"CLV non mesurable (pas de clôture relevée), calibration {n}/{LIVE_ALERT_N}"
    if n:
        txt += f" : annoncé {round(pa * 100)} %, réalisé {round(pr * 100)} %" + (f", écart {_pc(d)} ± {_pc(h, False)}" if h is not None else "") + ("" if n >= LIVE_ALERT_N else " (trop peu pour conclure)")
    return txt


def live_alert():
    """Règle du 14/10, remplacée le 15/10 (aucun prix de clôture live n'est relevé, le CLV live n'est pas mesurable) : taux RÉALISÉ contre taux ANNONCÉ des jambes `live` réglées. Sur au moins
    LIVE_ALERT_N jambes, si le réalisé est sous l'annoncé avec un IC à 95 % (par match) qui exclut zéro, on REVIENT vers l'utilisateur (bilan du matin) ; AUCUNE coupure automatique du
    live. Aucune requête. -> ligne de texte ou ''."""
    try:
        n, pa, pr, d, h = live_calib()
        if n >= LIVE_ALERT_N and verdict(n, d, h, LIVE_ALERT_N) == "neg":
            return (f"⚠️ Jambes live : réalisé {round(pr * 100)} % contre {round(pa * 100)} % annoncé ({_pc(d)} ± {_pc(h, False)}) sur {n} jambes, intervalle sous zéro. "
                    "Décision à prendre avec toi (aucune coupure automatique du live).")
    except Exception as e:                      # noqa: BLE001
        print(f"[lanes] alerte live illisible : {type(e).__name__}: {e}", file=sys.stderr)
    return ""
