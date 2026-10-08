# Kelly

Kelly est un bot Telegram personnel de paris sportifs (et oui, elle mise selon le critère de Kelly). Chaque matin elle règle les paris de la veille, dresse le bilan, apprend de la cote de clôture, puis envoie le pari du jour et des combinés agressifs construits en priorité sur les marchés que les bookmakers pricent mal.

## Ce qu'il envoie

**08:00 (Abidjan)**
- 📋 Résultats des paris d'hier, 📊 bilan (taux de réussite, rendement, CLV, par palier et par marché), 💼 bankroll, 🧠 ce qu'elle a appris.
- ⭐ Pari du jour.
- 📡 L'état des sources de stats gratuites (une ligne par source) et le nombre de matchs couverts par les stats d'avant-match.
- 🛡️ **Sûr** (cote 5 à 7), ⚖️ **Costaud** (12 à 20), 🎯 **Spécial corners/cartons** (6 à 15, uniquement corners, cartons et stats joueurs : les marchés les plus mal pricés), 🔥 **Méchant** (40 à 80), 💀 **Monstre** (150+). Le nombre de matchs est libre : il prend la combinaison la plus probable qui atteint la cote visée, un seul pari par match. Chaque combiné vient avec une mise conseillée.

**De 11:05 à 22:05 (UTC), chaque heure** : relevé de la cote de clôture des paris **envoyés** dont le match démarre dans les 90 minutes (voir « Forfait gratuit »), et 🚨 alerte si une cote chute brutalement sur les matchs relevés (l'argent des parieurs pros). Les alertes ne viennent donc que de ces passages : pas de balayage large ni de relevé du soir sur forfait gratuit.

## Commandes Telegram
`/bilan [jours]`, `/match PSG`, `/combi 30` (combiné sur mesure à la cote visée), `/top`, `/bank 50000` (règle ta bankroll), `/aide`. Seul ton chat est écouté.
- `/stats <équipe>` (ex. `/stats Arsenal`) : corners, cartons, tirs, tirs cadrés, fautes et buts pour et contre sur les 10 derniers matchs, à domicile et à l'extérieur, et la forme xG. Marche à toute heure (profils commités dans `data/`).
- `/analyse <match>` (ex. `/analyse Arsenal`) : tous les chiffres d'avant-match d'un match du jour (corners, cartons, fautes, tirs, buts et xG attendus) puis ses meilleurs paris avec leur « pourquoi » et l'écart modèle-ligne.

Les commandes sont lues à chaque passage de GitHub Actions (08:00 puis chaque heure de 11:05 à 22:05) : réponse dans l'heure en journée, au matin pour celles de la nuit. Pour des réponses instantanées, lance `python bot.py --serve` sur un petit serveur. `/bilan` inclut aussi le message 🧠.

## Comment il choisit
Sports : football (5 grands championnats + Ligue des champions par défaut ; NBA, tennis et autres ligues en les ajoutant à `SPORTS`). Marchés : 1N2, buts/points, handicap, les 2 équipes marquent, double chance, corners, cartons, tirs, tirs cadrés, fautes, points/rebonds/passes/3 pts des joueurs NBA.

Chaque pari reçoit un score :
1. **Valeur** : la meilleure cote du marché face au consensus des autres books (un book en retard = opportunité).
2. **Désaccord** entre bookmakers.
3. **Mouvement de la cote** depuis le relevé précédent : une cote qui baisse signale l'argent des pros.
4. **Priorité aux marchés mal pricés** : corners, cartons et stats joueurs reçoivent un gros bonus ; le 1N2 et les buts, très efficaces, sont pénalisés. Au moins 75 % des candidats aux combinés viennent des marchés secondaires.
5. **Stats réelles** gratuites, sans clé (`kelly/sources`) : chaque matin Kelly actualise football-data.co.uk (CSV : tirs, corners, fautes, cartons, arbitre, cotes d'ouverture et de clôture), Understat (xG par équipe, tirs et xG par 90 min des joueurs) et FBref (désactivée d'office si le site bloque les robots : Cloudflare, aucun contournement). Avec ces profils, un modèle de Poisson donne pour chaque pari une proba « vraies stats », mélangée à celle des books (60/40) :
   - **corners et cartons** attendus d'après les derniers matchs des deux équipes (et l'arbitre s'il est connu) ; cartons = jaunes + rouges ;
   - **tirs et tirs cadrés des joueurs** : tirs/90 du joueur sur ~85 minutes (titulaire régulier), face à la ligne (« stats réelles : 2.9 tirs/90 vs ligne 1.5 ») ; tirs cadrés estimés à 35 % des tirs quand FBref est indisponible ;
   - **buts et les 2 équipes marquent** : Poisson sur les xG attendus (poids 20 %, marché très efficace) ; 1N2 : seulement l'écart de buts attendus, gardé pour l'apprentissage.
   API-Football ne sert plus que de repli pour les corners/cartons quand les sources n'ont rien (économise le quota). Une ligne mal placée (écart de plus de 8 points) est signalée dans le « pourquoi ». Donnée absente = pari inchangé.
6. **Apprentissage par la cote de clôture.** Kelly ne se juge pas sur la chance (gagné/perdu : il faudrait des milliers de paris pour y voir clair) mais sur la question des pros : *la cote que j'ai prise le matin est-elle meilleure que la cote finale juste avant le coup d'envoi ?* Battre la clôture régulièrement, c'est la seule preuve fiable qu'une méthode gagne. Pour apprendre vite, elle suit aussi ses 50 meilleurs candidats du jour (paris fantômes), pas seulement ceux qu'elle envoie : leurs résultats (gratuits via API-Football) nourrissent la calibration ; leur cote de clôture n'est relevée que sur forfait payant (`CLOSE_GHOSTS=1`).
   - Un modèle (régression ridge) apprend quels signaux, marchés et bookmakers annoncent une cote qui va baisser, y compris les écarts modèle-ligne des stats d'avant-match (corners, cartons, tirs, buts) et l'écart de buts attendus, figés au moment du choix comme les autres. Il remplace les coefficients fixes dès 40 clôtures.
   - **Amorçage historique** : pour ne pas attendre des semaines, Kelly construit (`kelly/bootstrap.py`, `data/bootstrap.json`, ~1 200 lignes) des paris d'entraînement avec l'historique football-data des 3 dernières saisons : meilleure cote d'ouverture contre le consensus sans marge, et ce que la cote est devenue à la clôture (1N2 et plus/moins 2,5 buts), avec la valeur, le désaccord Avg/Pinnacle et les buts attendus calculés sans regarder le futur. Ces lignes pèsent moins qu'un pari réel (0,3) et sont marquées : le modèle peut donc être entraîné dès le premier jour.
   - Il n'est **adopté que s'il fait au moins aussi bien** que le score heuristique sur des données qu'il n'a pas vues (test hors échantillon) : sur tes paris réels dès qu'il y en a assez (40 clôtures sur au moins 2 jours), sinon sur la tranche la plus récente de l'historique, mise de côté. Un modèle adopté sur l'historique est écarté si le test sur les paris réels est perdu. Pas d'auto-illusion.
   - Les résultats servent en appoint : dès 300 paris réglés par famille de marchés, Kelly calibre ses probabilités (quand elle dit 60 %, ça doit passer 60 % du temps), ce qui affine directement les mises.
   - Elle mesure quels bookmakers sont lents (CLV moyen quand ils portent la meilleure cote).
   - Le matin, le message 🧠 dit ce qu'elle a appris : CLV moyen, « bat la clôture X % du temps », signaux qui pèsent, books lents, calibration, résultat du test hors échantillon et nombre de lignes historiques utilisées.

Le message indique pour chaque pari les signaux détectés. Ce n'est pas de la voyance : Kelly cherche les endroits où les bookmakers se trompent entre eux, et mesure honnêtement si elle a raison.

## Mises
Kelly fractionnaire (1/4), plafonné : 5 % de la bankroll max sur le Sûr, 2 % Costaud, 1,5 % Spécial, 1 % Méchant, 0,5 % Monstre. La bankroll est mise à jour avec les résultats des combinés. Après 5 combinés perdus d'affilée, il te dit de lever le pied.

## Installation
Secrets GitHub (Settings > Secrets and variables > Actions) : `ODDS_API_KEY` (the-odds-api.com), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (@BotFather, puis `getUpdates` pour lire ton chat id), et en option `STATS_API_KEY` (api-football sur api-sports.io : règle les corners/cartons/stats joueurs et nourrit le modèle ; sans elle ces paris restent « non réglés » et n'entrent pas dans l'apprentissage).

## Forfait gratuit
Kelly est réglée pour tourner **tout le mois** sur les forfaits gratuits : The Odds API 500 crédits/mois, API-Football 100 requêtes/jour, GitHub Actions 2 000 minutes/mois (dépôt privé).

**Crédits The Odds API** (`kelly/budget.py`). Chaque appel d'un endpoint de cotes coûte *marchés × régions* (`/sports` et `/events` sont gratuits). Kelly divise les crédits restants par le nombre de jours avant la remise à zéro (500 le 1er = 16 par jour) et répartit cette allocation : 60 % le matin, 40 % pour la clôture. Ce qui n'est pas dépensé un jour s'ajoute aux jours suivants (week-ends chargés en matchs). Un appel refusé par le budget est sauté avec un message dans les logs, jamais une erreur. L'état est dans `data/budget.json`, commité par le workflow. Le matin :
1. l'endpoint gratuit `/events` dit quelles ligues jouent : une ligue sans match ne coûte rien ;
2. marchés vedettes (`h2h,totals`, 2 crédits par ligue, tous ses matchs d'un coup) pour les ligues les plus chargées, au plus la moitié du budget du matin ;
3. le reste va aux marchés mal pricés, dans l'ordre : corners, cartons, puis les 2 équipes marquent, tirs cadrés, tirs, double chance, sur les matchs les mieux cotés.

**Clôture** (passages horaires) : seuls les paris envoyés sont relevés, le Sûr d'abord. Une ligue avec au moins 2 matchs à relever : un seul appel groupé ; sinon un appel par match, seulement les marchés envoyés. Sous le plancher `MIN_CREDITS` (30), le matin ne dépense plus : la réserve sert à clôturer les paris déjà envoyés.

**Règlement gratuit** : le football est réglé avec API-Football (buts du temps réglementaire, 1 requête par jour réglé, mise en cache), sans crédit de cotes, pour les paris envoyés comme pour les fantômes. Quand API-Football est absent ou que son quota du jour est épuisé, les **buts, corners et cartons** sont réglés avec les CSV football-data déjà téléchargés (cartons = jaunes + rouges, comme API-Football ; certains books comptent un rouge double, à vérifier). football-data ne se met à jour que ~2 fois par semaine : un pari de corners/cartons sans résultat n'est annulé qu'après **8 jours** (3 jours pour les buts et les stats joueurs, qui n'ont pas de repli). `/scores` (2 crédits par ligue, budgétés) ne sert que de dernier repli pour les marchés de buts, sans `STATS_API_KEY`, ou pour un sport hors foot.

**Sources de stats** (`kelly/sources`, aucun crédit, aucune clé) : téléchargements polis (User-Agent déclaré, robots.txt respecté, 3 s entre requêtes, 6 s pour FBref, requêtes conditionnelles, cache disque). Sur 403/429/page anti-robot la source est bloquée 24 h (7 jours si Cloudflare) et Kelly continue avec les autres, sans jamais contourner. Le cache brut `.cache/sources/` est conservé d'un passage GitHub Actions à l'autre (`actions/cache`, clé par jour) ; les petits profils calculés `data/src_*.json`, `sources_state.json` et `bootstrap.json` sont commités avec le reste de `data/`. `SOURCES_REFRESH=0` coupe l'actualisation, `FBREF_ENABLED=0` coupe FBref. `python bot.py --check` affiche l'état de chaque source. Le workflow « Kelly — sonde des sources » (`python tools/probe.py --sources`) fait un essai réel de bout en bout et écrit `probe/sources_report.json`.

**Minutes GitHub** : un seul workflow (`.github/workflows/kelly.yml`) : 08:00 `--daily`, puis chaque heure de 11:05 à 22:05 (UTC) `--track`, suivis de `--commands`. Environ 13 passages d'une minute par jour (le passage de 08:00 dure 1 à 3 minutes avec les sources), soit ~450 minutes par mois.

Pour passer en payant : relève `ODDS_MONTHLY_CREDITS` (et ajuste `ODDS_RESET_DAY`, le jour du mois où tes crédits sont remis à zéro) dans le workflow, `MAX_SPORTS` et `PROP_EVENTS` pour plus de ligues et de matchs détaillés, remets `basketball_nba` (ou `tennis_`) dans `SPORTS`, `CLOSE_GHOSTS=1` pour clôturer aussi les fantômes, et réactive les alertes larges avec `BUDGET_SHARES=daily=0.5,closing=0.3,alert=0.1,snapshot=0.1` puis des passages `python bot.py --alert` / `--snapshot` dans le workflow.

## Vérifier l'installation
Onglet **Actions** → workflow « Kelly » → **Run workflow** → mode `check` (les autres modes : `daily`, `track`, `commands`). Kelly vérifie les trois clés (sans dépenser de crédits de cotes), affiche les crédits restants, l'allocation du jour, le nombre de jours avant la remise à zéro, le quota API-Football du jour et les ligues qui ont des matchs aujourd'hui, puis t'envoie « ✅ Kelly est en ligne » sur Telegram. Si le message n'arrive pas, envoie d'abord `/start` à ton bot.

## Test hors ligne
```
pip install -r requirements.txt
python bot.py --sample          # journée fictive
python -m tests.test_stats      # stats API-Football (quota, noms) et estimations d'avant-match (sources d'abord, tirs des joueurs, xG)
python -m tests.test_sources    # sources gratuites (faux réseau), amorçage historique, sonde
python -m tests.test_bot        # tout le flux : budget de crédits, journée type, clôture, apprentissage et amorçage, règlement gratuit (API-Football, football-data), bilan, calibration, commandes, combiné spécial
```

## Fichiers
`kelly/odds.py` cotes et sélections · `budget.py` budget mensuel de crédits · `combos.py` combinés · `track.py` suivi des paris et clôture · `learn.py` apprentissage · `bootstrap.py` amorçage par l'historique football-data · `stats.py` API-Football et estimations d'avant-match · `prematch.py` modèle Poisson (corners, cartons, tirs, buts) · `sources/` football-data, Understat, FBref · `settle.py` règlement et bilan · `bankroll.py` mises · `telegram.py` envoi et commandes · `app.py` orchestration · `data/` paris suivis, combinés, historique des cotes, modèle, bankroll, budget de crédits, profils de sources et amorçage (commités par le workflow). `tools/probe.py` : sonde des sources.

Probabilités estimées, aucun gain garanti. Ne mise que ce que tu peux perdre. 18+
