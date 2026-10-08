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
SCAN_SECONDS = _int("SCAN_SECONDS", 150)          # relevé gratuit /events de toutes les ligues
SOURCES_SECONDS = _int("SOURCES_SECONDS", 420)    # téléchargements des stats gratuites (le reste vient du cache)
# Pronostics Kelly sans cote de bookmaker (kelly/fair.py) : proba du modèle + « cote juste » = 1 / proba.
FAIR_MAX = _int("FAIR_MAX", 10)                   # pronostics détaillés dans le message du matin (5 si des combinés existent)
FAIR_TRACK = _int("FAIR_TRACK", 12)               # pronostics suivis par jour pour la calibration (data/fair.json)
FAIR_MIN_N = 5                                    # matchs récents minimum pour croire une équipe
FAIR_P_RANGE = (0.55, 0.80)                       # proba utile : en dessous c'est du hasard, au-dessus la cote juste est < 1.25
FAIR_P_TARGET = 0.65                              # entre deux lignes, on garde celle dont la proba est la plus proche
FAIR_SHRINK = 0.9                                 # la proba du modèle est rapprochée de 50 % (le modèle de Poisson est trop sûr de lui)
FAIR_MARGIN = 0.05                                # « joue si la cote >= cote juste x 1.05 » : marge de sécurité du modèle
FAIR_API_EVENTS = _int("FAIR_API_EVENTS", 2)      # matchs sans profil football-data estimés par API-Football (~14 requêtes chacun)
FAIR_API_RESERVE = 30                             # requêtes API-Football du jour gardées pour le règlement
FAIR_KEEP_DAYS = 10                               # historique gardé dans data/fair.json
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

# (titre, cote minimale, cote maximale) : le nombre de matchs est libre.
SPECIAL = "🎯 SPÉCIAL CORNERS/CARTONS (cote 6 à 15)"     # construit uniquement sur corners, cartons et stats joueurs
COMBOS = [
    ("🛡️ SÛR (cote 5 à 7)", 5.0, 7.0),
    ("⚖️ COSTAUD (cote 12 à 20)", 12.0, 20.0),
    (SPECIAL, 6.0, 15.0),
    ("🔥 MÉCHANT (cote 40 à 80)", 40.0, 80.0),
    ("💀 MONSTRE (cote 150+)", 150.0, None),
]
MAX_LEGS = 8
POOL_SIZE = 22
SOFT_SHARE = 0.75          # part mini de marchés secondaires dans le pool

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
STREAK_WARNING = 5

# Suivi et apprentissage par la cote de clôture.
CLOSE_WINDOW_MIN = 90      # relevé de clôture : matchs qui démarrent dans les 90 min (tolère un passage horaire en retard)
TRACK_TOP = 50             # paris fantômes suivis par jour (en plus des envoyés)
LEARN_MIN_ROWS = 40        # lignes avec clôture avant d'entraîner un modèle
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
    "player_threes": "3 points",
}
