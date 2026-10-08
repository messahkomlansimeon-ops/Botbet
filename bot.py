"""Botbet : point d'entrée.

  python bot.py --daily      matin : règle hier, bilan, combinés du jour (Telegram)
  python bot.py --snapshot   soir : relevé des cotes du lendemain
  python bot.py --alert      journée : alerte si une cote chute
  python bot.py --commands   traite les commandes Telegram reçues (une passe)
  python bot.py --serve      écoute les commandes en continu (serveur)
  python bot.py --sample     test hors ligne avec données fictives (--daily par défaut)
"""
import sys

from botbet import app


def main(argv):
    sample = "--sample" in argv
    if "--snapshot" in argv:
        app.run_snapshot()
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
