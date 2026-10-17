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
               cupstats, edgemap, oddsonly, resultats, seasonlane, xprice, xworld)
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
    got_sea = _step("journal de la voie saison", lambda: settle.settle_season(now))              # même règlement gratuit, journal et apprentissage à part
    if got_sea and (got_sea[0] or got_sea[1]):
        print(f"[saison] journal : {len(got_sea[0])} ligne(s) réglée(s), {got_sea[1]} apprise(s)", file=sys.stderr)
    cut = _step("coupure automatique de la voie stats", lambda: lanestats.apply(now.date()), [])      # après le règlement : juge les lignes réglées ; le message passe en PREMIER
    _step("carte des edges", lambda: edgemap.apply(now.date()))            # le lundi seulement, après la coupure : lecture seule, aucun pari ne change tant que EDGEMAP_GATE est éteint
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


def _market_alerts(found, closing, rows, now, fetch=None):
    """Alertes du marché (💰 1xBet en retard, ✅ bonne nouvelle, voir kelly/xalerts.py) pour les mouvements `found` (find_alerts) et les
    cotes de clôture `closing`. Sans le today.json du jour Kelly en cours : pas de référence 1xBet (ni 💰), jour Kelly calculé."""
    today = store.load("today.json", {})
    if not _current_day(today, now):
        today = {}
    day = today.get("day") or str(now.date() if now.hour >= C.DAY_END_HOUR else now.date() - timedelta(days=1))
    combos = store.load("bets.json", {"combos": []}).get("combos", [])
    return xalerts.on_market(found, closing, today, rows, combos, day, now, fetch)


def _load_updates(day):
    sent = store.load(COMBO_UPDATES, {})
    return sent if isinstance(sent, dict) and sent.get("day") == day and isinstance(sent.get("tiers"), dict) else {"day": day, "tiers": {}}


def _base_legs(sent, row):
    """Jambes de la DERNIÈRE version envoyée du combiné `row` (COMBO_UPDATES), sinon celles du matin : c'est d'elles que partent la
    vérification « un match a commencé » et la reconstruction."""
    return (sent["tiers"].get(str(row["tier"])) or {}).get("legs") or row.get("legs") or []


def _started(legs, now):
    return any((l.get("commence") or "9")[:16] <= now.isoformat()[:16] for l in legs)


def _all_soon(legs, now):
    """Tous les matchs de `legs` commencent dans moins de C.XBET_SOON_MIN min (1xBet les retire de l'avant-match)."""
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    return bool(legs) and all(l.get("commence") and datetime.fromisoformat(l["commence"].replace("Z", "+00:00")) <= soon for l in legs)


def _slot(c):
    """« id|marché » d'un pronostic (ou d'une clé : son début) : une ligne qui change dans la journée reste le même pari pour l'annonce."""
    return f"{c['id']}|{c['market']}" if isinstance(c, dict) else "|".join(str(c).split("|")[:2])


def _priority_ids(day, upcoming_):
    """Matchs à relire en premier : les jambes des combinés du jour (dernière version envoyée) et les paris 💎 montrés."""
    ids = {c["id"] for c in CB.best_per_match(upcoming_) if c.get("xodds")}
    sent = _load_updates(day)
    for row in fair.load_combos():
        if row.get("date") == day:
            ids |= {l["id"] for l in _base_legs(sent, row)}
    return ids


def run_xbet_refresh(now=None, fetch=None, announce=True, live=False, seconds=None):
    """Relevé 1xBet de la journée (15:00, kelly-xbet.timer, ou à la demande : manual_refresh) : les pronostics du matin
    (data/today.json) dont le match n'a pas commencé sont remis aux cotes 1xBet du moment ; /top et /combi les relisent. Puis :
    - `announce` : la liste des paris 💎 devenus jouables depuis la dernière liste (suivis comme ceux du matin) ;
    - les combinés 💎 du matin sont contrôlés (_update_combos) : renvoyés avec un nouveau code s'ils ont changé.
    Les matchs qui comptent sont relus en premier (jambes des combinés du jour, paris 💎 montrés). `seconds` : durée maximale du
    relevé (relevé manuel). `live` : cotes relues à l'instant (xbet.live_mode). `fetch` : injectable pour les tests. -> nombre de
    nouveaux 💎 annoncés."""
    from .sources import xbet
    now = now or now_utc()
    migrate.ensure()
    today = store.load("today.json", {})
    if not _current_day(today, now) or not today.get("pronos"):
        print(f"[1xbet] pas de pronostics du jour Kelly en cours (today.json : {today.get('day')}) : relevé sauté", file=sys.stderr)
        return 0
    telegram.flush_outbox()
    pronos = today["pronos"]
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    upcoming_ = [c for c in pronos if datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > soon]
    if "announced" not in today:                    # premier relevé du jour : les 💎 du matin sont déjà connus
        today["announced"] = [_slot(c) for c in CB.shown_pronos(upcoming_) if c.get("xodds")]
    got = set()
    first = _priority_ids(today["day"], upcoming_)
    limit = time.monotonic() + seconds if seconds else None

    window = statswindow.window_ids(upcoming_, now) if C.STATS_WINDOW_ENABLED else set()      # matchs à pronostics stats qui commencent dans la fenêtre des stats

    def run(events, deadline=None, wanted=None):
        events = sorted(events, key=lambda e: (e[1]["id"] not in first, e[1]["id"] not in window))     # tri stable : les matchs qui comptent d'abord, puis ceux dont les paris stats sont ouverts
        if limit:
            deadline = min(deadline, limit) if deadline else limit
        found = fetch(events, deadline=deadline, wanted=wanted) if fetch else xbet.fetch_day(events, deadline=deadline,
                                                                                               wanted=wanted, live=live)
        got.update(found)
        return found
    xprice.apply(upcoming_, fetch=run)
    for c in upcoming_:                             # chaque pronostic garde l'heure de SA lecture : un relevé partiel ne date pas les autres
        if c["id"] in got and c.get("xodds"):
            c["xat"] = now.strftime("%H:%M")
        elif c["id"] in got:
            c.pop("xat", None)
    new = []
    if announce:
        seen = {_slot(k) for k in today["announced"]}                        # par match et marché : une ligne changée n'est pas annoncée deux fois
        new = [c for c in CB.shown_pronos(upcoming_) if c.get("xodds") and _slot(c) not in seen][:12]      # 12 = la limite du message : une jambe non montrée n'est jamais marquée annoncée
        today["announced"] += [_slot(c) for c in new]
    if got:
        today["xbet_at"] = now.strftime("%H:%M")    # heure du dernier relevé 1xBet (partiel ou non) ; l'heure de chaque cote est dans c["xat"] (fmt.pronos)
    store.save("today.json", today)                 # mêmes objets : les pronostics à venir portent les nouvelles cotes
    log_opinions(upcoming_, today["day"], now)      # derniers avis de l'ensemble avant le match (journal d'apprentissage)
    if new:
        fair.record(new, today["day"], shown=new)
        telegram.send(telegram.topic(fmt.pronos(new, limit=12, title="💎 Nouvelles cotes 1xBet", when=now.strftime("%H:%M"),
                                 intro="Paris devenus jouables avec les cotes 1xBet du moment (marchés ouverts ou cotes montées "
                                       "depuis ce matin). Vérifie la cote avant de valider."), "combos"), silent=True)       # annonce : visible, sans sonnerie (niveaux de bruit)
    n_upd = _update_combos(today["day"], pronos, now, got)
    n_single = _upgrade_single(today, now, got, fetch)          # pari du jour sans cote 1xBet au matin : s'il y est maintenant, avec son code
    xalerts.LAST["gap"] = 0
    xalerts.on_refresh(today, now, got, upcoming_, fetch)       # ⚠️ mes paris en danger, 📈 mieux payé qu'au matin, 💰 1xBet paie plus que le marché (jamais d'erreur)
    LAST_REFRESH.update(matches=len(got), new=len(new), updated=n_upd, single=n_single, gap=xalerts.LAST["gap"])
    print(f"[1xbet] relevé : {len(upcoming_)} pronostic(s) à venir, {len(new)} nouveau(x) 💎 annoncé(s), "
          f"{n_upd} combiné(s) du matin renvoyé(s) à jour, pari du jour {'passé aux cotes 1xBet' if n_single else 'inchangé'}", file=sys.stderr)
    return len(new)


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


def manual_refresh(now=None, fetch=None):
    """« 🔄 Mettre à jour tous les paris » : le relevé 1xBet du jour, tout de suite et en direct (pronostics, 💎 nouveaux, combinés du
    matin à jour), au plus une fois toutes les C.XBET_MANUAL_GAP minutes (données du proxy) et pendant C.XBET_MANUAL_SECONDS secondes au
    plus (le bot ne répond à rien d'autre pendant ce temps) : les combinés du jour et les paris 💎 montrés d'abord. -> réponse."""
    now = now or now_utc()
    today = store.load("today.json", {})
    if not _current_day(today, now) or not today.get("pronos"):
        return waiting_note(today, now) or "Pas encore de pronostics du jour : rien à actualiser."
    last = today.get("xbet_manual")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < C.XBET_MANUAL_GAP * 60:
        return (f"Cotes déjà actualisées à {last[11:16]} : réessaie dans quelques minutes (chaque relevé consomme des données "
                "du proxy).")
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    if not any(datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > soon for c in today["pronos"]):
        return (f"Tous les matchs du jour ont commencé (ou vont commencer dans moins de {C.XBET_SOON_MIN} minutes) : rien à actualiser. "
                f"Rendez-vous demain à {C.DAY_END_HOUR:02d}h00.")
    telegram.send(f"⏳ J'actualise les cotes 1xBet des paris du jour (jusqu'à {math.ceil(C.XBET_MANUAL_SECONDS / 60)} minutes) : "
                  "tes combinés et les paris 💎 d'abord…", queue=False, silent=True)
    LAST_REFRESH.update(matches=0, new=0, updated=0, single=0, gap=0)
    run_xbet_refresh(now, fetch, announce=True, live=True, seconds=C.XBET_MANUAL_SECONDS)
    st = LAST_REFRESH
    if not st["matches"]:                           # rien n'a été lu : le délai entre deux relevés n'est pas armé (on peut réessayer tout de suite)
        if not xbet.proxy() and not C.XBET_DIRECT:
            return "1xBet ne répond pas : proxy 1xBet non configuré (ajoute XBET_PROXY dans ton fichier .env)."
        h = state.health("xbet")
        err = (h.get("last_error") if isinstance(h, dict) else None)
        if err:
            return f"1xBet ne répond pas en ce moment ({err}) : cotes non actualisées. Réessaie dans quelques minutes."
        return "1xBet ne répond pas en ce moment : cotes non actualisées. Réessaie dans quelques minutes."
    today = store.load("today.json", {})
    today["xbet_manual"] = now.isoformat(timespec="seconds")
    store.save("today.json", today)
    return (f"✅ Cotes 1xBet actualisées à {now:%H:%M} : {st['matches']} match(s) relu(s), {st['new']} nouveau(x) pari(s) 💎, "
            f"{st['updated']} combiné(s) du matin mis à jour." + (" Le pari du jour est maintenant aux cotes 1xBet." if st["single"] else "")
            + (f" {st['gap']} pari(s) 💰 « 1xBet paie plus que le marché » envoyé(s)." if st["gap"] else "")
            + ("" if st["new"] or st["updated"] or st["single"] or st["gap"] else " Rien de nouveau à jouer."))


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


def _update_combos(day, pronos, now, refreshed):
    """Combinés 💎 du matin (fair_combos.json, toutes les jambes aux cotes 1xBet) remis aux cotes du relevé : un combiné dont un pari
    a été retiré ou est devenu mal payé, ou dont la cote totale a bougé de plus de C.XBET_UPDATE_MOVE depuis sa dernière version
    envoyée, part à jour avec un nouveau code coupon (et ses boutons de mise). Le point de départ est la DERNIÈRE version envoyée, pas
    celle du matin ; un combiné dont un de ces matchs a commencé n'est plus touché (il est en jeu). Un combiné renvoyé prend la place
    du matin dans le suivi (règlement, bilan) ; un combiné déclaré injouable en sort (fair.kill_combo). Un combiné NOTÉ (le ticket est placé) n'est
    JAMAIS remplacé ni déclaré injouable : Kelly relit et prévient une fois (_watch_ticket). Les remplaçants respectent les règles de réutilisation
    des matchs face aux autres combinés du jour (_others_usage). Rien relu (`refreshed` vide) : rien à juger. -> nombre de combinés renvoyés."""
    if not refreshed:
        return 0
    sent, rows, n = _load_updates(day), fair.load_combos(), 0
    hhmm = now.strftime("%H:%M")
    for row in rows:
        if played.is_custom(row) or CB.season_no(row["tier"]) is not None:       # combiné sur mesure : ses paris ne sont jamais remplacés tout seuls ; voie `saison` : relue au passage du matin seulement
            continue
        rng = _tier_range(row["tier"])
        tier = str(row["tier"])
        last = sent["tiers"].get(tier) or {"keys": row.get("keys") or [], "total": row.get("min_odds") or 1.0}
        legs0 = _base_legs(sent, row)
        if row["date"] != day or not legs0 or not rng or _started(legs0, now):
            continue
        if fair.is_ticket(row):                     # noté joué : rapport seulement
            _watch_ticket(sent, row, legs0, pronos, now, refreshed, tier)
            continue
        usage = _others_usage(rows, row, day, sent)
        if not all(l.get("xodds") for l in legs0):          # combiné du matin bâti avant les cotes 1xBet : peut-il passer aux cotes 1xBet ?
            n += _upgrade_combo(sent, row, rows, legs0, rng, tier, pronos, now, refreshed, last, usage)
            continue
        lo, hi, prefix = rng
        res = CB.patch_combo(legs0, upcoming(pronos, now), lo, hi, fresh_ids=refreshed, prefix=prefix, usage=usage)
        if res is None:
            if not last.get("dead"):
                telegram.send(telegram.topic(f"⚠️ Le combiné {_of_day(tier)} (cote {fmt.trim(row.get('min_odds') or lo, 1)}) n'est plus jouable chez 1xBet "
                              "(paris retirés ou devenus mal payés) et Kelly ne trouve pas de remplaçants : ne le joue pas.", "alerts"))
                sent["tiers"][tier] = last | {"dead": True}
                fair.kill_combo(row)                # pas joué : hors du règlement, du bilan et de l'argent
                store.save(fair.COMBOS_FILE, rows)
                store.save(COMBO_UPDATES, sent)
            continue
        legs, total, prob, removed, added, changed = res
        keys = [c["key"] for c in legs]
        if (not last.get("dead") and set(keys) == set(last["keys"])
                and abs(total / (last.get("total") or total) - 1) <= C.XBET_UPDATE_MOVE):
            continue
        code = _coupon(legs)
        dropped = _record_version(sent, row, rows, (legs, total, prob), hhmm, code)
        head = (f"⚠️ Ton combiné a changé : un pari est retiré ou moins bien payé chez 1xBet. Le voici aux cotes de {hhmm} "
                if removed else f"🔄 Un combiné {_of_day(tier)} a changé chez 1xBet : le voici aux cotes de {hhmm} ")
        msg = _fair_reply((legs, total, prob), head + "(l'ancien code est remplacé)." + (DROPPED_NOTE if dropped else ""),
                          fmt.combo_changes(removed, added, changed), title=f"🔄 COMBINÉ À JOUR {hhmm}", stake_txt=_stake_txt(row),
                          when=hhmm, code=code, retry=RETRY_BUTTON)
        telegram.send(chat.with_played_button(msg, f"v{tier}", day, refresh=tier, lookup=True, code=code))
        n += 1
    store.save(COMBO_UPDATES, sent)
    return n


def _upgrade_combo(sent, row, rows, legs0, rng, tier, pronos, now, refreshed, last, usage=None):
    """Combiné du matin bâti AVANT que 1xBet soit lu (« 🎲 À vérifier », cotes mini, donc sans code coupon) : au relevé, si ses paris, relus,
    sont maintenant TOUS chez 1xBet à un prix jouable — ou peuvent être complétés dans la fourchette de son palier (CB.patch_combo) —, il
    est renvoyé aux cotes 1xBet, avec son code coupon, comme une mise à jour (version retenue pour « Mes paris », le règlement et le bilan).
    Jamais un combiné que tu as noté (« joué à … » : le ticket est placé), ni déclaré injouable, ni sur mesure. Pas de remplaçant trouvé, ou
    un match pas relu : rien n'est dit, il reste à vérifier. -> 1 s'il a été renvoyé, 0 sinon."""
    if row.get("played") or row.get("dead") or last.get("dead") or played.is_custom(row):
        return 0
    lo, hi, prefix = rng
    res = CB.patch_combo(legs0, upcoming(pronos, now), lo, hi, fresh_ids=refreshed, prefix=prefix, usage=usage)
    if res is None or not all(c.get("xodds") for c in res[0]):
        return 0
    legs, total, prob, removed, added, changed = res
    hhmm = now.strftime("%H:%M")
    code = _coupon(legs)
    _record_version(sent, row, rows, (legs, total, prob), hhmm, code)
    msg = _fair_reply((legs, total, prob), f"💎 Ton combiné du matin est maintenant aux cotes 1xBet de {hhmm}, avec son code coupon.",
                      fmt.combo_changes(removed, added, changed), title=CB.shots_title(row["tier"]), stake_txt=_stake_txt(row), when=hhmm,
                      code=code, retry=RETRY_BUTTON)
    telegram.send(chat.with_played_button(msg, f"v{tier}", row["date"], refresh=tier, lookup=True, code=code))
    return 1


def _upgrade_single(today, now, got, fetch=None):
    """Pari du jour chiffré au matin à la cote de référence des autres bookmakers (1xBet pas lu à ce moment-là : « pas 1xBet », pas de code
    coupon) : au relevé, Kelly essaie de le chiffrer chez 1xBet (xprice.live_picks : le même relevé en direct que « Vérifier ce combiné », cache
    de C.XBET_FRESH_MIN min repris, plafond de données et délai de xbet.fetch_day). Proposé par 1xBet et toujours avec une raison d'être
    (CB.leg_reason) : sa ligne prend la cote 1xBet, sa référence et l'heure (xodds, xref, xat, coupon) et UN message annonce « 🎟️ Le pari du
    jour est maintenant aux cotes 1xBet de HH:MM » avec son code coupon. Pas proposé par 1xBet : rien n'est dit (il garde « cote de
    référence, pas 1xBet »). Jamais s'il est noté, retiré, déjà aux cotes 1xBet, réglé ou si son match a commencé. Ne lève jamais.
    -> 1 s'il a été annoncé, 0 sinon."""
    if not got:                                     # 1xBet muet à ce relevé : pas de nouvelle tentative qui consommerait des données pour rien
        return 0
    try:
        day = today["day"]
        rows, combos = track.load(), store.load("bets.json", {"combos": []}).get("combos", [])
        row = next((r for r in played.single_rows(rows, combos) if r.get("date") == day and r.get("result") is None), None)
        pick = next((c for c in today.get("picks") or [] if row and c["key"] == row["key"]), None)
        if (not row or not pick or row.get("xodds") or pick.get("xodds") or row.get("played") or row.get("skipped")
                or _started([pick], now) or _all_soon([pick], now)):
            return 0
        res, hhmm = xprice.live_picks([pick], now, fetch, fresh_min=C.XBET_FRESH_MIN)
        _, state, new = res[0]
        if state != "ok" or not CB.leg_reason(new):
            return 0
        code = _coupon([new])
        row.update(xodds=new["xodds"], xat=hhmm, **({"xref": new["xref"]} if new.get("xref") else {}), **({"coupon": code} if code else {}))
        track.save(rows)
        today["picks"] = [new if c["key"] == row["key"] else c for c in today["picks"]]
        today.setdefault("picks_at", hhmm)
        store.save("today.json", today)
        body = fmt.single(new, CB.leg_reason(new), stake_txt=_stake_txt(row), coupon=code, when=hhmm, retry=RETRY_BUTTON)
        telegram.send(chat.with_played_button(Html(fmt.esc(f"🎟️ Le pari du jour est maintenant aux cotes 1xBet de {hhmm}.") + "\n\n" + body),
                                               "pari", day, xrefresh=True, lookup=True, code=code))
        return 1
    except Exception as e:                          # noqa: BLE001 - une tentative en échec ne bloque jamais le relevé
        print(f"[1xbet] pari du jour aux cotes 1xBet ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def run_track(events_for=None, now=None, xfetch=None):
    """Passage horaire : cote de clôture des paris ENVOYÉS dont le match démarre bientôt (bulk par ligue
    quand c'est moins cher, sinon un appel par match, seulement les marchés suivis) + alertes 1xBet (xalerts.on_market : 💰 1xBet en
    retard sur un mouvement du marché, ✅ bonne nouvelle ; 1xBet relu en direct pour 3 matchs au plus).
    Chaque appel payant passe par le budget « closing » ; refusé = sauté (log), jamais d'erreur.
    `events_for(sport_key, ev_id, markets)` : injectable pour les tests (un appel par match, sans budget), `xfetch` : relevé 1xBet des alertes."""
    now = now or now_utc()
    migrate.ensure()
    telegram.flush_outbox()                         # messages restés en attente (réseau coupé) : renvoyés avant les alertes du jour
    rows = track.load()
    due, missed = track.pending_closing(rows, now)
    track.mark_missed(rows, missed, now)
    combos = store.load_doc("bets.json", {"combos": []})["combos"]
    calls = plan_closing(due, combos, bulk=events_for is None)
    if (calls and events_for is None and odds.CREDITS["remaining"] is None
            and store.load(budget.FILE, {}).get("remaining") is None):         # jamais connus : sinon l'état du budget suffit
        try:
            odds.get("/sports")                     # gratuit : fait connaître les crédits restants
        except Exception as e:                      # noqa: BLE001
            print(f"[odds] /sports : {e}", file=sys.stderr)
    closing, skipped = [], 0
    for c in calls:
        sk, ids = c["sport_key"], c["ids"]
        try:
            if events_for:
                evs = [events_for(sk, ids[0], c["markets"])]
            else:
                why = budget.why_not(c["cost"], "closing")
                if why:
                    skipped += 1
                    print(f"[budget] clôture de {', '.join(ids)} sautée ({c['cost']} crédit(s) : {why}).", file=sys.stderr)
                    continue
                params = dict(use="closing", regions="eu", oddsFormat="decimal", markets=",".join(sorted(c["markets"])))
                evs = (odds.get(f"/sports/{sk}/odds", eventIds=",".join(ids), **params) if c["kind"] == "bulk"
                       else [odds.get(f"/sports/{sk}/events/{ids[0]}/odds", **params)])
            for ev in evs or []:
                if not ev:
                    continue
                ev["sport_key"] = sk
                ev.setdefault("read_at", now.isoformat(timespec="seconds"))      # vraie heure de lecture : fraîcheur de la proba sharp (sharpgap)
                day = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).date()
                closing += odds.build_picks([ev], day)
        except Exception as e:                      # noqa: BLE001 - un match en erreur ne bloque pas les autres
            print(f"clôture {', '.join(ids)} : {e}", file=sys.stderr)
    n = track.close(rows, closing, now)
    track.save(rows)
    hist = store.load("odds_history.json", {})
    alerts = find_alerts(closing, hist)
    hist.update(odds.snapshot(closing))
    store.save("odds_history.json", hist)
    print(f"{n} clôtures relevées, {len(missed)} manquées, {skipped} sautées (budget), {len(alerts)} alertes.")
    if calls and events_for is None:
        print(f"[budget] {budget.line()}", file=sys.stderr)
    _market_alerts(alerts, closing, rows, now, xfetch)
    mark_xclose(now)                                # audit : dernier prix 1xBet (cache disque) des lignes du journal dont le match approche
    cache.flush()
    return n, alerts


def mark_xclose(now=None):
    """Note, sans AUCUNE requête, le dernier prix 1xBet connu (cache disque) des lignes du journal d'ensemble dont le match approche (`xclose`, voir
    ensemble.mark_x_close) : la clôture 1xBet des marchés de stats, qui n'ont pas de prix sharp. Appelé après run_track et après run_stats_window (qui
    relit 1xBet) ; sous le verrou des données comme eux. Ne lève jamais. -> nombre de lignes modifiées."""
    try:
        n = ensemble.mark_x_close(now)
        n += ensemble.mark_x_close(now, name=cupstats.LOG)
        return n + ensemble.mark_x_close(now, name=seasonlane.LOG)             # le journal de la voie `saison` : même lecture du cache, aucune requête
    except Exception as e:                          # noqa: BLE001 - audit seulement
        print(f"[suivi] prix 1xBet de clôture ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def run_stats_window(now=None, fetch=None, seconds=None):
    """Fenêtre des stats du passage horaire (`bot.py --track`, après run_track) : les pronostics tirs / fautes / touches / hors-jeu des matchs qui commencent dans
    les C.STATS_WINDOW_MAX_H prochaines heures sont relus chez 1xBet quand 1xBet les ouvre, remis à sa cote, et un « 🧮 COMBINÉ STATS » (+ les nouveaux 💎 stats) part si la matière
    le permet. Voir kelly/statswindow.py (verrou des données, plafonds de données). Ne lève jamais. -> bilan du passage."""
    return statswindow.run(now, fetch, seconds)


def fill_match_stats():
    """Passage horaire : quelques pages de match FotMob (touches, hors-jeu, fautes, tirs...) pour les profils d'équipe. Jamais au passage du matin :
    la page des ligues, lue le matin, est relue depuis le cache. Ne casse jamais le passage."""
    rep = None
    try:
        rep = fmmatch.fill()
        print("[fmmatch] " + fmmatch.report_line(rep), file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - facultatif
        print(f"[fmmatch] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    from .sources import s365
    s365.fill_safe()                                # stats de match 365scores (ligues sans FotMob détaillé) ; ne lève jamais
    from . import livedata
    livedata.safe_prepare()                         # équipes inconnues des matchs des prochaines heures, pour le bouton 🔴 Live ; ne lève jamais
    return rep


def run_snapshot():
    """Le soir : relevé des cotes du lendemain (pour voir les mouvements le matin).
    Désactivé sur forfait gratuit (BUDGET_SHARES sans « snapshot » = 0 crédit)."""
    day = now_utc().date() + timedelta(days=1)
    picks = odds.build_picks(odds.fetch_events(day, optional=True, use="snapshot"), day)
    hist = store.load("odds_history.json", {})
    hist.update(odds.snapshot(picks))
    store.save("odds_history.json", hist)
    print(f"{len(picks)} cotes relevées pour {day}.")


def find_alerts(picks, history):
    alerts = []
    for c in picks:
        prev = history.get(c["key"])
        if not isinstance(prev, dict):
            continue
        drop = (prev["o"] - c["odds"]) / prev["o"]
        # proba brute des bookmakers des deux côtés (« pb ») : l'ancienne « p » mêlait le modèle de Kelly et créait de fausses hausses
        if prev.get("pb") is None:
            rise = 0.0
        else:
            now_p = c.get("prob_book", c["prob"])
            rise = (now_p - prev["pb"]) / prev["pb"] if prev["pb"] else 0.0
        if drop >= C.ALERT_ODDS_DROP or rise >= C.ALERT_PROB_RISE:
            alerts.append((max(drop, rise), c, prev))
    alerts.sort(key=lambda a: -a[0])
    return alerts[:8]


def run_alert(sample=False):
    """Balayage large : cotes qui chutent = argent des pros. Désactivé sur forfait gratuit (BUDGET_SHARES sans
    « alert » = 0 crédit) : les alertes ne viennent plus que de run_track, sur les matchs relevés. Mêmes alertes 1xBet que run_track
    (xalerts.on_market) ; -> le message envoyé (liste vide sinon)."""
    now = now_utc()
    hist = store.load("odds_history.json", {})
    if sample:
        events = load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        picks = odds.build_picks(events, day)
    else:
        day = now.date()
        picks = odds.build_picks(odds.fetch_events(day, props=bool(os.environ.get("ALERT_PROPS")), optional=True,
                                                   use="alert", after=now), day, now)
    found = find_alerts(picks, hist)
    if not sample:
        hist.update(odds.snapshot(picks))
        store.save("odds_history.json", hist)
    if not found:
        print("Aucun mouvement notable.")
        return []
    msg = _market_alerts(found, picks, track.load(), now)
    return [msg] if msg else []


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
   🗺️ <b>Carte</b> (écris <i>carte</i>, <i>carte stats</i>, <i>carte saison</i>) : où les voies stats et saison battent le dernier prix 1xBet, case par case (lecture seule)
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


def _pair(v, k, label, fmt="{:.1f}"):
    f, a = v.get(k + "_for"), v.get(k + "_against")
    return f"{label} {fmt.format(f)} pour / {fmt.format(a)} contre" if f is not None and a is not None else None


def _venues(src):
    """Lignes « À domicile / À l'extérieur » d'un profil (football-data ou API-Football)."""
    out = []
    for venue, label in (("home", "À domicile"), ("away", "À l'extérieur")):
        v = src.get(venue) or {}
        bits = [x for x in (_pair(v, "corners", "corners", "{:.1f}"), _pair(v, "cards", "cartons")) if x]
        if v.get("n") and bits:
            out.append(f"{label} ({v['n']} matchs) : " + " ; ".join(b.replace(" pour / ", "/").replace(" contre", "") for b in bits))
    return out


def _stat_block(allv, skip=()):
    return [x for k, x in (("goals", _pair(allv, "goals", "• Buts :")), ("shots", _pair(allv, "shots", "• Tirs :")),
                           ("sot", _pair(allv, "sot", "• Tirs cadrés :")), ("corners", _pair(allv, "corners", "• Corners :")),
                           ("cards", _pair(allv, "cards", "• Cartons (jaunes + rouges) :")),
                           ("fouls", _pair(allv, "fouls", "• Fautes :")), ("throws", _pair(allv, "throws", "• Touches :")),
                           ("offsides", _pair(allv, "offsides", "• Hors-jeu :")))
            if x and k not in skip]


def _xg_team_line(p):
    """Ligne « buts attendus » d'une équipe d'après les xG par match de ses profils (FotMob, puis API-Football : même priorité que
    features.match_features), avec sa source ; None sous XG_MIN_MATCHES matchs portant un xG. Sans le mot « xG »."""
    from .sources import features
    for src, label in ((p.get("fm"), "FotMob"), (p.get("aps"), "API-Football")):
        v = features._xg_view(src or {}, "all")
        if v:
            txt = (f"Buts attendus d'après les occasions ({label}, {v['xg_n']} derniers matchs) : "
                   f"{v['xg_for']:.2f} pour / {v['xg_against']:.2f} contre")
            for venue, name in (("home", "domicile"), ("away", "extérieur")):
                w = (src or {}).get(venue) or {}
                if w.get("xg_n", 0) >= C.XG_MIN_MATCHES and w.get("xg_for") is not None and w.get("xg_against") is not None:
                    txt += f" ; {name} {w['xg_for']:.2f}/{w['xg_against']:.2f}"
            return txt
    return None


def fmt_team(p, query):
    """Texte de /stats : profil d'une équipe (derniers matchs : football-data, Understat, et API-Football pour les championnats
    que football-data ne détaille pas)."""
    if not query:
        return "Exemple : /stats Arsenal"
    if not p:
        return (f"Aucune stat pour « {query} » (équipe inconnue ou sources pas encore actualisées : "
                f"elles le sont chaque matin à {C.DAY_END_HOUR:02d}h00).")
    if "many" in p:
        return f"Plusieurs équipes correspondent à « {query} » : {', '.join(p['many'])}. Précise le nom."
    league = LEAGUES.get(p["div"], p["div"]) if p.get("div") else None
    lines = [f"📈 {p['team']}" + (f" ({league})" if league else "")]
    fd = p.get("fd") or {}
    allv = fd.get("all") or {}
    shown = set()
    if allv.get("n"):
        lines += ["", f"📋 {allv['n']} derniers matchs (football-data), pour / contre :"]
        lines += _stat_block(allv)
        lines += _venues(fd)
        shown |= {k for k in ("corners", "cards", "shots", "sot", "fouls", "goals") if allv.get(k + "_for") is not None}
    # FotMob (stats de match) puis API-Football complètent sans répéter ce qui figure déjà
    aps = p.get("aps") or {}
    for src, label in ((p.get("fm") or {}, "FotMob"), (aps, "API-Football" + (f", {aps['lg']}" if aps.get("lg") else ""))):
        av = src.get("all") or {}
        block = _stat_block(av, shown) if av.get("n") else []
        if block:
            lines += ["", f"📋 {av['n']} derniers matchs ({label}), pour / contre :"] + block
            shown |= {k for k in ("corners", "cards", "shots", "sot", "fouls", "goals", "throws", "offsides")
                      if av.get(k + "_for") is not None}
    us = p.get("us") or {}
    if (us.get("all") or {}).get("xg") is not None:
        u = us["all"]
        txt = f"xG (Understat, {u['n']} derniers) : {u['xg']:.2f} pour / {u.get('xga', 0):.2f} contre"
        for venue, label in (("home", "domicile"), ("away", "extérieur")):
            v = us.get(venue) or {}
            if v.get("xg") is not None and v.get("xga") is not None:
                txt += f" ; {label} {v['xg']:.2f}/{v['xga']:.2f}"
        lines += ["", txt]
    else:                                                       # pas d'Understat : buts attendus par match des profils FotMob / API-Football
        xl = _xg_team_line(p)
        if xl:
            lines += ["", xl]
    if len(lines) == 1:
        lines.append("Pas assez de matchs récents dans les sources.")
    if p.get("updated"):
        lines += ["", f"(sources actualisées le {p['updated'][8:10]}/{p['updated'][5:7]})"]
    return "\n".join(lines)


def comma(text):
    """Décimales à la française : 2.5 -> 2,5 (hors identifiants et dates)."""
    return re.sub(r"(?<=\d)\.(?=\d)", ",", str(text))


def pm_note(c):
    pm = c.get("pm") or {}
    if pm.get("exp") is None or c.get("point") is None:
        return ""
    return f" (Kelly prévoit {fmt.num(pm['exp'], 1)} ; ligne à {fmt.trim(c['point'])})"


def referee_of(c):
    """Arbitre désigné du match du pronostic ou pari `c` (liste du jour d'API-Football déjà en cache : aucune requête) ; None s'il n'est pas (encore) connu."""
    if not (c.get("sport_key") or "").startswith("soccer") or not c.get("commence"):
        return None
    try:
        return stats.referee_for({"home_team": c["home"], "away_team": c["away"], "commence_time": c["commence"]})
    except Exception:                               # noqa: BLE001 - l'arbitre est facultatif
        return None


def referee_line(ref, *texts):
    """« 👨‍⚖️ Arbitre désigné : M. Oliver » quand l'arbitre est connu et qu'aucun des textes déjà affichés (analyse du modèle, chiffres d'avant-match) n'en parle ; '' sinon."""
    if not ref or any("Arbitre" in str(t or "") for t in texts):
        return ""
    return f"👨‍⚖️ Arbitre désigné : {fmt.esc(ref)}"


def deep_analysis(first, referee=None):
    """Analyse approfondie (modèle de force, contexte, grille de marchés) ou None si indisponible. `referee` : arbitre désigné (referee_of), dont l'effet sur les cartons est chiffré."""
    if not first.get("sport_key", "").startswith("soccer"):
        return None
    try:
        txt = strength.analysis_text(first["home"], first["away"], first["sport_key"], referee=referee)
    except Exception as e:                       # noqa: BLE001 - l'analyse ne doit jamais casser la commande
        print(f"analyse approfondie : {e}", file=sys.stderr)
        return None
    if not txt or "Pas assez de données" in txt[:300]:
        return None
    lines = txt.split("\n")
    return "\n".join(lines[1:] if lines[0].startswith("🔬") else lines)   # l'en-tête est déjà affiché


XG_SOURCES = {"understat": "Understat", "fotmob-matchs": "FotMob, matchs récents", "api-football-matchs": "API-Football, matchs récents",
              "fotmob-classement": "FotMob, classement de saison"}


def features_lines(f, only=None, head="Chiffres d'avant-match"):
    """Lignes « corners, cartons, tirs, buts attendus » d'après sources.match_features ; `only` : clés exp_* à montrer."""
    n = lambda k: f.get(k)                                  # noqa: E731
    ok = lambda k: only is None or k in only                # noqa: E731

    def two(k, label, unit=""):
        t, h, a = n("exp_" + k), n(f"exp_{k}_home"), n(f"exp_{k}_away")
        if (t is None and h is None) or not (ok("exp_" + k) or ok(f"exp_{k}_home")):
            return None
        split = f"  {fmt.i(f'({h:.1f} + {a:.1f})')}" if h is not None and a is not None else ""
        return f"• {label} : {fmt.b(f'{t if t is not None else h + a:.1f}{unit}')}{split}"
    lines = ["", fmt.b(f"📐 {head}"), fmt.i(f"{fmt.esc(', '.join(f['sources']))} · {f.get('n_home', '?')} et "
                                          f"{f.get('n_away', '?')} derniers matchs"), ""]
    lines += [x for x in (two("goals", "Buts"), two("shots", "Tirs"), two("sot", "Tirs cadrés"), two("corners", "Corners"),
                          two("cards", "Cartons"), two("fouls", "Fautes"), two("throws", "Touches"),
                          two("offsides", "Hors-jeu")) if x]
    if only is None:
        if f.get("exp_xg_total") is not None:
            xg = f"{f['exp_xg_total']:.1f}"
            src = XG_SOURCES.get(f.get("xg_src"))
            lines.append(f"• Buts attendus d'après les occasions (xG) : {fmt.b(xg)}" + (f" {fmt.i('(' + src + ')')}" if src else ""))
        if f.get("ref_cards") is not None:
            lines.append(f"• Arbitre {fmt.esc(f['referee'])} : {f['ref_cards']:.1f} cartons par match")
    return lines if len(lines) > 4 else []


def event_matches(items, query):
    """Paris dont le match contient `query` : sans casse ni accents (« Atletico » trouve « Atlético Madrid »), puis, faute de mieux,
    par ressemblance avec le nom d'une équipe (comme la détection des équipes du texte libre, chat._team_command)."""
    q = played.norm(query)
    if not q:
        return []
    found = [c for c in items if q in played.norm(c["event"])]
    if found:
        return found
    from .sources import names
    return [c for c in items if any(t and names.team_sim(query, t) >= 0.8 for t in (c.get("home"), c.get("away")))]


def analyse(picks, query, pronos=(), when=None, picks_when=None):
    """Texte de /analyse : analyse approfondie d'un match du jour + ses meilleurs paris avec leur raison. `when` / `picks_when` : heures
    « HH:MM » des derniers relevés 1xBet des pronostics et des paris réels (affichées avec leurs cotes)."""
    if not query:
        return "Exemple : /analyse Arsenal"
    found = event_matches(picks, query)
    if not found:                               # match sans cotes réelles : analyse seule (+ pronostics)
        free = event_matches(pronos, query)
        events = list(dict.fromkeys(c["event"] for c in free))
        if not free:
            return f"Aucun match du jour ne contient « {query} »."
        if len(events) > 1:
            return "Plusieurs matchs correspondent, précise : " + " ; ".join(events[:8])
        ref = referee_of(free[0])
        deep = deep_analysis(free[0], ref)
        head = f"🔬 {fmt.b(fmt.esc(free[0]['event']))}\n{fmt.i(fmt.hhmm(free[0]['start']) + ' · ' + fmt.esc(free[0]['league']))}"
        fl = [] if deep else features_lines(sources.match_features(free[0]["home"], free[0]["away"], free[0]["sport_key"], **({"referee": ref} if ref else {})))
        body = ("\n" + fmt.esc(deep)) if deep else ("\n".join(fl) if fl else "\nPas assez de données pour une analyse approfondie de ce match.")
        rl = referee_line(ref, deep, "\n".join(fl))
        if rl:
            body += "\n" + rl
        shown, wait = CB.shown_pronos(free), "\n\n".join(x for x in (pending_lines(free), underpaid_lines(free)) if x)
        return Html(comma(head + "\n" + body) + "\n\n" + ("\n\n".join(([str(fmt.pronos(shown, 6, when=when))] if shown or not wait else []) + [wait])
                                                         if wait else str(fmt.pronos(shown, 6, when=when))))
    events = list(dict.fromkeys(c["event"] for c in found))
    if len(events) > 1:
        return "Plusieurs matchs correspondent, précise : " + " ; ".join(events[:8])
    first = found[0]
    lines = [f"🔬 {fmt.b(fmt.event_name(first))}", fmt.i(f"{fmt.hhmm(first['start'])} · {fmt.esc(first['league'])}")]
    ref = referee_of(first)
    deep = deep_analysis(first, ref)
    f = {"sources": []} if first["group"] != "soccer" else sources.match_features(first["home"], first["away"], first["sport_key"], **({"referee": ref} if ref else {}))
    shown_ = []
    if deep:
        lines += ["", fmt.esc(deep)]
        shown_.append(deep)
        if f.get("aps_keys"):                                   # le modèle approfondi n'a que les buts : API-Football complète
            lines += features_lines(f, only=set(f["aps_keys"]), head="Chiffres complémentaires")
    elif f["sources"]:
        lines += features_lines(f)
        shown_.append("\n".join(lines))
    else:
        lines.append("Pas de stats réelles pour ce match (équipes inconnues des sources ou sources indisponibles).")
    rl = referee_line(ref, *shown_)
    if rl:
        lines.append(rl)
    real = any(c.get("xodds") for c in picks)               # 1xBet a chiffré des paris du jour : les autres ne sont pas jouables
    best = sorted(found, key=lambda c: (bool(c.get("xodds")), c["score"]), reverse=True)[:6] if real else \
        sorted(found, key=lambda c: c["score"], reverse=True)[:6]
    lines += ["", fmt.b("🎯 Meilleurs paris")]
    for c in best:
        why = "" if real and not c.get("xodds") else fmt.why_line(c, CB.leg_reason(c))      # pas de « cote plus haute… » d'un autre book
        lines.append(f"\n👉 {fmt.esc(c['pick'])} — {fmt.price_txt(c, real, picks_when)} ({fmt.pct(c['prob'])} de chances)"
                     f"{'' if why else pm_note(c)}"
                     + (f"\n💡 {fmt.i(fmt.esc(why))}" if why else ""))
    shown = _event_pronos(pronos, found)
    if shown:
        lines += ["", str(fmt.pronos(shown, 6, when=when))]
    mine_ = [c for c in pronos if c["id"] in {x["id"] for x in found}]
    wait = "\n\n".join(x for x in (pending_lines(mine_), underpaid_lines(mine_)) if x)
    if wait:
        lines += ["", wait]
    return Html(comma("\n".join(lines)))


def _pending(rows, now=None):
    """Pronostics stats (tirs, tirs cadrés, fautes, touches, hors-jeu) SOLIDES que 1xBet n'a pas encore chiffrés : il ouvre ces paris quelques heures
    avant le match, le relevé du matin les a vus fermés (c["xnone"] : « absent », qui veut dire ici « pas encore ouvert »). Mêmes règles que les
    pronostics montrés (combos.viable, xnone mis à part, et combos.fair_eligible) ; matchs qui ne sont pas sur le point de commencer. Le plus probable
    par marché, les plus probables d'abord."""
    now = now or now_utc()
    limit = now + timedelta(minutes=C.STATS_WINDOW_MIN_MIN - 15)          # 1xBet ferme ces paris ~30 min avant : plus la peine d'attendre
    best = {}
    for c in rows:
        if not statswindow.is_stat(c) or c.get("xodds") or not CB.fair_eligible(c):
            continue
        if not CB.viable({k: v for k, v in c.items() if k != "xnone"}):
            continue
        try:
            if datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) <= limit:
                continue
        except (KeyError, ValueError):
            continue
        k = (c["id"], c["market"])
        if k not in best or c["prob"] > best[k]["prob"]:
            best[k] = c
    return sorted(best.values(), key=lambda c: -c["prob"])


def pending_lines(rows, now=None, limit=5):
    """Bloc « ⏳ Pas encore ouverts chez 1xBet » de /analyse et /match : les pronostics stats solides d'un match en attente de leur cote
    (_pending), avec la cote minimale à exiger et l'heure à partir de laquelle Kelly les relit (fenêtre des stats) ; '' s'il n'y en a pas."""
    now = now or now_utc()
    wait = _pending(rows, now)[:limit]
    if not wait:
        return ""
    lines = [fmt.b("⏳ Pas encore ouverts chez 1xBet"),
             fmt.i(f"Kelly les surveille (toutes les {C.STATS_WINDOW_FAR_RETRY_MIN // 60} h, puis chaque heure dans les "
                   f"{fmt.trim(C.STATS_WINDOW_NEAR_H, 1)} dernières heures) et te prévient.")]
    lines += [f"• {fmt.esc(c['pick'])} · Kelly {fmt.pct(c['prob'])} · joue dès {fmt.odds_txt(c['min_odds'])}" for c in wait]
    return "\n".join(lines)


def reread_match(pronos, query, now=None):
    """/analyse et /match : si le match demandé est dans la fenêtre des stats et qu'un de ses pronostics stats solides attend sa cote (_pending), ses
    paris sont relus EN DIRECT chez 1xBet (xprice.live_check, 2 matchs au plus) : un pari qui vient d'ouvrir est montré à sa cote. -> les pronostics
    du jour (relus ou non). Ne lève jamais."""
    now = now or now_utc()
    if not query or not C.XBET_ENABLED:
        return pronos
    ids = []
    for c in _pending(event_matches(pronos, query), now):
        if statswindow.in_window(c, now) and c["id"] not in ids:
            ids.append(c["id"])
    if not ids or len(ids) > 2:
        return pronos
    try:
        if not statswindow.month_ok():
            return pronos
        telegram.send("⏳ Je regarde chez 1xBet si les paris tirs / fautes de ce match viennent d'ouvrir…", queue=False, silent=True)
        return xprice.live_check(set(ids), now, fresh_min=C.STATS_WINDOW_FRESH_MIN)[0]
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même, avec les cotes déjà connues
        print(f"[1xbet] relecture du match impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return pronos


def _event_pronos(pronos, found):
    """Pronostics 💎 (cote 1xBet relevée) des matchs de `found` : de quoi montrer leurs prix 1xBet à côté des paris réels."""
    ids = {c["id"] for c in found}
    return CB.shown_pronos([c for c in pronos if c["id"] in ids and c.get("xodds")])


def top_reply(live, live_pronos, when=None, picks_when=None):
    """/top : les meilleurs paris réels encore jouables (cotes 1xBet de `picks_when` quand 1xBet les a chiffrés ; sinon cotes de référence
    des autres bookmakers, dites comme telles) ET les pronostics 💎 aux cotes 1xBet (`when`) des autres matchs. Un pari absent de
    1xBet n'est pas montré comme jouable (CB.make_pool)."""
    pool = CB.make_pool(live) if live else []
    keep = {c["key"] for c in CB.per_event(pool, k=1)} | {c["key"] for c in pool if CB.value_leg(c)}      # le meilleur pari de chaque match + TOUTES les jambes à valeur
    best = sorted((c for c in pool if c["key"] in keep), key=lambda c: c["score"], reverse=True)[:10]
    shown = CB.shown_pronos(live_pronos)
    if best:                                        # avec des paris réels : seulement les 💎 d'un autre marché ou d'un autre match (jamais deux fois le même pari)
        slots = {_slot(c) for c in best}
        shown = [c for c in shown if c.get("xodds") and _slot(c) not in slots]
    parts = []
    if best:
        priced = all(c.get("xodds") for c in best)
        note = (fmt.i(f"🕒 Cotes 1xBet de {fmt.esc(picks_when)} : vérifie-les avant de valider.") if priced and picks_when
                else fmt.i("🕒 Cotes 1xBet : vérifie-les avant de valider.") if priced
                else fmt.i("⚠️ Cotes de référence d'autres bookmakers, pas celles de 1xBet : vérifie chaque cote chez 1xBet."))
        parts.append(Html(fmt.b("🔝 Les meilleurs paris du jour") + "\n" + note + "\n\n" + "\n".join(
            fmt.leg(k, c, CB.leg_reason(c), price=fmt.price_txt(c)) for k, c in enumerate(best, 1))))
    if shown:
        parts.append(fmt.pronos(shown, 10 if not best else 6, when=when))
    if not parts:
        return ("Aucun pari de qualité aujourd'hui : rien qui ait un avantage clair, une vraie raison de statistiques "
                "ou une forte probabilité. /match <équipe> montre tout ce qui existe pour un match.")
    out = parts[0] if len(parts) == 1 else Html("\n\n".join(parts))
    return telegram.with_buttons(out, telegram.inline([[("🔄 Mettre à jour les cotes", "maj")]]))       # l'ancien bouton du menu, là où il sert


def upcoming(items, now=None):
    """Paris dont le match n'a pas encore commencé (on ne peut plus jouer les autres)."""
    t = (now or now_utc()).isoformat()[:16]
    return [c for c in items if (c.get("commence") or "9")[:16] > t]


def waiting_note(today, now=None):
    """Entre minuit et le passage du matin, l'analyse en mémoire est celle de la veille (matchs commencés ou finis) : le dire, avec
    l'heure de la prochaine analyse, au lieu de « rien aujourd'hui ». '' si l'analyse est celle du jour."""
    now = now or now_utc()
    if not today.get("day") or today["day"] >= str(now.date()):
        return ""
    return (f"⏳ Les matchs d'aujourd'hui ({fmt.fr_day(now.date())}) seront analysés au passage de "
            f"{C.DAY_END_HOUR:02d}h00 (heure d'Abidjan). Pour l'instant Kelly n'a que l'analyse d'hier, dont les matchs ont commencé.")


def _todays_usage(day, picks):
    """Réutilisation des matchs par les combinés déjà envoyés aujourd'hui (voir combos.Usage)."""
    combos = [c for c in store.load_doc("bets.json", {"combos": []})["combos"] if c.get("date") == str(day)]
    return day_usage(day), len(combos)


def send_backup():
    """/backup : envoie kelly.db (copie cohérente, vérifiée, zippée) sur Telegram -> None si c'est parti, sinon le message d'erreur (kelly/backup.py).
    N'a besoin d'AUCUN verrou des données (la copie est cohérente à elle seule) : le serveur la lance sans le verrou (chat.quick)."""
    from . import backup
    return backup.send()


def courbes_command():
    """« courbes » : envoie les images du capital et du CLV 1xBet (sans sonnerie) ; réponse vide quand elles sont parties, sinon pourquoi il n'y en a pas."""
    import shutil
    import tempfile
    from . import charts
    tmp = tempfile.mkdtemp(prefix="kelly-courbes-")
    try:
        return _courbes_send(charts.courbes(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)                  # les images sont parties (ou n'ont pas pu) : rien ne reste sur le téléphone


def _courbes_send(res):
    if not res:
        return Html("📈 <b>Pas encore de courbe</b>\n" + fmt.esc(f"Il faut au moins 2 jours de paris notés et réglés (capital), ou 2 jours et {C.CHART_MIN_CLV} paris de valeur réglés avec un prix de clôture (CLV)."))
    for path, caption in res:
        try:
            telegram.send_photo(path, caption)
        except (telegram.TelegramError, OSError) as e:
            return f"Courbe non envoyée : {e}"
    return ""


def handle_command(text, confirmed=False):
    """Répond à une commande Telegram. Hors ligne par construction : aucune requête réseau (cotes payantes ou non,
    API-Football, sources), seulement les données du matin dans data/. `confirmed` : la demande « ⚠️ Confirmer ? » a reçu ✅ (voir confirm.py)."""
    with cache.offline():
        cmd, reply = chat.route(text)
        if reply is not None or not cmd:
            return reply
        if not confirmed:
            ask = confirm.check(cmd)                # annuler une note, changer le capital, régler à la main… : d'abord ✅ / ❌
            if ask is not None:
                return ask
        return _handle_command(cmd)


def _handle_command(text):
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    today = store.load_doc("today.json", {"day": "", "picks": []})
    picks = today.get("picks") or []
    if cmd in ("/aide", "/help", "/start"):
        return telegram.with_buttons(HELP, chat.menu())
    if cmd in ("/combis", "/combines", "/combinés"):                  # résultats des combinés, vue séparée : « combis [N jours] »
        return resultats.combis(_days_arg(args))
    if cmd == "/bilan" and args and args[0].lower() in ("combi", "combis", "combiné", "combinés", "combines"):
        return resultats.combis(_days_arg(args[1:]))
    if cmd == "/bilan" and args and args[0].lower() == "saison":
        return resultats.saison(_days_arg(args[1:]))
    if cmd == "/bilan":
        detail = any(a.lower() in ("détail", "detail", "details", "détails") for a in args)
        nums = [a for a in args if a.isdigit()]
        if nums and (len(nums[0]) > 4 or int(nums[0]) > MAX_BILAN_DAYS):          # « /bilan 800000 » : message clair, pas une erreur de date
            return f"Choisis un nombre de jours raisonnable ({MAX_BILAN_DAYS} au plus), par exemple /bilan 30."
        days = int(nums[0]) if nums else None
        bets = store.load("bets.json", {"picks": [], "combos": []})
        out = settle.bilan(bets, days, detail=detail)
        line = money_line(bets["combos"])
        if detail:                                  # carte HTML : le bilan détaillé, la ligne capital, puis « 🧠 Kelly apprend » (texte, échappé)
            d = learn.describe(learn.load(), track.load())
            return Html(out + "\n\n" + fmt.esc(line) + ("\n\n" + fmt.esc(d) if d else ""))
        d = learn.describe_simple(learn.load(), track.load())
        return telegram.with_buttons(Html(out + "\n\n" + fmt.esc(line) + (("\n\n" + d) if d else "")),
                                     telegram.inline([[("🔎 Plus de détails", "bilan|detail")]]))
    if cmd in ("/etat", "/état"):
        return progress.describe()
    if cmd in ("/status", "/statut", "/sante", "/santé"):
        from . import status
        return status.report()
    if cmd in ("/backup", "/sauvegarde"):
        return send_backup()
    if cmd == "/bank":
        return ("💰 Kelly ne suit plus de bankroll en unités : elle travaille en FCFA, avec ton capital (« 💼 Mon capital » ou écris "
                "« capital 50000 ») pour les mises conseillées. Seuls les paris que tu notes comptent dans tes résultats (« 📊 Mes résultats »).")
    if cmd == "/stats":
        return Html(comma(fmt.esc(fmt_team(sources.team_profile(" ".join(args)) if args else None, " ".join(args)))))
    if cmd == "/budget":
        return money.command(args)
    if cmd == "/capital":
        return money.capital_command(args)
    if cmd == "/courbes":                           # les courbes du capital et du CLV 1xBet, en images (kelly/charts.py, aucune bibliothèque)
        return courbes_command()
    if cmd == "/erreurs":                           # « erreurs » / « erreurs 48 » : résumé du journal du téléphone, à coller pour un contrôle
        from . import logdigest
        return logdigest.command(args)
    if cmd == "/tendances":                         # « tendances corners premier » : ce que le modèle sait des équipes et des championnats
        from . import trends
        return trends.report(args)
    if cmd == "/marches":
        from . import markets
        return markets.by_league(args) if args else markets.card()      # « marchés détail [tirs | ldc…] » : par championnat et par sens
    if cmd == "/perte":
        return money.max_loss_command(args)
    if cmd == "/voies":
        return lanes.command(args)
    if cmd == "/carte":                             # « carte [stats | saison] » : carte des edges par case (kelly/edgemap.py)
        return edgemap.report(args)
    if cmd == "/saison":
        return season_command(args, today)
    if cmd == "/objectif":
        return money.goal_command(args)
    if cmd in ("/regle", "/reglé", "/régler", "/regler"):     # « réglé vm3 gagné 12/10 » : un ticket noté que le règlement gratuit ne trouve pas
        now_ = now_utc()
        kday = now_.date() if now_.hour >= C.DAY_END_HOUR else now_.date() - timedelta(days=1)
        return played.settle_command(args, str(today.get("day") or kday)[:10])
    if cmd in ("/joue", "/joué", "/jouer"):
        now_ = now_utc()
        kday = now_.date() if now_.hour >= C.DAY_END_HOUR else now_.date() - timedelta(days=1)
        day = str(today.get("day") or kday)[:10]
        reply = played.command(args, day)
        if isinstance(reply, str) and reply.startswith(played.NEED_STAKE):      # mise manquante : la réponse « cote mise » suivante la note
            chat.set_pending(args[0], day, None, odds=played.parse_odds_stake(args[1:])[0])      # la cote tapée est retenue : seule la mise manque
        elif getattr(reply, "confirm", None):                                   # cote très éloignée : « oui » la note telle quelle
            chat.set_pending(args[0], day, None, confirm=reply.confirm)
        elif isinstance(reply, str) and reply.startswith(("✅ Noté", "Note annulée")):
            chat.clear_pending_for(args[0], day)
        return reply
    if cmd == "/live":                              # combiné live (kelly/live.py) : ne dépend pas de l'analyse du matin
        target = combi_target(args)
        if target is None or target < MIN_COMBI:
            return chat.live_picker()
        toks = money.split_tokens(args)
        n, exact, _ = chat.parse_nlegs(toks[money.take_amount(toks)[1]:])        # « live 20 max 3 »
        return LV.combo(target, str(now_utc().date()), nlegs=(n, exact) if n and 2 <= n <= C.MAX_LEGS else None)
    if cmd in ("/monde", "/ligues"):
        # jour Kelly en cours ; après un rattrapage d'avant C.DAY_END_HOUR h (voir daily_skip_reason), today.json et monde.json portent la
        # date UTC du jour : c'est elle qu'il faut lire, sinon /monde ne trouve rien
        data = monde.load(monde.kelly_day(now_utc())) or (monde.load(today["day"]) if _current_day(today, now_utc()) and today.get("day") else None)
        return monde.fmt_monde(data, " ".join(args)) if cmd == "/monde" else monde.fmt_ligues(data)
    pronos = today.get("pronos") or []
    if not picks and not pronos:
        return f"Kelly n'a pas encore analysé la journée (ça arrive à {C.DAY_END_HOUR:02d}h00)."
    when, picks_when = today.get("xbet_at"), today.get("picks_at") or today.get("xbet_at")      # heures des derniers relevés 1xBet
    if cmd in ("/analyse", "/match") and args:      # paris stats que 1xBet vient peut-être d'ouvrir : relus en direct (fenêtre des stats)
        pronos = reread_match(pronos, " ".join(args))
    if cmd == "/analyse":
        return analyse(picks, " ".join(args), pronos, when, picks_when)
    if cmd == "/top":
        live, live_pronos = upcoming(picks), upcoming(pronos)
        if not live and not live_pronos:
            return waiting_note(today) or f"Tous les matchs du jour ont commencé : rendez-vous demain à {C.DAY_END_HOUR:02d}h00."
        return top_reply(live, live_pronos, when, picks_when)
    if cmd == "/match":
        if not args:
            return "Exemple : /match PSG"
        found = event_matches(picks, " ".join(args))
        if not found:
            free = event_matches(pronos, " ".join(args))
            if free:
                shown, wait = CB.shown_pronos(free), "\n\n".join(x for x in (pending_lines(free), underpaid_lines(free)) if x)
                if wait and not shown:
                    return Html(wait)
                return Html(str(fmt.pronos(shown, 8, when=when)) + ("\n\n" + wait if wait else ""))
            return f"Aucun match du jour ne contient « {' '.join(args)} »."
        real = any(c.get("xodds") for c in picks)           # 1xBet a chiffré des paris du jour : les autres ne sont pas chez lui
        found = sorted(found, key=lambda c: (bool(c.get("xodds")), c["score"]) if real else c["score"], reverse=True)[:8]
        lines = [f"🔎 {fmt.b(fmt.event_name(found[0]))} · {fmt.hhmm(found[0]['start'])} · {fmt.esc(found[0]['league'])}"]
        for c in found:
            why = "" if real and not c.get("xodds") else fmt.why_line(c, CB.leg_reason(c))      # pas de « cote plus haute… » d'un autre book
            lines.append(f"• {fmt.esc(c['pick'])} — {fmt.price_txt(c, real, picks_when)} ({fmt.pct(c['prob'])} de chances)"
                         + (f"\n   ↳ {fmt.esc(why)}" if why else ""))
        shown = _event_pronos(pronos, found)
        if shown:
            lines += ["", str(fmt.pronos(shown, 8, when=when))]
        mine_ = [c for c in pronos if c["id"] in {x["id"] for x in found}]
        wait = "\n\n".join(x for x in (pending_lines(mine_), underpaid_lines(mine_)) if x)
        if wait:
            lines += ["", wait]
        return Html("\n".join(lines))
    if cmd in ("/cotes", "/actualiser"):
        return manual_refresh()
    if cmd == "/combi":
        if args and played.norm(args[0]) in ("auto", "automatique"):      # « combi auto » : pas de cote à choisir, Kelly vise la plus haute cote raisonnable (10 au plus) avec les jambes du jour
            args = auto_args(picks, pronos, args[1:])
            if args is None:
                return "Pas assez de jambes jouables aujourd'hui pour un combiné automatique (il en faut au moins 2 de matchs différents)."
        target = combi_target(args)
        if target is None:
            return "Exemple : /combi 30, ou /combi 20 tirs fautes"
        if target < MIN_COMBI:                              # « /combi 0,5 » : un combiné de 2 paris au moins a une cote d'au moins 1,01
            return f"Un combiné a toujours une cote supérieure à {fmt.num(MIN_COMBI)}. Essaie par exemple /combi 5 ou /combi 30."
        toks = money.split_tokens(args)
        n, exact, rest = chat.parse_nlegs(toks[money.take_amount(toks)[1]:])    # « combi 20 max 4 », « combi 20 pile 4 », « combi 20 4 paris »
        if n is not None and not 2 <= n <= C.MAX_LEGS:
            return f"Un combiné a entre 2 et {C.MAX_LEGS} paris : écris par exemple « combi 20 max 4 » ou « combi 20 pile 4 »."
        bold = any(played.norm(w) in chat.BOLD_WORDS for w in rest)              # « combi 1900 tirs audacieux » / bouton « 🎲 Aller plus loin »
        kinds, unknown = chat.parse_kinds([w for w in rest if played.norm(w) not in chat.BOLD_WORDS])     # « combi 20 tirs fautes »
        if unknown:
            return chat.kinds_help(unknown)
        live = upcoming(picks)
        if not live and not upcoming(pronos) and waiting_note(today):
            return waiting_note(today)
        dropped = reuse_note(today.get("day"), live, upcoming(pronos))          # avant l'envoi : ce combiné-ci ne compte pas encore
        reply = custom_combo(live, upcoming(pronos), target, today.get("day"), started=bool(picks) and not live,
                             when=picks_when, kinds=kinds or None, bold=bold, nlegs=(n, exact) if n else None)
        return _with_sweep(reply, "\n".join(x for x in (sweep_line(live, upcoming(pronos)), dropped) if x))
    return "Commande inconnue. /aide pour la liste."


def auto_args(picks, pronos, rest):
    """Arguments de « /combi » pour « combi auto » : la cote visée est celle du combiné le plus probable que les jambes jouables du jour permettent, en partant de 10 (ou de la plus haute cote atteignable) et en descendant (min 1,5) ;
    None si aucun combiné de 2 paris n'est possible. `rest` : les mots qui suivent « auto » (types de paris, nombre de paris), repris tels quels."""
    live, free = upcoming(picks), upcoming(pronos)
    kinds, _ = chat.parse_kinds([w for w in rest if played.norm(w) not in chat.BOLD_WORDS])
    best = None
    xp, xk = any(c.get("xodds") for c in free), any(c.get("xodds") for c in live)
    for legs in [CB.fair_legs(free)] + ([CB.make_pool(live)] if live and (xk or not xp) else []):         # comme « combi N » : les pronostics, puis les paris réels (le 16/10 les pronostics étaient vides, les paris réels non)
        if kinds:                                   # « combi auto fautes » : la cote atteignable se calcule sur ces types-là seulement
            legs = CB._keep(legs, CB.kinds_prefix(kinds))
        t = min(10.0, CB.reach(legs))
        while t >= 1.5:                             # de la plus haute cote visée vers le bas : la première pour laquelle un combiné existe vraiment
            got = CB.best_combo(legs, t, t * (1 + C.CUSTOM_TOLERANCE))
            if got:
                best = max(best or 0.0, math.floor(got[1] * 10) / 10)      # arrondie vers le bas : la cote visée ne dépasse jamais la cote du combiné
                break
            t *= 0.85
    if best is None:
        return None
    target = best
    return [fmt.num(target)] + list(rest)


MIN_COMBI = 1.01                         # cote visée minimale de « /combi N »

SWEEP_FAMILIES = ("goals", "h2h", "dc", "btts", "team_goals", "corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides", "player")      # familles que « combi N » peut balayer


def sweep_line(picks, pronos):
    """« 📊 12 matchs · 9 familles analysées · 214 lignes évaluées · 31 jambes possibles (jusqu'à 5 marchés par match ; un combiné n'en prend qu'un par match) · non balayées : touches, hors-jeu » :
    ce que « combi N » avait à sa disposition (les pronostics et paris du jour chiffrés chez 1xBet), famille par famille, jamais seulement la meilleure ligne d'un match. '' sans aucune cote 1xBet."""
    try:
        return _sweep_line(picks, pronos)
    except Exception as e:               # noqa: BLE001  une ligne d'information ne doit jamais faire perdre un combiné déjà bâti
        print(f"[combi] ligne de balayage ignorée : {type(e).__name__}", file=sys.stderr)
        return ""


def _sweep_line(picks, pronos):
    legs = CB.fair_legs(pronos)
    rows = [c for c in pronos if c.get("xodds")]
    if not legs:                                      # « combi N » bâtit soit sur les pronostics, soit sur les paris réels, jamais les deux
        legs = CB.make_pool(picks)
        rows = [c for c in picks if c.get("xodds")]
    if not rows:
        return ""
    fams = {calib.family(c["market"]) for c in rows}
    lines = sum(len(c.get("alts") or [None]) for c in rows)
    miss = [calib.LABELS.get(f, f) for f in SWEEP_FAMILIES if f not in fams]
    s = lambda n, one, many: f"{n} {one if n <= 1 else many}"      # noqa: E731
    return (f"📊 {s(len({c['id'] for c in rows}), 'match', 'matchs')} · {s(len(fams), 'famille analysée', 'familles analysées')} · {s(lines, 'ligne évaluée', 'lignes évaluées')}"
            f" · {s(len(legs), 'jambe possible', 'jambes possibles')} · {CB.value_line(legs)}"
            + (f" · non balayées : {', '.join(miss)}" if miss else ""))


def reuse_note(day, picks, pronos):
    """« ♻️ 3 jambes écartées : déjà utilisées aujourd'hui » : jambes possibles de « combi N » que le plafond de réutilisation (CB.reuse_cap) retire à cet instant ; '' sinon."""
    try:
        u = day_usage(day)
        u.filter(CB.fair_legs(pronos) if any(c.get("xodds") for c in pronos) else CB.make_pool(picks))
        n = len(u.excluded)
        return f"♻️ {n} jambe{'s' if n > 1 else ''} écartée{'s' if n > 1 else ''} : déjà utilisée{'s' if n > 1 else ''} aujourd'hui" if n else ""
    except Exception as e:               # noqa: BLE001
        print(f"[combi] note de réutilisation ignorée : {type(e).__name__}", file=sys.stderr)
        return ""


def _with_sweep(reply, line):
    """Le message `reply` suivi de la ligne de balayage `line` (même type, mêmes boutons) ; inchangé sans ligne ou pour une réponse qui n'est pas un texte."""
    if not line or not isinstance(reply, str):
        return reply
    out = type(reply)(str(reply) + "\n\n" + (fmt.i(fmt.esc(line)) if isinstance(reply, Html) else line))
    for attr in ("markup", "confirm"):
        if getattr(reply, attr, None) is not None:
            setattr(out, attr, getattr(reply, attr))
    return out


def combi_target(args):
    """La cote visée de « /combi 20 » / « combi 10 000 » / « /combi 2,5 » (milliers réunis : « 10 000 », « 3 000 »), ou None si illisible (négatif, inf, nan,
    texte). Les mots qui suivent le nombre sont ignorés."""
    v, _ = money.take_amount(money.split_tokens(args))
    return float(v) if v is not None and math.isfinite(v) else None


SEASON_REBUILD_FILE = "season_rebuild.json"     # {"day", "at"} : dernière reconstruction `saison auto` (écart minimal C.SEASON_REBUILD_GAP)


def season_listing(today, day, mode, morning, now):
    """« saison » sans numéro : l'état de la voie, les combinés du jour (actifs ; les remplacés ne sont que comptés) avec pour chacun un bouton qui le rouvre (sans réseau), la mention
    « construit avant la dernière mise à jour » quand ses réglages ne sont plus ceux d'aujourd'hui, et « 🔄 Reconstruire »."""
    rows = [r for r in fair.load_combos() if str(r.get("date")) == day and not r.get("custom") and CB.season_no(r["tier"]) is not None]
    active, closed = [r for r in rows if not r.get("closed")], [r for r in rows if r.get("closed")]
    ghost = [r for r in fair.load_ghost() if r.get("date") == day and r.get("kind") == "saison" and not r.get("closed")]
    value = sum(1 for c in morning if c.get("sval"))
    tiers = "adaptatives (la cote est le résultat)" if C.SEASON_AUTO else ", ".join(fmt.trim(t, 1) for t in C.SEASON_TIERS)
    out = [f"🧮 Saison en cours — voie : {mode} · cotes visées {tiers} · poids du modèle {C.SEASON_MODEL_WEIGHT * 100:.0f} %",
           f"Jambes du matin encore jouables : {len(morning)} ({value} à valeur, {len(morning) - value} de remplissage)" if morning else "Aucune jambe « saison » jouable aujourd'hui."]
    buttons, sig = [], CB.season_signature()
    for r in sorted(active, key=lambda r: CB.season_no(r["tier"])):
        old = CB.season_auto(r["tier"]) and r.get("sbuild") != sig
        out.append(f"• cote {fmt.trim(r['min_odds'], 1)} ({len(r.get('legs') or [])} paris) — " + ("noté joué" if r.get("played") else "envoyé") + (f" · {r['result']}" if r.get("result") else "")
                   + (f" · reconstruit à {r['built_at']}" if r.get("built_at") else "") + (" · ⚠️ construit avant la dernière mise à jour" if old else ""))
        buttons.append([(f"🔓 Rouvrir · cote {fmt.trim(r['min_odds'], 1)}", f"so|{r['tier']}|{day}")])
    if closed:
        out.append(f"♻️ {len(closed)} combiné{'s' if len(closed) > 1 else ''} remplacé{'s' if len(closed) > 1 else ''} par une reconstruction")
    if ghost:
        out.append(f"👻 {len(ghost)} combiné{'s' if len(ghost) > 1 else ''} sans valeur suivi{'s' if len(ghost) > 1 else ''} en fantôme (non envoyé{'s' if len(ghost) > 1 else ''})")
    out.append("« saison 10 » : un combiné à la cote 10, jambes reconstruites sur les matchs du jour pas commencés (coupes comprises), cotes relues chez 1xBet. "
               "« saison auto » (ou 🔄 Reconstruire) : relance la construction automatique du jour ; un combiné noté joué est gardé.")
    if mode == "on" and C.SEASON_AUTO:
        buttons.append([("🔄 Reconstruire", f"sa|rb|{day}")])
    text = telegram.Text("\n".join(out))
    return telegram.with_buttons(text, telegram.inline(buttons)) if buttons else text


def season_open(tier, day):
    """« 🔓 Rouvrir » : le message d'un combiné `saison` du jour, refait depuis sa ligne enregistrée (jambes, cote, mise, code coupon, heure des cotes), avec les boutons d'un combiné du matin. Aucun réseau."""
    row = next((r for r in fair.load_combos() if str(r.get("date")) == str(day) and str(r.get("tier")) == str(tier) and not r.get("custom")), None)
    if row is None or CB.season_no(tier) is None:
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    if row.get("closed") and not fair.is_noted(row):
        return f"Ce combiné a été remplacé à {row.get('closed_at') or '?'} par la reconstruction de la voie saison : écris « saison » pour voir ceux d'aujourd'hui."
    legs = [dict(l) for l in row.get("legs") or []]
    mise = lambda st, prob=None, odds=None, nov=False, flat=False: money.stake_words(st if money.active() else None, prob, odds, novalue=nov, flat=flat)    # noqa: E731
    msg = fair_combo_message(row["tier"], legs, row["min_odds"], row["prob"], day, row.get("stake"), mise, row.get("coupon"), row.get("at"),
                             _season_number(day, row["tier"]), lookup=True)       # lookup : l'état réel (noté joué ou pas) dans les boutons
    old = CB.season_auto(row["tier"]) and row.get("sbuild") != CB.season_signature()
    if old:
        msg = telegram.with_buttons(telegram.Html(str(msg) + "\n\n" + fmt.i(fmt.esc("⚠️ Construit avant la dernière mise à jour de la construction automatique."))), msg.markup) \
            if getattr(msg, "markup", None) else msg
    return msg


def _season_number(day, tier):
    """Numéro « COMBINÉ N » d'un combiné `saison` auto du jour : sa place parmi les combinés actifs, dans l'ordre d'enregistrement (le même à l'envoi, au listing et à la réouverture)."""
    act = [str(r["tier"]) for r in fair.load_combos() if str(r.get("date")) == str(day) and not r.get("closed") and not r.get("custom") and CB.season_auto(r["tier"])]
    return act.index(str(tier)) + 1 if str(tier) in act else None


def _committed(day, rows, noted=True):
    """Mises déjà engagées aujourd'hui (FCFA) : combinés du matin et à vérifier non morts (`rows` : fair_combos), combinés réels envoyés et paris seuls envoyés.
    `noted=False` : sans les mises NOTÉES (en mode capital, money.daily() les a déjà retirées de la limite : les recompter les compterait deux fois)."""
    def amount(x):
        if not noted and fair.is_noted(x):
            return 0.0
        v = (x.get("played") or {}).get("stake") if fair.is_noted(x) else x.get("stake")      # un combiné noté joué : la mise NOTÉE (pas la conseillée)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0
    total = sum(amount(r) for r in rows if str(r.get("date")) == str(day) and not r.get("dead") and not r.get("closed"))
    total += sum(amount(c) for c in store.load_doc("bets.json", {"combos": []}).get("combos", []) if str(c.get("date")) == str(day) and not c.get("dead"))
    total += sum(amount(r) for r in track.load() if r.get("date") == str(day) and r.get("sent") and (r.get("single") is True or r.get("sharpgap")))
    return total


def _season_split(day):
    """(lignes fair_combos, gardés, remplaçables) des combinés `saison` du jour : gardés = notés joués ou déjà réglés (jamais remplacés), remplaçables = ni gardés, ni morts, ni fermés."""
    rows = fair.load_combos()
    mine = [r for r in rows if str(r.get("date")) == day and not r.get("custom") and CB.season_no(r["tier"]) is not None]
    kept = [r for r in mine if (fair.is_noted(r) or r.get("result") is not None) and not r.get("closed") and not r.get("dead")]
    return rows, kept, [r for r in mine if r not in kept and not r.get("dead") and not r.get("closed")]


def season_rebuild(today, now=None, fetch=None):
    """« saison auto » / « 🔄 Reconstruire » : relance la construction AUTOMATIQUE de la voie `saison` sur les matchs du jour pas commencés, avec les réglages installés (SEASON_AUTO_*), cotes 1xBet relues.
    Dans cet ordre, sans rien détruire avant d'avoir un remplaçant : (1) relecture, (2) usage des matchs sans les combinés remplaçables, (3) nouveau plan, (4) mises et coupons, (5) SEULEMENT s'il y a au moins
    un combiné à envoyer : les combinés du matin non joués sont fermés (« reconstruit », atomiquement), les nouveaux enregistrés et envoyés ; une panne entre les deux rouvre ce qui a été fermé. Un combiné noté
    joué ou déjà réglé est gardé, ses matchs restent consommés. Plafond : au plus C.SEASON_AUTO_MAX_COMBOS combinés ACTIFS dans la journée (gardés + nouveaux ; les remplacés ne comptent plus). Écart minimal
    C.SEASON_REBUILD_GAP minutes entre deux reconstructions (chacune relit 1xBet par le proxy)."""
    now = now or now_utc()
    day = today.get("day") or ""
    mode = lanes.season_mode()
    if mode != "on":
        return f"La voie « saison » est {'en fantôme (calculée, jamais jouée)' if mode == 'ghost' else 'éteinte'} : rien à reconstruire."
    if not C.SEASON_AUTO:
        return "La construction automatique est éteinte (SEASON_TIERS fixe dans ta configuration) : « saison 10 » construit un combiné à la cote voulue."
    last = store.load(SEASON_REBUILD_FILE, {})
    if isinstance(last, dict) and last.get("day") == day and last.get("at"):
        try:
            wait = C.SEASON_REBUILD_GAP * 60 - (now - datetime.fromisoformat(last["at"])).total_seconds()
        except (TypeError, ValueError):
            wait = 0
        if wait > 0:
            return f"Une reconstruction vient d'avoir lieu à {str(last['at'])[11:16]} : attends encore {math.ceil(wait / 60)} min (chaque reconstruction relit 1xBet)."
    rows, kept, replace = _season_split(day)
    slots = C.SEASON_AUTO_MAX_COMBOS - len(kept)
    if slots <= 0:                                  # avant toute relecture : le proxy n'est pas sollicité pour rien
        return f"Déjà {len(kept)} combinés `saison` joués ou réglés aujourd'hui : le plafond de {C.SEASON_AUTO_MAX_COMBOS} est atteint, rien à reconstruire."
    telegram.send("⏳ Je reconstruis la voie saison sur les matchs du jour chez 1xBet…", queue=False, silent=True)
    rebuilt, info = seasonlane.rebuild(now, fetch)
    store.save(SEASON_REBUILD_FILE, {"day": day, "at": now.isoformat(timespec="seconds")})       # même un relevé sans résultat compte pour l'écart (proxy)
    line = seasonlane.summary_line(info) + (f" · ⚠️ plafond mensuel de données 1xBet atteint à {C.SEASON_MONTH_PCT:g} % : pas de relecture" if info.get("capped") else "")
    legs = [c for c in rebuilt if lanes.usable(c)]
    rows, kept, replace = _season_split(day)           # RELU après la relecture réseau (longue) : une note ou un règlement arrivés entre-temps comptent
    slots = C.SEASON_AUTO_MAX_COMBOS - len(kept)
    if slots <= 0:
        return line + f"\nDéjà {len(kept)} combinés `saison` joués ou réglés aujourd'hui : le plafond de {C.SEASON_AUTO_MAX_COMBOS} est atteint, rien à reconstruire."
    if not info["read"]:
        return line + "\n1xBet n'a rien rendu à l'instant : tes combinés actuels restent tels quels."
    exclude = {str(l.get("id") or str(l.get("key") or "").split("|")[0]) for r in kept for l in (r.get("legs") or [])}
    usage = day_usage(day, rows=[r for r in rows if r not in replace])
    plan = CB.season_auto_plan(legs, usage, max_combos=slots, busy_tiers=[r["tier"] for r in rows if str(r.get("date")) == day], exclude=exclude)
    sent = [(t, c) for t, c in plan if not fair.unsent(c[0])]
    if not sent:                                    # rien d'ENVOYABLE (aucun, ou que des combinés sans valeur en fantôme) : on ne détruit rien
        return line + "\nPas assez de jambes `saison` jouables pour un combiné à envoyer : tes combinés actuels restent tels quels."
    hhmm = now.strftime("%H:%M")
    # tout ce qui peut échouer (mises, coupons) AVANT de fermer quoi que ce soit
    left = None
    if money.active():
        left = max(0.0, money.daily() - _committed(day, [r for r in rows if r not in replace], noted=not money.capital_mode()))       # mode capital : daily() a déjà retiré les mises notées
        if money.capital_mode():
            left = min(left, max(0.0, money.capital_state().get("left") or 0.0))
    items = []
    for t, (cl, odds_, prob) in sent:
        item = {"kind": "fair", "tier": odds_, "prob": CB.stake_prob(cl, prob), "odds": odds_, "p_mkt": CB.market_prob(cl)} | lanes.item_fields(cl)
        if odds_ >= C.SEASON_AUTO_LOTTERY_ODDS:
            item["lottery"] = True
        items.append(item)
    stakes = dict(zip([t for t, _ in sent], money.allocate(items, budget=left, step=money.round_step(money.daily()))[0])) if items and left is not None else {}
    coupons = {}
    for t, (cl, odds_, prob) in sent:
        if all(c.get("xref") and c.get("xodds") for c in cl):
            code = xbet.save_coupon(cl)
            if code:
                coupons[t] = code
    # fermeture ATOMIQUE (relue au moment de l'écriture : un combiné noté entre-temps n'est jamais fermé), puis enregistrement ; une panne entre les deux rouvre ce qui a été fermé
    def close(cur):
        done = []
        for r in cur if isinstance(cur, list) else []:
            if (str(r.get("date")) == day and not r.get("custom") and CB.season_no(r.get("tier")) is not None and not fair.is_noted(r) and r.get("result") is None
                    and not r.get("dead") and not r.get("closed")):
                fair.close_row(r, "reconstruit", hhmm)
                done.append(str(r["tier"]))
        return cur, done
    new_tiers = {str(t) for t, _ in sent}
    closed_tiers = store.update(fair.COMBOS_FILE, close, [])
    ghosts, closed_ghosts = fair.load_ghost(), []
    try:
        for g in ghosts:
            if g.get("date") == day and g.get("kind") == "saison" and not g.get("closed"):
                fair.close_row(g, "reconstruit", hhmm)
                closed_ghosts.append(g)
        if closed_ghosts:
            store.save(fair.GHOST_COMBOS_FILE, ghosts)
        for t, c in plan:
            if fair.unsent(c[0]):                   # sans valeur et non envoyé : fantôme, comme le matin
                fair.ghost_record(t, c, day, "saison")
        fair.record_combos(sent, day, stakes if left is not None else None, money.cur() if stakes else None, coupons=coupons, at=hhmm)
        def tag(cur):
            for r in cur if isinstance(cur, list) else []:
                if str(r.get("date")) == day and str(r.get("tier")) in new_tiers and not r.get("origin"):
                    r["origin"], r["built_at"] = "reconstruit", hhmm
            return cur, None
        store.update(fair.COMBOS_FILE, tag, [])
    except Exception as e:                          # noqa: BLE001 - les combinés fermés sont rouverts : jamais une journée sans combiné
        print(f"[saison] reconstruction interrompue ({type(e).__name__}: {e}) : combinés du matin rouverts", file=sys.stderr)

        def reopen(cur):
            cur = [r for r in cur if isinstance(r, dict)] if isinstance(cur, list) else []
            cur = [r for r in cur if not (str(r.get("date")) == day and str(r.get("tier")) in new_tiers and not r.get("origin"))]       # les nouveaux, enregistrés mais jamais envoyés, partent
            for r in cur:
                if str(r.get("date")) == day and str(r.get("tier")) in closed_tiers and r.get("closed") == "reconstruit" and r.get("closed_at") == hhmm:
                    for k in ("dead", "closed", "closed_at"):
                        r.pop(k, None)
                    r["result"] = None
            return cur, None
        try:
            store.update(fair.COMBOS_FILE, reopen, [])
            if closed_ghosts:                       # ... et les fantômes aussi
                back = fair.load_ghost()
                for g in back:
                    if g.get("date") == day and g.get("closed") == "reconstruit" and g.get("closed_at") == hhmm:
                        for k in ("dead", "closed", "closed_at"):
                            g.pop(k, None)
                        g["result"] = None
                store.save(fair.GHOST_COMBOS_FILE, back)
        except Exception as e2:                     # noqa: BLE001
            print(f"[saison] réouverture impossible ({type(e2).__name__}: {e2})", file=sys.stderr)
        return line + "\n⚠️ La reconstruction a été interrompue : tes combinés du matin sont rouverts, rien n'a changé. Réessaie dans quelques minutes."
    mise = lambda st, prob=None, odds=None, nov=False, flat=False: money.stake_words(st if money.active() else None, prob, odds, novalue=nov, flat=flat)    # noqa: E731
    msgs = [fair_combo_message(t, cl, odds_, prob, day, stakes.get(t), mise, coupons.get(t), hhmm, _season_number(day, t)) for t, (cl, odds_, prob) in sent]
    head = (line + f"\n♻️ {len(closed_tiers)} combiné{'s' if len(closed_tiers) > 1 else ''} du matin remplacé{'s' if len(closed_tiers) > 1 else ''}"
            + (f" · {len(kept)} joué{'s' if len(kept) > 1 else ''} ou réglé{'s' if len(kept) > 1 else ''} gardé{'s' if len(kept) > 1 else ''}" if kept else "")
            + f" · {len(plan)} nouveau{'x' if len(plan) > 1 else ''}" + (f" ({len(plan) - len(sent)} sans valeur en fantôme)" if len(plan) > len(sent) else "")
            + f" · plafond {C.SEASON_AUTO_MAX_COMBOS} combinés actifs")
    reply = telegram.Text(head)
    if msgs:
        reply.more = msgs
    return reply


def _days_arg(args):
    """Nombre de jours d'une commande de résultats (« combis 7 ») ; None = tout ; plafonné à MAX_BILAN_DAYS."""
    n = next((a for a in args if str(a).isdigit() and len(str(a)) <= 4), None)
    return min(int(n), MAX_BILAN_DAYS) if n else None


def season_command(args, today, now=None, fetch=None):
    """« saison » : l'état de la voie `saison` (jambes du matin, combinés du jour, fantômes) ; « saison N » : un combiné sur mesure à la cote N bâti sur les SEULES jambes `saison`,
    RECONSTRUITES à la demande sur les matchs du jour pas encore commencés (championnats suivis et coupes, LDC comprise) avec les cotes 1xBet relues à l'instant (seasonlane.rebuild :
    au plus C.SEASON_MAX_MATCHES matchs et C.SEASON_SECONDS s). Une ligne dit ce qui a été trouvé : matchs lus, de coupe, équipes sous le minimum de matchs, matchs sans prix. Jambes de valeur
    d'abord, puis le remplissage ; code coupon, mise et bouton « ✅ Je le joue » (suivi comme « combi N »). 1xBet muet : les jambes du matin, cotes du dernier relevé, dit clairement."""
    now = now or now_utc()
    day = today.get("day") or ""
    morning = [c for c in upcoming(today.get("season") or [], now) if lanes.usable(c)]
    mode = lanes.season_mode()
    if args and str(args[0]).lower() in ("auto", "reconstruire"):          # « saison auto » / « 🔄 Reconstruire » : relance la construction automatique du jour
        return season_rebuild(today, now, fetch)
    if args and str(args[0]).lower() in ("résultats", "resultats", "résultat", "resultat", "bilan"):     # « saison résultats [N] » : la vue séparée des résultats de la voie
        return resultats.saison(_days_arg(args[1:]))
    if not args:
        return season_listing(today, day, mode, morning, now)
    target = combi_target(args)
    if target is None or target <= 1.0:
        return "Exemple : saison 10 (un combiné « saison en cours » à la cote 10), ou saison seul pour l'état de la voie."
    if mode != "on":
        return f"La voie « saison » est {'en fantôme (calculée, jamais jouée)' if mode == 'ghost' else 'éteinte'} : aucun combiné proposé."
    telegram.send("⏳ Je relis les matchs du jour chez 1xBet…", queue=False, silent=True)
    rebuilt, info = seasonlane.rebuild(now, fetch)
    line = seasonlane.summary_line(info) + (f" · ⚠️ plafond mensuel de données 1xBet atteint à {C.SEASON_MONTH_PCT:g} % : pas de relecture" if info.get("capped") else "")
    legs, stale, when = [c for c in rebuilt if lanes.usable(c)], False, now.strftime("%H:%M")
    if not info["read"]:                              # rien relu : les jambes du matin, cotes du dernier relevé
        legs, stale, when = morning, True, today.get("xbet_at")
        line += (" — aucun match du jour gardé pour la voie (ni liste du matin, ni tour du monde) : jambes et cotes du matin ; la voie prendra les matchs au prochain passage du matin." if not info["events"]
                 else " — 1xBet n'a rien rendu à l'instant : jambes et cotes du matin, vérifie-les avant de valider." if info["predicted"]
                 else " — aucun match du jour à lire (aucune équipe suivie, ou tous commencés) : jambes et cotes du matin, vérifie-les avant de valider.")
    if len(legs) < 2:
        return line + "\nPas assez de jambes « saison » jouables (il en faut au moins deux sur deux matchs différents)."
    used = day_usage(day)
    used.filter(legs)
    if used.excluded:
        line += f"\n♻️ {len(used.excluded)} jambe{'s' if len(used.excluded) > 1 else ''} écartée{'s' if len(used.excluded) > 1 else ''} : déjà utilisée{'s' if len(used.excluded) > 1 else ''} aujourd'hui"
    combo = CB.season_plan(legs, [target], usage=day_usage(day))[0][1]
    if not combo:
        lo = target * (1 - C.SEASON_SPREAD)
        free = day_usage(day).filter(CB.season_pool(legs))
        top = CB.reach([c for c in free if c.get("sval") or C.FILL_LEGS], C.MAX_LEGS)        # les MÊMES jambes que le constructeur (voie saison, cote 1xBet, libres, de valeur si le remplissage est coupé)
        head = f"\nAucun combiné « saison » à la cote {fmt.trim(target, 1)} avec ces jambes"
        if top > 1 and top >= lo:       # la cote visée est atteignable en principe : le message ne doit pas dire le contraire
            return line + head + f" : la plus haute cote atteignable ({fmt.trim(top, 1)}) atteint la fourchette de {fmt.trim(lo, 0)} à {fmt.trim(target * (1 + C.SEASON_SPREAD), 0)}, mais aucune combinaison ne respecte les règles de construction (un pari par match, plafond de remplissage). Essaie une autre cote."
        return line + head + (f" : la plus haute cote atteignable est {fmt.trim(top, 1)}." if top > 1 else ".")
    lo, hi = target * (1 - C.SEASON_SPREAD), target * (1 + C.SEASON_SPREAD)
    keep = {"day": day, "target": target, "lo": lo, "hi": hi, "src": "saison"}
    intro = f"{C.SEASON_TITLE.split(' ', 1)[0]} Combiné « saison en cours » à la cote {fmt.trim(target, 1)} (modèle des matchs de cette saison, mélangé au marché).\n{line}"
    return _fair_reply(combo, intro, title=f"{C.SEASON_TITLE} · cote {fmt.trim(target, 1)}", when=when, stale=stale, retry="redemande le combi", budget=True, keep=keep,
                       shared=_shared_counts(day, combo[0]))


def _coupon(legs):
    """Code coupon 1xBet d'un combiné dont toutes les jambes ont leur référence et leur cote 1xBet (xbet.save_coupon : ticket sans mise) ;
    None sinon, ou si l'enregistrement échoue (jamais d'erreur)."""
    return xbet.save_coupon(legs) if legs and all(c.get("xref") and c.get("xodds") for c in legs) else None


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
    return _stake_words(_budget_stake(prob, odds), prob, odds)


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
        code = _coupon(legs) if coupon else None
    stake = None
    if budget and stake_txt is None:
        p_stake = CB.stake_prob(legs, prob)          # jambe « probable » (sans avis sharp) : proba 1xBet sans marge
        stake = _budget_stake(p_stake, min_total, legs=legs)
        stake_txt = _stake_words(stake, p_stake, min_total, legs)
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
    now = now or now_utc()
    ids = {l["id"] for l in legs}
    spares = sorted((c for c in upcoming(store.load("today.json", {}).get("season") or [], now) if c["id"] not in ids and lanes.usable(c)), key=lambda c: -c["prob"])
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
    pool = [c for c in upcoming(store.load("today.json", {}).get("pronos") or [], now) if c["id"] not in avoid]
    mine = {l["id"] for l in legs}
    spares = []
    for c in sorted((c for c in CB.fair_legs(pool) if c["id"] not in mine and (not prefix or c["market"].startswith(prefix))),
                    key=lambda c: -(c.get("xev") or 0) * c["prob"]):
        if c["id"] not in spares:
            spares.append(c["id"])
    pronos, got, hhmm = (check or xprice.live_check)(mine | set(spares[:C.XBET_LIVE_SPARES]), now, fresh_min=fresh_min)
    if not got:
        return None, hhmm, False
    return CB.patch_combo(legs, [c for c in upcoming(pronos, now) if c["id"] not in avoid], lo, hi, fresh_ids=got, prefix=prefix, usage=usage), hhmm, True


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
        res, hhmm, ok = _live_fair(fc[0], lo, hi, prefix=CB.kinds_prefix((keep or {}).get("kinds")), now=now, check=check,
                                   fresh_min=C.XBET_COMBI_FRESH_MIN, usage=usage, avoid=avoid)
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même
        print(f"[1xbet] vérification en direct impossible : {type(e).__name__}: {e}", file=sys.stderr)
        res, hhmm, ok = None, "", False
    if not ok and _all_soon(fc[0], now or now_utc()):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes. Demande plutôt un autre « combi ».")
    if not ok:                                      # cotes du dernier relevé : l'heure réelle de leur relevé (cache), sinon celle du relevé du jour
        t0 = xbet.odds_time([(c.get("xref") or [c.get("xgid")])[0] for c in fc[0]])
        last = datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%H:%M") if t0 else store.load("today.json", {}).get("xbet_at") or ""
        why = xbet_why()
        return _fair_reply(fc, f"⚠️ 1xBet ne répond pas en ce moment{f' ({why})' if why else ''} : combiné aux cotes du dernier relevé (cote "
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
    when, stale = _when_note(legs)
    when = when or hhmm
    only = CB.kinds_label((keep or {}).get("kinds"))
    return _fair_reply((legs, total, prob), "", tail=(f"\n🎯 Paris choisis : {only}" if only else "") + stale + _note(keep),
                       shared=_shared_counts((keep or {}).get("day"), legs), changes=fmt.combo_changes(removed, added, changed), when=when, retry="redemande le combi", budget=True, keep=keep)


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


def _check_ticket(row, legs, day, tier, now, check=None):
    """« 🎟️ Vérifier ce combiné » sous un combiné du matin NOTÉ joué : ses matchs sont relus EN DIRECT chez 1xBet et Kelly dit si un pari du ticket n'y
    est plus ou paie moins ; la ligne de suivi (paris, note, règlement) ne change JAMAIS. Pour une version à jour : « ↩️ Annuler » la note, puis
    « 🎟️ Vérifier ce combiné ». -> la réponse."""
    telegram.send("⏳ Je vérifie ton ticket chez 1xBet…", queue=False, silent=True)
    season = CB.season_no(tier) is not None or any(l.get("strategy") == "saison" or l.get("lane") == "saison" for l in legs)
    pool, got, hhmm = (check or (seasonlane.live_check if season else xprice.live_check))({l["id"] for l in legs}, now, fresh_min=None)
    if not got:
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    probs = _ticket_problems(legs, pool, got, now)
    p = row["played"]
    if probs:
        text = (fmt.esc(f"⚠️ Un pari de ton ticket n'est plus proposé chez 1xBet (combiné joué à {fmt.odds_txt(p['odds'])}, vérifié à {hhmm}) :")
                + "\n" + "\n".join(_ticket_lines(probs)) + "\n\n" + fmt.i(fmt.esc(TICKET_ADVICE)))
    else:
        text = fmt.esc(f"✅ Ton ticket (combiné joué à {fmt.odds_txt(p['odds'])}) est toujours chez 1xBet à {hhmm} : tous ses paris sont encore proposés. "
                       "Ta cote est acquise, rien à changer.")
    return chat.with_played_button(Html(text), f"v{tier}", day, refresh=tier, lookup=True, code=row.get("coupon"))


def refresh_combo(tier, day, now=None, check=None, fetch=None):
    """Bouton « 🎟️ Vérifier ce combiné » sous un combiné du matin : ses paris relus en direct chez 1xBet, ceux retirés ou devenus mal payés
    remplacés (même fourchette de cote que le palier), nouveau code coupon. Part de la DERNIÈRE version envoyée du combiné (COMBO_UPDATES),
    pas de celle du matin, et refuse si un de ses matchs a commencé (il est en jeu). La nouvelle version devient celle du suivi
    (règlement, bilan) comme dans _update_combos, avec les mêmes boutons de mise. Un combiné du matin NOTÉ joué (le ticket est placé) n'est JAMAIS
    remplacé : ses matchs sont relus et Kelly dit si un de ses paris n'y est plus ou paie moins (_check_ticket) ; pour une version à jour, « ↩️ Annuler »
    d'abord. Combiné sur mesure suivi (« m<n> », kelly/custom.py) : même
    chose dans SA fourchette de cote (celle de « combi N ») ; construit sur des paris réels, il est relu comme un combiné réel
    (_refresh_picks : paris non remplacés). Un combiné sur mesure NOTÉ joué est protégé comme un ticket du matin : sa note et ses paris
    ne tombent jamais (relu et dit seulement). `fetch` : relevé 1xBet de ce dernier cas, injectable pour les tests."""
    now = now or now_utc()
    rows = fair.load_combos()
    row = next((r for r in rows if r["date"] == str(day) and str(r["tier"]) == str(tier)), None)
    mine = bool(row) and played.is_custom(row)
    rng = (row["lo"], row["hi"], CB.kinds_prefix(row.get("kinds"))) if mine else _tier_range(tier)
    if not row or not rng:
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    if row.get("closed") and not fair.is_noted(row):
        return f"Ce combiné a été remplacé à {row.get('closed_at') or '?'} par la reconstruction de la voie saison : écris « saison » pour voir ceux d'aujourd'hui."
    lo, hi, prefix = rng
    sent = _load_updates(str(day))
    base = _base_legs(sent, row)
    if _started(base, now):
        return "Ce combiné est en jeu : un match a commencé, Kelly ne le modifie plus."
    if mine and not played.is_real(row):
        return "Ce combiné est à la cote mini de Kelly, pas aux cotes de 1xBet : il n'y a rien à revérifier, compare chaque cote dans l'appli."
    if mine and row.get("src") == "picks":
        return _refresh_picks(row, rows, sent, str(day), now, fetch)        # noté : relu et affiché, jamais remplacé (fair.is_noted)
    if fair.is_ticket(row) or fair.is_noted(row):   # noté joué (combiné du matin ou sur mesure) : le ticket est placé, on le relit et on le dit, sans jamais le remplacer
        return _check_ticket(row, base, str(day), str(tier), now, check)
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False, silent=True)
    if CB.season_no(tier) is not None or (mine and row.get("src") == "saison"):      # voie `saison` : relue sur SES jambes, jamais remplacée par celles des autres voies
        res, hhmm, ok = _live_season(base, lo, hi, now, check, usage=None if mine else _others_usage(rows, row, str(day), sent))
    else:
        res, hhmm, ok = _live_fair(base, lo, hi, prefix, now, check, usage=None if mine else _others_usage(rows, row, str(day), sent))
    if not ok:
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    if res is None:
        return ("Les paris de ce combiné ne sont plus jouables chez 1xBet et Kelly ne trouve pas de remplaçants dans la même "
                "fourchette de cote. Demande plutôt un « combi » sur mesure.")
    legs, total, prob, removed, added, changed = res
    code = _coupon(legs)
    dropped = _record_version(sent, row, rows, (legs, total, prob), hhmm, code)
    head = (f"🔄 {'Combi sur mesure' if mine else 'Combiné ' + _of_day(tier)} remis aux cotes 1xBet de {hhmm}"
            + (" : aucun pari à changer." if not (removed or added or changed) else ".") + (DROPPED_NOTE if dropped else ""))
    msg = _fair_reply((legs, total, prob), head, fmt.combo_changes(removed, added, changed),
                      title=f"🔄 {'COMBI SUR MESURE' if mine else 'COMBINÉ'} À JOUR {hhmm}", stake_txt=_stake_txt(row), when=hhmm, code=code,
                      retry=RETRY_BUTTON)
    return chat.with_played_button(msg, f"v{tier}", str(day), refresh=tier, lookup=True, code=code)


def _real_changes(res):
    """Ce que la relecture 1xBet des paris réels `res` (xprice.live_picks) a changé : (paris que 1xBet ne propose plus, les paris remis au
    prix du moment, lignes « ❌ Plus chez 1xBet / ⚠️ Moins bien payé / 📈 📉 » à montrer). Voir refresh_real et _refresh_picks."""
    gone = [c for c, st, _ in res if st == "gone"]
    new = [c2 if st == "ok" else c for c, st, c2 in res]
    changes = [f"❌ Plus chez 1xBet : {fmt.esc(c['home'])} – {fmt.esc(c['away'])} ({fmt.esc(c['pick'])})" for c in gone]
    for c, st, c2 in res:
        if st != "ok":
            continue
        old, cur = c.get("xodds") or c["odds"], c2["xodds"]
        if not CB.leg_reason(c2):
            changes.append(f"⚠️ Moins bien payé : {fmt.esc(c['pick'])} {fmt.odds_txt(old)} ➜ {fmt.odds_txt(cur)} (plus d'avantage clair)")
        elif abs(cur / old - 1) >= 0.02:
            changes.append(f"{'📈' if cur > old else '📉'} {fmt.esc(c['pick'])} : {fmt.odds_txt(old)} ➜ {fmt.odds_txt(cur)}")
    return gone, new, changes


def _refresh_picks(row, rows, sent, day, now, fetch=None):
    """« 🎟️ Vérifier ce combiné » sous un combiné sur mesure suivi bâti sur des paris RÉELS remis au prix 1xBet (refresh_combo) : ses matchs
    sont relus EN DIRECT chez 1xBet comme ceux d'un combiné réel (refresh_real) ; les paris ne sont jamais remplacés. Un pari qui n'est plus
    chez 1xBet : le combiné n'est plus jouable tel quel (rien n'est modifié). Sinon sa ligne de suivi prend les cotes du moment et le NOUVEAU
    code coupon (mise gardée, comme _record_version). Les jambes viennent de today.json (tous les champs du pari), au prix de leur dernière
    version suivie."""
    today = store.load("today.json", {})
    by_key = {c["key"]: c for c in today.get("picks") or []}
    legs = []
    for l in _base_legs(sent, row):
        c = by_key.get(l["key"])
        legs.append(c and c | {k: v for k, v in l.items() if v is not None and k != "result"})
    if today.get("day") != day or not all(legs):
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    if _all_soon(legs, now):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes.")
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False, silent=True)
    res, hhmm = xprice.live_picks(legs, now, fetch)
    if all(st == "stale" for _, st, _ in res):
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    gone, new, changes = _real_changes(res)
    if gone:
        return Html(fmt.esc(f"🔄 Ce combiné vérifié chez 1xBet à {hhmm} : il n'est plus jouable tel quel.") + "\n" + "\n".join(changes)
                    + "\n\n" + fmt.i("Un pari n'est plus proposé par 1xBet. Demande un nouveau « combi » sur mesure, ou touche « 🔄 Mettre à jour "
                                      "tous les paris »."))
    total, prob = math.prod(c["odds"] for c in new), CB.combo_prob(new)
    code = _coupon(new)
    dropped = _record_version(sent, row, rows, (new, total, prob), hhmm, code)      # noté : rien n'est écrit (la note et les paris restent)
    head = f"🔄 Combi sur mesure remis aux cotes 1xBet de {hhmm}" + ("." if changes else " : aucun changement.") + (DROPPED_NOTE if dropped else "")
    if fair.is_noted(row):                          # ticket placé : on montre les cotes du moment, ta note « joué à … » et tes paris ne changent pas
        head = (f"🎟️ Ton ticket (combi sur mesure joué à {fmt.odds_txt(row['played']['odds'])}) relu chez 1xBet à {hhmm}" + ("." if changes else " : aucun changement.")
                + " Ta note et ses paris ne changent pas ; voici les cotes du moment.")
    body = fmt.combo(f"🎟️ COTES DU MOMENT {hhmm}" if fair.is_noted(row) else f"🔄 COMBI SUR MESURE À JOUR {hhmm}", new, total, prob, reasons={c["key"]: CB.leg_reason(c) for c in new},
                     stake_txt=_stake_txt(row), coupon=code, when=hhmm, retry=RETRY_BUTTON)
    return chat.with_played_button(Html(fmt.esc(head) + ("\n" + "\n".join(changes) if changes else "") + "\n\n" + body),
                                   f"v{row['tier']}", day, refresh=str(row["tier"]), lookup=True, code=code)


def refresh_real(what, day, now=None, fetch=None):
    """Bouton « 🎟️ Vérifier ce combiné » sous un combiné réel du matin (The Odds API, remis aux cotes 1xBet : `what` = mot du palier, comme
    « sur ») ou sous le pari du jour (`what` = « pari ») : ses matchs sont relus EN DIRECT chez 1xBet (xprice.live_picks, mêmes règles de
    fraîcheur que les autres vérifications). Les paris ne sont pas remplacés (un combiné réel est suivi par ses paris) : la réponse donne
    les cotes du moment, signale un pari qui n'est plus chez 1xBet (le combiné n'est alors plus jouable tel quel) ou qui a perdu son
    avantage, et un NOUVEAU code coupon. Les cotes et le code du moment sont retenus dans la ligne du pari (« xnow », lu par « Mes
    paris »). `fetch` : injectable pour les tests."""
    now = now or now_utc()
    day = str(day)[:10]
    t = played.find(what, day)
    if isinstance(t, str):
        return t
    if t is None or t.item.get("date") != day or not (t.item.get("xlegs") or t.item.get("xodds")):
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    x = t.item
    today = store.load("today.json", {})
    by_key = {c["key"]: c for c in today.get("picks") or []}
    legs = [by_key.get(k) for k in (x.get("keys") or [x["key"]])]
    if today.get("day") != day or not all(legs):
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    if _started(legs, now):
        return "Ce combiné est en jeu : un match a commencé, Kelly ne peut plus le revérifier."
    if _all_soon(legs, now):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes.")
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False, silent=True)
    res, hhmm = xprice.live_picks(legs, now, fetch)
    if all(st == "stale" for _, st, _ in res):
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    gone, new, changes = _real_changes(res)
    label = "combiné" if x.get("keys") else "pari du jour"
    if gone:                                        # un pari n'existe plus chez 1xBet : pas de nouveau code, le combiné n'est plus jouable tel quel
        x["xnow"] = {"at": hhmm, "dead": True}
        t.save()
        return Html(fmt.esc(f"🔄 Ce {label} vérifié chez 1xBet à {hhmm} : il n'est plus jouable tel quel.") + "\n" + "\n".join(changes)
                    + "\n\n" + fmt.i("Un pari n'est plus proposé par 1xBet. Demande un « combi » sur mesure, ou touche « 🔄 Mettre à jour "
                                      "tous les paris »."))
    total, prob = math.prod(c["odds"] for c in new), CB.combo_prob(new)
    code = _coupon(new)
    x["xnow"] = {"at": hhmm, "odds": round(total, 3)} | ({"coupon": code} if code else {})
    t.save()
    head = f"🔄 Ce {label} remis aux cotes 1xBet de {hhmm}" + ("." if changes else " : aucun changement.")
    if x.get("keys"):
        body = fmt.combo(f"🔄 {fmt.tier_name(x['tier']).upper()} À JOUR {hhmm}", new, total, prob,
                         reasons={c["key"]: CB.leg_reason(c) for c in new}, stake_txt=_stake_txt(x), coupon=code, when=hhmm, retry=RETRY_BUTTON)
    else:
        body = fmt.single(new[0], CB.leg_reason(new[0]), title=f"🔄 PARI DU JOUR À JOUR {hhmm}", stake_txt=_stake_txt(x), coupon=code,
                          when=hhmm, retry=RETRY_BUTTON)
    return chat.with_played_button(Html(fmt.esc(head) + ("\n" + "\n".join(changes) if changes else "") + "\n\n" + body),
                                   what, day, xrefresh=True, lookup=True, code=code)


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
    now = now or now_utc()
    if prefix or bold:                              # paris stats que 1xBet vient d'ouvrir (quelques heures avant les matchs)
        pronos = _open_stats(pronos, prefix, now)
    if avoid:                                       # « 🔀 Un autre sans mes matchs » : ni tes matchs joués ni ceux du combiné affiché
        pronos, picks = [c for c in pronos if c["id"] not in avoid], [c for c in picks if c["id"] not in avoid]
    # tes tickets joués du jour ne retirent rien (les meilleurs paris restent proposés) : le message dit seulement lesquels y sont déjà (_shared_counts)
    left = (f"🔀 Sans {'le match' if len(avoid) == 1 else f'les {len(avoid)} matchs'} de tes tickets du jour et du combiné précédent."
            if avoid else "")
    if bold:
        return _bold_combo(picks, pronos, target, day, kinds, now, usage=day_usage(day), note=left, nlegs=nlegs)
    real = "Pas de cotes réelles aujourd'hui" if not started else "Plus de cotes réelles jouables aujourd'hui (matchs commencés)"
    reach = []                                      # cotes les plus hautes atteignables (réelles, puis cote mini des pronostics)
    xp, xk = any(c.get("xodds") for c in pronos), any(c.get("xodds") for c in picks)
    # un combiné proposé (pas les réponses « impossible » ni les cotes de référence) part avec « ✅ Je le joue » (kelly/custom.py)
    split = _kinds_split(picks, pronos, kinds) if prefix else ""
    note = "\n".join(x for x in (split, left) if x)
    keep = {"day": day, "target": target, "lo": target, "hi": target * (1 + C.CUSTOM_TOLERANCE)} | ({"kinds": list(kinds)} if prefix else {}) \
        | ({"note": note} if note else {}) | ({"nlegs": [n_max, exact]} if n_max else {})
    if xp:                                          # cotes 1xBet du jour connues : le combiné se joue chez 1xBet, à SES cotes
        used = day_usage(day)
        fc = CB.fair_custom(pronos, target, usage=used, prefix=prefix, **lim)
        if fc:
            return _live_reply(fc, target, target * (1 + C.CUSTOM_TOLERANCE), keep=keep, usage=used, avoid=avoid)
    if not (xp or xk):                              # aucune cote 1xBet : d'abord les pronostics de Kelly (cote mini), jamais un prix inventé
        fc = CB.fair_custom(pronos, target, usage=day_usage(day), prefix=prefix, **lim)
        if fc:
            why = real if not picks else "Kelly n'a vu aucune cote de 1xBet aujourd'hui"
            return _fair_reply(fc, f"{why} : voici un combiné bâti sur les pronostics de Kelly (cote mini d'au moins {t}), à vérifier "
                                   "chez 1xBet." + _note(keep), budget=True, keep=keep,
                               shared=_shared_counts(day, fc[0]))
    if picks and (xk or not xp):                    # paris réels : à leur prix 1xBet s'il existe, sinon cotes de référence (annoncées)
        usage, n_sent = _todays_usage(day, picks)
        combo = CB.custom(picks, target, usage, prefix=prefix, **lim)
        pool = [c for c in CB.make_pool(picks) if not prefix or c["market"].startswith(prefix)]
        if combo:
            picks_, odds_, prob = combo
            if money.active():
                st = _budget_stake(prob, odds_, "combo", "🎯", legs=picks_)
                txt = _stake_words(st, prob, odds_, picks_)
            else:
                st, txt = None, None                # pas de budget du jour : aucune mise conseillée (le message dit de le régler)
            xcombo = all(c.get("xref") and c.get("xodds") for c in picks_)
            code = xbet.save_coupon(picks_) if xcombo else None
            msg = fmt.combo("🎯 COMBINÉ SUR MESURE" + (f" · {only.upper()}" if only else ""), picks_, odds_, prob, reasons={c["key"]: CB.leg_reason(c) for c in picks_}, stake_txt=txt,
                            coupon=code, when=when, retry="redemande le combi" if xcombo and not code else None, shared=_shared_counts(day, picks_))
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
        filled = _bold_combo(picks, pronos, target, day, kinds, now, usage=day_usage(day), note=note, nlegs=nlegs, fill=True)
        if filled:
            return filled
    if n_max and not avoid:                         # nombre de paris choisi : la vraie raison en une phrase, et les solutions en boutons
        return _legs_failure(picks, pronos, target, kinds, prefix, n_max, exact)
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
    closed = _not_open(pronos, prefix, now)
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
    bold_max = max((_reach(legs) for legs in _bold_pools(picks, pronos, prefix)), default=1.0)
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


STAT_PREFIX = C.STATS_WINDOW_PREFIX
_OPEN_READ = {}                     # id du match -> instant de sa dernière relecture par « combi » (le bot tourne en continu : rythme de statswindow.retry_min)


def _open_stats(pronos, prefix, now):
    """« combi N » avec des types stats (tirs, tirs cadrés, fautes, touches, hors-jeu ; tous si `prefix` est None) : 1xBet n'ouvre ces paris que
    quelques heures avant les matchs, le relevé du matin les a presque tous vus fermés. Les matchs qui commencent dans la fenêtre des stats
    (statswindow.in_window) et dont un pronostic de ces types n'a pas de cote 1xBet sont relus EN DIRECT (xprice.live_check, C.COMBI_OPEN_MAX au
    plus, les plus proches d'abord, jamais au-delà du plafond mensuel de la fenêtre). -> les pronostics à venir, relus ou non. Ne lève jamais."""
    pre = tuple(p for p in (prefix or STAT_PREFIX) if p in STAT_PREFIX)
    if not pre or not C.XBET_ENABLED:
        return pronos
    ids, clock = [], time.time()
    # seulement les pronostics SOLIDES en attente (_pending), de matchs connus de 1xBet, pas relus depuis le rythme du passage horaire (retry_min)
    for c in sorted(_pending(pronos, now), key=lambda c: c.get("commence") or ""):
        last = _OPEN_READ.get(c["id"])
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
        _OPEN_READ.update({i: clock for i in got})      # horodatés APRÈS la lecture (matchs réellement relus) : un échec laisse retenter plus tôt
        return upcoming(fresh, now)
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même, avec les cotes déjà connues
        print(f"[1xbet] relecture des stats pour « combi » impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return pronos


def _not_open(pronos, prefix, now):
    """Ligne « 1xBet n'a pas encore ouvert les paris tirs de 71 matchs… redemande vers 12h30 (21 matchs à 14h00) » d'un « combi » qui échoue ;
    '' si tous les matchs à pronostic stats de ces types ont leur cote (ou si aucun type stats n'est demandé)."""
    pre = tuple(p for p in (prefix or STAT_PREFIX) if p in STAT_PREFIX)
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
    ts = {id(c): _leg_time(c) for c in legs}
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
        if (underpaid(c) and c.get("prob", 0) >= C.COMBI_FILL_MIN_P and c["prob"] * c["xodds"] - 1 >= C.COMBI_FILL_MIN_EV
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
    mine, real = _bold_pools(picks, pronos, prefix, **thr)
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
    old = [c for c in (first[0] if first else []) if (_leg_time(c) or 0) < time.time() - C.COMBI_BOLD_FRESH_MIN * 60]
    if old:                                         # cotes de plus de C.COMBI_BOLD_FRESH_MIN min : relues en direct avant de répondre
        try:
            telegram.send(f"⏳ Je revérifie {len(old)} cote{'s' if len(old) > 1 else ''} chez 1xBet…", queue=False, silent=True)
            pronos = upcoming(xprice.live_check({c["id"] for c in old}, now, fresh_min=C.COMBI_BOLD_FRESH_MIN)[0], now)
            mine, _ = _bold_pools(picks, pronos, prefix, **thr)
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
        marks = {"weak": {c["key"] for c in weak}, "extra": {c["key"]: "mal payé" for c in weak if underpaid(c)}, "shared": _shared_counts(day, combo[0])}
        foot = (f"\n🎯 Paris choisis : {only}" if only else "") + (f"\n{note}" if note else "")
        when, stale = _when_note(combo[0])
        when = when or store.load("today.json", {}).get("xbet_at")
        foot += stale
        if src == "pronos":
            return _fair_reply(combo, "", title=title, when=when, retry="redemande le combi", budget=True, keep=keep, tail=foot, note=head, **marks)
        st = _budget_stake(combo[2], combo[1], "combo", "🎯", legs=combo[0]) if money.active() else None
        code = xbet.save_coupon(combo[0]) if all(c.get("xref") for c in combo[0]) else None
        msg = Html(fmt.combo(title + (f" · {only.upper()}" if only else ""), combo[0], combo[1], combo[2],
                             reasons={c["key"]: (CB.leg_reason(c) or "proche") for c in combo[0]}, stake_txt=_stake_words(st, combo[2], combo[1], combo[0]), coupon=code,
                             when=when, note=head, **marks) + ("\n" + fmt.esc(foot.strip("\n")) if foot.strip() else ""))
        cid = custom.remember(day, target, lo, hi, combo, st, money.cur() if st is not None else None, code, when, src="picks",
                              kinds=kinds if prefix else None, nlegs=nlegs)
        return custom.with_button(msg, cid, day, code)
    if fill:
        return None
    best = max(_reach(mine), _reach(real))
    closed = _not_open(pronos, prefix, now)
    return (f"❌ Pas de combiné à la cote {t}{' (' + only + ')' if only else ''}, même avec des paris plus risqués."
            + (f"\n📉 Au plus {fmt.trim(best, 1)}." if best > 1 else "") + (f"\n{closed}" if closed else ""))


def _legs_failure(picks, pronos, target, kinds, prefix, n_max, exact):
    """« combi 10 max 3 » impossible : les bons paris du jour ont de petites cotes, en N paris on plafonne à X. Boutons : la plus haute cote en N
    paris, sans limite de paris, et des paris plus risqués en N paris (seulement s'ils atteignent la cote)."""
    t, lab = fmt.trim(target, 1), chat.legs_label(n_max, exact)
    codes = ".".join(k for k in CB.KIND_CODES if k in set(kinds or ()))
    if exact == "min":                              # au moins N : il manque des matchs, ou N paris dépassent déjà la cote
        return _min_legs_failure(picks, pronos, target, prefix, n_max, codes)
    words = " ".join(chat.KIND_WORD[k] for k in CB.KIND_CODES if k in set(kinds or ()))
    keep = lambda legs: [c for c in legs if not prefix or c["market"].startswith(prefix)]       # noqa: E731
    xp, xk = any(c.get("xodds") for c in pronos), any(c.get("xodds") for c in picks)
    # un combiné est bâti sur les pronostics OU sur les paris réels, jamais les deux : le plafond se calcule réserve par réserve, comme custom_combo
    pools = [keep(CB.fair_legs(pronos))] + ([keep(_todays_usage(str(now_utc().date()), picks)[0].filter(CB.make_pool(picks)))] if picks and (xk or not xp) else [])
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
    bold = CB.reach([c for legs in _bold_pools(picks, pronos, prefix) for c in legs], n_max)
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
        bold = {c["id"] for legs in _bold_pools(picks, pronos, prefix) for c in legs}
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
    return custom_combo(upcoming(today.get("picks") or []), upcoming(today.get("pronos") or []), target or item["target"], day,
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


LEGS_CHOICES = (3, 5, 8)           # « Nombre de paris : Libre · ≤ 3 · ≤ 5 · ≤ 8 » dans les cases à cocher


def kinds_picker(target, sel=(), n=None):
    """Cases à cocher des types de paris d'un combiné à la cote `target` (après « 🎯 Un combiné » -> cote) : « ✅ Tout » puis les types jouables
    aujourd'hui avec leur nombre de matchs (combos.kinds_available), et « 🎯 Construire le combiné ». Rien de coché = tous les paris.
    Boutons « ck|<cote>|<codes> » (cocher / décocher : les boutons du message sont refaits) et « cgo|<cote>|<codes> » (64 octets au plus)."""
    today = store.load("today.json", {})
    picks, pronos = upcoming(today.get("picks") or []), upcoming(today.get("pronos") or [])
    avail = CB.kinds_available(picks, pronos)
    wait = {}                                       # pronostics stats solides pas encore ouverts chez 1xBet : relus en construisant (_open_stats)
    for c in _pending(pronos):
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
                + [(("✅ " if num == k and not at_least else "") + f"≤ {k}", f"ck|{t}|{codes}|{k}") for k in LEGS_CHOICES])
    if num and (at_least or num not in LEGS_CHOICES):    # un nombre écrit (« ✏️ Autre ») : au moins / au plus, au choix
        rows.append([(("✅ " if at_least else "") + f"≥ {num} (au moins)", f"ck|{t}|{codes}|{num}+"),
                     (("✅ " if not at_least else "") + f"≤ {num} (au plus)", f"ck|{t}|{codes}|{num}")])
    rows.append([("✏️ Autre nombre de paris", f"cn|{t}|{codes}")])
    rows.append([("🎯 Construire le combiné", f"cgo|{t}|{codes}|{n}")])
    text = (f"🎯 Combiné à la cote {t} : quels paris ?\n" + fmt.i("Rien de coché = tout. (N) = matchs avec un bon pari"
                                                                    + (" · ⏳ pas encore ouverts chez 1xBet" if wait else "") + "."))
    if not avail and not wait:
        text += "\n" + fmt.i("Aucun bon pari jouable pour l'instant.")
    return text, telegram.inline(rows)


def _flush_outbox_locked():
    """Renvoie les messages en attente sous le verrou des données (le matin / le relevé horaire écrivent outbox.json aussi). Verrou pris :
    on laisse tomber, ce passage les renvoie lui-même au début. Ne lève pas."""
    if not store.load(telegram.OUTBOX, []):
        return
    try:
        with store.data_lock(wait=0):
            telegram.flush_outbox()
    except store.LockTimeout:
        pass


def _waiting(fn):
    """`fn` (commande ou bouton Telegram) pendant laquelle l'utilisateur attend : un code coupon refusé par 1xBet puis réessayé lui est
    annoncé (xbet.coupon_notice)."""
    def run(*args):
        with xbet.coupon_notice(lambda text: telegram.send(text, queue=False, silent=True)):
            return fn(*args)
    return run


def run_commands(serve=False):
    """Une passe (--commands, sans verrou : à lancer hors du matin) ou le serveur (--serve) : chaque message / bouton est traité sous
    le verrou des données (store.data_lock, le même fichier que `flock` dans les unités systemd), relâché entre deux ; les messages
    en attente sont renvoyés au démarrage puis toutes les minutes tant qu'il en reste. Serveur : un message reçu pendant un passage est mis
    de côté (telegram.poll defer=True) et traité dès la fin, sans bloquer les suivants ; « état » répond tout de suite."""
    if serve:
        print("Bot à l'écoute (Ctrl+C pour arrêter)...")
        try:                                        # sans attendre : un démarrage pendant le matin (~30 min) laissait le bot muet jusqu'à la fin
            with store.data_lock(wait=0):
                migrate.ensure()                    # sous le verrou : un passage du matin n'écrit pas les mêmes documents en même temps
        except store.LockTimeout:
            print("[migration] verrou occupé (un passage la fait déjà) : bot à l'écoute tout de suite", file=sys.stderr)
        telegram.set_commands()                     # menu « / » de Telegram (une fois par démarrage)
        last_flush = float("-inf")
        while True:
            try:
                if time.monotonic() - last_flush >= 60:
                    last_flush = time.monotonic()
                    _flush_outbox_locked()
                telegram.poll(_waiting(handle_command), timeout=10, on_callback=_waiting(chat.on_callback), defer=True, quick=chat.quick)   # court : réponse rapide après une coupure
            except Exception as e:                  # noqa: BLE001 - le serveur ne s'arrête pas sur une erreur
                print(f"[serve] {e}", file=sys.stderr)
                time.sleep(2)
    else:
        migrate.ensure()
        n = telegram.poll(_waiting(handle_command), on_callback=_waiting(chat.on_callback))
        print(f"{n} message(s) traité(s).")


# ---------- diagnostic : python bot.py --check ----------

STATS_STATUS_URL = "https://v3.football.api-sports.io/status"


def _check_odds():
    """-> (ok, lignes). N'utilise que /sports et /events (gratuits) : aucun crédit dépensé."""
    try:
        sports = odds.get("/sports")
    except odds.OddsError as e:
        if e.code == "NO_KEY":
            hint = "ajoute le secret ODDS_API_KEY (GitHub > Settings > Secrets and variables > Actions)"
        elif e.code == "OUT_OF_USAGE_CREDITS":
            hint = "quota du mois épuisé : attends le renouvellement ou passe à un forfait payant"
        elif e.status in (401, 403):
            hint = "clé refusée : recopie la clé de the-odds-api.com dans le secret ODDS_API_KEY (sans espace ni guillemets)"
        elif e.status == 429:
            hint = "trop de requêtes d'un coup : relance dans une minute"
        elif e.status is None:
            hint = "the-odds-api.com injoignable (réseau, proxy ou panne)"
        else:
            hint = "voir le message ci-dessus"
        return False, [f"❌ The Odds API : {e}", f"   → {hint}"]
    rem, used = odds.CREDITS["remaining"], odds.CREDITS["used"]
    lines = ["✅ The Odds API : clé valide",
             f"   Crédits : {rem if rem is not None else '?'} restants, {used if used is not None else '?'} utilisés"]
    chosen = odds.select_sports(sports, limit=False)
    lines.append(f"   Ligues suivies en saison : {len(chosen)} sur {len(C.LEAGUE_KEYS)} du catalogue (au plus {C.MAX_SPORTS} payées "
                 "par jour) : " + (", ".join(s["key"] for s in chosen[:8]) + (" …" if len(chosen) > 8 else "") or "aucune"))
    active = {s["key"] for s in sports}
    absent = [p for p in C.SPORTS if not p.endswith("_") and p not in active]
    if absent:
        lines.append(f"   Hors saison ou clé inconnue : {len(absent)} ligue(s) (ex. {', '.join(absent[:4])})")
    if not chosen:
        lines.append("   ⚠️ Aucun sport actif ne correspond à SPORTS : Kelly n'aura aucun match.")
    lines.append(f"   Allocation du jour : {budget.allowance()} crédits (matin {budget.share('daily')}, clôture "
                 f"{budget.share('closing')}), {budget.days_left()} jour(s) avant la remise à zéro "
                 f"(jour {C.ODDS_RESET_DAY} du mois ; forfait {C.ODDS_MONTHLY_CREDITS}/mois)")
    scan = odds.scan_today(now_utc().date(), now_utc(), sports=chosen)      # gratuit, relu du cache du matin si possible
    counts = {k: len(v) for k, v in scan["leagues"].items()}
    cov = odds.covered_leagues(scan)
    playing = [f"{k} ({counts[k]})" for k in catalogue.prioritise(counts, C.SPORTS, cov)]
    lines.append("   Ligues avec matchs aujourd'hui (gratuit) : " + (", ".join(playing) or "aucune"))
    if playing:
        lines.append("   Payées ce matin dans cet ordre (région, ligues majeures, nombre de matchs) : "
                     + ", ".join(catalogue.prioritise(counts, C.SPORTS, cov)[:C.MAX_SPORTS]))
    if rem is not None and rem < C.MIN_CREDITS:
        lines.append(f"   ⚠️ Sous le seuil MIN_CREDITS={C.MIN_CREDITS} : le matin ne dépense plus, la réserve sert à clôturer.")
    return True, lines


def _check_stats():
    """-> (ok|None si ignoré, lignes)."""
    key = os.environ.get("STATS_API_KEY")
    if not key:
        return None, ["⏭️ API-Football : STATS_API_KEY absente (facultatif : sans elle, corners/cartons/stats joueurs "
                      "restent « non réglés »)"]
    rapid = "" if re.fullmatch(r"[0-9a-fA-F]{32}", key.strip()) else (
        " Ta clé n'a pas le format api-sports (32 caractères hexadécimaux) : une clé RapidAPI ne marche pas sur "
        "v3.football.api-sports.io, prends celle de dashboard.api-football.com.")
    try:
        r = requests.get(STATS_STATUS_URL, headers={"x-apisports-key": key.strip()}, timeout=20)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return False, [f"❌ API-Football : {type(e).__name__} (api-sports.io injoignable ou réponse illisible)",
                       "   → vérifie le réseau, puis la clé STATS_API_KEY." + rapid]
    errors = body.get("errors") if isinstance(body, dict) else None
    resp = body.get("response") if isinstance(body, dict) else None
    if errors or not isinstance(resp, dict) or not resp:
        msg = "; ".join(f"{k}: {v}" for k, v in errors.items()) if isinstance(errors, dict) else (
            "; ".join(map(str, errors)) if errors else f"HTTP {r.status_code}")
        return False, [f"❌ API-Football : {msg[:200]}",
                       "   → clé invalide ou compte non activé : copie la clé du tableau de bord api-sports (en-tête "
                       "x-apisports-key)." + rapid]
    sub, req = resp.get("subscription") or {}, resp.get("requests") or {}
    lines = [f"✅ API-Football : plan {sub.get('plan', '?')}{'' if sub.get('active', True) else ' (INACTIF)'}, "
             f"requêtes aujourd'hui {req.get('current', '?')}/{req.get('limit_day', '?')} (Kelly s'arrête à {stats._cap()})"]
    if sub.get("active") is False:
        lines.append("   → abonnement inactif : réactive-le sur dashboard.api-football.com.")
    lines += apistats.check_lines()
    return sub.get("active") is not False, lines


def _check_telegram_bot():
    """-> (ok, lignes, chat_ok)."""
    token, chat = telegram.creds()
    if not token:
        return False, ["❌ Telegram : TELEGRAM_TOKEN absent", "   → crée un bot avec @BotFather et copie son token dans le secret."], False
    try:
        me = telegram.api("getMe") or {}
    except telegram.TelegramError as e:
        hint = ("token refusé : recopie-le depuis @BotFather (format 123456:ABC…, sans espace)"
                if e.code in (401, 404) else "Telegram injoignable ou erreur inattendue")
        return False, [f"❌ Telegram getMe : {e}", f"   → {hint}"], False
    lines = [f"✅ Telegram : bot @{me.get('username', '?')} joignable"]
    if not chat:
        lines += ["❌ Telegram : TELEGRAM_CHAT_ID absent",
                  "   → envoie /start à ton bot puis lis ton chat id via https://api.telegram.org/bot<TOKEN>/getUpdates"]
        return False, lines, False
    return True, lines, True


def _telegram_hint(err):
    e = err.lower()
    if "chat not found" in e:
        return ("le bot ne connaît pas ce chat : ouvre-le dans Telegram et envoie-lui /start d'abord, puis vérifie "
                "TELEGRAM_CHAT_ID (un nombre, pas un @pseudo)")
    if "blocked" in e or "forbidden" in e:
        return "le bot est bloqué ou exclu du chat : débloque-le / renvoie /start"
    if "unauthorized" in e:
        return "token refusé : recopie-le depuis @BotFather"
    return "voir le message d'erreur Telegram ci-dessus"


def run_check():
    """Vérifie chaque service configuré et affiche un rapport. Ne dépense aucun crédit
    The Odds API (seuls /sports et /events, gratuits). Renvoie True si tout ce qui est configuré marche."""
    print("🔎 KELLY — DIAGNOSTIC")
    from . import configactive
    configactive.refresh()                           # CONFIG_ACTIVE.md : les réglages actifs de cette machine (jamais un secret)
    ok_odds, l_odds = _check_odds()
    ok_stats, l_stats = _check_stats()
    l_src = sources.report().splitlines()           # facultatif : n'influe pas sur le verdict
    l_cache = [cache.line(cache.today_totals())]    # cumul du jour, tous passages confondus
    try:                                            # chiffres techniques de l'apprentissage : ici et dans /bilan détail
        l_learn = learn.describe(learn.load(), track.load()).splitlines()
    except Exception as e:                          # noqa: BLE001
        l_learn = [f"(apprentissage illisible : {type(e).__name__})"]
    ok_bot, l_bot, chat_ok = _check_telegram_bot()
    lines = l_odds + l_stats + l_src + l_cache + l_learn + l_bot
    ok_send = False
    if chat_ok:
        summary = "\n".join(lines)
        if telegram.send(telegram.topic("✅ Kelly est en ligne\n\n" + summary, "tech"), silent=True):
            ok_send = True
            lines.append("✅ Telegram : message de test envoyé à ton chat")
        else:
            lines += [f"❌ Telegram sendMessage : {telegram.LAST_ERROR}",
                      f"   → {_telegram_hint(telegram.LAST_ERROR)}"]
    print("\n".join(lines))
    cache.flush()
    ok = ok_odds and ok_stats is not False and ok_bot and ok_send
    print("\n" + ("✅ Tout est prêt." if ok else "⚠️ Au moins un service est mal configuré (voir ❌ ci-dessus)."))
    return ok
