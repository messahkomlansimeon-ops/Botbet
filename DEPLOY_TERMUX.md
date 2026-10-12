# Déploiement et Exploitation sur Android / Termux (GrapheneOS)

Ce document décrit l'architecture et les procédures d'exploitation de Kelly hébergée 24h/24 et 7j/7 sur un smartphone dédié sous GrapheneOS via Termux.

---

## 1. Vue d'ensemble de l'architecture

- **Nœud de production (Master)** : Smartphone sous GrapheneOS avec Termux. Il tourne 24h/24, écoute Telegram et exécute toutes les tâches planifiées.
- **Coût** : **0 FCFA** (aucun VPS payant requis).
- **Nœud de développement / Secours** : PC de travail (`/home/.../kelly`), synchronisé via Git, services d'écoute désactivés par défaut pour éviter les conflits d'écoute Telegram.

---

## 2. L'orchestrateur Termux (`termux_runner.py`)

Sur Android/Termux, `systemd` et `crond` ne sont pas disponibles par défaut. 
L'orchestrateur centralisé en Python ([`termux_runner.py`](termux_runner.py)) remplace systemd et pilote l'ensemble des tâches en boucle continue :

1. **Écoute Telegram continue (`bot.py --serve`)** :
   - Lancé en sous-processus d'arrière-plan dès le démarrage ; surveillé en permanence par un fil dédié, **même pendant un passage long** (le matin dure ~30 min).
   - Relancé automatiquement s'il s'arrête. Arrêts rapprochés (< 2 min de vie) : attente croissante 15 s, 30 s, 1 min... plafonnée à 15 min, avec une ligne claire dans `logs/termux.log` (un `bot.py` cassé par un `git pull` ne boucle plus toutes les 15 s).
2. **Suivi des tickets en direct (`--watch`)** :
   - Déclenché toutes les 10 minutes.
   - Lit les matchs en cours via 365scores pour mettre à jour les tickets et envoyer les alertes en direct.
3. **Passage horaire (`--track --own-lock`)** :
   - Exécuté chaque heure à la 5e minute (ex: 11:05, 12:05... UTC).
   - Effectue la clôture des cotes, ouvre la fenêtre des stats 1xBet et actualise FotMob.
4. **Passage du matin (`--daily`)** :
   - Exécuté chaque jour dès 04:00 UTC (04h00 Abidjan), **avec rattrapage** : téléphone éteint ou runner relancé à 05:00 -> le matin part dès que le runner est de retour (jusqu'à 20:00 UTC, 3 essais espacés de 30 min en cas d'échec). Un matin déjà fait (même à la main) est ignoré par `bot.py` lui-même (`data/daily_done.json`) : jamais de doublon.
   - Tient le verrou des données pendant tout le passage : un « Je l'ai joué » ou un budget envoyé pendant ce temps reçoit « Kelly est en plein travail » et est traité à la fin (plus d'écrasement).
   - Règle les paris de la veille, télécharge les sources gratuites, analyse les matchs du monde, génère les pronostics et combinés du matin.
5. **Relevé de l'après-midi (`--xbet`)** : chaque jour à 15:00 UTC (cotes 1xBet des matchs à venir).
6. **Relevé du soir (`--evening`)** : chaque jour à 18:00 UTC.
7. **Sauvegarde (`--export --own-lock`)** : chaque jour à 23:00 UTC, copie lisible de la base dans `data/sauvegarde/`.
8. **Réglage hebdomadaire (`--tune`)** : chaque dimanche entre 02:00 et 04:00 UTC (avant le matin).

**Robustesse du runner** :
- **Une seule instance** : verrou `logs/runner.lock` ; un second `termux_runner.py` refuse de démarrer. Au démarrage, un ancien `bot.py --serve` resté orphelin (runner tué brutalement, `logs/serve.pid`) est arrêté avant d'en lancer un autre (sinon conflits Telegram 409).
- **Arrêt propre** : SIGTERM, SIGHUP (session fermée, sauf sous `nohup`) et Ctrl+C arrêtent `--serve` et le passage en cours.
- **Délais maximaux** par passage (watch 9 min, track/xbet/evening 25 min, export 15 min, tune 40 min, daily 90 min) : un appel réseau figé ne bloque plus le reste.
- **Chemins absolus** : `termux_runner.py` peut être lancé depuis n'importe quel dossier (il travaille dans le dépôt).
- L'avancement du planning est gardé dans `logs/runner_state.json` (pas de relance en double d'un `--xbet` ou `--export` après un redémarrage). `logs/` est ignoré par git.

---

## 3. L'outil CLI Termux (`kelly`)

Sur le téléphone, un script wrapper a été installé dans `/data/data/com.termux/files/usr/bin/kelly` pour administrer le bot en une seule commande depuis n'importe où dans le terminal :

```bash
kelly <commande>
```

### Commandes disponibles :

- `kelly update` : Met à jour le code depuis GitHub (`git pull`) et redémarre le bot proprement.
- `kelly start` : Lance `termux_runner.py` en arrière-plan avec `nohup`.
- `kelly stop` : Arrête proprement le runner et tous les processus python du bot.
- `kelly restart` : Arrête puis redémarre le runner.
- `kelly logs` : Suit les logs en temps réel (`tail -f logs/termux.log`).
- `kelly status` : Vérifie si le runner et le bot sont actifs (affiche les PIDs).

### Code du wrapper (`/data/data/com.termux/files/usr/bin/kelly`) :

```bash
#!/data/data/com.termux/files/usr/bin/bash
DIR="$HOME/kelly"
LOG="$DIR/logs/termux.log"

case "$1" in
    start)
        cd "$DIR" || exit 1
        mkdir -p logs
        if pgrep -f "termux_runner.py" > /dev/null; then
            echo "⚠️ Kelly est déjà en cours d'exécution."
        else
            nohup python -u termux_runner.py > "$LOG" 2>&1 &
            echo "🚀 Kelly démarrée en arrière-plan."
        fi
        ;;
    stop)
        pkill -f "termux_runner.py"
        pkill -f "bot.py"
        echo "🛑 Kelly arrêtée."
        ;;
    restart)
        $0 stop
        sleep 2
        $0 start
        ;;
    update)
        cd "$DIR" || exit 1
        echo "🔄 Mise à jour du dépôt..."
        git pull && $0 restart
        ;;
    logs)
        tail -n 50 -f "$LOG"
        ;;
    status)
        if pgrep -f "termux_runner.py" > /dev/null; then
            echo "✅ Kelly est active (runner PID: $(pgrep -f termux_runner.py | head -n1))"
        else
            echo "❌ Kelly est arrêtée."
        fi
        ;;
    *)
        echo "Usage: kelly {start|stop|restart|update|logs|status}"
        exit 1
        ;;
esac
```

---

## 4. Maintien en veille (GrapheneOS / Android)

Pour que le bot continue de tourner lorsque l'écran du téléphone est éteint :
1. **Wake-lock Termux** :
   - Exécuter la commande `termux-wake-lock` dans Termux. Cela empêche le noyau Android de suspendre le CPU (partial wake lock).
2. **Optimisation de la batterie** :
   - Dans les paramètres Android de GrapheneOS : `Applications > Termux > Batterie > Non restreinte` (Unrestricted).

---

## 5. Stratégie Réseau & Proxy Webshare (`XBET_PROXY`)

Le fichier `.env` sur le téléphone et le PC contient la clé :
```env
XBET_PROXY=http://username:password@p.webshare.io:80
```

### Pourquoi un proxy ?
- **Isolement d'IP** : Empêche 1xBet de repérer le trafic de requêtes automatisées du bot sur l'adresse IP résidentielle/mobile où l'utilisateur place ses paris avec son compte réel.
- **Contournement des blocages géographiques** : 1xBet et 365scores peuvent restreindre certaines IPs mobiles ou africaines.

### Consommation et quotas (Forfait Webshare gratuit 3 Go / mois) :
- **1xBet** : plafonné à ≈ 400 Mo / mois (`C.XBET_MONTH_MB = 400`).
- **365scores** : plafonné à ≈ 200 Mo / mois (`C.S365_MONTH_MB = 200`).
- **Total consommé sur le proxy** : ≈ 600 Mo / mois sur les 3 000 Mo alloués.
- **FotMob et BetExplorer** : se connectent **en direct sans proxy**. Ces deux sources téléchargent des pages HTML lourdes (tableaux de ligues, résultats, xG) représentant ≈ 100 Mo/jour. Les passer par le proxy saturerait les 3 Go en moins de 10 jours. Leurs requêtes sont publiques et ne présentent aucun risque pour le compte de paris.

---

## 6. Sauvegarde et Base de données (`/backup`)

- **Base de données** : SQLite unifiée dans `data/kelly.db` (≈ 15 à 16 Mo).
- **Commande `/backup` sur Telegram** :
  - Compresse à la volée `data/kelly.db` en format ZIP (`data/kelly_backup.zip`, ≈ 1,8 Mo, soit une réduction de taille par 9).
  - Télécharge le fichier compressé directement dans la conversation Telegram avec un timeout réseau de 300 s (résilient aux coupures des réseaux mobiles 4G/3G).
- **Restauration** :
  - Sur PC ou nouveau téléphone, dézipper `kelly_backup.zip` dans `data/kelly.db`.
  - Ou exécuter `python bot.py --import data/sauvegarde/`.

---

## 7. Gestion de Git & Environnement (.env)

- Le fichier `.env` contient les secrets réels (`ODDS_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `STATS_API_KEY`, `XBET_PROXY`).
- `.env` est **exclu de git** (`.gitignore`) et son suivi a été dissocié du dépôt (`git rm --cached .env`).
- Cela garantit que `git pull` (ou `kelly update`) s'exécute **sans jamais provoquer de conflit de fusion** sur la configuration locale.
- Le dépôt Termux a son URL distante configurée avec un Personal Access Token (PAT) GitHub pour ne jamais bloquer sur une demande d'identifiants.
