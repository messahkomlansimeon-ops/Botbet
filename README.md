# Botbet

Bot Telegram personnel : chaque matin (08:00 Abidjan), il analyse les matchs de foot du jour dans jusqu'à 25 championnats et envoie :
- **⭐ Pari du jour** ;
- **🛡️ Sûr** (3 matchs), **⚖️ Costaud** (5 matchs, cote >= 10), **🔥 Méchant** (7 matchs, cote >= 40), **💀 Monstre** (9 matchs, cote >= 120).

Marchés : 1N2, plus/moins de buts, les 2 équipes marquent, double chance, corners, cartons, tirs, tirs cadrés, fautes (selon ce que fournit ton forfait The Odds API ; un marché indisponible est ignoré). Les probabilités viennent du consensus des bookmakers (marge retirée) : ce sont des estimations, aucun gain n'est garanti. Plus la cote est grosse, plus la probabilité est faible (un combiné à cote 100+ passe moins de 1 fois sur 100).

**Crédits API :** les marchés détaillés (corners, tirs, fautes...) coûtent 1 crédit par match et par marché. Le forfait gratuit (500 crédits/mois) ne suffit pas pour un usage quotidien complet : réduis `MAX_SPORTS`, `PROP_EVENTS` ou `PROP_MARKETS`, ou prends un forfait payant.

## Installation
1. Clé gratuite sur https://the-odds-api.com -> `ODDS_API_KEY`.
2. Sur Telegram, crée un bot avec @BotFather -> `TELEGRAM_TOKEN`. Écris-lui un message, puis ouvre `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire ton `chat.id` -> `TELEGRAM_CHAT_ID`.
3. Sur GitHub : Settings > Secrets and variables > Actions, ajoute ces 3 secrets. Le workflow `daily.yml` s'exécute tous les jours (ou à la main via "Run workflow").

## Test local
```
pip install -r requirements.txt
python bot.py --sample                       # données fictives
ODDS_API_KEY=... python bot.py               # affiche le message
ODDS_API_KEY=... TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py   # envoie sur Telegram
```
