"""Combinés à la demande (combi N, live N, types, plafonds) : issus du découpage d'app.py (le code lit app via A.nom)."""

import math
import sys
import time
from . import config as C, combos as CB, budget, chat, custom, exposure, fmt, lanes, money, odds, seasonlane, statswindow, store, telegram, xprice
from .sources import http, state, xbet
from .telegram import Html
from datetime import datetime, timedelta, timezone
from ._appref import A


def custom_combo(picks, pronos, target, day, started=False, when=None, kinds=None, bold=False, now=None, avoid=None, again=None, nlegs=None):
    """/combi <cote> : tout est joué chez 1xBet, donc aux cotes 1xBet. Dès que les cotes 1xBet du jour sont connues, combiné aux cotes
    1xBet (pronostics 💎 ou paris réels remis à leur prix 1xBet, code coupon) ; jamais la cote typique d'un autre bookmaker : si aucun
    combiné n'atteint la cote visée, la réponse donne la plus haute cote atteignable aux cotes 1xBet. Sans AUCUNE cote 1xBet (relevé en
    panne) : combiné à cote juste (pronostics de Kelly, « à vérifier chez 1xBet », cote mini) d'abord, puis combiné aux cotes de référence
    des autres bookmakers, annoncé comme tel et sans code coupon ; sinon l'explication précise de ce qui manque et une cote atteignable.
    `picks` et `pronos` : matchs pas encore commencés seulement ; `started` : il y en avait d'autres, déjà commencés. `when` : heure
    « HH:MM » du relevé 1xBet des paris réels. Un combiné proposé aux cotes de 1xBet (ou à cote mini) part avec le bouton « ✅ Je le joue »
    (kelly/custom.py) : il n'est suivi, réglé et compté dans le bilan qu'après cet appui ; pas de bouton sous les cotes de référence.
    `kinds` : types de paris choisis (codes de combos.KINDS : « combi 20 tirs fautes », cases à cocher), None = tous. Un combiné est bâti soit
    sur les pronostics, soit sur les paris réels, jamais les deux : des types pris des deux côtés sont dits « pas mélangeables » s'il échoue.
    Types stats (tirs, fautes...) : les matchs de la fenêtre des stats que 1xBet n'avait pas encore chiffrés sont d'abord relus en direct (_open_stats).
    `bold` (« 🎲 Aller plus loin quand même », « combi 1900 tirs audacieux ») : complété avec des paris moins solides (_bold_combo).
    Tes tickets joués du jour (kelly/exposure.py) ne retirent rien : chaque match qui y est déjà porte « 🔗N » (_shared_counts).
    `avoid` (« 🔀 Un autre sans mes matchs ») : ids de matchs exclus, jusque dans la vérification en direct (_live_reply) ; `again` : le combi dont on a
    touché « 🔀 » (un échec propose « 🔀 Cote X sans mes matchs » au plus haut atteignable, et « 🎯 Cote N avec mes matchs »). `nlegs` : (N, exact) —
    au plus N paris (« combi 20 max 4 », case « ≤ 4 »), ou exactement N (« combi 20 pile 4 ») ; un échec dit la plus haute cote en N paris et propose
    « ➕ Autoriser plus de paris »."""
    n_max, exact = (nlegs or (None, False))
    mx, mn = chat.legs_bounds(n_max, exact)        # au plus / pile / au moins (« combi 100 min 15 »)
    lim = {"max_legs": mx, "min_legs": mn}
    t = fmt.trim(target, 1)
    prefix = CB.kinds_prefix(kinds)
    only = CB.kinds_label(kinds)
    now = now or A.now_utc()
    if prefix or bold:                              # paris stats que 1xBet vient d'ouvrir (quelques heures avant les matchs)
        pronos = A._open_stats(pronos, prefix, now)
    if avoid:                                       # « 🔀 Un autre sans mes matchs » : ni tes matchs joués ni ceux du combiné affiché
        pronos, picks = [c for c in pronos if c["id"] not in avoid], [c for c in picks if c["id"] not in avoid]
    # tes tickets joués du jour ne retirent rien (les meilleurs paris restent proposés) : le message dit seulement lesquels y sont déjà (_shared_counts)
    left = (f"🔀 Sans {'le match' if len(avoid) == 1 else f'les {len(avoid)} matchs'} de tes tickets du jour et du combiné précédent."
            if avoid else "")
    if bold:
        return A._bold_combo(picks, pronos, target, day, kinds, now, usage=A.day_usage(day), note=left, nlegs=nlegs)
    real = "Pas de cotes réelles aujourd'hui" if not started else "Plus de cotes réelles jouables aujourd'hui (matchs commencés)"
    reach = []                                      # cotes les plus hautes atteignables (réelles, puis cote mini des pronostics)
    xp, xk = any(c.get("xodds") for c in pronos), any(c.get("xodds") for c in picks)
    # un combiné proposé (pas les réponses « impossible » ni les cotes de référence) part avec « ✅ Je le joue » (kelly/custom.py)
    split = A._kinds_split(picks, pronos, kinds) if prefix else ""
    note = "\n".join(x for x in (split, left) if x)
    keep = {"day": day, "target": target, "lo": target, "hi": target * (1 + C.CUSTOM_TOLERANCE)} | ({"kinds": list(kinds)} if prefix else {}) \
        | ({"note": note} if note else {}) | ({"nlegs": [n_max, exact]} if n_max else {})
    if xp:                                          # cotes 1xBet du jour connues : le combiné se joue chez 1xBet, à SES cotes
        used = A.day_usage(day)
        fc = CB.fair_custom(pronos, target, usage=used, prefix=prefix, **lim)
        if fc:
            return A._live_reply(fc, target, target * (1 + C.CUSTOM_TOLERANCE), keep=keep, usage=used, avoid=avoid)
    if not (xp or xk):                              # aucune cote 1xBet : d'abord les pronostics de Kelly (cote mini), jamais un prix inventé
        fc = CB.fair_custom(pronos, target, usage=A.day_usage(day), prefix=prefix, **lim)
        if fc:
            why = real if not picks else "Kelly n'a vu aucune cote de 1xBet aujourd'hui"
            return A._fair_reply(fc, f"{why} : voici un combiné bâti sur les pronostics de Kelly (cote mini d'au moins {t}), à vérifier "
                                   "chez 1xBet." + A._note(keep), budget=True, keep=keep,
                               shared=A._shared_counts(day, fc[0]))
    if picks and (xk or not xp):                    # paris réels : à leur prix 1xBet s'il existe, sinon cotes de référence (annoncées)
        usage, n_sent = A._todays_usage(day, picks)
        combo = CB.custom(picks, target, usage, prefix=prefix, **lim)
        pool = [c for c in CB.make_pool(picks) if not prefix or c["market"].startswith(prefix)]
        if combo:
            picks_, odds_, prob = combo
            if money.active():
                st = A._budget_stake(prob, odds_, "combo", "🎯", legs=picks_)
                txt = A._stake_words(st, prob, odds_, picks_)
            else:
                st, txt = None, None                # pas de budget du jour : aucune mise conseillée (le message dit de le régler)
            xcombo = all(c.get("xref") and c.get("xodds") for c in picks_)
            code = xbet.save_coupon(picks_) if xcombo else None
            msg = fmt.combo("🎯 COMBINÉ SUR MESURE" + (f" · {only.upper()}" if only else ""), picks_, odds_, prob, reasons={c["key"]: CB.leg_reason(c) for c in picks_}, stake_txt=txt,
                            coupon=code, when=when, retry="redemande le combi" if xcombo and not code else None, shared=A._shared_counts(day, picks_))
            if not xk:                              # ni 1xBet ni code : les cotes affichées sont celles des autres bookmakers
                msg = Html(msg + "\n\n" + fmt.i("⚠️ Ce sont des cotes de référence (pas celles de 1xBet) : vérifie chaque cote chez 1xBet "
                                                "avant de jouer, la cote totale chez lui peut être plus basse."))
            if CB.ev(combo) < C.COMBO_MIN_EV:
                msg = Html(msg + "\n\n" + fmt.i(f"⚠️ Espérance négative : à ces cotes, ce combiné rend en moyenne "
                                                f"{fmt.pct(1 - CB.ev(combo))} de moins que la mise. "
                                                + ("Kelly ne conseille pas d'y miser : vise une autre cote." if money.active() and not money.has_edge(prob, odds_)
                                                   else "Mise petit, ou vise une autre cote.")))
            if note:
                msg = Html(msg + "\n\n" + fmt.i(fmt.esc(note.strip())))
            if n_sent:
                msg = Html(msg + "\n\n" + fmt.i(f"Un pari déjà dans tes tickets du jour n'est repris que s'il est sûr "
                                                f"({fmt.pct(C.LEG_SAFE_PROB)} de chances ou plus : {C.LEG_REUSE_MAX} tickets au plus, {C.LEG_REUSE_VERY_SAFE} dès {fmt.pct(C.LEG_VERY_SAFE_PROB)})."))
            if all(c.get("xodds") for c in picks_):        # paris réels remis au prix 1xBet : suivi possible (cotes de référence : non)
                cid = custom.remember(day, target, target * (1 - C.CUSTOM_TOLERANCE), target * (1 + C.CUSTOM_TOLERANCE), combo, st,
                                      money.cur() if st is not None else None, code, when, src="picks", kinds=kinds if prefix else None, nlegs=nlegs)
                msg = custom.with_button(msg, cid, day, code)
            return msg
        free = usage.filter(pool)
        best = {}
        for c in free:
            best[c["id"]] = max(best.get(c["id"], 1.0), c["odds"])
        if len(best) >= 2:
            reach.append(CB.reach(free, n_max))
        if pool and not free:                       # les bons paris réels (de ce type) sont déjà dans les combinés du matin
            real = (f"ℹ️ Les {len(pool)} bons paris réels du jour{' (' + only + ')' if only else ''} sont déjà dans tes combinés du matin"
                    + (" ; le 1N2 est très bien estimé par les bookmakers, Kelly y trouve rarement de l'avantage." if "v" in (kinds or ()) else "."))
        else:
            real = ""
    else:
        real = ""
    if n_max and exact == "min" and not avoid:      # au moins N : le trou est comblé avec les paris les plus probables après les solides
        filled = A._bold_combo(picks, pronos, target, day, kinds, now, usage=A.day_usage(day), note=note, nlegs=nlegs, fill=True)
        if filled:
            return filled
    if n_max and not avoid:                         # nombre de paris choisi : la vraie raison en une phrase, et les solutions en boutons
        return A._legs_failure(picks, pronos, target, kinds, prefix, n_max, exact)
    sf = CB.fair_shortfall(pronos, target, prefix=prefix)
    if sf["matches"] >= 2:
        reach.append(CB.reach([c for c in CB.fair_legs(pronos) if not prefix or c["market"].startswith(prefix)], n_max) if n_max else sf["best_total"])
    top = max(reach, default=1.0)
    hi = fmt.trim(target * (1 + C.CUSTOM_TOLERANCE), 1)
    lines = ([note] if note else []) + [f"❌ Pas de combiné à la cote {t}" + (f" ({only})" if only else "")
                                        + (" sans tes matchs" if avoid else "") + (f" {chat.legs_label(n_max, exact)}" if n_max else "") + "."]
    if top >= target:
        lines.append(f"📉 Les bons paris existent, mais aucune combinaison ne tombe entre {t} et {hi} (un pari par match).")
    elif top > 1:
        n_m = max(sf["matches"], 2)
        lines.append(f"📉 Avec les bons paris du jour, la cote monte au plus à {fmt.trim(top, 1)} ({n_m} matchs).")
    else:
        lines.append("📉 Aucun pari assez solide en ce moment" + (f" ({only})" if only else "") + ".")
    if real:
        lines.append(real)
    if started and not picks:                       # tous les matchs à cotes réelles ont commencé
        lines.append("ℹ️ Les matchs du jour à cotes réelles ont commencé : il ne reste que les pronostics de Kelly.")
    closed = A._not_open(pronos, prefix, now)
    if closed:
        lines.append(closed)
    codes = ".".join(k for k in CB.KIND_CODES if k in set(kinds or ()))
    low = math.floor(top * 10) / 10
    best_ok = fmt.trim(low, 1) if 2.0 <= low < target else None
    rows = []
    if avoid:
        rows += ([[(f"🔀 Cote {best_ok} sans mes matchs", f"cnew|{again}|{str(day)[:10]}|{best_ok}")]] if best_ok and again else []) \
            + [[(f"🎯 Cote {t} avec mes matchs", f"cgo|{t}|{codes}"
                 + (f"|{'=' if exact and exact != 'min' else ''}{n_max}{'+' if exact == 'min' else ''}" if n_max else ""))]]
        return telegram.with_buttons("\n".join(lines), telegram.inline(rows))
    if best_ok:
        rows.append([(f"🎯 Cote {best_ok}" + (f" ({only})" if only else ""), f"cgo|{best_ok}|{codes}|")])
    bold_max = max((A._reach(legs) for legs in A._bold_pools(picks, pronos, prefix)), default=1.0)
    if bold_max >= target:                          # des paris moins solides suffiraient : proposé, jamais imposé
        lines.append(f"💡 Tu as le choix de forcer l'analyse pour dénicher les événements les plus proches statistiquement (bouton 🎲 ci-dessous ou « combi {t} forcer »).")
        rows.append([("🎲 Plus risqué, même cote", f"cplus|{t}|{codes}")])
    elif bold_max > max(top, 2.0) * 1.2:
        b_low = fmt.trim(math.floor(bold_max * 10) / 10, 1)
        lines.append(f"💡 Tu peux dénicher les événements les plus proches jusqu'à la cote {b_low} (bouton 🎲 ou « combi {b_low} forcer »).")
        rows.append([(f"🎲 Plus risqué : jusqu'à {b_low}", f"cplus|{b_low}|{codes}")])
    if prefix:
        rows.append([("🔄 Tous les types de paris", f"cgo|{t}||")])
    if not rows:
        lines.append("🕒 Réessaie plus tard (de nouveaux paris s'ouvrent dans la journée) ou demain.")
    return telegram.with_buttons("\n".join(lines), telegram.inline(rows)) if rows else "\n".join(lines)


def _open_stats(pronos, prefix, now):
    """« combi N » avec des types stats (tirs, tirs cadrés, fautes, touches, hors-jeu ; tous si `prefix` est None) : 1xBet n'ouvre ces paris que
    quelques heures avant les matchs, le relevé du matin les a presque tous vus fermés. Les matchs qui commencent dans la fenêtre des stats
    (statswindow.in_window) et dont un pronostic de ces types n'a pas de cote 1xBet sont relus EN DIRECT (xprice.live_check, C.COMBI_OPEN_MAX au
    plus, les plus proches d'abord, jamais au-delà du plafond mensuel de la fenêtre). -> les pronostics à venir, relus ou non. Ne lève jamais."""
    pre = tuple(p for p in (prefix or A.STAT_PREFIX) if p in A.STAT_PREFIX)
    if not pre or not C.XBET_ENABLED:
        return pronos
    ids, clock = [], time.time()
    # seulement les pronostics SOLIDES en attente (_pending), de matchs connus de 1xBet, pas relus depuis le rythme du passage horaire (retry_min)
    for c in sorted(A._pending(pronos, now), key=lambda c: c.get("commence") or ""):
        last = A._OPEN_READ.get(c["id"])
        if (c["market"].startswith(pre) and c.get("xgid") and c["id"] not in ids and statswindow.in_window(c, now)
                and not (last and clock - last < statswindow.retry_min(c, now) * 60)):
            ids.append(c["id"])
    ids = ids[:C.COMBI_OPEN_MAX]
    if not ids:
        return pronos
    try:
        if not statswindow.month_ok():
            return pronos
        label = CB.kinds_label([CB.kind_of(p) for p in pre]) or "stats"
        telegram.send(f"⏳ Je regarde chez 1xBet si les paris {label} de {len(ids)} match{'s' if len(ids) > 1 else ''} viennent d'ouvrir…", queue=False, silent=True)
        fresh, got, _ = xprice.live_check(set(ids), now, fresh_min=C.STATS_WINDOW_FRESH_MIN)
        A._OPEN_READ.update({i: clock for i in got})      # horodatés APRÈS la lecture (matchs réellement relus) : un échec laisse retenter plus tôt
        return A.upcoming(fresh, now)
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même, avec les cotes déjà connues
        print(f"[1xbet] relecture des stats pour « combi » impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return pronos


def _not_open(pronos, prefix, now):
    """Ligne « 1xBet n'a pas encore ouvert les paris tirs de 71 matchs… redemande vers 12h30 (21 matchs à 14h00) » d'un « combi » qui échoue ;
    '' si tous les matchs à pronostic stats de ces types ont leur cote (ou si aucun type stats n'est demandé)."""
    pre = tuple(p for p in (prefix or A.STAT_PREFIX) if p in A.STAT_PREFIX)
    if not pre:
        return ""
    priced = {c["id"] for c in pronos if c.get("xodds") and c["market"].startswith(pre)}
    kos = {}
    for c in pronos:
        if c["market"].startswith(pre) and c["id"] not in priced:
            try:
                kos[c["id"]] = datetime.fromisoformat(c["commence"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                pass
    later = [ko for ko in kos.values() if ko >= now + timedelta(minutes=C.STATS_WINDOW_MIN_MIN)]
    if not later:
        return ""
    slots = {}
    for ko in later:
        slots[ko] = slots.get(ko, 0) + 1
    slot, n_slot = min(slots.items(), key=lambda kv: (-kv[1], kv[0]))
    label = CB.kinds_label([CB.kind_of(p) for p in pre]) if prefix else "stats (tirs, fautes...)"
    return (f"⏳ Paris {label} pas encore ouverts chez 1xBet sur {len(kos)} match{'s' if len(kos) > 1 else ''} : redemande plus près des matchs "
            f"({n_slot} à {fmt.hhmm(slot.strftime('%H:%M'))}).")


def _leg_time(c):
    """Instant (timestamp) du relevé en cache de la cote 1xBet de la jambe `c` (sous-match des corners, tirs... ou match) ; None si inconnu."""
    return xbet.odds_time([(c.get("xref") or [c.get("xgid")])[0]])


def _when_note(legs):
    """Heure des cotes 1xBet d'un combiné -> (« HH:MM » de la plus RÉCENTE, ligne qui nomme celles lues plus de 15 min avant, '' sinon). Avant : l'heure
    de la plus ancienne seule (« Cotes 1xBet de 05:01 » alors que 3 cotes sur 4 venaient d'être relues à 07:08)."""
    ts = {id(c): A._leg_time(c) for c in legs}
    known = [t for t in ts.values() if t]
    if not known:
        return None, ""
    new = max(known)
    old = [c for c in legs if ts[id(c)] and ts[id(c)] < new - 15 * 60]
    hm = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M")      # noqa: E731
    if not old:
        return hm(new), ""
    names_ = ", ".join(f"{c['home']} – {c['away']}" for c in old[:2]) + (f" +{len(old) - 2}" if len(old) > 2 else "")
    many = len(old) > 1
    oldest = fmt.hhmm(hm(min(ts[id(c)] for c in old)))
    return hm(new), f"\n⚠️ {len(old)} cote{'s' if many else ''} pas relue{'s' if many else ''} depuis {oldest} : {names_} — vérifie-{'les' if many else 'la'}"


def _bold_pools(picks, pronos, prefix, min_p=None, min_ev=None):
    """Jambes d'un combiné « audacieux » : (pronostics, paris réels) chiffrés chez 1xBet, des types choisis, que Kelly voit gagnants au moins
    C.COMBI_BOLD_MIN_P et d'espérance d'au moins C.COMBI_BOLD_MIN_EV à la cote 1xBet, sur des équipes qu'elle connaît assez (combos.data_ok : 68 % calculés
    sur 3 matchs contre 31 % chez 1xBet ne tiennent pas, le 10/10) ; jamais
    un pari sans cote 1xBet, ni un pari que l'ensemble écarte (c["xveto"] : modèle et marché en désaccord). `min_p` / `min_ev` : seuils du
    « au moins N » (C.COMBI_FILL_MIN_P / C.COMBI_FILL_MIN_EV : paris probables mais mal payés pour combler le trou)."""
    lo_p = C.COMBI_BOLD_MIN_P if min_p is None else min_p
    lo_ev = C.COMBI_BOLD_MIN_EV if min_ev is None else min_ev
    real_odds = any(c.get("xodds") for c in pronos) or any(c.get("xodds") for c in picks)
    ok = lambda c, odd: (odd and (not prefix or c["market"].startswith(prefix)) and c.get("prob", 0) >= lo_p   # noqa: E731
                         and c["prob"] * odd - 1 >= lo_ev and CB.data_ok(c) and not c.get("xveto") and lanes.usable(c) and lanes.ticket_ok(c)       # famille hors voies : jamais ; voie « ghost » / « off », ligue en fantôme, « fill » coupé
                         and (c.get("xodds") or not real_odds))
    mine = [c | {"odds": c.get("xodds") or c.get("min_odds")} for c in pronos if ok(c, c.get("xodds") or c.get("min_odds") or 0)]
    real = [c for c in picks if ok(c, c.get("xodds") or c.get("odds") or 0)]
    return mine, real


def underpaid(c):
    """Pari que 1xBet paie sous la cote mini de Kelly (sa juste valeur + marge) : probable, mais il fait perdre un peu sur la durée."""
    return bool(c.get("xodds") and c.get("min_odds") and c["xodds"] < c["min_odds"])


def underpaid_lines(rows, limit=4):
    """Bloc « ⚠️ Probables mais mal payés » de /analyse et /match : paris du match que Kelly voit gagnants au moins C.COMBI_FILL_MIN_P mais que 1xBet
    paie sous leur juste valeur (C.COMBI_FILL_MIN_EV au pire), données suffisantes, pas écartés par l'ensemble. Ils ne sont jamais proposés seuls ;
    « combi N min M » s'en sert pour compléter. '' s'il n'y en a pas."""
    best = {}
    for c in rows:
        if (A.underpaid(c) and c.get("prob", 0) >= C.COMBI_FILL_MIN_P and c["prob"] * c["xodds"] - 1 >= C.COMBI_FILL_MIN_EV
                and CB.data_ok(c) and not c.get("xveto") and lanes.usable(c)):          # une jambe de stats de coupe en fantôme ne s'affiche pas
            k = (c["id"], c["market"])
            if k not in best or c["prob"] > best[k]["prob"]:
                best[k] = c
    top = sorted(best.values(), key=lambda c: -c["prob"])[:limit]
    if not top:
        return ""
    lines = [fmt.b("⚠️ Probables mais mal payés"), fmt.i("1xBet paie moins que leur juste valeur : jamais seuls, seulement pour compléter un « combi … min N ».")]
    lines += [f"• {fmt.esc(c['pick'])} · {fmt.prob_label(c)} {fmt.pct(c['prob'])} · 1xBet {fmt.odds_txt(c['xodds'])} (juste {fmt.odds_txt(round(1 / c['prob'], 2))})"
              for c in top]
    return "\n".join(lines)


def _reach(legs):
    """Plus haute cote atteignable avec `legs` (la meilleure cote par match)."""
    best = {}
    for c in legs:
        best[c["id"]] = max(best.get(c["id"], 1.0), c["odds"])
    return math.prod(best.values()) if len(best) >= 2 else 1.0


def _chances(p):
    """« 0,05 % (environ 1 chance sur 2 000) » ; « 12 % » au-dessus de 1 %."""
    return fmt.pct(p) if p >= 0.01 else f"{p * 100:.2f} %".replace(".", ",") + f" (environ 1 chance sur {int(round(1 / p, -1 if p > 0.001 else -2)):,})".replace(",", " ")


def _bold_combo(picks, pronos, target, day, kinds, now, usage=None, note="", nlegs=None, fill=False):
    """« 🎲 Aller plus loin quand même » : combiné à la cote visée avec les paris chiffrés chez 1xBet des types choisis, solides ou non (_bold_pools) ;
    l'optimiseur prend d'abord les plus probables. Les paris moins solides (hors combos.fair_legs / make_pool) portent 🟠 (fmt.leg), les chances et l'espérance
    dites, la mise conseillée vient du budget (petite). Pronostics d'abord, puis paris réels ; jamais les deux mélangés. Gardé comme un « combi »
    (« ✅ Je le joue »). `fill` (« combi 100 min 15 » sans assez de paris solides) : les N paris sont toujours là — le trou est comblé avec les
    plus probables après les solides, et si N paris dépassent déjà la cote, c'est la plus proche au-dessus ; -> None si même ainsi c'est impossible."""
    t, prefix, only = fmt.trim(target, 1), CB.kinds_prefix(kinds), CB.kinds_label(kinds)
    lo, hi = target, target * (1 + C.CUSTOM_TOLERANCE)
    thr = {"min_p": C.COMBI_FILL_MIN_P, "min_ev": C.COMBI_FILL_MIN_EV} if fill else {}
    mine, real = A._bold_pools(picks, pronos, prefix, **thr)
    if usage is not None:                           # tes tickets joués du jour : mêmes règles de réutilisation
        mine, real = usage.filter(mine), usage.filter(real)
    mx_, mn_ = chat.legs_bounds(*(nlegs or (None, False)))

    def pick(legs):
        combo = CB.best_combo(legs, lo, hi, max_legs=mx_, min_legs=mn_)
        if combo or not (fill and mn_):
            return combo, None
        low = {}                                    # N paris dépassent déjà la cote : la plus petite cote en N paris (les plus sûrs de chaque match)
        for c in legs:
            low[c["id"]] = min(low.get(c["id"], 99.0), c["odds"])
        if len(low) < mn_:
            return None, None
        floor_n = math.prod(sorted(low.values())[:mn_])
        if floor_n <= hi:
            return None, None
        return CB.best_combo(legs, floor_n * 0.999, floor_n * (1 + C.CUSTOM_TOLERANCE), max_legs=mx_, min_legs=mn_), floor_n   # 0,999 : arrondis du produit
    first = pick(mine)[0]
    old = [c for c in (first[0] if first else []) if (A._leg_time(c) or 0) < time.time() - C.COMBI_BOLD_FRESH_MIN * 60]
    if old:                                         # cotes de plus de C.COMBI_BOLD_FRESH_MIN min : relues en direct avant de répondre
        try:
            telegram.send(f"⏳ Je revérifie {len(old)} cote{'s' if len(old) > 1 else ''} chez 1xBet…", queue=False, silent=True)
            pronos = A.upcoming(xprice.live_check({c["id"] for c in old}, now, fresh_min=C.COMBI_BOLD_FRESH_MIN)[0], now)
            mine, _ = A._bold_pools(picks, pronos, prefix, **thr)
            mine = usage.filter(mine) if usage is not None else mine
        except Exception as e:                      # noqa: BLE001 - la réponse part avec les cotes connues, leur heure est dite
            print(f"[1xbet] relecture du combiné audacieux impossible : {type(e).__name__}: {e}", file=sys.stderr)
    keep = {"day": day, "target": target, "lo": lo, "hi": hi} | ({"kinds": list(kinds)} if prefix else {}) | ({"nlegs": list(nlegs)} if nlegs and nlegs[0] else {})
    title = "🎯 COMBINÉ SUR MESURE" if fill else "🎲 COMBINÉ AUDACIEUX"
    for legs, solid, src in ((mine, {c["key"] for c in CB.fair_legs(pronos)}, "pronos"), (real, {c["key"] for c in CB.make_pool(picks)}, "picks")):
        combo, over = pick(legs)
        if not combo:
            continue
        weak = [c for c in combo[0] if c["key"] not in solid]
        many = len(weak) > 1
        sure_loss = money.active() and not money.has_edge(combo[2], combo[1])      # espérance négative : Kelly ne conseille aucune mise (money.allocate)
        small = "Kelly ne conseille pas d'y miser" if sure_loss else "mise petite"
        # sous le titre, en quelques mots : les paris moins sûrs ne sont plus nommés, ils portent 🟠 dans la liste (« mal payé » sur leur ligne)
        lottery = money.is_lottery(combo[2], combo[1], CB.market_prob(combo[0]))       # la ligne 🎰 sous les chances le dit déjà (fmt.combo_chances)
        rare = f" Environ 1 chance sur {int(round(1 / combo[2], -1 if combo[2] > 0.001 else -2)):,}.".replace(",", " ") if combo[2] < 0.01 and not lottery else ""
        if fill:                                    # au moins N : les N paris d'abord, les moins sûrs comblent le trou
            lines = [f"🧩 {len(combo[0])} paris (au moins {mn_} demandés)"
                     + (f" : en {mn_} paris, même les plus sûrs, la cote ne descend pas sous {fmt.trim(over, 1)} (pas {t}) ; voici le plus sûr à partir de là." if over else "."),
                     f"🎲 {small[0].upper() + small[1:]}.{rare}"]
        elif lottery:
            lines = ["🎲 Kelly ne conseille pas d'y miser."] if sure_loss else []
        else:
            lines = [f"🎲 Ticket de loterie, {small if sure_loss else 'mise très petite'}.{rare}"]
        if src == "pronos" and CB.ev(combo) < 1:     # le combiné à cote juste n'affiche pas l'espérance dans son en-tête
            m = fmt.market_ev_txt(combo[0], combo[1]) if fmt.all_fill(combo[0]) else None
            esp = (f"Espérance au prix du marché {m}." if m is not None else "Espérance au prix du marché non mesurable.") if fmt.all_fill(combo[0]) else f"Espérance selon Kelly {fmt.signed((CB.ev(combo) - 1) * 100, ' %')}."
            if lines:
                lines[-1] += " " + esp
            else:
                lines.append(esp)
        lines.append(f"{fmt.WEAK} {len(weak)} pari{'s' if many else ''} moins sûr{'s' if many else ''} {'pour compléter' if fill else 'pour atteindre la cote'}"
                     if weak else "✅ Tous ses paris sont solides.")
        head = fmt.esc("\n".join(lines))
        marks = {"weak": {c["key"] for c in weak}, "extra": {c["key"]: "mal payé" for c in weak if A.underpaid(c)}, "shared": A._shared_counts(day, combo[0])}
        foot = (f"\n🎯 Paris choisis : {only}" if only else "") + (f"\n{note}" if note else "")
        when, stale = A._when_note(combo[0])
        when = when or store.load("today.json", {}).get("xbet_at")
        foot += stale
        if src == "pronos":
            return A._fair_reply(combo, "", title=title, when=when, retry="redemande le combi", budget=True, keep=keep, tail=foot, note=head, **marks)
        st = A._budget_stake(combo[2], combo[1], "combo", "🎯", legs=combo[0]) if money.active() else None
        code = xbet.save_coupon(combo[0]) if all(c.get("xref") for c in combo[0]) else None
        msg = Html(fmt.combo(title + (f" · {only.upper()}" if only else ""), combo[0], combo[1], combo[2],
                             reasons={c["key"]: (CB.leg_reason(c) or "proche") for c in combo[0]}, stake_txt=A._stake_words(st, combo[2], combo[1], combo[0]), coupon=code,
                             when=when, note=head, **marks) + ("\n" + fmt.esc(foot.strip("\n")) if foot.strip() else ""))
        cid = custom.remember(day, target, lo, hi, combo, st, money.cur() if st is not None else None, code, when, src="picks",
                              kinds=kinds if prefix else None, nlegs=nlegs)
        return custom.with_button(msg, cid, day, code)
    if fill:
        return None
    best = max(A._reach(mine), A._reach(real))
    closed = A._not_open(pronos, prefix, now)
    return (f"❌ Pas de combiné à la cote {t}{' (' + only + ')' if only else ''}, même avec des paris plus risqués."
            + (f"\n📉 Au plus {fmt.trim(best, 1)}." if best > 1 else "") + (f"\n{closed}" if closed else ""))


def _legs_failure(picks, pronos, target, kinds, prefix, n_max, exact):
    """« combi 10 max 3 » impossible : les bons paris du jour ont de petites cotes, en N paris on plafonne à X. Boutons : la plus haute cote en N
    paris, sans limite de paris, et des paris plus risqués en N paris (seulement s'ils atteignent la cote)."""
    t, lab = fmt.trim(target, 1), chat.legs_label(n_max, exact)
    codes = ".".join(k for k in CB.KIND_CODES if k in set(kinds or ()))
    if exact == "min":                              # au moins N : il manque des matchs, ou N paris dépassent déjà la cote
        return A._min_legs_failure(picks, pronos, target, prefix, n_max, codes)
    words = " ".join(chat.KIND_WORD[k] for k in CB.KIND_CODES if k in set(kinds or ()))
    keep = lambda legs: [c for c in legs if not prefix or c["market"].startswith(prefix)]       # noqa: E731
    xp, xk = any(c.get("xodds") for c in pronos), any(c.get("xodds") for c in picks)
    # un combiné est bâti sur les pronostics OU sur les paris réels, jamais les deux : le plafond se calcule réserve par réserve, comme custom_combo
    pools = [keep(CB.fair_legs(pronos))] + ([keep(A._todays_usage(str(A.now_utc().date()), picks)[0].filter(CB.make_pool(picks)))] if picks and (xk or not xp) else [])
    pool = max(pools, key=lambda p: CB.reach(p, n_max))
    best = {}
    for c in pool:
        best[c["id"]] = max(best.get(c["id"], 1.0), c["odds"])
    if len(best) < 2:
        return (f"❌ Pas de combiné à la cote {t} {lab} : moins de 2 matchs ont un bon pari"
                + (f" ({CB.kinds_label(kinds)})" if prefix else "") + " en ce moment.")
    top = CB.reach(pool, n_max)
    odds = sorted(best.values())
    lines = [f"❌ Pas de combiné à la cote {t} {lab}.",
             f"📉 Les bons paris du jour ont de petites cotes ({fmt.odds_txt(odds[0])} à {fmt.odds_txt(odds[-1])}) : {lab}, au plus "
             f"{fmt.trim(top, 1)}."]
    rows = []
    low = math.floor(top * 10) / 10
    if 2.0 <= low < target:
        rows.append([(f"🎯 Cote {fmt.trim(low, 1)} en {n_max} paris", f"cgo|{fmt.trim(low, 1)}|{codes}|{n_max}")])
    rows.append([(f"➕ Plus de paris (cote {t})", f"cgo|{t}|{codes}|")])
    bold = CB.reach([c for legs in A._bold_pools(picks, pronos, prefix) for c in legs], n_max)
    if bold >= target:
        rows.append([(f"🎲 Paris plus risqués, {n_max} paris", f"cplus|{t}|{codes}|{n_max}")])
        lines.append(f"🎲 Avec des paris un peu moins sûrs, la cote {t} passe {lab}.")
    return telegram.with_buttons("\n".join(lines), telegram.inline(rows))


def _min_legs_failure(picks, pronos, target, prefix, n, codes):
    """« combi 100 min 15 » impossible : pas assez de matchs avec un bon pari (boutons : sans minimum, paris plus risqués), ou 15 paris font déjà
    plus que la cote demandée (bouton : la plus petite cote en 15 paris)."""
    t = fmt.trim(target, 1)
    hi = target * (1 + C.CUSTOM_TOLERANCE)
    pool = [c for c in CB.fair_legs(pronos) if not prefix or c["market"].startswith(prefix)]
    low = {}
    for c in pool:
        low[c["id"]] = min(low.get(c["id"], 99.0), c["odds"])
    lines, rows = [f"❌ Pas de combiné à la cote {t} en {n} paris au moins."], []
    if len(low) < n:
        lines.append(f"📉 Seulement {len(low)} match{'s' if len(low) > 1 else ''} avec un bon pari en ce moment : il en faut {n}.")
        rows.append([(f"➕ Sans minimum (cote {t})", f"cgo|{t}|{codes}|")])
        bold = {c["id"] for legs in A._bold_pools(picks, pronos, prefix) for c in legs}
        if len(bold) >= n:
            rows.append([(f"🎲 Paris plus risqués, {n} paris au moins", f"cplus|{t}|{codes}|{n}+")])
    else:
        floor_n = math.prod(sorted(low.values())[:n])
        if floor_n > hi:
            up = math.ceil(floor_n)
            lines.append(f"📈 En {n} paris, même les plus sûrs, la cote est déjà d'au moins {fmt.trim(floor_n, 1)}.")
            rows.append([(f"🎯 Cote {up} en {n} paris au moins", f"cgo|{up}|{codes}|{n}+")])
        else:
            lines.append(f"📉 Aucune combinaison de {n} paris au moins ne tombe entre {t} et {fmt.trim(hi, 1)}.")
        rows.append([(f"➕ Sans minimum (cote {t})", f"cgo|{t}|{codes}|")])
    return telegram.with_buttons("\n".join(lines), telegram.inline(rows))


def _shared_counts(day, legs):
    """{id du match: nombre de tes tickets joués du jour (exposure) où il est déjà} pour les matchs de `legs` qui y sont ; {} sinon. Rien n'est
    retiré : chaque jambe concernée porte « 🔗N » dans le message (fmt.leg) et le pied l'explique."""
    if not day:
        return {}
    ids = {c.get("id") for c in legs}
    count = {}
    for t in exposure.tickets(day):
        for i in {l.get("id") for l in t["legs"]} & ids:
            count[i] = count.get(i, 0) + 1
    return count


def another_combo(cid, day, target=None):
    """« 🔀 Un autre sans mes matchs » sous le combi `cid` : même cote (ou `target` : « 🔀 Cote X sans mes matchs » après un échec), mêmes types, sans
    aucun match de tes tickets joués du jour ni de ce combi."""
    item = custom._load(str(day)[:10])["items"].get(cid)
    if item is None:
        return custom.GONE
    today = store.load("today.json", {})
    avoid = exposure.played_ids(day) | {l.get("id") for l in item["legs"]}
    return A.custom_combo(A.upcoming(today.get("picks") or []), A.upcoming(today.get("pronos") or []), target or item["target"], day,
                        when=today.get("picks_at") or today.get("xbet_at"), kinds=item.get("kinds"), avoid=avoid, again=cid,
                        nlegs=(item["nmax"], item.get("exact")) if item.get("nmax") else None)


def _note(keep):
    """Ligne « ⚠️ Kelly ne mélange pas… » (_kinds_split) d'un combiné sur mesure, précédée d'un saut de ligne ; '' sans."""
    return "\n" + keep["note"] if keep and keep.get("note") else ""


def _kinds_split(picks, pronos, kinds):
    """« combi 20 victoire tirs » qui échoue : si les types choisis sont les uns dans les paris réels (victoire, buts), les autres dans les
    pronostics de Kelly (tirs, fautes...), une ligne le dit (un combiné est bâti sur les uns OU les autres) ; '' sinon."""
    real = {CB.kind_of(c["market"]) for c in CB.make_pool(picks)} & set(kinds)
    mine = {CB.kind_of(c["market"]) for c in CB.fair_legs(pronos)} & set(kinds)
    if not real or not mine or real <= mine or mine <= real:
        return ""
    return (f"⚠️ {CB.kinds_label(real - mine).capitalize()} et {CB.kinds_label(mine - real)} ne vont pas dans le même combiné : "
            "demande-les à part.")


def kinds_picker(target, sel=(), n=None):
    """Cases à cocher des types de paris d'un combiné à la cote `target` (après « 🎯 Un combiné » -> cote) : « ✅ Tout » puis les types jouables
    aujourd'hui avec leur nombre de matchs (combos.kinds_available), et « 🎯 Construire le combiné ». Rien de coché = tous les paris.
    Boutons « ck|<cote>|<codes> » (cocher / décocher : les boutons du message sont refaits) et « cgo|<cote>|<codes> » (64 octets au plus)."""
    today = store.load("today.json", {})
    picks, pronos = A.upcoming(today.get("picks") or []), A.upcoming(today.get("pronos") or [])
    avail = CB.kinds_available(picks, pronos)
    wait = {}                                       # pronostics stats solides pas encore ouverts chez 1xBet : relus en construisant (_open_stats)
    for c in A._pending(pronos):
        k = CB.kind_of(c["market"])
        if k:
            wait.setdefault(k, set()).add(c["id"])
    wait = {k: len(v) for k, v in wait.items()}
    sel = [k for k in CB.KIND_CODES if k in set(sel)]
    t = fmt.trim(target, 1)
    n = str(n) if n else ""                          # « 5 » : au plus 5 ; « 15+ » : au moins 15 ; « » : libre
    num, at_least = (int(n.rstrip("+")) if n.rstrip("+").isdigit() else None), n.endswith("+")
    rows = [[(("✅ " if not sel else "⬜ ") + "Tout", f"ck|{t}||{n}")]]
    items = []
    for k, label, _ in CB.KINDS:
        if avail.get(k) or wait.get(k) or k in sel:
            on = k in sel
            nxt = [x for x in CB.KIND_CODES if x in set(sel) ^ {k}]          # cocher / décocher, dans l'ordre de KINDS
            count = f"{avail.get(k, 0)}" + (f" + {wait[k]} ⏳" if wait.get(k) else "")
            items.append((("✅ " if on else "⬜ ") + f"{label} ({count})", f"ck|{t}|{'.'.join(nxt)}|{n}"))
    rows += [items[i:i + 2] for i in range(0, len(items), 2)]
    codes = ".".join(sel)
    rows.append([(("✅ " if not n else "") + "Paris : libre", f"ck|{t}|{codes}|")]
                + [(("✅ " if num == k and not at_least else "") + f"≤ {k}", f"ck|{t}|{codes}|{k}") for k in A.LEGS_CHOICES])
    if num and (at_least or num not in A.LEGS_CHOICES):    # un nombre écrit (« ✏️ Autre ») : au moins / au plus, au choix
        rows.append([(("✅ " if at_least else "") + f"≥ {num} (au moins)", f"ck|{t}|{codes}|{num}+"),
                     (("✅ " if not at_least else "") + f"≤ {num} (au plus)", f"ck|{t}|{codes}|{num}")])
    rows.append([("✏️ Autre nombre de paris", f"cn|{t}|{codes}")])
    rows.append([("🎯 Construire le combiné", f"cgo|{t}|{codes}|{n}")])
    text = (f"🎯 Combiné à la cote {t} : quels paris ?\n" + fmt.i("Rien de coché = tout. (N) = matchs avec un bon pari"
                                                                    + (" · ⏳ pas encore ouverts chez 1xBet" if wait else "") + "."))
    if not avail and not wait:
        text += "\n" + fmt.i("Aucun bon pari jouable pour l'instant.")
    return text, telegram.inline(rows)


def _tier_range(tier):
    """(cote mini, cote maxi, préfixe de marché ou None) d'un palier de combiné à cote juste (5, 12, 25, tc5, tv5, ts5, ts5w1...)."""
    for t, lo, hi in C.FAIR_COMBO_TIERS:
        if str(t) == str(tier):
            return lo, hi, None
    for k, prefix, _, lo, hi in C.FAIR_SHOTS_TIERS:
        if str(k) == str(tier):
            return lo, hi, prefix
    if CB.window_no(tier) is not None:             # « ts5w1 » : combiné de la fenêtre des stats (statswindow), mêmes familles et même fourchette que « ts5 »
        return C.STATS_WINDOW_COMBO[0], C.STATS_WINDOW_COMBO[1], C.STATS_WINDOW_PREFIX
    sn = CB.season_no(tier)
    if sn is not None:                             # « ss10 » : combiné de la voie `saison` (fourchette de son palier) ; jamais remplacé tout seul (voir _update_combos, check)
        return sn * (1 - C.SEASON_SPREAD), sn * (1 + C.SEASON_SPREAD), None
    return None


def _budget_stake(prob, odds, kind="fair", tier=None, legs=None):
    """Mise (devise) d'un combiné demandé à la main (« combi 20 ») sur le budget du jour ; None sans budget. 0 : trop petite pour le budget.
    `kind` / `tier` : voir money.allocate (combiné à cote juste : sa cote ; combiné de paris réels : « combo » et son palier). `legs` : ses jambes, pour
    ses chances selon 1xBet (ticket « loterie », money.is_lottery)."""
    if not money.active():
        return None
    item = {"kind": kind, "tier": odds if tier is None else tier, "prob": prob, "odds": odds,
            "p_mkt": CB.market_prob(legs) if legs else None} | (lanes.item_fields(legs) if legs else {})
    if C.SEASON_AUTO and legs and odds >= C.SEASON_AUTO_LOTTERY_ODDS and all(l.get("lane") == "saison" or l.get("strategy") == "saison" for l in legs):
        item["lottery"] = True                       # « saison 30 » / « saison 100 » à la demande : mise plafonnée comme un ticket loterie (100 FCFA)
    return money.allocate([item])[0][0]


def _stake_words(st, prob=None, odds=None, legs=None):
    """La mise `st` (_budget_stake) en mots pour le message : « 1 500 FCFA (sur ton budget du jour) » ; None sans budget. Mise 0 : « pas de mise (trop
    petite pour ton budget) », ou, si `prob` et `odds` montrent une espérance négative, « Kelly ne conseille pas de miser dessus » (money.no_stake_txt).
    `legs` : voir _budget_stake."""
    fields = lanes.item_fields(legs) if legs else {}
    return money.stake_words(st, prob, odds, " (sur ton budget du jour)", p_mkt=CB.market_prob(legs) if legs else None,
                             novalue=bool(fields.get("novalue")), flat=bool(fields.get("flat")))


def _budget_stake_txt(prob, odds):
    """Mise conseillée d'un combiné demandé à la main (« combi 20 ») sur le budget du jour ; None sans budget (le message dit de le régler)."""
    return A._stake_words(A._budget_stake(prob, odds), prob, odds)


def _fair_reply(fc, head, changes="", coupon=True, title=None, stake_txt=None, when=None, code=..., retry=None, stale=False, budget=False,
                keep=None, tail="", weak=None, shared=None, note="", extra=None):
    """Réponse « combiné à cote juste » de custom_combo : phrase d'introduction, paris remplacés, combiné, et code coupon 1xBet quand
    toutes les jambes ont leur référence 1xBet (_coupon ; échec = pas de code, une ligne le dit avec `retry`). `code` : code déjà
    enregistré (None : échec), sinon calculé ici. `stake_txt` : la mise du budget du jour (combiné du matin renvoyé), `budget` : la
    calculer sur le budget du jour (combiné demandé à la main), `when` : l'heure « HH:MM » des cotes 1xBet, `stale` : cotes du dernier
    relevé (1xBet injoignable), voir fmt.fair_combo. `keep` ({"day", "target", "lo", "hi"} : combiné demandé à la main) : le combiné est
    gardé en mémoire avec sa mise, son code et l'heure de ses cotes (custom.remember) et part avec le bouton « ✅ Je le joue ».
    `weak` / `shared` / `note` / `extra` : repères des jambes et lignes sous le titre, voir fmt.combo."""
    legs, min_total, prob = fc
    if code is ...:
        code = A._coupon(legs) if coupon else None
    stake = None
    if budget and stake_txt is None:
        p_stake = CB.stake_prob(legs, prob)          # jambe « probable » (sans avis sharp) : proba 1xBet sans marge
        stake = A._budget_stake(p_stake, min_total, legs=legs)
        stake_txt = A._stake_words(stake, p_stake, min_total, legs)
    expected = coupon and bool(retry) and all(c.get("xref") and c.get("xodds") for c in legs) and not code
    top = "\n".join(x for x in (fmt.esc(head).strip("\n"), changes) if x)
    msg = Html((top + "\n\n" if top.strip() else "")
               + fmt.fair_combo(min_total, legs, prob, CB.fair_stake(min_total), stake_txt=stake_txt, coupon=code, title=title,
                                when=when, retry=retry if expected else None, stale=stale, weak=weak, shared=shared, note=note, extra=extra)
               + ("\n" + fmt.esc(tail.strip("\n")) if tail.strip() else ""))      # avertissements courts, une ligne chacun, sous le combiné
    if keep:                                        # gardé en mémoire avec la mise conseillée du message (budget du jour ; None sans budget)
        cid = custom.remember(keep["day"], keep["target"], keep["lo"], keep["hi"], fc, stake, None if stake is None else money.cur(), code, when,
                              src=keep.get("src") or "pronos", kinds=keep.get("kinds"), nlegs=keep.get("nlegs"))
        return custom.with_button(msg, cid, keep["day"], code, another=keep.get("src") != "saison")      # « 🔀 Un autre » rebâtirait sur les paris des autres voies
    return msg


def _live_season(legs, lo, hi, now=None, check=None, usage=None):
    """Combiné `saison` `legs` vérifié EN DIRECT chez 1xBet : ses matchs + C.XBET_LIVE_SPARES remplaçants (les jambes `saison` les plus probables du matin) relus à l'instant
    (seasonlane.live_check), puis CB.season_patch dans [lo, hi]. -> (résultat de season_patch ou None, heure, relu : faux si 1xBet n'a rien rendu). `check` : injectable."""
    now = now or A.now_utc()
    ids = {l["id"] for l in legs}
    spares = sorted((c for c in A.upcoming(store.load("today.json", {}).get("season") or [], now) if c["id"] not in ids and lanes.usable(c)), key=lambda c: -c["prob"])
    spare_ids = list(dict.fromkeys(c["id"] for c in spares))[:C.XBET_LIVE_SPARES]
    pool, got, hhmm = (check or seasonlane.live_check)(ids | set(spare_ids), now, fresh_min=C.XBET_COMBI_FRESH_MIN)
    if not got:
        return None, hhmm, False
    return CB.season_patch(legs, pool, lo, hi, fresh_ids=got, usage=usage), hhmm, True


def _live_fair(legs, lo, hi, prefix=None, now=None, check=None, fresh_min=None, usage=None, avoid=None):
    """Combiné `legs` vérifié EN DIRECT chez 1xBet : ses matchs + C.XBET_LIVE_SPARES remplaçants (les meilleurs autres paris 💎 du
    jour) relus à l'instant (xprice.live_check), puis CB.patch_combo dans [lo, hi] (`usage` : réutilisation des matchs par les autres combinés
    du jour ; `avoid` : matchs jamais pris comme remplaçants, « 🔀 Un autre sans mes matchs »). -> (résultat de patch_combo ou None, heure, relu : faux si 1xBet
    n'a rien rendu). `check` : injectable pour les tests."""
    from . import xprice
    avoid = set(avoid or ())
    pool = [c for c in A.upcoming(store.load("today.json", {}).get("pronos") or [], now) if c["id"] not in avoid]
    mine = {l["id"] for l in legs}
    spares = []
    for c in sorted((c for c in CB.fair_legs(pool) if c["id"] not in mine and (not prefix or c["market"].startswith(prefix))),
                    key=lambda c: -(c.get("xev") or 0) * c["prob"]):
        if c["id"] not in spares:
            spares.append(c["id"])
    pronos, got, hhmm = (check or xprice.live_check)(mine | set(spares[:C.XBET_LIVE_SPARES]), now, fresh_min=fresh_min)
    if not got:
        return None, hhmm, False
    return CB.patch_combo(legs, [c for c in A.upcoming(pronos, now) if c["id"] not in avoid], lo, hi, fresh_ids=got, prefix=prefix, usage=usage), hhmm, True


def xbet_why():
    """Cause du dernier échec 1xBet en mots simples (« le proxy ne répond pas », « 1xBet refuse l'accès (erreur 429), pause jusqu'à 14h05 UTC »…) ;
    '' si elle est inconnue. Jamais l'adresse du proxy."""
    try:
        h = state.health(xbet.SOURCE)
        cause = http.cause(h.get("last_error") or "", "1xBet") if h.get("last_error") else ""
        if h.get("blocked_until"):
            cause = (cause or "1xBet refuse l'accès") + f", pause jusqu'à {h['blocked_until'][11:16].replace(':', 'h')} UTC"
        return cause
    except Exception:                               # noqa: BLE001
        return ""


def _live_reply(fc, lo, hi, now=None, check=None, keep=None, usage=None, avoid=None):
    """combi N aux cotes 1xBet : « ⏳ » tout de suite, puis le combiné vérifié en direct (paris retirés ou mal payés remplacés) et son
    code coupon aux cotes de l'instant. 1xBet injoignable : le combiné aux cotes du dernier relevé, sans code, en le disant. `keep` : voir
    _fair_reply (« ✅ Je le joue » sous le combiné, pas sous les réponses « impossible »)."""
    telegram.send("⏳ Je vérifie les cotes 1xBet en direct…", queue=False, silent=True)
    try:
        res, hhmm, ok = A._live_fair(fc[0], lo, hi, prefix=CB.kinds_prefix((keep or {}).get("kinds")), now=now, check=check,
                                   fresh_min=C.XBET_COMBI_FRESH_MIN, usage=usage, avoid=avoid)
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même
        print(f"[1xbet] vérification en direct impossible : {type(e).__name__}: {e}", file=sys.stderr)
        res, hhmm, ok = None, "", False
    if not ok and A._all_soon(fc[0], now or A.now_utc()):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes. Demande plutôt un autre « combi ».")
    if not ok:                                      # cotes du dernier relevé : l'heure réelle de leur relevé (cache), sinon celle du relevé du jour
        t0 = xbet.odds_time([(c.get("xref") or [c.get("xgid")])[0] for c in fc[0]])
        last = datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%H:%M") if t0 else store.load("today.json", {}).get("xbet_at") or ""
        why = A.xbet_why()
        return A._fair_reply(fc, f"⚠️ 1xBet ne répond pas en ce moment{f' ({why})' if why else ''} : combiné aux cotes du dernier relevé (cote "
                               f"{fmt.trim(fc[1], 1)}), sans code coupon. Vérifie chaque cote avant de jouer.", coupon=False,
                           when=last, stale=True, budget=True, keep=keep)
    nl = (keep or {}).get("nlegs")
    if res is not None and nl:                      # des remplaçants ne doivent pas sortir du nombre de paris choisi
        mx_, mn_ = chat.legs_bounds(*nl)
        if not (mn_ or 2) <= len(res[0]) <= (mx_ or C.MAX_LEGS):
            res = None
    if res is None:
        return (f"Les cotes 1xBet ont bougé depuis le dernier relevé : plus de combiné à la cote {fmt.trim(lo, 1)} avec des paris "
                "encore jouables. Essaie une autre cote.")
    legs, total, prob, removed, added, changed = res
    # GameId de la cote (sous-match des corners, tirs...) ; cotes de moins de 3 h reprises : l'heure réelle de leur relevé, et celles plus vieilles nommées
    when, stale = A._when_note(legs)
    when = when or hhmm
    only = CB.kinds_label((keep or {}).get("kinds"))
    return A._fair_reply((legs, total, prob), "", tail=(f"\n🎯 Paris choisis : {only}" if only else "") + stale + A._note(keep),
                       shared=A._shared_counts((keep or {}).get("day"), legs), changes=fmt.combo_changes(removed, added, changed), when=when, retry="redemande le combi", budget=True, keep=keep)


def _coupon(legs):
    """Code coupon 1xBet d'un combiné dont toutes les jambes ont leur référence et leur cote 1xBet (xbet.save_coupon : ticket sans mise) ;
    None sinon, ou si l'enregistrement échoue (jamais d'erreur)."""
    return xbet.save_coupon(legs) if legs and all(c.get("xref") and c.get("xodds") for c in legs) else None
