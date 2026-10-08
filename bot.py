"""Kelly : point d'entrée.

  python bot.py --daily      matin : règle hier, bilan, combinés du jour (Telegram)
  python bot.py --snapshot   soir : relevé des cotes du lendemain
  python bot.py --track      toutes les heures : cote de clôture des matchs imminents + alertes
  python bot.py --alert      journée : alerte si une cote chute (balayage large)
  python bot.py --commands   traite les commandes Telegram reçues (une passe)
  python bot.py --serve      écoute les commandes en continu (serveur)
  python bot.py --sample     test hors ligne avec données fictives (--daily par défaut)
"""
import sys

from kelly import app


def main(argv):
    sample = "--sample" in argv
    if "--snapshot" in argv:
        app.run_snapshot()
    elif "--track" in argv:
        app.run_track()
    elif "--alert" in argv:
        app.run_alert(sample)
    elif "--serve" in argv:
        app.run_commands(serve=True)
    elif "--commands" in argv:
        app.run_commands()
    else:
        app.run_daily(sample)


if __name__ == "__main__":
    main(sys.argv[1:])
