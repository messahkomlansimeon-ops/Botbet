"""Orchestration : journée type, relevés, alertes, commandes."""
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import (apistats, bankroll, bootstrap, budget, cache, catalogue, chat, combos as CB, config as C, fair, fmt, freeclose, learn, monde,
               money, odds, played, settle, sources, stats, store, strength, telegram, track, weekly)
from .sources import fmmatch, http
from .telegram import Html

DISCLAIMER = Html("<i>Probabilités estimées, aucun gain garanti. Les grosses cotes passent très rarement : "
                  "mise petit. 18+</i>")


def now_utc():
    return datetime.now(timezone.utc)


def load_sample(name="sample_events.json"):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "tests", name)) as f:
        return json.load(f)


def bank_line(bets_combos=None):
    """« 💼 Bankroll : 104,3 u (+4,3 depuis le début) » + alerte après une série de défaites."""
    b = bankroll.load()
    lines = [f"💼 Bankroll : {fmt.trim(b['bankroll'], 1)} u ({fmt.signed(b['bankroll'] - b['initial'], '')} depuis le début)"]
    st = bankroll.streak(bets_combos or [])
    if st >= C.STREAK_WARNING:
        lines.append(f"⚠️ {st} combinés perdus d'affilée : lève le pied, réduis les mises ou fais une pause.")
    return "\n".join(lines)


def day_stakes(top, built, built_fair):
    """Mises du jour sur le budget (money.allocate) : ({"pari" | titre du palier | ("fair", cible): mise}, misé, gardé, journée pauvre) ;
    ({}, 0, 0, False) sans budget du jour."""
    if not money.active():
        return {}, 0, 0, False
    keys, items = [], []
    if top:
        keys.append("pari")
        items.append({"kind": "pari", "tier": "pari", "prob": top["prob"], "odds": top["odds"]})
    for title, (_, odds_, prob) in built:
        keys.append(title)
        items.append({"kind": "combo", "tier": title, "prob": prob, "odds": odds_})
    for target, (_, min_total, prob) in built_fair:
        keys.append(("fair", target))
        items.append({"kind": "fair", "tier": min_total, "prob": prob, "odds": min_total})
    stakes, spent, kept, poor = money.allocate(items, detail=True)
    return dict(zip(keys, stakes)), spent, kept, poor


def build_day(picks, day, world=None, pronos=(), why_no_odds=None, fair_combos=(), fair_stakes=None):
    """Messages du jour + enregistrement des paris envoyés. `world` : cache « tour du monde » (monde.build), `pronos` :
    pronostics sans cote (fair.make), `fair_combos` : [(palier, combiné à cote juste | None)] (combos.fair_plan), un message
    chacun, à part des combinés réels et sans mise de bankroll. Un message par combiné ; les paliers impossibles à bâtir avec
    des jambes de qualité sont signalés UNE fois, en une ligne, dans l'en-tête. Toujours au moins un message utile : le tour
    du monde et les pronostics remplacent les combinés les jours sans cotes réelles. Avec un budget du jour (money), les mises sont
    prises dessus ; `fair_stakes` (dict) reçoit celles des combinés à vérifier, pour leur suivi."""
    bank = bankroll.load()["bankroll"]
    quality = CB.make_pool(picks)
    n_events = len({c["id"] for c in picks})
    messages, sent_picks, sent_combos = [], [], []
    top = max(quality, key=lambda c: c["score"]) if quality else None
    rejected = []
    plan = CB.plan(picks, rejected=rejected) if quality else [(t, None) for t, _, _ in C.COMBOS]
    built = [(t, c) for t, c in plan if c]
    missing = [t for t, c in plan if not c and t not in rejected]
    built_fair = [(t, c) for t, c in fair_combos if c]
    stakes, spent, kept, poor = day_stakes(top, built, built_fair)
    cur = money.cur()
    if fair_stakes is not None:
        fair_stakes.update({t: st for (kind, t), st in ((k, v) for k, v in stakes.items() if isinstance(k, tuple)) if st})
    mise = lambda st: (money.money(st) if st else "pas de mise (trop petite pour ton budget)") if money.active() else None    # noqa: E731
    if picks:
        n_world = monde.total(world) if world else 0
        seen = (f"{n_world} matchs analysés dans le monde, {n_events} avec des cotes réelles" if n_world
                else f"{n_events} matchs analysés avec des cotes réelles")
        n_c = f"{len(built)} combiné{'s' if len(built) > 1 else ''} aujourd'hui" if built else "aucun combiné aujourd'hui"
        head = [fmt.b(f"🔥 Kelly — {fmt.fr_day(day)}"), "", f"📊 {seen}", f"🎯 {n_c}"]
        if built_fair:
            head.append(f"🎲 {len(built_fair)} combiné{'s' if len(built_fair) > 1 else ''} à vérifier chez 1xBet "
                        + fmt.i("(cotes de Kelly, pas du bookmaker)"))
        head.append(money.budget_line(spent, kept, poor) if money.active() else
                    "💡 " + fmt.i("Écris « budget 5000 » pour avoir tes mises en FCFA, prises sur un budget par jour."))
        if missing or rejected:
            head.append("")
        if missing:
            head.append(fmt.i(fmt.esc(CB.skip_line(missing, n_events, anything=bool(quality)))))
        if rejected:
            head.append(fmt.i(fmt.esc(CB.ev_line(rejected))))
        messages.append(telegram.with_buttons(Html("\n".join(head)), chat.menu()))
    if world:
        messages.append(monde.tour_du_monde(world, len(pronos), why_no_odds))
    if top:
        if money.active():                          # mise 0 : notée telle quelle (stake 0, sans devise) = pas de mise sur le budget
            top["stake"] = stakes.get("pari", 0)
            if top["stake"]:
                top["cur"] = cur
        messages.append(chat.with_played_button(fmt.single(top, CB.leg_reason(top), stake_txt=mise(stakes.get("pari"))), "pari", day,
                                                funded=bool(stakes.get("pari"))))
        sent_picks.append(top)
    for title, combo in built:
        picks_, odds_, prob = combo
        st = stakes.get(title, 0) if money.active() else bankroll.stake(prob, odds_, title, bank)
        messages.append(chat.with_played_button(fmt.combo(title, picks_, odds_, prob, st, {c["key"]: CB.leg_reason(c) for c in picks_},
                                                          stake_txt=mise(st)),
                                                played.norm(fmt.tier_word(title)), day, funded=bool(st)))
        sent_picks += picks_
        sent_combos.append({"tier": title, "keys": [c["key"] for c in picks_], "odds": round(odds_, 2), "prob": round(prob, 5),
                            "stake": st} | ({"cur": cur} if money.active() and st else {}))
    for target, (legs, min_total, prob) in built_fair:
        messages.append(chat.with_played_button(fmt.fair_combo(min_total, legs, prob, CB.fair_stake(min_total),
                                                               stake_txt=mise(stakes.get(("fair", target)))),
                                                f"v{target}", day, funded=bool(stakes.get(("fair", target)))))
    if pronos:
        messages.append(fmt.pronos(pronos, C.FAIR_MAX if not (sent_combos or built_fair) else max(3, C.FAIR_MAX // 2)))
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
    """Gain d'un pari : en devise s'il est misé sur le budget du jour, sinon en unités ; « pas joué » s'il a été retiré."""
    if item.get("skipped"):
        return fmt.i("pas joué")
    return fmt.esc(money.signed(x, item["cur"]) if item.get("cur") else fmt.signed(x))


def combo_result(c, by_key):
    """Bloc de résultat d'un combiné terminé."""
    name = fmt.tier_name(c["tier"]).split(" ", 1)[-1]
    legs = [by_key.get(k) or {} for k in c["keys"]]
    stake = c["played"]["stake"] if c.get("played") else c["stake"]
    if c["result"] == "win":
        odds_ = c.get("odds_final", c["odds"])
        gain = stake * (odds_ - 1)
        if c.get("played"):
            odds_, gain = c["played"]["odds"] * odds_ / c["odds"], bankroll.played_gain(c)
        where =" chez 1xBet" if c.get("played") else ""
        return "\n".join([f"{fmt.b(f'✅ {fmt.esc(name)} gagné — cote {fmt.odds_txt(odds_)}{where}')} · {_signed(gain, c)}"]
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
            + (f", raté : {why}" if why else "") + f" · {_signed(-stake, c)}")


def results_message(bets, rows, settled):
    """Résultats de la veille : un bloc par combiné (et le pari du jour), puis une ligne de bilan. None s'il n'y a rien de neuf."""
    by_key = {r["key"]: r for r in rows}
    single_keys = {(r.get("date"), r["key"]) for r in played.single_rows(rows, bets["combos"])}   # pari du jour (marque r["single"])
    done = [c for c in bets["combos"] if c.get("result") and not c.get("reported")]
    singles = [b for b in settled if b.get("sent") and (b.get("date"), b["key"]) in single_keys
               and b["result"] in ("win", "loss", "unknown")]
    if not done and not singles:
        return None
    blocks = []
    for b in singles:
        if b["result"] == "unknown":
            blocks.append(f"{fmt.b('❔ Pari du jour non vérifiable')} : Kelly n'a pas trouvé les stats de {fmt.esc(b['home'])} – "
                          f"{fmt.esc(b['away'])} (vérifie chez 1xBet)\n{leg_result_line(b)}")
            continue
        icon = "✅" if b["result"] == "win" else "❌"
        blocks.append(f"{fmt.b(f'{icon} Pari du jour — cote {fmt.odds_txt(b['odds'])}')}\n{leg_result_line(b)}")
    for c in done:
        c["reported"] = True
        blocks.append(combo_result(c, by_key))
    played_ = [c for c in done if c["result"] in ("win", "loss")]       # « unknown » (non vérifiable) : hors des comptes
    wins = sum(1 for c in played_ if c["result"] == "win")
    bank = bankroll.load()
    head = f"Hier : {wins} combiné{'s' if wins > 1 else ''} gagné{'s' if wins > 1 else ''} sur {len(played_)}" if played_ else ""
    if money.active():
        n, total, _ = money.result(bets, rows, fair.load_combos(), days=1)
        summary = " · ".join(x for x in (head, f"résultat de tes mises : {money.signed(total)}" if n else "") if x) or "Rien de misé hier"
    else:
        summary = (head + " · " if head else "") + f"bankroll {fmt.trim(bank['bankroll'], 1)} u ({fmt.signed(bank['bankroll'] - bank['initial'], '')})"
    if bankroll.streak(bets["combos"]) >= C.STREAK_WARNING:
        summary += f"\n⚠️ {bankroll.streak(bets['combos'])} combinés perdus d'affilée : lève le pied ou fais une pause."
    return Html(fmt.b("📋 Résultats d'hier") + "\n\n" + "\n\n".join(blocks) + "\n\n" + fmt.i(fmt.esc(summary)))


def morning_report(now):
    """Règle les paris d'hier, met à jour bankroll + apprentissage, renvoie les résultats en mots simples."""
    bets, settled = settle.settle_pending(now)
    if not money.active():                          # budget du jour : le bilan en argent remplace la bankroll
        bankroll.apply_results(bets["combos"])
    store.save("bets.json", bets)
    rows = track.load()
    if not money.active():
        bankroll.apply_results([r for r in rows if r.get("played")], played_only=True)     # pari du jour noté avec /joue
    try:
        n = freeclose.fill(rows, now)               # cote finale gratuite des paris fantômes : l'apprentissage avance sans crédit
        print(f"[apprentissage] {n} cote(s) finale(s) gratuite(s) (football-data, BetExplorer)", file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - facultatif
        print(f"[apprentissage] cotes finales gratuites ignorées : {type(e).__name__}: {e}", file=sys.stderr)
    track.save(rows)
    model = learn.update(rows, bets["combos"])
    msgs = []
    res = results_message(bets, rows, settled)
    if res:
        msgs.append(res)
        store.save("bets.json", bets)
    try:
        msgs += fair_report(now)
        msgs += fair_combos_report(now)
    except Exception as e:                          # noqa: BLE001 - les pronostics sans cote sont facultatifs
        print(f"[fair] règlement ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    d = learn.describe_simple(model, rows)
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


def fair_combos_report(now):
    """Règle les combinés à cote juste (gratuit) et résume les nouveaux résultats : [] s'il n'y a rien de neuf. Ces combinés
    n'ont pas de mise : « gagné » veut dire que toutes les jambes sont passées (à jouer seulement si 1xBet payait les cotes
    minimales)."""
    rows, settled = settle.settle_fair_combos(now)
    if any(c.get("played") and not c.get("counted") for c in settled):
        bankroll.apply_results([c for c in rows if c.get("played")], played_only=True)
        store.save(fair.COMBOS_FILE, rows)
    new = [c for c in settled if c["result"] in ("win", "loss", "unknown") and not c.get("reported")]
    if not new:
        return []
    lines = [fmt.b("🎲 Combinés à vérifier chez 1xBet — résultats")]
    for c in new[:6]:
        legs = c["legs"]
        ok = sum(1 for r in legs if r.get("result") in ("win", "push", "void"))
        icon = {"win": "✅", "loss": "❌"}.get(c["result"], "❔")
        word = {"win": "gagné", "unknown": "non vérifiable (stats introuvables : vérifie chez 1xBet)"}.get(
            c["result"], f"perdu — {ok} sur {len(legs)}")
        lines.append("\n" + fmt.b(f"{icon} Combiné à cote mini {fmt.odds_txt(c['min_odds'])} {word}"))
        lines += [leg_result_line(r) for r in legs if r.get("result")]
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
    sans réseau) et data/fair.json (suivi). Ne casse jamais le run -> (cache, pronostics)."""
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
        pronos = fair.make(scan, picks, day, deadline=time.monotonic() + C.SCAN_SECONDS, africa=africa)
        fair.record(pronos, day)
    except Exception as e:                          # noqa: BLE001
        print(f"[fair] pronostics ignorés : {type(e).__name__}: {e}", file=sys.stderr)
    data = monde.build(scan, day, now, paid=scan.get("paid", []), africa=africa, ahead=ahead)     # sans crédits : technique
    monde.save(data)
    print(f"[monde] {monde.total(data)} matchs, {len(data['leagues'])} ligues, {len(scan.get('paid', []))} payée(s), "
          f"{len(pronos)} pronostic(s) sans cote.", file=sys.stderr)
    return data, pronos


DAILY_FILE = "daily_done.json"
DELIVERY = {"sent": 0, "failed": 0}        # messages du dernier run_daily : envoyés / mis en attente ou refusés (voir daily_entry)


def daily_skip_reason(now):
    """Pourquoi le matin ne doit PAS tourner maintenant (None = il peut). run_daily note dans data/daily_done.json le jour UTC `day`
    (= now.date()) qu'il a terminé. Règle : (1) déjà terminé pour la date du jour -> doublon (rattrapage systemd Persistent au
    démarrage du PC puis timer de 08:00, ou relance manuelle) ; (2) lancé avant C.DAY_END_HOUR h UTC alors que le jour précédent est
    terminé -> son « jour Kelly » court jusqu'à C.DAY_END_HOUR h : ces matchs sont déjà couverts, le vrai passage de 08:00 suivra ;
    (3) lancé avant C.DAY_END_HOUR h sans que la veille ait été faite -> rattrapage de la veille manquée : il tourne et compte pour
    la date du jour, donc le passage de 08:00 qui suit est ignoré par (1). `--force` passe outre."""
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


def run_daily(sample=False):
    now = now_utc()
    scan = None
    DELIVERY.update(sent=0, failed=0)
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
    world, pronos, why, fair_combos = None, [], None, []
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
        world, pronos = world_report(scan, picks, day, now)
        if not scan.get("paid"):
            why = budget.why_not(len(C.FEATURED["soccer"].split(",")), "daily")
            why = "budget de cotes du jour épuisé" if why else None
    if pronos:
        try:
            fair_combos = CB.fair_plan(pronos)
        except Exception as e:                      # noqa: BLE001 - les combinés à cote juste sont facultatifs
            print(f"[fair] combinés à cote juste ignorés : {type(e).__name__}: {e}", file=sys.stderr)
    fair_stakes = {}
    messages, sent_picks, sent_combos = build_day(picks, day, world, pronos, why, fair_combos, fair_stakes)
    track.record(picks, {c["key"] for c in sent_picks}, day, now, single_key=single_key(sent_picks))
    settle.record_combos(sent_combos, day)
    fair.record_combos([(t, c) for t, c in fair_combos if c], day, fair_stakes, money.cur() if fair_stakes else None)
    # /top, /match, /combi relisent ces pronostics : les 40 meilleurs + les autres assez solides pour un combiné à cote juste
    keep = pronos[:40] + [c for c in pronos[40:] if CB.fair_eligible(c)][:40]
    store.save("today.json", {"day": str(day), "picks": picks, "pronos": [{k: c[k] for k in c if k != "score"} for c in keep]})
    if not sample:                                  # avant l'envoi : mises et paris sont écrits, une relance ne les doublerait pas
        store.save(DAILY_FILE, {"day": str(day), "at": now.isoformat()})
    for m in report + messages:
        DELIVERY["sent" if telegram.send(m) else "failed"] += 1
    print(cache.line(), file=sys.stderr)
    if not sample:
        run_track()
    cache.flush()
    return report + messages


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


def run_track(events_for=None, now=None):
    """Passage horaire : cote de clôture des paris ENVOYÉS dont le match démarre bientôt (bulk par ligue
    quand c'est moins cher, sinon un appel par match, seulement les marchés suivis) + alertes de cote.
    Chaque appel payant passe par le budget « closing » ; refusé = sauté (log), jamais d'erreur.
    `events_for(sport_key, ev_id, markets)` : injectable pour les tests (un appel par match, sans budget)."""
    now = now or now_utc()
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
    if alerts:
        telegram.send(alert_text(alerts))
    cache.flush()
    return n, alerts


def fill_match_stats():
    """Passage horaire : quelques pages de match FotMob (touches, hors-jeu, fautes, tirs...) pour les profils d'équipe. Jamais à 08:00 :
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
    « alert » = 0 crédit) : les alertes ne viennent plus que de run_track, sur les matchs relevés."""
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
    alerts = find_alerts(picks, hist)
    if not sample:
        hist.update(odds.snapshot(picks))
        store.save("odds_history.json", hist)
    if not alerts:
        print("Aucun mouvement notable.")
        return []
    text = alert_text(alerts)
    telegram.send(text)
    return [text]


def alert_text(alerts):
    lines = [fmt.b("🚨 La cote chute : de l'argent arrive sur ces paris")]
    for _, c, prev in alerts:
        lines.append(f"\n{fmt.b(fmt.event_name(c))} · {fmt.hhmm(c['start'])} · {fmt.esc(c['league'])}\n"
                     f"{fmt.esc(c['pick'])} : {fmt.num(prev['o'])} ➜ {fmt.b(fmt.num(c['odds']))} "
                     f"({fmt.pct(prev['p'])} ➜ {fmt.pct(c['prob'])} de chances) depuis {fmt.hhmm(prev['t'][11:16])}")
    lines.append("\n" + fmt.i("Une cote qui chute vite = beaucoup de mises sur ce pari. Vérifie les compositions avant de suivre."))
    return Html("\n".join(lines))


# ---------- commandes Telegram ----------

HELP = Html("""<b>🤖 Kelly — mode d'emploi</b>

<b>Les boutons en bas</b>
🔝 <b>Paris du jour</b> : les meilleurs paris encore jouables
🎯 <b>Un combiné</b> : choisis une cote, Kelly le construit
📊 <b>Mes résultats</b> : gagnés, perdus, ta bankroll
🧾 <b>Mes paris</b> : les paris du jour et leur mise
💰 <b>Mon budget</b> : voir ou changer ton budget du jour

<b>Ton budget du jour</b>
Écris une fois <i>budget 5000</i> (ou <i>5 000</i>, <i>5.000</i>) : chaque matin, Kelly le répartit entre ses paris selon sa confiance et affiche la mise de chacun. Les jours pauvres, elle en garde une partie. Change-le quand tu veux.

<b>Tu n'as pas joué un pari ?</b>
Kelly compte que tu joues ce qu'elle propose. Sous le pari, <b>❌ Pas joué</b> le retire de ton bilan, <b>✏️ Autre cote</b> corrige la cote si 1xBet payait autre chose (réponds par exemple <i>5,40</i>, ou <i>5,40 2 000</i> avec ta mise).

<b>Tu peux aussi écrire</b>
• un nom d'équipe : <i>Flamengo</i>
• <i>combi 20</i>, <i>bilan</i>, <i>bilan détail</i>, <i>budget 5000</i>

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
                "elles le sont chaque matin à 08:00).")
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


def analyse(picks, query, pronos=()):
    """Texte de /analyse : analyse approfondie d'un match du jour + ses meilleurs paris avec leur raison."""
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
        return Html(comma(head + "\n" + body) + "\n\n" + fmt.pronos(free, 6))
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
    best = sorted(found, key=lambda c: c["score"], reverse=True)[:6]
    lines += ["", fmt.b("🎯 Meilleurs paris")]
    for c in best:
        why = fmt.why_line(c, CB.leg_reason(c))
        lines.append(f"\n👉 {fmt.esc(c['pick'])} — {fmt.b(fmt.odds_txt(c['odds']))} ({fmt.pct(c['prob'])} de chances)"
                     f"{'' if why else pm_note(c)}"
                     + (f"\n💡 {fmt.i(fmt.esc(why))}" if why else ""))
    return Html(comma("\n".join(lines)))


def upcoming(items, now=None):
    """Paris dont le match n'a pas encore commencé (on ne peut plus jouer les autres)."""
    t = (now or now_utc()).isoformat()[:16]
    return [c for c in items if (c.get("commence") or "9")[:16] > t]


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
        days = next((int(a) for a in args if a.isdigit()), None)
        bets = store.load("bets.json", {"picks": [], "combos": []})
        out = settle.bilan(bets, days, detail=detail)
        money_line = f"💰 Budget du jour : {money.money(money.daily())} (écris « budget 7500 » pour le changer)" if money.active() else None
        if detail:
            d = learn.describe(learn.load(), track.load())
            return out + "\n\n" + (money_line or bankroll.summary(bets["combos"])) + ("\n\n" + d if d else "")
        d = learn.describe_simple(learn.load(), track.load())
        return telegram.with_buttons(Html(out + "\n\n" + fmt.esc(money_line or bank_line(bets["combos"])) + (("\n\n" + d) if d else "")),
                                     telegram.inline([[("🔎 Plus de détails", "bilan|detail")]]))
    if cmd == "/bank":
        b = bankroll.load()
        if args:
            try:
                amount = float(args[0].replace(",", "."))
            except ValueError:
                return "Montant invalide. Exemple : /bank 50000"
            if not math.isfinite(amount) or amount <= 0 or amount > money.MAX_AMOUNT:        # « /bank inf », « /bank -5 »
                return "Montant invalide. Exemple : /bank 50000"
            b = {**b, "bankroll": amount, "initial": amount}
            bankroll.save(b)
            return f"Bankroll réglée à {fmt.trim(amount, 2)} unités. Les mises conseillées s'adaptent dès demain."
        return f"💼 Bankroll : {fmt.trim(b['bankroll'], 1)} unités (départ {fmt.trim(b['initial'], 2)})"
    if cmd == "/stats":
        return Html(comma(fmt.esc(fmt_team(sources.team_profile(" ".join(args)) if args else None, " ".join(args)))))
    if cmd == "/budget":
        return money.command(args)
    if cmd in ("/joue", "/joué", "/jouer"):
        return played.command(args, today.get("day") or now_utc().date())
    if cmd in ("/monde", "/ligues"):
        data = monde.load(monde.kelly_day(now_utc()))
        return monde.fmt_monde(data, " ".join(args)) if cmd == "/monde" else monde.fmt_ligues(data)
    pronos = today.get("pronos") or []
    if not picks and not pronos:
        return "Kelly n'a pas encore analysé la journée (ça arrive à 08:00)."
    if cmd == "/analyse":
        return analyse(picks, " ".join(args), pronos)
    if cmd == "/top":
        live, live_pronos = upcoming(picks), upcoming(pronos)
        if not live:
            return fmt.pronos(live_pronos, 10) if live_pronos else "Tous les matchs du jour ont commencé : rendez-vous demain à 08:00."
        best = sorted(CB.per_event(CB.make_pool(live), k=1), key=lambda c: c["score"], reverse=True)[:10]
        if not best:
            return ("Aucun pari de qualité aujourd'hui : rien qui ait un avantage clair, une vraie raison de statistiques "
                    "ou une forte probabilité. /match <équipe> montre tout ce qui existe pour un match.")
        return Html(fmt.b("🔝 Les meilleurs paris du jour") + "\n\n" + "\n\n".join(
            fmt.leg(k, c, CB.leg_reason(c)) for k, c in enumerate(best, 1)))
    if cmd == "/match":
        if not args:
            return "Exemple : /match PSG"
        found = event_matches(picks, " ".join(args))
        if not found:
            free = event_matches(pronos, " ".join(args))
            if free:
                return fmt.pronos(free, 8)
            return f"Aucun match du jour ne contient « {' '.join(args)} »."
        found = sorted(found, key=lambda c: c["score"], reverse=True)[:8]
        lines = [f"🔎 {fmt.b(fmt.event_name(found[0]))} · {fmt.hhmm(found[0]['start'])} · {fmt.esc(found[0]['league'])}"]
        for c in found:
            why = fmt.why_line(c, CB.leg_reason(c))
            lines.append(f"• {fmt.esc(c['pick'])} — {fmt.b(fmt.odds_txt(c['odds']))} ({fmt.pct(c['prob'])} de chances)"
                         + (f"\n   ↳ {fmt.esc(why)}" if why else ""))
        return Html("\n".join(lines))
    if cmd == "/combi":
        try:
            target = float(args[0].replace(",", "."))
        except (IndexError, ValueError):
            return "Exemple : /combi 30"
        if not math.isfinite(target) or target <= 0:        # « /combi inf », « /combi nan », « /combi -5 »
            return "Exemple : /combi 30"
        live = upcoming(picks)
        return custom_combo(live, upcoming(pronos), target, today.get("day"), started=bool(picks) and not live)
    return "Commande inconnue. /aide pour la liste."


def custom_combo(picks, pronos, target, day, started=False):
    """/combi <cote> : combiné à cotes réelles d'abord ; s'il est impossible, combiné à cote juste (pronostics Kelly, annoncé
    « à vérifier chez 1xBet » et sans mise de bankroll) ; sinon l'explication précise de ce qui manque et une cote atteignable.
    `picks` et `pronos` : matchs pas encore commencés seulement ; `started` : il y en avait d'autres, déjà commencés."""
    t = fmt.trim(target, 1)
    real = "Pas de cotes réelles aujourd'hui" if not started else "Plus de cotes réelles jouables aujourd'hui (matchs commencés)"
    reach = []                                      # cotes les plus hautes atteignables (réelles, puis cote mini des pronostics)
    if picks:
        usage, n_sent = _todays_usage(day, picks)
        combo = CB.custom(picks, target, usage)
        pool = CB.make_pool(picks)
        if combo:
            picks_, odds_, prob = combo
            if money.active():
                st = money.allocate([{"kind": "combo", "tier": "🎯", "prob": prob, "odds": odds_}])[0][0]
                txt = money.money(st) + " (sur ton budget du jour)" if st else "pas de mise (trop petite pour ton budget)"
            else:
                st, txt = bankroll.stake(prob, odds_, "🎯", bankroll.load()["bankroll"]), None
            msg = fmt.combo("🎯 COMBINÉ SUR MESURE", picks_, odds_, prob, st, {c["key"]: CB.leg_reason(c) for c in picks_}, stake_txt=txt)
            if CB.ev(combo) < C.COMBO_MIN_EV:
                msg = Html(msg + "\n\n" + fmt.i(f"⚠️ Espérance négative : à ces cotes, ce combiné rend en moyenne "
                                                f"{fmt.pct(1 - CB.ev(combo))} de moins que la mise. Mise petit, ou vise une autre cote."))
            if n_sent:
                msg = Html(msg + "\n\n" + fmt.i(f"Les paris déjà dans tes combinés du jour ne sont repris que s'ils sont sûrs "
                                                f"({fmt.pct(C.LEG_SAFE_PROB)} de chances ou plus, {C.LEG_REUSE_MAX} combinés au plus)."))
            return msg
        free = usage.filter(pool)
        best = {}
        for c in free:
            best[c["id"]] = max(best.get(c["id"], 1.0), c["odds"])
        if len(best) >= 2:
            reach.append(math.prod(best.values()))
        real = (f"Avec les cotes réelles, les {len(free)} paris de qualité encore jouables (sur {len(pool)}) ne "
                f"permettent pas d'atteindre la cote {t} (les paris déjà dans tes combinés du jour ne sont repris que s'ils sont sûrs)")
    fc = CB.fair_custom(pronos, target)
    if fc:
        legs, min_total, prob = fc
        head = f"{real} : voici un combiné bâti sur les pronostics de Kelly (cote mini d'au moins {t}), à vérifier chez 1xBet."
        return Html(fmt.esc(head) + "\n\n" + fmt.fair_combo(min_total, legs, prob, CB.fair_stake(min_total)))
    sf = CB.fair_shortfall(pronos, target)
    if sf["matches"] >= 2:
        reach.append(sf["best_total"])
    n_cov = apistats.counts()[1] + fmmatch.summary()["usable"]
    first = (f"Impossible de monter un combiné à la cote {t} aujourd'hui.\n• {real}." if picks else
             f"{real} et pas assez de pronostics solides pour un combiné à la cote {t}.")
    fair_txt = (f"• Pronostics Kelly sans cote : {sf['matches']} match{'s' if sf['matches'] > 1 else ''} avec un pari solide "
                f"({fmt.pct(C.FAIR_COMBO_MIN_P)} de chances ou plus) ; en les prenant tous, la cote minimale monte à "
                f"{fmt.trim(sf['best_total'], 1)} seulement, il en faudrait environ {sf['need']} pour viser {t}." if sf["matches"] else
                f"• Pronostics Kelly sans cote : aucun pari assez solide aujourd'hui ({fmt.pct(C.FAIR_COMBO_MIN_P)} de chances ou "
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


def run_commands(serve=False):
    """Une passe (--commands, sans verrou : à lancer hors du matin) ou le serveur (--serve) : chaque message / bouton est traité sous
    le verrou des données (store.data_lock, le même fichier que `flock` dans les unités systemd), relâché entre deux ; les messages
    en attente sont renvoyés au démarrage puis toutes les minutes tant qu'il en reste."""
    if serve:
        print("Bot à l'écoute (Ctrl+C pour arrêter)...")
        lock = lambda on_busy: store.data_lock(wait=C.LOCK_WAIT_MIN * 60, on_busy=on_busy)  # noqa: E731
        last_flush = float("-inf")
        while True:
            try:
                if time.monotonic() - last_flush >= 60:
                    last_flush = time.monotonic()
                    _flush_outbox_locked()
                telegram.poll(handle_command, timeout=30, on_callback=chat.on_callback, lock=lock)
            except Exception as e:                  # noqa: BLE001 - le serveur ne s'arrête pas sur une erreur
                print(f"[serve] {e}", file=sys.stderr)
                time.sleep(5)
    else:
        n = telegram.poll(handle_command, on_callback=chat.on_callback)
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
