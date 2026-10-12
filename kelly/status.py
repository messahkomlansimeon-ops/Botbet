"""« status » sur Telegram (/status, « santé ») : l'état de Kelly en un message, répondu TOUT DE SUITE par le serveur (sans le verrou des données,
comme « état ») :
  - le passage du matin s'il tourne (kelly/progress.py) ;
  - chaque service systemd du PC (bot, passage du matin, passage horaire, relevé 1xBet de 15:00, relecture de 12:00, sauvegarde, réglage) : en cours,
    dernier passage réussi ou en échec, prochain passage (`systemctl --user show`, ignoré hors systemd : GitHub Actions) ;
  - les sources de stats (sources.report : dernier état connu de chaque source, 365scores compris) ;
  - les API et les quotas : The Odds API (clé, crédits : /sports, gratuit), API-Football (requêtes du jour), données du proxy 1xBet et 365scores du
    mois, Telegram (getMe).
Aucun crédit dépensé, aucune écriture. Ne lève jamais : une partie en échec est dite en une ligne."""
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import config as C, progress

# (unité systemd, nom dit à l'utilisateur, minuterie ou None pour le bot)
UNITS = (("kelly-serve", "Bot Telegram", False), ("kelly-daily", "Passage du matin", True), ("kelly-track", "Passage horaire", True),
         ("kelly-xbet", "Relevé 1xBet de 15h", True), ("kelly-evening", "Relecture de 12h", True), ("kelly-watch", "Suivi des tickets (10 min)", True), ("kelly-backup", "Sauvegarde de 23h", True),
         ("kelly-tune", "Réglage du dimanche", True))
PROPS = "ActiveState,SubState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp,LoadState"
TIMER_PROPS = "NextElapseUSecRealtime,LastTriggerUSec,LoadState"
WORDS = {"status", "statut", "sante", "santé", "/status", "/statut", "/sante", "/santé"}


def _show(unit, props):
    """`systemctl --user show unit -p props` -> {propriété: valeur} ; {} hors systemd ou en erreur."""
    try:
        out = subprocess.run(["systemctl", "--user", "show", unit, "-p", props], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode:
        return {}
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def _when(stamp, now):
    """« Sat 2026-10-10 05:18:43 GMT » (systemd) -> « 05h18 », « hier 23h00 », « dim. 04h00 » ; '' si vide."""
    stamp = (stamp or "").strip()
    if not stamp or stamp in ("n/a", "0"):
        return ""
    try:
        parts = stamp.split()
        dt = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return ""
    hm = dt.strftime("%Hh%M")
    delta = (dt.date() - now.date()).days
    return hm if delta == 0 else f"hier {hm}" if delta == -1 else f"demain {hm}" if delta == 1 else f"{dt:%d/%m} {hm}"


def service_lines(now=None):
    """Une ligne par service systemd ; [] hors systemd (aucune unité connue)."""
    now = now or datetime.now(timezone.utc)
    lines = []
    for unit, name, timer in UNITS:
        s = _show(f"{unit}.service", PROPS)
        if not s or s.get("LoadState") == "not-found":
            continue
        t = _show(f"{unit}.timer", TIMER_PROPS) if timer else {}
        nxt = _when(t.get("NextElapseUSecRealtime"), now) if t and t.get("LoadState") != "not-found" else ""
        # avant un redémarrage du PC, systemd n'a plus l'heure d'exécution : celle du dernier déclenchement de la minuterie (gardée, Persistent=true)
        last = _when(s.get("ExecMainExitTimestamp") or s.get("ExecMainStartTimestamp") or t.get("LastTriggerUSec"), now)
        state, result = s.get("ActiveState"), s.get("Result")
        if not timer:                                   # le bot : il doit tourner en continu
            since = _when(s.get("ExecMainStartTimestamp"), now)
            lines.append(f"✅ {name} : actif depuis {since}" if state == "active" else f"❌ {name} : arrêté ({state})")
            continue
        if state == "activating":
            head = f"⏳ {name} : en cours depuis {_when(s.get('ExecMainStartTimestamp'), now)}"
        elif state == "failed" or result not in ("success", None, ""):
            why = {"timeout": "trop long, coupé", "exit-code": "erreur", "signal": "arrêté"}.get(result, result)
            head = f"❌ {name} : échec ({why}){' à ' + last if last else ''}"
        elif last:
            head = f"✅ {name} : dernier {last}"
        else:
            head = f"⚪ {name} : pas encore lancé"
        lines.append(head + (f" · prochain {nxt}" if nxt else ""))
    return lines


def _num(n):
    """11440 -> « 11 440 »."""
    return f"{int(n):,}".replace(",", " ")


def _gauge(name, used, total, unit=""):
    """« The Odds API   ▰▰▱▱▱▱▱▱▱▱ 87/500 ce mois » : une jauge de quota (texte brut)."""
    from . import fmt
    return f"{name} {fmt.bar(used / total if total else 0)} {_num(used)}/{_num(total)}{unit}"


def odds_line():
    """The Odds API en UNE ligne (jauge des crédits du mois), sans le tour de ligues de app._check_odds (odds.scan_today : jusqu'à 150 s cache froid,
    dans la boucle qui répond à Telegram) : les crédits déjà connus de ce processus (odds.CREDITS, lus dans les en-têtes du dernier appel), sinon UN
    appel /sports (gratuit, jamais hors passage : commande Telegram = hors ligne) pour les lire."""
    from . import odds
    try:
        if odds.CREDITS["remaining"] is None and os.environ.get("ODDS_API_KEY"):
            odds.get("/sports")                         # un seul appel, gratuit : met les crédits à jour
        rem, used = odds.CREDITS["remaining"], odds.CREDITS["used"]
        if not os.environ.get("ODDS_API_KEY"):
            return "❌ The Odds API : ODDS_API_KEY absente"
        if rem is None:
            return "✅ The Odds API : clé présente (crédits connus au prochain passage)"
        if used is None:
            return f"✅ The Odds API : {rem} crédits restants ce mois"
        return ("✅" if rem > 0.1 * (rem + used) else "⚠️") + " " + _gauge("The Odds API", used, rem + used, " ce mois")
    except odds.OddsError as e:
        if e.code == "OFFLINE":
            return "✅ The Odds API : clé présente (crédits connus au prochain passage)"
        return f"❌ The Odds API : {e}"
    except Exception as e:                              # noqa: BLE001
        return f"⚠️ The Odds API : vérification impossible ({type(e).__name__})"


def api_lines():
    """The Odds API, API-Football, données du proxy (1xBet, 365scores) en jauges, Telegram : une ligne chacun (texte brut)."""
    import re
    from . import app
    from .sources import s365, xbet
    out = [odds_line()]
    try:
        ok, lines = app._check_stats()
        m = re.search(r"requêtes aujourd'hui (\d+)/(\d+)", lines[0])
        out.append(lines[0] if not m or not lines[0].startswith("✅") else
                   ("✅" if int(m[1]) < 0.9 * int(m[2]) else "⚠️") + " " + _gauge("API-Football", int(m[1]), int(m[2]), " aujourd'hui"))
    except Exception as e:                              # noqa: BLE001
        out.append(f"⚠️ API-Football : vérification impossible ({type(e).__name__})")
    try:
        p = xbet.proxy()
        if not p and not C.XBET_DIRECT:
            out.append("❌ Proxy 1xBet : non configuré (XBET_PROXY manquant dans .env) — cotes 1xBet coupées")
        else:
            u = xbet.usage()
            out.append(f"{'✅' if u['bytes'] < 0.9 * C.XBET_MONTH_MB * 1e6 else '⚠️'} "
                       + _gauge("1xBet (proxy)", round(u["bytes"] / 1e6), C.XBET_MONTH_MB, " Mo"))
        u = s365.usage()
        out.append(f"{'✅' if u['bytes'] < 0.9 * C.S365_MONTH_MB * 1e6 else '⚠️'} " + _gauge("365scores (proxy)", round(u["bytes"] / 1e6), C.S365_MONTH_MB, " Mo"))
    except Exception as e:                              # noqa: BLE001
        out.append(f"⚠️ Proxy : compteurs illisibles ({type(e).__name__})")
    try:
        ok, lines, _ = app._check_telegram_bot()
        out.append("✅ Telegram joignable" if lines[0].startswith("✅") else lines[0])
    except Exception as e:                              # noqa: BLE001
        out.append(f"⚠️ Telegram : vérification impossible ({type(e).__name__})")
    return out


def _at(iso, now):
    """Heure d'une mise à jour : « 04h03 » aujourd'hui, « 11/10 04h03 » sinon ; '' si illisible."""
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt:%Hh%M}" if dt.date() == now.date() else f"{dt:%d/%m %Hh%M}"


def source_lines(now=None):
    """Sources de stats en lignes courtes (texte brut) : « ✅ football-data · 11 440 · 04h03 » ; les sources bloquées par leur site (robots.txt,
    blocage) regroupées sur une ligne « 💤 … (normal) » ; les avertissements comptés (« ⚠️ 3 téléchargements reportés au prochain passage ») au lieu
    d'être listés (détail : python bot.py --check)."""
    from .sources import SOURCES, s365
    now = now or datetime.now(timezone.utc)
    out, sleeping, later, odd = [], [], 0, 0
    mods = [(m, m.LABEL, "") for m in SOURCES] + [(s365, s365.LABEL, " matchs")]
    for m, label, unit in mods:
        h = m.health()
        err = h["last_error"] or ""
        if h["blocked_until"] or ("robots.txt" in err and not h["ok"]):
            sleeping.append(label)
            continue
        if not getattr(m, "enabled", lambda: True)():
            continue
        if h["ok"] is None:
            if m is not s365:
                out.append(f"⚪ {label} : pas encore lancée")
            continue
        if not h["ok"]:
            first = err.split(" ; ")[0] or "échec"
            out.append(f"⚠️ {label} : {first[:90]}")
            continue
        at = _at(h["last_success"], now)
        out.append(f"✅ {label} · {_num(h['rows'])}{unit}" + (f" · {at}" if at else ""))
        for part in (x for x in err.split(" ; ") if x.strip()):
            if "report" in part:
                later += 1
            else:
                odd += 1
    if sleeping:
        out.append(f"💤 {', '.join(sleeping)} : bloqué{'s' if len(sleeping) > 1 else ''} par {'les sites' if len(sleeping) > 1 else 'le site'} (normal)")
    if later:
        out.append(f"⚠️ {later} téléchargement{'s' if later > 1 else ''} reporté{'s' if later > 1 else ''} au prochain passage")
    if odd:
        out.append(f"⚠️ {odd} page{'s' if odd > 1 else ''} sans données (détail : python bot.py --check)")
    return out


def _mood(won, said):
    return "trop confiante" if won < said - 0.05 else "un peu prudente" if won > said + 0.05 else "juste"


def _state(n, w, s, need, k=None):
    """« 🟢 Buts · 131 · juste (71 % / 69 %) », « 🔴 Tirs · 67 · trop confiante (45 % / 65 %) → écartés des combinés », « ⏳ Corners · 49/60 » :
    sans le libellé (ajouté par l'appelant) -> (pastille, suite)."""
    from . import fmt
    won, said, n = w / n, s / n, int(round(n))           # cumul vieilli : nombre effectif, décimal
    if n < need:
        return "⏳", f"{n}/{need}"
    mood = _mood(won, said)
    out = f"{n} · {mood} ({fmt.pct(won)} / {fmt.pct(said)})"
    if k is not None and k < C.VIABLE_MIN_K:
        out += " → écartés des combinés"
    return ("🔴" if mood == "trop confiante" else "🟢"), out


def learning_lines(top=6):
    """Section « 🧠 Ce que Kelly apprend » (texte brut) : une ligne par famille (pastille, résultats, verdict, réalisé / annoncé), l'ensemble (avis de
    Kelly face au marché, prêt ou en attente) et le live (par famille et tranche). [] s'il n'y a encore rien de réglé."""
    from . import calib, ensemble, live
    out = []
    d = calib.load()
    fams = sorted(((f, a) for f, a in d.items() if not f.startswith("_") and isinstance(a, list) and a[0]), key=lambda x: -x[1][0])
    for f, (n, w, s) in fams[:top]:
        dot, txt = _state(n, w, s, calib.min_need(f), calib.family_k(f, d, real_only=True))
        out.append(f"{dot} {calib.LABELS.get(f, f).capitalize()} · {txt}" + calib.new_rule_note(f, d))
    try:
        from . import replay
        rl = replay.status_line()
        if rl:
            out.append(rl)
    except Exception:                                   # noqa: BLE001 - facultatif
        pass
    ens = [(f, ensemble.weights(f)) for f in ("goals", "dc", "btts", "h2h")]
    ready = [calib.LABELS.get(f, f) for f, w in ens if w["n_m"] >= C.ENSEMBLE_MIN_N]
    wait = [f"{calib.LABELS.get(f, f)} {int(w['n_m'])}/{int(C.ENSEMBLE_MIN_N)}" for f, w in ens if w["n_m"] < C.ENSEMBLE_MIN_N]
    out.append("⚖️ Avis face au marché : " + " · ".join(x for x in ((", ".join(ready) + (" prêts" if len(ready) > 1 else " prêt")) if ready else "",
                                                                    ", ".join(wait)) if x))
    ld = live.calib_data()
    tr = sorted(((k, a) for k, a in ld.items() if a[0]), key=lambda x: -x[1][0])
    for k, (n, w, s) in tr[:4]:
        fam, t = k.split("|")
        dot, txt = _state(n, w, s, C.FAIR_CALIB_MIN, calib._k([n, w, s]))
        out.append(f"{dot} Live {calib.LABELS.get(fam, fam)} {t} · {txt}")
    if not tr:
        out.append("💤 Live : rien de réglé pour l'instant")
    return out if fams or ld else []


def report(now=None):
    """Le message « status » (HTML) : verdict en tête, puis services (PC), sources, apprentissage, quotas."""
    from . import fmt
    from .telegram import Html
    now = now or datetime.now(timezone.utc)
    blocks = []
    try:
        st = progress.describe()
        if st.startswith(("⏳", "⚠️")):                  # passage du matin en cours ou interrompu : en premier
            blocks.append([fmt.esc(st)])
    except Exception:                                   # noqa: BLE001
        pass
    svc = service_lines(now)
    if svc:
        blocks.append([fmt.b("⚙️ Services")] + [fmt.esc(x) for x in svc])
    try:
        blocks.append([fmt.b("📡 Sources")] + [fmt.esc(x) for x in source_lines(now)])
    except Exception as e:                              # noqa: BLE001
        blocks.append([f"⚠️ Sources de stats : rapport illisible ({type(e).__name__})"])
    try:
        learn = learning_lines()
        if learn:
            blocks.append([fmt.b("🧠 Ce que Kelly apprend") + " " + fmt.i("(réalisé / annoncé)")] + [fmt.esc(x) for x in learn])
    except Exception as e:                              # noqa: BLE001
        blocks.append([f"⚠️ Apprentissage : illisible ({type(e).__name__})"])
    blocks.append([fmt.b("🔌 Quotas")] + [fmt.esc(x) for x in api_lines()])
    bad = sum(1 for blk in blocks for line in blk if line.startswith("❌"))       # 💤 (source bloquée par son site) : dit, rien à faire de ton côté
    verdict = "tout va bien 👍" if not bad else f"{bad} point{'s' if bad > 1 else ''} à regarder ❌"
    head = fmt.b(f"🩺 Kelly — {now:%Hh%M}") + f" · {verdict}"
    return Html("\n\n".join([head] + ["\n".join(blk) for blk in blocks]))


def is_status(text):
    """Le message est-il « status » (ou /status, statut, santé) ?"""
    from . import played
    t = played.norm((text or "").split("@")[0]).strip(" ?!.")
    return t in {played.norm(w) for w in WORDS}


def quick(text):
    """Réponse immédiate (telegram.poll `quick`) : le rapport si le message est « status », None sinon. Ne lève jamais."""
    if not is_status(text):
        return None
    try:
        return report()
    except Exception as e:                              # noqa: BLE001
        print(f"[status] rapport impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return "⚠️ Kelly n'a pas pu faire son rapport d'état. Réessaie dans une minute."
