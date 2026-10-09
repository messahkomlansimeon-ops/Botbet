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


def _set(name, default):
    """« goals,h2h » -> frozenset({"goals", "h2h"}) ; « none » (ou aucune, 0, -, non) : ensemble vide ; variable absente ou vide : `default`."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return frozenset(default)
    if raw in ("none", "aucune", "aucun", "0", "-", "non", "no"):
        return frozenset()
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


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
# Championnats hors The Odds API, analysés grâce à 1xBet (liste des matchs et cotes, kelly/xworld.py) et FotMob (classement et stats de
# match ; id vérifiés : pays et nom lus sur la page de la ligue) : (clé Kelly, nom affiché, nom anglais chez 1xBet, id FotMob). Rang 2
# dans leur région ; un championnat que 1xBet ne liste pas ce jour-là est simplement absent du relevé.
XB_LEAGUES = {
    "europe": [
        ("soccer_xb_romania", "Liga 1 roumaine", "Romania. Liga 1", 189), ("soccer_xb_croatia", "HNL croate", "Croatia. HNL", 252),
        ("soccer_xb_serbia", "SuperLiga serbe", "Serbia. SuperLiga", 182),
        ("soccer_xb_czech", "Chance Liga tchèque", "Czech Republic. Chance Liga", 122),
        ("soccer_xb_russia", "Premier Liga russe", "Russia. Premier League", 63),
        ("soccer_xb_ukraine", "Premier Liga ukrainienne", "Ukraine. Premier League", 441),
        ("soccer_xb_hungary", "NB I hongroise", "Hungary. NB I", 212), ("soccer_xb_bulgaria", "1re ligue bulgare", "Bulgaria. First League", 270),
        ("soccer_xb_israel", "Ligat ha'Al", "Israel. Premier League", 127), ("soccer_xb_cyprus", "1re division chypriote", "Cyprus. First Division", 136),
        ("soccer_xb_slovakia", "1re ligue slovaque", "Slovakia. Super League", 176), ("soccer_xb_slovenia", "PrvaLiga slovène", "Slovenia. League 1", 173),
        ("soccer_xb_scotland_2", "Championship écossais", "Scotland. Championship", 123),
        ("soccer_xb_netherlands_2", "Eerste Divisie", "Netherlands. Eerste Divisie", 111),
        ("soccer_xb_portugal_2", "Liga Portugal 2", "Portugal. Segunda Liga", 185), ("soccer_xb_turkey_2", "1. Lig turque", "Turkey. TFF 1. Lig", 165),
        ("soccer_xb_belgium_2", "Challenger Pro League", "Belgium. Challenger Pro League", 264),
        ("soccer_xb_austria_2", "2. Liga autrichienne", "Austria. Second League", 119),
        ("soccer_xb_switzerland_2", "Challenge League suisse", "Switzerland. Challenge League", 163),
        ("soccer_xb_denmark_2", "1re division danoise", "Denmark. 1st Division", 85),
        ("soccer_xb_norway_2", "1re division norvégienne", "Norway. Adeccoligaen", 203),
        ("soccer_xb_poland_2", "I Liga polonaise", "Poland Championship. Liga 1", 197),
        ("soccer_xb_northern_ireland", "Premiership nord-irlandaise", "Northern Ireland. IFA Premiership", 129),
    ],
    "ameriques": [
        ("soccer_xb_colombia", "Primera A colombienne", "Colombia. Categoria Primera A", 274), ("soccer_xb_peru", "Liga 1 péruvienne", "Peru. Liga 1", 131),
        ("soccer_xb_ecuador", "LigaPro équatorienne", "Ecuador. Serie A", 246),
        ("soccer_xb_uruguay", "Primera uruguayenne", "Uruguay. Primera Division", 161),
        ("soccer_xb_paraguay", "Primera paraguayenne", "Paraguay. Primera Division", 199),
        ("soccer_xb_bolivia", "Primera bolivienne", "Bolivia. Primera Division", 144),
        ("soccer_xb_venezuela", "Primera vénézuélienne", "Venezuela. Primera Division", 339),
        ("soccer_xb_costa_rica", "Primera costaricienne", "Costa Rica. Primera Division", 121),
        ("soccer_xb_guatemala", "Liga Nacional guatémaltèque", "Guatemala. Liga Nacional", 336),
        ("soccer_xb_usl", "USL Championship", "USA. USL", 8972),
        ("soccer_xb_argentina_2", "Primera Nacional argentine", "Argentina. Primera B Nacional", 8965),
        ("soccer_xb_mexico_2", "Liga de Expansión MX", "Mexico. Liga de Expansion MX", 8976),
        ("soccer_xb_brazil_c", "Brasileirão Série C", "Brazil. Campeonato Brasileiro Serie C", 8971),
        ("soccer_xb_canada", "Premier League canadienne", "Canada. Premier League", 9986),
    ],
    "asie": [
        ("soccer_xb_japan_2", "J2 League", "Japan. J-League Division 2", 8974), ("soccer_xb_korea_2", "K League 2", "South Korea. K League 2", 9116),
        ("soccer_xb_qatar", "Qatar Stars League", "Qatar Championship", 535), ("soccer_xb_iran", "Persian Gulf Pro League", "Iran. Pro League", 523),
        ("soccer_xb_indonesia", "Super League indonésienne", "Indonesia. Super League", 8983),
        ("soccer_xb_china_2", "China League One", "China. League One", 9137),
    ],
    "afrique": [
        ("africa_egypt_premier_league", "Premier League égyptienne", "Egypt. Premier League", 519),
        ("africa_morocco_botola_pro", "Botola Pro marocaine", "Morocco. Botola", 530),
        ("soccer_xb_algeria", "Ligue 1 algérienne", "Algeria. Ligue 1", 516),
    ],
}
XB_KEYS = [row[0] for r, _ in REGIONS for row in XB_LEAGUES.get(r, ())]
XWORLD_ENABLED = _bool("XWORLD_ENABLED", True)     # 0 : Kelly s'en tient aux championnats de The Odds API
# Sports suivis, dans l'ordre de priorité (MAX_SPORTS coupe la fin). Une entrée se terminant par "_" est un préfixe de
# clés The Odds API ; sinon c'est une clé exacte (soccer_uefa_champs_league n'attrape pas la version féminine).
# Défaut : tout le catalogue ci-dessus, foot uniquement (réglable gratuitement). Élargis via la variable SPORTS
# (ex. SPORTS_EXTRA=basketball_nba ajoute la NBA au catalogue).
SPORTS = _list("SPORTS", ",".join(LEAGUE_KEYS)) + _list("SPORTS_EXTRA", "")      # SPORTS_EXTRA : ajoute sans remplacer
# Ligues dont l'historique sert à l'amorçage de l'apprentissage (télécharge 4 saisons par ligue : on reste sur les 5 grandes).
BOOT_LEAGUES = _list("BOOT_LEAGUES", "soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga,"
                                     "soccer_france_ligue_one")
# Durées maximales (secondes) pour tenir le passage de 08:00 sous les 15 minutes du workflow.
DAY_END_HOUR = _int("DAY_END_HOUR", 4)            # heure du passage du matin ; le « jour Kelly » du run du matin (04:00 UTC) va jusqu'au lendemain à cette heure UTC (matchs du soir des Amériques)
LOCK_WAIT_MIN = _int("LOCK_WAIT_MIN", 20)         # le bot Telegram attend au plus ce temps (minutes) que le matin / le relevé horaire libère les données
OUTBOX_MAX_HOURS = _int("OUTBOX_MAX_HOURS", 36)   # un message resté en attente d'envoi (réseau coupé) plus longtemps que ça est abandonné
SCAN_SECONDS = _int("SCAN_SECONDS", 150)          # relevé gratuit /events de toutes les ligues
FAIR_SECONDS = _int("FAIR_SECONDS", 300)          # calcul des pronostics de tous les matchs du jour (championnats 1xBet compris)
SOURCES_SECONDS = _int("SOURCES_SECONDS", 720)    # téléchargements des stats gratuites (le reste vient du cache) : partagé entre les sources, les ligues qui jouent bientôt d'abord
SOURCES_CACHE_KEEP_DAYS = _int("SOURCES_CACHE_KEEP_DAYS", 45)   # .cache/sources : une entrée ni téléchargée ni relue depuis ce nombre de jours est effacée
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
APISTATS_SECONDS = _int("APISTATS_SECONDS", 360)  # durée maximale du remplissage du matin (55 requêtes x STATS_MIN_GAP = ~340 s)
STATS_MIN_GAP = _float("STATS_MIN_GAP", 6.2)      # secondes entre deux appels API-Football : le forfait gratuit limite à 10 requêtes par minute (HTTP 429 au-delà)
STATS_RATE_WAIT = _float("STATS_RATE_WAIT", 60)   # attente (s) après un HTTP 429 avant l'unique nouvel essai, dans la limite du délai du passage
STATS_DATE_WINDOW = _int("STATS_DATE_WINDOW", 1)  # jours demandés autour d'aujourd'hui à /fixtures?date= (forfait gratuit : hier, aujourd'hui, demain)
# ---- Stats de match FotMob (kelly/sources/fmmatch.py) : touches, hors-jeu, fautes, corners, tirs, cartons par équipe ----
# Une page de match (~1 Mo) par match lu, jamais gardée brute ; remplissage étalé sur les passages horaires (pas celui de 08:00).
FM_PAGES_PER_RUN = _int("FM_PAGES_PER_RUN", 25)   # pages de match lues par passage horaire (0 = désactivé)
FM_SECONDS = _int("FM_SECONDS", 170)              # durée maximale du remplissage d'un passage (6 s entre deux pages FotMob)
FM_KEEP_MATCHES = 12                              # derniers matchs gardés par équipe
FM_KEEP_DAYS = 150                                # matchs plus anciens ignorés ; équipe oubliée sans mise à jour depuis
FM_RECENT_DAYS = 10                               # matchs gardés en entier pour le règlement des paris
FM_SETTLE_PAGES = 15                              # pages lues au plus par passage pour régler un pari (match pas encore accumulé)
# xG par match (fmmatch -> features._fmmatch) : meilleur prédicteur des buts futurs que les buts ou le total de saison du classement FotMob
XG_MIN_MATCHES = _int("XG_MIN_MATCHES", 4)        # matchs portant un xG, par équipe, pour que exp_xg_* vienne des matchs (sinon classement de saison)
XG_FULL_N = _int("XG_FULL_N", 6)                  # xG pleinement cru à partir de là ; en dessous, régression vers les buts : n/XG_FULL_N xG + le reste en buts
# ---- Combinés à cote juste (kelly/combos.py) : bâtis sur les pronostics sans cote, jamais mélangés aux combinés réels ----
# Pronostic « viable » (combos.viable) : le seul montré pour un match est son meilleur pronostic viable ; les combinés à vérifier
# n'utilisent que des pronostics viables. Niveau « modéré » choisi par l'utilisateur.
VIABLE_MIN_P = _float("VIABLE_MIN_P", 0.58)       # chances minimales (après correction de fiabilité)
VIABLE_MIN_N = _int("VIABLE_MIN_N", 4)            # matchs de données minimum pour chacune des deux équipes
VIABLE_MIN_Q = _float("VIABLE_MIN_Q", 0.25)       # qualité minimale des données (0 à 1)
VIABLE_MIN_K = _float("VIABLE_MIN_K", 0.85)       # marché (ou championnat) où Kelly s'est montrée trop sûre d'elle : écarté
FAIR_COMBO_MIN_P = 0.60                           # proba Kelly minimale d'une jambe
FAIR_COMBO_MIN_Q = 0.40                           # qualité des données minimale (0 à 1)
FAIR_COMBO_TIERS = ((5, 5.0, 6.5), (12, 12.0, 15.0), (25, 25.0, 32.0))   # (cote mini visée, fourchette de la cote mini totale)
# Combinés « spécial tirs » à vérifier chez 1xBet, chaque matin : (palier, préfixe des marchés, titre, fourchette de la cote mini)
# Le préfixe peut être un tuple de préfixes (str.startswith) : « ts5 » mélange tirs, tirs cadrés, fautes, touches et hors-jeu (un pari par match).
FAIR_STATS_TIER = _bool("FAIR_STATS_TIER", True)      # 0 : plus de palier « 🧮 COMBINÉ STATS » (tc5 et tv5 restent)
FAIR_STATS_PAID = _bool("FAIR_STATS_PAID", True)      # 0 : les matchs payés (cotes réelles) n'ont plus de pronostics tirs / fautes / touches / hors-jeu (comportement d'avant)
FAIR_SHOTS_TIERS = (("tc5", "team_sot_", "🎯 COMBINÉ TIRS CADRÉS", 5.0, 7.0),
                    ("tv5", "team_shots_", "🎯 COMBINÉ TIRS VERS LE BUT", 5.0, 7.0)) \
    + ((("ts5", ("team_fouls_", "team_throws_", "team_offsides_", "team_shots_", "team_sot_"), "🧮 COMBINÉ STATS", 5.0, 7.0),) if FAIR_STATS_TIER else ())
FAIR_TRACK_SHOTS = _int("FAIR_TRACK_SHOTS", 20)  # pronostics tirs / tirs cadrés suivis par jour (fiabilité de Kelly sur ces marchés)
# Cotes 1xBet (kelly/sources/xbet.py) : le prix exact des pronostics chez le bookmaker de l'utilisateur, lu le matin (gratuit, poli)
XBET_ENABLED = os.environ.get("XBET_ENABLED", "1") != "0"
XBET_SECONDS = _int("XBET_SECONDS", 1200)           # temps maximal du relevé 1xBet du matin (3 s entre requêtes)
XBET_MAX_MATCHES = _int("XBET_MAX_MATCHES", 150)    # matchs relevés au plus (les mieux classés d'abord) : ~2 Mo
# Proxy de l'utilisateur pour 1xBet (celui de scrap_stat) : les relevés ne partent jamais de la connexion du compte 1xBet. XBET_PROXY
# (variable d'environnement), sinon la ligne XBET_PROXY= de XBET_PROXY_FILE. Introuvable : pas de relevé (jamais en direct), sauf
# XBET_DIRECT=1.
# Tests et --sample (DATA_DIR réglé) : jamais le vrai fichier de la machine, donc aucun relevé réel (XBET_PROXY pour un essai manuel).
XBET_PROXY_FILE = os.environ.get("XBET_PROXY_FILE") or (os.path.join(os.environ["DATA_DIR"], "sofa.env") if os.environ.get("DATA_DIR")
                                                         else os.path.expanduser("~/.config/scrap_stat/sofa.env"))
XBET_DIRECT = _bool("XBET_DIRECT", False)
# Partenaire des codes coupon : 1 = version internationale, celle de l'appli mobile 1xBet de l'utilisateur (son propre ticket y est
# rattaché) ; le site 1xbet.ci (286) relit aussi ces codes. Un code créé pour 286 répond « code incorrect » dans l'appli.
XBET_COUPON_PARTNER = _int("XBET_COUPON_PARTNER", 1)
XBET_MONTH_MB = _float("XBET_MONTH_MB", 400)        # plafond mensuel de données 1xBet via le proxy (forfait 1 Go partagé avec SofaScore)
# Vérification EN DIRECT (combi N, bouton « 🎟️ Vérifier ce combiné », relevés de 2 h) : cotes relues à l'instant, sans cache
XBET_FRESH_MIN = _int("XBET_FRESH_MIN", 5)          # en direct : cotes lues il y a moins de N min reprises telles quelles (pas de proxy)
XBET_COMBI_FRESH_MIN = _int("XBET_COMBI_FRESH_MIN", 180)   # « combi N » : cotes de moins de 3 h reprises (relevé de 15:00, actualisation)
XBET_LIVE_DELAY = _float("XBET_LIVE_DELAY", 1.0)    # secondes entre deux requêtes 1xBet en direct (3 s pour les relevés)
XBET_LIVE_SPARES = _int("XBET_LIVE_SPARES", 8)      # matchs remplaçants relus avec ceux du combiné
XBET_SOON_MIN = _int("XBET_SOON_MIN", 10)           # match qui commence dans moins de N min : retiré par 1xBet, jamais repris
XBET_MANUAL_GAP = _int("XBET_MANUAL_GAP", 10)      # minutes au moins entre deux relevés manuels (« 🔄 Mettre à jour tous les paris »)
XBET_MANUAL_SECONDS = _int("XBET_MANUAL_SECONDS", 150)  # durée maximale d'un relevé manuel (sous le verrou des données) : combinés et 💎 d'abord
# Relecture du soir (`python bot.py --evening`, app.run_evening) : les matchs du jour Kelly pas encore commencés sont réanalysés avec les stats du moment (profils
# FotMob accumulés depuis le matin) et remis aux cotes 1xBet ; un seul message « 🌙 Pronostics de ce soir mis à jour ».
EVENING_SOON_MIN = _int("EVENING_SOON_MIN", 30)         # un match qui commence dans moins de N min n'est pas retouché (1xBet ferme les sous-matchs stats avant le coup d'envoi)
EVENING_SECONDS = _int("EVENING_SECONDS", 600)          # durée maximale du relevé 1xBet du soir (3 s entre requêtes)
EVENING_LIMIT = _int("EVENING_LIMIT", 12)               # paris 💎 au plus dans le message du soir
XBET_UPDATE_MOVE = _float("XBET_UPDATE_MOVE", 0.05)  # combiné du matin renvoyé si sa cote totale a bougé de plus de 5 %
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
def _days(name, default):
    """Liste de jours d'environnement (« 60,90,110 ») ; vide ou invalide = défaut."""
    try:
        return tuple(sorted({int(x) for x in _list(name, default)})) or _days_default(default)
    except ValueError:
        return _days_default(default)


def _days_default(default):
    return tuple(int(x) for x in default.split(","))


# Réglage mesuré sur l'historique (python bot.py --tune, kelly/tune.py -> data/tuning.json) : demi-vie du modèle de force par ligue et
# poids des buts face aux books (remplace MODEL_WEIGHT["goals"] là où il est mesuré). Corners et cartons : AUCUNE cote historique dans
# les CSV football-data, leurs poids restent ceux ci-dessus.
TUNE_HALF_LIVES = _days("TUNE_HALF_LIVES", "60,90,110,150,220,330,500,800")   # demi-vies candidates (jours) ; 500 et 800 : l optimum mesuré touche 330 sur la plupart des grandes ligues
TUNE_SECONDS = _int("TUNE_SECONDS", 1800)         # durée maximale d'un passage (reprise au passage suivant)
TUNE_TEST_DAYS = _int("TUNE_TEST_DAYS", 365)      # fenêtre de test : la dernière saison (walk-forward)
TUNE_STEP_DAYS = _int("TUNE_STEP_DAYS", 7)        # le modèle est réajusté tous les N jours pendant le test (3 en backtest)
TUNE_MIN_SPAN_DAYS = _int("TUNE_MIN_SPAN_DAYS", 540)   # historique minimal d'une ligue (environ 2 saisons)
TUNE_MIN_TEST = _int("TUNE_MIN_TEST", 150)        # matchs de test prédits au minimum, sinon la ligue garde la demi-vie par défaut
TUNE_MARGIN = _float("TUNE_MARGIN", 0.002)        # gain de log-loss (moyenne 1N2 / plus de 2,5) exigé pour quitter 110 j : évite le bruit
TUNE_WEIGHT_STEP = 0.05                           # grille du poids des buts : 0, 0,05 ... 0,5
TUNE_WEIGHT_MAX = 0.5
TUNE_WEIGHT_ON = (os.environ.get("TUNE_WEIGHT_ON") or "ou25").strip().lower()   # marchés qui fixent le poids : ou25 (le mélange ne sert qu'aux buts), both (1N2 + plus de 2,5), 1x2
TUNE_SHRINK_N = _float("TUNE_SHRINK_N", 600)      # matchs à partir desquels le poids d'une ligue pèse autant que le poids global
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
                 "alternate_totals_corners": 1.6, "alternate_totals_cards": 1.6,
                 # tirs et tirs cadrés (total ou une équipe) : marchés mal pricés, comme corners et cartons
                 "team_shots_total": 1.6, "team_shots_home": 1.5, "team_shots_away": 1.5,
                 "team_sot_total": 1.6, "team_sot_home": 1.5, "team_sot_away": 1.5}
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
# Alertes centrées sur 1xBet (kelly/xalerts.py) : 💰 1xBet en retard sur le marché, ⚠️ mes paris en danger, 📈 mieux payé qu'au matin,
# ✅ bonne nouvelle. Un seul message par passage (6 alertes au plus), jamais deux fois la même alerte sans nouveau mouvement.
XALERTS_ENABLED = _bool("XALERTS_ENABLED", True)    # 0 : plus aucune alerte
XALERT_LAG_EDGE = _float("XALERT_LAG_EDGE", 0.05)   # 💰 valeur mini (proba du marché x cote 1xBet - 1) pour dire « joue maintenant »
XALERT_RISE = _float("XALERT_RISE", 0.05)           # 📈 hausse mini de la cote 1xBet depuis le matin ; aussi mouvement mini pour répéter une alerte
XALERT_LIVE_MAX = _int("XALERT_LIVE_MAX", 3)        # matchs relus en direct chez 1xBet par passage d'alertes (les autres : cache ou rien)
XALERT_MAX_ITEMS = 6                                # alertes au plus dans un message

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


# ======================= LOT K : ensemble adaptatif (kelly/ensemble.py) et écarts « sharp » (kelly/sharpgap.py) =======================
# Les probas de Kelly (modèle de force + profils) perdent face au marché sur les buts et le 1N2 (audit : log-loss modèle > marché ; mêler
# le modèle n'améliore jamais, poids optimal 0 ; les paris où le modèle s'écarte le plus du marché perdent 9 à 16 %). La proba retenue est donc une
# COMBINAISON, en log-cotes, des avis disponibles : marché « sharp » (The Odds API, Pinnacle), marché 1xBet (sa cote sans marge), modèle de Kelly.
# Mode « mixte » (phase 2) : l'ensemble DÉCIDE pour les familles où le marché a prouvé qu'il vaut mieux que le modèle (ENSEMBLE_ACTIVE_FAMILIES : buts,
# 1N2, double chance, les 2 marquent) ; ailleurs (corners, cartons, tirs, fautes, touches, hors-jeu) la règle d'avant reste (proba du modèle face à la
# cote 1xBet, marge FAIR_MARGIN), mais les avis de l'ensemble y sont calculés, affichés et JOURNALISÉS (« fantôme ») pour que les poids apprennent aussi.
#   ENSEMBLE_ENABLED=0         tout éteint d'un coup : comportement d'avant l'ensemble (rien décidé, calculé, affiché, journalisé ni appris)
#   ENSEMBLE_ACTIVE_FAMILIES   familles où l'ensemble décide (« goals,h2h,dc,btts » ; « none » : aucune, mode fantôme seulement)
#   ENSEMBLE_LOG=0             plus de journal des avis ni d'apprentissage (décisions et affichage inchangés)
ENSEMBLE_ENABLED = _bool("ENSEMBLE_ENABLED", True)
ENSEMBLE_ACTIVE_FAMILIES = _set("ENSEMBLE_ACTIVE_FAMILIES", ("goals", "h2h", "dc", "btts"))
ENSEMBLE_LOG = _bool("ENSEMBLE_LOG", True)
ENSEMBLE_MARGIN = _float("ENSEMBLE_MARGIN", 0.03)             # valeur : cote 1xBet x proba retenue >= 1 + marge
ENSEMBLE_REQUIRE_MARKET = _bool("ENSEMBLE_REQUIRE_MARKET", True)  # vrai : pas de valeur sans AU MOINS un avis de marché (sharp ou 1xBet sans marge), dans toutes les familles
ENSEMBLE_MAX_DISAGREE = _float("ENSEMBLE_MAX_DISAGREE", 0.12)  # veto : |proba du modèle - proba du marché| au-delà (familles où le marché pèse beaucoup)
ENSEMBLE_VETO_WEIGHT = _float("ENSEMBLE_VETO_WEIGHT", 0.7)    # le veto vaut quand le poids du marché est >= à ce seuil
# Poids a priori du MODÈLE face au marché, par famille (calib.family) : buts / 1N2 / les 2 marquent / double chance : le marché pèse 85 à 90 % ;
# corners, cartons, tirs, fautes, touches, hors-jeu : aucune mesure fiable, moitié-moitié avec la cote 1xBet. « ENSEMBLE_PRIOR_MODEL=goals=0.1,... »
ENSEMBLE_PRIOR_MODEL = {k: min(1.0, v) for k, v in _shares("ENSEMBLE_PRIOR_MODEL", {"goals": 0.12, "btts": 0.12, "dc": 0.12, "h2h": 0.12}).items()}
ENSEMBLE_PRIOR_OTHER = min(1.0, max(0.0, _float("ENSEMBLE_PRIOR_OTHER", 0.5)))   # famille sans a priori propre
ENSEMBLE_PRIOR_SHARP = min(1.0, max(0.0, _float("ENSEMBLE_PRIOR_SHARP", 0.75)))  # part du « sharp » dans l'avis du marché quand sharp ET 1xBet existent
ENSEMBLE_SHARP_MAX_AGE_MIN = _int("ENSEMBLE_SHARP_MAX_AGE_MIN", 180)   # une proba sharp plus vieille (min) n'entre plus dans l'ensemble ; âge inconnu : acceptée
ENSEMBLE_X_MARGIN = _float("ENSEMBLE_X_MARGIN", 0.05)         # marge supposée de 1xBet quand sa cote sans marge n'est pas calculable (double chance sans les 3 cotes)
# Apprentissage (par famille) : posterior bayésien sur une grille de poids, a priori Beta centré sur le poids a priori et d'écart-type
# ENSEMBLE_PRIOR_SD (petit = rappel fort : buts / 1N2 / 2 marquent / double chance, où l'audit a mesuré ; grand : familles non mesurées). Rien ne
# bouge avant ENSEMBLE_MIN_N pronostics réglés (avec les deux avis) ; le passage est progressif sur ENSEMBLE_MIN_N de plus.
ENSEMBLE_PRIOR_SD = {k: max(0.01, v) for k, v in _shares("ENSEMBLE_PRIOR_SD", {"goals": 0.07, "btts": 0.07, "dc": 0.07, "h2h": 0.07}).items()}
ENSEMBLE_PRIOR_SD_OTHER = max(0.01, _float("ENSEMBLE_PRIOR_SD_OTHER", 0.20))   # écart-type a priori du poids du modèle ailleurs
ENSEMBLE_PRIOR_SD_SHARP = max(0.01, _float("ENSEMBLE_PRIOR_SD_SHARP", 0.15))   # écart-type a priori de la part du sharp (ws)
ENSEMBLE_MIN_N = _float("ENSEMBLE_MIN_N", 100)
ENSEMBLE_HALF_LIFE = _float("ENSEMBLE_HALF_LIFE", 2000)       # oubli exponentiel : un résultat compte moitié moins après autant de résultats
ENSEMBLE_TEMP = _float("ENSEMBLE_TEMP", 0.5)                  # vraisemblance atténuée : lignes d'un même match corrélées, choix non aléatoire
ENSEMBLE_LOG_DAYS = _int("ENSEMBLE_LOG_DAYS", 10)             # jours gardés dans data/ensemble_log.json (avis en attente de règlement)
# Écarts « sharp » (kelly/sharpgap.py) : cote 1xBet payée au-dessus de la cote juste des books de référence.
SHARPGAP_EDGE = _float("SHARPGAP_EDGE", 0.03)                 # écart minimal : cote 1xBet x proba sharp - 1
SHARPGAP_MAX_EDGE = _float("SHARPGAP_MAX_EDGE", 0.25)         # au-delà : donnée suspecte (cote en retard, mauvais pari), écartée
SHARPGAP_MAX_AGE_MIN = _int("SHARPGAP_MAX_AGE_MIN", 120)      # âge maximal du relevé sharp (min) ; âge inconnu = refusé
SHARPGAP_MAX_X_AGE_MIN = _int("SHARPGAP_MAX_X_AGE_MIN", 240)  # âge maximal du relevé 1xBet (min) ; inconnu = accepté (re-vérifié en direct avant envoi)
SHARPGAP_SOON_MIN = _int("SHARPGAP_SOON_MIN", 20)             # match qui commence dans moins de N min : trop tard pour jouer
SHARPGAP_MIN_SHARP = _int("SHARPGAP_MIN_SHARP", 2)            # books de référence au moins, sauf si Pinnacle en fait partie
# Paris « 💰 1xBet paie plus que le marché » (phase 2) : annoncés le matin et aux relevés, suivis comme les autres paris (note, argent, clôture, bilan).
SHARPGAP_ENABLED = _bool("SHARPGAP_ENABLED", True)            # 0 : plus de section, plus d'alerte, plus de suivi neuf
SHARPGAP_MAX_SHOWN = _int("SHARPGAP_MAX_SHOWN", 3)            # paris au plus dans la section du matin (les plus gros écarts d'abord)
SHARPGAP_MAX_DAY = _int("SHARPGAP_MAX_DAY", 6)                # paris au plus annoncés par jour (matin + relevés + alertes)
SHARPGAP_PRESCREEN = _float("SHARPGAP_PRESCREEN", 0.02)       # relevés et alertes : seuls les paris à moins de 2 points du seuil, au prix du matin, sont relus en direct
# ===================================================================================================================================

# ===== FENÊTRE DES STATS (lot M, kelly/statswindow.py) : bloc à part, ne dépend d'aucun autre réglage ================================
# 1xBet n'ouvre les sous-matchs « tirs, tirs cadrés, fautes, touches, hors-jeu » que quelques heures avant le coup d'envoi (et les ferme ~30 min avant) : au
# relevé de 04:00 ils sont presque tous fermés. Le passage horaire (`bot.py --track`, :05 de 05h à 23h) relit donc, pour les SEULS matchs qui commencent dans
# la fenêtre [STATS_WINDOW_MIN_MIN min ; STATS_WINDOW_MAX_H h], les sous-matchs stats de leurs pronostics stats, remet ces pronostics à la cote 1xBet,
# envoie au plus STATS_WINDOW_MAX_COMBOS « 🧮 COMBINÉ STATS » par jour (palier « ts5w<n> ») et annonce les nouveaux 💎 stats.
# Données (≈ 6,4 Ko par requête, voir XBET_MONTH_MB) : un match relu = 1 requête du match + 1 par famille stats ouverte, soit ~12 à 38 Ko ;
# plafonds : STATS_WINDOW_MAX_MATCHES par passage, STATS_WINDOW_DAY_READS par jour, jamais au-delà de STATS_WINDOW_MONTH_PCT % du plafond mensuel.
STATS_WINDOW_ENABLED = _bool("STATS_WINDOW_ENABLED", True)          # 0 : plus de relecture horaire des stats (comportement d'avant)
STATS_WINDOW_MIN_MIN = _int("STATS_WINDOW_MIN_MIN", 45)             # un match qui commence dans moins de N min n'est plus relu : 1xBet ferme les sous-matchs stats ~30 min avant
STATS_WINDOW_MAX_H = _float("STATS_WINDOW_MAX_H", 4)                # ... ni avant N heures : trop tôt, les sous-matchs stats ne sont pas encore ouverts
STATS_WINDOW_SECONDS = _int("STATS_WINDOW_SECONDS", 120)            # durée maximale de la relecture d'un passage (1 s entre requêtes en direct)
STATS_WINDOW_MAX_MATCHES = _int("STATS_WINDOW_MAX_MATCHES", 8)      # matchs relus au plus par passage (ceux qui ont le plus de pronostics stats d'abord)
STATS_WINDOW_DAY_READS = _int("STATS_WINDOW_DAY_READS", 48)         # relectures de match au plus par jour (tous passages)
STATS_WINDOW_TRIES = _int("STATS_WINDOW_TRIES", 4)                  # relectures réussies au plus d'un même match par jour (la fenêtre de 4 h compte 4 passages)
STATS_WINDOW_MISSES = _int("STATS_WINDOW_MISSES", 3)                # passages sans réponse de 1xBet pour un même match avant d'y renoncer pour la journée
STATS_WINDOW_RETRY_MIN = _int("STATS_WINDOW_RETRY_MIN", 45)         # minutes au moins entre deux relectures d'un même match
STATS_WINDOW_FRESH_MIN = _int("STATS_WINDOW_FRESH_MIN", 50)         # un pronostic stats à cote 1xBet lue il y a moins de N min n'est pas relu ; plus vieux, il ne sert pas dans un combiné
STATS_WINDOW_MONTH_PCT = _float("STATS_WINDOW_MONTH_PCT", 85)       # % du plafond mensuel (XBET_MONTH_MB) au-delà duquel la fenêtre se tait (le matin passe avant)
STATS_WINDOW_MAX_COMBOS = _int("STATS_WINDOW_MAX_COMBOS", 2)        # « 🧮 COMBINÉ STATS » au plus par jour, hors celui du matin (0 : aucun)
STATS_WINDOW_COMBO = (5.0, 7.0)                                     # fourchette de cote du combiné (comme « ts5 »)
STATS_WINDOW_ANNOUNCE = _int("STATS_WINDOW_ANNOUNCE", 6)            # nouveaux 💎 stats au plus dans un message « 🧮 Stats ouvertes chez 1xBet » (0 : aucun message)
STATS_WINDOW_DAY_ANNOUNCE = _int("STATS_WINDOW_DAY_ANNOUNCE", 12)   # nouveaux 💎 stats annoncés au plus par jour
STATS_WINDOW_NOTE = _bool("STATS_WINDOW_NOTE", True)                # 0 : plus de ligne « les paris stats arrivent quelques heures avant les matchs » au message du matin
STATS_WINDOW_PREFIX = ("team_fouls_", "team_throws_", "team_offsides_", "team_shots_", "team_sot_")   # marchés « stats » (ceux de « ts5 »)
STATS_WINDOW_TIER = "ts5w"                                          # palier d'un combiné de la fenêtre : « ts5w1 », « ts5w2 »... (« ts5 » reste celui du matin)
STATS_WINDOW_TITLE = "🧮 COMBINÉ STATS"
# ===================================================================================================================================
