"""Orchestrateur tout-en-un pour Termux (Android / GrapheneOS).
Remplace systemd en exécutant bot.py --serve en continu et en planifiant les passages périodiques (--watch, --track, --daily, --xbet, --evening).
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def run_cmd(args):
    """Lance une commande Kelly sans bloquer le runner indéfiniment."""
    try:
        p = subprocess.run([sys.executable, "-u", "bot.py"] + args, check=False)
        return p.returncode
    except Exception as e:
        print(f"[termux] Erreur {args}: {e}", file=sys.stderr)
        return 1


def main():
    print("🔥 Démarrage de Kelly sur GrapheneOS / Termux...")
    os.makedirs("logs", exist_ok=True)

    # 1. Lance l'écoute des commandes Telegram (--serve)
    serve_proc = subprocess.Popen([sys.executable, "-u", "bot.py", "--serve"])
    print(f"✅ Bot Telegram actif (PID {serve_proc.pid})")

    last_watch = 0
    last_track_hour = -1
    last_daily_day = ""
    last_xbet_day = ""
    last_evening_day = ""

    try:
        while True:
            # Relance automatique de --serve en cas de coupure inattendue
            if serve_proc.poll() is not None:
                print("⚠️ bot.py --serve s'est arrêté, relance immédiate...", file=sys.stderr)
                serve_proc = subprocess.Popen([sys.executable, "-u", "bot.py", "--serve"])

            now = datetime.now(timezone.utc)
            t = time.time()
            day_str = str(now.date())

            # A. --watch toutes les 10 minutes (suivi tickets en direct + alertes)
            if t - last_watch >= 600:
                last_watch = t
                run_cmd(["--watch"])

            # B. --track toutes les heures à la 5e minute (sauf à 04h réservé au daily)
            if now.minute >= 5 and now.hour != last_track_hour and now.hour != 4:
                last_track_hour = now.hour
                run_cmd(["--track", "--own-lock"])

            # C. --daily chaque matin à 04:00 UTC (bilan, combinés du jour, stats)
            if now.hour == 4 and day_str != last_daily_day:
                last_daily_day = day_str
                run_cmd(["--daily"])

            # D. --xbet chaque après-midi à 15:00 UTC (relecture des cotes du jour)
            if now.hour == 15 and day_str != last_xbet_day:
                last_xbet_day = day_str
                run_cmd(["--xbet"])

            # E. --evening chaque soir à 18:00 UTC (relecture des matchs du soir)
            if now.hour == 18 and day_str != last_evening_day:
                last_evening_day = day_str
                run_cmd(["--evening"])

            time.sleep(15)
    except KeyboardInterrupt:
        print("\nArrêt demandé...")
        serve_proc.terminate()


if __name__ == "__main__":
    main()
