"""Kelly : point d'entrée.

  python bot.py --daily      matin : actualise les stats gratuites, règle hier, bilan, combinés du jour (Telegram)
                             déjà fait aujourd'hui = ignoré ; --force le refait. Code 1 si aucun message n'a pu partir (en attente)
                             Prend lui-même le verrou des données pendant tout le passage (les commandes Telegram de --serve attendent : « Kelly est en plein
                             travail ») ; --no-lock s'il est déjà lancé sous `flock` (unité systemd), sinon inter-blocage
  python bot.py --track      toutes les heures de matchs : clôture des paris envoyés + alertes de cote, puis la fenêtre des stats (paris tirs / fautes / touches
                             chez 1xBet des matchs qui commencent dans 45 min à 4 h : relus, remis à la cote, « 🧮 COMBINÉ STATS » + 💎 ; STATS_WINDOW_ENABLED=0 l'éteint),
                             puis stats de match FotMob
                             --own-lock : le processus prend lui-même le verrou des données (pour la clôture et les alertes, puis quelques
                             millisecondes pour choisir et fusionner la fenêtre des stats, puis pour fusionner les pages FotMob lues) au lieu d'être lancé
                             sous `flock` pendant ~3 minutes ;
                             à utiliser SANS le `flock` de l'unité systemd (voir README), sinon inter-blocage
  python bot.py --snapshot   relevé des cotes du lendemain (désactivé sur forfait gratuit : budget 0)
  python bot.py --alert      balayage large pour alertes (désactivé sur forfait gratuit : budget 0)
  python bot.py --commands   traite les commandes Telegram reçues (une passe)
  python bot.py --serve      écoute les commandes en continu (serveur)
  python bot.py --check      diagnostic des clés + budget de crédits (aucun crédit dépensé) + message de test
  python bot.py --tune       réglage sur l'historique football-data en cache (demi-vie par ligue, poids des buts) -> data/tuning.json ;
                             budget TUNE_SECONDS (1800 s), reprend où il s'est arrêté ; --force refait tout. À planifier chaque semaine.
                             Mode séparé : ne touche ni aux paris ni à Telegram, ne plante jamais le run du matin.
  python bot.py --replay     rejoue la saison passée (CSV football-data en cache, aucune requête) : pronostics de stats refaits match par match
                             sans voir l'avenir, réglés, comptés à poids borné dans la fiabilité -> data/calib_boot.json. Reprend où il s'est arrêté ;
                             --force refait tout. Aussi poursuivi après le réglage du dimanche (--tune) tant qu'il n'est pas fini.
  python bot.py --xbet       second relevé des cotes 1xBet (15:00) : pronostics du matin remis aux cotes du moment, message si nouveaux 💎.
                              Prend lui-même le verrou des données (relecture-écriture de today.json pendant tout le relevé) ; --no-lock s'il est déjà lancé sous `flock`
  python bot.py --stats-window  la fenêtre des stats seule (déjà faite par chaque `--track`) : relit les paris tirs / fautes / touches des matchs qui commencent dans
                             45 min à 4 h, « 🧮 COMBINÉ STATS » + 💎 si possible. Prend lui-même le verrou des données (seulement pour choisir puis fusionner) ; --no-lock s'il est déjà sous `flock`
  python bot.py --evening    relecture du SOIR : les matchs du jour Kelly pas encore commencés sont réanalysés (stats du moment, sources en cache, aucun appel
                             payant), remis aux cotes 1xBet et fusionnés dans today.json ; UN message « 🌙 Pronostics de ce soir mis à jour : N 💎 »,
                             aucun combiné envoyé (« combi N » les bâtit). Prend lui-même le verrou des données ; --no-lock s'il est déjà lancé sous `flock`
  python bot.py --watch      toutes les 10 min : suivi de tes tickets pendant les matchs (⚽ buts, ✅/❌ paris décidés, 🏆 ticket gagné), 🔴 opportunités live,
                             alertes gardées pendant les heures calmes (QUIET_HOURS) ; ne prend pas le verrou des données (documents à part)
  python bot.py --export     copie lisible de la base (data/kelly.db) en JSON dans data/sauvegarde/ (la sauvegarde de 23:00 la commite) ;
                             --own-lock : prend lui-même le verrou des données (Termux, sans `flock`)
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
    elif "--replay" in argv:
        from kelly import replay
        sys.exit(replay.main(argv))
    elif "--snapshot" in argv:
        app.run_snapshot()
    elif "--track" in argv:
        if "--own-lock" in argv:
            from kelly import store
            from kelly.sources import fmmatch
            os.environ[fmmatch.OWN_LOCK_ENV] = "1"      # fmmatch.fill télécharge SANS le verrou et ne le reprend que pour fusionner
            try:
                with store.data_lock(wait=600, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):
                    app.run_track()                     # clôture + alertes : lire-modifier-écrire sous le verrou (comme avant sous flock)
            except store.LockTimeout:
                print("Kelly travaille depuis plus de 10 minutes : passage horaire abandonné", file=sys.stderr)
                sys.exit(1)
            app.run_stats_window()                      # paris stats chez 1xBet : verrou pris seulement pour choisir puis fusionner, jamais pendant les requêtes
            try:
                with store.data_lock(wait=120):
                    app.mark_xclose()                   # audit : dernier prix 1xBet (cache disque, aucune requête) des lignes du journal dont le match approche
            except store.LockTimeout:
                print("Kelly travaille : prix 1xBet de clôture sauté pour ce passage", file=sys.stderr)
            app.fill_match_stats()
        else:
            app.run_track()
            app.run_stats_window()
            app.mark_xclose()
            app.fill_match_stats()
    elif "--xbet" in argv:
        if "--no-lock" in argv:                 # déjà sous `flock` (unité systemd) : en reprendre un bloquerait
            app.run_xbet_refresh()
        else:
            from kelly import store
            try:
                with store.data_lock(wait=600, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):
                    app.run_xbet_refresh()
            except store.LockTimeout:
                print("Kelly travaille depuis plus de 10 minutes : relevé 1xBet abandonné", file=sys.stderr)
                sys.exit(1)
    elif "--stats-window" in argv:
        if "--no-lock" not in argv:             # à la main (ou par un timer) : le verrou des données n'est pris que pour choisir puis fusionner, jamais pendant les requêtes 1xBet
            from kelly.sources import fmmatch
            os.environ[fmmatch.OWN_LOCK_ENV] = "1"
        app.run_stats_window()
    elif "--watch" in argv:                     # toutes les 10 min (kelly-watch.timer) : tes tickets pendant les matchs, opportunités live, alertes de la nuit
        from kelly import watch
        watch.tick()
    elif "--evening" in argv:
        if "--no-lock" in argv:                 # déjà sous `flock` (unité systemd) : en reprendre un bloquerait
            app.run_evening()
        else:
            from kelly import store
            try:
                with store.data_lock(wait=600, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):
                    app.run_evening()
            except store.LockTimeout:
                print("Kelly travaille depuis plus de 10 minutes : relecture du soir abandonnée", file=sys.stderr)
                sys.exit(1)
    elif "--alert" in argv:
        app.run_alert(sample)
    elif "--serve" in argv:
        app.run_commands(serve=True)
    elif "--commands" in argv:
        app.run_commands()
    elif "--backup-auto" in argv:           # sauvegarde automatique de la base sur Telegram (termux_runner : tous les 2 jours), sans sonnerie
        from kelly import backup
        err = backup.send(auto=True)
        if err:
            print(f"[sauvegarde] {err}", file=sys.stderr)
            sys.exit(1)
        print("[sauvegarde] envoyée sur Telegram")
    elif "--restore" in argv:               # kelly restaurer [fichier] : installe une sauvegarde reçue sur Telegram (Kelly arrêtée par le script)
        from kelly import backup
        i = argv.index("--restore")
        src = (argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else "") or backup.latest()
        if not src:
            print("❌ Aucune sauvegarde trouvée dans tes téléchargements. Télécharge le dernier fichier « kelly_backup… .zip » reçu sur Telegram,"
                  " puis : kelly restaurer <chemin du fichier>  (termux-setup-storage d'abord si ~/storage n'existe pas)", file=sys.stderr)
            sys.exit(1)
        print(f"📦 Sauvegarde : {src}")
        try:
            print(backup.restore(src))
        except backup.BackupError as e:
            print(f"❌ Restauration impossible : {e}. Ta base actuelle n'a pas été touchée.", file=sys.stderr)
            sys.exit(1)
    elif "--export" in argv:                # lancé sous flock par kelly-backup : pas de data_lock par défaut (inter-blocage) ; --own-lock (Termux, sans flock) le prend
        from kelly import store
        if "--own-lock" in argv:
            try:
                with store.data_lock(wait=600, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):
                    print(f"{store.export()} document(s) copié(s) dans {store.backup_dir()}")
            except store.LockTimeout:
                print("Kelly travaille depuis plus de 10 minutes : sauvegarde abandonnée", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"{store.export()} document(s) copié(s) dans {store.backup_dir()}")
    elif "--import" in argv:
        from kelly import store
        i = argv.index("--import")
        src = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else None
        with store.data_lock(wait=900, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):
            print(f"{store.import_dir(src)} document(s) restauré(s) depuis {src or store.backup_dir()}")
    else:
        force = "--force" in argv
        if sample or "--no-lock" in argv:       # --sample : dossier temporaire, aucun verrou ; --no-lock : déjà sous `flock` (unité systemd), en reprendre un bloquerait
            sys.exit(app.daily_entry(sample, force=force))
        from kelly import store
        try:                                    # tout le matin (~30 min) sous le verrou des données : sans lui, un « Je l'ai joué » ou un budget traité par --serve
            with store.data_lock(wait=1800, reentrant=True, on_busy=lambda: print("Kelly travaille : attente de la fin du passage...")):   # pendant ce temps était écrasé et perdu
                code = app.daily_entry(sample, force=force)
        except store.LockTimeout:
            print("Kelly travaille depuis plus de 30 minutes : passage du matin abandonné", file=sys.stderr)
            sys.exit(1)
        sys.exit(code)


if __name__ == "__main__":
    main(sys.argv[1:])
