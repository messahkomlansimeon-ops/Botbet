"""Kelly : point d'entrée.

  python bot.py --daily      matin : actualise les stats gratuites, règle hier, bilan, combinés du jour (Telegram)
                             déjà fait aujourd'hui = ignoré ; --force le refait. Code 1 si aucun message n'a pu partir (en attente)
  python bot.py --track      toutes les heures de matchs : clôture des paris envoyés + alertes de cote, puis stats de match FotMob
  python bot.py --snapshot   relevé des cotes du lendemain (désactivé sur forfait gratuit : budget 0)
  python bot.py --alert      balayage large pour alertes (désactivé sur forfait gratuit : budget 0)
  python bot.py --commands   traite les commandes Telegram reçues (une passe)
  python bot.py --serve      écoute les commandes en continu (serveur)
  python bot.py --check      diagnostic des clés + budget de crédits (aucun crédit dépensé) + message de test
  python bot.py --tune       réglage sur l'historique football-data en cache (demi-vie par ligue, poids des buts) -> data/tuning.json ;
                             budget TUNE_SECONDS (1800 s), reprend où il s'est arrêté ; --force refait tout. À planifier chaque semaine.
                             Mode séparé : ne touche ni aux paris ni à Telegram, ne plante jamais le run du matin.
  python bot.py --export     copie lisible de la base (data/kelly.db) en JSON dans data/sauvegarde/ (la sauvegarde de 23:00 la commite)
  python bot.py --import [dossier]  restaure la base depuis une copie JSON (data/sauvegarde/ par défaut) ; sous le verrou des données
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
    elif "--tune" in argv:
        from kelly import tune
        sys.exit(tune.main(argv))
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
    elif "--export" in argv:                # lancé sous flock par kelly-backup : pas de data_lock ici (inter-blocage)
        from kelly import store
        print(f"{store.export()} document(s) copié(s) dans {store.backup_dir()}")
    elif "--import" in argv:
        from kelly import store
        i = argv.index("--import")
        src = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else None
        with store.data_lock(wait=900, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):
            print(f"{store.import_dir(src)} document(s) restauré(s) depuis {src or store.backup_dir()}")
    else:
        sys.exit(app.daily_entry(sample, force="--force" in argv))


if __name__ == "__main__":
    main(sys.argv[1:])
