"""Orchestration : journée type, relevés, alertes, commandes."""
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import (apistats, bankroll, bootstrap, budget, cache, catalogue, chat, combos as CB, config as C, custom, ensemble, fair, fmt, freeclose, gapbets,
               learn, migrate, monde, money, odds, played, settle, sharpgap, sources, stats, store, strength, telegram, track, weekly, xalerts, xprice,
               xworld)
from .sources import fmmatch, http, xbet
from .telegram import Html

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
    lines = [f"💰 Budget du jour : {money.money(money.daily())} (écris « budget 7500 » pour le changer)" if money.active()
             else f"💡 Pas de budget du jour : Kelly ne donne pas de mise conseillée. {money.NO_BUDGET[0].upper() + money.NO_BUDGET[1:]}."]
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
        items.append({"kind": "pari", "tier": "pari", "prob": top["prob"], "odds": top["odds"]})
    for g in gaps:
        keys.append(("gap", g["key"]))
        items.append(gapbets.stake_item(g))
    for title, (_, odds_, prob) in built:
        keys.append(title)
        items.append({"kind": "combo", "tier": title, "prob": prob, "odds": odds_})
    for target, (_, min_total, prob) in built_fair:
        keys.append(("fair", target))
        items.append({"kind": "fair", "tier": min_total, "prob": prob, "odds": min_total})
    stakes, spent, kept, poor = money.allocate(items, detail=True)
    return dict(zip(keys, stakes)), spent, kept, poor


RETRY_BUTTON = "touche « 🎟️ Vérifier ce combiné »"        # sous un combiné qui devait avoir son code coupon et ne l'a pas eu (fmt.coupon_block)


def build_day(picks, day, world=None, pronos=(), why_no_odds=None, fair_combos=(), fair_stakes=None, odds_time=None, fair_coupons=None, gaps=None,
              gap_out=None):
    """Messages du jour + enregistrement des paris envoyés. `world` : cache « tour du monde » (monde.build), `pronos` :
    pronostics sans cote (fair.make), `fair_combos` : [(palier, combiné à cote juste | None)] (combos.fair_plan), un message
    chacun, à part des combinés réels. Un message par combiné ; les paliers impossibles à bâtir avec
    des jambes de qualité sont signalés UNE fois, en une ligne, dans l'en-tête. Toujours au moins un message utile : le tour
    du monde et les pronostics remplacent les combinés les jours sans cotes réelles. Avec un budget du jour (money), les mises sont
    prises dessus ; `fair_stakes` (dict) reçoit celles des combinés à vérifier, pour leur suivi, `fair_coupons` (dict) leurs codes coupon.
    `odds_time` : heure « HH:MM » du relevé 1xBet, rappelée sous les combinés et les pronostics aux cotes 1xBet. `gaps` : écarts sharp du jour (sharpgap.find) ->
    le message « 💰 1xBet paie plus que le marché » (au plus C.SHARPGAP_MAX_SHOWN, jamais le pari du jour), mises prises sur le budget du jour comme les autres
    paris ; `gap_out` (liste) reçoit [(pari, candidat numéroté, mise, code)] à suivre (track.record_gap, APRÈS track.record)."""
    quality = CB.make_pool(picks)
    n_events = len({c["id"] for c in picks})
    messages, sent_picks, sent_combos = [], [], []
    top = max(quality, key=lambda c: c["score"]) if quality else None
    rejected = []
    plan = CB.plan(picks, rejected=rejected) if quality else [(t, None) for t, _, _ in C.COMBOS]
    built = [(t, c) for t, c in plan if c]
    missing = [t for t, c in plan if not c and t not in rejected]
    built_fair = [(t, c) for t, c in fair_combos if c]
    top_gap = [g for g in (gaps or []) if top and g["key"] == top["key"]]
    gaps = [g for g in (gaps or []) if not (top and g["key"] == top["key"])][:C.SHARPGAP_MAX_SHOWN]      # le pari du jour a déjà son message et son bouton
    if top_gap and gap_out is not None:                  # ... mais il reste un écart sharp : marqué dans le suivi pour mesurer sa clôture contre Pinnacle
        gap_out.append((top, top_gap[0], None, None))
    stakes, spent, kept, poor = day_stakes(top, built, built_fair, gaps)
    cur = money.cur()
    if fair_stakes is not None:
        fair_stakes.update({t: st for (kind, t), st in ((k, v) for k, v in stakes.items() if isinstance(k, tuple) and k[0] == "fair")})   # 0 compris
    mise = lambda st: (money.money(st) if st else "pas de mise (trop petite pour ton budget)") if money.active() else None    # noqa: E731
    if picks:
        n_world = monde.total(world) if world else 0
        seen = (f"{n_world} matchs analysés dans le monde, {n_events} avec des cotes réelles" if n_world
                else f"{n_events} matchs analysés avec des cotes réelles")
        n_c = f"{len(built)} combiné{'s' if len(built) > 1 else ''} aujourd'hui" if built else "aucun combiné aujourd'hui"
        head = [fmt.b(f"🔥 Kelly — {fmt.fr_day(day)}"), "", f"📊 {seen}", f"🎯 {n_c}"]
        if built_fair:
            n_f = f"{len(built_fair)} combiné{'s' if len(built_fair) > 1 else ''}"
            if all(all(c.get("xodds") for c in legs) for _, (legs, _, _) in built_fair):      # tout est aux cotes relevées chez 1xBet
                head.append(f"💎 {n_f} aux cotes 1xBet "
                            + fmt.i("(code coupon à charger dans l'appli)" if all(all(c.get("xref") for c in legs)
                                                                                  for _, (legs, _, _) in built_fair)
                                    else "(cotes relevées chez 1xBet)"))
            else:
                head.append(f"🎲 {n_f} à vérifier chez 1xBet " + fmt.i("(cotes de Kelly, pas du bookmaker)"))
        head.append(money.budget_line(spent, kept, poor) if money.active() else
                    "💡 " + fmt.i("Écris « budget 5000 » (ou touche « 💰 Mon budget ») pour avoir des mises conseillées en FCFA, prises sur ton "
                                  "budget du jour. Seuls les paris que tu notes comptent dans tes résultats."))
        if missing or rejected:
            head.append("")
        if missing:
            head.append(fmt.i(fmt.esc(CB.skip_line(missing, n_events, anything=bool(quality)))))
        if rejected:
            head.append(fmt.i(fmt.esc(CB.ev_line(rejected))))
        messages.append(telegram.with_buttons(Html("\n".join(head)), chat.menu()))
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
        messages.append(chat.with_played_button(fmt.single(top, CB.leg_reason(top), stake_txt=mise(stakes.get("pari")),
                                                           coupon=top.get("coupon"), when=odds_time,
                                                           retry=RETRY_BUTTON if xtop and not top.get("coupon") else None),
                                                "pari", day, stake=stakes.get("pari"), xrefresh=xtop, code=top.get("coupon")))
        sent_picks.append(top)
    for title, combo in built:
        picks_, odds_, prob = combo
        st = stakes.get(title, 0) if money.active() else None      # mise conseillée en FCFA (0 : trop petite pour le budget) ; None sans budget
        xcombo = all(c.get("xref") and c.get("xodds") for c in picks_)
        coupon = xbet.save_coupon(picks_) if xcombo else None      # combiné aux cotes 1xBet
        messages.append(chat.with_played_button(fmt.combo(title, picks_, odds_, prob, st, {c["key"]: CB.leg_reason(c) for c in picks_},
                                                          stake_txt=mise(st), coupon=coupon, when=odds_time,
                                                          retry=RETRY_BUTTON if xcombo and not coupon else None),
                                                played.norm(fmt.tier_word(title)), day, stake=st, xrefresh=xcombo, code=coupon))
        sent_picks += picks_
        sent_combos.append({"tier": title, "keys": [c["key"] for c in picks_], "odds": round(odds_, 2), "prob": round(prob, 5),
                            "stake": st} | ({"cur": cur} if money.active() and st else {})
                           | ({"xlegs": [{"key": c["key"], "xodds": c["xodds"], "xref": c.get("xref")} for c in picks_],
                               "coupon": coupon, "at": odds_time} if all(c.get("xodds") for c in picks_) else {}))      # de quoi refaire le coupon plus tard
    for target, (legs, min_total, prob) in built_fair:
        xfair = all(c.get("xref") and c.get("xodds") for c in legs)
        coupon = xbet.save_coupon(legs) if xfair else None
        if fair_coupons is not None and coupon:
            fair_coupons[target] = coupon
        messages.append(chat.with_played_button(fmt.fair_combo(min_total, legs, prob, CB.fair_stake(min_total),
                                                               stake_txt=mise(stakes.get(("fair", target))),
                                                               title=CB.shots_title(target), coupon=coupon, when=odds_time,
                                                               retry=RETRY_BUTTON if xfair and not coupon else None),
                                                f"v{target}", day, stake=stakes.get(("fair", target)),
                                                refresh=target if all(c.get("xodds") for c in legs) else None, code=coupon))
    if pronos:
        shown = CB.best_per_match(pronos)          # un seul pronostic par match : le meilleur, s'il est viable
        if shown:
            messages.append(fmt.pronos(shown, C.FAIR_MAX if not (sent_combos or built_fair) else max(3, C.FAIR_MAX // 2), when=odds_time))
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
    bets, settled = got if got else (store.load("bets.json", {"picks": [], "combos": []}), [])
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
    return (played.custom_name(c) if played.is_custom(c) else
            "Combiné " + (f"aux cotes 1xBet (cote {fmt.odds_txt(c['min_odds'])})" if played.is_real(c)
                          else f"à cote mini {fmt.odds_txt(c['min_odds'])}"))


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
        if xb:                        # cotes 1xBet réelles : meilleure ligne, ou pronostic écarté si mal payé (les paris réels donnent l'avis sharp)
            xprice.apply(pronos, picks=picks)
        fair.record(pronos, day, shown=CB.best_per_match(pronos))     # suivis : les montrés + d'autres en silence (fiabilité)
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
    run_daily(sample)
    return 1 if DELIVERY["failed"] and not DELIVERY["sent"] else 0


def log_opinions(rows, day, now=None):
    """Journal des avis de l'ensemble (ensemble.record) : le modèle, 1xBet sans marge et le book de référence de chaque pronostic ou pari réel, gardés jusqu'à
    leur règlement (settle.settle_ensemble) pour que les poids apprennent dans TOUTES les familles, décidées par l'ensemble ou non. Ne lève jamais.
    -> nombre de lignes ajoutées."""
    if not ensemble.logging_on():
        return 0
    try:
        n = ensemble.record(rows, str(day), now or now_utc())
        print(f"[ensemble] {n} avis ajouté(s) au journal ({len(rows)} pari(s) examiné(s))", file=sys.stderr)
        return n
    except Exception as e:                          # noqa: BLE001 - le journal est facultatif : le matin part quand même
        print(f"[ensemble] journal des avis ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def morning_gaps(picks, pronos, day, odds_at):
    """Écarts sharp du matin (sharpgap.find) sur les paris réels remis au prix 1xBet : la cote 1xBet dépasse la cote juste d'un book de référence lu il y a moins de
    C.SHARPGAP_MAX_AGE_MIN minutes. [] si l'option est éteinte ou sans paris chiffrés. Ne lève jamais."""
    if not C.SHARPGAP_ENABLED or not picks:
        return []
    try:
        found = sharpgap.find({"day": str(day), "picks": picks, "pronos": pronos, "picks_at": odds_at, "xbet_at": odds_at}, now_utc())
        print(f"[sharp] {len(found)} écart(s) : 1xBet paie plus que la cote juste des books de référence", file=sys.stderr)
        return found
    except Exception as e:                          # noqa: BLE001
        print(f"[sharp] écarts ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        return []


def run_daily(sample=False):
    now = now_utc()
    scan = None
    DELIVERY.update(sent=0, failed=0)
    migrate.ensure()                                # données d'avant le mode « je note en FCFA » (une seule fois)
    telegram.flush_outbox()                         # messages restés en attente (réseau coupé à l'heure prévue) : avant les nouveaux
    cache.reset()
    if sample:
        events = load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        after, report, model = None, [], learn.load()
        now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    else:
        refresh_sources()                           # avant le bilan : le règlement gratuit lit les CSV à jour
        report, model = morning_report(now)
        day, after = now.date(), now
        scan = odds.scan_today(day, after)          # gratuit : toutes les ligues en saison, jouées ou non
        fill_profiles(day, scan)                    # stats mondiales API-Football : avant l'achat des cotes (elles guident les marchés payés)
        events = odds.fetch_events(day, after=now, scan=scan)
        print(f"[budget] {budget.line()}", file=sys.stderr)
    picks = odds.build_picks(events, day, after, history=store.load("odds_history.json", {}), model=model)
    world, pronos, why, fair_combos, odds_at, gaps = None, [], None, [], None, []
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
        xworld.extend_scan(scan, day, now)
        world, pronos = world_report(scan, picks, day, now)
        if C.SOURCES_REFRESH and C.XBET_ENABLED:    # paris réels à la cote 1xBet : après le relevé des pronostics (mêmes matchs en cache)
            try:
                xprice.price_picks(picks, pronos=pronos, model=model)
            except Exception as e:                  # noqa: BLE001 - facultatif : sans cote 1xBet, les cotes typiques restent
                print(f"[1xbet] paris réels ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        odds_at = now_utc().strftime("%H:%M")       # fin du relevé 1xBet : l'heure des cotes des combinés
        log_opinions(list(pronos) + list(picks), day)          # avis de l'ensemble (journal « fantôme » de toutes les familles)
        gaps = morning_gaps(picks, pronos, day, odds_at)
        if not scan.get("paid"):
            why = budget.why_not(len(C.FEATURED["soccer"].split(",")), "daily")
            why = "budget de cotes du jour épuisé" if why else None
    if pronos:
        try:
            fair_combos = fair_plan_all(pronos)
        except Exception as e:                      # noqa: BLE001 - les combinés à cote juste sont facultatifs
            print(f"[fair] combinés à cote juste ignorés : {type(e).__name__}: {e}", file=sys.stderr)
    fair_stakes, fair_coupons, gap_rows = {}, {}, []
    messages, sent_picks, sent_combos = build_day(picks, day, world, pronos, why, fair_combos, fair_stakes, odds_time=odds_at,
                                                  fair_coupons=fair_coupons, gaps=gaps, gap_out=gap_rows)
    track.record(picks, {c["key"] for c in sent_picks}, day, now, single_key=single_key(sent_picks))
    if gap_rows:                                    # 💰 suivis comme les autres paris, APRÈS track.record (le pari du jour garde sa marque)
        try:
            track.record_gap(gap_rows, day, now, read_at=odds_at)
        except Exception as e:                      # noqa: BLE001 - le suivi neuf ne bloque pas le matin
            print(f"[sharp] suivi des écarts ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    settle.record_combos(sent_combos, day)
    fair.record_combos([(t, c) for t, c in fair_combos if c], day, fair_stakes, money.cur() if fair_stakes else None,
                       coupons=fair_coupons, at=odds_at)
    keep = keep_pronos(pronos, fair_combos)         # /top, /match, /combi et le relevé de 15:00 les relisent
    # xbet_at : heure du dernier relevé 1xBet des pronostics (relevés de l'après-midi compris), picks_at : celle des paris réels (le matin) ;
    # affichées avec les cotes 1xBet (/top, /match, /analyse)
    store.save("today.json", {"day": str(day), "picks": picks,
                              "pronos": [{k: c[k] for k in c if k != "score"} | ({"xat": odds_at} if odds_at and c.get("xodds") else {}) for c in keep]}
               | ({"xbet_at": odds_at} if odds_at and any(c.get("xodds") for c in keep) else {})
               | ({"picks_at": odds_at} if odds_at and any(c.get("xodds") for c in picks) else {}))
    deliver_morning(report + messages, day, now, sample)          # le garde-fou « matin fait » n'est écrit qu'APRÈS l'envoi
    print(cache.line(), file=sys.stderr)
    if not sample:
        run_track()
    cache.flush()
    return report + messages


def deliver_morning(messages, day, now, sample=False):
    """Envoie les messages du matin (un échec réseau les met en attente dans data/outbox.json, voir telegram.send), PUIS note le jour dans
    data/daily_done.json (pas pour --sample) : un arrêt en pleine route (SIGTERM, coupure) laisse le matin « pas fait », il pourra être refait au
    lieu de perdre des messages sans que rien ne le dise. Les mises et les paris sont déjà écrits avant l'envoi (une relance ne les double pas)."""
    for m in messages:
        DELIVERY["sent" if telegram.send(m) else "failed"] += 1
    if not sample:
        store.save(DAILY_FILE, {"day": str(day), "at": now.isoformat()})


def fair_plan_all(pronos):
    """Tous les combinés à cote juste du matin (paliers 5 / 12 / 25 puis « tirs ») avec UN SEUL Usage : un match n'est jamais repris dans deux
    combinés au-delà des règles de réutilisation (une jambe risquée une seule fois, sûre LEG_REUSE_MAX fois)."""
    usage = CB.Usage()
    return CB.fair_plan(pronos, usage=usage) + CB.fair_shots_plan(pronos, usage=usage)


def keep_pronos(pronos, fair_combos=()):
    """Pronostics gardés dans today.json (relus par /top, /match, /combi et le relevé de 15:00) : TOUJOURS les paris montrés (un par
    match, CB.best_per_match) et chaque jambe des combinés du jour, puis, jusqu'à la limite, les 40 meilleurs des autres et ceux assez
    solides pour un combiné à cote juste ou trouvés chez 1xBet (relus à 15:00). Dans l'ordre du classement."""
    must = {c["key"] for c in CB.best_per_match(pronos)} | {l["key"] for _, combo in fair_combos if combo for l in combo[0]}
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
        today["announced"] = [_slot(c) for c in CB.best_per_match(upcoming_) if c.get("xodds")]
    got = set()
    first = _priority_ids(today["day"], upcoming_)
    limit = time.monotonic() + seconds if seconds else None

    def run(events, deadline=None, wanted=None):
        events = sorted(events, key=lambda e: e[1]["id"] not in first)        # tri stable : les matchs qui comptent d'abord
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
        new = [c for c in CB.best_per_match(upcoming_) if c.get("xodds") and _slot(c) not in seen]
        today["announced"] += [_slot(c) for c in new]
    if got:
        today["xbet_at"] = now.strftime("%H:%M")    # heure du dernier relevé 1xBet (partiel ou non) ; l'heure de chaque cote est dans c["xat"] (fmt.pronos)
    store.save("today.json", today)                 # mêmes objets : les pronostics à venir portent les nouvelles cotes
    log_opinions(upcoming_, today["day"], now)      # derniers avis de l'ensemble avant le match (journal d'apprentissage)
    if new:
        fair.record(new, today["day"], shown=new)
        telegram.send(fmt.pronos(new, limit=12, title="💎 Nouvelles cotes 1xBet", when=now.strftime("%H:%M"),
                                 intro="Paris devenus jouables avec les cotes 1xBet du moment (marchés ouverts ou cotes montées "
                                       "depuis ce matin). Vérifie la cote avant de valider."))
    n_upd = _update_combos(today["day"], pronos, now, got)
    n_single = _upgrade_single(today, now, got, fetch)          # pari du jour sans cote 1xBet au matin : s'il y est maintenant, avec son code
    xalerts.LAST["gap"] = 0
    xalerts.on_refresh(today, now, got, upcoming_, fetch)       # ⚠️ mes paris en danger, 📈 mieux payé qu'au matin, 💰 1xBet paie plus que le marché (jamais d'erreur)
    LAST_REFRESH.update(matches=len(got), new=len(new), updated=n_upd, single=n_single, gap=xalerts.LAST["gap"])
    print(f"[1xbet] relevé : {len(upcoming_)} pronostic(s) à venir, {len(new)} nouveau(x) 💎 annoncé(s), "
          f"{n_upd} combiné(s) du matin renvoyé(s) à jour, pari du jour {'passé aux cotes 1xBet' if n_single else 'inchangé'}", file=sys.stderr)
    return len(new)


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
                  "tes combinés et les paris 💎 d'abord…", queue=False)
    LAST_REFRESH.update(matches=0, new=0, updated=0, single=0, gap=0)
    run_xbet_refresh(now, fetch, announce=True, live=True, seconds=C.XBET_MANUAL_SECONDS)
    st = LAST_REFRESH
    if not st["matches"]:                           # rien n'a été lu : le délai entre deux relevés n'est pas armé (on peut réessayer tout de suite)
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
    return money.money(st) if st else "pas de mise (trop petite pour ton budget)"


def _record_version(sent, row, rows, combo, hhmm, coupon=None):
    """Nouvelle version envoyée d'un combiné : retenue dans COMBO_UPDATES (base des prochaines vérifications et du bouton « Vérifier ce
    combiné ») et dans sa ligne de suivi fair_combos.json (règlement et bilan comptent la version envoyée en dernier, avec la mise du
    matin ; « Mes paris » y lit ses cotes et son code coupon `coupon`). -> la note « joué à … » de l'ancienne version est-elle tombée ?"""
    legs, total, prob = combo
    sent["tiers"][str(row["tier"])] = {"keys": [c["key"] for c in legs], "total": round(total, 3),
                                        "legs": [fair.combo_leg(c) for c in legs]}
    had = bool(row.get("played"))
    fair.update_combo(row, combo, hhmm, coupon)
    store.save(fair.COMBOS_FILE, rows)
    store.save(COMBO_UPDATES, sent)
    return had and not row.get("played")


DROPPED_NOTE = " Ta note « joué à … » de l'ancienne version est retirée : note la nouvelle (✅ / ✏️) si tu la joues."


def _others_usage(rows, row, day, sent):
    """Usage (CB.Usage) des AUTRES combinés du matin du jour `day` (leur dernière version envoyée) : un remplaçant n'est repris dans `row` que si les
    règles de réutilisation le permettent (une jambe risquée une seule fois). Les combis sur mesure (« combi N ») n'y comptent pas : ils ne sont pas
    soumis à ces règles. Un combiné déclaré mort n'en fait pas partie."""
    usage = CB.Usage()
    for r in rows:
        if r is not row and r.get("date") == day and not played.is_custom(r) and not r.get("dead"):
            usage.add(_base_legs(sent, r))
    return usage


def _ticket_problems(legs, pool, fresh_ids, now):
    """Paris du ticket noté `legs` (la version jouée) que le relevé `pool` (pronostics à jour) ne retrouve plus à un prix jouable : [(jambe, cote 1xBet
    du moment ou None)]. Seuls les matchs relus (`fresh_ids`) sont jugés. « None » : 1xBet ne propose plus le pari ; une cote : il paie moins qu'un pari
    viable (combos.fair_legs). Une ligne qui a seulement changé de côté ou de seuil dans le marché n'est pas un problème (la ligne jouée a pu rester)."""
    viable = {(c["id"], c["market"]) for c in CB.fair_legs(upcoming(pool, now)) if c.get("xodds")}
    out = []
    for l in legs:
        if (fresh_ids is not None and l["id"] not in fresh_ids) or (l["id"], l["market"]) in viable:
            continue
        cur = next((c for c in pool if c["id"] == l["id"] and c["market"] == l["market"] and c.get("xodds")), None)
        out.append((l, cur["xodds"] if cur else None))
    return out


def _ticket_lines(problems):
    """Une ligne par pari du ticket qui ne tient plus : « ❌ Plus chez 1xBet : … » ou « 📉 Moins bien payé : … ». HTML."""
    lines = []
    for l, price in problems:
        who = f"{fmt.esc(l.get('home', '?'))} – {fmt.esc(l.get('away', '?'))} ({fmt.esc(l.get('pick', ''))})"
        lines.append(f"❌ Plus chez 1xBet : {who}" if price is None else
                     f"📉 Moins bien payé : {who} : {fmt.odds_txt(l.get('xodds') or l.get('min_odds') or 0)} ➜ {fmt.b(fmt.odds_txt(price))}")
    return lines


TICKET_ADVICE = ("Ton combiné reste suivi tel que tu l'as noté : ses paris, ta note et son règlement ne changent pas. Vérifie ton ticket dans l'appli 1xBet. "
                 "Pour obtenir une version à jour, touche « ↩️ Annuler » puis « 🎟️ Vérifier ce combiné ».")


def _watch_ticket(sent, row, legs0, pool, now, refreshed, tier):
    """Combiné du matin NOTÉ (fair.is_ticket : le ticket est placé chez 1xBet) au relevé : jamais remplacé, jamais déclaré mort ni annulé. Kelly relit
    ses paris et PRÉVIENT une fois (data/combo_updates.json retient les paris déjà signalés) quand l'un n'est plus chez 1xBet ou paie moins. -> 0
    (rien n'est renvoyé)."""
    probs = _ticket_problems(legs0, pool, refreshed, now)
    keys = sorted(l["key"] for l, _ in probs)
    last = sent["tiers"].get(tier) or {"keys": row.get("keys") or [], "total": row.get("min_odds") or 1.0}
    if keys and keys != last.get("warned"):
        p = row["played"]
        telegram.send(Html(fmt.esc(f"⚠️ Un pari de ton ticket n'est plus chez 1xBet ou est moins bien payé (combiné joué à {fmt.odds_txt(p['odds'])}) :")
                           + "\n" + "\n".join(_ticket_lines(probs)) + "\n\n" + fmt.i(fmt.esc(TICKET_ADVICE))))
        sent["tiers"][tier] = last | {"warned": keys}
    elif not keys and last.get("warned"):
        sent["tiers"][tier] = {k: v for k, v in last.items() if k != "warned"}          # tout est revenu à la normale : une future alerte sera redite
    return 0


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
        if played.is_custom(row):                   # combiné sur mesure : le ticket est placé, ses paris ne sont jamais remplacés tout seuls
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
                telegram.send(f"⚠️ Le combiné du matin (cote {fmt.trim(row.get('min_odds') or lo, 1)}) n'est plus jouable chez 1xBet "
                              "(paris retirés ou devenus mal payés) et Kelly ne trouve pas de remplaçants : ne le joue pas.")
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
                if removed else f"🔄 Un combiné du matin a changé chez 1xBet : le voici aux cotes de {hhmm} ")
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
    combos = store.load("bets.json", {"combos": []})["combos"]
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
    cache.flush()
    return n, alerts


def fill_match_stats():
    """Passage horaire : quelques pages de match FotMob (touches, hors-jeu, fautes, tirs...) pour les profils d'équipe. Jamais au passage du matin :
    la page des ligues, lue le matin, est relue depuis le cache. Ne casse jamais le passage."""
    try:
        rep = fmmatch.fill()
        print("[fmmatch] " + fmmatch.report_line(rep), file=sys.stderr)
        return rep
    except Exception as e:                          # noqa: BLE001 - facultatif
        print(f"[fmmatch] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return None


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
🔝 <b>Paris du jour</b> : les meilleurs paris encore jouables
🎯 <b>Un combiné</b> : choisis une cote, Kelly le construit
📊 <b>Mes résultats</b> : gagnés, perdus, et le résultat en FCFA de tes paris notés
🧾 <b>Mes paris</b> : les paris du jour, leur mise conseillée et ceux que tu as notés
💰 <b>Mon budget</b> : voir ou changer ton budget du jour
🔄 <b>Mettre à jour tous les paris</b> : Kelly relit tout de suite les cotes 1xBet de tous les paris du jour, t'envoie les nouveaux paris 💎, les combinés du matin qui ont changé ou qui passent aux cotes 1xBet, et le pari du jour s'il y est maintenant (sinon : 04:00 et 15:00)
🎟️ <b>Vérifier ce combiné</b> (sous un combiné du matin) : juste avant de jouer, Kelly revérifie ce combiné chez 1xBet, remplace les paris retirés et te donne un nouveau code
🔔 <b>Alertes</b> (envoyées toutes seules, en un message) : 💰 1xBet est en retard sur le marché, joue maintenant · ⚠️ un de tes paris n'est plus jouable · 📈 une cote 1xBet a monté depuis ce matin · ✅ tu as pris mieux que le marché

<b>Ton budget du jour</b>
Écris une fois <i>budget 5000</i> (ou <i>5 000</i>, <i>5.000</i>, <i>5k</i>) : c'est ce que tu es prêt à miser aujourd'hui. Chaque matin, Kelly le répartit entre ses paris selon sa confiance et affiche la <b>mise conseillée</b> de chacun, en FCFA. Les jours pauvres, elle en garde une partie. Change-le n'importe quel jour : il reste tel quel jusqu'à ce que tu le changes. Sans budget, pas de mise conseillée.

<b>Les paris que tu joues</b>
Seuls les paris que tu <b>notes</b> comptent dans tes résultats (tous les paris de Kelly restent suivis et réglés). Sous un pari, <b>✅ Je l'ai joué</b> (avec la mise conseillée) : quand la cote 1xBet du pari est connue (combiné ou pari du jour aux cotes 1xBet), un appui le note tout de suite à cette cote et à la mise conseillée (sans mise conseillée, Kelly ne demande que ta mise : <i>1000</i>) ; sinon réponds la cote obtenue chez 1xBet, par exemple <i>5,40</i>, ou <i>5,40 2 000</i> pour donner aussi ta mise en FCFA. Une fois noté : <b>✏️ Autre cote</b> corrige la cote, <b>↩️ Annuler</b> retire la note. Sous un combiné demandé avec <i>combi 20</i>, <b>✅ Je le joue</b> fait la même chose.

<b>Les codes coupon</b>
Touche un code pour le copier, ou le bouton <b>📋 Copier le code</b> sous un message qui n'en a qu'un, puis colle-le dans l'appli 1xBet (ticket, « Charger le coupon »).

<b>Tu peux aussi écrire</b>
• un nom d'équipe : <i>Flamengo</i>
• <i>combi 20</i>, <i>bilan</i>, <i>bilan détail</i>, <i>mes paris</i>, <i>budget 5000</i>, <i>joué sûr 5,40 2000</i> (ou <i>2k</i>)

<i>Les anciennes commandes marchent toujours : /top, /combi, /bilan, /joue, /match, /analyse, /stats, /monde, /ligues, /bank.</i>""")


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


def deep_analysis(first):
    """Analyse approfondie (modèle de force, contexte, grille de marchés) ou None si indisponible."""
    if not first.get("sport_key", "").startswith("soccer"):
        return None
    try:
        txt = strength.analysis_text(first["home"], first["away"], first["sport_key"])
    except Exception as e:                       # noqa: BLE001 - l'analyse ne doit jamais casser la commande
        print(f"analyse approfondie : {e}", file=sys.stderr)
        return None
    if not txt or "Pas assez de données" in txt[:300]:
        return None
    lines = txt.split("\n")
    return "\n".join(lines[1:] if lines[0].startswith("🔬") else lines)   # l'en-tête est déjà affiché


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
            lines.append(f"• Buts attendus d'après les occasions (xG) : {fmt.b(xg)}")
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
        deep = deep_analysis(free[0])
        head = f"🔬 {fmt.b(fmt.esc(free[0]['event']))}\n{fmt.i(fmt.hhmm(free[0]['start']) + ' · ' + fmt.esc(free[0]['league']))}"
        fl = [] if deep else features_lines(sources.match_features(free[0]["home"], free[0]["away"], free[0]["sport_key"]))
        body = ("\n" + fmt.esc(deep)) if deep else ("\n".join(fl) if fl else "\nPas assez de données pour une analyse approfondie de ce match.")
        return Html(comma(head + "\n" + body) + "\n\n" + fmt.pronos(CB.best_per_match(free), 6, when=when))
    events = list(dict.fromkeys(c["event"] for c in found))
    if len(events) > 1:
        return "Plusieurs matchs correspondent, précise : " + " ; ".join(events[:8])
    first = found[0]
    lines = [f"🔬 {fmt.b(fmt.event_name(first))}", fmt.i(f"{fmt.hhmm(first['start'])} · {fmt.esc(first['league'])}")]
    deep = deep_analysis(first)
    f = {"sources": []} if first["group"] != "soccer" else sources.match_features(first["home"], first["away"], first["sport_key"])
    if deep:
        lines += ["", fmt.esc(deep)]
        if f.get("aps_keys"):                                   # le modèle approfondi n'a que les buts : API-Football complète
            lines += features_lines(f, only=set(f["aps_keys"]), head="Chiffres complémentaires")
    elif f["sources"]:
        lines += features_lines(f)
    else:
        lines.append("Pas de stats réelles pour ce match (équipes inconnues des sources ou sources indisponibles).")
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
    return Html(comma("\n".join(lines)))


def _event_pronos(pronos, found):
    """Pronostics 💎 (cote 1xBet relevée) des matchs de `found` : de quoi montrer leurs prix 1xBet à côté des paris réels."""
    ids = {c["id"] for c in found}
    return CB.best_per_match([c for c in pronos if c["id"] in ids and c.get("xodds")])


def top_reply(live, live_pronos, when=None, picks_when=None):
    """/top : les meilleurs paris réels encore jouables (cotes 1xBet de `picks_when` quand 1xBet les a chiffrés ; sinon cotes de référence
    des autres bookmakers, dites comme telles) ET les pronostics 💎 aux cotes 1xBet (`when`) des autres matchs. Un pari absent de
    1xBet n'est pas montré comme jouable (CB.make_pool)."""
    best = sorted(CB.per_event(CB.make_pool(live), k=1), key=lambda c: c["score"], reverse=True)[:10] if live else []
    shown = CB.best_per_match(live_pronos)
    if best:                                        # avec des paris réels : seulement les 💎 d'autres matchs (jamais deux fois le même match)
        ids = {c["id"] for c in best}
        shown = [c for c in shown if c.get("xodds") and c["id"] not in ids]
    parts = []
    if best:
        priced = all(c.get("xodds") for c in best)
        note = (fmt.i(f"💎 Cotes 1xBet de {fmt.esc(picks_when)} : vérifie qu'elles n'ont pas baissé avant de valider.") if priced and picks_when
                else fmt.i("💎 Cotes relevées chez 1xBet par Kelly : vérifie qu'elles n'ont pas baissé avant de valider.") if priced
                else fmt.i("⚠️ Cotes de référence des autres bookmakers, pas celles de 1xBet : vérifie chaque cote chez 1xBet avant de jouer."))
        parts.append(Html(fmt.b("🔝 Les meilleurs paris du jour") + "\n" + note + "\n\n" + "\n\n".join(
            fmt.leg(k, c, CB.leg_reason(c), price=fmt.price_txt(c)) for k, c in enumerate(best, 1))))
    if shown:
        parts.append(fmt.pronos(shown, 10 if not best else 6, when=when))
    if not parts:
        return ("Aucun pari de qualité aujourd'hui : rien qui ait un avantage clair, une vraie raison de statistiques "
                "ou une forte probabilité. /match <équipe> montre tout ce qui existe pour un match.")
    return parts[0] if len(parts) == 1 else Html("\n\n".join(parts))


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
    combos = [c for c in store.load("bets.json", {"combos": []})["combos"] if c.get("date") == str(day)]
    return CB.Usage.from_sent(combos, picks), len(combos)


def handle_command(text):
    """Répond à une commande Telegram. Hors ligne par construction : aucune requête réseau (cotes payantes ou non,
    API-Football, sources), seulement les données du matin dans data/."""
    with cache.offline():
        cmd, reply = chat.route(text)
        if reply is not None or not cmd:
            return reply
        return _handle_command(cmd)


def _handle_command(text):
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    today = store.load("today.json", {"day": "", "picks": []})
    picks = today["picks"]
    if cmd in ("/aide", "/help", "/start"):
        return telegram.with_buttons(HELP, chat.menu())
    if cmd == "/bilan":
        detail = any(a.lower() in ("détail", "detail", "details", "détails") for a in args)
        nums = [a for a in args if a.isdigit()]
        if nums and (len(nums[0]) > 4 or int(nums[0]) > MAX_BILAN_DAYS):          # « /bilan 800000 » : message clair, pas une erreur de date
            return f"Choisis un nombre de jours raisonnable ({MAX_BILAN_DAYS} au plus), par exemple /bilan 30."
        days = int(nums[0]) if nums else None
        bets = store.load("bets.json", {"picks": [], "combos": []})
        out = settle.bilan(bets, days, detail=detail)
        line = money_line(bets["combos"])
        if detail:
            d = learn.describe(learn.load(), track.load())
            return out + "\n\n" + line + ("\n\n" + d if d else "")
        d = learn.describe_simple(learn.load(), track.load())
        return telegram.with_buttons(Html(out + "\n\n" + fmt.esc(line) + (("\n\n" + d) if d else "")),
                                     telegram.inline([[("🔎 Plus de détails", "bilan|detail")]]))
    if cmd == "/bank":
        return ("💰 Kelly ne suit plus de bankroll en unités : elle travaille en FCFA, avec ton budget du jour (« 💰 Mon budget » ou écris "
                "« budget 5000 ») pour les mises conseillées. Seuls les paris que tu notes comptent dans tes résultats (« 📊 Mes résultats »).")
    if cmd == "/stats":
        return Html(comma(fmt.esc(fmt_team(sources.team_profile(" ".join(args)) if args else None, " ".join(args)))))
    if cmd == "/budget":
        return money.command(args)
    if cmd in ("/joue", "/joué", "/jouer"):
        day = str(today.get("day") or now_utc().date())[:10]
        reply = played.command(args, day)
        if isinstance(reply, str) and reply.startswith(played.NEED_STAKE):      # mise manquante : la réponse « cote mise » suivante la note
            chat.set_pending(args[0], day, None, odds=played.parse_odds_stake(args[1:])[0])      # la cote tapée est retenue : seule la mise manque
        elif getattr(reply, "confirm", None):                                   # cote très éloignée : « oui » la note telle quelle
            chat.set_pending(args[0], day, None, confirm=reply.confirm)
        elif isinstance(reply, str) and reply.startswith(("✅ Noté", "Note annulée")):
            chat.clear_pending_for(args[0], day)
        return reply
    if cmd in ("/monde", "/ligues"):
        # jour Kelly en cours ; après un rattrapage d'avant C.DAY_END_HOUR h (voir daily_skip_reason), today.json et monde.json portent la
        # date UTC du jour : c'est elle qu'il faut lire, sinon /monde ne trouve rien
        data = monde.load(monde.kelly_day(now_utc())) or (monde.load(today["day"]) if _current_day(today, now_utc()) and today.get("day") else None)
        return monde.fmt_monde(data, " ".join(args)) if cmd == "/monde" else monde.fmt_ligues(data)
    pronos = today.get("pronos") or []
    if not picks and not pronos:
        return f"Kelly n'a pas encore analysé la journée (ça arrive à {C.DAY_END_HOUR:02d}h00)."
    when, picks_when = today.get("xbet_at"), today.get("picks_at") or today.get("xbet_at")      # heures des derniers relevés 1xBet
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
                return fmt.pronos(CB.best_per_match(free), 8, when=when)
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
        return Html("\n".join(lines))
    if cmd in ("/cotes", "/actualiser"):
        return manual_refresh()
    if cmd == "/combi":
        target = combi_target(args)
        if target is None:
            return "Exemple : /combi 30"
        if target < MIN_COMBI:                              # « /combi 0,5 » : un combiné de 2 paris au moins a une cote d'au moins 1,01
            return f"Un combiné a toujours une cote supérieure à {fmt.num(MIN_COMBI)}. Essaie par exemple /combi 5 ou /combi 30."
        live = upcoming(picks)
        if not live and not upcoming(pronos) and waiting_note(today):
            return waiting_note(today)
        return custom_combo(live, upcoming(pronos), target, today.get("day"), started=bool(picks) and not live,
                            when=picks_when)
    return "Commande inconnue. /aide pour la liste."


MIN_COMBI = 1.01                         # cote visée minimale de « /combi N »


def combi_target(args):
    """La cote visée de « /combi 20 » / « combi 10 000 » / « /combi 2,5 » (milliers réunis : « 10 000 », « 3 000 »), ou None si illisible (négatif, inf, nan,
    texte). Les mots qui suivent le nombre sont ignorés."""
    v, _ = money.take_amount(money.split_tokens(args))
    return float(v) if v is not None and math.isfinite(v) else None


def _coupon(legs):
    """Code coupon 1xBet d'un combiné dont toutes les jambes ont leur référence et leur cote 1xBet (xbet.save_coupon : ticket sans mise) ;
    None sinon, ou si l'enregistrement échoue (jamais d'erreur)."""
    return xbet.save_coupon(legs) if legs and all(c.get("xref") and c.get("xodds") for c in legs) else None


def _budget_stake(prob, odds, kind="fair", tier=None):
    """Mise (devise) d'un combiné demandé à la main (« combi 20 ») sur le budget du jour ; None sans budget. 0 : trop petite pour le budget.
    `kind` / `tier` : voir money.allocate (combiné à cote juste : sa cote ; combiné de paris réels : « combo » et son palier)."""
    if not money.active():
        return None
    return money.allocate([{"kind": kind, "tier": odds if tier is None else tier, "prob": prob, "odds": odds}])[0][0]


def _stake_words(st):
    """La mise `st` (_budget_stake) en mots pour le message : « 1 500 FCFA (sur ton budget du jour) » ; None sans budget."""
    if st is None:
        return None
    return money.money(st) + " (sur ton budget du jour)" if st else "pas de mise (trop petite pour ton budget)"


def _budget_stake_txt(prob, odds):
    """Mise conseillée d'un combiné demandé à la main (« combi 20 ») sur le budget du jour ; None sans budget (le message dit de le régler)."""
    return _stake_words(_budget_stake(prob, odds))


def _fair_reply(fc, head, changes="", coupon=True, title=None, stake_txt=None, when=None, code=..., retry=None, stale=False, budget=False,
                keep=None):
    """Réponse « combiné à cote juste » de custom_combo : phrase d'introduction, paris remplacés, combiné, et code coupon 1xBet quand
    toutes les jambes ont leur référence 1xBet (_coupon ; échec = pas de code, une ligne le dit avec `retry`). `code` : code déjà
    enregistré (None : échec), sinon calculé ici. `stake_txt` : la mise du budget du jour (combiné du matin renvoyé), `budget` : la
    calculer sur le budget du jour (combiné demandé à la main), `when` : l'heure « HH:MM » des cotes 1xBet, `stale` : cotes du dernier
    relevé (1xBet injoignable), voir fmt.fair_combo. `keep` ({"day", "target", "lo", "hi"} : combiné demandé à la main) : le combiné est
    gardé en mémoire avec sa mise, son code et l'heure de ses cotes (custom.remember) et part avec le bouton « ✅ Je le joue »."""
    legs, min_total, prob = fc
    if code is ...:
        code = _coupon(legs) if coupon else None
    stake = None
    if budget and stake_txt is None:
        stake = _budget_stake(prob, min_total)
        stake_txt = _stake_words(stake)
    expected = coupon and bool(retry) and all(c.get("xref") and c.get("xodds") for c in legs) and not code
    msg = Html(fmt.esc(head) + ("\n" + changes if changes else "") + "\n\n"
               + fmt.fair_combo(min_total, legs, prob, CB.fair_stake(min_total), stake_txt=stake_txt, coupon=code, title=title,
                                when=when, retry=retry if expected else None, stale=stale))
    if keep:                                        # gardé en mémoire avec la mise conseillée du message (budget du jour ; None sans budget)
        cid = custom.remember(keep["day"], keep["target"], keep["lo"], keep["hi"], fc, stake, None if stake is None else money.cur(), code, when)
        return custom.with_button(msg, cid, keep["day"], code)
    return msg


def _live_fair(legs, lo, hi, prefix=None, now=None, check=None, fresh_min=None, usage=None):
    """Combiné `legs` vérifié EN DIRECT chez 1xBet : ses matchs + C.XBET_LIVE_SPARES remplaçants (les meilleurs autres paris 💎 du
    jour) relus à l'instant (xprice.live_check), puis CB.patch_combo dans [lo, hi] (`usage` : réutilisation des matchs par les autres combinés
    du jour). -> (résultat de patch_combo ou None, heure, relu : faux si 1xBet n'a rien rendu). `check` : injectable pour les tests."""
    from . import xprice
    pool = upcoming(store.load("today.json", {}).get("pronos") or [], now)
    mine = {l["id"] for l in legs}
    spares = []
    for c in sorted((c for c in CB.fair_legs(pool) if c["id"] not in mine and (not prefix or c["market"].startswith(prefix))),
                    key=lambda c: -(c.get("xev") or 0) * c["prob"]):
        if c["id"] not in spares:
            spares.append(c["id"])
    pronos, got, hhmm = (check or xprice.live_check)(mine | set(spares[:C.XBET_LIVE_SPARES]), now, fresh_min=fresh_min)
    if not got:
        return None, hhmm, False
    return CB.patch_combo(legs, upcoming(pronos, now), lo, hi, fresh_ids=got, prefix=prefix, usage=usage), hhmm, True


def _live_reply(fc, lo, hi, now=None, check=None, keep=None):
    """combi N aux cotes 1xBet : « ⏳ » tout de suite, puis le combiné vérifié en direct (paris retirés ou mal payés remplacés) et son
    code coupon aux cotes de l'instant. 1xBet injoignable : le combiné aux cotes du dernier relevé, sans code, en le disant. `keep` : voir
    _fair_reply (« ✅ Je le joue » sous le combiné, pas sous les réponses « impossible »)."""
    telegram.send("⏳ Je vérifie les cotes 1xBet en direct…", queue=False)
    try:
        res, hhmm, ok = _live_fair(fc[0], lo, hi, now=now, check=check, fresh_min=C.XBET_COMBI_FRESH_MIN)
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même
        print(f"[1xbet] vérification en direct impossible : {type(e).__name__}: {e}", file=sys.stderr)
        res, hhmm, ok = None, "", False
    if not ok and _all_soon(fc[0], now or now_utc()):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes. Demande plutôt un autre « combi ».")
    if not ok:                                      # cotes du dernier relevé : l'heure réelle de leur relevé (cache), sinon celle du relevé du jour
        t0 = xbet.odds_time([(c.get("xref") or [c.get("xgid")])[0] for c in fc[0]])
        last = datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%H:%M") if t0 else store.load("today.json", {}).get("xbet_at") or ""
        return _fair_reply(fc, f"⚠️ 1xBet ne répond pas en ce moment : combiné aux cotes du dernier relevé (cote "
                               f"{fmt.trim(fc[1], 1)}), sans code coupon. Vérifie chaque cote avant de jouer.", coupon=False,
                           when=last, stale=True, budget=True, keep=keep)
    if res is None:
        return (f"Les cotes 1xBet ont bougé depuis le dernier relevé : plus de combiné à la cote {fmt.trim(lo, 1)} avec des paris "
                "encore jouables. Essaie une autre cote.")
    legs, total, prob, removed, added, changed = res
    # GameId de la cote (sous-match des corners, tirs...) ; cotes de moins de 3 h reprises : l'heure réelle de leur relevé
    t0 = xbet.odds_time([(c.get("xref") or [c.get("xgid")])[0] for c in legs])
    when = datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%H:%M") if t0 else hhmm
    return _fair_reply((legs, total, prob), f"Voici un combiné aux cotes 1xBet de {when} (cote {fmt.trim(total, 1)}).",
                       fmt.combo_changes(removed, added, changed), when=when, retry="redemande le combi", budget=True, keep=keep)


def _tier_range(tier):
    """(cote mini, cote maxi, préfixe de marché ou None) d'un palier de combiné à cote juste (5, 12, 25, tc5, tv5)."""
    for t, lo, hi in C.FAIR_COMBO_TIERS:
        if str(t) == str(tier):
            return lo, hi, None
    for k, prefix, _, lo, hi in C.FAIR_SHOTS_TIERS:
        if str(k) == str(tier):
            return lo, hi, prefix
    return None


def _check_ticket(row, legs, day, tier, now, check=None):
    """« 🎟️ Vérifier ce combiné » sous un combiné du matin NOTÉ joué : ses matchs sont relus EN DIRECT chez 1xBet et Kelly dit si un pari du ticket n'y
    est plus ou paie moins ; la ligne de suivi (paris, note, règlement) ne change JAMAIS. Pour une version à jour : « ↩️ Annuler » la note, puis
    « 🎟️ Vérifier ce combiné ». -> la réponse."""
    telegram.send("⏳ Je vérifie ton ticket chez 1xBet…", queue=False)
    pool, got, hhmm = (check or xprice.live_check)({l["id"] for l in legs}, now, fresh_min=None)
    if not got:
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    probs = _ticket_problems(legs, pool, got, now)
    p = row["played"]
    if probs:
        text = (fmt.esc(f"⚠️ Un pari de ton ticket n'est plus chez 1xBet ou est moins bien payé (combiné joué à {fmt.odds_txt(p['odds'])}, vérifié à {hhmm}) :")
                + "\n" + "\n".join(_ticket_lines(probs)) + "\n\n" + fmt.i(fmt.esc(TICKET_ADVICE)))
    else:
        text = fmt.esc(f"✅ Ton ticket (combiné joué à {fmt.odds_txt(p['odds'])}) est toujours chez 1xBet à {hhmm} : tous ses paris sont encore proposés à un prix "
                       "jouable. Rien à changer.")
    return chat.with_played_button(Html(text), f"v{tier}", day, refresh=tier, lookup=True, code=row.get("coupon"))


def refresh_combo(tier, day, now=None, check=None, fetch=None):
    """Bouton « 🎟️ Vérifier ce combiné » sous un combiné du matin : ses paris relus en direct chez 1xBet, ceux retirés ou devenus mal payés
    remplacés (même fourchette de cote que le palier), nouveau code coupon. Part de la DERNIÈRE version envoyée du combiné (COMBO_UPDATES),
    pas de celle du matin, et refuse si un de ses matchs a commencé (il est en jeu). La nouvelle version devient celle du suivi
    (règlement, bilan) comme dans _update_combos, avec les mêmes boutons de mise. Un combiné du matin NOTÉ joué (le ticket est placé) n'est JAMAIS
    remplacé : ses matchs sont relus et Kelly dit si un de ses paris n'y est plus ou paie moins (_check_ticket) ; pour une version à jour, « ↩️ Annuler »
    d'abord. Combiné sur mesure suivi (« m<n> », kelly/custom.py) : même
    chose dans SA fourchette de cote (celle de « combi N ») ; construit sur des paris réels, il est relu comme un combiné réel
    (_refresh_picks : paris non remplacés). `fetch` : relevé 1xBet de ce dernier cas, injectable pour les tests."""
    now = now or now_utc()
    rows = fair.load_combos()
    row = next((r for r in rows if r["date"] == str(day) and str(r["tier"]) == str(tier)), None)
    mine = bool(row) and played.is_custom(row)
    rng = (row["lo"], row["hi"], None) if mine else _tier_range(tier)
    if not row or not rng:
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    lo, hi, prefix = rng
    sent = _load_updates(str(day))
    base = _base_legs(sent, row)
    if _started(base, now):
        return "Ce combiné est en jeu : un match a commencé, Kelly ne le modifie plus."
    if mine and not played.is_real(row):
        return "Ce combiné est à la cote mini de Kelly, pas aux cotes de 1xBet : il n'y a rien à revérifier, compare chaque cote dans l'appli."
    if mine and row.get("src") == "picks":
        return _refresh_picks(row, rows, sent, str(day), now, fetch)
    if fair.is_ticket(row):                         # noté joué : le ticket est placé, on le relit et on le dit, sans jamais le remplacer
        return _check_ticket(row, base, str(day), str(tier), now, check)
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False)
    res, hhmm, ok = _live_fair(base, lo, hi, prefix, now, check, usage=None if mine else _others_usage(rows, row, str(day), sent))
    if not ok:
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    if res is None:
        return ("Les paris de ce combiné ne sont plus jouables chez 1xBet et Kelly ne trouve pas de remplaçants dans la même "
                "fourchette de cote. Demande plutôt un « combi » sur mesure.")
    legs, total, prob, removed, added, changed = res
    code = _coupon(legs)
    dropped = _record_version(sent, row, rows, (legs, total, prob), hhmm, code)
    head = (f"🔄 {'Combi sur mesure' if mine else 'Combiné du matin'} remis aux cotes 1xBet de {hhmm}"
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
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False)
    res, hhmm = xprice.live_picks(legs, now, fetch)
    if all(st == "stale" for _, st, _ in res):
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    gone, new, changes = _real_changes(res)
    if gone:
        return Html(fmt.esc(f"🔄 Ce combiné vérifié chez 1xBet à {hhmm} : il n'est plus jouable tel quel.") + "\n" + "\n".join(changes)
                    + "\n\n" + fmt.i("Un pari n'est plus proposé par 1xBet. Demande un nouveau « combi » sur mesure, ou touche « 🔄 Mettre à jour "
                                      "tous les paris »."))
    total, prob = math.prod(c["odds"] for c in new), math.prod(c["prob"] for c in new)
    code = _coupon(new)
    dropped = _record_version(sent, row, rows, (new, total, prob), hhmm, code)
    head = f"🔄 Combi sur mesure remis aux cotes 1xBet de {hhmm}" + ("." if changes else " : aucun changement.") + (DROPPED_NOTE if dropped else "")
    body = fmt.combo(f"🔄 COMBI SUR MESURE À JOUR {hhmm}", new, total, prob, reasons={c["key"]: CB.leg_reason(c) for c in new},
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
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False)
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
    total, prob = math.prod(c["odds"] for c in new), math.prod(c["prob"] for c in new)
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


def custom_combo(picks, pronos, target, day, started=False, when=None):
    """/combi <cote> : tout est joué chez 1xBet, donc aux cotes 1xBet. Dès que les cotes 1xBet du jour sont connues, combiné aux cotes
    1xBet (pronostics 💎 ou paris réels remis à leur prix 1xBet, code coupon) ; jamais la cote typique d'un autre bookmaker : si aucun
    combiné n'atteint la cote visée, la réponse donne la plus haute cote atteignable aux cotes 1xBet. Sans AUCUNE cote 1xBet (relevé en
    panne) : combiné à cote juste (pronostics de Kelly, « à vérifier chez 1xBet », cote mini) d'abord, puis combiné aux cotes de référence
    des autres bookmakers, annoncé comme tel et sans code coupon ; sinon l'explication précise de ce qui manque et une cote atteignable.
    `picks` et `pronos` : matchs pas encore commencés seulement ; `started` : il y en avait d'autres, déjà commencés. `when` : heure
    « HH:MM » du relevé 1xBet des paris réels. Un combiné proposé aux cotes de 1xBet (ou à cote mini) part avec le bouton « ✅ Je le joue »
    (kelly/custom.py) : il n'est suivi, réglé et compté dans le bilan qu'après cet appui ; pas de bouton sous les cotes de référence."""
    t = fmt.trim(target, 1)
    real = "Pas de cotes réelles aujourd'hui" if not started else "Plus de cotes réelles jouables aujourd'hui (matchs commencés)"
    reach = []                                      # cotes les plus hautes atteignables (réelles, puis cote mini des pronostics)
    xp, xk = any(c.get("xodds") for c in pronos), any(c.get("xodds") for c in picks)
    # un combiné proposé (pas les réponses « impossible » ni les cotes de référence) part avec « ✅ Je le joue » (kelly/custom.py)
    keep = {"day": day, "target": target, "lo": target, "hi": target * (1 + C.CUSTOM_TOLERANCE)}
    if xp:                                          # cotes 1xBet du jour connues : le combiné se joue chez 1xBet, à SES cotes
        fc = CB.fair_custom(pronos, target)
        if fc:
            return _live_reply(fc, target, target * (1 + C.CUSTOM_TOLERANCE), keep=keep)
    if not (xp or xk):                              # aucune cote 1xBet : d'abord les pronostics de Kelly (cote mini), jamais un prix inventé
        fc = CB.fair_custom(pronos, target)
        if fc:
            why = real if not picks else "Kelly n'a vu aucune cote de 1xBet aujourd'hui"
            return _fair_reply(fc, f"{why} : voici un combiné bâti sur les pronostics de Kelly (cote mini d'au moins {t}), à vérifier "
                                   "chez 1xBet.", budget=True, keep=keep)
    if picks and (xk or not xp):                    # paris réels : à leur prix 1xBet s'il existe, sinon cotes de référence (annoncées)
        usage, n_sent = _todays_usage(day, picks)
        combo = CB.custom(picks, target, usage)
        pool = CB.make_pool(picks)
        if combo:
            picks_, odds_, prob = combo
            if money.active():
                st = _budget_stake(prob, odds_, "combo", "🎯")
                txt = _stake_words(st)
            else:
                st, txt = None, None                # pas de budget du jour : aucune mise conseillée (le message dit de le régler)
            xcombo = all(c.get("xref") and c.get("xodds") for c in picks_)
            code = xbet.save_coupon(picks_) if xcombo else None
            msg = fmt.combo("🎯 COMBINÉ SUR MESURE", picks_, odds_, prob, reasons={c["key"]: CB.leg_reason(c) for c in picks_}, stake_txt=txt,
                            coupon=code, when=when, retry="redemande le combi" if xcombo and not code else None)
            if not xk:                              # ni 1xBet ni code : les cotes affichées sont celles des autres bookmakers
                msg = Html(msg + "\n\n" + fmt.i("⚠️ Ce sont des cotes de référence (pas celles de 1xBet) : vérifie chaque cote chez 1xBet "
                                                "avant de jouer, la cote totale chez lui peut être plus basse."))
            if CB.ev(combo) < C.COMBO_MIN_EV:
                msg = Html(msg + "\n\n" + fmt.i(f"⚠️ Espérance négative : à ces cotes, ce combiné rend en moyenne "
                                                f"{fmt.pct(1 - CB.ev(combo))} de moins que la mise. Mise petit, ou vise une autre cote."))
            if n_sent:
                msg = Html(msg + "\n\n" + fmt.i(f"Les paris déjà dans tes combinés du jour ne sont repris que s'ils sont sûrs "
                                                f"({fmt.pct(C.LEG_SAFE_PROB)} de chances ou plus, {C.LEG_REUSE_MAX} combinés au plus)."))
            if all(c.get("xodds") for c in picks_):        # paris réels remis au prix 1xBet : suivi possible (cotes de référence : non)
                cid = custom.remember(day, target, target * (1 - C.CUSTOM_TOLERANCE), target * (1 + C.CUSTOM_TOLERANCE), combo, st,
                                      money.cur() if st is not None else None, code, when, src="picks")
                msg = custom.with_button(msg, cid, day, code)
            return msg
        free = usage.filter(pool)
        best = {}
        for c in free:
            best[c["id"]] = max(best.get(c["id"], 1.0), c["odds"])
        if len(best) >= 2:
            reach.append(math.prod(best.values()))
        real = (f"Avec les cotes {'1xBet' if xk else 'réelles'}, les {len(free)} paris de qualité encore jouables (sur {len(pool)}) ne "
                f"permettent pas d'atteindre la cote {t}" + ("" if xk else " (cotes de référence des autres bookmakers, pas celles de 1xBet)")
                + " (les paris déjà dans tes combinés du jour ne sont repris que s'ils sont sûrs)")
    elif picks and xp:                              # 1xBet n'a chiffré que les pronostics : les paris réels n'ont pas de prix 1xBet
        real = "Les paris réels du jour n'ont pas de cote chez 1xBet : ils ne sont pas proposés (jamais les cotes d'un autre bookmaker)"
    sf = CB.fair_shortfall(pronos, target)
    if sf["matches"] >= 2:
        reach.append(sf["best_total"])
    n_cov = apistats.counts()[1] + fmmatch.summary()["usable"]
    first = (f"Impossible de monter un combiné à la cote {t} aujourd'hui.\n• {real}." if picks else
             f"{real} et pas assez de pronostics solides pour un combiné à la cote {t}.")
    kind = "Pronostics Kelly aux cotes 1xBet" if xp else "Pronostics Kelly sans cote"
    fair_txt = (f"• {kind} : {sf['matches']} match{'s' if sf['matches'] > 1 else ''} avec un pari solide "
                f"({fmt.pct(C.FAIR_COMBO_MIN_P)} de chances ou plus) ; en les prenant tous, la cote monte à "
                f"{fmt.trim(sf['best_total'], 1)} seulement, il en faudrait environ {sf['need']} pour viser {t}." if sf["matches"] else
                f"• {kind} : aucun pari assez solide aujourd'hui ({fmt.pct(C.FAIR_COMBO_MIN_P)} de chances ou "
                "plus, assez de matchs récents).")
    ok = [x for x in reach if 2.0 <= x < target]
    tip = (f"Réessaie demain, ou demande au plus /combi {fmt.trim(math.floor(max(ok) * 10) / 10, 1)} (le plus haut atteignable aujourd'hui)."
           if ok else "Réessaie demain : aujourd'hui, même une petite cote n'est pas atteignable.")
    return "\n".join([first, fair_txt, f"Kelly accumule ses statistiques mondiales (Brésil, Afrique...) un peu plus chaque jour : {n_cov} équipes "
                       f"déjà couvertes ({C.APISTATS_MIN_N} matchs ou plus). {tip}"])


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
        with xbet.coupon_notice(lambda text: telegram.send(text, queue=False)):
            return fn(*args)
    return run


def run_commands(serve=False):
    """Une passe (--commands, sans verrou : à lancer hors du matin) ou le serveur (--serve) : chaque message / bouton est traité sous
    le verrou des données (store.data_lock, le même fichier que `flock` dans les unités systemd), relâché entre deux ; les messages
    en attente sont renvoyés au démarrage puis toutes les minutes tant qu'il en reste."""
    if serve:
        print("Bot à l'écoute (Ctrl+C pour arrêter)...")
        lock = lambda on_busy: store.data_lock(wait=C.LOCK_WAIT_MIN * 60, on_busy=on_busy)  # noqa: E731
        try:
            with lock(lambda: print("Kelly travaille : attente de la fin du passage...")):
                migrate.ensure()                    # sous le verrou : un passage du matin n'écrit pas les mêmes documents en même temps
        except store.LockTimeout:
            print("[migration] verrou occupé : reportée au prochain démarrage", file=sys.stderr)
        last_flush = float("-inf")
        while True:
            try:
                if time.monotonic() - last_flush >= 60:
                    last_flush = time.monotonic()
                    _flush_outbox_locked()
                telegram.poll(_waiting(handle_command), timeout=10, on_callback=_waiting(chat.on_callback), lock=lock)   # court : réponse rapide après une coupure
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
        if telegram.send("✅ Kelly est en ligne\n\n" + summary):
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
