"""Commandes Telegram et chat (routage des /commandes) : issues du découpage d'app.py (le code lit app via A.nom)."""

import math
import sys
import time
from . import config as C, combos as CB, live as LV, cache, calib, chat, confirm, edgemap, fmt, lanes, learn, migrate, monde, money, morningcmd, oddsonly, played, progress, resultats, settle, sources, store, telegram, track
from .sources import xbet
from .telegram import Html
from datetime import timedelta
from ._appref import A


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
        return A._handle_command(cmd)


def _handle_command(text):
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    today = store.load_doc("today.json", {"day": "", "picks": []})
    picks = today.get("picks") or []
    if cmd in ("/aide", "/help", "/start"):
        return telegram.with_buttons(A.HELP, chat.menu())
    if cmd in ("/combis", "/combines", "/combinés"):                  # résultats des combinés, vue séparée : « combis [N jours] »
        return resultats.combis(A._days_arg(args))
    if cmd == "/bilan" and args and args[0].lower() in ("combi", "combis", "combiné", "combinés", "combines"):
        return resultats.combis(A._days_arg(args[1:]))
    if cmd == "/bilan" and args and args[0].lower() == "saison":
        return resultats.saison(A._days_arg(args[1:]))
    if cmd == "/bilan":
        detail = any(a.lower() in ("détail", "detail", "details", "détails") for a in args)
        nums = [a for a in args if a.isdigit()]
        if nums and (len(nums[0]) > 4 or int(nums[0]) > A.MAX_BILAN_DAYS):          # « /bilan 800000 » : message clair, pas une erreur de date
            return f"Choisis un nombre de jours raisonnable ({A.MAX_BILAN_DAYS} au plus), par exemple /bilan 30."
        days = int(nums[0]) if nums else None
        bets = store.load("bets.json", {"picks": [], "combos": []})
        out = settle.bilan(bets, days, detail=detail)
        line = A.money_line(bets["combos"])
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
        return A.send_backup()
    if cmd == "/bank":
        return ("💰 Kelly ne suit plus de bankroll en unités : elle travaille en FCFA, avec ton capital (« 💼 Mon capital » ou écris "
                "« capital 50000 ») pour les mises conseillées. Seuls les paris que tu notes comptent dans tes résultats (« 📊 Mes résultats »).")
    if cmd == "/stats":
        return Html(A.comma(fmt.esc(A.fmt_team(sources.team_profile(" ".join(args)) if args else None, " ".join(args)))))
    if cmd == "/budget":
        return money.command(args)
    if cmd == "/capital":
        return money.capital_command(args)
    if cmd == "/courbes":                           # les courbes du capital et du CLV 1xBet, en images (kelly/charts.py, aucune bibliothèque)
        return A.courbes_command()
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
    if cmd in ("/incoherences", "/incohérences"):                   # rapport de la voie cotes (fantôme, kelly/oddsonly.py)
        return oddsonly.report(args)
    if cmd == "/matin":                             # « matin » : relance le passage du matin s'il n'est pas fait ; « matin force » le refait (kelly/morningcmd.py)
        return morningcmd.handle(args)
    if cmd == "/familles":                         # « familles » : états des familles de marché ; « familles X on » : jouer une famille proposée (kelly/familyeval.py)
        from . import familyeval
        return familyeval.command(args)
    if cmd == "/carte":                             # « carte [stats | saison] » : carte des edges par case (kelly/edgemap.py)
        return edgemap.report(args)
    if cmd == "/saison":
        reply = A.season_command(args, today)
        return telegram.topic(reply, "season") if reply else reply
    if cmd == "/objectif":
        return money.goal_command(args)
    if cmd in ("/regle", "/reglé", "/régler", "/regler"):     # « réglé vm3 gagné 12/10 » : un ticket noté que le règlement gratuit ne trouve pas
        now_ = A.now_utc()
        kday = now_.date() if now_.hour >= C.DAY_END_HOUR else now_.date() - timedelta(days=1)
        return played.settle_command(args, str(today.get("day") or kday)[:10])
    if cmd in ("/joue", "/joué", "/jouer"):
        now_ = A.now_utc()
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
        target = A.combi_target(args)
        if target is None or target < A.MIN_COMBI:
            return chat.live_picker()
        toks = money.split_tokens(args)
        n, exact, _ = chat.parse_nlegs(toks[money.take_amount(toks)[1]:])        # « live 20 max 3 »
        reply = LV.combo(target, str(A.now_utc().date()), nlegs=(n, exact) if n and 2 <= n <= C.MAX_LEGS else None)
        return telegram.topic(reply, "livebet") if reply else reply
    if cmd in ("/monde", "/ligues"):
        # jour Kelly en cours ; après un rattrapage d'avant C.DAY_END_HOUR h (voir daily_skip_reason), today.json et monde.json portent la
        # date UTC du jour : c'est elle qu'il faut lire, sinon /monde ne trouve rien
        data = monde.load(monde.kelly_day(A.now_utc())) or (monde.load(today["day"]) if A._current_day(today, A.now_utc()) and today.get("day") else None)
        return monde.fmt_monde(data, " ".join(args)) if cmd == "/monde" else monde.fmt_ligues(data)
    pronos = today.get("pronos") or []
    if not picks and not pronos:
        return f"Kelly n'a pas encore analysé la journée (ça arrive à {C.DAY_END_HOUR:02d}h00)."
    when, picks_when = today.get("xbet_at"), today.get("picks_at") or today.get("xbet_at")      # heures des derniers relevés 1xBet
    if cmd in ("/analyse", "/match") and args:      # paris stats que 1xBet vient peut-être d'ouvrir : relus en direct (fenêtre des stats)
        pronos = A.reread_match(pronos, " ".join(args))
    if cmd == "/analyse":
        return A.analyse(picks, " ".join(args), pronos, when, picks_when)
    if cmd == "/top":
        live, live_pronos = A.upcoming(picks), A.upcoming(pronos)
        if not live and not live_pronos:
            return A.waiting_note(today) or f"Tous les matchs du jour ont commencé : rendez-vous demain à {C.DAY_END_HOUR:02d}h00."
        return A.top_reply(live, live_pronos, when, picks_when)
    if cmd == "/match":
        if not args:
            return "Exemple : /match PSG"
        found = A.event_matches(picks, " ".join(args))
        if not found:
            free = A.event_matches(pronos, " ".join(args))
            if free:
                shown, wait = CB.info_pronos(free), "\n\n".join(x for x in (A.pending_lines(free), A.underpaid_lines(free)) if x)
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
        shown = A._event_pronos(pronos, found)
        if shown:
            lines += ["", str(fmt.pronos(shown, 8, when=when))]
        mine_ = [c for c in pronos if c["id"] in {x["id"] for x in found}]
        wait = "\n\n".join(x for x in (A.pending_lines(mine_), A.underpaid_lines(mine_)) if x)
        if wait:
            lines += ["", wait]
        return Html("\n".join(lines))
    if cmd in ("/cotes", "/actualiser"):
        return A.manual_refresh()
    if cmd == "/combi":
        if args and played.norm(args[0]) in ("auto", "automatique"):      # « combi auto » : pas de cote à choisir, Kelly vise la plus haute cote raisonnable (10 au plus) avec les jambes du jour
            args = A.auto_args(picks, pronos, args[1:])
            if args is None:
                return "Pas assez de jambes jouables aujourd'hui pour un combiné automatique (il en faut au moins 2 de matchs différents)."
        target = A.combi_target(args)
        if target is None:
            return "Exemple : /combi 30, ou /combi 20 tirs fautes"
        if target < A.MIN_COMBI:                              # « /combi 0,5 » : un combiné de 2 paris au moins a une cote d'au moins 1,01
            return f"Un combiné a toujours une cote supérieure à {fmt.num(A.MIN_COMBI)}. Essaie par exemple /combi 5 ou /combi 30."
        toks = money.split_tokens(args)
        n, exact, rest = chat.parse_nlegs(toks[money.take_amount(toks)[1]:])    # « combi 20 max 4 », « combi 20 pile 4 », « combi 20 4 paris »
        if n is not None and not 2 <= n <= C.MAX_LEGS:
            return f"Un combiné a entre 2 et {C.MAX_LEGS} paris : écris par exemple « combi 20 max 4 » ou « combi 20 pile 4 »."
        bold = any(played.norm(w) in chat.BOLD_WORDS for w in rest)              # « combi 1900 tirs audacieux » / bouton « 🎲 Aller plus loin »
        kinds, unknown = chat.parse_kinds([w for w in rest if played.norm(w) not in chat.BOLD_WORDS])     # « combi 20 tirs fautes »
        if unknown:
            return chat.kinds_help(unknown)
        live = A.upcoming(picks)
        if not live and not A.upcoming(pronos) and A.waiting_note(today):
            return A.waiting_note(today)
        dropped = A.reuse_note(today.get("day"), live, A.upcoming(pronos))          # avant l'envoi : ce combiné-ci ne compte pas encore
        reply = A.custom_combo(live, A.upcoming(pronos), target, today.get("day"), started=bool(picks) and not live,
                             when=picks_when, kinds=kinds or None, bold=bold, nlegs=(n, exact) if n else None)
        return A._with_sweep(reply, "\n".join(x for x in (A.sweep_line(live, A.upcoming(pronos)), dropped) if x))
    return "Commande inconnue. /aide pour la liste."


def auto_args(picks, pronos, rest):
    """Arguments de « /combi » pour « combi auto » : la cote visée est celle du combiné le plus probable que les jambes jouables du jour permettent, en partant de 10 (ou de la plus haute cote atteignable) et en descendant (min 1,5) ;
    None si aucun combiné de 2 paris n'est possible. `rest` : les mots qui suivent « auto » (types de paris, nombre de paris), repris tels quels."""
    live, free = A.upcoming(picks), A.upcoming(pronos)
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


def courbes_command():
    """« courbes » : envoie les images du capital et du CLV 1xBet (sans sonnerie) ; réponse vide quand elles sont parties, sinon pourquoi il n'y en a pas."""
    import shutil
    import tempfile
    from . import charts
    tmp = tempfile.mkdtemp(prefix="kelly-courbes-")
    try:
        return A._courbes_send(charts.courbes(tmp))
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


def sweep_line(picks, pronos):
    """« 📊 12 matchs · 9 familles analysées · 214 lignes évaluées · 31 jambes possibles (jusqu'à 5 marchés par match ; un combiné n'en prend qu'un par match) · non balayées : touches, hors-jeu » :
    ce que « combi N » avait à sa disposition (les pronostics et paris du jour chiffrés chez 1xBet), famille par famille, jamais seulement la meilleure ligne d'un match. '' sans aucune cote 1xBet."""
    try:
        return A._sweep_line(picks, pronos)
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
    miss = [calib.LABELS.get(f, f) for f in A.SWEEP_FAMILIES if f not in fams]
    s = lambda n, one, many: f"{n} {one if n <= 1 else many}"      # noqa: E731
    return (f"📊 {s(len({c['id'] for c in rows}), 'match', 'matchs')} · {s(len(fams), 'famille analysée', 'familles analysées')} · {s(lines, 'ligne évaluée', 'lignes évaluées')}"
            f" · {s(len(legs), 'jambe possible', 'jambes possibles')} · {CB.value_line(legs)}"
            + (f" · non balayées : {', '.join(miss)}" if miss else ""))


def reuse_note(day, picks, pronos):
    """« ♻️ 3 jambes écartées : déjà utilisées aujourd'hui » : jambes possibles de « combi N » que le plafond de réutilisation (CB.reuse_cap) retire à cet instant ; '' sinon."""
    try:
        u = A.day_usage(day)
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
                    A._flush_outbox_locked()
                telegram.poll(A._waiting(A.handle_command), timeout=10, on_callback=A._waiting(chat.on_callback), defer=True, quick=chat.quick)   # court : réponse rapide après une coupure
            except Exception as e:                  # noqa: BLE001 - le serveur ne s'arrête pas sur une erreur
                print(f"[serve] {e}", file=sys.stderr)
                time.sleep(2)
    else:
        migrate.ensure()
        n = telegram.poll(A._waiting(A.handle_command), on_callback=A._waiting(chat.on_callback))
        print(f"{n} message(s) traité(s).")


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


def send_backup():
    """/backup : envoie kelly.db (copie cohérente, vérifiée, zippée) sur Telegram -> None si c'est parti, sinon le message d'erreur (kelly/backup.py).
    N'a besoin d'AUCUN verrou des données (la copie est cohérente à elle seule) : le serveur la lance sans le verrou (chat.quick)."""
    from . import backup
    return backup.send()
