"""Kelly : point d'entrée.

  python bot.py --daily      matin : actualise les stats gratuites, règle hier, bilan, combinés du jour (Telegram)
  python bot.py --track      toutes les heures de matchs : clôture des paris envoyés + alertes de cote, puis stats de match FotMob
  python bot.py --snapshot   relevé des cotes du lendemain (désactivé sur forfait gratuit : budget 0)
  python bot.py --alert      balayage large pour alertes (désactivé sur forfait gratuit : budget 0)
  python bot.py --commands   traite les commandes Telegram reçues (une passe)
  python bot.py --serve      écoute les commandes en continu (serveur)
  python bot.py --check      diagnostic des clés + budget de crédits (aucun crédit dépensé) + message de test
  python bot.py --sample     test hors ligne avec données fictives (--daily par défaut)
"""
import os
import sys
import tempfile

# Clés : secrets GitHub / variables d'environnement d'abord, sinon le fichier .env.
from kelly import envfile  # noqa: E402

if "--sample" not in sys.argv:          # le mode test n'utilise jamais les vraies clés (pas d'envoi Telegram)
    envfile.load()

# Mode test : données fictives dans un dossier temporaire, jamais dans data/.
if "--sample" in sys.argv and not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-sample-")

from kelly import app  # noqa: E402


def main(argv):
    sample = "--sample" in argv
    if "--check" in argv:
        sys.exit(0 if app.run_check() else 1)
    elif "--snapshot" in argv:
        app.run_snapshot()
    elif "--track" in argv:
        app.run_track()
        app.fill_match_stats()
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
