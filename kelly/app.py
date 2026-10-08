"""Orchestration : journée type, relevés, alertes, commandes."""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import bankroll, bootstrap, budget, combos as CB, config as C, learn, odds, settle, sources, stats, store, telegram, track

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
        combo = CB.make_combo(CB.pool_for(title, picks, pool), lo, hi)
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


def refresh_sources():
    """Actualise les stats gratuites (football-data, Understat, FBref) et l'amorçage historique avant de bâtir les
    paris. Téléchargements polis et mis en cache ; ne casse JAMAIS le run (erreur = message dans les logs)."""
    if not C.SOURCES_REFRESH:
        return {}
    out = {}
    try:
        out = sources.refresh_all(C.SPORTS)
        print("[sources] " + ", ".join(f"{k} : {v.get('rows', 0)} lignes" for k, v in out.items()), file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - les stats sont facultatives
        print(f"[sources] actualisation ignorée : {type(e).__name__}: {e}", file=sys.stderr)
    try:
        boot = bootstrap.ensure(C.SPORTS)
        print(f"[sources] amorçage : {len(boot.get('rows', []))} lignes historiques", file=sys.stderr)
    except Exception as e:                          # noqa: BLE001
        print(f"[sources] amorçage ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return out


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


def run_daily(sample=False):
    now = now_utc()
    if sample:
        events = load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        after, report, model = None, [], learn.load()
        now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    else:
        refresh_sources()                           # avant le bilan : le règlement gratuit lit les CSV à jour
        report, model = morning_report(now)
        day, after = now.date(), now
        events = odds.fetch_events(day, after=now)
        print(f"[budget] {budget.line()}", file=sys.stderr)
        if not events:
            report.append("⚠️ Kelly n'a récupéré aucune cote aujourd'hui (aucun match, clé, budget de crédits ou "
                          "saison : lance « python bot.py --check » ou regarde les logs).")
    picks = odds.build_picks(events, day, after, history=store.load("odds_history.json", {}), model=model)
    if not sample:
        try:
            picks = odds.rescore(stats.enrich(picks, day), model)       # modèle appris : après les stats d'avant-match
        except Exception as e:                      # noqa: BLE001 - les stats sont facultatives
            print(f"[stats] ignorées : {e}", file=sys.stderr)
        if picks:
            store.save("odds_history.json", odds.snapshot(picks))
        try:
            report.append(sources_message(picks))
        except Exception as e:                      # noqa: BLE001
            print(f"[sources] rapport ignoré : {e}", file=sys.stderr)
    messages, sent_picks, sent_combos = build_day(picks, day)
    track.record(picks, {c["key"] for c in sent_picks}, day, now)
    settle.record_combos(sent_combos, day)
    store.save("today.json", {"day": str(day), "picks": picks})
    for m in report + messages:
        telegram.send(m)
    if not sample:
        run_track()
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
    rows = track.load()
    due, missed = track.pending_closing(rows, now)
    track.mark_missed(rows, missed, now)
    combos = store.load("bets.json", {"combos": []})["combos"]
    calls = plan_closing(due, combos, bulk=events_for is None)
    if calls and events_for is None and odds.CREDITS["remaining"] is None:
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
    return n, alerts


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
        rise = (c["prob"] - prev["p"]) / prev["p"] if prev["p"] else 0
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
/analyse <match> : chiffres d'avant-match (corners, cartons, tirs, xG) d'un match du jour + ses meilleurs paris
/stats <équipe> : corners, cartons, tirs, fautes et xG des 10 derniers matchs
/bank [montant] : voir ou régler la bankroll
/aide : ce message"""


LEAGUES = {"E0": "Premier League", "SP1": "Liga", "I1": "Serie A", "D1": "Bundesliga", "F1": "Ligue 1",
           "EPL": "Premier League", "La_liga": "Liga", "Serie_A": "Serie A", "Ligue_1": "Ligue 1"}


def _pair(v, k, label, fmt="{:.1f}"):
    f, a = v.get(k + "_for"), v.get(k + "_against")
    return f"{label} {fmt.format(f)} pour / {fmt.format(a)} contre" if f is not None and a is not None else None


def fmt_team(p, query):
    """Texte de /stats : profil d'une équipe (derniers matchs, football-data et Understat)."""
    if not query:
        return "Exemple : /stats Arsenal"
    if not p:
        return (f"Aucune stat pour « {query} » (équipe inconnue ou sources pas encore actualisées : "
                "elles le sont chaque matin à 08:00).")
    if "many" in p:
        return f"Plusieurs équipes correspondent à « {query} » : {', '.join(p['many'])}. Précise le nom."
    lines = [f"📈 {p['team']} ({LEAGUES.get(p['div'], p['div'])})"]
    fd = p.get("fd") or {}
    allv = fd.get("all") or {}
    if allv.get("n"):
        lines.append(f"{allv['n']} derniers matchs (football-data) :")
        lines += [x for x in (_pair(allv, "corners", "• Corners :"), _pair(allv, "cards", "• Cartons (jaunes + rouges) :"),
                              _pair(allv, "shots", "• Tirs :"), _pair(allv, "sot", "• Tirs cadrés :"),
                              _pair(allv, "fouls", "• Fautes :"), _pair(allv, "goals", "• Buts :")) if x]
        for venue, label in (("home", "À domicile"), ("away", "À l'extérieur")):
            v = fd.get(venue) or {}
            bits = [x for x in (_pair(v, "corners", "corners", "{:.1f}"), _pair(v, "cards", "cartons")) if x]
            if v.get("n") and bits:
                lines.append(f"{label} ({v['n']} matchs) : " + " ; ".join(b.replace(" pour / ", "/").replace(" contre", "")
                                                                         for b in bits))
    us = p.get("us") or {}
    if (us.get("all") or {}).get("xg") is not None:
        u = us["all"]
        txt = f"xG (Understat, {u['n']} derniers) : {u['xg']:.2f} pour / {u.get('xga', 0):.2f} contre"
        for venue, label in (("home", "domicile"), ("away", "extérieur")):
            v = us.get(venue) or {}
            if v.get("xg") is not None and v.get("xga") is not None:
                txt += f" ; {label} {v['xg']:.2f}/{v['xga']:.2f}"
        lines.append(txt)
    if len(lines) == 1:
        lines.append("Pas assez de matchs récents dans les sources.")
    if p.get("updated"):
        lines.append(f"(sources actualisées le {p['updated'][8:10]}/{p['updated'][5:7]})")
    return "\n".join(lines)


def pm_note(c):
    pm = c.get("pm") or {}
    return f" [modèle {pm['exp']:.1f} vs ligne {c['point']}]" if pm.get("exp") is not None and c.get("point") is not None else ""


def analyse(picks, query):
    """Texte de /analyse : chiffres d'avant-match d'un match du jour + ses meilleurs paris avec leur « pourquoi »."""
    if not query:
        return "Exemple : /analyse Arsenal"
    found = [c for c in picks if query.lower() in c["event"].lower()]
    events = list(dict.fromkeys(c["event"] for c in found))
    if not found:
        return f"Aucun match du jour ne contient « {query} »."
    if len(events) > 1:
        return "Plusieurs matchs correspondent, précise : " + " ; ".join(events[:8])
    first = found[0]
    lines = [f"🔬 {first['event']} ({first['start']} UTC) [{first['league']}]"]
    f = sources.match_features(first["home"], first["away"], first["sport_key"]) if first["group"] == "soccer" else {"sources": []}
    if f["sources"]:
        n = lambda k: f.get(k)                                  # noqa: E731
        def two(k, label, unit=""):
            t, h, a = n("exp_" + k), n(f"exp_{k}_home"), n(f"exp_{k}_away")
            if t is None and h is None:
                return None
            split = f" ({h:.1f} + {a:.1f})" if h is not None and a is not None else ""
            return f"• {label} : {t if t is not None else h + a:.1f}{unit}{split}"
        lines.append(f"Chiffres d'avant-match ({', '.join(f['sources'])}, {f.get('n_home', '?')} et {f.get('n_away', '?')} matchs) :")
        lines += [x for x in (two("corners", "Corners attendus"), two("cards", "Cartons attendus"),
                              two("fouls", "Fautes attendues"), two("shots", "Tirs attendus"),
                              two("sot", "Tirs cadrés attendus"), two("goals", "Buts attendus (moyennes)")) if x]
        if f.get("exp_xg_total") is not None:
            lines.append(f"• xG attendus : {f['exp_xg_total']:.2f} ({f['exp_xg_home']:.2f} - {f['exp_xg_away']:.2f})")
        if f.get("ref_cards") is not None:
            lines.append(f"• Arbitre {f['referee']} : {f['ref_cards']:.1f} cartons par match")
    else:
        lines.append("Pas de stats réelles pour ce match (équipes inconnues des sources ou sources indisponibles).")
    best = sorted(found, key=lambda c: c["score"], reverse=True)[:6]
    lines.append("Meilleurs paris :")
    lines += [f'• {c["pick"]} @ {c["odds"]:.2f} (proba {c["prob"]*100:.0f}%){pm_note(c)}{" — " + c["why"] if c["why"] else ""}'
              for c in best]
    return "\n".join(lines)


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
    if cmd == "/stats":
        return fmt_team(sources.team_profile(" ".join(args)) if args else None, " ".join(args))
    if not picks:
        return "Kelly n'a pas encore analysé la journée (ça arrive à 08:00)."
    if cmd == "/analyse":
        return analyse(picks, " ".join(args))
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
            try:
                telegram.poll(handle_command, timeout=30)
            except Exception as e:                  # noqa: BLE001 - le serveur ne s'arrête pas sur une erreur
                print(f"[serve] {e}", file=sys.stderr)
                time.sleep(5)
    else:
        n = telegram.poll(handle_command)
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
    lines.append(f"   Sports suivis ({len(chosen)}, au plus {C.MAX_SPORTS} ligues avec matchs payées par jour) : "
                 + (", ".join(s["key"] for s in chosen) or "aucun"))
    active = {s["key"] for s in sports}
    absent = [p for p in C.SPORTS if not p.endswith("_") and p not in active]
    if absent:
        lines.append("   Hors saison ou clé inconnue : " + ", ".join(absent))
    if not chosen:
        lines.append("   ⚠️ Aucun sport actif ne correspond à SPORTS : Kelly n'aura aucun match.")
    lines.append(f"   Allocation du jour : {budget.allowance()} crédits (matin {budget.share('daily')}, clôture "
                 f"{budget.share('closing')}), {budget.days_left()} jour(s) avant la remise à zéro "
                 f"(jour {C.ODDS_RESET_DAY} du mois ; forfait {C.ODDS_MONTHLY_CREDITS}/mois)")
    window, playing = odds.day_window(now_utc().date()), []
    for sp in chosen[:15]:
        try:
            n = len(odds.league_events(sp["key"], window))           # gratuit
        except odds.OddsError:
            continue
        if n:
            playing.append(f"{sp['key']} ({n})")
    lines.append("   Ligues avec matchs aujourd'hui (gratuit) : " + (", ".join(playing) or "aucune"))
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
    ok_bot, l_bot, chat_ok = _check_telegram_bot()
    lines = l_odds + l_stats + l_src + l_bot
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
    ok = ok_odds and ok_stats is not False and ok_bot and ok_send
    print("\n" + ("✅ Tout est prêt." if ok else "⚠️ Au moins un service est mal configuré (voir ❌ ci-dessus)."))
    return ok
