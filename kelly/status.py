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
MAX_LINE = 170                      # une ligne de source plus longue (liste d'avertissements) est coupée : le détail est dans « bot.py --check »
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


def odds_line():
    """The Odds API en UNE ligne, sans le tour de ligues de app._check_odds (odds.scan_today : jusqu'à 150 s cache froid, dans la boucle qui répond
    à Telegram) : les crédits déjà connus de ce processus (odds.CREDITS, lus dans les en-têtes du dernier appel), sinon UN appel /sports (gratuit,
    jamais hors passage : commande Telegram = hors ligne) pour les lire."""
    from . import odds
    try:
        if odds.CREDITS["remaining"] is None and os.environ.get("ODDS_API_KEY"):
            odds.get("/sports")                         # un seul appel, gratuit : met les crédits à jour
        rem, used = odds.CREDITS["remaining"], odds.CREDITS["used"]
        if not os.environ.get("ODDS_API_KEY"):
            return "❌ The Odds API : ODDS_API_KEY absente"
        if rem is None:
            return "✅ The Odds API : clé présente (crédits connus au prochain passage)"
        return f"✅ The Odds API : {rem} crédits restants ({used if used is not None else '?'} utilisés ce mois)"
    except odds.OddsError as e:
        if e.code == "OFFLINE":
            return "✅ The Odds API : clé présente (crédits connus au prochain passage)"
        return f"❌ The Odds API : {e}"
    except Exception as e:                              # noqa: BLE001
        return f"⚠️ The Odds API : vérification impossible ({type(e).__name__})"


def api_lines():
    """The Odds API, API-Football, données du proxy (1xBet, 365scores), Telegram : une ligne chacun."""
    from . import app
    from .sources import s365, xbet
    out = []
    out.append(odds_line())
    try:
        ok, lines = app._check_stats()
        out.append(lines[0])
    except Exception as e:                              # noqa: BLE001
        out.append(f"⚠️ API-Football : vérification impossible ({type(e).__name__})")
    try:
        p = xbet.proxy()
        if not p and not C.XBET_DIRECT:
            out.append("❌ Proxy 1xBet : non configuré (XBET_PROXY manquant dans .env) — cotes 1xBet coupées")
        else:
            u = xbet.usage()
            out.append(f"{'✅' if u['bytes'] < 0.9 * C.XBET_MONTH_MB * 1e6 else '⚠️'} 1xBet via proxy : {u['bytes'] / 1e6:.1f} Mo ce mois "
                       f"sur {C.XBET_MONTH_MB:.0f} ({u['requests']} requêtes)")
        out.append("✅ " + s365.usage_line())
    except Exception as e:                              # noqa: BLE001
        out.append(f"⚠️ Proxy : compteurs illisibles ({type(e).__name__})")
    try:
        ok, lines, _ = app._check_telegram_bot()
        out.append(lines[0])
    except Exception as e:                              # noqa: BLE001
        out.append(f"⚠️ Telegram : vérification impossible ({type(e).__name__})")
    return out


def _state(n, w, s, need, k=None):
    """« 22/60 · 76 % gagnés pour 66 % annoncés · trop prudente, correction dans 38 résultats »."""
    won, said = w / n, s / n
    mood = "trop confiante" if won < said - 0.05 else "trop prudente" if won > said + 0.05 else "juste"
    when = f"correction dans {need - n} résultat{'s' if need - n > 1 else ''}" if n < need else (
        f"correction active (×{k:.2f})".replace(".", ",") if k is not None else "correction active")
    from . import fmt
    count = f"{n}/{need}" if n < need else f"{n} réglés"
    return f"{count} · {fmt.pct(won)} gagnés pour {fmt.pct(said)} annoncés · {mood}, {when}"


def learning_lines(top=6):
    """Section « 🧠 Apprentissage » : où en est la correction de Kelly, avant-match (calib, par famille), ensemble (buts, 1N2…) et live (par famille
    et par tranche, sur les paris retenus). [] s'il n'y a encore rien de réglé."""
    from . import calib, ensemble, live
    out = []
    d = calib.load()
    fams = sorted(((f, a) for f, a in d.items() if not f.startswith("_") and isinstance(a, list) and a[0]), key=lambda x: -x[1][0])
    for f, (n, w, s) in fams[:top]:
        out.append(f"• {calib.LABELS.get(f, f).capitalize()} : " + _state(n, w, s, calib.min_need(f), calib._k([n, w, s], fam=f)))
    ens = [(f, ensemble.weights(f)) for f in ("goals", "dc", "btts", "h2h")]
    ens_txt = ", ".join(f"{calib.LABELS.get(f, f)} {int(w['n_m'])}/{int(C.ENSEMBLE_MIN_N)}" for f, w in ens)
    out.append(f"• Ensemble (avis de Kelly face au marché) : {ens_txt}")
    ld = live.calib_data()
    tr = sorted(((k, a) for k, a in ld.items() if a[0]), key=lambda x: -x[1][0])
    for k, (n, w, s) in tr[:4]:
        fam, t = k.split("|")
        out.append(f"• Live {calib.LABELS.get(fam, fam)} {t} : " + _state(n, w, s, C.FAIR_CALIB_MIN, calib._k([n, w, s])))
    if not tr:
        out.append("• Live : aucun pari live retenu et réglé pour l'instant")
    return out if fams or ld else []


def report(now=None):
    """Le message « status » (texte brut)."""
    from . import sources
    now = now or datetime.now(timezone.utc)
    parts = [f"🩺 État de Kelly — {now:%Hh%M}"]
    try:
        st = progress.describe()
        if st.startswith(("⏳", "⚠️")):                  # passage du matin en cours ou interrompu : en premier
            parts += ["", st]
    except Exception:                                   # noqa: BLE001
        pass
    svc = service_lines(now)
    if svc:
        parts += ["", "⚙️ Services"] + svc
    try:
        src = [line if len(line) <= MAX_LINE else line[:MAX_LINE - 1].rstrip() + "…" for line in sources.report().splitlines()]
        parts += [""] + src
    except Exception as e:                              # noqa: BLE001
        parts += ["", f"⚠️ Sources de stats : rapport illisible ({type(e).__name__})"]
    try:
        learn = learning_lines()
        if learn:
            parts += ["", "🧠 Apprentissage"] + learn
    except Exception as e:                              # noqa: BLE001
        parts += ["", f"⚠️ Apprentissage : illisible ({type(e).__name__})"]
    parts += ["", "🔌 API et quotas"] + api_lines()
    bad = sum(1 for p in parts if p.startswith("❌"))     # ⛔ (source bloquée par son site, comme FBref) : dit, mais rien à faire de ton côté
    parts += ["", "Tout va bien 👍" if not bad else f"{bad} point{'s' if bad > 1 else ''} à regarder (❌ ci-dessus)."]
    return "\n".join(parts)


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
