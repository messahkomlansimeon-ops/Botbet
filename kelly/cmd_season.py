"""Commandes de la voie `saison` (saison, saison résultats, saison reconstruire...) : issues du découpage d'app.py (le code lit app via A.nom)."""

import math
import sys
from . import config as C, combos as CB, fair, fmt, lanes, money, odds, resultats, seasonlane, store, telegram, track
from .sources import xbet
from datetime import datetime
from ._appref import A


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
    msg = A.fair_combo_message(row["tier"], legs, row["min_odds"], row["prob"], day, row.get("stake"), mise, row.get("coupon"), row.get("at"),
                             A._season_number(day, row["tier"]), lookup=True)       # lookup : l'état réel (noté joué ou pas) dans les boutons
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
    now = now or A.now_utc()
    day = today.get("day") or ""
    mode = lanes.season_mode()
    if mode != "on":
        return f"La voie « saison » est {'en fantôme (calculée, jamais jouée)' if mode == 'ghost' else 'éteinte'} : rien à reconstruire."
    if not C.SEASON_AUTO:
        return "La construction automatique est éteinte (SEASON_TIERS fixe dans ta configuration) : « saison 10 » construit un combiné à la cote voulue."
    last = store.load(A.SEASON_REBUILD_FILE, {})
    if isinstance(last, dict) and last.get("day") == day and last.get("at"):
        try:
            wait = C.SEASON_REBUILD_GAP * 60 - (now - datetime.fromisoformat(last["at"])).total_seconds()
        except (TypeError, ValueError):
            wait = 0
        if wait > 0:
            return f"Une reconstruction vient d'avoir lieu à {str(last['at'])[11:16]} : attends encore {math.ceil(wait / 60)} min (chaque reconstruction relit 1xBet)."
    rows, kept, replace = A._season_split(day)
    slots = C.SEASON_AUTO_MAX_COMBOS - len(kept)
    if slots <= 0:                                  # avant toute relecture : le proxy n'est pas sollicité pour rien
        return f"Déjà {len(kept)} combinés `saison` joués ou réglés aujourd'hui : le plafond de {C.SEASON_AUTO_MAX_COMBOS} est atteint, rien à reconstruire."
    telegram.send("⏳ Je reconstruis la voie saison sur les matchs du jour chez 1xBet…", queue=False, silent=True)
    rebuilt, info = seasonlane.rebuild(now, fetch)
    store.save(A.SEASON_REBUILD_FILE, {"day": day, "at": now.isoformat(timespec="seconds")})       # même un relevé sans résultat compte pour l'écart (proxy)
    line = seasonlane.summary_line(info) + (f" · ⚠️ plafond mensuel de données 1xBet atteint à {C.SEASON_MONTH_PCT:g} % : pas de relecture" if info.get("capped") else "")
    legs = [c for c in rebuilt if lanes.usable(c)]
    rows, kept, replace = A._season_split(day)           # RELU après la relecture réseau (longue) : une note ou un règlement arrivés entre-temps comptent
    slots = C.SEASON_AUTO_MAX_COMBOS - len(kept)
    if slots <= 0:
        return line + f"\nDéjà {len(kept)} combinés `saison` joués ou réglés aujourd'hui : le plafond de {C.SEASON_AUTO_MAX_COMBOS} est atteint, rien à reconstruire."
    if not info["read"]:
        return line + "\n1xBet n'a rien rendu à l'instant : tes combinés actuels restent tels quels."
    exclude = {str(l.get("id") or str(l.get("key") or "").split("|")[0]) for r in kept for l in (r.get("legs") or [])}
    usage = A.day_usage(day, rows=[r for r in rows if r not in replace])
    plan = CB.season_auto_plan(legs, usage, max_combos=slots, busy_tiers=[r["tier"] for r in rows if str(r.get("date")) == day], exclude=exclude)
    sent = [(t, c) for t, c in plan if not fair.unsent(c[0])]
    if not sent:                                    # rien d'ENVOYABLE (aucun, ou que des combinés sans valeur en fantôme) : on ne détruit rien
        return line + "\nPas assez de jambes `saison` jouables pour un combiné à envoyer : tes combinés actuels restent tels quels."
    hhmm = now.strftime("%H:%M")
    # tout ce qui peut échouer (mises, coupons) AVANT de fermer quoi que ce soit
    left = None
    if money.active():
        left = max(0.0, money.daily() - A._committed(day, [r for r in rows if r not in replace], noted=not money.capital_mode()))       # mode capital : daily() a déjà retiré les mises notées
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
    msgs = [A.fair_combo_message(t, cl, odds_, prob, day, stakes.get(t), mise, coupons.get(t), hhmm, A._season_number(day, t)) for t, (cl, odds_, prob) in sent]
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
    return min(int(n), A.MAX_BILAN_DAYS) if n else None


def season_command(args, today, now=None, fetch=None):
    """« saison » : l'état de la voie `saison` (jambes du matin, combinés du jour, fantômes) ; « saison N » : un combiné sur mesure à la cote N bâti sur les SEULES jambes `saison`,
    RECONSTRUITES à la demande sur les matchs du jour pas encore commencés (championnats suivis et coupes, LDC comprise) avec les cotes 1xBet relues à l'instant (seasonlane.rebuild :
    au plus C.SEASON_MAX_MATCHES matchs et C.SEASON_SECONDS s). Une ligne dit ce qui a été trouvé : matchs lus, de coupe, équipes sous le minimum de matchs, matchs sans prix. Jambes de valeur
    d'abord, puis le remplissage ; code coupon, mise et bouton « ✅ Je le joue » (suivi comme « combi N »). 1xBet muet : les jambes du matin, cotes du dernier relevé, dit clairement."""
    now = now or A.now_utc()
    day = today.get("day") or ""
    morning = [c for c in A.upcoming(today.get("season") or [], now) if lanes.usable(c)]
    mode = lanes.season_mode()
    if args and str(args[0]).lower() in ("auto", "reconstruire"):          # « saison auto » / « 🔄 Reconstruire » : relance la construction automatique du jour
        return A.season_rebuild(today, now, fetch)
    if args and str(args[0]).lower() in ("résultats", "resultats", "résultat", "resultat", "bilan"):     # « saison résultats [N] » : la vue séparée des résultats de la voie
        return resultats.saison(A._days_arg(args[1:]))
    if not args:
        return A.season_listing(today, day, mode, morning, now)
    target = A.combi_target(args)
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
    used = A.day_usage(day)
    used.filter(legs)
    if used.excluded:
        line += f"\n♻️ {len(used.excluded)} jambe{'s' if len(used.excluded) > 1 else ''} écartée{'s' if len(used.excluded) > 1 else ''} : déjà utilisée{'s' if len(used.excluded) > 1 else ''} aujourd'hui"
    combo = CB.season_plan(legs, [target], usage=A.day_usage(day))[0][1]
    if not combo:
        lo = target * (1 - C.SEASON_SPREAD)
        free = A.day_usage(day).filter(CB.season_pool(legs))
        top = CB.reach([c for c in free if c.get("sval") or C.FILL_LEGS], C.MAX_LEGS)        # les MÊMES jambes que le constructeur (voie saison, cote 1xBet, libres, de valeur si le remplissage est coupé)
        head = f"\nAucun combiné « saison » à la cote {fmt.trim(target, 1)} avec ces jambes"
        if top > 1 and top >= lo:       # la cote visée est atteignable en principe : le message ne doit pas dire le contraire
            return line + head + f" : la plus haute cote atteignable ({fmt.trim(top, 1)}) atteint la fourchette de {fmt.trim(lo, 0)} à {fmt.trim(target * (1 + C.SEASON_SPREAD), 0)}, mais aucune combinaison ne respecte les règles de construction (un pari par match, plafond de remplissage). Essaie une autre cote."
        return line + head + (f" : la plus haute cote atteignable est {fmt.trim(top, 1)}." if top > 1 else ".")
    lo, hi = target * (1 - C.SEASON_SPREAD), target * (1 + C.SEASON_SPREAD)
    keep = {"day": day, "target": target, "lo": lo, "hi": hi, "src": "saison"}
    intro = f"{C.SEASON_TITLE.split(' ', 1)[0]} Combiné « saison en cours » à la cote {fmt.trim(target, 1)} (modèle des matchs de cette saison, mélangé au marché).\n{line}"
    return A._fair_reply(combo, intro, title=f"{C.SEASON_TITLE} · cote {fmt.trim(target, 1)}", when=when, stale=stale, retry="redemande le combi", budget=True, keep=keep,
                       shared=A._shared_counts(day, combo[0]))
