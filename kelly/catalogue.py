"""Catalogue mondial des ligues : région, nom affiché et ordre de priorité (voir config.CATALOGUE)."""
from . import config as C

REGION_LABEL = dict(C.REGIONS)
REGION_ORDER = [r for r, _ in C.REGIONS]
OTHER = "autre"
_INFO = {}          # clé exacte ou préfixe -> (région, nom)
_TIER2 = set()      # clés de rang 2 (voir config.SECOND_TIER)
for _r in REGION_ORDER:
    _second = False
    for _k, _n in C.CATALOGUE[_r]:
        _INFO[_k] = (_r, _n)
        _second = _second or C.SECOND_TIER.get(_r) == _k
        if _second:
            _TIER2.add(_k)


def _lookup(key):
    if key in _INFO:
        return _INFO[key], ""
    for pat, info in _INFO.items():
        if pat.endswith("_") and key.startswith(pat):
            return info, key[len(pat):].replace("_", " ")
    return None, ""


def region(key):
    """Région d'une ligue (clé The Odds API) : europe | ameriques | asie | afrique | monde | autre."""
    info, _ = _lookup(key)
    return info[0] if info else OTHER


def name(key):
    """Nom lisible d'une ligue (le préfixe « Qualif. Coupe du monde » est complété par le suffixe de la clé)."""
    info, rest = _lookup(key)
    if info:
        return f"{info[1]} ({rest})" if rest else info[1]
    return key.split("_", 1)[-1].replace("_", " ").title()


def region_label(r):
    return REGION_LABEL.get(r, "🌐 Autres")


def is_break_day(counts):
    """Vrai si aucune grande affiche (config.TOP_LEAGUES : 5 grands championnats, coupes d'Europe) ne joue
    aujourd'hui : trêve internationale ou jour creux. `counts` : {clé: nombre de matchs}."""
    return not any(counts.get(k) for k in C.TOP_LEAGUES)


def region_rank(r, break_day=False):
    """Rang d'une région (0 = d'abord). Les jours sans grande affiche, l'international passe en tête : ce sont les
    seuls gros matchs du jour."""
    if r == "monde" and break_day:
        return -1
    return REGION_ORDER.index(r) if r in REGION_ORDER else len(REGION_ORDER)


def tier(key):
    """0 = grande affiche (config.TOP_LEAGUES), 1 = ligue majeure de sa région, 2 = 2e division, petit championnat ou
    coupe nationale (config.SECOND_TIER)."""
    if key in C.TOP_LEAGUES:
        return 0
    info, _ = _lookup(key)
    pat = next((p for p, i in _INFO.items() if i is info), key)
    return 2 if pat in _TIER2 else 1


def prioritise(counts, order=None, covered=()):
    """Clés des ligues qui jouent (n > 0), dans l'ordre de paiement : région (international en tête les jours sans
    grande affiche), puis grandes affiches, ligues majeures et petites ligues, puis `covered` (ligues dont Kelly a déjà les
    profils d'équipe, football-data ou API-Football : leurs corners et cartons sont chiffrés, donc comparables aux cotes), puis
    nombre de matchs décroissant, puis ordre du catalogue. `order` : liste des clés dans l'ordre de config.SPORTS (départage
    les ex æquo)."""
    order = {k: i for i, k in enumerate(order or [])}
    brk = is_break_day(counts)
    live = [k for k, n in counts.items() if n]
    return sorted(live, key=lambda k: (region_rank(region(k), brk), tier(k), k not in covered, -counts[k],
                                       order.get(k, len(order)), k))


_ALIASES = {"europe": "europe", "ameriques": "ameriques", "amerique": "ameriques", "americas": "ameriques",
            "amerique du sud": "ameriques", "asie": "asie", "asia": "asie", "oceanie": "asie", "afrique": "afrique",
            "africa": "afrique", "international": "monde", "monde": "monde", "selections": "monde", "sélections": "monde"}


def norm_region(text):
    """Région désignée par un mot de commande (/monde europe, /monde asie...), ou None."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower().strip()
    return _ALIASES.get(t) or next((r for a, r in _ALIASES.items() if a.startswith(t) and len(t) >= 3), None)
