# Botbet

Bot Telegram personnel : chaque matin (08:00 Abidjan), il envoie le pari du jour et trois combinés :
- **SÛR** (3 matchs, proba max), **ÉQUILIBRÉ** (4 matchs, cote >= 4), **GROS COUP** (5 matchs, cote >= 8) ;

Marchés : 1N2 et plus/moins de buts. Les probabilités viennent du consensus de plusieurs bookmakers (marge retirée). Ce sont des estimations : aucun gain n'est garanti.

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
