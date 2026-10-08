# Botbet

Bot Telegram personnel de paris sportifs. Chaque matin il règle les paris de la veille, dresse le bilan, apprend de ses résultats, puis envoie le pari du jour et des combinés agressifs construits en priorité sur les marchés que les bookmakers pricent mal.

## Ce qu'il envoie

**08:00 (Abidjan)**
- 📋 Résultats des paris d'hier, 📊 bilan (taux de réussite, rendement par palier et par marché), 💼 bankroll, 🧠 ce qu'il a appris.
- ⭐ Pari du jour.
- 🛡️ **Sûr** (cote 5 à 7), ⚖️ **Costaud** (12 à 20), 🔥 **Méchant** (40 à 80), 💀 **Monstre** (150+). Le nombre de matchs est libre : il prend la combinaison la plus probable qui atteint la cote visée, un seul pari par match. Chaque combiné vient avec une mise conseillée.

**12:00 et 16:00** : 🚨 alerte si une cote chute brutalement depuis le dernier relevé (l'argent des parieurs pros).

**20:00** : relevé silencieux des cotes du lendemain, pour détecter les mouvements de la nuit.

## Commandes Telegram
`/bilan [jours]`, `/match PSG`, `/combi 30` (combiné sur mesure à la cote visée), `/top`, `/bank 50000` (règle ta bankroll), `/aide`. Seul ton chat est écouté.

Les commandes sont lues toutes les 30 minutes par GitHub Actions. Pour des réponses instantanées, lance `python bot.py --serve` sur un petit serveur.

## Comment il choisit
Sports : football (jusqu'à 25 ligues), NBA, Euroligue, tennis. Marchés : 1N2, buts/points, handicap, les 2 équipes marquent, double chance, corners, cartons, tirs, tirs cadrés, fautes, points/rebonds/passes/3 pts des joueurs NBA.

Chaque pari reçoit un score :
1. **Valeur** : la meilleure cote du marché face au consensus des autres books (un book en retard = opportunité).
2. **Désaccord** entre bookmakers.
3. **Mouvement de la cote** depuis le relevé précédent : une cote qui baisse signale l'argent des pros.
4. **Priorité aux marchés mal pricés** : corners, cartons et stats joueurs reçoivent un gros bonus ; le 1N2 et les buts, très efficaces, sont pénalisés. Au moins 75 % des candidats aux combinés viennent des marchés secondaires.
5. **Stats réelles** (si API-Football est configurée) : corners et cartons attendus d'après les derniers matchs des deux équipes, mélangés à la proba des books (60/40). Une ligne mal placée est signalée.
6. **Apprentissage** : chaque marché et chaque ligue reçoit un facteur selon les résultats réels, lissé pour ne pas sur-réagir aux petits échantillons. Un marché qui gagne plus que prévu est favorisé, un marché qui déçoit est pénalisé.

Le message indique pour chaque pari les signaux détectés. Ce n'est pas de la voyance : le bot cherche les endroits où les bookmakers se trompent entre eux, et mesure s'il a raison.

## Mises
Kelly fractionnaire (1/4), plafonné : 5 % de la bankroll max sur le Sûr, 2 % Costaud, 1 % Méchant, 0,5 % Monstre. La bankroll est mise à jour avec les résultats des combinés. Après 5 combinés perdus d'affilée, il te dit de lever le pied.

## Installation
Secrets GitHub (Settings > Secrets and variables > Actions) : `ODDS_API_KEY` (the-odds-api.com), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (@BotFather, puis `getUpdates` pour lire ton chat id), et en option `STATS_API_KEY` (api-football sur api-sports.io : règle les corners/cartons/stats joueurs et nourrit le modèle ; sans elle ces paris restent « non réglés » et n'entrent pas dans l'apprentissage).

Les marchés détaillés coûtent 1 crédit par match et par marché chez The Odds API : adapte `MAX_SPORTS`, `PROP_EVENTS`, `PROP_MARKETS` dans le workflow selon ton forfait.

## Test hors ligne
```
pip install -r requirements.txt
python bot.py --sample          # journée fictive
python -m tests.test_bot        # tout le flux : journée, règlement, bilan, apprentissage, alertes, commandes
```

## Fichiers
`botbet/odds.py` cotes et sélections · `combos.py` combinés · `stats.py` stats réelles · `settle.py` règlement et bilan · `learn.py` apprentissage · `bankroll.py` mises · `telegram.py` envoi et commandes · `app.py` orchestration · `data/` paris, historique des cotes, modèle, bankroll (commités par les workflows).

Probabilités estimées, aucun gain garanti. Ne mise que ce que tu peux perdre. 18+
