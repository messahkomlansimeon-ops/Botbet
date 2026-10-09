"""Fiabilité de Kelly par famille de marché, mesurée sur ses pronostics sans cote réglés gratuitement (data/fair.json n'en garde
que FAIR_KEEP_DAYS jours ; ce cumul-ci est gardé pour toujours dans data/calib.json, quelques centaines d'octets).

data/calib.json : {famille: [n, gagnés, somme des probas brutes], "_lg": {championnat: {famille: [...]}}}. Proba « brute »
(c["p0"]) = avant la correction ci-dessous : la correction ne se mesure jamais elle-même.

Par championnat : dès FAIR_CALIB_MIN pronostics d'une famille dans un championnat (corners au Brésil...), la correction de ce
championnat remplace celle de la famille, rapprochée d'elle tant que les pronostics sont peu nombreux. Kelly peut être juste sur
les corners en Europe et trop sûre d'elle au Brésil.

Correction (factor) : la proba annoncée est 50 % + k x (proba brute - 50 %). k = (réussite - 50 %) / (proba brute moyenne - 50 %),
ramené vers 1 tant que les pronostics sont peu nombreux (poids n / (n + FAIR_CALIB_PRIOR)), borné à K_RANGE, et 1 sous
FAIR_CALIB_MIN pronostics réglés. k < 1 : Kelly est trop sûre d'elle sur ce marché, ses probas sont rapprochées de 50 % et la
« cote mini » à exiger chez 1xBet monte.
"""
import time

from . import catalogue, config as C, store

FILE = "calib.json"
K_RANGE = (0.4, 1.2)
SHOW_MIN = 20              # pronostics réglés minimum pour afficher une famille dans le bilan
FAMILIES = {"totals": "goals", "btts": "btts", "double_chance": "dc", "h2h": "h2h",
            "alternate_totals_corners": "corners", "alternate_totals_cards": "cards"}
LABELS = {"goals": "buts", "btts": "les 2 marquent", "dc": "double chance", "h2h": "1N2", "corners": "corners",
          "cards": "cartons", "shots": "tirs", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu",
          "player": "stats joueurs"}

_memo = {}
RECHECK_SECONDS = 1.0       # le document n'est pas reinterrogé plus d'une fois par seconde (calibrate() est appelé des milliers de fois par passage)
_clock = time.monotonic


def family(market):
    if market in FAMILIES:
        return FAMILIES[market]
    if market.startswith("team_"):                 # team_sot_total -> sot, team_throws_home -> throws
        return market.split("_")[1]
    return "player" if market.startswith("player_") else market


def load():
    """Cumul de fiabilité (data/calib.json), gardé en mémoire et relu dès que le document change dans la base (store.version, comme
    strength.tuning) : le bot Telegram qui tourne en continu voit le cumul que le règlement du matin vient d'écrire."""
    now = _clock()
    if "data" in _memo and now - _memo.get("checked", float("-inf")) < RECHECK_SECONDS:
        return _memo["data"]
    sig = store.version(FILE)
    _memo["checked"] = now
    if "data" not in _memo or _memo.get("sig") != sig:
        d = store.load(FILE, {})
        _memo["data"], _memo["sig"] = (d if isinstance(d, dict) else {}), sig
    return _memo["data"]


def add(rows):
    """Ajoute au cumul les pronostics réglés (gagnés ou perdus) pas encore comptés (marqués « calib »). -> nombre ajouté."""
    data, n = load(), 0
    for r in rows:
        if r.get("calib") or r.get("result") not in ("win", "loss"):
            continue
        fam, p0, won = family(r["market"]), r.get("p0") or r["prob"], r["result"] == "win"
        accs = [data.setdefault(fam, [0, 0, 0.0])]
        if r.get("sport_key"):
            accs.append(data.setdefault("_lg", {}).setdefault(r["sport_key"], {}).setdefault(fam, [0, 0, 0.0]))
        for acc in accs:
            acc[0] += 1
            acc[1] += won
            acc[2] = round(acc[2] + p0, 4)
        r["calib"] = True
        n += 1
    if n:
        store.save(FILE, data, compact=True)
        _memo["sig"], _memo["checked"] = store.version(FILE), _clock()          # ce qu'on vient d'écrire est déjà en mémoire
    return n


def _k(acc, prior_k=1.0):
    """k d'un cumul [n, gagnés, somme des probas], rapproché de `prior_k` tant que n est petit ; None sous FAIR_CALIB_MIN."""
    if not acc or acc[0] < C.FAIR_CALIB_MIN:
        return None
    n, w, s = acc
    p0 = s / n - 0.5
    if p0 < 0.02:
        return None
    raw = (w / n - 0.5) / p0
    k = prior_k + (raw - prior_k) * n / (n + C.FAIR_CALIB_PRIOR)
    return max(K_RANGE[0], min(K_RANGE[1], k))


def factor(market, data=None, sport_key=None):
    """k du marché (1 = pas de correction) : celui du championnat `sport_key` s'il a assez de pronostics de cette famille, sinon celui
    de la famille."""
    data = data if data is not None else load()
    fam = family(market)
    k = _k(data.get(fam))
    k = 1.0 if k is None else k
    lg = _k(((data.get("_lg") or {}).get(sport_key) or {}).get(fam), k) if sport_key else None
    return k if lg is None else lg


def calibrate(market, p, data=None, sport_key=None):
    """Proba corrigée d'un pronostic de proba brute `p` (championnat `sport_key` facultatif)."""
    return 0.5 + (p - 0.5) * factor(market, data, sport_key)


def lines(data=None, detail=False):
    """« corners : 58 % réalisés pour 66 % annoncés (120) » par famille (texte brut), des plus suivies aux moins suivies."""
    data = data if data is not None else load()
    out = []
    for fam, (n, w, s) in sorted(((f, a) for f, a in data.items() if not f.startswith("_")), key=lambda kv: -kv[1][0]):
        if n < SHOW_MIN:
            continue
        txt = f"{LABELS.get(fam, fam)} : {round(w / n * 100)} % réalisés pour {round(s / n * 100)} % annoncés ({n})"
        k = factor(next((m for m, f in FAMILIES.items() if f == fam), fam), data)
        if detail and k != 1.0:
            txt += f", correction k = {k:.2f}"
        out.append(txt)
    return out


def league_name(sk):
    return "Afrique" if sk == "africa" else catalogue.name(sk)


def league_rows(data=None, min_n=SHOW_MIN):
    """[(championnat, n, réussite, proba annoncée moyenne)] tous marchés confondus, championnats d'au moins `min_n` pronostics
    réglés, du plus fiable (réussite - annonce la plus haute) au moins fiable."""
    out = []
    for sk, fams in ((data if data is not None else load()).get("_lg") or {}).items():
        n, w, s = (sum(a[i] for a in fams.values()) for i in range(3))
        if n >= min_n:
            out.append((league_name(sk), n, w / n, s / n))
    return sorted(out, key=lambda x: x[3] - x[2])


def league_lines(data=None, limit=None):
    """« Brasileirão : 64 % réalisés pour 66 % annoncés (45) », du championnat le plus fiable au moins fiable."""
    rows = league_rows(data)
    return [f"{nm} : {round(w * 100)} % réalisés pour {round(s * 100)} % annoncés ({n})" for nm, n, w, s in rows[:limit]]
