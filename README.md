# Kelly

Kelly est un bot Telegram personnel de paris sportifs (et oui, elle mise selon le critère de Kelly). Chaque matin elle règle les paris de la veille, dresse le bilan, apprend de la cote de clôture, puis envoie le pari du jour et des combinés agressifs construits en priorité sur les marchés que les bookmakers pricent mal.

## Ce qu'il envoie

**08:00 (Abidjan)**
- 📋 Résultats des paris d'hier, 📊 bilan (taux de réussite, rendement, CLV, par palier et par marché), 💼 bankroll, 🧠 ce qu'elle a appris.
- ⭐ Pari du jour.
- 🛡️ **Sûr** (cote 5 à 7), ⚖️ **Costaud** (12 à 20), 🔥 **Méchant** (40 à 80), 💀 **Monstre** (150+). Le nombre de matchs est libre : il prend la combinaison la plus probable qui atteint la cote visée, un seul pari par match. Chaque combiné vient avec une mise conseillée.

**Toutes les heures** : relevé de la cote de clôture des paris suivis dont le match démarre dans l'heure (un appel par match, seulement ses marchés), et 🚨 alerte si une cote chute brutalement (l'argent des parieurs pros). **12:00 et 16:00** : balayage large pour les alertes.

**20:00** : relevé silencieux des cotes du lendemain, pour détecter les mouvements de la nuit.

## Commandes Telegram
`/bilan [jours]`, `/match PSG`, `/combi 30` (combiné sur mesure à la cote visée), `/top`, `/bank 50000` (règle ta bankroll), `/aide`. Seul ton chat est écouté.

Les commandes sont lues toutes les 30 minutes par GitHub Actions. Pour des réponses instantanées, lance `python bot.py --serve` sur un petit serveur. `/bilan` inclut aussi le message 🧠.

## Comment il choisit
Sports : football (jusqu'à 25 ligues), NBA, Euroligue, tennis. Marchés : 1N2, buts/points, handicap, les 2 équipes marquent, double chance, corners, cartons, tirs, tirs cadrés, fautes, points/rebonds/passes/3 pts des joueurs NBA.

Chaque pari reçoit un score :
1. **Valeur** : la meilleure cote du marché face au consensus des autres books (un book en retard = opportunité).
2. **Désaccord** entre bookmakers.
3. **Mouvement de la cote** depuis le relevé précédent : une cote qui baisse signale l'argent des pros.
4. **Priorité aux marchés mal pricés** : corners, cartons et stats joueurs reçoivent un gros bonus ; le 1N2 et les buts, très efficaces, sont pénalisés. Au moins 75 % des candidats aux combinés viennent des marchés secondaires.
5. **Stats réelles** (si API-Football est configurée) : corners et cartons attendus d'après les derniers matchs des deux équipes, mélangés à la proba des books (60/40). Une ligne mal placée est signalée.
6. **Apprentissage par la cote de clôture.** Kelly ne se juge pas sur la chance (gagné/perdu : il faudrait des milliers de paris pour y voir clair) mais sur la question des pros : *la cote que j'ai prise le matin est-elle meilleure que la cote finale juste avant le coup d'envoi ?* Battre la clôture régulièrement, c'est la seule preuve fiable qu'une méthode gagne. Pour apprendre vite, elle suit aussi ses 50 meilleurs candidats du jour (paris fantômes), pas seulement ceux qu'elle envoie.
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

Les marchés détaillés coûtent 1 crédit par match et par marché chez The Odds API ; le relevé de clôture coûte environ le nombre de marchés suivis par match, 1 à 2 fois par match et par jour. Adapte `MAX_SPORTS`, `PROP_EVENTS`, `PROP_MARKETS` dans le workflow selon ton forfait.

## Test hors ligne
```
pip install -r requirements.txt
python bot.py --sample          # journée fictive
python -m tests.test_bot        # tout le flux : journée, clôture, apprentissage, règlement, bilan, calibration, commandes
```

## Fichiers
`kelly/odds.py` cotes et sélections · `combos.py` combinés · `track.py` suivi des paris et clôture · `learn.py` apprentissage · `stats.py` stats réelles · `settle.py` règlement et bilan · `bankroll.py` mises · `telegram.py` envoi et commandes · `app.py` orchestration · `data/` paris suivis, combinés, historique des cotes, modèle, bankroll (commités par les workflows).

Probabilités estimées, aucun gain garanti. Ne mise que ce que tu peux perdre. 18+
