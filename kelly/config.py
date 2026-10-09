"""Réglages (surchargeables par variables d'environnement)."""
import os

ODDS_API = "https://api.the-odds-api.com/v4"
MIN_BOOKMAKERS = 3
ODDS_RANGE = (1.15, 6.0)


def _int(name, default):
    """Entier d'environnement ; vide ou invalide = valeur par défaut."""
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _float(name, default):
    """Réel d'environnement ; vide ou invalide = valeur par défaut."""
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _bool(name, default=False):
    v = (os.environ.get(name) or "").strip().lower()
    return default if not v else v in ("1", "true", "oui", "yes")


def _shares(name, default):
    """« daily=0.6,closing=0.4 » -> dict ; entrée invalide ignorée."""
    out = dict(default)
    for part in (os.environ.get(name) or "").split(","):
        k, _, v = part.partition("=")
        try:
            out[k.strip()] = max(0.0, float(v))
        except ValueError:
            pass
    return out


def _list(name, default):
    return [x.strip() for x in (os.environ.get(name) or default).split(",") if x.strip()]


MAX_SPORTS = _int("MAX_SPORTS", 6)       # plafond de ligues AVEC matchs du jour (les plus chargées d'abord)
PROP_EVENTS = _int("PROP_EVENTS", 8)     # matchs avec marchés détaillés (le budget limite avant)
# Budget The Odds API (forfait gratuit : 500 crédits/mois). Voir kelly/budget.py.
ODDS_MONTHLY_CREDITS = _int("ODDS_MONTHLY_CREDITS", 500)
ODDS_RESET_DAY = _int("ODDS_RESET_DAY", 1)          # jour du mois où les crédits sont remis à zéro
# Part de l'allocation du jour par usage ; un usage absent (alert, snapshot) = 0 = désactivé.
BUDGET_SHARES = _shares("BUDGET_SHARES", {"daily": 0.60, "closing": 0.40})
FEATURED_SHARE = 0.5      # part max du budget du matin pour les marchés vedettes (le reste : corners, cartons...)
# Plancher de sécurité : le matin ne descend pas sous ce nombre de crédits restants (réserve la clôture).
MIN_CREDITS = _int("MIN_CREDITS", 30)
CLOSE_GHOSTS = _bool("CLOSE_GHOSTS")     # clôture aussi les paris fantômes (payant : désactivé sur gratuit)
# Catalogue mondial des ligues (clés The Odds API), par région et par priorité décroissante : (clé, nom affiché).
# Une clé finissant par "_" est un préfixe (toutes les clés qui commencent ainsi). Une clé inconnue ou hors saison est
# ignorée : odds.select_sports garde l'intersection avec /sports (gratuit). La priorité du matin est : région (ordre
# de REGIONS, l'international passe en tête les jours sans grande ligue européenne), puis nombre de matchs du jour.
REGIONS = (("europe", "🇪🇺 Europe"), ("ameriques", "🌎 Amériques"), ("asie", "🌏 Asie / Océanie"),
           ("afrique", "🌍 Afrique"), ("monde", "🌐 International"))
CATALOGUE = {
    "europe": [
        ("soccer_epl", "Premier League"), ("soccer_spain_la_liga", "Liga"), ("soccer_italy_serie_a", "Serie A"),
        ("soccer_germany_bundesliga", "Bundesliga"), ("soccer_france_ligue_one", "Ligue 1"),
        ("soccer_uefa_champs_league", "Ligue des champions"), ("soccer_uefa_europa_league", "Ligue Europa"),
        ("soccer_uefa_europa_conference_league", "Ligue Conférence"),
        ("soccer_portugal_primeira_liga", "Liga portugaise"), ("soccer_netherlands_eredivisie", "Eredivisie"),
        ("soccer_belgium_first_div", "Pro League belge"), ("soccer_turkey_super_league", "Süper Lig"),
        ("soccer_spl", "Premiership écossaise"), ("soccer_efl_champ", "Championship"),
        ("soccer_germany_bundesliga2", "Bundesliga 2"), ("soccer_spain_segunda_division", "Liga 2 espagnole"),
        ("soccer_italy_serie_b", "Serie B"), ("soccer_france_ligue_two", "Ligue 2"),
        ("soccer_england_league1", "League One"), ("soccer_england_league2", "League Two"),
        ("soccer_greece_super_league", "Super League grecque"), ("soccer_switzerland_superleague", "Super League suisse"),
        ("soccer_austria_bundesliga", "Bundesliga autrichienne"), ("soccer_poland_ekstraklasa", "Ekstraklasa"),
        ("soccer_denmark_superliga", "Superliga danoise"), ("soccer_sweden_allsvenskan", "Allsvenskan"),
        ("soccer_norway_eliteserien", "Eliteserien"), ("soccer_finland_veikkausliiga", "Veikkausliiga"),
        ("soccer_league_of_ireland", "Ligue irlandaise"), ("soccer_sweden_superettan", "Superettan"),
        ("soccer_germany_liga3", "3. Liga"), ("soccer_fa_cup", "FA Cup"), ("soccer_england_efl_cup", "Coupe de la Ligue (ENG)"),
        ("soccer_spain_copa_del_rey", "Coupe du Roi"),
    ],
    "ameriques": [
        ("soccer_brazil_campeonato", "Brasileirão"), ("soccer_argentina_primera_division", "Primera argentine"),
        ("soccer_usa_mls", "MLS"), ("soccer_mexico_ligamx", "Liga MX"), ("soccer_conmebol_copa_libertadores", "Copa Libertadores"),
        ("soccer_conmebol_copa_sudamericana", "Copa Sudamericana"), ("soccer_concacaf_leagues_cup", "Leagues Cup"),
        ("soccer_chile_campeonato", "Primera chilienne"), ("soccer_brazil_serie_b", "Brasileirão Série B"),
    ],
    "asie": [
        ("soccer_japan_j_league", "J League"), ("soccer_korea_kleague1", "K League 1"),
        ("soccer_saudi_arabia_pro_league", "Saudi Pro League"), ("soccer_china_superleague", "Super League chinoise"),
        ("soccer_australia_aleague", "A-League"),
    ],
    "afrique": [("soccer_africa_cup_of_nations", "CAN")],
    "monde": [
        ("soccer_uefa_nations_league", "Ligue des nations UEFA"),
        ("soccer_fifa_world_cup_qualifiers_europe", "Qualif. Coupe du monde Europe"),
        ("soccer_fifa_world_cup_qualifiers_south_america", "Qualif. Coupe du monde Amérique du Sud"),
        ("soccer_fifa_world_cup_qualifiers_", "Qualif. Coupe du monde"),      # préfixe : Afrique, Asie, Concacaf... si présents
        ("soccer_uefa_euro_qualifiers", "Qualif. Euro"), ("soccer_international_friendlies", "Matchs amicaux"),
        ("soccer_fifa_world_cup", "Coupe du monde"), ("soccer_uefa_european_championship", "Euro"),
        ("soccer_conmebol_copa_america", "Copa América"), ("soccer_concacaf_gold_cup", "Gold Cup"),
    ],
}
# Dans une région, les ligues « majeures » passent avant les autres (2es divisions, petits championnats, coupes),
# même si celles-ci ont plus de matchs : à partir de la clé indiquée, une ligue est de rang 2.
SECOND_TIER = {"europe": "soccer_germany_bundesliga2", "ameriques": "soccer_brazil_serie_b"}
# « Grandes affiches » : si aucune ne joue (trêve internationale), l'international passe en tête des priorités.
TOP_LEAGUES = ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga",
               "soccer_france_ligue_one", "soccer_uefa_champs_league", "soccer_uefa_europa_league",
               "soccer_uefa_europa_conference_league")
LEAGUE_KEYS = [k for r, _ in REGIONS for k, _ in CATALOGUE[r]]
# Sports suivis, dans l'ordre de priorité (MAX_SPORTS coupe la fin). Une entrée se terminant par "_" est un préfixe de
# clés The Odds API ; sinon c'est une clé exacte (soccer_uefa_champs_league n'attrape pas la version féminine).
# Défaut : tout le catalogue ci-dessus, foot uniquement (réglable gratuitement). Élargis via la variable SPORTS
# (ex. SPORTS_EXTRA=basketball_nba ajoute la NBA au catalogue).
SPORTS = _list("SPORTS", ",".join(LEAGUE_KEYS)) + _list("SPORTS_EXTRA", "")      # SPORTS_EXTRA : ajoute sans remplacer
# Ligues dont l'historique sert à l'amorçage de l'apprentissage (télécharge 4 saisons par ligue : on reste sur les 5 grandes).
BOOT_LEAGUES = _list("BOOT_LEAGUES", "soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga,"
                                     "soccer_france_ligue_one")
# Durées maximales (secondes) pour tenir le passage de 08:00 sous les 15 minutes du workflow.
DAY_END_HOUR = _int("DAY_END_HOUR", 8)            # le « jour Kelly » du run du matin (08:00 UTC) va jusqu'au lendemain à cette heure UTC (matchs du soir des Amériques)
LOCK_WAIT_MIN = _int("LOCK_WAIT_MIN", 20)         # le bot Telegram attend au plus ce temps (minutes) que le matin / le relevé horaire libère les données
OUTBOX_MAX_HOURS = _int("OUTBOX_MAX_HOURS", 36)   # un message resté en attente d'envoi (réseau coupé) plus longtemps que ça est abandonné
SCAN_SECONDS = _int("SCAN_SECONDS", 150)          # relevé gratuit /events de toutes les ligues
SOURCES_SECONDS = _int("SOURCES_SECONDS", 420)    # téléchargements des stats gratuites (le reste vient du cache)
# Pronostics Kelly sans cote de bookmaker (kelly/fair.py) : proba du modèle + « cote juste » = 1 / proba.
FAIR_MAX = _int("FAIR_MAX", 10)                   # pronostics détaillés dans le message du matin (5 si des combinés existent)
FAIR_TRACK = _int("FAIR_TRACK", 12)               # pronostics suivis par jour pour la calibration (data/fair.json), montrés le matin
FAIR_TRACK_FREE = _int("FAIR_TRACK_FREE", 150)    # en plus, pronostics buts / 1N2 / double chance / 2 marquent suivis en silence (réglés sans requête)
FAIR_CALIB_MIN = 60                               # pronostics réglés d'une famille avant de corriger ses probas (kelly/calib.py)
FAIR_CALIB_PRIOR = 200                            # la correction n'a tout son poids qu'avec bien plus de pronostics : n / (n + 200)
FAIR_MIN_N = 5                                    # matchs récents minimum pour croire une équipe
FAIR_P_RANGE = (0.55, 0.80)                       # proba utile : en dessous c'est du hasard, au-dessus la cote juste est < 1.25
FAIR_P_TARGET = 0.65                              # entre deux lignes, on garde celle dont la proba est la plus proche
FAIR_SHRINK = 0.9                                 # la proba du modèle est rapprochée de 50 % (le modèle de Poisson est trop sûr de lui)
FAIR_MARGIN = 0.05                                # « joue si la cote >= cote juste x 1.05 » : marge de sécurité du modèle
FAIR_API_EVENTS = _int("FAIR_API_EVENTS", 2)      # repli corners/cartons d'API-Football (historique des profils, aucune requête de plus) : inutilisé quand apistats est actif
FAIR_API_RESERVE = 30                             # requêtes API-Football du jour gardées pour le règlement
FAIR_KEEP_DAYS = 10                               # historique gardé dans data/fair.json
# ---- Stats mondiales API-Football (kelly/apistats.py) : accumulation jour après jour (forfait gratuit) ----
# Le forfait gratuit refuse « last », « ids » et toute date hors hier..demain : Kelly ne peut pas aller chercher l'historique d'une
# équipe. Chaque matin il lit la liste des matchs d'hier (et celle d'aujourd'hui), demande les statistiques des matchs terminés
# des ligues utiles (1 requête par match) et les ajoute à l'historique compact de chaque équipe.
APISTATS_DAILY = _int("APISTATS_DAILY", 55)       # requêtes API-Football par jour pour les profils (0 = désactivé), listes de matchs comprises
APISTATS_LAST = 10                                # derniers matchs d'une équipe dans les moyennes de son profil
APISTATS_KEEP_MATCHES = 15                        # matchs gardés par équipe (les plus anciens sont oubliés) : sert aux vues domicile / extérieur
APISTATS_MIN_N = 3                                # matchs minimum pour qu'un profil serve : « utilisable », confiance réduite (features.aps_quality)
APISTATS_GOOD_N = 6                               # à partir de là le profil est « bon » : confiance pleine, admis dans les combinés à cote juste
APISTATS_SAT_N = 10                               # équipe « saturée » : au moins autant de matchs que la fenêtre des moyennes (APISTATS_LAST)
APISTATS_MAX_TEAMS = 300                          # équipes gardées dans data/apistats_profiles.json (< 300 Ko avec 15 matchs chacune)
APISTATS_KEEP_DAYS = 120                          # équipe oubliée si elle n'a pas joué (ni été mise à jour) depuis
APISTATS_SECONDS = _int("APISTATS_SECONDS", 150)  # durée maximale du remplissage du matin
STATS_DATE_WINDOW = _int("STATS_DATE_WINDOW", 1)  # jours demandés autour d'aujourd'hui à /fixtures?date= (forfait gratuit : hier, aujourd'hui, demain)
# ---- Stats de match FotMob (kelly/sources/fmmatch.py) : touches, hors-jeu, fautes, corners, tirs, cartons par équipe ----
# Une page de match (~1 Mo) par match lu, jamais gardée brute ; remplissage étalé sur les passages horaires (pas celui de 08:00).
FM_PAGES_PER_RUN = _int("FM_PAGES_PER_RUN", 15)   # pages de match lues par passage horaire (0 = désactivé)
FM_SECONDS = _int("FM_SECONDS", 150)              # durée maximale du remplissage d'un passage (6 s entre deux pages FotMob)
FM_KEEP_MATCHES = 12                              # derniers matchs gardés par équipe
FM_KEEP_DAYS = 150                                # matchs plus anciens ignorés ; équipe oubliée sans mise à jour depuis
FM_RECENT_DAYS = 10                               # matchs gardés en entier pour le règlement des paris
FM_SETTLE_PAGES = 15                              # pages lues au plus par passage pour régler un pari (match pas encore accumulé)
# ---- Combinés à cote juste (kelly/combos.py) : bâtis sur les pronostics sans cote, jamais mélangés aux combinés réels ----
FAIR_COMBO_MIN_P = 0.60                           # proba Kelly minimale d'une jambe
FAIR_COMBO_MIN_Q = 0.40                           # qualité des données minimale (0 à 1)
FAIR_COMBO_TIERS = ((5, 5.0, 6.5), (12, 12.0, 15.0), (25, 25.0, 32.0))   # (cote mini visée, fourchette de la cote mini totale)
FAIR_COMBO_STAKE = ((8, 1.0), (20, 0.5), (1e9, 0.3))                      # mise INDICATIVE (u) selon la cote mini ; pas la bankroll
FAIR_COMBO_KEEP_DAYS = 14
FEATURED = {"soccer": "h2h,totals", "basketball": "h2h,spreads,totals", "tennis": "h2h,totals"}
# Marchés détaillés : 1 appel par match et par marché (1 crédit chacun et par région), DANS CET
# ORDRE de priorité (marchés mal pricés d'abord : si le budget manque, on coupe la fin).
# Indisponible (HTTP 422) = ignoré. Clés vérifiées dans la doc « additional markets »
# de The Odds API ; player_fouls n'y figure pas (il reste compris par le règlement et
# les libellés, mais n'est plus demandé).
PROP_MARKETS = {
    "soccer": _list("PROP_MARKETS", "alternate_totals_corners,alternate_totals_cards,btts,"
                                    "player_shots_on_target,player_shots,double_chance"),
    "basketball": ["player_points", "player_rebounds", "player_assists", "player_threes"],
    "tennis": [],
}

# Stats gratuites (kelly/sources) : actualisées chaque matin par run_daily (SOURCES_REFRESH=0 les coupe).
SOURCES_REFRESH = _bool("SOURCES_REFRESH", True)
# Poids du modèle « vraies stats » face à la proba des books (corners, cartons, tirs : 40 %; buts : 20 %, marché efficace).
MODEL_WEIGHT = {"corners": 0.4, "cards": 0.4, "player": 0.4, "goals": 0.2}
PLAYER_MINUTES = 85        # minutes attendues d'un titulaire régulier (tirs/90 -> tirs du match)
SOT_RATIO = 0.35           # tirs cadrés ~ 35 % des tirs, si seul le nombre de tirs est connu (FBref bloqué)
PM_FLAG = 0.08             # écart modèle/books (en proba) à partir duquel la ligne est signalée dans le « pourquoi »

# Paliers : seule la fourchette de cote compte, jamais le nombre de matchs (4, 7 ou 20 : peu importe, la cote visée doit
# être atteinte avec les paris les plus probables). (titre, cote minimale, cote maximale).
SPECIAL = "🎯 SPÉCIAL CORNERS/CARTONS (cote 6 à 15)"     # construit uniquement sur corners, cartons et stats joueurs
COMBOS = [
    ("🛡️ SÛR (cote 5 à 7)", 5.0, 7.0),
    ("⚖️ COSTAUD (cote 12 à 20)", 12.0, 20.0),
    (SPECIAL, 6.0, 15.0),
    ("🔥 MÉCHANT (cote 40 à 80)", 40.0, 80.0),
    ("💀 MONSTRE (cote 150+)", 150.0, 300.0),     # plafond : sans lui la cote dériverait vers l'absurde
]
CUSTOM_TOLERANCE = 0.25    # /combi <cote> : fourchette cote x (1 - 25 %) à cote x (1 + 25 %)
MAX_LEGS = _int("MAX_LEGS", 25)            # simple borne de sécurité, pas un réglage de jeu
DP_BIN = 0.005             # pas de discrétisation du log des cotes pour l'optimiseur (kelly/combos.py)

# ---- Cote « typique » (kelly/odds.py) ----
# La meilleure cote parmi ~20 books est souvent une aberration (exchange sans commission, book en retard). Kelly
# raisonne sur la cote typique : celle du (n // 4 + 1)-ième meilleur book (au moins le 2e), c'est-à-dire le 75e
# centile : au moins un book sur quatre la propose ou mieux, et un seul prix aberrant ne la déplace pas. La
# meilleure cote n'est plus qu'une note (« jusqu'à 4,60 chez Coolbet ») quand elle dépasse la typique de BEST_NOTE_GAP.
TYPICAL_RANK_SHARE = 0.25
TYPICAL_MIN_RANK = 2
BEST_NOTE_GAP = 0.05
# ---- « Books en désaccord » ----
# Seuil de l'écart (max - min des probas) / proba : DISAGREE_BASE x (1 + FEW_BOOKS / nb de books). Peu de books : un
# écart est normal, il faut plus. Dans le score, le désaccord pèse au prorata de min(1, nb de books / DISAGREE_FULL_BOOKS)
# (petites ligues = peu de books = signal faible).
DISAGREE_BASE = 0.12
FEW_BOOKS = 4.0
DISAGREE_FULL_BOOKS = 14
# ---- Proba « vraie » : book de référence (kelly/odds.py.consensus) ----
# Pinnacle est le book le plus précis du marché (marge ~2-3 %, limites hautes, il accepte les gagnants) : quand il affiche
# TOUTES les issues d'un marché/d'une ligne, la proba de consensus = SHARP_WEIGHT x sa proba (sans marge de Shin) +
# (1 - SHARP_WEIGHT) x la moyenne (Shin) des autres books. Sans book de référence : moyenne de tous les books.
# SHARP_BOOKS : clés The Odds API des books de référence (moyenne entre eux s'il y en a plusieurs). Les exchanges
# ("betfair_ex_eu", "matchbook") ont ~0 marge, ce que la méthode de Shin traite sans réglage (S ~ 1 : proche d'une
# simple normalisation), mais leurs prix peuvent être minces/en retard : absents par défaut, à ajouter via SHARP_BOOKS.
SHARP_WEIGHT = min(1.0, max(0.0, _float("SHARP_WEIGHT", 0.7)))
SHARP_BOOKS = tuple(x.lower() for x in _list("SHARP_BOOKS", "pinnacle"))
# ---- Plancher de qualité d'une jambe de combiné (kelly/combos.py.leg_reason) ----
# Une jambe doit porter une vraie raison, sinon elle n'entre dans aucun combiné ni pari du jour :
#   valeur : écart (proba x cote typique - 1) >= LEG_MIN_EDGE x (1 + FEW_BOOKS / nb de books) ;
#   stats  : le modèle d'avant-match (c["pm"]) donne au moins LEG_PM_GAP de proba de plus que les books, ou, au 1N2,
#            un avantage de buts attendus >= LEG_XGD pour l'équipe choisie (jamais pour le nul) ;
#   sûr    : proba >= LEG_SAFE_PROB.
# Garde-fous communs : écart >= LEG_EDGE_FLOOR (on ne paie pas plus de 5 % de marge) et pas de veto du modèle
# (proba du modèle inférieure de LEG_PM_GAP ou plus à celle des books).
LEG_MIN_EDGE = _float("LEG_MIN_EDGE", 0.03)
LEG_EDGE_FLOOR = -0.05
LEG_SAFE_EDGE_FLOOR = _float("LEG_SAFE_EDGE_FLOOR", -0.09)   # jambes sûres : la marge pèse plus sur les petites cotes
LEG_PM_GAP = 0.04
LEG_XGD = 0.5
LEG_SAFE_PROB = _float("LEG_SAFE_PROB", 0.68)
# Réutilisation d'un match dans plusieurs combinés du même jour : une jambe sûre (proba >= LEG_SAFE_PROB) peut figurer dans
# au plus LEG_REUSE_MAX combinés ; une jambe risquée dans un seul.
LEG_REUSE_MAX = _int("LEG_REUSE_MAX", 3)

# Priorité aux marchés mal pricés. 1N2/buts/points : efficaces, pénalisés.
MARKET_WEIGHT = {"h2h": 0.7, "double_chance": 0.7, "totals": 0.8, "spreads": 0.8, "btts": 1.0,
                 "alternate_totals_corners": 1.6, "alternate_totals_cards": 1.6}
PLAYER_WEIGHT = 1.8

UNITS = {
    "totals": {"soccer": "buts", "basketball": "points", "tennis": "jeux"},
    "alternate_totals_corners": "corners", "alternate_totals_cards": "cartons",
    "player_shots_on_target": "tirs cadrés", "player_shots": "tirs", "player_fouls": "fautes",
    "player_points": "points", "player_rebounds": "rebonds", "player_assists": "passes",
    "player_threes": "paniers à 3 pts",
}

# Alertes : baisse de la meilleure cote ou hausse de la proba de consensus.
ALERT_ODDS_DROP = 0.10
ALERT_PROB_RISE = 0.07

# Bankroll
KELLY_FRACTION = 0.25
STAKE_CAP = {"🛡️": 0.05, "⚖️": 0.02, "🎯": 0.015, "🔥": 0.01, "💀": 0.005}   # part max de la bankroll par tier
MIN_STAKE_SHARE = 0.002
# Espérance minimale d'un combiné (proba x cote typique) : la marge du bookmaker se multiplie à chaque jambe. Sous ce seuil Kelly
# retente le palier avec les seules jambes de valeur positive, sinon le saute (« espérance négative »). 0 = désactivé.
COMBO_MIN_EV = _float("COMBO_MIN_EV", 1.0)
STREAK_WARNING = 5

# Suivi et apprentissage par la cote de clôture.
CLOSE_WINDOW_MIN = 90      # relevé de clôture : matchs qui démarrent dans les 90 min (tolère un passage horaire en retard)
TRACK_KEEP_DAYS = _int("TRACK_KEEP_DAYS", 180)   # jours gardés dans tracked.json (réglés plus anciens : data/archive/tracked-AAAA.json)
TRACK_TOP = 50             # paris fantômes suivis par jour (en plus des envoyés)
LEARN_MIN_ROWS = 40        # lignes avec clôture avant d'entraîner un modèle
FREE_CLOSE_DAYS = 10       # cote finale gratuite (football-data, BetExplorer) des paris fantômes : matchs des 10 derniers jours
FREE_CLOSE_WEIGHT = 0.6    # poids d'une cote finale gratuite dans l'apprentissage, face à un vrai relevé de clôture (1)
CALIB_MIN_ROWS = 300       # résultats réglés par famille avant de calibrer
RIDGE_LAMBDA = 1.0
TOP_BOOKS = 12             # bookmakers encodés individuellement
# Amorçage de l'apprentissage par l'historique football-data (cotes d'ouverture vs clôture, kelly/bootstrap.py).
BOOT_SEASONS = _int("BOOT_SEASONS", 3)   # saisons passées en plus de la saison en cours
BOOT_MAX_ROWS = 1200       # lignes gardées dans data/bootstrap.json (fichier petit)
BOOT_PER_DAY = 3           # meilleurs candidats gardés par jour de match (comme les paris suivis)
BOOT_MIN_ROWS = 200        # lignes historiques minimum pour valider un modèle sans paris réels
BOOT_WEIGHT = 0.3          # poids d'une ligne historique face à un pari réel suivi
BOOT_REFRESH_DAYS = 7      # le fichier est reconstruit au plus une fois par semaine
# Règlement : jours d'attente avant d'annuler un pari sans résultat. football-data ne se met à jour que
# ~2 fois par semaine : corners et cartons attendent plus longtemps que les buts.
WAIT_DAYS = 3
WAIT_DAYS_STATS = 8

MARKET_LABELS = {
    "h2h": "1N2", "double_chance": "double chance", "totals": "buts/points", "spreads": "handicap",
    "btts": "les 2 marquent", "alternate_totals_corners": "corners", "alternate_totals_cards": "cartons",
    "player_shots_on_target": "tirs cadrés", "player_shots": "tirs", "player_fouls": "fautes",
    "player_points": "points joueur", "player_rebounds": "rebonds", "player_assists": "passes",
    "player_threes": "3 points", "team_shots_total": "tirs (total)", "team_sot_total": "tirs cadrés (total)",
    "team_shots_home": "tirs d'une équipe", "team_shots_away": "tirs d'une équipe", "team_sot_home": "tirs cadrés d'une équipe",
    "team_sot_away": "tirs cadrés d'une équipe", "team_fouls_total": "fautes (total)", "team_fouls_home": "fautes d'une équipe",
    "team_fouls_away": "fautes d'une équipe", "team_throws_total": "touches (total)", "team_throws_home": "touches d'une équipe",
    "team_throws_away": "touches d'une équipe", "team_offsides_total": "hors-jeu (total)",
}

# ---- Caches (kelly/cache.py) : ne jamais redemander ce qu'on a déjà ----
# The Odds API : les réponses payantes du jour (marchés vedettes par ligue, marchés détaillés par match) sont gardées dans
# .cache/odds avec leur heure de relevé ; un second passage du matin (relance manuelle) les relit sans dépenser un crédit.
ODDS_CACHE_HOURS = _float("ODDS_CACHE_HOURS", 12)
EVENTS_CACHE_HOURS = _float("EVENTS_CACHE_HOURS", 6)     # /sports et /events (gratuits : on ménage surtout le temps du run)
# API-Football : listes de matchs d'un jour passé déjà terminé = pour toujours ; liste encore incomplète = au plus toutes les
# FIXTURES_RETRY_HOURS et seulement pendant FIXTURES_STALE_DAYS jours ; une réponse en erreur ou vide n'est pas redemandée pendant STATS_NEG_MINUTES (anti-boucle de requêtes).
FIXTURES_RETRY_HOURS = _float("FIXTURES_RETRY_HOURS", 6)
FIXTURES_STALE_DAYS = _int("FIXTURES_STALE_DAYS", 3)
STATS_NO_DATA_DAYS = _int("STATS_NO_DATA_DAYS", 3)       # match terminé sans statistiques : on ne le redemande pas avant
STATS_NEG_MINUTES = _float("STATS_NEG_MINUTES", 20)
