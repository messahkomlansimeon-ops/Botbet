"""Passage horaire (clôtures, alertes, fenêtre des stats, remplissage, relevé du soir) : issu du découpage d'app.py (le code lit app via A.nom)."""

import os
import sys
from . import config as C, budget, cache, cupstats, ensemble, migrate, odds, seasonlane, statswindow, store, telegram, track, xalerts
from .sources import fmmatch
from datetime import datetime, timedelta
from ._appref import A


def run_track(events_for=None, now=None, xfetch=None):
    """Passage horaire : cote de clôture des paris ENVOYÉS dont le match démarre bientôt (bulk par ligue
    quand c'est moins cher, sinon un appel par match, seulement les marchés suivis) + alertes 1xBet (xalerts.on_market : 💰 1xBet en
    retard sur un mouvement du marché, ✅ bonne nouvelle ; 1xBet relu en direct pour 3 matchs au plus).
    Chaque appel payant passe par le budget « closing » ; refusé = sauté (log), jamais d'erreur.
    `events_for(sport_key, ev_id, markets)` : injectable pour les tests (un appel par match, sans budget), `xfetch` : relevé 1xBet des alertes."""
    now = now or A.now_utc()
    migrate.ensure()
    telegram.flush_outbox()                         # messages restés en attente (réseau coupé) : renvoyés avant les alertes du jour
    rows = track.load()
    due, missed = track.pending_closing(rows, now)
    track.mark_missed(rows, missed, now)
    combos = store.load_doc("bets.json", {"combos": []})["combos"]
    calls = A.plan_closing(due, combos, bulk=events_for is None)
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
    alerts = A.find_alerts(closing, hist)
    hist.update(odds.snapshot(closing))
    store.save("odds_history.json", hist)
    print(f"{n} clôtures relevées, {len(missed)} manquées, {skipped} sautées (budget), {len(alerts)} alertes.")
    if calls and events_for is None:
        print(f"[budget] {budget.line()}", file=sys.stderr)
    A._market_alerts(alerts, closing, rows, now, xfetch)
    A.mark_xclose(now)                                # audit : dernier prix 1xBet (cache disque) des lignes du journal dont le match approche
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
    day = A.now_utc().date() + timedelta(days=1)
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
    now = A.now_utc()
    hist = store.load("odds_history.json", {})
    if sample:
        events = A.load_sample()
        day = datetime.fromisoformat(events[0]["commence_time"].replace("Z", "+00:00")).date()
        picks = odds.build_picks(events, day)
    else:
        day = now.date()
        picks = odds.build_picks(odds.fetch_events(day, props=bool(os.environ.get("ALERT_PROPS")), optional=True,
                                                   use="alert", after=now), day, now)
    found = A.find_alerts(picks, hist)
    if not sample:
        hist.update(odds.snapshot(picks))
        store.save("odds_history.json", hist)
    if not found:
        print("Aucun mouvement notable.")
        return []
    msg = A._market_alerts(found, picks, track.load(), now)
    return [msg] if msg else []


def _market_alerts(found, closing, rows, now, fetch=None):
    """Alertes du marché (💰 1xBet en retard, ✅ bonne nouvelle, voir kelly/xalerts.py) pour les mouvements `found` (find_alerts) et les
    cotes de clôture `closing`. Sans le today.json du jour Kelly en cours : pas de référence 1xBet (ni 💰), jour Kelly calculé."""
    today = store.load("today.json", {})
    if not A._current_day(today, now):
        today = {}
    day = today.get("day") or str(now.date() if now.hour >= C.DAY_END_HOUR else now.date() - timedelta(days=1))
    combos = store.load("bets.json", {"combos": []}).get("combos", [])
    return xalerts.on_market(found, closing, today, rows, combos, day, now, fetch)
