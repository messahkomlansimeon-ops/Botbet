"""Fiabilité de Kelly par famille de marché, mesurée sur ses pronostics sans cote réglés gratuitement (data/fair.json n'en garde
que FAIR_KEEP_DAYS jours ; ce cumul-ci est gardé pour toujours dans data/calib.json, quelques centaines d'octets).

data/calib.json : {famille: [n, gagnés, somme des probas brutes]}. Proba « brute » (c["p0"]) = avant la correction ci-dessous :
la correction ne se mesure jamais elle-même.

Correction (factor) : la proba annoncée est 50 % + k x (proba brute - 50 %). k = (réussite - 50 %) / (proba brute moyenne - 50 %),
ramené vers 1 tant que les pronostics sont peu nombreux (poids n / (n + FAIR_CALIB_PRIOR)), borné à K_RANGE, et 1 sous
FAIR_CALIB_MIN pronostics réglés. k < 1 : Kelly est trop sûre d'elle sur ce marché, ses probas sont rapprochées de 50 % et la
« cote mini » à exiger chez 1xBet monte.
"""
from . import config as C, store

FILE = "calib.json"
K_RANGE = (0.4, 1.2)
SHOW_MIN = 20              # pronostics réglés minimum pour afficher une famille dans le bilan
FAMILIES = {"totals": "goals", "btts": "btts", "double_chance": "dc", "h2h": "h2h",
            "alternate_totals_corners": "corners", "alternate_totals_cards": "cards"}
LABELS = {"goals": "buts", "btts": "les 2 marquent", "dc": "double chance", "h2h": "1N2", "corners": "corners",
          "cards": "cartons", "shots": "tirs", "sot": "tirs cadrés", "player": "stats joueurs"}

_memo = {}


def family(market):
    if market in FAMILIES:
        return FAMILIES[market]
    if market.startswith("team_sot_"):
        return "sot"
    if market.startswith("team_shots_"):
        return "shots"
    return "player" if market.startswith("player_") else market


def load():
    if "data" not in _memo:
        d = store.load(FILE, {})
        _memo["data"] = d if isinstance(d, dict) else {}
    return _memo["data"]


def add(rows):
    """Ajoute au cumul les pronostics réglés (gagnés ou perdus) pas encore comptés (marqués « calib »). -> nombre ajouté."""
    data, n = load(), 0
    for r in rows:
        if r.get("calib") or r.get("result") not in ("win", "loss"):
            continue
        acc = data.setdefault(family(r["market"]), [0, 0, 0.0])
        acc[0] += 1
        acc[1] += r["result"] == "win"
        acc[2] = round(acc[2] + (r.get("p0") or r["prob"]), 4)
        r["calib"] = True
        n += 1
    if n:
        store.save(FILE, data, compact=True)
    return n


def factor(market, data=None):
    """k de la famille du marché (1 = pas de correction)."""
    acc = (data if data is not None else load()).get(family(market))
    if not acc or acc[0] < C.FAIR_CALIB_MIN:
        return 1.0
    n, w, s = acc
    p0 = s / n - 0.5
    if p0 < 0.02:
        return 1.0
    raw = (w / n - 0.5) / p0
    k = 1 + (raw - 1) * n / (n + C.FAIR_CALIB_PRIOR)
    return max(K_RANGE[0], min(K_RANGE[1], k))


def calibrate(market, p, data=None):
    """Proba corrigée d'un pronostic de proba brute `p`."""
    return 0.5 + (p - 0.5) * factor(market, data)


def lines(data=None, detail=False):
    """« corners : 58 % réalisés pour 66 % annoncés (120) » par famille (texte brut), des plus suivies aux moins suivies."""
    data = data if data is not None else load()
    out = []
    for fam, (n, w, s) in sorted(data.items(), key=lambda kv: -kv[1][0]):
        if n < SHOW_MIN:
            continue
        txt = f"{LABELS.get(fam, fam)} : {round(w / n * 100)} % réalisés pour {round(s / n * 100)} % annoncés ({n})"
        k = factor(next((m for m, f in FAMILIES.items() if f == fam), fam), data)
        if detail and k != 1.0:
            txt += f", correction k = {k:.2f}"
        out.append(txt)
    return out
