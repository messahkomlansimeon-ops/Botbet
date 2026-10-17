"""Orchestration : journée type, relevés, alertes, commandes."""
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import (apistats, bankroll, bootstrap, budget, cache, calib, catalogue, chat, combos as CB, confirm, combovariants, config as C, custom, dayhealth, ensemble, fair, fmt, freeclose, gapbets, gapdist, lanes, lanestats,
               exposure, learn, live as LV, migrate, monde, money, odds, played, progress, settle, sharpgap, sources, stats, statswindow, store, strength, telegram, track, weekly, xalerts,
               cupstats, edgemap, familyeval, oddsonly, resultats, seasonlane, xprice, xworld)
from .sources import fmmatch, http, state, xbet
from .telegram import Html

if oddsonly.observe not in xbet.OBSERVERS:             # voie « cotes » (fantôme) : observe chaque relevé 1xBet déjà fait, aucune requête de plus
    xbet.OBSERVERS.append(oddsonly.observe)

DISCLAIMER = Html("<i>Probabilités estimées, aucun gain garanti. Les grosses cotes passent très rarement : "
                  "mise petit. 18+</i>")


def now_utc():
    return datetime.now(timezone.utc)


def load_sample(name="sample_events.json"):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "tests", name)) as f:
        return json.load(f)


def money_line(combos=None):
    """Ligne du budget pour /bilan : le budget du jour (FCFA) ou, sans budget, de quoi le régler ; puis l'alerte de série de combinés perdus.
    Jamais de bankroll en unités."""
    lines = ([money.capital_line(html=False) + " (écris « capital » pour le détail)"] if money.capital_mode() else
             [f"💰 Budget du jour : {money.money(money.daily())} (écris « budget 7500 » pour le changer)"] if money.active()
             else [f"💡 Pas de capital : Kelly ne donne pas de mise conseillée. {money.NO_BUDGET[0].upper() + money.NO_BUDGET[1:]}."])
    streak = bankroll.streak_line(combos or [])
    return "\n".join(lines + ([streak] if streak else []))


def day_stakes(top, built, built_fair, gaps=()):
    """Mises du jour sur le budget (money.allocate) : ({"pari" | titre du palier | ("fair", cible) | ("gap", clé du pari « 1xBet paie plus que le marché »): mise},
    misé, gardé, journée pauvre) ; ({}, 0, 0, False) sans budget du jour."""
    if not money.active():
        return {}, 0, 0, False
    keys, items = [], []
    if top:
        keys.append("pari")
        items.append({"kind": "pari", "tier": "pari", "prob": top["prob"], "odds": top["odds"]} | lanes.item_fields([top], single=True))
    for g in gaps:
        keys.append(("gap", g["key"]))
        items.append(gapbets.stake_item(g))
    for title, (legs, odds_, prob) in built:
        keys.append(title)
        items.append({"kind": "combo", "tier": title, "prob": prob, "odds": odds_, "p_mkt": CB.market_prob(legs)} | lanes.item_fields(legs))
    s_keys, s_items = [], []                         # voie `saison` : allouée À PART, sur ce qui reste du budget après les autres (ses parts ne réduisent jamais les mises des autres voies)
    for target, (legs, min_total, prob) in built_fair:
        item = {"kind": "fair", "tier": min_total, "prob": CB.stake_prob(legs, prob), "odds": min_total,           # jambe « probable » (sans avis sharp) :
                "p_mkt": CB.market_prob(legs)} | lanes.item_fields(legs)                                          # proba 1xBet sans marge
        if C.SEASON_AUTO and CB.season_auto(target) and min_total >= C.SEASON_AUTO_LOTTERY_ODDS:
            item["lottery"] = True                       # voie `saison` auto : dès la cote 25, mise plafonnée comme un ticket loterie
        (s_keys if CB.season_no(target) is not None else keys).append(("fair", target))
        (s_items if CB.season_no(target) is not None else items).append(item)
    stakes, spent, kept, poor = money.allocate(items, detail=True)
    out = dict(zip(keys, stakes))
    if s_items:
        s_stakes, s_spent, _, s_poor = money.allocate(s_items, budget=max(0, kept), detail=True, step=money.round_step(money.daily()))
        out |= dict(zip(s_keys, s_stakes))
        spent, kept, poor = round(spent + s_spent, 2), round(kept - s_spent, 2), poor and s_poor
    return out, spent, kept, poor


def morning_plan(picks, gaps=()):
    """Plan commun des paris RÉELS du matin -> (paris de qualité, pari du jour, écart sharp du pari du jour, écarts sharp montrés, plan [(titre, combiné | None)],
    titres écartés pour l'espérance, Usage). L'Usage est celui de TOUTE la journée : les combinés réels le remplissent d'abord, puis le pari du jour et les
    écarts sharp (des paris seuls) y sont comptés, enfin les combinés à vérifier (fair_plan_all) et ceux de la fenêtre des stats (statswindow, day_usage) le
    reprennent : une jambe sûre (proba >= C.LEG_SAFE_PROB) sert dans C.LEG_REUSE_MAX tickets au plus, une jambe risquée dans un seul, toutes familles confondues."""
    quality = CB.make_pool(picks)
    top = max(quality, key=lambda c: c["score"]) if quality else None
    top_gap = [g for g in (gaps or []) if top and g["key"] == top["key"]]
    shown = [g for g in (gaps or []) if not (top and g["key"] == top["key"])][:C.SHARPGAP_MAX_SHOWN]      # le pari du jour a déjà son message et son bouton
    usage, rejected = CB.Usage(), []
    plan = CB.plan(picks, usage=usage, rejected=rejected) if quality else [(t, None) for t, _, _ in C.COMBOS]
    by_key = {c["key"]: c for c in picks}
    usage.add(([top] if top else []) + [by_key[g["key"]] for g in shown if g["key"] in by_key])
    return quality, top, top_gap, shown, plan, rejected, usage


def _recorded_real(day):
    """Combinés réels déjà enregistrés pour le jour `day` (bets.json) : {titre du palier: ligne}. Une relance du matin les renvoie tels quels."""
    return {c["tier"]: c for c in store.load_doc("bets.json", {"combos": []})["combos"] if str(c.get("date")) == str(day)}


def _resend_real(plan, again, picks):
    """RELANCE du matin (interruption avant l'envoi complet, --force) : un palier déjà enregistré pour le jour est RENVOYÉ tel quel (mêmes paris, mêmes
    cotes 1xBet lues, même code coupon) et non reconstruit : le suivi, les notes et le règlement restent ceux du combiné envoyé. Une jambe disparue (match
    commencé, plus de pari) : le palier est reconstruit ; s'il est déjà noté ou réglé, il n'est pas renvoyé (le ticket joué reste le suivi), sinon la ligne de
    suivi prend la nouvelle version (settle.record_combos, replace). -> (plan, {titre: ligne enregistrée} des paliers renvoyés tels quels)."""
    by_key, out, resent = {c["key"]: c for c in picks}, [], {}
    for title, combo in plan:
        old = again.get(title)
        if old is None:
            out.append((title, combo))
            continue
        xl = {x["key"]: x for x in old.get("xlegs") or []}
        legs = [by_key.get(k) for k in old.get("keys") or []]
        if legs and all(legs):
            legs = [c | ({"xodds": xl[c["key"]]["xodds"], "odds": xl[c["key"]]["xodds"]} | ({"xref": xl[c["key"]]["xref"]} if xl[c["key"]].get("xref") else {})
                         if c["key"] in xl else {}) for c in legs]
            out.append((title, (legs, old["odds"], old["prob"])))
            resent[title] = old
        elif combo and not (old.get("played") or old.get("result") is not None):
            out.append((title, combo))
        else:
            out.append((title, None))              # noté / réglé et illisible aujourd'hui : rien de nouveau (record_combos garde la ligne)
    return out, resent


def _resend_fair(pronos, again, fair_combos):
    """Comme _resend_real pour les combinés à vérifier / aux cotes 1xBet (fair_combos.json, paliers du matin) : `again` = {palier: ligne du jour}. Les jambes sont
    relues dans `pronos` par leur clé et gardent leur cote 1xBet et leur référence ENREGISTRÉES. -> fair_combos avec les paliers déjà enregistrés renvoyés tels quels."""
    by_key, out = {c["key"]: c for c in pronos}, []
    for target, combo in fair_combos:
        old = again.get(str(target))
        if old is None:
            out.append((target, combo))
            continue
        legs = []
        for k, l in zip(old.get("keys") or [], old.get("legs") or []):
            c = by_key.get(k)
            legs.append(c and c | {f: l[f] for f in ("xodds", "xref", "odds", "min_odds") if l.get(f) is not None})
        if legs and all(legs):
            out.append((target, (legs, old["min_odds"], old["prob"])))
        elif combo and not (old.get("played") or old.get("result") is not None):
            out.append((target, combo))
        else:
            out.append((target, None))
    return out


def _recorded_fair(day):
    """Combinés à cote juste du matin déjà enregistrés pour le jour `day` (fair_combos.json) : {palier: ligne}, sans les combis sur mesure ni ceux de la
    fenêtre des stats (envoyés dans la journée, jamais rebâtis le matin)."""
    return {str(r["tier"]): r for r in fair.load_combos() if str(r.get("date")) == str(day) and not r.get("custom") and CB.window_no(r["tier"]) is None
            and CB.season_no(r["tier"]) is None}


def _recorded_season(day):
    """Combinés de la voie `saison` déjà enregistrés pour le jour `day` (fair_combos.json, paliers « ss3 », « ss10 »...) : {palier: ligne}. Une relance du matin les renvoie tels quels."""
    return {str(r["tier"]): r for r in fair.load_combos() if str(r.get("date")) == str(day) and not r.get("custom") and CB.season_no(r["tier"]) is not None and not r.get("closed")}


def season_plan_all(season_legs, day, usage):
    """Combinés de la voie `saison` du matin [(palier « ss10 », combiné | None)] : paliers de C.SEASON_TIERS sur les jambes `saison` jouables (voie « on », remplissage selon FILL_LEGS),
    avec le Usage de la journée ; les paliers déjà enregistrés pour ce jour (relance) sont renvoyés tels quels. [] quand la voie n'est pas « on »."""
    if lanes.season_mode() != "on":
        return []
    legs = [c for c in season_legs if lanes.usable(c)]
    again = _recorded_season(day)
    if C.SEASON_AUTO and again:                      # relance du matin en mode auto : les combinés du jour sont renvoyés tels quels, jamais reconstruits (leurs paliers portent leur cote réelle)
        return _resend_fair(season_legs, again, [(t, None) for t in again])
    return _resend_fair(season_legs, again, CB.season_plan(legs, usage=usage))


RETRY_BUTTON = "touche « 🎟️ Vérifier ce combiné »"        # sous un combiné qui devait avoir son code coupon et ne l'a pas eu (fmt.coupon_block)


def fair_combo_message(target, legs, min_total, prob, day, stake, mise, coupon, when, k=None, lookup=False):
    """Le message d'un combiné à cote juste / saison du matin (ou d'une reconstruction `saison auto`) avec ses boutons : `stake` = sa mise (None : aucune), `mise` = formateur de mise
    (build_day), `coupon` / `when` : code coupon et heure de ses cotes, `k` : numéro d'un combiné `saison` auto (« 🧮 SAISON · COMBINÉ 1 », avec jambes, espérance, remplissage)."""
    auto = CB.season_auto(target)
    xfair = all(c.get("xref") and c.get("xodds") for c in legs)
    flat = bool(lanes.item_fields(legs).get("flat"))                              # voie `saison`, remplissage seul ENVOYÉ : « sans valeur », avec sa mise plancher
    nov = bool(C.ENSEMBLE_ENABLED and ensemble.has_value(legs) is False) and not flat       # sans valeur et envoyé (NOVALUE_SEND) : toujours dit, jamais de mise conseillée
    extra = {}
    if auto:                                                         # la cote est le résultat : jambes, espérance et part de remplissage
        ev = math.prod(c["prob"] * (c.get("xodds") or c.get("odds") or 1.0) for c in legs) - 1
        fill = sum(1 for c in legs if not c.get("sval"))
        ev_txt = f"espérance {ev * 100:+.0f} %"
        if fmt.all_fill(legs):                                       # pur remplissage : espérance au prix du marché seulement (pas aux chances de Kelly)
            m = fmt.market_ev_txt(legs, math.prod(c.get("xodds") or c.get("odds") or 1.0 for c in legs))
            ev_txt = f"espérance au prix du marché {m}" if m is not None else "espérance au prix du marché non mesurable"
        extra = {"title": f"{C.SEASON_TITLE.split(' ', 1)[0]} SAISON · COMBINÉ {k or 1}",
                 "note": fmt.i(fmt.esc(f"{len(legs)} jambes · {ev_txt} · remplissage {fill}/{len(legs)}"))}
    return chat.with_played_button(fmt.fair_combo(min_total, legs, prob, CB.fair_stake(min_total),
                                                  stake_txt=mise(stake, CB.stake_prob(legs, prob), min_total, nov, flat),
                                                  title=extra.pop("title", None) or CB.shots_title(target), coupon=coupon, when=when,
                                                  retry=RETRY_BUTTON if xfair and not coupon else None, **extra),
                                   f"v{target}", day, stake=stake, refresh=target if all(c.get("xodds") for c in legs) else None, code=coupon, lookup=lookup)


def build_day(picks, day, world=None, pronos=(), why_no_odds=None, fair_combos=(), fair_stakes=None, odds_time=None, fair_coupons=None, gaps=None,
              gap_out=None, ghost_out=None):
    """Messages du jour + enregistrement des paris envoyés. `world` : cache « tour du monde » (monde.build), `pronos` :
    pronostics sans cote (fair.make), `fair_combos` : [(palier, combiné à cote juste | None)] (combos.fair_plan), un message
    chacun, à part des combinés réels. Un message par combiné ; les paliers impossibles à bâtir avec
    des jambes de qualité sont signalés UNE fois, en une ligne, dans l'en-tête. Toujours au moins un message utile : le tour
    du monde et les pronostics remplacent les combinés les jours sans cotes réelles. Avec un budget du jour (money), les mises sont
    prises dessus ; `fair_stakes` (dict) reçoit celles des combinés à vérifier, pour leur suivi, `fair_coupons` (dict) leurs codes coupon.
    `odds_time` : heure « HH:MM » du relevé 1xBet, rappelée sous les combinés et les pronostics aux cotes 1xBet. `gaps` : écarts sharp du jour (sharpgap.find) ->
    le message « 💰 1xBet paie plus que le marché » (au plus C.SHARPGAP_MAX_SHOWN, jamais le pari du jour), mises prises sur le budget du jour comme les autres
    paris ; `gap_out` (liste) reçoit [(pari, candidat numéroté, mise, code)] à suivre (track.record_gap, APRÈS track.record)."""
    stats_note = statswindow.morning_note(pronos)
    n_events = len({c["id"] for c in picks})
    messages, sent_picks, sent_combos = [], [], []
    quality, top, top_gap, gaps, plan, rejected, _ = morning_plan(picks, gaps)
    again = _recorded_real(day)
    plan, resent = _resend_real(plan, again, picks)          # relance du même jour : les combinés déjà enregistrés sont renvoyés tels quels
    again_fair = _recorded_fair(day)
    built = [(t, c) for t, c in plan if c]
    missing = [t for t, c in plan if not c and t not in rejected and t not in again]
    built_fair = [(t, c) for t, c in fair_combos if c and CB.season_no(t) is None]
    built_season = [(t, c) for t, c in fair_combos if c and CB.season_no(t) is not None]      # voie `saison` : section à part, mêmes mécanismes (mise, coupon, bouton, suivi)
    n_ghost = 0
    ghost_line = ""                                  # combinés SANS valeur (toutes jambes fill) : construits et journalisés, jamais envoyés (NOVALUE_SEND les réaffiche) ; une relance garde ceux déjà envoyés
    for lst, kind, known in ((built, "matin réel", resent), (built_fair, "matin à vérifier", {str(k) for k in again_fair}),
                             (built_season, "saison", {str(k) for k in _recorded_season(day)})):
        for t, c in list(lst):
            if str(t) not in {str(k) for k in known} and fair.unsent(c[0]):
                lst.remove((t, c))
                n_ghost += fair.ghost_record(t, c, day, kind)
                if kind != "matin réel" and ghost_out is not None:
                    ghost_out.add(str(t))
            else:
                fair.ghost_drop(day, [l["key"] for l in c[0]])         # envoyé finalement (relance où une jambe a gagné de la valeur, NOVALUE_SEND) : plus un fantôme
    if n_ghost:
        ghost_line = fmt.i(f"👻 {n_ghost} combiné{'s' if n_ghost > 1 else ''} sans valeur suivi{'s' if n_ghost > 1 else ''} en fantôme, non envoyé{'s' if n_ghost > 1 else ''}")
    if top_gap and gap_out is not None:                  # ... mais il reste un écart sharp : marqué dans le suivi pour mesurer sa clôture contre Pinnacle
        gap_out.append((top, top_gap[0], None, None))
    stakes, spent, kept, poor = day_stakes(top, built, built_fair + built_season, gaps)
    cur = money.cur()
    if fair_stakes is not None:
        fair_stakes.update({t: st for (kind, t), st in ((k, v) for k, v in stakes.items() if isinstance(k, tuple) and k[0] == "fair")})   # 0 compris
    # sans budget ni capital : pas de montant, mais « Kelly ne conseille pas de miser dessus » reste dit quand le pari perd en moyenne
    mise = lambda st, prob=None, odds=None, nov=False, flat=False: money.stake_words(st if money.active() else None, prob, odds, novalue=nov, flat=flat)    # noqa: E731
    if picks:
        n_world = monde.total(world) if world else 0
        seen = (f"{n_world} matchs analysés dans le monde, {n_events} avec des cotes réelles" if n_world
                else f"{n_events} matchs analysés avec des cotes réelles")
        n_c = f"{len(built)} combiné{'s' if len(built) > 1 else ''} aujourd'hui" if built else "aucun combiné aujourd'hui"
        head = [fmt.b(f"🔥 Kelly — {fmt.fr_day(day)}"), "", f"📊 {seen}", f"🎯 {n_c}"]
        if built_season:
            head.append(f"{C.SEASON_TITLE.split(' ', 1)[0]} {len(built_season)} combiné{'s' if len(built_season) > 1 else ''} " + fmt.i("« saison en cours » (modèle des matchs de cette saison seulement)"))
        if built_fair:
            n_f = f"{len(built_fair)} combiné{'s' if len(built_fair) > 1 else ''}"
            if all(all(c.get("xodds") for c in legs) for _, (legs, _, _) in built_fair):      # tout est aux cotes relevées chez 1xBet
                head.append(f"💎 {n_f} aux cotes 1xBet "
                            + fmt.i("(code coupon à charger dans l'appli)" if all(all(c.get("xref") for c in legs)
                                                                                  for _, (legs, _, _) in built_fair)
                                    else "(cotes relevées chez 1xBet)"))
            else:
                head.append(f"🎲 {n_f} à vérifier chez 1xBet " + fmt.i("(cotes de Kelly, pas du bookmaker)"))
        if n_ghost:
            head.append(ghost_line)
        if money.capital_mode():
            head.append(money.capital_line())
        head.append(money.budget_line(spent, kept, poor) if money.active() else
                    "💡 " + fmt.i("Écris « capital 50000 » (ou touche « 💼 Mon capital ») pour avoir des mises conseillées en FCFA, prises sur ton "
                                  "capital avec une limite par jour. Seuls les paris que tu notes comptent dans tes résultats."))
        if stats_note:                               # pronostics stats sans cote 1xBet au matin : la fenêtre des stats (statswindow) les chiffre dans la journée
            head.append(fmt.i(fmt.esc(stats_note)))
        if missing or rejected:
            head.append("")
        if missing:
            head.append(fmt.i(fmt.esc(CB.skip_line(missing, n_events, anything=bool(quality)))))
        if rejected:
            head.append(fmt.i(fmt.esc(CB.ev_line(rejected))))
        messages.append(telegram.with_buttons(Html("\n".join(head)), chat.menu()))
    elif n_ghost:                                    # jour sans pari réel : la ligne passe seule (jamais un silence sur un combiné retenu en fantôme)
        messages.append(Html(ghost_line))
    if world:
        try:
            messages.append(monde.tour_du_monde(world, len(CB.best_per_match(pronos)), why_no_odds))
        except Exception as e:                      # noqa: BLE001 - le tour du monde est facultatif : le reste du matin part quand même
            print(f"[monde] message du tour du monde ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    if gaps:                                        # 💰 le seul écart que Kelly a mesuré : tout de suite après l'en-tête
        try:
            gmsg, rec = gapbets.message(gaps, picks, day, now_utc(), "matin", {g["key"]: stakes.get(("gap", g["key"]), 0) for g in gaps}
                                        if money.active() else None, when=odds_time)
            if gmsg is not None:
                messages.append(gmsg)
                if gap_out is not None:
                    gap_out.extend(rec)
        except Exception as e:                      # noqa: BLE001 - la section est facultative : le reste du matin part quand même
            print(f"[sharp] section « 1xBet paie plus que le marché » ignorée : {type(e).__name__}: {e}", file=sys.stderr)
    if top:
        if money.active():                          # mise 0 : notée telle quelle (stake 0, sans devise) = pas de mise sur le budget
            top["stake"] = stakes.get("pari", 0)
            if top["stake"]:
                top["cur"] = cur
        xtop = bool(top.get("xref") and top.get("xodds"))
        if xtop:                                    # pari du jour à la cote 1xBet : son code coupon (échec = pas de code)
            top["coupon"] = xbet.save_coupon([top])
        if top.get("xodds") and odds_time:
            top["xat"] = odds_time                  # heure de la cote 1xBet (« Mes paris »)
        messages.append(chat.with_played_button(fmt.single(top, CB.leg_reason(top), stake_txt=mise(stakes.get("pari"), top["prob"], top["odds"]),
                                                           coupon=top.get("coupon"), when=odds_time,
                                                           retry=RETRY_BUTTON if xtop and not top.get("coupon") else None),
                                                "pari", day, stake=stakes.get("pari"), xrefresh=xtop, code=top.get("coupon")))
        sent_picks.append(top)
    for title, combo in built:
        picks_, odds_, prob = combo
        st = stakes.get(title, 0) if money.active() else None      # mise conseillée en FCFA (0 : trop petite pour le budget) ; None sans budget
        xcombo = all(c.get("xref") and c.get("xodds") for c in picks_)
        old = resent.get(title) or {}                              # relance : même code coupon et même heure de cotes que le combiné déjà envoyé
        when = old.get("at") or odds_time if title in resent else odds_time
        coupon = (old.get("coupon") if title in resent and old.get("coupon") else xbet.save_coupon(picks_)) if xcombo else None      # combiné aux cotes 1xBet
        nov = bool(C.ENSEMBLE_ENABLED and ensemble.has_value(picks_) is False)       # sans valeur et envoyé (NOVALUE_SEND) : toujours dit, jamais de mise conseillée
        messages.append(chat.with_played_button(fmt.combo(title, picks_, odds_, prob, st, {c["key"]: CB.leg_reason(c) for c in picks_},
                                                          stake_txt=mise(st, prob, odds_, nov), coupon=coupon, when=when,
                                                          retry=RETRY_BUTTON if xcombo and not coupon else None),
                                                played.norm(fmt.tier_word(title)), day, stake=st, xrefresh=xcombo, code=coupon))
        sent_picks += picks_
        sent_combos.append({"tier": title, "keys": [c["key"] for c in picks_], "odds": round(odds_, 2), "prob": round(prob, 5),
                            "stake": st} | ({"cur": cur} if money.active() and st else {}) | fair._has_value(picks_) | {k: v for k, v in lanes.item_fields(picks_).items() if k == "frac"}
                           | ({"xlegs": [{"key": c["key"], "xodds": c["xodds"], "xref": c.get("xref")} for c in picks_],
                               "coupon": coupon, "at": when} if all(c.get("xodds") for c in picks_) else {}))      # de quoi refaire le coupon plus tard
    for target, (legs, min_total, prob) in built_fair + built_season:
        xfair = all(c.get("xref") and c.get("xodds") for c in legs)
        old = again_fair.get(str(target)) or {}
        old = old if set(old.get("keys") or []) == {c["key"] for c in legs} else {}      # relance : mêmes paris = même code coupon et même heure de cotes que l'envoi d'origine
        when = old.get("at") or odds_time if old else odds_time
        coupon = (old.get("coupon") if old.get("coupon") else xbet.save_coupon(legs)) if xfair else None
        if fair_coupons is not None and coupon:
            fair_coupons[target] = coupon
        k = [t for t, _ in built_season].index(target) + 1 if CB.season_auto(target) else None
        messages.append(fair_combo_message(target, legs, min_total, prob, day, stakes.get(("fair", target)), mise, coupon, when, k))
    if pronos:
        shown = CB.shown_pronos(pronos)          # toutes les jambes à valeur viables (toutes familles) + le meilleur pari du match
        if shown:
            msg = fmt.pronos(shown, C.FAIR_MAX if not (sent_combos or built_fair or built_season) else max(3, C.FAIR_MAX // 2), when=odds_time)
            if stats_note and not picks:             # pas d'en-tête (aucun pari réel) : la ligne passe sous la liste des pronostics
                msg = Html(msg + "\n\n" + fmt.i(fmt.esc(stats_note)))
            messages.append(msg)
    if not messages:
        messages.append(Html(fmt.b(f"🔥 Kelly — {fmt.fr_day(day)}") + "\nPas de cotes réelles aujourd'hui"
                             + (f" — {fmt.esc(why_no_odds)}" if why_no_odds else "")
                             + " : ni combiné ni pronostic. /monde pour les matchs du jour."))
    messages.append(DISCLAIMER)
    return messages, sent_picks, sent_combos


def single_key(sent_picks):
    """Clé du pari du jour parmi les paris envoyés par build_day : toujours le premier (`top`, envoyé avant les combinés ; il y a
    des combinés seulement s'il y a un `top`). Il peut aussi être une jambe d'un combiné : track.record le marque explicitement."""
    return sent_picks[0]["key"] if sent_picks else None


def leg_result_line(row):
    """« ✅ Flamengo gagne (2-0) » ; le match est rappelé quand le pari ne le nomme pas (« Plus de 2,5 buts »)."""
    icon = {"win": "✅", "loss": "❌", "push": "↩️", "void": "⚪", "unknown": "❔"}.get(row.get("result"), "⏳")
    pick = fmt.esc(row["pick"])
    if row["market"] not in ("h2h", "double_chance", "spreads") or row.get("name") == "Draw":
        pick = f"{fmt.esc(row['home'])} – {fmt.esc(row['away'])} : {pick}"
    return f"{icon} {pick}" + (f" ({fmt.esc(row['final'])})" if row.get("final") else "")


def _signed(x, item):
    """Gain d'un pari NOTÉ en FCFA (la devise de sa mise) ; « pas joué » s'il a été retiré avec l'ancien bouton « ❌ Pas joué »."""
    if item.get("skipped"):
        return fmt.i("pas joué")
    return fmt.esc(money.signed(x, item.get("cur")))


def single_price(b):
    """Prix du pari du jour dans le bilan du matin : la cote jouée chez 1xBet si elle est notée (/joue), sinon la cote 1xBet lue par
    Kelly (xodds), sinon la cote de référence (typique, pas 1xBet)."""
    if b.get("played"):
        return f"cote {fmt.odds_txt(b['played']['odds'])} chez 1xBet"
    if b.get("xodds"):
        return f"cote 1xBet {fmt.odds_txt(b['xodds'])}"
    return f"cote de référence {fmt.odds_txt(b['odds'])} (pas 1xBet)"


def single_gain_txt(b):
    """« · +1 500 FCFA » : gain du pari du jour s'il est noté (« ✅ Je l'ai joué », /joue) ; « · pas joué » s'il a été retiré (ancien bouton) ;
    '' sinon : un pari que tu n'as pas noté ne compte pas."""
    if b.get("skipped"):
        return f" · {_signed(0, b)}"
    g = money.gain(b)
    return f" · {_signed(g, b)}" if g is not None else ""


def combo_result(c, by_key):
    """Bloc de résultat d'un combiné terminé."""
    name = fmt.tier_name(c["tier"]).split(" ", 1)[-1]
    legs = [by_key.get(k) or {} for k in c["keys"]]
    if c["result"] == "win":
        odds_ = c.get("odds_final", c["odds"])
        if c.get("played"):
            odds_ = c["played"]["odds"] * odds_ / c["odds"]
        where = (" chez 1xBet" if c.get("played") else " aux cotes 1xBet" if c.get("xlegs")
                 else " (cotes de référence, pas 1xBet)")                 # le prix utilisé, dit clairement
        mine = single_gain_txt(c)                                          # seulement si tu l'as noté
        return "\n".join([f"{fmt.b(f'✅ {fmt.esc(name)} gagné — cote {fmt.odds_txt(odds_)}{where}')}{mine}"]
                         + [leg_result_line(r) for r in legs if r])
    if c["result"] == "void":
        return f"⚪ {fmt.b(fmt.esc(name) + ' annulé')} — les matchs n'ont pas eu lieu, mise rendue"
    if c["result"] == "unknown":                    # jambe jouée mais stats introuvables : ni gagné ni perdu, bankroll inchangée
        miss = ", ".join(f"{fmt.esc(r['home'])} – {fmt.esc(r['away'])} ({fmt.esc(r['pick'])})" for r in legs
                         if r.get("result") == "unknown")
        return "\n".join([f"❔ {fmt.b(fmt.esc(name) + ' non vérifiable')} : Kelly n'a pas trouvé les stats de {miss} (vérifie chez 1xBet)"]
                         + [leg_result_line(r) for r in legs if r])
    missed = [r for r in legs if r.get("result") == "loss"]
    ok = sum(1 for r in legs if r.get("result") in ("win", "push", "void"))
    why = ", ".join(f"{fmt.esc(r['home'])} – {fmt.esc(r['away'])}" + (f" ({fmt.esc(r['final'])})" if r.get("final") else "")
                    for r in missed)
    return (f"❌ {fmt.b(fmt.esc(name) + ' perdu')} — {ok} sur {len(c['keys'])}"
            + (f", raté : {why}" if why else "") + single_gain_txt(c))


def results_message(bets, rows, settled):
    """Résultats de la veille : un bloc par combiné (et le pari du jour), puis une ligne de bilan. None s'il n'y a rien de neuf."""
    by_key = {r["key"]: r for r in rows}
    single_keys = {(r.get("date"), r["key"]) for r in played.single_rows(rows, bets["combos"])}   # pari du jour (marque r["single"])
    done = [c for c in bets["combos"] if c.get("result") and not c.get("reported")]
    singles = [b for b in settled if b.get("sent") and (b.get("date"), b["key"]) in single_keys
               and b["result"] in ("win", "loss", "unknown")]
    gaps = [b for b in settled if b.get("sharpgap") and b.get("sent") and b.get("result") in ("win", "loss", "unknown")
            and (b.get("date"), b["key"]) not in single_keys and by_key.get(b["key"]) is not None and not by_key[b["key"]].get("gap_reported")]
    if not done and not singles and not gaps:
        return None
    blocks = []
    for b in singles:
        if b["result"] == "unknown":
            blocks.append(f"{fmt.b('❔ Pari du jour non vérifiable')} : Kelly n'a pas trouvé les stats de {fmt.esc(b['home'])} – "
                          f"{fmt.esc(b['away'])} (vérifie chez 1xBet)\n{leg_result_line(b)}")
            continue
        icon = "✅" if b["result"] == "win" else "❌"
        blocks.append(f"{fmt.b(f'{icon} Pari du jour — {single_price(b)}')}{single_gain_txt(b)}\n{leg_result_line(b)}")
    for b in gaps:                                  # 💰 paris « 1xBet paie plus que le marché » : annoncés avec leur code, leur résultat est dit aussi
        by_key[b["key"]]["gap_reported"] = True
        row = by_key[b["key"]]
        if b["result"] == "unknown":
            blocks.append(f"{fmt.b('❔ 1xBet paie plus que le marché — non vérifiable')} : Kelly n'a pas trouvé les stats de {fmt.esc(b['home'])} – "
                          f"{fmt.esc(b['away'])} (vérifie chez 1xBet)\n{leg_result_line(b)}")
            continue
        icon = "✅" if b["result"] == "win" else "❌"
        blocks.append(f"{fmt.b(f'{icon} 1xBet paie plus que le marché — {single_price(row)}')}{single_gain_txt(row)}\n{leg_result_line(b)}")
    for c in done:
        c["reported"] = True
        blocks.append(combo_result(c, by_key))
    played_ = [c for c in done if c["result"] in ("win", "loss")]       # « unknown » (non vérifiable) : hors des comptes
    wins = sum(1 for c in played_ if c["result"] == "win")
    head = f"Hier : {wins} combiné{'s' if wins > 1 else ''} gagné{'s' if wins > 1 else ''} sur {len(played_)}" if played_ else ""
    n, total, _ = money.result(bets, rows, fair.load_combos(), days=1)          # seulement les paris que tu as notés
    summary = " · ".join(x for x in (head, f"résultat de tes paris notés : {money.signed(total)}" if n else "") if x) or "Rien de noté hier"
    if bankroll.streak(bets["combos"]) >= C.STREAK_WARNING:
        summary += f"\n⚠️ {bankroll.streak(bets['combos'])} combinés perdus d'affilée : lève le pied ou fais une pause."
    return Html(fmt.b("📋 Résultats d'hier") + "\n\n" + "\n\n".join(blocks) + "\n\n" + fmt.i(fmt.esc(summary)))


def _step(label, fn, default=None):
    """Une partie du matin : un échec est écrit dans les logs et n'empêche ni les autres parties ni le message du matin."""
    try:
        return fn()
    except Exception as e:                          # noqa: BLE001 - un morceau en panne ne tue pas le matin
        print(f"[matin] {label} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return default


def morning_report(now):
    """Règle les paris d'hier, met à jour bankroll + apprentissage, renvoie les résultats en mots simples. Chaque partie est isolée (_step) : un
    échec est écrit dans les logs, les autres parties et le message du matin partent quand même. Les combinés à cote juste / sur mesure sont réglés AVANT
    le message des résultats : son total en argent compte ceux réglés ce matin-là."""
    got = _step("règlement des paris", lambda: settle.settle_pending(now))
    bets, settled = got if got else (store.load_doc("bets.json", {"picks": [], "combos": []}), [])
    if not money.active():                          # budget du jour : le bilan en argent remplace la bankroll
        _step("bankroll", lambda: bankroll.apply_results(bets["combos"]))
    _step("sauvegarde des paris", lambda: store.save("bets.json", bets))
    rows = _step("lecture du suivi", track.load, [])
    if not money.active():
        _step("bankroll (paris notés)", lambda: bankroll.apply_results([r for r in rows if r.get("played")], played_only=True))
    try:
        n = freeclose.fill(rows, now)               # cote finale gratuite des paris fantômes : l'apprentissage avance sans crédit
        print(f"[apprentissage] {n} cote(s) finale(s) gratuite(s) (football-data, BetExplorer)", file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - facultatif
        print(f"[apprentissage] cotes finales gratuites ignorées : {type(e).__name__}: {e}", file=sys.stderr)
    _step("sauvegarde du suivi", lambda: track.save(rows))
    model = _step("apprentissage", lambda: learn.update(rows, bets["combos"])) or learn.load()
    fair_settled = _step("règlement des combinés à vérifier", lambda: settle.settle_fair_combos(now))
    msgs = []
    res = _step("résultats d'hier", lambda: results_message(bets, rows, settled))
    if res:
        msgs.append(res)
        _step("sauvegarde des résultats", lambda: store.save("bets.json", bets))
        _step("sauvegarde du suivi (résultats annoncés)", lambda: track.save(rows))        # marque « gap_reported » des paris 1xBet en retard
    msgs += _step("pronostics sans cote", lambda: fair_report(now), [])
    got_ens = _step("journal des avis de l'ensemble", lambda: settle.settle_ensemble(now))      # après le règlement des pronostics : leurs stats sont en cache
    _step("journal des stats de coupe", lambda: settle.settle_cups(now))                      # même règlement gratuit, journal à part, rien n'est appris
    _step("journal de la voie cotes", lambda: settle.settle_oddsonly(now))            # fantôme : même règlement gratuit, aucun apprentissage
    got_sea = _step("journal de la voie saison", lambda: settle.settle_season(now))              # même règlement gratuit, journal et apprentissage à part
    if got_sea and (got_sea[0] or got_sea[1]):
        print(f"[saison] journal : {len(got_sea[0])} ligne(s) réglée(s), {got_sea[1]} apprise(s)", file=sys.stderr)
    cut = _step("coupure automatique de la voie stats", lambda: lanestats.apply(now.date()), [])      # après le règlement : juge les lignes réglées ; le message passe en PREMIER
    _step("carte des edges", lambda: edgemap.apply(now.date()))            # le lundi seulement, après la coupure : lecture seule, aucun pari ne change tant que EDGEMAP_GATE est éteint
    _step("familles de marché", lambda: familyeval.apply(now.date()))      # le lundi seulement, après la carte : admission des familles (hors_ticket -> fantome -> proposee), jamais jouee toute seule
    cut_msgs = _step("état des voies", lanestats.cut_lines, [])
    if cut_msgs:
        cut_msg = Html("\n".join(fmt.esc(x) for x in cut_msgs))
        cut_msg.loud = True                                                   # 🛑 voie coupée : sonne, même au milieu du bilan silencieux
        msgs.insert(0, cut_msg)        # coupure décidée ce matin ou en cours : même ligne 🛑, une seule fois
    got_live = _step("journal live", lambda: LV.settle(now))                  # apprentissage live, à part de l'avant-match
    if got_live and (got_live[0] or got_live[1]):
        print(f"[live] journal : {got_live[0]} pari(s) réglé(s), {got_live[1]} appris par tranche", file=sys.stderr)
    if got_ens and (got_ens[0] or got_ens[1]):
        print(f"[ensemble] journal : {len(got_ens[0])} ligne(s) réglée(s), {got_ens[1]} apprise(s)", file=sys.stderr)
    msgs += _step("combinés à vérifier", lambda: fair_combos_report(now, fair_settled), [])
    d = _step("apprentissage (texte)", lambda: learn.describe_simple(model, rows))
    if d:
        msgs.append(d)
    try:
        if weekly.due(now):                         # premier matin de la semaine : bilan d'apprentissage en mots simples
            w = weekly.message(now)
            if w:
                msgs.append(w)
            weekly.mark(now)
    except Exception as e:                          # noqa: BLE001 - facultatif
        print(f"[semaine] bilan d'apprentissage ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return msgs, model


def fair_report(now):
    """Règle les pronostics sans cote (gratuit) et résume les nouveaux résultats : [] s'il n'y a rien de neuf."""
    rows, settled = settle.settle_fair(now)
    new = [b for b in settled if b["result"] in ("win", "loss") and not b.get("reported") and not b.get("x")]   # x : suivi en silence
    if not new:
        return []
    lines, last = [fmt.b("🎲 Pronostics sans cote — résultats")], None
    for b in new[:12]:
        icon = "✅" if b["result"] == "win" else "❌"
        match = f"{fmt.esc(b['home'])} – {fmt.esc(b['away'])}"
        if match != last:
            lines.append("\n" + fmt.b(match))
            last = match
        lines.append(f"{icon} {fmt.esc(fair.pick_label(b))} (Kelly annonçait {fmt.pct(b['prob'])} de chances)")
    if len(new) > 12:
        lines.append(fmt.i(f"+ {len(new) - 12} autres"))
    summ = settle.fair_line(rows)
    if summ:
        lines.append("\n" + fmt.i(fmt.esc(summ)))
    for b in new:
        b["reported"] = True
    store.save(fair.FILE, rows)
    return [Html("\n".join(lines))]


FAIR_REPORT_DETAIL = 6      # combinés détaillés jambe par jambe dans le rapport du matin (les notés toujours) ; les suivants tiennent en une ligne


def _fair_combo_name(c):
    if played.is_custom(c):
        return played.custom_name(c)
    name = "Combiné " + (f"aux cotes 1xBet (cote {fmt.odds_txt(c['min_odds'])})" if played.is_real(c)
                         else f"à cote mini {fmt.odds_txt(c['min_odds'])}")
    shot = played._fair_name(str(c["tier"])) if CB.shots_title(str(c["tier"])) else None     # « Stats », « Tirs cadrés »... : jamais la clé interne (« ts5 »)
    return f"{name} — {shot}" if shot else name


def fair_combos_report(now, settled_=None):
    """Règle les combinés à cote juste (gratuit) et résume les nouveaux résultats : [] s'il n'y a rien de neuf. « gagné » veut dire que toutes
    les jambes sont passées ; seuls les combinés que tu as NOTÉS ont un résultat en argent. TOUS les combinés réglés sont annoncés, les notés
    d'abord (jamais perdus) : les FAIR_REPORT_DETAIL premiers (et tous les notés) avec leurs paris, les suivants en une ligne ; aucun combiné n'est
    marqué « reported » sans avoir été montré. `settled_` : (lignes, réglés) déjà calculés par settle.settle_fair_combos (le matin les règle avant
    le message des résultats : il ne faut pas les régler deux fois)."""
    rows, settled = settled_ if settled_ is not None else settle.settle_fair_combos(now)
    if any(c.get("played") and not c.get("counted") for c in settled):
        _step("bankroll (combinés notés)", lambda: bankroll.apply_results([c for c in rows if c.get("played")], played_only=True))
        store.save(fair.COMBOS_FILE, rows)
    new = [c for c in settled if c["result"] in ("win", "loss", "unknown") and not c.get("reported")]
    if not new:
        return []
    new.sort(key=lambda c: not (c.get("played") and not c.get("skipped")))          # tri stable : les notés d'abord
    detailed = max(FAIR_REPORT_DETAIL, sum(1 for c in new if c.get("played") and not c.get("skipped")))
    lines = [fmt.b("🎲 Combinés à vérifier chez 1xBet — résultats")]
    for k, c in enumerate(new):
        legs = c["legs"]
        ok = sum(1 for r in legs if r.get("result") in ("win", "push", "void"))
        icon = {"win": "✅", "loss": "❌"}.get(c["result"], "❔")
        word = {"win": "gagné", "unknown": "non vérifiable (stats introuvables : vérifie chez 1xBet)"}.get(
            c["result"], f"perdu — {ok} sur {len(legs)}")
        gain = money.gain(c)                                        # combiné noté : son résultat en argent (pas noté : rien)
        head = fmt.b(f"{icon} {_fair_combo_name(c)} {word}") + (f" · {fmt.esc(money.signed(gain, c.get('cur')))}" if gain is not None else "")
        if k < detailed:
            lines.append("\n" + head)
            lines += [leg_result_line(r) for r in legs if r.get("result")]
        else:
            lines.append(head)
    summ = settle.fair_combo_line(rows)
    if summ:
        lines.append("\n" + fmt.i(fmt.esc(summ)))
    for c in new:
        c["reported"] = True
    store.save(fair.COMBOS_FILE, rows)
    return [Html("\n".join(lines))]


def refresh_sources():
    """Actualise les stats gratuites (football-data, Understat, FBref) et l'amorçage historique avant de bâtir les
    paris. Téléchargements polis et mis en cache ; ne casse JAMAIS le run (erreur = message dans les logs)."""
    if not C.SOURCES_REFRESH:
        return {}
    out = {}
    http.set_deadline(C.SOURCES_SECONDS)            # cache froid : on reporte au lendemain ce qui dépasse
    try:
        out = sources.refresh_all(C.SPORTS)
        print("[sources] " + ", ".join(f"{k} : {v.get('rows', 0)} lignes" for k, v in out.items()), file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - les stats sont facultatives
        print(f"[sources] actualisation ignorée : {type(e).__name__}: {e}", file=sys.stderr)
    try:
        boot = bootstrap.ensure(C.BOOT_LEAGUES)
        print(f"[sources] amorçage : {len(boot.get('rows', []))} lignes historiques", file=sys.stderr)
    except Exception as e:                          # noqa: BLE001
        print(f"[sources] amorçage ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        http.set_deadline(None)
    return out


def fill_profiles(day, scan):
    """Remplit les profils API-Football du jour (apistats.fill) et écrit le bilan dans les logs. Ne casse jamais le run."""
    try:
        rep = apistats.fill(day, scan)
        for line in apistats.report_lines(rep):
            print(line, file=sys.stderr)
        return rep
    except Exception as e:                          # noqa: BLE001 - facultatif
        print(f"[apistats] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def sources_message(picks):
    """📡 État des sources (une ligne chacune) + couverture des stats d'avant-match du jour."""
    lines = [sources.report()]
    events = {c["id"] for c in picks if c.get("group") == "soccer"}
    if events:
        covered = {c["id"] for c in picks if c.get("pm")}
        adjusted = sum(1 for c in picks if "pgap" in (c.get("pm") or {}))
        lines.append(f"Stats d'avant-match : {len(covered)}/{len(events)} matchs couverts, "
                     f"{adjusted} paris ajustés par le modèle")
    return "\n".join(lines)


def world_report(scan, picks, day, now):
    """Tour du monde + pronostics sans cote. Écrit data/monde.json (relu par /monde, /ligues et les passages horaires,
    sans réseau) et data/fair.json (suivi). Ne casse jamais le run -> (cache | None si le tour du monde a échoué, pronostics)."""
    africa, ahead, pronos = [], None, []
    try:
        africa = stats.africa_matches(day)           # 1 requête API-Football, la même que le règlement (cache)
    except Exception as e:                          # noqa: BLE001
        print(f"[monde] Afrique ignorée : {type(e).__name__}: {e}", file=sys.stderr)
    if not scan.get("leagues") and not africa and not scan.get("error"):
        try:
            ahead = odds.scan_ahead(day, scan.get("sports", []))
        except Exception as e:                      # noqa: BLE001
            print(f"[monde] prochains matchs ignorés : {e}", file=sys.stderr)
    try:
        xb = C.SOURCES_REFRESH and C.XBET_ENABLED
        pronos = fair.make(scan, picks, day, deadline=time.monotonic() + C.FAIR_SECONDS, africa=africa, all_families=xb)
        pronos = pronos + cupstats.make(scan, now)         # stats des COUPES (profils de championnat) : fantôme par défaut, journal à part
        if xb:                        # cotes 1xBet réelles : meilleure ligne, ou pronostic écarté si mal payé (les paris réels donnent l'avis sharp)
            xprice.apply(pronos, picks=picks)
        tracked = [c for c in pronos if not c.get("cup")]                # les stats de coupe ne sont jamais suivies dans fair.json (calibration, moyennes)
        fair.record(tracked, day, shown=[c for c in CB.shown_pronos(pronos) if not c.get("cup")])     # suivis : les montrés + d'autres en silence (fiabilité)
    except Exception as e:                          # noqa: BLE001
        print(f"[fair] pronostics ignorés : {type(e).__name__}: {e}", file=sys.stderr)
    data = None
    try:
        data = monde.build(scan, day, now, paid=scan.get("paid", []), africa=africa, ahead=ahead)     # sans crédits : technique
        monde.save(data)
        print(f"[monde] {monde.total(data)} matchs, {len(data['leagues'])} ligues, {len(scan.get('paid', []))} payée(s), "
              f"{len(pronos)} pronostic(s) sans cote.", file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - le tour du monde ne tue pas le matin (data/monde.json reste celui d'avant)
        print(f"[monde] tour du monde ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return data, pronos


DAILY_FILE = "daily_done.json"
DELIVERY = {"sent": 0, "failed": 0}        # messages du dernier run_daily : envoyés / mis en attente ou refusés (voir daily_entry)


def daily_skip_reason(now):
    """Pourquoi le matin ne doit PAS tourner maintenant (None = il peut). run_daily note dans data/daily_done.json le jour UTC `day`
    (= now.date()) qu'il a terminé. Règle : (1) déjà terminé pour la date du jour -> doublon (rattrapage systemd Persistent au
    démarrage du PC puis timer du matin, ou relance manuelle) ; (2) lancé avant C.DAY_END_HOUR h UTC alors que le jour précédent est
    terminé -> son « jour Kelly » court jusqu'à C.DAY_END_HOUR h : ces matchs sont déjà couverts, le vrai passage du matin suivra ;
    (3) lancé avant C.DAY_END_HOUR h sans que la veille ait été faite -> rattrapage de la veille manquée : il tourne et compte pour
    la date du jour, donc le passage du matin qui suit est ignoré par (1). `--force` passe outre."""
    done = store.load(DAILY_FILE, {}).get("day")
    today = now.date()
    if done == str(today):
        return f"le matin du {today} est déjà fait (data/{DAILY_FILE})"
    if now.hour < C.DAY_END_HOUR and done == str(today - timedelta(days=1)):
        return f"{now:%H:%M} UTC < {C.DAY_END_HOUR} h : le jour Kelly du {done} court encore et a déjà été traité"
    return None


def daily_entry(sample=False, force=False):
    """Point d'entrée de `python bot.py --daily [--force]` -> code de sortie. Ignore le run (0, note sur stderr) si daily_skip_reason le
    juge doublon, sauf `force`. 1 si AUCUN message du matin n'a pu partir (réseau / Telegram en panne : ils sont en attente dans
    data/outbox.json, les données du jour sont bien écrites), pour que systemd le signale."""
    if not (sample or force):
        why = daily_skip_reason(now_utc())
        if why:
            print(f"[daily] ignoré : {why}. Relance avec --force pour le refaire.", file=sys.stderr)
            return 0
    try:
        run_daily(sample, retry=not force)
    except MorningRetry as e:
        print(f"[daily] {e} : rien n'a été fait ni envoyé, nouvel essai dans 30 min (essai {e.n}/{C.DAILY_SCAN_TRIES}).", file=sys.stderr)
        return RETRY_RC
    return 1 if DELIVERY["failed"] and not DELIVERY["sent"] else 0


RETRY_RC = 75                       # code de sortie « réessaie plus tard » (le runner relance le matin 30 min après)
TRY_FILE = "daily_try.json"         # {"day", "n", "error"} : essais du matin ratés faute de liste des matchs


class MorningRetry(Exception):
    """La liste des matchs du jour (The Odds API) est injoignable : le matin s'arrête AVANT tout règlement et tout message, pour être refait 30 min plus
    tard (une mauvaise minute à 04:00 ne perd plus la journée). Au C.DAILY_SCAN_TRIES-ième essai, il part quand même (le message le dit)."""

    def __init__(self, error, n):
        super().__init__(f"liste des matchs injoignable ({str(error)[:120]})")
        self.n = n


def _scan_failed(day, error):
    """Note un essai raté faute de liste des matchs -> (réessayer plus tard ?, n° de l'essai). Le runner donne le n° de l'essai du jour (KELLY_DAILY_TRY :
    TOUS ses essais, même ceux coupés pour une autre raison) : le dernier essai qu'il accorde part toujours. Sans lui : les essais ratés notés ici."""
    t = store.load(TRY_FILE, {})
    n = (t.get("n", 0) if isinstance(t, dict) and t.get("day") == str(day) else 0) + 1
    store.save(TRY_FILE, {"day": str(day), "n": n, "error": str(error)[:200]})
    try:
        n = max(n, int(os.environ.get("KELLY_DAILY_TRY") or 0))
    except ValueError:
        pass
    return n < C.DAILY_SCAN_TRIES, n


def log_opinions(rows, day, now=None):
    """Journal des avis de l'ensemble (ensemble.record) : le modèle, 1xBet sans marge et le book de référence de chaque pronostic ou pari réel, gardés jusqu'à
    leur règlement (settle.settle_ensemble) pour que les poids apprennent dans TOUTES les familles, décidées par l'ensemble ou non. Ne lève jamais.
    -> nombre de lignes ajoutées."""
    try:
        gapdist.observe(rows, now or now_utc())     # décision 5 : distribution des écarts 1xBet / sharp par heure (document à part, aucun effet sur un pari) ; ne lève jamais
    except Exception as e:                          # noqa: BLE001
        print(f"[gapdist] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    if not ensemble.logging_on():
        return 0
    try:
        cup_rows, rows = cupstats.split(rows)                       # stats de coupe : journal à part (data/cup_log.json), jamais celui de l'ensemble
        cupstats.record(cup_rows, day, now or now_utc())
        n = ensemble.record(rows, str(day), now or now_utc())
        print(f"[ensemble] {n} avis ajouté(s) au journal ({len(rows)} pari(s) examiné(s))", file=sys.stderr)
        return n
    except Exception as e:                          # noqa: BLE001 - le journal est facultatif : le matin part quand même
        print(f"[ensemble] journal des avis ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def morning_gaps(picks, pronos, day, odds_at):
    """Écarts sharp du matin (sharpgap.find) sur les paris réels remis au prix 1xBet : la cote 1xBet dépasse la cote juste d'un book de référence lu il y a moins de
    C.SHARPGAP_MAX_AGE_MIN minutes. [] si l'option est éteinte ou sans paris chiffrés. Ne lève jamais."""
    if not C.SHARPGAP_ENABLED or not picks or not lanes.gaps_allowed():      # voie sharpgap « off » / « ghost » : aucun écart annoncé ni numéroté (reserve) ni suivi (record_gap)
        return []
    picks = [c for c in picks if lanes.usable(c) and lanes.ticket_ok(c)]     # un écart sur un marché de stats d'une ligue en fantôme n'est pas annoncé non plus ; ni sur une famille hors voies
    if not picks:
        return []
    try:
        found = sharpgap.find({"day": str(day), "picks": picks, "pronos": pronos, "picks_at": odds_at, "xbet_at": odds_at}, now_utc())
        print(f"[sharp] {len(found)} écart(s) : 1xBet paie plus que la cote juste des books de référence", file=sys.stderr)
        return found
    except Exception as e:                          # noqa: BLE001
        print(f"[sharp] écarts ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        return []


REPAIR_LABELS = {"season_weights.json": "les poids de la voie saison", "season_calib.json": "la fiabilité de la voie saison", "strategy_state.json": "la coupure automatique des voies", "calib.json": "la fiabilité par marché", "calib_boot.json": "le rejeu de la saison passée", "means.json": "les moyennes des stats",
                 "ensemble.json": "les poids modèle / marché", "live_calib.json": "la correction du direct", "tuning.json": "le réglage des championnats",
                 "model.json": "le modèle de classement", "bets.json": "tes paris et combinés", "tracked.json": "les paris suivis",
                 "fair.json": "les pronostics suivis", "fair_combos.json": "les combinés à cote juste", "mybudget.json": "ton capital"}


def announce_repairs():
    """🛠️ Documents précieux trouvés abîmés (store._repair) pas encore annoncés : UN message, puis marqués annoncés. Ne lève jamais."""
    try:
        new = []

        def mark(r):
            r = r if isinstance(r, list) else []
            for x in r:
                if isinstance(x, dict) and not x.get("said"):
                    new.append(dict(x))
                    x["said"] = 1
            return (r if new else None), None
        store.update(store.REPAIRS, mark, [])
        if not new:
            return None
        lines = ["🛠️ " + fmt.b("Kelly a réparé sa mémoire")]
        for x in new:
            what = REPAIR_LABELS.get(x["name"], x["name"])
            lines.append(fmt.esc(f"• {what} : document abîmé, remis tel qu'il était le {fmt.fr_day(x['from'])} (ce qui a changé depuis est perdu)"
                                 if x.get("from")
                                 else f"• {what} : document abîmé et pas encore de copie : Kelly n'y écrit plus. Restaure la dernière sauvegarde "
                                      "reçue ici (kelly restaurer dans Termux)."))
        msg = telegram.Html("\n".join(lines))
        telegram.send(telegram.topic(msg, "tech"))
        return msg
    except Exception as e:                          # noqa: BLE001
        print(f"[store] annonce des réparations ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def record_fair_morning(fair_combos, day, fair_stakes, fair_coupons, odds_at, ghost=()):
    """Enregistre (fair_combos.json) les combinés à vérifier du matin ENVOYÉS ; `ghost` (build_day, ghost_out) : paliers retenus en fantôme (sans valeur, non envoyés), qui n'y entrent
    jamais : ni mise, ni coupon, ni bouton. Un palier est exclu parce que build_day l'a RETIRÉ, pas parce que ses clés ressemblent à un fantôme d'une autre lecture. -> nombre enregistré."""
    ghost = {str(t) for t in ghost}
    return fair.record_combos([(t, c) for t, c in fair_combos if c and str(t) not in ghost], day, fair_stakes, money.cur() if fair_stakes else None,
                              coupons=fair_coupons, at=odds_at, replace=True)


def run_daily(sample=False, retry=False):
    """Le passage du matin. `retry` (lancé par le runner, pas --force) : liste des matchs injoignable -> MorningRetry (rien n'est fait ni envoyé) tant
    que C.DAILY_SCAN_TRIES essais n'ont pas eu lieu aujourd'hui."""
    now = now_utc()
    scan = None
    DELIVERY.update(sent=0, failed=0)
    migrate.ensure()                                # données d'avant le mode « je note en FCFA » (une seule fois)
    telegram.flush_outbox()                         # messages restés en attente (réseau coupé à l'heure prévue) : avant les nouveaux
    if not sample:
        announce_repairs()                          # 🛠️ mémoire réparée depuis le dernier matin
    cache.reset()
    if sample:
        events = load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        after, report, model = None, [], learn.load()
        now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    else:
        progress.start()                            # « [progression] 45 % · … », systemctl status, « état » sur Telegram
        refresh_sources()                           # avant le bilan : le règlement gratuit lit les CSV à jour
        day, after = now.date(), now
        scan = odds.scan_today(day, after)          # gratuit : toutes les ligues en saison, jouées ou non ; AVANT le bilan (voir MorningRetry)
        if retry and scan.get("error"):
            again, n = _scan_failed(day, scan["error"])
            if again:
                raise MorningRetry(scan["error"], n)
        progress.step("bilan")
        report, model = morning_report(now)
        moved = _step("marchés entrés / sortis des combinés", calib.combo_changes, [])
        if moved:
            report = report + [Html("\n".join(fmt.esc(x) for x in moved))]
        report = report + _step("paris joués sans résultat", lambda: played.ask_messages(now), [])     # ❓ un appui : gagné / perdu / remboursé
        progress.step("monde")
        progress.step("profils")
        fill_profiles(day, scan)                    # stats mondiales API-Football : avant l'achat des cotes (elles guident les marchés payés)
        progress.step("cotes")
        events = odds.fetch_events(day, after=now, scan=scan)
        print(f"[budget] {budget.line()}", file=sys.stderr)
        progress.step("stats")
    picks = odds.build_picks(events, day, after, history=store.load("odds_history.json", {}), model=model)
    world, pronos, why, fair_combos, odds_at, gaps, season_legs = None, [], None, [], None, [], []
    if not sample:
        try:
            picks = odds.rescore(stats.enrich(picks, day), model)       # modèle appris : après les stats d'avant-match
        except Exception as e:                      # noqa: BLE001 - les stats sont facultatives
            print(f"[stats] ignorées : {e}", file=sys.stderr)
        if picks:
            store.save("odds_history.json", odds.snapshot(picks))
        try:
            print(sources_message(picks), file=sys.stderr)       # détail technique : dans les logs et --check, pas dans Telegram
        except Exception as e:                      # noqa: BLE001
            print(f"[sources] rapport ignoré : {e}", file=sys.stderr)
        # + championnats hors The Odds API proposés par 1xBet (via le proxy) : APRÈS l'achat des cotes et les profils API-Football,
        # pour ne prendre ni crédit ni quota aux championnats de The Odds API
        progress.step("xworld")
        xworld.extend_scan(scan, day, now)
        progress.step("pronos")
        world, pronos = world_report(scan, picks, day, now)
        progress.step("xbet")
        if C.SOURCES_REFRESH and C.XBET_ENABLED:    # paris réels à la cote 1xBet : après le relevé des pronostics (mêmes matchs en cache)
            try:
                xprice.price_picks(picks, pronos=pronos, model=model)
            except Exception as e:                  # noqa: BLE001 - facultatif : sans cote 1xBet, les cotes typiques restent
                print(f"[1xbet] paris réels ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        season_legs = _step("voie saison", lambda: seasonlane.run(scan, picks, day, now_utc())[0], [])      # jambes de la voie `saison` (journal à part) : après les paris réels, leur avis sharp
        progress.step("combis")
        odds_at = now_utc().strftime("%H:%M")       # fin du relevé 1xBet : l'heure des cotes des combinés
        log_opinions(list(pronos) + list(picks), day)          # avis de l'ensemble (journal « fantôme » de toutes les familles)
        gaps = morning_gaps(picks, pronos, day, odds_at)
        if not scan.get("paid"):
            why = budget.why_not(len(C.FEATURED["soccer"].split(",")), "daily")
            why = "budget de cotes du jour épuisé" if why else None
    usage = _step("réutilisation des matchs", lambda: morning_plan(picks, gaps)[6] if picks else CB.Usage(), None)      # UN Usage pour toute la journée : combinés réels, à vérifier, saison
    if pronos and usage is not None:
        try:
            fair_combos = _resend_fair(pronos, _recorded_fair(day), fair_plan_all(pronos, picks, gaps, usage=usage))
        except Exception as e:                      # noqa: BLE001 - les combinés à cote juste sont facultatifs
            print(f"[fair] combinés à cote juste ignorés : {type(e).__name__}: {e}", file=sys.stderr)
    if season_legs:
        fair_combos = list(fair_combos) + (_step("combinés saison", lambda: season_plan_all(season_legs, day, usage if usage is not None else CB.Usage()), []) or [])
    fair_stakes, fair_coupons, gap_rows, ghost_out = {}, {}, [], set()
    messages, sent_picks, sent_combos = build_day(picks, day, world, pronos, why, fair_combos, fair_stakes, odds_time=odds_at,
                                                  fair_coupons=fair_coupons, gaps=gaps, gap_out=gap_rows, ghost_out=ghost_out)
    if not sample:                                  # ⚠️ Ce matin : journée inhabituelle, relevé 1xBet coupé, proxy refusé (rien si tout va bien)
        alert = _step("contrôle du jour", lambda: dayhealth.message(day, dayhealth.counts(world, picks, pronos), now_utc()))
        if alert:
            messages = [alert] + messages
    track.record(picks, {c["key"] for c in sent_picks}, day, now, single_key=single_key(sent_picks))
    if gap_rows:                                    # 💰 suivis comme les autres paris, APRÈS track.record (le pari du jour garde sa marque)
        try:
            track.record_gap(gap_rows, day, now, read_at=odds_at)
        except Exception as e:                      # noqa: BLE001 - le suivi neuf ne bloque pas le matin
            print(f"[sharp] suivi des écarts ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    settle.record_combos(sent_combos, day, replace=True)       # relance du même jour : voir _resend_real
    record_fair_morning(fair_combos, day, fair_stakes, fair_coupons, odds_at, ghost_out)
    keep = keep_pronos(pronos, fair_combos)         # /top, /match, /combi et le relevé de 15:00 les relisent
    # xbet_at : heure du dernier relevé 1xBet des pronostics (relevés de l'après-midi compris), picks_at : celle des paris réels (le matin) ;
    # affichées avec les cotes 1xBet (/top, /match, /analyse)
    store.save("today.json", {"day": str(day), "picks": picks,
                              "pronos": [{k: c[k] for k in c if k != "score"} | ({"xat": odds_at} if odds_at and c.get("xodds") else {}) for c in keep],
                              "season": [{k: c[k] for k in c if k != "score"} | ({"xat": odds_at} if odds_at else {}) for c in season_legs]}
               | ({"xbet_at": odds_at} if odds_at and any(c.get("xodds") for c in keep) else {})
               | ({"picks_at": odds_at} if odds_at and any(c.get("xodds") for c in picks) else {}))
    progress.step("envoi")
    deliver_morning([telegram.topic(m if getattr(m, "loud", False) else telegram.silent(m), "report") for m in report] + [telegram.topic(m, "combos") for m in messages], day, now, sample)       # bilan, apprentissage, questions : sans sonnerie ; les combinés du matin sonnent          # le garde-fou « matin fait » n'est écrit qu'APRÈS l'envoi
    if not sample:                                  # APRÈS l'envoi : jamais un retard du matin ; pool du jour gardé, quatre variantes en fantôme (jamais envoyées, hors Usage)
        _step("variantes du constructeur", lambda: combovariants.run(day, picks, pronos, season_legs))
    print(cache.line(), file=sys.stderr)
    if not sample:
        _step("instantané de la mémoire", lambda: store.snapshot(str(day)))     # copie du jour des documents précieux (3 jours gardés)
        progress.step("cloture")
        run_track()
    cache.flush()
    progress.finish()
    return report + messages


def deliver_morning(messages, day, now, sample=False):
    """Envoie les messages du matin (un échec réseau les met en attente dans data/outbox.json, voir telegram.send), PUIS note le jour dans
    data/daily_done.json (pas pour --sample) : un arrêt en pleine route (SIGTERM, coupure) laisse le matin « pas fait », il pourra être refait au
    lieu de perdre des messages sans que rien ne le dise. Les mises et les paris sont déjà écrits avant l'envoi (une relance ne les double pas)."""
    for m in messages:
        DELIVERY["sent" if telegram.send(m) else "failed"] += 1
    if not sample:
        store.save(DAILY_FILE, {"day": str(day), "at": now.isoformat()})


def fair_plan_all(pronos, picks=None, gaps=(), usage=None):
    """Tous les combinés à cote juste du matin (paliers 5 / 12 / 25 puis « tirs cadrés », « tirs vers le but », « stats ») avec UN SEUL Usage, celui de toute la
    journée : avec `picks` (paris réels du matin), il part de ce que les combinés réels, le pari du jour et les écarts sharp `gaps` ont déjà pris (morning_plan) ; un
    match n'est jamais repris dans plusieurs tickets au-delà des règles de réutilisation (une jambe risquée une seule fois, sûre LEG_REUSE_MAX fois)."""
    if usage is None:
        usage = morning_plan(picks, gaps)[6] if picks else CB.Usage()
    return CB.fair_plan(pronos, usage=usage) + CB.fair_shots_plan(pronos, usage=usage)


def keep_pronos(pronos, fair_combos=()):
    """Pronostics gardés dans today.json (relus par /top, /match, /combi et le relevé de 15:00) : TOUJOURS les paris montrés (toutes les
    jambes à valeur, CB.shown_pronos), chaque jambe des combinés du jour et toute jambe possible d'un combiné (CB.fair_legs : « combi N » les rebâtit), puis,
    jusqu'à la limite, les 40 meilleurs des autres et ceux assez solides pour un combiné à cote juste ou trouvés chez 1xBet (relus à 15:00).
    Dans l'ordre du classement."""
    must = {c["key"] for c in CB.shown_pronos(pronos)} | {l["key"] for _, combo in fair_combos if combo for l in combo[0]}
    must |= {c["key"] for c in CB.fair_legs(pronos)}        # toutes les jambes possibles d'un combiné : « combi N » et la relecture de 15:00 les rebâtissent (rejeu du 09/10 : 3 sur 26 perdues)
    must |= {c["key"] for legs in _bold_pools([], pronos, None) for c in legs}    # paris probables chiffrés, mal payés compris : « 🎲 » et « min N » s'en servent
    must |= {c["key"] for c in pronos if c.get("cup")}       # stats de coupe (fantôme comprises) : relues à 15:00 et par la fenêtre des stats, sans quoi leur CLV serait sous-échantillonné
    first = [c for c in pronos if c["key"] in must]
    rest = [c for c in pronos if c["key"] not in must]
    n = max(0, 40 - len(first))
    fill = rest[:n] + [c for c in rest[n:] if CB.fair_eligible(c) or c.get("xgid")][:300]
    chosen = {id(c) for c in first + fill}
    return [c for c in pronos if id(c) in chosen]


def plan_closing(due, combos, bulk=True):
    """Appels de clôture à faire, du plus prioritaire au moins prioritaire (palier Sûr d'abord, puis grosse
    mise, puis fantômes) : [{kind: "bulk"|"event", sport_key, ids, markets, cost, rank}].
    Une ligue avec >= 2 matchs à relever sur les marchés vedettes : un seul appel bulk (coût = nb de
    marchés) au lieu d'un appel par match, tant qu'il ne coûte pas plus cher. Les autres marchés
    (corners, cartons, joueurs) : un appel par match, seulement les marchés suivis."""
    rk = track.ranks(combos)
    evs = {}
    for r in due:
        e = evs.setdefault((r["sport_key"], r["id"]), {"markets": set(), "rank": (99, 0)})
        e["markets"].add(r["market"])
        e["rank"] = min(e["rank"], rk.get(r["key"], (-1, 0)) if r.get("sent") else (99, 0))
    leagues, calls = {}, []
    for (sk, eid), e in evs.items():
        leagues.setdefault(sk, []).append((eid, e))
    for sk, items in leagues.items():
        feat = set(C.FEATURED.get(odds.sport_group(sk), "h2h").split(","))
        need = [(eid, e) for eid, e in items if e["markets"] & feat]
        union = set().union(*(e["markets"] & feat for _, e in need))
        use_bulk = bulk and len(need) >= 2 and len(union) <= sum(len(e["markets"] & feat) for _, e in need)
        if use_bulk:
            calls.append({"kind": "bulk", "sport_key": sk, "ids": [eid for eid, _ in need], "markets": union,
                          "cost": len(union), "rank": min(e["rank"] for _, e in need)})
        for eid, e in items:
            rest = e["markets"] - feat if use_bulk else e["markets"]
            if rest:
                calls.append({"kind": "event", "sport_key": sk, "ids": [eid], "markets": rest,
                              "cost": len(rest), "rank": e["rank"]})
    return sorted(calls, key=lambda c: (c["rank"], c["cost"]))


COMBO_UPDATES = "combo_updates.json"     # dernière version envoyée de chaque combiné du matin : {"day", "tiers": {palier: {keys, total, legs, dead?}}}


LAST_REFRESH = {"matches": 0, "new": 0, "updated": 0, "single": 0, "gap": 0}       # bilan du dernier run_xbet_refresh (réponse du relevé manuel)


def _current_day(today, now):
    """Le today.json est-il celui du jour Kelly en cours ? Le jour Kelly du matin va jusqu'au lendemain à C.DAY_END_HOUR h UTC (avant
    cette heure : la veille) ; le rattrapage d'avant C.DAY_END_HOUR h (voir daily_skip_reason) écrit, lui, la date UTC du jour."""
    kday = now.date() if now.hour >= C.DAY_END_HOUR else now.date() - timedelta(days=1)
    return today.get("day") in (str(kday), str(now.date()))


def _sent_keys(day):
    """Clés des pronostics déjà dans un combiné du jour (combinés du matin, dernière version envoyée, combinés sur mesure gardés) : la relecture du soir
    ne les retire jamais de today.json."""
    keys = set()
    for row in fair.load_combos():
        if row.get("date") == day:
            keys |= set(row.get("keys") or []) | {l.get("key") for l in row.get("legs") or []}
    for t in _load_updates(day)["tiers"].values():
        keys |= set(t.get("keys") or []) | {l.get("key") for l in t.get("legs") or []}
    for it in custom._load(day)["items"].values():
        keys |= {l.get("key") for l in it.get("legs") or []}
    return keys - {None}


def run_evening(now=None, fetch=None, seconds=None):
    """Relecture du SOIR (`python bot.py --evening`) : les matchs du jour Kelly de data/today.json pas encore commencés (et pas imminents :
    C.EVENING_SOON_MIN min) sont réanalysés avec fair.make (toutes les familles, stats comprises : les profils FotMob se sont remplis depuis le
    matin) à partir des SEULES sources déjà en cache (aucun appel payant, aucun quota API-Football : day=None), puis remis aux cotes 1xBet
    (xprice.apply, session et plafonds habituels, C.EVENING_SECONDS au plus, matchs à pronostics stats d'abord). Fusion dans today.json :
    seuls les pronostics des matchs RELUS chez 1xBet sont remplacés (les autres gardent leurs pronostics et leurs cotes) ; sont conservés tels
    quels : les paris du jour (picks), les pronostics des matchs commencés ou imminents, ceux qui sont déjà jambe d'un combiné envoyé ou gardé
    (_sent_keys) ; les suivis (fair.json : seulement de NOUVELLES lignes), combinés (fair_combos.json), notes et bets.json ne sont pas touchés.
    Journal des avis de l'ensemble mis à jour. UN seul message « 🌙 Pronostics de ce soir mis à jour : N 💎 » (les 💎 pas encore annoncés, par match et
    marché), rien s'il n'y en a pas ; aucun combiné envoyé tout seul : « combi N » les rebâtit. Idempotent, ne lève jamais.
    `fetch` : injectable pour les tests. -> nombre de 💎 annoncés."""
    try:
        return _run_evening(now or now_utc(), fetch, seconds)
    except Exception as e:                          # noqa: BLE001 - une relecture facultative ne casse jamais rien
        print(f"[soir] relecture du soir ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def _run_evening(now, fetch, seconds):
    import copy
    from .sources import xbet
    migrate.ensure()
    today = store.load("today.json", {})
    today = today if isinstance(today, dict) else {}
    if not _current_day(today, now) or not today.get("pronos"):
        print(f"[soir] pas de pronostics du jour Kelly en cours (today.json : {today.get('day')}) : rien à relire", file=sys.stderr)
        return 0
    day, soon = today["day"], now + timedelta(minutes=C.EVENING_SOON_MIN)
    telegram.flush_outbox()
    evs = {}
    for c in today["pronos"]:
        if c.get("sport_key") == "africa" or datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) <= soon:
            continue                                # commencé / imminent, ou match africain (profils API-Football seuls : non relu ici)
        ev = evs.setdefault(c["id"], {"id": c["id"], "sport_key": c["sport_key"], "league": c.get("league"), "home_team": c["home"],
                                      "away_team": c["away"], "commence_time": c["commence"]})
        if c.get("xgid") and "xgid" not in ev:
            ev["xgid"] = c["xgid"]                  # match 1xBet déjà connu : lu directement, sans menu ni liste
    if not evs:
        print("[soir] aucun match à venir à relire", file=sys.stderr)
        return 0
    scan = {"leagues": {}}
    for ev in evs.values():
        scan["leagues"].setdefault(ev["sport_key"], []).append(ev)
    picks = today.get("picks") or []
    new = fair.make(scan, picks, None, deadline=time.monotonic() + C.FAIR_SECONDS, africa=(), all_families=True)      # day=None : jamais API-Football
    new = new + cupstats.make(scan, now)                 # stats des coupes (fair.make ne les fait plus) : relues et journalisées comme le matin
    first = _priority_ids(day, [c for c in today["pronos"] if c["id"] in evs])
    limit = time.monotonic() + (seconds or C.EVENING_SECONDS)
    got = set()

    def run(events, deadline=None, wanted=None):
        events = sorted(xbet.prioritise_stats(events, wanted), key=lambda e: e[1]["id"] not in first)       # tri stable : stats d'abord, puis les matchs qui comptent
        deadline = min(deadline, limit) if deadline else limit
        found = (fetch or xbet.fetch_day)(events, deadline=deadline, wanted=wanted)
        got.update(found)
        return found
    xprice.apply(new, fetch=run, picks=copy.deepcopy(picks))
    if not got:
        print("[soir] 1xBet n'a relu aucun match : today.json inchangé", file=sys.stderr)
        return 0
    stamp = now.strftime("%H:%M")
    redo = {i for i in got if i in evs}
    fresh = [{k: c[k] for k in c if k != "score"} | ({"xat": stamp} if c.get("xodds") else {}) for c in new if c["id"] in redo]
    protected = _sent_keys(day)
    old = today["pronos"]
    kept = [c for c in old if c["id"] not in redo or c["key"] in protected]
    held = {_slot(c) for c in kept if c["id"] in redo}
    merged = kept + [c for c in fresh if _slot(c) not in held]
    if "announced" not in today:                    # premier relevé du jour : les 💎 du matin sont déjà connus
        today["announced"] = [_slot(c) for c in CB.shown_pronos([c for c in old if (c.get("commence") or "9")[:16] > soon.isoformat()[:16]])
                              if c.get("xodds")]
    seen = {_slot(k) for k in today["announced"]}
    upcoming_ = [c for c in merged if (c.get("commence") or "9")[:16] > soon.isoformat()[:16]]
    announce = [c for c in CB.shown_pronos(upcoming_) if c["id"] in redo and c.get("xodds") and _slot(c) not in seen][:C.EVENING_LIMIT]      # la limite du message : une jambe non montrée n'est pas marquée annoncée
    today["pronos"] = merged
    today["announced"] += [_slot(c) for c in announce]
    today["xbet_at"] = stamp
    store.save("today.json", today)
    log_opinions([c for c in fresh if _slot(c) not in held], day, now)
    if announce:
        have = {(r.get("date"), r.get("id"), r.get("market")) for r in fair.load()}
        rec = [c for c in announce if (day, c["id"], c["market"]) not in have]      # suivi : seulement de NOUVELLES lignes (jamais une ligne existante)
        if rec:
            fair.record(rec, day, shown=rec)
        late = now.hour >= 17                       # relecture de 12:00 (timer) : « de la journée » ; lancée le soir : « de ce soir »
        telegram.send(telegram.topic(fmt.pronos(announce, limit=C.EVENING_LIMIT,
                                 title=f"{'🌙 Pronostics de ce soir' if late else '🔄 Pronostics de la journée'} mis à jour : {len(announce)} 💎",
                                 when=stamp, intro=f"Matchs {'de ce soir' if late else 'à venir'} réanalysés avec les statistiques du moment et remis aux cotes 1xBet. "
                                                   "Vérifie la cote avant de valider ; « combi N » bâtit un combiné avec ces paris."), "combos"), silent=True)
    print(f"[soir] {len(redo)}/{len(evs)} match(s) à venir relus chez 1xBet, {len(announce)} 💎 annoncé(s)", file=sys.stderr)
    return len(announce)


def _stake_txt(row):
    """Mise d'un pari suivi (combiné de fair_combos.json / bets.json, pari du jour) pour un message renvoyé : celle que tu as NOTÉE si tu l'as
    jouée, sinon la mise conseillée du budget du jour (« 1 500 FCFA », « pas de mise (trop petite pour ton budget) ») ; None sans budget du
    jour (le message dit alors de le régler)."""
    if row.get("played"):
        return money.money(row["played"]["stake"], row.get("cur"))
    if not money.active():
        return None
    st = money.suggested(row)
    if st:
        return money.money(st)
    it = money.row_item(row) or {}
    return money.novalue_txt() if it.get("novalue") else money.NOFILL_TXT if it.get("flat") else money.no_stake_txt(it.get("prob"), it.get("odds"))


def _record_version(sent, row, rows, combo, hhmm, coupon=None):
    """Nouvelle version envoyée d'un combiné : retenue dans COMBO_UPDATES (base des prochaines vérifications et du bouton « Vérifier ce
    combiné ») et dans sa ligne de suivi fair_combos.json (règlement et bilan comptent la version envoyée en dernier, avec la mise du
    matin ; « Mes paris » y lit ses cotes et son code coupon `coupon`). -> la note « joué à … » de l'ancienne version est-elle tombée ?
    Un combiné NOTÉ (fair.is_noted, sur mesure compris) n'est jamais modifié : rien n'est écrit, la note reste."""
    if fair.is_noted(row):
        return False
    legs, total, prob = combo
    sent["tiers"][str(row["tier"])] = {"keys": [c["key"] for c in legs], "total": round(total, 3),
                                        "legs": [fair.combo_leg(c) for c in legs]}
    had = bool(row.get("played"))
    fair.update_combo(row, combo, hhmm, coupon)
    store.save(fair.COMBOS_FILE, rows)
    store.save(COMBO_UPDATES, sent)
    return had and not row.get("played")


DROPPED_NOTE = " Ta note « joué à … » de l'ancienne version est retirée : note la nouvelle (✅ / ✏️) si tu la joues."


def day_usage(day, rows=None, row=None, sent=None):
    """Usage (CB.Usage) de TOUS les tickets du jour `day`, toutes familles confondues : combinés réels envoyés (bets.json), combinés à vérifier / aux cotes 1xBet
    et ceux de la fenêtre des stats (fair_combos.json : leur dernière version envoyée), pari du jour et paris « 1xBet paie plus que le marché » (tracked.json).
    Plafond PAR JAMBE (CB.reuse_cap) : 1 ticket sous C.LEG_SAFE_PROB, C.LEG_REUSE_MAX dès C.LEG_SAFE_PROB, C.LEG_REUSE_VERY_SAFE dès C.LEG_VERY_SAFE_PROB. `row` : le combiné à
    refaire, exclu (il ne se compte pas lui-même). Les combinés sur mesure (« combi N », « saison N », live) comptent dès qu'ils sont envoyés (custom.json) ; un combiné
    déclaré mort et les combinés fantômes non."""
    day = str(day)[:10]
    rows = fair.load_combos() if rows is None else rows
    sent = _load_updates(day) if sent is None else sent
    today = store.load("today.json", {})
    picks = today.get("picks") or [] if today.get("day") == day else []
    combos = [c for c in store.load_doc("bets.json", {"combos": []})["combos"] if str(c.get("date")) == day]
    usage = CB.Usage.from_sent(combos, picks)
    for r in rows:
        if r is not row and r.get("date") == day and not played.is_custom(r) and not r.get("dead"):
            usage.add(_base_legs(sent, r))
    for r in track.load():                          # paris seuls du jour : le pari du jour et les écarts sharp annoncés
        if r.get("date") == day and r.get("sent") and (r.get("single") is True or r.get("sharpgap")):
            usage.add([{"key": r.get("key") or r.get("id")}])
    for item in (custom._load(day).get("items") or {}).values():      # « combi N », « saison N » et combinés live ENVOYÉS (même non joués) : ils comptent comme les autres
        usage.add(item.get("legs") or [])
    return usage


def _others_usage(rows, row, day, sent):
    """Usage (day_usage) des AUTRES tickets du jour `day` : un remplaçant n'est repris dans `row` que si les règles de réutilisation le permettent (une jambe
    risquée une seule fois, toutes familles confondues)."""
    return day_usage(day, rows, row, sent)


def _ticket_problems(legs, pool, fresh_ids, now):
    """Paris du ticket NOTÉ `legs` (la version jouée) que 1xBet ne propose PLUS : [(jambe, None)]. Seuls les matchs relus (`fresh_ids`) sont jugés. Le pari
    EXACT est cherché dans le relevé du match (xbet.cached_cell sur sa référence c["xref"]) ; sans référence : plus aucune ligne de son marché dans
    `pool`. Jamais une question de cote : celle d'un ticket placé est acquise, et un pari que Kelly ne choisirait plus aujourd'hui (sa correction a
    changé, une autre ligne est préférée) n'est pas un problème du ticket (13/10 : « moins bien payé 1,91 ➜ 1,91 » après une mise à jour de Kelly)."""
    out = []
    for l in legs:
        if fresh_ids is not None and l["id"] not in fresh_ids:
            continue
        cell = xbet.cached_cell(l["xref"]) if l.get("xref") else None
        if cell is not None:
            if cell.cf is None:                     # le relevé du match ne propose plus CE pari
                out.append((l, None))
            continue                                # proposé (ou suspendu un moment) : rien à dire
        if not any(c["id"] == l["id"] and c["market"] == l["market"] and c.get("xodds") for c in pool):
            out.append((l, None))
    return out


def _ticket_lines(problems):
    """Une ligne par pari du ticket qui ne tient plus : « ❌ Plus chez 1xBet : … » ou « 📉 Moins bien payé : … ». HTML."""
    lines = []
    for l, price in problems:
        who = f"{fmt.esc(l.get('home', '?'))} – {fmt.esc(l.get('away', '?'))} ({fmt.esc(l.get('pick', ''))})"
        lines.append(f"❌ Plus chez 1xBet : {who}" if price is None else
                     f"📉 Moins bien payé : {who} : {fmt.odds_txt(l.get('xodds') or l.get('min_odds') or 0)} ➜ {fmt.b(fmt.odds_txt(price))}")
    return lines


TICKET_ADVICE = ("Ton combiné reste suivi tel que tu l'as noté. Déjà placé chez 1xBet : rien à faire, un pari déjà pris reste valable à ta cote (réglé "
                 "normalement, ou remboursé si le match est annulé). Pas encore placé : touche « ↩️ Annuler » puis « 🎟️ Vérifier ce combiné » pour une "
                 "version à jour.")


def _watch_ticket(sent, row, legs0, pool, now, refreshed, tier):
    """Combiné du matin NOTÉ (fair.is_ticket : le ticket est placé chez 1xBet) au relevé : jamais remplacé, jamais déclaré mort ni annulé. Kelly relit
    ses paris et PRÉVIENT une fois (data/combo_updates.json retient les paris déjà signalés) quand l'un n'est plus chez 1xBet ou paie moins. -> 0
    (rien n'est renvoyé)."""
    probs = _ticket_problems(legs0, pool, refreshed, now)
    keys = sorted(l["key"] for l, _ in probs)
    last = sent["tiers"].get(tier) or {"keys": row.get("keys") or [], "total": row.get("min_odds") or 1.0}
    if keys and keys != last.get("warned"):
        p = row["played"]
        telegram.send(telegram.topic(Html(fmt.esc(f"⚠️ Un pari de ton ticket n'est plus proposé chez 1xBet (combiné joué à {fmt.odds_txt(p['odds'])}) :")
                           + "\n" + "\n".join(_ticket_lines(probs)) + "\n\n" + fmt.i(fmt.esc(TICKET_ADVICE))), "alerts"))
        sent["tiers"][tier] = last | {"warned": keys}
    elif not keys and last.get("warned"):
        sent["tiers"][tier] = {k: v for k, v in last.items() if k != "warned"}          # tout est revenu à la normale : une future alerte sera redite
    return 0


def _of_day(tier):
    """« du matin » (combinés bâtis au passage de 04:00) ou « stats » (envoyé dans la journée par la fenêtre des stats, statswindow) : de quel combiné parlent les messages de mise à jour."""
    return "stats" if CB.window_no(tier) is not None else "saison en cours" if CB.season_no(tier) is not None else "du matin"


# ---------- commandes Telegram ----------

MAX_BILAN_DAYS = 3650                    # « /bilan N » : N jours au plus (10 ans)

HELP = Html("""<b>🤖 Kelly — mode d'emploi</b>

<b>Les boutons en bas</b>
🎯 <b>Combiné</b> : choisis la cote, les types de paris (victoire, buts, corners, tirs, fautes…) et le nombre de paris
🔝 <b>Du jour</b> : les meilleurs paris encore jouables ; dessous, 🔄 <b>Mettre à jour les cotes</b> (ou écris <i>maj</i>)
🧾 <b>Mes paris</b> : tes paris du jour, à noter quand tu les joues
   quand Kelly te demande ta mise, des boutons <b>500 · 1 000 · 2 000 · 5 000</b> la notent d'un appui
🔴 <b>Live</b> : un combiné sur les matchs en cours (30e à 88e minute), chances recalculées avec le score
📊 <b>Résultats</b> : gagnés, perdus, et le résultat en FCFA de tes paris notés
➕ <b>Plus</b> : les autres outils, en boutons sous le message :
   💼 <b>Mon capital</b> (bouton « Capital ») : ton capital, ta limite du jour, ce que tu as déjà misé et tes deux objectifs mesurés (CLV 1xBet, capital)
   🌱 <b>Saison</b> (ou écris <i>saison</i>, <i>saison 10</i>, <i>saison auto</i>) : la voie « saison en cours », qui juge les équipes sur les seuls matchs de cette saison ; son état (un bouton rouvre chaque combiné, « 🔄 Reconstruire » relance la construction automatique du jour : les combinés non joués sont remplacés, ceux que tu as notés joués sont gardés), ou un combiné à la cote voulue
   📈 <b>Tendances</b> : les championnats et les équipes qui obtiennent ou concèdent le plus de corners, cartons, tirs, fautes…
   🎯 <b>Combis</b> (écris <i>combis</i>, <i>combis 7</i>) : les résultats de tes combinés, jour par jour · 🌱 <i>saison résultats</i> : ceux de la voie saison, séparés
   📉 <b>Courbes</b> (ou écris <i>courbes</i>) : deux images, ton capital jour après jour et ton CLV 1xBet cumulé avec son intervalle
   🧮 <b>Incohérences</b> (écris <i>incohérences</i>, <i>incohérences 7</i>) : la voie « cotes » (fantôme), prix 1xBet incohérents entre eux, durée de vie, CLV
   🌅 <b>Matin</b> (écris <i>matin</i>) : relance le passage du matin s'il n'est pas fait ; <i>matin force</i> le refait (les combinés déjà enregistrés sont renvoyés tels quels, sans doublon)
   🗺️ <b>Carte</b> (écris <i>carte</i>, <i>carte stats</i>, <i>carte saison</i>) : où les voies stats et saison battent le dernier prix 1xBet, case par case (lecture seule)
   🧬 <b>Familles</b> (écris <i>familles</i>) : l'état de chaque famille de marché (hors ticket, fantôme, proposée, jouée), calibration et CLV ; <i>familles X on</i> joue une famille proposée
   🧮 <b>Marchés</b> : marché par marché (buts, corners, tirs…), si Kelly est fiable, si elle fait mieux que 1xBet, et lesquels sont écartés des combinés
   🩺 <b>Status</b> : l'état de Kelly (services, sources, quotas, apprentissage) · ❓ <b>Aide</b> · 🔄 <b>Actualiser les cotes</b>
Le bouton <b>/</b> à gauche de la zone de saisie liste aussi les commandes principales.
⚠️ Avant ce qui ne se défait pas d'un geste (annuler une note, changer ou arrêter ton capital, retirer ta perte max ou ton budget, régler un ticket à la main), Kelly te demande de <b>confirmer</b> : ✅ Oui ou ❌ Non.
🎟️ <b>Vérifier ce combiné</b> (sous un combiné du matin) : juste avant de jouer, Kelly revérifie ce combiné chez 1xBet, remplace les paris retirés et te donne un nouveau code
🔔 <b>Alertes</b> (envoyées toutes seules, en un message) : 💰 1xBet est en retard sur le marché, joue maintenant · ⚠️ un de tes paris n'est plus jouable · 📈 une cote 1xBet a monté depuis ce matin · ✅ tu as pris mieux que le marché

<b>Ton capital</b>
Écris une fois <i>capital 50000</i> (la somme que tu confies à Kelly), <i>perte max 5000</i> (le plus que tu acceptes de perdre en un jour) (plus d'objectif en FCFA : la carte affiche deux objectifs mesurés). Le capital suit les paris que tu notes. Chaque jour, Kelly répartit au plus ta limite : 3 % du capital (jamais plus que ta perte max), 1 % au plus par ticket, 100 FCFA au plus sur un ticket loterie (cote de 50 et plus, ou trop peu de chances). Elle s'arrête quand tu as misé ta limite du jour et n'augmente jamais une mise pour atteindre l'objectif. Sous chaque combiné : ses chances selon Kelly ET selon 1xBet.

<b>Ou un budget du jour</b>
Écris une fois <i>budget 5000</i> (ou <i>5 000</i>, <i>5.000</i>, <i>5k</i>) : c'est ce que tu es prêt à miser aujourd'hui. Chaque matin, Kelly le répartit entre ses paris selon sa confiance et affiche la <b>mise conseillée</b> de chacun, en FCFA. Les jours pauvres, elle en garde une partie. Change-le n'importe quel jour : il reste tel quel jusqu'à ce que tu le changes. Sans budget, pas de mise conseillée.

<b>Les paris que tu joues</b>
Seuls les paris que tu <b>notes</b> comptent dans tes résultats (tous les paris de Kelly restent suivis et réglés). Sous un pari, <b>✅ Je l'ai joué</b> (avec la mise conseillée) : quand la cote 1xBet du pari est connue (combiné ou pari du jour aux cotes 1xBet), un appui le note tout de suite à cette cote et à la mise conseillée (sans mise conseillée, Kelly ne demande que ta mise : <i>1000</i>) ; sinon réponds la cote obtenue chez 1xBet, par exemple <i>5,40</i>, ou <i>5,40 2 000</i> pour donner aussi ta mise en FCFA. Une fois noté : <b>✏️ Autre cote</b> corrige la cote, <b>↩️ Annuler</b> retire la note. Sous un combiné demandé avec <i>combi 20</i>, <b>✅ Je le joue</b> fait la même chose.

<b>Les codes coupon</b>
Touche un code pour le copier, ou le bouton <b>📋 Copier le code</b> sous un message qui n'en a qu'un, puis colle-le dans l'appli 1xBet (ticket, « Charger le coupon »).

<b>Tu peux aussi écrire</b>
• un nom d'équipe : <i>Flamengo</i>
• <i>combi auto</i> (Kelly choisit la cote, 10 au plus), <i>combi 20</i>, <i>combi 20 tirs fautes</i> (seulement ces types de paris), <i>combi 20 max 4</i> (4 paris au plus ; <i>pile 4</i> : exactement 4 ; <i>min 15</i> : 15 au moins, complétés si besoin par des paris probables mais mal payés, signalés), <i>bilan</i>, <i>bilan détail</i>, <i>mes paris</i>, <i>capital 50000</i>, <i>perte max 5000</i>, <i>objectif 5000</i>, <i>joué sûr 5,40 2000</i> (ou <i>2k</i>)
• <i>état</i> : où en est le passage du matin (pourcentage et heure de fin estimée), réponse immédiate même pendant qu'il tourne
• <i>status</i> : l'état de Kelly en un message (services, sources de stats, API et quotas), réponse immédiate
• <i>live 20</i> (ou le bouton 🔴 Live) : un combiné sur les matchs en cours (30e à 88e minute), chances recalculées avec le score et le temps restant
• <i>backup</i> (ou <i>sauvegarde</i>) : recevoir la base de données kelly.db tout de suite (elle arrive aussi toute seule, sans sonnerie, tous les 2 jours vers 12h00) ; téléphone perdu : sur le nouveau, <i>kelly restaurer</i> dans Termux
• <i>marchés détail</i> (ou <i>marchés détail tirs</i>, <i>marchés détail ldc</i>) : chaque championnat et coupe, marché par marché, en plus et en moins
• <i>erreurs</i> (ou <i>erreurs 48</i>) : résumé du journal du téléphone (erreurs, avertissements, plantages) des dernières 24 h, à copier-coller pour un contrôle
• <i>tendances</i> (ou le bouton 📈 Tendances ; <i>tendances cartons</i>, <i>tendances tirs premier</i>) : les championnats et les équipes qui obtiennent ou concèdent le plus de corners, cartons, tirs, fautes…
• <i>réglé vm3 gagné 12/10</i> : donner toi-même le résultat d'un ticket noté que Kelly ne trouve pas (petite ligue sans stats) : gagné, perdu ou remboursé

<i>Les anciennes commandes marchent toujours : /top, /combi, /bilan, /joue, /match, /analyse, /stats, /monde, /ligues, /etat, /status, /live, /bank, /backup.</i>""")


LEAGUES = {"E0": "Premier League", "SP1": "Liga", "I1": "Serie A", "D1": "Bundesliga", "F1": "Ligue 1",
           "EPL": "Premier League", "La_liga": "Liga", "Serie_A": "Serie A", "Ligue_1": "Ligue 1"}


XG_SOURCES = {"understat": "Understat", "fotmob-matchs": "FotMob, matchs récents", "api-football-matchs": "API-Football, matchs récents",
              "fotmob-classement": "FotMob, classement de saison"}


def _todays_usage(day, picks):
    """Réutilisation des matchs par les combinés déjà envoyés aujourd'hui (voir combos.Usage)."""
    combos = [c for c in store.load_doc("bets.json", {"combos": []})["combos"] if c.get("date") == str(day)]
    return day_usage(day), len(combos)


MIN_COMBI = 1.01                         # cote visée minimale de « /combi N »

SWEEP_FAMILIES = ("goals", "h2h", "dc", "btts", "team_goals", "corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides", "player")      # familles que « combi N » peut balayer


SEASON_REBUILD_FILE = "season_rebuild.json"     # {"day", "at"} : dernière reconstruction `saison auto` (écart minimal C.SEASON_REBUILD_GAP)


_OPEN_READ = {}                     # id du match -> instant de sa dernière relecture par « combi » (le bot tourne en continu : rythme de statswindow.retry_min)


LEGS_CHOICES = (3, 5, 8)           # « Nombre de paris : Libre · ≤ 3 · ≤ 5 · ≤ 8 » dans les cases à cocher


# ---------- diagnostic : python bot.py --check ----------

STATS_STATUS_URL = "https://v3.football.api-sports.io/status"


# Ré-exportations (découpage d'app.py) : les fonctions déplacées restent joignables par `app.nom` (tests, autres modules) ; le code déplacé lit app via A.nom
from .cmd_season import (  # noqa: E402,F401
    season_listing, season_open, _season_number, _committed,
    _season_split, season_rebuild, _days_arg, season_command)
from .cmd_custom import (  # noqa: E402,F401
    custom_combo, _open_stats, _not_open, _leg_time,
    _when_note, _bold_pools, underpaid, underpaid_lines,
    _reach, _chances, _bold_combo, _legs_failure,
    _min_legs_failure, _shared_counts, another_combo, _note,
    _kinds_split, kinds_picker, _tier_range, _budget_stake,
    _stake_words, _budget_stake_txt, _fair_reply, _live_season,
    _live_fair, xbet_why, _live_reply, _coupon)
from .run_refresh import (  # noqa: E402,F401
    run_xbet_refresh, manual_refresh, _priority_ids, _slot,
    _all_soon, _started, _base_legs, _load_updates,
    _update_combos, _upgrade_combo, _upgrade_single, refresh_combo,
    _real_changes, _refresh_picks, refresh_real, _check_ticket)
from .run_check import (  # noqa: E402,F401
    _check_odds, _check_stats, _check_telegram_bot, _telegram_hint,
    run_check)
from .run_track import (  # noqa: E402,F401
    run_track, mark_xclose, run_stats_window, fill_match_stats,
    run_snapshot, find_alerts, run_alert, _market_alerts)
from .analysis import (  # noqa: E402,F401
    _pair, _venues, _stat_block, _xg_team_line,
    fmt_team, comma, pm_note, referee_of,
    referee_line, deep_analysis, features_lines, event_matches,
    analyse, _pending, pending_lines, reread_match,
    _event_pronos, top_reply, upcoming, waiting_note)
from .commands import (  # noqa: E402,F401
    handle_command, _handle_command, auto_args, courbes_command,
    _courbes_send, sweep_line, _sweep_line, reuse_note,
    _with_sweep, combi_target, run_commands, _flush_outbox_locked,
    _waiting, send_backup)
