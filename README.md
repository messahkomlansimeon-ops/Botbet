# Kelly

Kelly est un bot Telegram personnel de paris sportifs (et oui, elle mise selon le critère de Kelly). Chaque matin elle règle les paris de la veille, dresse le bilan, apprend de la cote de clôture, puis envoie le pari du jour et des combinés agressifs construits en priorité sur les marchés que les bookmakers pricent mal.

## Ce qu'il envoie

**08:00 (Abidjan)**
- 📋 Résultats des paris d'hier, 📊 bilan (taux de réussite, rendement, CLV, par palier et par marché), 💼 bankroll, 🧠 ce qu'elle a appris.
- ⭐ Pari du jour.
- 🛡️ **Sûr** (cote 5 à 7), ⚖️ **Costaud** (12 à 20), 🔥 **Méchant** (40 à 80), 💀 **Monstre** (150+). Le nombre de matchs est libre : il prend la combinaison la plus probable qui atteint la cote visée, un seul pari par match. Chaque combiné vient avec une mise conseillée.

**De 11:05 à 22:05 (UTC), chaque heure** : relevé de la cote de clôture des paris **envoyés** dont le match démarre dans les 90 minutes (voir « Forfait gratuit »), et 🚨 alerte si une cote chute brutalement sur les matchs relevés (l'argent des parieurs pros). Les alertes ne viennent donc que de ces passages : pas de balayage large ni de relevé du soir sur forfait gratuit.

## Commandes Telegram
`/bilan [jours]`, `/match PSG`, `/combi 30` (combiné sur mesure à la cote visée), `/top`, `/bank 50000` (règle ta bankroll), `/aide`. Seul ton chat est écouté.

Les commandes sont lues à chaque passage de GitHub Actions (08:00 puis chaque heure de 11:05 à 22:05) : réponse dans l'heure en journée, au matin pour celles de la nuit. Pour des réponses instantanées, lance `python bot.py --serve` sur un petit serveur. `/bilan` inclut aussi le message 🧠.

## Comment il choisit
Sports : football (5 grands championnats + Ligue des champions par défaut ; NBA, tennis et autres ligues en les ajoutant à `SPORTS`). Marchés : 1N2, buts/points, handicap, les 2 équipes marquent, double chance, corners, cartons, tirs, tirs cadrés, fautes, points/rebonds/passes/3 pts des joueurs NBA.

Chaque pari reçoit un score :
1. **Valeur** : la meilleure cote du marché face au consensus des autres books (un book en retard = opportunité).
2. **Désaccord** entre bookmakers.
3. **Mouvement de la cote** depuis le relevé précédent : une cote qui baisse signale l'argent des pros.
4. **Priorité aux marchés mal pricés** : corners, cartons et stats joueurs reçoivent un gros bonus ; le 1N2 et les buts, très efficaces, sont pénalisés. Au moins 75 % des candidats aux combinés viennent des marchés secondaires.
5. **Stats réelles** (si API-Football est configurée) : corners et cartons attendus d'après les derniers matchs des deux équipes, mélangés à la proba des books (60/40). Une ligne mal placée est signalée.
6. **Apprentissage par la cote de clôture.** Kelly ne se juge pas sur la chance (gagné/perdu : il faudrait des milliers de paris pour y voir clair) mais sur la question des pros : *la cote que j'ai prise le matin est-elle meilleure que la cote finale juste avant le coup d'envoi ?* Battre la clôture régulièrement, c'est la seule preuve fiable qu'une méthode gagne. Pour apprendre vite, elle suit aussi ses 50 meilleurs candidats du jour (paris fantômes), pas seulement ceux qu'elle envoie : leurs résultats (gratuits via API-Football) nourrissent la calibration ; leur cote de clôture n'est relevée que sur forfait payant (`CLOSE_GHOSTS=1`).
   - Un modèle (régression ridge) apprend quels signaux, marchés et bookmakers annoncent une cote qui va baisser. Il remplace les coefficients fixes dès 40 clôtures.
   - Il n'est **adopté que s'il fait au moins aussi bien** que le score heuristique sur des jours qu'il n'a pas vus (test hors échantillon). Pas d'auto-illusion.
   - Les résultats servent en appoint : dès 300 paris réglés par famille de marchés, Kelly calibre ses probabilités (quand elle dit 60 %, ça doit passer 60 % du temps), ce qui affine directement les mises.
   - Elle mesure quels bookmakers sont lents (CLV moyen quand ils portent la meilleure cote).
   - Le matin, le message 🧠 dit ce qu'elle a appris : CLV moyen, « bat la clôture X % du temps », signaux qui pèsent, books lents, calibration, résultat du test hors échantillon.

Le message indique pour chaque pari les signaux détectés. Ce n'est pas de la voyance : Kelly cherche les endroits où les bookmakers se trompent entre eux, et mesure honnêtement si elle a raison.

## Mises
Kelly fractionnaire (1/4), plafonné : 5 % de la bankroll max sur le Sûr, 2 % Costaud, 1 % Méchant, 0,5 % Monstre. La bankroll est mise à jour avec les résultats des combinés. Après 5 combinés perdus d'affilée, il te dit de lever le pied.

## Installation
Secrets GitHub (Settings > Secrets and variables > Actions) : `ODDS_API_KEY` (the-odds-api.com), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (@BotFather, puis `getUpdates` pour lire ton chat id), et en option `STATS_API_KEY` (api-football sur api-sports.io : règle les corners/cartons/stats joueurs et nourrit le modèle ; sans elle ces paris restent « non réglés » et n'entrent pas dans l'apprentissage).

## Forfait gratuit
Kelly est réglée pour tourner **tout le mois** sur les forfaits gratuits : The Odds API 500 crédits/mois, API-Football 100 requêtes/jour, GitHub Actions 2 000 minutes/mois (dépôt privé).

**Crédits The Odds API** (`kelly/budget.py`). Chaque appel d'un endpoint de cotes coûte *marchés × régions* (`/sports` et `/events` sont gratuits). Kelly divise les crédits restants par le nombre de jours avant la remise à zéro (500 le 1er = 16 par jour) et répartit cette allocation : 60 % le matin, 40 % pour la clôture. Ce qui n'est pas dépensé un jour s'ajoute aux jours suivants (week-ends chargés en matchs). Un appel refusé par le budget est sauté avec un message dans les logs, jamais une erreur. L'état est dans `data/budget.json`, commité par le workflow. Le matin :
1. l'endpoint gratuit `/events` dit quelles ligues jouent : une ligue sans match ne coûte rien ;
2. marchés vedettes (`h2h,totals`, 2 crédits par ligue, tous ses matchs d'un coup) pour les ligues les plus chargées, au plus la moitié du budget du matin ;
3. le reste va aux marchés mal pricés, dans l'ordre : corners, cartons, puis les 2 équipes marquent, tirs cadrés, tirs, double chance, sur les matchs les mieux cotés.

**Clôture** (passages horaires) : seuls les paris envoyés sont relevés, le Sûr d'abord. Une ligue avec au moins 2 matchs à relever : un seul appel groupé ; sinon un appel par match, seulement les marchés envoyés. Sous le plancher `MIN_CREDITS` (30), le matin ne dépense plus : la réserve sert à clôturer les paris déjà envoyés.

**Règlement gratuit** : le football est réglé avec API-Football (buts du temps réglementaire, 1 requête par jour réglé, mise en cache), sans crédit de cotes, pour les paris envoyés comme pour les fantômes. `/scores` (2 crédits par ligue, budgétés) ne sert que de repli sans `STATS_API_KEY` ou pour un sport hors foot.

**Minutes GitHub** : un seul workflow (`.github/workflows/kelly.yml`) : 08:00 `--daily`, puis chaque heure de 11:05 à 22:05 (UTC) `--track`, suivis de `--commands`. Environ 13 passages d'une minute par jour, soit ~400 minutes par mois.

Pour passer en payant : relève `ODDS_MONTHLY_CREDITS` (et ajuste `ODDS_RESET_DAY`, le jour du mois où tes crédits sont remis à zéro) dans le workflow, `MAX_SPORTS` et `PROP_EVENTS` pour plus de ligues et de matchs détaillés, remets `basketball_nba` (ou `tennis_`) dans `SPORTS`, `CLOSE_GHOSTS=1` pour clôturer aussi les fantômes, et réactive les alertes larges avec `BUDGET_SHARES=daily=0.5,closing=0.3,alert=0.1,snapshot=0.1` puis des passages `python bot.py --alert` / `--snapshot` dans le workflow.

## Vérifier l'installation
Onglet **Actions** → workflow « Kelly » → **Run workflow** → mode `check` (les autres modes : `daily`, `track`, `commands`). Kelly vérifie les trois clés (sans dépenser de crédits de cotes), affiche les crédits restants, l'allocation du jour, le nombre de jours avant la remise à zéro, le quota API-Football du jour et les ligues qui ont des matchs aujourd'hui, puis t'envoie « ✅ Kelly est en ligne » sur Telegram. Si le message n'arrive pas, envoie d'abord `/start` à ton bot.

## Test hors ligne
```
pip install -r requirements.txt
python bot.py --sample          # journée fictive
python -m tests.test_stats      # stats API-Football (quota, correspondance des noms, cache du jour)
python -m tests.test_bot        # tout le flux : budget de crédits, journée type, clôture, apprentissage, règlement gratuit, bilan, calibration, commandes
```

## Fichiers
`kelly/odds.py` cotes et sélections · `budget.py` budget mensuel de crédits · `combos.py` combinés · `track.py` suivi des paris et clôture · `learn.py` apprentissage · `stats.py` stats réelles · `settle.py` règlement et bilan · `bankroll.py` mises · `telegram.py` envoi et commandes · `app.py` orchestration · `data/` paris suivis, combinés, historique des cotes, modèle, bankroll, budget de crédits (commités par le workflow).

Probabilités estimées, aucun gain garanti. Ne mise que ce que tu peux perdre. 18+
