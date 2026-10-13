"""Contrôle du suivi des matchs et des notifications (`kelly suivi`, ou `python tools/suivi.py` depuis le dossier de Kelly) : runner et bot Telegram
en marche, version déployée, dernier passage du suivi (toutes les 10 min), 365scores prêt, heures calmes, messages Telegram en attente, matchs à suivre.
Lecture seule : aucune requête, aucun message envoyé, rien d'écrit."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from kelly import config as C, envfile  # noqa: E402

envfile.load()                                   # mêmes réglages que le bot (proxy, heures calmes) ; rien n'est affiché
from kelly import exposure, quiet, store, telegram, watch  # noqa: E402
from kelly.sources import s365  # noqa: E402

FIX = "f9435c9"                                  # correctif « suivi des matchs des tickets perdus » (au moins)


def ok(b):
    return "✅" if b else "❌"


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def running(pattern):
    """Un processus de Kelly dont la ligne de commande contient `pattern` tourne-t-il (hors ce contrôle et ses parents) ?"""
    mine, pid = set(), os.getpid()
    while pid > 1:                                   # ce script et les shells qui l'ont lancé
        mine.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return any(int(p) not in mine for p in out if p.isdigit())


def main():
    now = datetime.now(timezone.utc)
    print(ok(running("termux_runner.py")), "Kelly (runner) en marche")
    print(ok(running("bot.py --serve")), "bot Telegram (--serve) en marche")
    v = run("git log -1 --format=%h")
    print(ok(run(f"git merge-base --is-ancestor {FIX} HEAD && echo y") == "y"), "version", v, "(correctif du suivi inclus)")
    print(ok(C.WATCH_AFTER_LOSS), "suivi des matchs des tickets déjà perdus :", "oui" if C.WATCH_AFTER_LOSS else "non")
    try:
        with open(os.path.join("logs", "runner_state.json"), encoding="utf-8") as f:
            age = (time.time() - json.load(f).get("watch_at", 0)) / 60
        print(ok(age < 15), f"dernier passage du suivi il y a {age:.0f} min (un toutes les 10 min)")
    except (OSError, ValueError):
        print("❌ dernier passage du suivi inconnu (logs/runner_state.json illisible)")
    with s365._via_proxy(settle=True) as why:   # aucune requête : vérifie seulement le proxy et le plafond du mois
        print(ok(not why), "365scores pour le suivi :", why or "prêt (" + s365.usage_line() + ")")
    calm = quiet.is_quiet(now)
    print(ok(not calm), f"heures calmes ({C.QUIET_HOURS} UTC) :", "en ce moment, les messages sont gardés pour le réveil" if calm else "pas maintenant")
    box = store.load(telegram.OUTBOX, [])
    print(ok(not box), "messages Telegram en attente d'envoi :", len(box))
    from kelly.sources import state as src_state
    bl = src_state.health("s365").get("blocked_until")
    print(ok(not bl), "365scores", f"BLOQUÉ jusqu'au {str(bl)[:16].replace('T', ' ')} UTC" if bl else "pas bloqué")
    try:
        d = watch._load()
    except watch.WatchStateError as e:
        print("❌ document du suivi illisible :", e)
        return
    h = d.get("health") or {}
    if h.get("at"):
        state_txt = ("secours FotMob (365scores : " + str(h.get("why") or "?") + ")" if h.get("backup") else "en panne : " + str(h.get("why") or "?")) \
            if not h.get("ok") else "en bonne santé"
        print(ok(h.get("ok")), f"santé du suivi au dernier passage ({h['at'][11:16]} UTC) :", state_txt)
    print(ok(not d.get("out")), "lignes du suivi en attente d'envoi :", len(d.get("out") or []))
    b = d.get("budget") or {}
    print("ℹ️ fiches de match lues aujourd'hui :", b.get("detail", 0) if b.get("day") == str(now.date()) else 0, "/", C.WATCH_DETAIL_DAY)
    ts = [t for x in (now.date(), now.date() - timedelta(days=1)) for t in exposure.tickets(str(x))]
    todo = watch._open_legs(ts, d, now)
    print("ℹ️", len(ts), "tickets joués (hier et aujourd'hui) ;", len({l["id"] for _, l, _ in todo}), "matchs à suivre maintenant ou dans l'heure")
    last = max((x.get("at", "") for x in d["legs"].values()), default="")
    print("ℹ️ dernière mise à jour d'un pari par le suivi :", (last[:16].replace("T", " ") + " UTC") if last else "aucune")


if __name__ == "__main__":
    main()
