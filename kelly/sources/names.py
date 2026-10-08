"""Rapprochement des noms d'équipes, de joueurs et d'arbitres entre sources
(football-data, Understat, FBref, The Odds API). Réutilise kelly.stats.team_sim."""
import difflib
import re
import unicodedata

try:
    from ..stats import team_sim as _team_sim
except Exception:                     # stats.py absent ou cassé : repli minimal
    _team_sim = None

_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L",
                           "đ": "d", "Đ": "D", "ß": "ss", "ı": "i"})
# Variantes propres aux sources (clés normalisées) vers un nom que stats.team_sim comprend.
_EXTRA = {
    "nott m forest": "nottingham forest", "man city": "manchester city", "man united": "manchester united",
    "ath madrid": "atletico madrid", "ath bilbao": "athletic bilbao", "espanol": "espanyol",
    "vallecano": "rayo vallecano", "sociedad": "real sociedad", "ein frankfurt": "eintracht frankfurt",
    "m gladbach": "monchengladbach", "borussia m gladbach": "monchengladbach", "fortuna dusseldorf": "dusseldorf",
    "st etienne": "saint etienne", "paris sg": "psg", "sp lisbon": "sporting", "sp braga": "braga",
    "wolverhampton": "wolverhampton wanderers", "sheffield weds": "sheffield wednesday",
    "sheffield utd": "sheffield united", "west brom": "west bromwich albion", "qpr": "queens park rangers",
    "inter milan": "inter", "fc koln": "koln", "1 fc koln": "koln", "mainz 05": "mainz",
    "1 fsv mainz 05": "mainz", "bielefeld": "arminia bielefeld",
    "greuther furth": "furth", "spvgg greuther furth": "furth",
}
_SUFFIX = {"jr", "junior", "sr", "filho"}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and ")).split())


def _pre(name):
    n = norm(name)
    return _EXTRA.get(n, n)


def _fallback_sim(a, b):
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    if a == b:
        return 1.0
    if ta <= tb or tb <= ta:
        return 0.88
    return difflib.SequenceMatcher(None, a, b).ratio()


def team_sim(a, b):
    """Similarité 0..1 entre deux noms d'équipes, tous formats confondus."""
    a, b = _pre(a), _pre(b)
    if _team_sim:
        try:
            return _team_sim(a, b)
        except Exception:
            pass
    return _fallback_sim(a, b)


def best_team(name, candidates, min_sim=0.8):
    """Candidat (parmi `candidates`, itérable de noms) le plus proche de `name`, ou None."""
    best, best_s = None, 0.0
    for c in candidates:
        s = team_sim(name, c)
        if s > best_s:
            best, best_s = c, s
    return best if best_s >= min_sim else None


def _pkey(name):
    toks = [t for t in norm(name).split() if t not in _SUFFIX]
    if not toks:
        return None, frozenset(), frozenset()
    return (toks[0][0] if len(toks) > 1 else None), frozenset(toks[1:] if len(toks) > 1 else toks), frozenset(toks)


def player_match(name, candidates):
    """Nom unique de `candidates` désignant le même joueur (« K. Mbappé » ~ « Kylian Mbappe »,
    « Heung-Min Son » ~ « Son Heung-Min »), sinon None (absent ou ambigu)."""
    qi, qs, qall = _pkey(name)
    if not qs:
        return None
    hits, exact = [], []
    for c in candidates:
        ci, cs, call = _pkey(c)
        if not cs:
            continue
        if call == qall:
            exact.append(c)
        elif (qs <= cs or cs <= qs) and not (qi and ci and qi != ci):
            hits.append(c)
    pool = exact or hits
    return pool[0] if len(set(pool)) == 1 else None
