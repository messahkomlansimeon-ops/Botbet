"""🧾 Résumé du journal de Kelly (« erreurs » sur Telegram, `kelly erreurs` dans Termux) : les erreurs et avertissements des dernières heures, regroupés
(« 12× [1xbet] proxy en panne… »), les plantages (Traceback) avec leur ligne de code, et quelques compteurs (démarrages, écoute relancée, passages trop
longs). À coller tel quel pour un contrôle : rien de secret (token, mot de passe du proxy, adresse healthchecks, clés : masqués).

Le journal : logs/termux.log (et termux.log.1, l'ancien, quand il a été tourné ou après un redémarrage). Seules les lignes du runner portent l'heure
(« [termux 2026-10-13 12:01:27Z] … ») : les lignes des passages (bot.py) prennent celle de la dernière ligne datée qui les précède."""
import os
import re
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = re.compile(r"^\[termux (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z\] ")
BAD = re.compile(r"traceback|error|erreur|échec|échou|impossible|ignoré|abandonn|refus|interrompu|dépasse|bloqu|panne|⚠️|❌|exception|killed",
                 re.I)
FINE = ("[progression]", "✅", "rétabli", "aucun avertissement", "0 erreur")      # lignes normales malgré un mot qui inquiète
SECRETS = ((re.compile(r"bot\d+:[\w-]+"), "bot***"), (re.compile(r"://[^/\s@]+@"), "://***@"), (re.compile(r"hc-ping\.com/[\w-]+"), "hc-ping.com/***"),
           (re.compile(r"(apikey|api_key|apiKey|key|token|password|pass)=[^&\s]+", re.I), r"\1=***"))
TOP, WIDTH = 20, 150


def scrub(line):
    for rx, rep in SECRETS:
        line = rx.sub(rep, line)
    return line


def paths(root=ROOT):
    logs = os.path.join(root, "logs")
    return [p for p in (os.path.join(logs, "termux.log.1"), os.path.join(logs, "termux.log")) if os.path.exists(p)]


def _read(files):
    """[(instant ou None, ligne)] dans l'ordre : l'heure de la dernière ligne datée."""
    out, at = [], None
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    m = STAMP.match(line)
                    if m:
                        at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        line = line[m.end():]
                    out.append((at, line))
        except OSError:
            continue
    return out


def _key(line):
    """Sorte d'une ligne : chiffres et identifiants effacés (« Hansa - Meppen : proxy… » et « Saarbrücken - Duisburg : proxy… » restent deux lignes, mais
    les heures, numéros de match et tailles ne séparent plus)."""
    return re.sub(r"\d+([.,]\d+)?", "#", line.strip())[:90]


def _short(path):
    """« /data/…/kelly/kelly/app.py » -> « kelly/app.py » ; ailleurs le nom du fichier seul (bot.py, une bibliothèque)."""
    return "kelly/" + path.split("/kelly/kelly/")[-1] if "/kelly/kelly/" in path else os.path.basename(path)


def digest(hours=24, now=None, files=None):
    """Le résumé (texte brut, prêt à coller)."""
    now = now or datetime.now(timezone.utc)
    files = paths() if files is None else files
    if not files:
        return "🧾 Pas de journal ici (logs/termux.log) : la commande sert sur le téléphone, où Kelly tourne."
    since = now - timedelta(hours=hours)
    rows = [(at, ln) for at, ln in _read(files) if at is None or at >= since]
    rows = [(at, ln) for at, ln in rows if at is not None]       # avant la première ligne datée : trop vieux pour être situé
    groups, crashes, n_bad = {}, {}, 0
    starts = sum(1 for _, ln in rows if "Démarrage de Kelly" in ln)
    serve = sum(1 for _, ln in rows if "--serve s'est arrêté" in ln)
    slow = sum(1 for _, ln in rows if " dépasse " in ln and "interrompu" in ln)
    i = 0
    while i < len(rows):
        at, ln = rows[i]
        if ln.startswith("Traceback"):
            j, frame = i + 1, ""
            while j < len(rows) and (rows[j][1].startswith((" ", "\t")) or not rows[j][1].strip()):
                if rows[j][1].strip().startswith("File "):
                    frame = rows[j][1].strip()
                j += 1
            exc = rows[j][1].strip() if j < len(rows) else "?"
            m = re.search(r'File "([^"]+)", line (\d+), in (\S+)', frame)
            where = f"{_short(m.group(1))} ligne {m.group(2)} ({m.group(3)})" if m else ""
            k = (exc.split(":")[0], where)
            c = crashes.setdefault(k, [0, at, scrub(exc)[:WIDTH], where])
            c[0], c[1] = c[0] + 1, at
            i = j + 1
            continue
        if BAD.search(ln) and not any(f in ln for f in FINE):
            n_bad += 1
            g = groups.setdefault(_key(scrub(ln)), [0, at, scrub(ln.strip())[:WIDTH]])
            g[0], g[1], g[2] = g[0] + 1, at, scrub(ln.strip())[:WIDTH]
        i += 1
    first = rows[0][0] if rows else since
    out = [f"🧾 Journal de Kelly — {hours} h ({max(first, since):%d/%m %Hh%M} → {now:%d/%m %Hh%M} UTC)",
           f"Démarrages : {starts} · écoute Telegram relancée : {serve} · passages trop longs : {slow}"]
    if crashes:
        out += ["", f"💥 Plantages : {sum(c[0] for c in crashes.values())}"]
        for cnt, at, exc, where in sorted(crashes.values(), key=lambda c: -c[0])[:TOP]:
            out.append(f"• {cnt}× {exc}" + (f" — {where}" if where else "") + f" (dernier {at:%d/%m %Hh%M})")
    if groups:
        out += ["", f"⚠️ {n_bad} ligne{'s' if n_bad > 1 else ''} d'erreur ou d'avertissement, {len(groups)} sorte{'s' if len(groups) > 1 else ''} :"]
        for cnt, at, ex in sorted(groups.values(), key=lambda g: (-g[0], g[2]))[:TOP]:
            out.append(f"• {cnt}× {ex} (dernière {at:%d/%m %Hh%M})")
        if len(groups) > TOP:
            out.append(f"… et {len(groups) - TOP} autre(s) sorte(s)")
    if not crashes and not groups:
        out += ["", "✅ Aucune erreur ni avertissement sur la période."]
    return "\n".join(out)


def command(args=()):
    """« erreurs » (24 h), « erreurs 48 » (48 h, 1 à 168)."""
    hours = 24
    for a in args or ():
        if str(a).isdigit():
            hours = max(1, min(168, int(a)))
    return digest(hours)
