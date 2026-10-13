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
   **Sauvegarde sur Telegram (`--backup-auto`)** : tous les 2 jours dès 12:00 UTC, la base (vérifiée, zippée, `kelly_backup_<date>.zip`) arrive dans la conversation **sans sonnerie** ; envoi raté (réseau) : nouvel essai toutes les heures. Elle ne contient jamais `.env`.
8. **Réglage hebdomadaire (`--tune`)** : chaque dimanche entre 02:00 et 04:00 UTC (avant le matin).

**Jamais mort en silence** :
- **Garde** (`kelly guard`, lancée par `kelly start`) : toutes les 5 min, relance Kelly si elle s'est arrêtée sans `kelly stop` (plantage, processus tué). `kelly stop` l'arrête aussi (fichier `logs/stopped`).
- **Termux:Boot** (appli F-Droid, à ouvrir une fois) : `kelly start` écrit `~/.termux/boot/kelly`, qui relance Kelly au démarrage du téléphone, après le premier déverrouillage. Sur un téléphone dédié à Kelly, désactiver le **redémarrage automatique** de GrapheneOS (Paramètres, chercher « redémarrage automatique » / « auto reboot ») : après un redémarrage, le téléphone reste verrouillé et rien ne peut tourner tant qu'il n'a pas été déverrouillé une fois.
- **Surveillance extérieure (healthchecks.io)** : le runner envoie un signal de vie toutes les 5 min, **toujours par le proxy** (jamais en direct). Sans signal pendant 1 h (téléphone éteint, sans réseau, Termux tué), healthchecks.io prévient sur Telegram par son propre bot ; l'écoute Telegram de Kelly arrêtée depuis 15 min : alerte « en panne » tout de suite. Réglage : sur le site, un « check » avec Période 15 min et Délai de grâce 45 min, intégration Telegram ; son adresse de ping dans `.env` (`HC_PING_URL=https://hc-ping.com/…`), puis `kelly restart`. Vérifier : `kelly test-alerte` (alerte « en panne » de test, puis « ok » 5 min après).
- **Mise à jour sûre** : `kelly update` teste la nouvelle version (imports et matin d'essai hors ligne) avant de la lancer ; si elle ne marche pas, retour automatique à la version d'avant et message sur Telegram. Le miroir `data/sauvegarde/` n'est jamais touché par git pendant la mise à jour.
- `kelly doctor` montre la garde, Termux:Boot et la surveillance extérieure.
- **Alertes Telegram du runner** (envoyées directement, sans le code de Kelly, une fois chacune, gardées si le réseau manque, sans sonnerie aux heures calmes) : écoute Telegram qui plante depuis 15 min (puis « rétablie »), passage du matin raté 3 fois ou pas tourné, sauvegarde Telegram qui ne part plus (6 essais), moins de 200 Mo libres, base abîmée (contrôle chaque jour à 03:00 UTC).
- **🩺 Status** montre « 📱 Kelly sur le téléphone » : programme principal, matin, sauvegarde, dernier signal de vie, garde et relance au démarrage, alertes en attente ; plus 1xBet (relevé coupé, format suspect) et la jauge du forfait Webshare.
- **« ⚠️ Ce matin »** en tête des messages du matin quand quelque chose cloche : journée inhabituelle (moitié moins de matchs, de championnats, de cotes… que d'habitude), relevé 1xBet coupé, proxy qui refuse (mot de passe, forfait).

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
- `kelly logs-avant` : Le journal du lancement précédent (gardé à chaque `kelly start` / `restart` / `update` ; au-delà de 5 Mo, le journal est aussi tourné en `termux.log.1`).
- `kelly status` : Vérifie si le runner et le bot sont actifs (affiche les PIDs).
- `kelly test-alerte` : vérifie la surveillance extérieure (healthchecks.io doit t'écrire sur Telegram).
- `kelly restaurer [fichier.zip]` : installe une sauvegarde reçue sur Telegram (sans fichier : la plus récente de tes téléchargements). Kelly est arrêtée pendant ce temps puis relancée ; la sauvegarde est vérifiée avant d'être installée, et l'ancienne base gardée à côté (`data/kelly.db.avant-restauration-…`).

### Le wrapper (`$PREFIX/bin/kelly`)

Le script est dans le dépôt : `scripts/kelly`. Installation ou mise à jour, une seule ligne à coller dans Termux :

```bash
cd ~/kelly && git pull && cp scripts/kelly $PREFIX/bin/kelly && chmod +x $PREFIX/bin/kelly && kelly restart
```

Ensuite, `kelly update` fait tout seul `git pull`, réinstalle le script et redémarre Kelly. `stop` attend que le runner et `bot.py` soient vraiment arrêtés (20 s au plus), et le runner lui-même attend jusqu'à 30 s le verrou `logs/runner.lock` au démarrage : un redémarrage rapide ne laisse jamais Kelly arrêtée.

---

## 4. Maintien en veille (GrapheneOS / Android)

En veille profonde, Android coupe le réseau et ralentit Termux : les logs montrent alors des `[telegram] lecture des commandes échouée : … Read timed out`, Kelly répond en retard et les passages programmés peuvent glisser. Pour l'éviter :

1. **Wake-lock** : `kelly start` (et donc `kelly restart` / `kelly update`) appelle maintenant `termux-wake-lock` tout seul, et `kelly stop` le relâche. Le journal l'indique au démarrage (« wake-lock demandé »). À défaut : notification Termux → « Acquire wakelock ».
2. **Batterie** : Paramètres → Applications → Termux → Batterie → **Non restreinte**.
3. **Réseau (GrapheneOS)** : Paramètres → Applications → Termux → autorisation **Réseau** activée, et **données en arrière-plan** autorisées.
4. **Après un redémarrage du téléphone** : relancer `kelly start`, ou l'automatiser avec l'appli **Termux:Boot** (F-Droid) : créer `~/.termux/boot/kelly` contenant `#!/data/data/com.termux/files/usr/bin/sh` puis `kelly start`, et le rendre exécutable (`chmod +x`).
5. **Wi-Fi** : si le réglage existe, garder le Wi-Fi actif pendant la veille.

`kelly doctor` vérifie tout ça d'un coup : runner actif, wake-lock demandé, dernier passage du matin, nombre de coupures Telegram récentes, et rappelle les réglages Android.

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
- **Sauvegarde automatique** : tous les 2 jours vers 12:00 UTC, sans sonnerie (voir §2) ; la légende donne la date, la taille, le nombre de paris notés et le capital.
- **Ce qui n'est PAS dedans : `.env`** (token Telegram, clés, proxy). Garde son contenu ailleurs (gestionnaire de mots de passe) : sans lui, un nouveau téléphone ne peut pas faire tourner Kelly.
- **Téléphone perdu ou en panne** :
  1. installer Termux et Kelly sur le nouveau téléphone (ce guide) ;
  2. remettre le fichier `.env` dans `~/kelly/` ;
  3. dans Telegram, ouvrir la dernière sauvegarde `kelly_backup_<date>.zip` pour la télécharger ;
  4. `kelly restaurer` (la plus récente des téléchargements est trouvée toute seule ; sinon `kelly restaurer <chemin du fichier>`).
- Ou, sur PC : dézipper `kelly.db` dans `data/`, ou `python bot.py --import data/sauvegarde/`.

---

## 7. Gestion de Git & Environnement (.env)

- Le fichier `.env` contient les secrets réels (`ODDS_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `STATS_API_KEY`, `XBET_PROXY`).
- `.env` est **exclu de git** (`.gitignore`) et son suivi a été dissocié du dépôt (`git rm --cached .env`).
- Cela garantit que `git pull` (ou `kelly update`) s'exécute **sans jamais provoquer de conflit de fusion** sur la configuration locale.
- Le dépôt Termux a son URL distante configurée avec un Personal Access Token (PAT) GitHub pour ne jamais bloquer sur une demande d'identifiants.
