import difflib
import re
import unicodedata

try:
    from ..stats import team_sim as _team_sim
except Exception:
    _team_sim = None

_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L",
                           "đ": "d", "Đ": "D", "ß": "ss", "ı": "i"})
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
    "greuther furth": "furth", "spvgg greuther furth": "furth", "austria wien": "austria vienna",
}
_SUFFIX = {"jr", "junior", "sr", "filho"}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and ")).split())


def _pre(name):
    n = norm(name)
    if n in _EXTRA:
        return _EXTRA[n]
    n = norm(str(name or "").replace("'", "").replace("\u2019", ""))         # « Newell's » = « Newells »
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
    a, b = _pre(a), _pre(b)
    if _team_sim:
        try:
            return _team_sim(a, b)
        except Exception:
            pass
    return _fallback_sim(a, b)


def best_team(name, candidates, min_sim=0.8, margin=0.05):
    """Candidat le plus proche de `name`, ou None. Deux candidats différents à `margin` près l'un de l'autre sont ambigus (« Tokyo Verdy »
    ~ « FC Tokyo » et « Verdy ») : None, sauf si l'un est le nom exact (casse et accents près). Plusieurs noms d'une même équipe
    (même profil dans un dict) ne sont pas une ambiguïté."""
    scored = sorted(((team_sim(name, c), i, c) for i, c in enumerate(candidates)), key=lambda x: (-x[0], x[1]))
    if not scored or scored[0][0] < min_sim:
        return None
    best_s, _, best = scored[0]
    close = [c for s, _, c in scored if s >= best_s - margin]
    ident = lambda c: id(candidates[c]) if hasattr(candidates, "items") and isinstance(candidates[c], (dict, list)) else c    # noqa: E731
    if len({ident(c) for c in close}) == 1:
        return best
    exact = [c for c in close if norm(c) == norm(name)]
    return exact[0] if len({ident(c) for c in exact}) == 1 else None


def _pkey(name):
    toks = [t for t in norm(name).split() if t not in _SUFFIX]
    if not toks:
        return None, frozenset(), frozenset()
    return (toks[0][0] if len(toks) > 1 else None), frozenset(toks[1:] if len(toks) > 1 else toks), frozenset(toks)


def player_match(name, candidates):
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
