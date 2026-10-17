"""Relevés 1xBet à la demande et mises à jour des paris (🔄, combi N, actualisation des combinés et des paris réels) : issus du découpage d'app.py (le code lit app via A.nom)."""

import math
import sys
import time
from . import config as C, combos as CB, chat, fair, fmt, migrate, played, seasonlane, statswindow, store, telegram, track, xalerts, xprice
from .sources import state, xbet
from .telegram import Html
from datetime import datetime, timedelta
from ._appref import A


def run_xbet_refresh(now=None, fetch=None, announce=True, live=False, seconds=None):
    """Relevé 1xBet de la journée (15:00, kelly-xbet.timer, ou à la demande : manual_refresh) : les pronostics du matin
    (data/today.json) dont le match n'a pas commencé sont remis aux cotes 1xBet du moment ; /top et /combi les relisent. Puis :
    - `announce` : la liste des paris 💎 devenus jouables depuis la dernière liste (suivis comme ceux du matin) ;
    - les combinés 💎 du matin sont contrôlés (_update_combos) : renvoyés avec un nouveau code s'ils ont changé.
    Les matchs qui comptent sont relus en premier (jambes des combinés du jour, paris 💎 montrés). `seconds` : durée maximale du
    relevé (relevé manuel). `live` : cotes relues à l'instant (xbet.live_mode). `fetch` : injectable pour les tests. -> nombre de
    nouveaux 💎 annoncés."""
    from .sources import xbet
    now = now or A.now_utc()
    migrate.ensure()
    today = store.load("today.json", {})
    if not A._current_day(today, now) or not today.get("pronos"):
        print(f"[1xbet] pas de pronostics du jour Kelly en cours (today.json : {today.get('day')}) : relevé sauté", file=sys.stderr)
        return 0
    telegram.flush_outbox()
    pronos = today["pronos"]
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    upcoming_ = [c for c in pronos if datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > soon]
    if "announced" not in today:                    # premier relevé du jour : les 💎 du matin sont déjà connus
        today["announced"] = [A._slot(c) for c in CB.shown_pronos(upcoming_) if c.get("xodds")]
    got = set()
    first = A._priority_ids(today["day"], upcoming_)
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
        seen = {A._slot(k) for k in today["announced"]}                        # par match et marché : une ligne changée n'est pas annoncée deux fois
        new = [c for c in CB.shown_pronos(upcoming_) if c.get("xodds") and A._slot(c) not in seen][:12]      # 12 = la limite du message : une jambe non montrée n'est jamais marquée annoncée
        today["announced"] += [A._slot(c) for c in new]
    if got:
        today["xbet_at"] = now.strftime("%H:%M")    # heure du dernier relevé 1xBet (partiel ou non) ; l'heure de chaque cote est dans c["xat"] (fmt.pronos)
    store.save("today.json", today)                 # mêmes objets : les pronostics à venir portent les nouvelles cotes
    A.log_opinions(upcoming_, today["day"], now)      # derniers avis de l'ensemble avant le match (journal d'apprentissage)
    if new:
        fair.record(new, today["day"], shown=new)
        telegram.send(telegram.topic(fmt.pronos(new, limit=12, title="💎 Nouvelles cotes 1xBet", when=now.strftime("%H:%M"),
                                 intro="Paris devenus jouables avec les cotes 1xBet du moment (marchés ouverts ou cotes montées "
                                       "depuis ce matin). Vérifie la cote avant de valider."), "combos"), silent=True)       # annonce : visible, sans sonnerie (niveaux de bruit)
    n_upd = A._update_combos(today["day"], pronos, now, got)
    n_single = A._upgrade_single(today, now, got, fetch)          # pari du jour sans cote 1xBet au matin : s'il y est maintenant, avec son code
    xalerts.LAST["gap"] = 0
    xalerts.on_refresh(today, now, got, upcoming_, fetch)       # ⚠️ mes paris en danger, 📈 mieux payé qu'au matin, 💰 1xBet paie plus que le marché (jamais d'erreur)
    A.LAST_REFRESH.update(matches=len(got), new=len(new), updated=n_upd, single=n_single, gap=xalerts.LAST["gap"])
    print(f"[1xbet] relevé : {len(upcoming_)} pronostic(s) à venir, {len(new)} nouveau(x) 💎 annoncé(s), "
          f"{n_upd} combiné(s) du matin renvoyé(s) à jour, pari du jour {'passé aux cotes 1xBet' if n_single else 'inchangé'}", file=sys.stderr)
    return len(new)


def manual_refresh(now=None, fetch=None):
    """« 🔄 Mettre à jour tous les paris » : le relevé 1xBet du jour, tout de suite et en direct (pronostics, 💎 nouveaux, combinés du
    matin à jour), au plus une fois toutes les C.XBET_MANUAL_GAP minutes (données du proxy) et pendant C.XBET_MANUAL_SECONDS secondes au
    plus (le bot ne répond à rien d'autre pendant ce temps) : les combinés du jour et les paris 💎 montrés d'abord. -> réponse."""
    now = now or A.now_utc()
    today = store.load("today.json", {})
    if not A._current_day(today, now) or not today.get("pronos"):
        return A.waiting_note(today, now) or "Pas encore de pronostics du jour : rien à actualiser."
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
    A.LAST_REFRESH.update(matches=0, new=0, updated=0, single=0, gap=0)
    A.run_xbet_refresh(now, fetch, announce=True, live=True, seconds=C.XBET_MANUAL_SECONDS)
    st = A.LAST_REFRESH
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


def _priority_ids(day, upcoming_):
    """Matchs à relire en premier : les jambes des combinés du jour (dernière version envoyée) et les paris 💎 montrés."""
    ids = {c["id"] for c in CB.best_per_match(upcoming_) if c.get("xodds")}
    sent = A._load_updates(day)
    for row in fair.load_combos():
        if row.get("date") == day:
            ids |= {l["id"] for l in A._base_legs(sent, row)}
    return ids


def _slot(c):
    """« id|marché » d'un pronostic (ou d'une clé : son début) : une ligne qui change dans la journée reste le même pari pour l'annonce."""
    return f"{c['id']}|{c['market']}" if isinstance(c, dict) else "|".join(str(c).split("|")[:2])


def _all_soon(legs, now):
    """Tous les matchs de `legs` commencent dans moins de C.XBET_SOON_MIN min (1xBet les retire de l'avant-match)."""
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    return bool(legs) and all(l.get("commence") and datetime.fromisoformat(l["commence"].replace("Z", "+00:00")) <= soon for l in legs)


def _started(legs, now):
    return any((l.get("commence") or "9")[:16] <= now.isoformat()[:16] for l in legs)


def _base_legs(sent, row):
    """Jambes de la DERNIÈRE version envoyée du combiné `row` (COMBO_UPDATES), sinon celles du matin : c'est d'elles que partent la
    vérification « un match a commencé » et la reconstruction."""
    return (sent["tiers"].get(str(row["tier"])) or {}).get("legs") or row.get("legs") or []


def _load_updates(day):
    sent = store.load(A.COMBO_UPDATES, {})
    return sent if isinstance(sent, dict) and sent.get("day") == day and isinstance(sent.get("tiers"), dict) else {"day": day, "tiers": {}}


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
    sent, rows, n = A._load_updates(day), fair.load_combos(), 0
    hhmm = now.strftime("%H:%M")
    for row in rows:
        if played.is_custom(row) or CB.season_no(row["tier"]) is not None:       # combiné sur mesure : ses paris ne sont jamais remplacés tout seuls ; voie `saison` : relue au passage du matin seulement
            continue
        rng = A._tier_range(row["tier"])
        tier = str(row["tier"])
        last = sent["tiers"].get(tier) or {"keys": row.get("keys") or [], "total": row.get("min_odds") or 1.0}
        legs0 = A._base_legs(sent, row)
        if row["date"] != day or not legs0 or not rng or A._started(legs0, now):
            continue
        if fair.is_ticket(row):                     # noté joué : rapport seulement
            A._watch_ticket(sent, row, legs0, pronos, now, refreshed, tier)
            continue
        usage = A._others_usage(rows, row, day, sent)
        if not all(l.get("xodds") for l in legs0):          # combiné du matin bâti avant les cotes 1xBet : peut-il passer aux cotes 1xBet ?
            n += A._upgrade_combo(sent, row, rows, legs0, rng, tier, pronos, now, refreshed, last, usage)
            continue
        lo, hi, prefix = rng
        res = CB.patch_combo(legs0, A.upcoming(pronos, now), lo, hi, fresh_ids=refreshed, prefix=prefix, usage=usage)
        if res is None:
            if not last.get("dead"):
                telegram.send(telegram.topic(f"⚠️ Le combiné {A._of_day(tier)} (cote {fmt.trim(row.get('min_odds') or lo, 1)}) n'est plus jouable chez 1xBet "
                              "(paris retirés ou devenus mal payés) et Kelly ne trouve pas de remplaçants : ne le joue pas.", "alerts"))
                sent["tiers"][tier] = last | {"dead": True}
                fair.kill_combo(row)                # pas joué : hors du règlement, du bilan et de l'argent
                store.save(fair.COMBOS_FILE, rows)
                store.save(A.COMBO_UPDATES, sent)
            continue
        legs, total, prob, removed, added, changed = res
        keys = [c["key"] for c in legs]
        if (not last.get("dead") and set(keys) == set(last["keys"])
                and abs(total / (last.get("total") or total) - 1) <= C.XBET_UPDATE_MOVE):
            continue
        code = A._coupon(legs)
        dropped = A._record_version(sent, row, rows, (legs, total, prob), hhmm, code)
        head = (f"⚠️ Ton combiné a changé : un pari est retiré ou moins bien payé chez 1xBet. Le voici aux cotes de {hhmm} "
                if removed else f"🔄 Un combiné {A._of_day(tier)} a changé chez 1xBet : le voici aux cotes de {hhmm} ")
        msg = A._fair_reply((legs, total, prob), head + "(l'ancien code est remplacé)." + (A.DROPPED_NOTE if dropped else ""),
                          fmt.combo_changes(removed, added, changed), title=f"🔄 COMBINÉ À JOUR {hhmm}", stake_txt=A._stake_txt(row),
                          when=hhmm, code=code, retry=A.RETRY_BUTTON)
        telegram.send(chat.with_played_button(msg, f"v{tier}", day, refresh=tier, lookup=True, code=code))
        n += 1
    store.save(A.COMBO_UPDATES, sent)
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
    res = CB.patch_combo(legs0, A.upcoming(pronos, now), lo, hi, fresh_ids=refreshed, prefix=prefix, usage=usage)
    if res is None or not all(c.get("xodds") for c in res[0]):
        return 0
    legs, total, prob, removed, added, changed = res
    hhmm = now.strftime("%H:%M")
    code = A._coupon(legs)
    A._record_version(sent, row, rows, (legs, total, prob), hhmm, code)
    msg = A._fair_reply((legs, total, prob), f"💎 Ton combiné du matin est maintenant aux cotes 1xBet de {hhmm}, avec son code coupon.",
                      fmt.combo_changes(removed, added, changed), title=CB.shots_title(row["tier"]), stake_txt=A._stake_txt(row), when=hhmm,
                      code=code, retry=A.RETRY_BUTTON)
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
                or A._started([pick], now) or A._all_soon([pick], now)):
            return 0
        res, hhmm = xprice.live_picks([pick], now, fetch, fresh_min=C.XBET_FRESH_MIN)
        _, state, new = res[0]
        if state != "ok" or not CB.leg_reason(new):
            return 0
        code = A._coupon([new])
        row.update(xodds=new["xodds"], xat=hhmm, **({"xref": new["xref"]} if new.get("xref") else {}), **({"coupon": code} if code else {}))
        track.save(rows)
        today["picks"] = [new if c["key"] == row["key"] else c for c in today["picks"]]
        today.setdefault("picks_at", hhmm)
        store.save("today.json", today)
        body = fmt.single(new, CB.leg_reason(new), stake_txt=A._stake_txt(row), coupon=code, when=hhmm, retry=A.RETRY_BUTTON)
        telegram.send(chat.with_played_button(Html(fmt.esc(f"🎟️ Le pari du jour est maintenant aux cotes 1xBet de {hhmm}.") + "\n\n" + body),
                                               "pari", day, xrefresh=True, lookup=True, code=code))
        return 1
    except Exception as e:                          # noqa: BLE001 - une tentative en échec ne bloque jamais le relevé
        print(f"[1xbet] pari du jour aux cotes 1xBet ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


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
    now = now or A.now_utc()
    rows = fair.load_combos()
    row = next((r for r in rows if r["date"] == str(day) and str(r["tier"]) == str(tier)), None)
    mine = bool(row) and played.is_custom(row)
    rng = (row["lo"], row["hi"], CB.kinds_prefix(row.get("kinds"))) if mine else A._tier_range(tier)
    if not row or not rng:
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    if row.get("closed") and not fair.is_noted(row):
        return f"Ce combiné a été remplacé à {row.get('closed_at') or '?'} par la reconstruction de la voie saison : écris « saison » pour voir ceux d'aujourd'hui."
    lo, hi, prefix = rng
    sent = A._load_updates(str(day))
    base = A._base_legs(sent, row)
    if A._started(base, now):
        return "Ce combiné est en jeu : un match a commencé, Kelly ne le modifie plus."
    if mine and not played.is_real(row):
        return "Ce combiné est à la cote mini de Kelly, pas aux cotes de 1xBet : il n'y a rien à revérifier, compare chaque cote dans l'appli."
    if mine and row.get("src") == "picks":
        return A._refresh_picks(row, rows, sent, str(day), now, fetch)        # noté : relu et affiché, jamais remplacé (fair.is_noted)
    if fair.is_ticket(row) or fair.is_noted(row):   # noté joué (combiné du matin ou sur mesure) : le ticket est placé, on le relit et on le dit, sans jamais le remplacer
        return A._check_ticket(row, base, str(day), str(tier), now, check)
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False, silent=True)
    if CB.season_no(tier) is not None or (mine and row.get("src") == "saison"):      # voie `saison` : relue sur SES jambes, jamais remplacée par celles des autres voies
        res, hhmm, ok = A._live_season(base, lo, hi, now, check, usage=None if mine else A._others_usage(rows, row, str(day), sent))
    else:
        res, hhmm, ok = A._live_fair(base, lo, hi, prefix, now, check, usage=None if mine else A._others_usage(rows, row, str(day), sent))
    if not ok:
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    if res is None:
        return ("Les paris de ce combiné ne sont plus jouables chez 1xBet et Kelly ne trouve pas de remplaçants dans la même "
                "fourchette de cote. Demande plutôt un « combi » sur mesure.")
    legs, total, prob, removed, added, changed = res
    code = A._coupon(legs)
    dropped = A._record_version(sent, row, rows, (legs, total, prob), hhmm, code)
    head = (f"🔄 {'Combi sur mesure' if mine else 'Combiné ' + A._of_day(tier)} remis aux cotes 1xBet de {hhmm}"
            + (" : aucun pari à changer." if not (removed or added or changed) else ".") + (A.DROPPED_NOTE if dropped else ""))
    msg = A._fair_reply((legs, total, prob), head, fmt.combo_changes(removed, added, changed),
                      title=f"🔄 {'COMBI SUR MESURE' if mine else 'COMBINÉ'} À JOUR {hhmm}", stake_txt=A._stake_txt(row), when=hhmm, code=code,
                      retry=A.RETRY_BUTTON)
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
    for l in A._base_legs(sent, row):
        c = by_key.get(l["key"])
        legs.append(c and c | {k: v for k, v in l.items() if v is not None and k != "result"})
    if today.get("day") != day or not all(legs):
        return "Ce combiné n'est plus en mémoire (il date d'un autre jour)."
    if A._all_soon(legs, now):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes.")
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False, silent=True)
    res, hhmm = xprice.live_picks(legs, now, fetch)
    if all(st == "stale" for _, st, _ in res):
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    gone, new, changes = A._real_changes(res)
    if gone:
        return Html(fmt.esc(f"🔄 Ce combiné vérifié chez 1xBet à {hhmm} : il n'est plus jouable tel quel.") + "\n" + "\n".join(changes)
                    + "\n\n" + fmt.i("Un pari n'est plus proposé par 1xBet. Demande un nouveau « combi » sur mesure, ou touche « 🔄 Mettre à jour "
                                      "tous les paris »."))
    total, prob = math.prod(c["odds"] for c in new), CB.combo_prob(new)
    code = A._coupon(new)
    dropped = A._record_version(sent, row, rows, (new, total, prob), hhmm, code)      # noté : rien n'est écrit (la note et les paris restent)
    head = f"🔄 Combi sur mesure remis aux cotes 1xBet de {hhmm}" + ("." if changes else " : aucun changement.") + (A.DROPPED_NOTE if dropped else "")
    if fair.is_noted(row):                          # ticket placé : on montre les cotes du moment, ta note « joué à … » et tes paris ne changent pas
        head = (f"🎟️ Ton ticket (combi sur mesure joué à {fmt.odds_txt(row['played']['odds'])}) relu chez 1xBet à {hhmm}" + ("." if changes else " : aucun changement.")
                + " Ta note et ses paris ne changent pas ; voici les cotes du moment.")
    body = fmt.combo(f"🎟️ COTES DU MOMENT {hhmm}" if fair.is_noted(row) else f"🔄 COMBI SUR MESURE À JOUR {hhmm}", new, total, prob, reasons={c["key"]: CB.leg_reason(c) for c in new},
                     stake_txt=A._stake_txt(row), coupon=code, when=hhmm, retry=A.RETRY_BUTTON)
    return chat.with_played_button(Html(fmt.esc(head) + ("\n" + "\n".join(changes) if changes else "") + "\n\n" + body),
                                   f"v{row['tier']}", day, refresh=str(row["tier"]), lookup=True, code=code)


def refresh_real(what, day, now=None, fetch=None):
    """Bouton « 🎟️ Vérifier ce combiné » sous un combiné réel du matin (The Odds API, remis aux cotes 1xBet : `what` = mot du palier, comme
    « sur ») ou sous le pari du jour (`what` = « pari ») : ses matchs sont relus EN DIRECT chez 1xBet (xprice.live_picks, mêmes règles de
    fraîcheur que les autres vérifications). Les paris ne sont pas remplacés (un combiné réel est suivi par ses paris) : la réponse donne
    les cotes du moment, signale un pari qui n'est plus chez 1xBet (le combiné n'est alors plus jouable tel quel) ou qui a perdu son
    avantage, et un NOUVEAU code coupon. Les cotes et le code du moment sont retenus dans la ligne du pari (« xnow », lu par « Mes
    paris »). `fetch` : injectable pour les tests."""
    now = now or A.now_utc()
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
    if A._started(legs, now):
        return "Ce combiné est en jeu : un match a commencé, Kelly ne peut plus le revérifier."
    if A._all_soon(legs, now):
        return (f"Les matchs de ce combiné commencent dans moins de {C.XBET_SOON_MIN} minutes : 1xBet les retire de l'avant-match, "
                "Kelly ne peut plus vérifier leurs cotes.")
    telegram.send("⏳ Je vérifie ce combiné chez 1xBet…", queue=False, silent=True)
    res, hhmm = xprice.live_picks(legs, now, fetch)
    if all(st == "stale" for _, st, _ in res):
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    gone, new, changes = A._real_changes(res)
    label = "combiné" if x.get("keys") else "pari du jour"
    if gone:                                        # un pari n'existe plus chez 1xBet : pas de nouveau code, le combiné n'est plus jouable tel quel
        x["xnow"] = {"at": hhmm, "dead": True}
        t.save()
        return Html(fmt.esc(f"🔄 Ce {label} vérifié chez 1xBet à {hhmm} : il n'est plus jouable tel quel.") + "\n" + "\n".join(changes)
                    + "\n\n" + fmt.i("Un pari n'est plus proposé par 1xBet. Demande un « combi » sur mesure, ou touche « 🔄 Mettre à jour "
                                      "tous les paris »."))
    total, prob = math.prod(c["odds"] for c in new), CB.combo_prob(new)
    code = A._coupon(new)
    x["xnow"] = {"at": hhmm, "odds": round(total, 3)} | ({"coupon": code} if code else {})
    t.save()
    head = f"🔄 Ce {label} remis aux cotes 1xBet de {hhmm}" + ("." if changes else " : aucun changement.")
    if x.get("keys"):
        body = fmt.combo(f"🔄 {fmt.tier_name(x['tier']).upper()} À JOUR {hhmm}", new, total, prob,
                         reasons={c["key"]: CB.leg_reason(c) for c in new}, stake_txt=A._stake_txt(x), coupon=code, when=hhmm, retry=A.RETRY_BUTTON)
    else:
        body = fmt.single(new[0], CB.leg_reason(new[0]), title=f"🔄 PARI DU JOUR À JOUR {hhmm}", stake_txt=A._stake_txt(x), coupon=code,
                          when=hhmm, retry=A.RETRY_BUTTON)
    return chat.with_played_button(Html(fmt.esc(head) + ("\n" + "\n".join(changes) if changes else "") + "\n\n" + body),
                                   what, day, xrefresh=True, lookup=True, code=code)


def _check_ticket(row, legs, day, tier, now, check=None):
    """« 🎟️ Vérifier ce combiné » sous un combiné du matin NOTÉ joué : ses matchs sont relus EN DIRECT chez 1xBet et Kelly dit si un pari du ticket n'y
    est plus ou paie moins ; la ligne de suivi (paris, note, règlement) ne change JAMAIS. Pour une version à jour : « ↩️ Annuler » la note, puis
    « 🎟️ Vérifier ce combiné ». -> la réponse."""
    telegram.send("⏳ Je vérifie ton ticket chez 1xBet…", queue=False, silent=True)
    season = CB.season_no(tier) is not None or any(l.get("strategy") == "saison" or l.get("lane") == "saison" for l in legs)
    pool, got, hhmm = (check or (seasonlane.live_check if season else xprice.live_check))({l["id"] for l in legs}, now, fresh_min=None)
    if not got:
        return "1xBet ne répond pas en ce moment (ou tous les matchs de ce combiné commencent) : réessaie dans quelques minutes."
    probs = A._ticket_problems(legs, pool, got, now)
    p = row["played"]
    if probs:
        text = (fmt.esc(f"⚠️ Un pari de ton ticket n'est plus proposé chez 1xBet (combiné joué à {fmt.odds_txt(p['odds'])}, vérifié à {hhmm}) :")
                + "\n" + "\n".join(A._ticket_lines(probs)) + "\n\n" + fmt.i(fmt.esc(A.TICKET_ADVICE)))
    else:
        text = fmt.esc(f"✅ Ton ticket (combiné joué à {fmt.odds_txt(p['odds'])}) est toujours chez 1xBet à {hhmm} : tous ses paris sont encore proposés. "
                       "Ta cote est acquise, rien à changer.")
    return chat.with_played_button(Html(text), f"v{tier}", day, refresh=tier, lookup=True, code=row.get("coupon"))
