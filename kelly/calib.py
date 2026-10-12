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

Correction fine (data["_cell"]) : en plus de la famille, Kelly cumule par sens du pari (side : plus / moins, oui / non, dom / nul / ext, 1X / X2 / 12)
et, dans chaque sens, par tranche de proba brute (C.CALIB_BANDS : < 60 %, 60-70 %, >= 70 %). Chaque niveau est rapproché du précédent tant qu'il a
peu de résultats (même formule) ; le k des tranches est interpolé entre leurs centres et borné pour qu'une proba brute plus haute ne donne jamais une
proba corrigée plus basse. Kelly peut être juste à 55 % et trop sûre d'elle à 75 %, ou sur les « plus de » et pas sur les « moins de ».

Mémoire qui vieillit : tous les cumuls (familles, championnats, cases, jambes de combinés) sont multipliés par 0,5^(jours / C.CALIB_HALF_LIFE) à
chaque ajout (data["_t"] : date du dernier vieillissement). Les rapports ne bougent pas, les vieux résultats pèsent moins (n devient un nombre
effectif, décimal).

Jambes de combinés (data["_combo"]) : réussite des jambes des combinés réglés face à leur proba (déjà corrigée) ; combos.leg_discount en tire la
prudence par jambe.
"""
import time
from datetime import date, datetime, timezone

from . import catalogue, config as C, store

FILE = "calib.json"
K_RANGE = (0.4, 1.2)
SHOW_MIN = 20              # pronostics réglés minimum pour afficher une famille dans le bilan
FAMILIES = {"totals": "goals", "btts": "btts", "double_chance": "dc", "h2h": "h2h", "team_totals_home": "team_goals", "team_totals_away": "team_goals",
            "alternate_totals_corners": "corners", "alternate_totals_cards": "cards"}
LABELS = {"goals": "buts", "team_goals": "buts d'une équipe", "btts": "les 2 marquent", "dc": "double chance", "h2h": "1N2", "corners": "corners",
          "cards": "cartons", "yellow": "cartons jaunes", "shots": "tirs", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu",
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


def _acc_list(v):
    return isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)


def scale(node, f):
    """Multiplie en place chaque cumul [n, gagnés, somme] de `node` (dictionnaires imbriqués) par `f`."""
    for k, v in node.items():
        if _acc_list(v):
            node[k] = [round(v[0] * f, 4), round(v[1] * f, 4), round(v[2] * f, 4)]
        elif isinstance(v, dict):
            scale(v, f)


def aged(node, since, today=None):
    """Vieillit `node` depuis la date `since` (« AAAA-MM-JJ », None : pas encore datée) jusqu'à `today` (C.CALIB_HALF_LIFE) ; -> la date du jour
    (à garder pour la fois suivante). Rien ne bouge le jour même ; demi-vie 0 : jamais d'oubli."""
    today = today or datetime.now(timezone.utc).date()
    try:
        days = (today - date.fromisoformat(str(since)[:10])).days if since else 0
    except ValueError:
        days = 0
    if days > 0 and C.CALIB_HALF_LIFE > 0:
        scale(node, 0.5 ** (days / C.CALIB_HALF_LIFE))
    return str(today)


def side(c, market=None):
    """Sens d'un pari pour la correction fine : « plus » / « moins », « oui » / « non », « dom » / « nul » / « ext » (1N2, handicap), « 1X » / « X2 » /
    « 12 » (double chance) ; None si on ne le reconnaît pas. Nom du pari : c["name"], sinon la fin de c["key"] ; équipes : home / away, sinon
    « event » (« A - B »)."""
    market = market or c.get("market") or ""
    name = c.get("name") or (str(c["key"]).rsplit("|", 1)[-1] if c.get("key") else "")
    if name in ("Over", "Under"):
        return "plus" if name == "Over" else "moins"
    if name in ("Yes", "No"):
        return "oui" if name == "Yes" else "non"
    home, away = c.get("home"), c.get("away")
    if not (home and away) and " - " in str(c.get("event") or ""):
        home, away = str(c["event"]).split(" - ", 1)
    if market == "double_chance":
        parts = set(name.split(" or "))
        if parts == {home, "Draw"}:
            return "1X"
        if parts == {"Draw", away}:
            return "X2"
        return "12" if parts == {home, away} else None
    if market in ("h2h", "spreads"):
        return "nul" if name == "Draw" else "dom" if name == home else "ext" if name == away else None
    return None


def band(p):
    """Tranche de proba brute (C.CALIB_BANDS) : 0 (< 60 %), 1 (60-70 %), 2 (>= 70 %)."""
    return sum(p >= e for e in C.CALIB_BANDS)


def add(rows, today=None):
    """Ajoute au cumul les pronostics réglés (gagnés ou perdus) pas encore comptés (marqués « calib ») : famille, championnat, sens et tranche
    (data["_cell"]), après avoir vieilli les cumuls (C.CALIB_HALF_LIFE). -> nombre ajouté."""
    data, n = load(), 0
    new = [r for r in rows if not r.get("calib") and r.get("result") in ("win", "loss")]
    if not new:
        return 0
    data["_t"] = aged(data, data.get("_t"), today)
    for r in new:
        fam, p0, won = family(r["market"]), r.get("p0") or r["prob"], r["result"] == "win"
        accs = [data.setdefault(fam, [0, 0, 0.0])]
        if r.get("sport_key"):
            accs.append(data.setdefault("_lg", {}).setdefault(r["sport_key"], {}).setdefault(fam, [0, 0, 0.0]))
        sd = side(r)
        if sd:
            cells = data.setdefault("_cell", {})
            accs += [cells.setdefault(f"{fam}|{sd}", [0, 0, 0.0]), cells.setdefault(f"{fam}|{sd}|{band(p0)}", [0, 0, 0.0])]
        for acc in accs:
            acc[0] = round(acc[0] + 1, 4)
            acc[1] = round(acc[1] + won, 4)
            acc[2] = round(acc[2] + p0, 4)
        r["calib"] = True
        n += 1
    _save(data)
    return n


def add_combo_legs(legs, today=None):
    """Cumule (data["_combo"]) les jambes de combinés réglées (gagnées ou perdues) pas encore comptées (marque « calib_combo ») : réussite face à la
    proba annoncée (déjà corrigée). -> nombre ajouté."""
    new = [leg for leg in legs if not leg.get("calib_combo") and leg.get("result") in ("win", "loss") and leg.get("prob")]
    if not new:
        return 0
    data = load()
    data["_t"] = aged(data, data.get("_t"), today)
    acc = data.setdefault("_combo", [0, 0, 0.0])
    for leg in new:
        acc[0] = round(acc[0] + 1, 4)
        acc[1] = round(acc[1] + (leg["result"] == "win"), 4)
        acc[2] = round(acc[2] + leg["prob"], 4)
        leg["calib_combo"] = True
    _save(data)
    return len(new)


def _save(data):
    store.save(FILE, data, compact=True)
    _memo["data"], _memo["sig"], _memo["checked"] = data, store.version(FILE), _clock()     # ce qu'on vient d'écrire est déjà en mémoire


PROPS_FAMILIES = {"shots", "sot", "fouls", "throws", "offsides", "player"}


def min_need(fam):
    """Pronostics réglés requis pour activer la calibration d'une famille."""
    return C.FAIR_CALIB_MIN_PROPS if fam in PROPS_FAMILIES else C.FAIR_CALIB_MIN


def prior_need(fam):
    """Inertie prioritaire de la calibration d'une famille."""
    return C.FAIR_CALIB_PRIOR_PROPS if fam in PROPS_FAMILIES else C.FAIR_CALIB_PRIOR


def _k(acc, prior_k=1.0, fam=None):
    """k d'un cumul [n, gagnés, somme des probas], rapproché de `prior_k` tant que n est petit ; None sous le seuil requis."""
    min_n = min_need(fam) if fam else C.FAIR_CALIB_MIN
    prior_n = prior_need(fam) if fam else C.FAIR_CALIB_PRIOR
    if not acc or acc[0] < min_n:
        return None
    n, w, s = acc
    p0 = s / n - 0.5
    if p0 < 0.02:
        return None
    raw = (w / n - 0.5) / p0
    k = prior_k + (raw - prior_k) * n / (n + prior_n)
    return max(K_RANGE[0], min(K_RANGE[1], k))


def _centers():
    """Centres des tranches (C.CALIB_BANDS) : 5 points sous la première borne, milieux, 5 points au-dessus de la dernière."""
    e = C.CALIB_BANDS
    return [e[0] - 0.05] + [(a + b) / 2 for a, b in zip(e, e[1:])] + [e[-1] + 0.05]


def _fine(fam, sd, p, k_side, cells):
    """k des tranches du sens `sd` (chacune rapprochée de `k_side`), interpolé en `p` entre les centres. Une tranche ne descend jamais assez sous la
    précédente pour qu'une proba brute plus haute donne une proba corrigée plus basse (k_suivant >= r / (1 + r) x k_précédent, r = écart au milieu
    du centre suivant / largeur, avec une petite marge)."""
    cs = _centers()
    ks = []
    for i in range(len(cs)):
        k = _k(cells.get(f"{fam}|{sd}|{i}"), k_side, fam=fam)
        ks.append(k_side if k is None else k)
    for i in range(1, len(ks)):
        r = (cs[i] - 0.5) / (cs[i] - cs[i - 1])
        ks[i] = max(ks[i], ks[i - 1] * r / (1 + r) * 1.01)
    if p <= cs[0]:
        return ks[0]
    for i in range(1, len(cs)):
        if p <= cs[i]:
            t = (p - cs[i - 1]) / (cs[i] - cs[i - 1])
            return ks[i - 1] + t * (ks[i] - ks[i - 1])
    return ks[-1]


def factor(market, data=None, sport_key=None, side=None, p=None):
    """k du marché (1 = pas de correction), en cascade : famille ; sens du pari `side` (rapproché de la famille) ; tranche de la proba brute `p`
    (rapprochée du sens, interpolée) ; puis l'écart du championnat `sport_key` (k du championnat / k de la famille, s'il a assez de pronostics de
    cette famille). Sans sens, sans proba ou sans résultats dans ces cases : exactement la correction famille / championnat."""
    data = data if data is not None else load()
    fam = family(market)
    k_fam = _k(data.get(fam), fam=fam)
    k_fam = 1.0 if k_fam is None else k_fam
    k = k_fam
    cells = data.get("_cell") or {}
    if side and cells:
        ks = _k(cells.get(f"{fam}|{side}"), k_fam, fam=fam)
        k = k_fam if ks is None else ks
        if p is not None and p >= 0.5:
            k = _fine(fam, side, p, k, cells)
    lg = _k(((data.get("_lg") or {}).get(sport_key) or {}).get(fam), k_fam, fam=fam) if sport_key else None
    if lg is not None:
        k = k * lg / k_fam
    return max(K_RANGE[0], min(K_RANGE[1], k))


def calibrate(market, p, data=None, sport_key=None, side=None):
    """Proba corrigée d'un pronostic de proba brute `p` (championnat `sport_key` et sens `side` facultatifs, voir factor)."""
    return 0.5 + (p - 0.5) * factor(market, data, sport_key, side, p)


def count(n):
    """Nombre de résultats à afficher (le cumul vieilli est décimal)."""
    return int(round(n))


def cell_lines(data=None):
    """« buts · plus · 60-70 % : 58 % réalisés pour 66 % annoncés (84), correction k = 0,86 » : sens et tranches assez suivis (SHOW_MIN), pour
    /bilan détail."""
    data = data if data is not None else load()
    names = ["< " + str(round(C.CALIB_BANDS[0] * 100)) + " %"] + [f"{round(a * 100)}-{round(b * 100)} %" for a, b in zip(C.CALIB_BANDS, C.CALIB_BANDS[1:])] \
        + ["≥ " + str(round(C.CALIB_BANDS[-1] * 100)) + " %"]
    out = []
    for key, (n, w, s) in sorted((data.get("_cell") or {}).items(), key=lambda kv: -kv[1][0]):
        if n < SHOW_MIN:
            continue
        fam, sd, *tr = key.split("|")
        where = f"{LABELS.get(fam, fam)} · {sd}" + (f" · {names[int(tr[0])]}" if tr else "")
        out.append(f"{where} : {round(w / n * 100)} % réalisés pour {round(s / n * 100)} % annoncés ({count(n)})")
    return out


def lines(data=None, detail=False):
    """« corners : 58 % réalisés pour 66 % annoncés (120) » par famille (texte brut), des plus suivies aux moins suivies."""
    data = data if data is not None else load()
    out = []
    for fam, (n, w, s) in sorted(((f, a) for f, a in data.items() if not f.startswith("_") and _acc_list(a)), key=lambda kv: -kv[1][0]):
        if n < SHOW_MIN:
            continue
        txt = f"{LABELS.get(fam, fam)} : {round(w / n * 100)} % réalisés pour {round(s / n * 100)} % annoncés ({count(n)})"
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
    return [f"{nm} : {round(w * 100)} % réalisés pour {round(s * 100)} % annoncés ({count(n)})" for nm, n, w, s in rows[:limit]]
