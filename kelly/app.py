"""Orchestration : journée type, relevés, alertes, commandes."""
import json
import os
from datetime import datetime, timedelta, timezone

from . import bankroll, combos as CB, config as C, learn, odds, settle, stats, store, telegram, track

DISCLAIMER = ("Probabilités estimées, aucun gain garanti. Les grosses cotes passent très rarement : "
              "mise petit. 18+")


def now_utc():
    return datetime.now(timezone.utc)


def load_sample(name="sample_events.json"):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "tests", name)) as f:
        return json.load(f)


def fmt_pick(c, stake_hint=None):
    why = f' ({c["why"]})' if c.get("why") else ""
    return f'• {c["start"]} {c["event"]} [{c["league"]}]\n   → {c["pick"]} @ {c["odds"]:.2f}{why}'


def fmt_combo(title, combo, bank):
    if not combo:
        return f"{title}\nPas assez de matchs aujourd'hui pour ce combiné."
    picks, odds_, prob = combo
    lines = [title] + [fmt_pick(c) for c in picks]
    st = bankroll.stake(prob, odds_, title, bank)
    lines.append(f"💰 {len(picks)} matchs | Cote totale : {odds_:.1f} | Proba estimée : {prob*100:.1f}% | "
                 f"Mise conseillée : {st} u")
    return "\n".join(lines), st


def build_day(picks, day):
    """Messages du jour + enregistrement des paris envoyés."""
    bank = bankroll.load()["bankroll"]
    pool = CB.make_pool(picks)
    soft = sum(1 for c in picks if CB.soft_market(c))
    messages = [f"🔥 KELLY — COMBINÉS DU {day.strftime('%d/%m/%Y')} 🔥\n{len(CB.per_event(picks))} matchs analysés, "
                f"{len(picks)} paris étudiés dont {soft} sur marchés secondaires (corners, cartons, joueurs)"]
    sent_picks, sent_combos = [], []
    if pool:
        top = max(pool, key=lambda c: c["score"])
        messages.append(f'⭐ PARI DU JOUR\n{top["start"]} {top["event"]} [{top["league"]}]\n→ {top["pick"]} @ '
                        f'{top["odds"]:.2f} (proba {top["prob"]*100:.0f}%)'
                        + (f'\nPourquoi : {top["why"]}' if top["why"] else ""))
        sent_picks.append(top)
    for title, lo, hi in C.COMBOS:
        combo = CB.make_combo(pool, lo, hi)
        if combo:
            text, st = fmt_combo(title, combo, bank)
            sent_picks += combo[0]
            sent_combos.append({"tier": title, "keys": [c["key"] for c in combo[0]],
                                "odds": round(combo[1], 2), "prob": round(combo[2], 5), "stake": st})
        else:
            text = fmt_combo(title, None, bank)
        messages.append(text)
    messages.append(DISCLAIMER)
    return messages, sent_picks, sent_combos


def morning_report(now):
    """Règle les paris d'hier, met à jour bankroll + apprentissage, renvoie le bilan."""
    bets, settled = settle.settle_pending(now)
    bankroll.apply_results(bets["combos"])
    store.save("bets.json", bets)
    rows = track.load()
    model = learn.update(rows, bets["combos"])
    msgs = []
    sent_settled = [b for b in settled if b.get("sent")]
    if sent_settled:
        lines = ["📋 RÉSULTATS DES DERNIERS PARIS"]
        for b in sent_settled:
            icon = {"win": "✅", "loss": "❌", "push": "↩️", "void": "⚪"}[b["result"]]
            clv = f' | CLV {b["clv"]*100:+.0f}%' if b.get("clv") is not None else ""
            lines.append(f'{icon} {b["event"]} : {b["pick"]} @ {b["odds"]:.2f}{clv}')
        combos_done = [c for c in bets["combos"] if c.get("result") and not c.get("reported")]
        for c in combos_done:
            c["reported"] = True
            icon = {"win": "🏆", "loss": "❌", "void": "⚪"}[c["result"]]
            lines.append(f'{icon} {c["tier"].split(" (")[0]} (cote {c.get("odds_final", c["odds"]):.1f}, mise {c["stake"]} u)')
        store.save("bets.json", bets)
        msgs += ["\n".join(lines), settle.bilan(bets, rows=rows), bankroll.summary(bets["combos"])]
    d = learn.describe(model, rows)
    if d:
        msgs.append(d)
    return msgs, model


def run_daily(sample=False):
    now = now_utc()
    if sample:
        events = load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        after, report, model = None, [], learn.load()
        now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    else:
        report, model = morning_report(now)
        day, after = now.date(), now
        events = odds.fetch_events(day)
    picks = odds.build_picks(events, day, after, history=store.load("odds_history.json", {}), model=model)
    if not sample:
        picks = stats.enrich(picks, day)
        store.save("odds_history.json", odds.snapshot(picks))
    messages, sent_picks, sent_combos = build_day(picks, day)
    track.record(picks, {c["key"] for c in sent_picks}, day, now)
    settle.record_combos(sent_combos, day)
    store.save("today.json", {"day": str(day), "picks": picks})
    for m in report + messages:
        telegram.send(m)
    if not sample:
        run_track()
    return report + messages


def run_track(events_for=None, now=None):
    """Toutes les heures : cote de clôture des paris dont le match démarre
    bientôt (un appel par événement, seulement ses marchés suivis) + alertes."""
    now = now or now_utc()
    rows = track.load()
    due, missed = track.pending_closing(rows, now)
    track.mark_missed(rows, missed, now)
    by_event = {}
    for r in due:
        by_event.setdefault((r["sport_key"], r["id"]), set()).add(r["market"])
    closing, alerts_src = [], []
    for (sport_key, ev_id), markets in by_event.items():
        try:
            ev = events_for(sport_key, ev_id, markets) if events_for else odds.get(
                f"/sports/{sport_key}/events/{ev_id}/odds", regions="eu", oddsFormat="decimal",
                markets=",".join(sorted(markets)))
        except Exception as e:                      # noqa: BLE001
            print(f"clôture {ev_id} : {e}")
            continue
        if not ev:
            continue
        ev["sport_key"] = sport_key
        day = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).date()
        closing += odds.build_picks([ev], day)
    n = track.close(rows, closing, now)
    track.save(rows)
    hist = store.load("odds_history.json", {})
    alerts = find_alerts(closing, hist)
    hist.update(odds.snapshot(closing))
    store.save("odds_history.json", hist)
    print(f"{n} clôtures relevées, {len(missed)} manquées, {len(alerts)} alertes.")
    if alerts:
        telegram.send(alert_text(alerts))
    return n, alerts


def run_snapshot():
    """Le soir : relevé des cotes du lendemain (pour voir les mouvements le matin)."""
    day = now_utc().date() + timedelta(days=1)
    picks = odds.build_picks(odds.fetch_events(day), day)
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
        rise = (c["prob"] - prev["p"]) / prev["p"] if prev["p"] else 0
        if drop >= C.ALERT_ODDS_DROP or rise >= C.ALERT_PROB_RISE:
            alerts.append((max(drop, rise), c, prev))
    alerts.sort(key=lambda a: -a[0])
    return alerts[:8]


def run_alert(sample=False):
    """En journée : cotes qui chutent = argent des pros. Alerte immédiate."""
    now = now_utc()
    hist = store.load("odds_history.json", {})
    if sample:
        events = load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        picks = odds.build_picks(events, day)
    else:
        day = now.date()
        picks = odds.build_picks(odds.fetch_events(day, props=bool(os.environ.get("ALERT_PROPS"))), day, now)
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
    lines = ["🚨 KELLY — ALERTE COTES : l'argent bouge"]
    for _, c, prev in alerts:
        lines.append(f'• {c["start"]} {c["event"]} [{c["league"]}]\n   → {c["pick"]} : {prev["o"]:.2f} ➜ {c["odds"]:.2f} '
                     f'(proba {prev["p"]*100:.0f}% ➜ {c["prob"]*100:.0f}%) depuis {prev["t"][11:16]} UTC')
    lines.append("Une cote qui chute vite = gros volumes sur ce pari. Vérifie la compo avant de suivre.")
    return "\n".join(lines)


# ---------- commandes Telegram ----------

HELP = """🤖 Kelly — commandes :
/bilan [jours] : résultats et rendement
/match <nom> : paris intéressants sur un match du jour
/combi <cote> : combiné sur mesure à la cote visée
/top : les 10 meilleurs paris du jour
/bank [montant] : voir ou régler la bankroll
/aide : ce message"""


def handle_command(text):
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    today = store.load("today.json", {"day": "", "picks": []})
    picks = today["picks"]
    if cmd in ("/aide", "/help", "/start"):
        return HELP
    if cmd == "/bilan":
        days = int(args[0]) if args and args[0].isdigit() else None
        bets = store.load("bets.json", {"picks": [], "combos": []})
        out = settle.bilan(bets, days) + "\n\n" + bankroll.summary(bets["combos"])
        d = learn.describe(learn.load(), track.load())
        return out + ("\n\n" + d if d else "")
    if cmd == "/bank":
        b = bankroll.load()
        if args:
            try:
                amount = float(args[0].replace(",", "."))
            except ValueError:
                return "Montant invalide. Exemple : /bank 50000"
            b = {"bankroll": amount, "initial": amount}
            bankroll.save(b)
            return f"Bankroll réglée à {amount:.2f} unités. Les mises conseillées s'adaptent dès demain."
        return f"💼 Bankroll : {b['bankroll']:.2f} unités (départ {b['initial']:.2f})"
    if not picks:
        return "Kelly n'a pas encore analysé la journée (ça arrive à 08:00)."
    if cmd == "/top":
        best = sorted(CB.per_event(picks, k=1), key=lambda c: c["score"], reverse=True)[:10]
        return "🔝 TOP 10 DU JOUR\n" + "\n".join(fmt_pick(c) for c in best)
    if cmd == "/match":
        if not args:
            return "Exemple : /match PSG"
        q = " ".join(args).lower()
        found = [c for c in picks if q in c["event"].lower()]
        if not found:
            return f"Aucun match du jour ne contient « {' '.join(args)} »."
        found = sorted(found, key=lambda c: c["score"], reverse=True)[:8]
        return f"🔎 {found[0]['event']} ({found[0]['start']} UTC)\n" + "\n".join(
            f'• {c["pick"]} @ {c["odds"]:.2f} (proba {c["prob"]*100:.0f}%){" — " + c["why"] if c["why"] else ""}'
            for c in found)
    if cmd == "/combi":
        try:
            target = float(args[0].replace(",", "."))
        except (IndexError, ValueError):
            return "Exemple : /combi 30"
        combo = CB.make_combo(CB.make_pool(picks), target, target * 1.6)
        if not combo:
            return f"Impossible de monter un combiné à {target:g} avec les matchs restants."
        text, _ = fmt_combo(f"🎯 COMBINÉ SUR MESURE (cote ~{target:g})", combo, bankroll.load()["bankroll"])
        return text
    return "Commande inconnue. /aide pour la liste."


def run_commands(serve=False):
    if serve:
        print("Bot à l'écoute (Ctrl+C pour arrêter)...")
        while True:
            telegram.poll(handle_command, timeout=30)
    else:
        n = telegram.poll(handle_command)
        print(f"{n} message(s) traité(s).")
