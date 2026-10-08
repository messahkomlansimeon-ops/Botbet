"""Ligues d'API-Football utiles aux profils mondiaux (kelly/apistats.py) : pays + nom de la ligue -> clé du catalogue.

Chaque matin Kelly lit la liste de TOUS les matchs d'hier (une requête, ~230 matchs dans le monde) mais ne peut payer les
statistiques que de ~55 matchs : cette table dit lesquels valent une requête. On ne garde que les ligues dont football-data
ne donne pas corners ni cartons (ses grands championnats européens sont déjà détaillés) et les compétitions de sélections :

  GAP       100  ligue du catalogue sans corners ni cartons chez football-data (Brésil, Argentine, MLS, Mexique, Chili, Japon,
                 Corée, Chine, Arabie saoudite, Australie, pays nordiques, Suisse, Autriche, Pologne, Irlande, coupes sud-américaines)
  AFRICA     80  championnats nationaux africains et compétitions CAF (reconnus par stats.african_league, pas par cette table)
  INTL       70  qualifications de Coupe du monde / Euro, Ligue des nations, compétitions continentales de sélections
  FRIENDLY   50  matchs amicaux de sélections
  EXTRA      30  ligues absentes du catalogue (Colombie, Uruguay...) : utiles à /stats, pas encore aux pronostics

Rien n'est retenu hors de ces règles : réserves, jeunes et féminines ne passent jamais (motif _NOT_SENIOR), pas plus que les
coupes nationales. Les noms sont ceux d'API-Football (pays en anglais) ; une ligue renommée tombe simplement hors table.
"""
import re
import unicodedata

GAP, AFRICA, INTL, FRIENDLY, EXTRA = 100, 80, 70, 50, 30

_NOT_SENIOR = re.compile(r"women|femin|\bu\d\d\b|youth|reserve|\bres\b|junior", re.I)

# (pays, motif du nom, clé The Odds API, nom affiché, priorité)
RULES = [
    ("brazil", r"^serie a$", "soccer_brazil_campeonato", "Brasileirão", GAP),
    ("brazil", r"^serie b$", "soccer_brazil_serie_b", "Brasileirão Série B", GAP),
    ("argentina", r"^liga profesional|^primera division$|^superliga", "soccer_argentina_primera_division", "Primera argentine", GAP),
    ("usa", r"^major league soccer$|^mls$", "soccer_usa_mls", "MLS", GAP),
    ("mexico", r"^liga mx$", "soccer_mexico_ligamx", "Liga MX", GAP),
    ("chile", r"^primera division$", "soccer_chile_campeonato", "Primera chilienne", GAP),
    ("japan", r"^j1 league$|^j league$|^j-league$", "soccer_japan_j_league", "J League", GAP),
    ("south korea", r"^k league 1$", "soccer_korea_kleague1", "K League 1", GAP),
    ("china", r"^super league$|^chinese super league$", "soccer_china_superleague", "Super League chinoise", GAP),
    ("saudi arabia", r"^pro league$|^saudi pro league$|professional league", "soccer_saudi_arabia_pro_league", "Saudi Pro League", GAP),
    ("australia", r"^a league", "soccer_australia_aleague", "A-League", GAP),
    ("denmark", r"^superliga$", "soccer_denmark_superliga", "Superliga danoise", GAP),
    ("norway", r"^eliteserien$", "soccer_norway_eliteserien", "Eliteserien", GAP),
    ("sweden", r"^allsvenskan$", "soccer_sweden_allsvenskan", "Allsvenskan", GAP),
    ("sweden", r"^superettan$", "soccer_sweden_superettan", "Superettan", GAP),
    ("finland", r"^veikkausliiga$", "soccer_finland_veikkausliiga", "Veikkausliiga", GAP),
    ("poland", r"^ekstraklasa$", "soccer_poland_ekstraklasa", "Ekstraklasa", GAP),
    ("austria", r"^bundesliga$", "soccer_austria_bundesliga", "Bundesliga autrichienne", GAP),
    ("switzerland", r"^super league$", "soccer_switzerland_superleague", "Super League suisse", GAP),
    ("ireland", r"^premier division$", "soccer_league_of_ireland", "Ligue irlandaise", GAP),
    ("germany", r"^3\. liga$", "soccer_germany_liga3", "3. Liga", GAP),
    ("world", r"libertadores", "soccer_conmebol_copa_libertadores", "Copa Libertadores", GAP),
    ("world", r"sudamericana", "soccer_conmebol_copa_sudamericana", "Copa Sudamericana", GAP),
    ("world", r"^leagues cup$", "soccer_concacaf_leagues_cup", "Leagues Cup", GAP),
    ("world", r"^uefa nations league$", "soccer_uefa_nations_league", "Ligue des nations UEFA", INTL),
    ("world", r"^concacaf nations league", "soccer_concacaf_nations_league", "Ligue des nations Concacaf", INTL),
    ("world", r"^world cup qualification europe$", "soccer_fifa_world_cup_qualifiers_europe", "Qualif. Coupe du monde Europe", INTL),
    ("world", r"^world cup qualification south america$", "soccer_fifa_world_cup_qualifiers_south_america",
     "Qualif. Coupe du monde Amérique du Sud", INTL),
    ("world", r"^world cup qualification (?:africa|asia|concacaf|oceania)", None, "Qualif. Coupe du monde", INTL),
    ("world", r"^(?:uefa )?euro(?: championship)? qualification$", "soccer_uefa_euro_qualifiers", "Qualif. Euro", INTL),
    ("world", r"^(?:uefa )?euro(?: championship)?$", "soccer_uefa_european_championship", "Euro", INTL),
    ("world", r"^world cup$", "soccer_fifa_world_cup", "Coupe du monde", INTL),
    ("world", r"^copa america$", "soccer_conmebol_copa_america", "Copa América", INTL),
    ("world", r"^concacaf gold cup$|^gold cup$", "soccer_concacaf_gold_cup", "Gold Cup", INTL),
    ("world", r"^friendlies$", "soccer_international_friendlies", "Matchs amicaux", FRIENDLY),
    ("colombia", r"^primera a$", "soccer_colombia_primera_a", "Primera colombienne", EXTRA),
    ("uruguay", r"^primera division$", "soccer_uruguay_primera_division", "Primera uruguayenne", EXTRA),
    ("peru", r"^liga 1$", "soccer_peru_liga_1", "Liga 1 péruvienne", EXTRA),
    ("ecuador", r"^liga ?pro$|^serie a$", "soccer_ecuador_ligapro", "LigaPro", EXTRA),
    ("paraguay", r"^primera division|^division profesional", "soccer_paraguay_primera", "Primera paraguayenne", EXTRA),
    ("qatar", r"^stars league$", "soccer_qatar_stars_league", "Stars League", EXTRA),
    ("united arab emirates", r"^pro league$|^uae league", "soccer_uae_pro_league", "UAE Pro League", EXTRA),
    ("costa rica", r"^primera division$", "soccer_costa_rica_primera", "Primera costaricienne", EXTRA),
]
_COMPILED = [(c, re.compile(p, re.I), k, n, w) for c, p, k, n, w in RULES]


def priority(key):
    """Priorité (GAP, AFRICA, INTL, FRIENDLY, EXTRA) d'une clé de ligue retenue par classify ou « africa » ; 0 si inconnue."""
    if key == "africa":
        return AFRICA
    if str(key).startswith("soccer_fifa_world_cup_qualifiers_"):
        return INTL
    return next((w for _, _, k, _, w in _COMPILED if k == key), 0)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9. ]+", " ", s).split())


def classify(country, name):
    """(clé The Odds API, nom affiché, priorité) d'une ligue d'API-Football, ou None si elle ne vaut pas une requête.
    Les ligues déjà détaillées par football-data (corners et cartons) et les championnats africains (stats.african_league) n'y sont
    pas : None."""
    nm, ct = _norm(name), _norm(country)
    if not nm or not ct or _NOT_SENIOR.search(str(name)):
        return None
    for c, rx, key, label, pri in _COMPILED:
        m = rx.search(nm)
        if c == ct and m:
            if key is None:                                # qualifications hors Europe / Amérique du Sud : une clé par continent
                tail = nm.split("qualification", 1)[1].strip().replace(" ", "_").replace(".", "")
                key = "soccer_fifa_world_cup_qualifiers_" + tail
                label = f"Qualif. Coupe du monde ({tail.replace('_', ' ')})"
            return key, label, pri
    return None
