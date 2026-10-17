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

Rejeu de la saison passée (data/calib_boot.json, kelly/replay.py) : les mêmes cumuls (familles, championnats, cases), mesurés sur la saison passée des
championnats football-data. Ils ne se mélangent jamais aux vrais résultats : la correction qu'ils donnent (même cascade) devient le POINT DE DÉPART
des vrais résultats, à la place de « pas de correction » (k = 1). Sans vrai résultat, c'est elle qui s'applique ; dès qu'il y en a assez, les vrais
résultats s'en écartent avec le même poids qu'avant (n / (n + inertie)). Un vrai signal (tirs : 45 % réalisés pour 65 % annoncés) n'est donc pas
dilué par une saison rejouée où la méthode seule s'est montrée plus juste. C.REPLAY_PRIOR=0 l'ignore.

Jambes de combinés (data["_combo"]) : réussite des jambes des combinés réglés face à leur proba (déjà corrigée) ; combos.leg_discount en tire la
prudence par jambe.
"""
import math
import time
from datetime import date, datetime, timezone

from . import catalogue, config as C, store

FILE = "calib.json"
BOOT_FILE = "calib_boot.json"      # rejeu de la saison passée (kelly/replay.py)
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
    """Cumul de fiabilité des vrais résultats (data/calib.json), gardés en mémoire et relus dès que le document change dans la base (store.version, comme
    strength.tuning) : le bot Telegram qui tourne en continu voit le cumul que le règlement du matin vient d'écrire."""
    now = _clock()
    if "data" in _memo and now - _memo.get("checked", float("-inf")) < RECHECK_SECONDS:
        return _memo["data"]
    sig = store.version(FILE)
    _memo["checked"] = now
    if "data" not in _memo or _memo.get("sig") != sig:
        d = store.load(FILE, {})
        if FILE in store._unreadable:               # lecture ratée (base occupée...) : jamais gardée ; les derniers bons cumuls, relecture au prochain contrôle
            return _memo.get("data", {})
        _memo["data"], _memo["sig"] = (d if isinstance(d, dict) else {}), sig
    return _memo["data"]


def boot():
    """Document du rejeu de la saison passée (data/calib_boot.json, voir kelly/replay.py), {} s'il n'existe pas ; relu dès qu'il change."""
    now = _clock()
    if "boot" in _memo and now - _memo.get("bchecked", float("-inf")) < RECHECK_SECONDS:
        return _memo["boot"]
    sig = store.version(BOOT_FILE)
    _memo["bchecked"] = now
    if "boot" not in _memo or _memo.get("bsig") != sig:
        d = store.load(BOOT_FILE, {})
        if BOOT_FILE in store._unreadable:          # lecture ratée : jamais gardée (le rejeu serait ignoré jusqu'à la prochaine écriture)
            return _memo.get("boot", {})
        _memo["boot"], _memo["bsig"] = (d if isinstance(d, dict) else {}), sig
    return _memo["boot"]


def boot_acc():
    """Cumuls du rejeu de la saison passée ({} sans rejeu ou si C.REPLAY_PRIOR vaut 0)."""
    return (boot().get("acc") or {}) if C.REPLAY_PRIOR else {}


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


VERSION = 2         # cumuls : 2 = chaque MATCH compte une fois par famille (ses k lignes pèsent 1/k chacune) ; 1 = chaque ligne comptait 1 (3 lignes de tirs
                    # d'un même match comptaient 3 fois : les tirs sont sortis des combinés sur 30 matchs comptés 43)


def _mkey(r):
    """(jour, match, famille) d'un pronostic : ses lignes d'une même famille sont liées (Plus de 8,5 et de 9,5 corners du même match). Sans
    identifiant de match : la ligne compte seule."""
    mid = r.get("id") or str(r.get("key") or "").split("|")[0]
    return str(r.get("date") or "")[:10], str(mid) if mid else f"#{id(r)}", family(r["market"])


def _migrate(data, rows):
    """Cumuls de la version 1 (comptés par ligne) -> version 2 (par match) : chaque famille est ramenée au rapport matchs / lignes mesuré sur les
    pronostics déjà comptés encore gardés (fair.json, 10 jours). Réussite et probas annoncées gardent leurs proportions : seule la quantité de preuve
    baisse (la correction se rapproche un peu de son point de départ). Une fois (data["_v"])."""
    if data.get("_v", 1) >= VERSION:
        return
    from collections import Counter
    per = Counter(_mkey(r) for r in rows if r.get("calib") and r.get("market"))
    lines, matches = Counter(), Counter()
    for (_, _, fam), k in per.items():
        lines[fam] += k
        matches[fam] += 1
    for fam, nl in lines.items():
        f = matches[fam] / nl
        if f >= 0.999:
            continue
        for doc in (data, data.get("_new") or {}):
            accs = [doc.get(fam)] + [lg.get(fam) for lg in (doc.get("_lg") or {}).values() if isinstance(lg, dict)]
            accs += [v for k, v in (doc.get("_cell") or {}).items() if k.split("|")[0] == fam]
            for acc in accs:
                if isinstance(acc, list) and len(acc) == 3:
                    acc[:] = [round(x * f, 4) for x in acc]
    _rebuild_cells(data, rows, per)
    data["_v"] = VERSION


def _rebuild_ls(data, rows):
    """Cumuls par championnat ET sens (data["_ls"]) refaits à partir des pronostics déjà comptés encore gardés (fair.json, 10 jours), au poids 1/k par
    match comme add() : le jugement par championnat dans chaque sens part avec l'historique connu."""
    from collections import Counter
    done = [r for r in rows if r.get("calib") and r.get("market") and r.get("sport_key") and r.get("result") in ("win", "loss") and side(r)]
    per = Counter(_mkey(r) for r in done)
    ls = data.setdefault("_ls", {})
    for r in done:
        p0, w, won = r.get("p0") or r["prob"], 1.0 / per[_mkey(r)], r["result"] == "win"
        acc = ls.setdefault(r["sport_key"], {}).setdefault(f"{family(r['market'])}|{side(r)}", [0, 0, 0.0])
        acc[0], acc[1], acc[2] = round(acc[0] + w, 4), round(acc[1] + w * won, 4), round(acc[2] + w * p0, 4)


def _rebuild_cells(data, rows, per):
    """Familles comptées AVANT que Kelly sépare les sens (« plus » / « moins ») : leurs cases de sens et de tranche sont refaites à partir des pronostics
    déjà comptés encore gardés (fair.json), au poids 1/k par match comme add(). 13/10 : les tirs « plus de » (58 % pour 65 % annoncés) étaient jugés
    avec les « moins de » (32 %), donc écartés ensemble. Les cases déjà là ne sont jamais touchées."""
    cells = data.setdefault("_cell", {})
    have = {k.split("|")[0] for k in cells}
    for r in rows:
        if not r.get("calib") or not r.get("market") or r.get("result") not in ("win", "loss"):
            continue
        fam, sd = family(r["market"]), side(r)
        if fam in have or not sd or not _acc_list(data.get(fam)):
            continue
        p0, w, won = r.get("p0") or r["prob"], 1.0 / per[_mkey(r)], r["result"] == "win"
        for acc in (cells.setdefault(f"{fam}|{sd}", [0, 0, 0.0]), cells.setdefault(f"{fam}|{sd}|{band(p0)}", [0, 0, 0.0])):
            acc[0], acc[1], acc[2] = round(acc[0] + w, 4), round(acc[1] + w * won, 4), round(acc[2] + w * p0, 4)


def add(rows, today=None, defer=None):
    """Ajoute au cumul les pronostics réglés (gagnés ou perdus) pas encore comptés (marqués « calib ») : famille, championnat, sens et tranche
    (data["_cell"]), après avoir vieilli les cumuls (C.CALIB_HALF_LIFE). Chaque match compte UNE fois par famille : ses k lignes réglées ensemble pèsent
    1/k chacune (comme ensemble.learn et new_roi). `defer` (dict) : le cumul y est rangé au lieu d'être écrit (l'appelant l'écrit AVEC les lignes
    marquées, store.save_many). -> nombre ajouté."""
    from collections import Counter
    data, n = load(), 0
    new = [r for r in rows if not r.get("calib") and r.get("result") in ("win", "loss")]
    if not new or FILE in store._unreadable:        # cumul illisible : rien n'est compté ni marqué (sinon perdu : save() refuserait d'écrire)
        return 0
    _migrate(data, rows)
    if "_ls" not in data:                           # cumuls par championnat et sens : nouveaux le 13/10, refaits avec les pronostics gardés
        _rebuild_ls(data, rows)
    data["_t"] = aged(data, data.get("_t"), today)
    per = Counter(_mkey(r) for r in new)
    for r in new:
        w = 1.0 / per[_mkey(r)]
        accumulate(data, r, r["result"] == "win", w)
        if r.get("rule") == C.RULE and family(r["market"]) in C.STATS_NEW_FAMILIES:     # compteur « depuis la correction » : la proba ANNONCÉE
            accumulate(data.setdefault("_new", {}), r | {"p0": r["prob"]}, r["result"] == "win", w)      # (mélange avec 1xBet), pas celle du modèle seul
            if r.get("xodds") and not r.get("ghost"):                                  # gain réel à la cote 1xBet, par match (new_roi) : jamais les lignes en fantôme (jamais misées) ; leur n et leur taux réalisé comptent
                data.setdefault("_new_rows", []).append([str(r.get("date") or "")[:10], str(r.get("id") or r["key"].split("|")[0]), family(r["market"]),
                                                         round(float(r["xodds"]), 3), 1 if r["result"] == "win" else 0])
        r["calib"] = True
        n += 1
    if len(data.get("_new_rows") or []) > C.NEW_RULE_ROWS:
        data["_new_rows"] = data["_new_rows"][-C.NEW_RULE_ROWS:]
    if defer is not None:
        defer[FILE] = data
    else:
        _save(data)
    return n


def accumulate(data, r, won, w=1.0):
    """Compte un pronostic réglé `r` (market, p0 ou prob, sport_key, name, home, away) dans `data` : famille, championnat, sens et tranche. `w` : son
    poids (1/k pour k lignes liées du même match, voir add)."""
    fam, p0 = family(r["market"]), r.get("p0") or r["prob"]
    accs = [data.setdefault(fam, [0, 0, 0.0])]
    sd = side(r)
    if r.get("sport_key"):
        accs.append(data.setdefault("_lg", {}).setdefault(r["sport_key"], {}).setdefault(fam, [0, 0, 0.0]))
        if sd:                                          # championnat ET sens : « tirs plus de » au Brésil n'est pas « tirs moins de » en Grèce
            accs.append(data.setdefault("_ls", {}).setdefault(r["sport_key"], {}).setdefault(f"{fam}|{sd}", [0, 0, 0.0]))
    if sd:
        cells = data.setdefault("_cell", {})
        accs += [cells.setdefault(f"{fam}|{sd}", [0, 0, 0.0]), cells.setdefault(f"{fam}|{sd}|{band(p0)}", [0, 0, 0.0])]
    for acc in accs:
        acc[0] = round(acc[0] + w, 4)
        acc[1] = round(acc[1] + w * won, 4)
        acc[2] = round(acc[2] + w * p0, 4)


def add_combo_legs(legs, today=None):
    """Cumule (data["_combo"]) les jambes de combinés réglées (gagnées ou perdues) pas encore comptées (marque « calib_combo ») : réussite face à la
    proba annoncée (déjà corrigée). -> nombre ajouté."""
    new = [leg for leg in legs if not leg.get("calib_combo") and leg.get("result") in ("win", "loss") and leg.get("prob")]
    if not new:
        return 0
    data = load()
    if FILE in store._unreadable:
        return 0
    data["_t"] = aged(data, data.get("_t"), today)
    acc = data.setdefault("_combo", [0, 0, 0.0])
    for leg in new:
        acc[0] = round(acc[0] + 1, 4)
        acc[1] = round(acc[1] + (leg["result"] == "win"), 4)
        acc[2] = round(acc[2] + leg["prob"], 4)
        leg["calib_combo"] = True
    _save(data)
    return len(new)


def reset():
    """Oublie le cumul gardé en mémoire (relu au prochain appel) : après une écriture faite hors de _save (store.save_many du règlement)."""
    _memo.clear()


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


def factor(market, data=None, sport_key=None, side=None, p=None, base=None):
    """k du marché (1 = pas de correction), en cascade : famille ; sens du pari `side` (rapproché de la famille) ; tranche de la proba brute `p`
    (rapprochée du sens, interpolée) ; puis l'écart du championnat `sport_key` (k du championnat / k de la famille, s'il a assez de pronostics de
    cette famille). Sans sens, sans proba ou sans résultats dans ces cases : exactement la correction famille / championnat. Point de départ `base`
    (k vers lequel la famille est rapprochée) : par défaut la correction que donne le rejeu de la saison passée (même cascade sur boot_acc), 1 sans
    rejeu."""
    data = data if data is not None else load()
    fam = family(market)
    if base is None and (fam, side) == TRIAL and trial_state(data)[0]:         # essai : la chance corrigée par les seuls tirs « plus de »
        tk = trial_k(data)
        if tk is not None:
            return tk
    if base is None:
        acc = boot_acc()
        base = factor(market, acc, sport_key, side, p, base=1.0) if acc else 1.0
    k_fam = _k(data.get(fam), base, fam=fam)
    k_fam = base if k_fam is None else k_fam
    k = k_side = k_fam
    cells = data.get("_cell") or {}
    if side and cells:
        ks = _k(cells.get(f"{fam}|{side}"), k_fam, fam=fam)
        k = k_side = k_fam if ks is None else ks
        if p is not None and p >= 0.5:
            k = _fine(fam, side, p, k, cells)
    ls = _k(((data.get("_ls") or {}).get(sport_key) or {}).get(f"{fam}|{side}"), k_side, fam=fam) if sport_key and side else None
    lg = _k(((data.get("_lg") or {}).get(sport_key) or {}).get(fam), k_fam, fam=fam) if sport_key and ls is None else None
    if ls is not None:                                  # ce championnat a assez de résultats DANS CE SENS : son écart à lui, pour ce sens
        k = k * ls / k_side
    elif lg is not None:
        k = k * lg / k_fam
    return max(K_RANGE[0], min(K_RANGE[1], k))


TRIAL = ("shots", "plus")       # essai du 13/10 : tirs vers le but « plus de » (C.SHOTS_TRIAL)
TRIAL_MIN_N = 10                # résultats de l'essai pour que SA correction remplace celle de la famille


def trial_state(data=None):
    """(essai actif ?, cumul [n, gagnés, somme des probas] des vrais résultats des tirs « plus de »). Arrêté de lui-même si, après
    C.SHOTS_TRIAL_STOP_N matchs, ils réussissent moins de C.SHOTS_TRIAL_STOP_RATE."""
    data = data if data is not None else load()
    cell = (data.get("_cell") or {}).get("|".join(TRIAL))
    cell = cell if _acc_list(cell) else None
    if not C.SHOTS_TRIAL:
        return False, cell
    if cell and cell[0] >= C.SHOTS_TRIAL_STOP_N and cell[1] / cell[0] < C.SHOTS_TRIAL_STOP_RATE:
        return False, cell
    return True, cell


def trial_k(data=None):
    """Correction des tirs « plus de » d'après LEURS seuls résultats (sans le « moins de » qui tirait la famille vers le bas) ; None sous TRIAL_MIN_N."""
    _, cell = trial_state(data)
    if not cell or cell[0] < TRIAL_MIN_N or cell[2] / cell[0] - 0.5 < 0.02:
        return None
    return max(K_RANGE[0], min(K_RANGE[1], (cell[1] / cell[0] - 0.5) / (cell[2] / cell[0] - 0.5)))


TRIAL_LG_N, TRIAL_LG_BAD, TRIAL_LG_GOOD = 8, 0.50, 0.62     # championnat : 8 matchs de tirs « plus de » à moins de 50 % : écarté ; à 62 % et plus : repris


def trial_league(sport_key, data=None):
    """Le championnat `sport_key` est-il dans l'essai ? D'abord SES résultats de tirs « plus de » (TRIAL_LG_N matchs au moins : écarté sous TRIAL_LG_BAD,
    repris à TRIAL_LG_GOOD et plus, même s'il était écarté d'avance) ; sinon la liste de départ (C.SHOTS_TRIAL_EXCLUDE : les plus faibles au rejeu)."""
    data = data if data is not None else load()
    acc = ((data.get("_ls") or {}).get(sport_key) or {}).get("|".join(TRIAL))
    if _acc_list(acc) and acc[0] >= TRIAL_LG_N:
        rate = acc[1] / acc[0]
        if rate < TRIAL_LG_BAD:
            return False
        if rate >= TRIAL_LG_GOOD:
            return True
    return sport_key not in C.SHOTS_TRIAL_EXCLUDE


def trial_ok(c, data=None):
    """Le pronostic `c` est-il repris par l'essai (tirs vers le but « plus de », championnat dans l'essai : trial_league, essai actif) ?"""
    if family(c.get("market") or "") != TRIAL[0] or side(c) != TRIAL[1] or not trial_league(c.get("sport_key"), data):
        return False
    return trial_state(data)[0]


def trial_line(data=None):
    """« 🧪 Essai tirs vers le but « plus de » : 26 matchs · 60 % / 65 % · … » ; '' sans essai."""
    on, cell = trial_state(data)
    if not C.SHOTS_TRIAL:
        return ""
    got = f"{cell[0]:.0f} matchs · {cell[1] / cell[0]:.0%} / {cell[2] / cell[0]:.0%}" if cell and cell[0] else "pas encore de résultat"
    if not on:
        return f"🧪 Essai tirs vers le but « plus de » ARRÊTÉ : {got} (sous {C.SHOTS_TRIAL_STOP_RATE:.0%})".replace("%", " %")
    data = data if data is not None else load()
    from . import catalogue
    decided = []
    for sk, cells in sorted((data.get("_ls") or {}).items()):
        acc = cells.get("|".join(TRIAL))
        if _acc_list(acc) and acc[0] >= TRIAL_LG_N:
            decided.append(f"{catalogue.name(sk)} {acc[1] / acc[0]:.0%} {'✅' if trial_league(sk, data) else '⛔'}")
    return (f"🧪 Essai tirs vers le but « plus de » : {got} · au plus {C.SHOTS_TRIAL_MAX_LEGS} par combiné · arrêt automatique sous "
            f"{C.SHOTS_TRIAL_STOP_RATE:.0%} après {C.SHOTS_TRIAL_STOP_N} matchs" + (" · par championnat : " + ", ".join(decided) if decided else "")
            ).replace("%", " %")


P_MIN, P_MAX = 0.01, 0.99      # bornes d'une proba corrigée (A4, audit du 17/10) : k jusqu'à 1,2 sortait de [0, 1] (prob 1,04 ; cote minimale négative)


def bound(p):
    """`p` ramenée dans [P_MIN, P_MAX] : la cote minimale 1 / p x (1 + marge) qui en découle est toujours > 1."""
    return max(P_MIN, min(P_MAX, p))


def calibrate(market, p, data=None, sport_key=None, side=None):
    """Proba corrigée d'un pronostic de proba brute `p` (championnat `sport_key` et sens `side` facultatifs, voir factor), bornée dans [P_MIN, P_MAX]."""
    return bound(0.5 + (p - 0.5) * factor(market, data, sport_key, side, p))


def rep_market(fam):
    """Un marché de la famille `fam` (pour factor)."""
    return next((m for m, f in FAMILIES.items() if f == fam), f"team_{fam}_total")


def family_k(fam, data=None, real_only=False):
    """Correction de la famille, sans sens ni championnat : celle qu'affichent les bilans (rejeu compris). `real_only` : celle qui écarte un
    marché des combinés (viable_factor : vrais résultats, ceux de la nouvelle règle dès qu'il y en a assez)."""
    return viable_factor(rep_market(fam), data=data) if real_only else factor(rep_market(fam), data)


def new_rule(data=None):
    """Cumuls « depuis la correction » (data["_new"], même forme que data) : pronostics de C.STATS_NEW_FAMILIES choisis avec la nouvelle règle."""
    return (data if data is not None else load()).get("_new") or {}


def new_count(fam, data=None):
    acc = new_rule(data).get(fam)
    return acc[0] if _acc_list(acc) else 0


def new_min(fam):
    """Résultats de la nouvelle règle requis pour juger la famille : C.STATS_NEW_MIN (30), au moins le seuil de la famille (60 pour corners, cartons)."""
    return max(C.STATS_NEW_MIN, min_need(fam))


def judged_on_new(fam, data=None):
    """L'exclusion des combinés de la famille se décide-t-elle sur les résultats de la nouvelle règle (au moins new_min) ?"""
    return fam in C.STATS_NEW_FAMILIES and new_count(fam, data) >= new_min(fam)


def new_roi(fam, data=None):
    """Gain réel à la cote 1xBet des pronostics de la nouvelle règle de la famille `fam` (data["_new_rows"]) : {"n" : résultats, "m" : matchs, "roi" : gain
    moyen par mise, "hw" : marge d'erreur (C.NEW_RULE_Z erreurs-types)} ; None sans résultat. L'erreur-type est comptée MATCH PAR MATCH (estimateur de ratio
    à grappes) : deux lignes d'un même match passent ou cassent souvent ensemble, elles n'apportent pas deux fois l'information."""
    rows = [r for r in ((data if data is not None else load()).get("_new_rows") or []) if isinstance(r, list) and len(r) == 5 and r[2] == fam]
    if not rows:
        return None
    by = {}
    for day, mid, _, odds, won in rows:
        g = by.setdefault((day, mid), [0.0, 0])
        g[0] += won * odds - 1
        g[1] += 1
    tot_r, tot_n, m = sum(g[0] for g in by.values()), sum(g[1] for g in by.values()), len(by)
    roi = tot_r / tot_n
    var = m / (m - 1) * sum((g[0] - roi * g[1]) ** 2 for g in by.values()) / tot_n ** 2 if m > 1 else float("inf")
    return {"n": tot_n, "m": m, "roi": roi, "hw": C.NEW_RULE_Z * math.sqrt(var) if math.isfinite(var) else float("inf")}


def old_bad(fam, data=None):
    """La famille était-elle écartée des combinés par les résultats de l'ancienne règle (tous les vrais résultats, sans rejeu) ?"""
    return factor(rep_market(fam), data if data is not None else load(), base=1.0) < C.VIABLE_MIN_K


def new_rule_ok(fam, data=None):
    """Verdict du GAIN RÉEL de la nouvelle règle (new_roi), une fois la famille jugée sur elle (judged_on_new) : une famille écartée par l'ancienne règle
    (old_bad) ne revient que si le gain, au pire de sa marge d'erreur, reste au-dessus de C.NEW_RULE_ROI_FLOOR ; une famille admise n'est écartée que si
    le gain, au mieux de sa marge, reste en dessous (la preuve est demandée au changement, dans les deux sens). Pas encore jugée : True (l'ancienne règle
    décide, viable_factor)."""
    data = data if data is not None else load()
    if not judged_on_new(fam, data):
        return True
    r = new_roi(fam, data)
    if old_bad(fam, data):
        return r is not None and r["roi"] - r["hw"] >= C.NEW_RULE_ROI_FLOOR
    return r is None or r["roi"] + r["hw"] >= C.NEW_RULE_ROI_FLOOR


COMBO_FILE = "combo_fams.json"      # {famille: dans les combinés ?} au dernier matin (combo_changes)


def combo_changes(data=None):
    """Familles sorties des combinés ou revenues depuis le dernier matin -> lignes (« 📏 Tirs : retirés des combinés, réussite 45 % pour 65 % annoncés… ») ;
    mémorise l'état du jour. Seulement les familles assez suivies pour le bilan (SHOW_MIN) ; le tout premier matin : rien (référence)."""
    data = data if data is not None else load()
    if FILE in store._unreadable:                   # cumul illisible : la référence n'est pas touchée
        return []
    now = {f: in_combos(f, data) for f, a in data.items() if not f.startswith("_") and _acc_list(a) and a[0] >= SHOW_MIN}
    old = store.load(COMBO_FILE, None)
    store.save(COMBO_FILE, (old if isinstance(old, dict) else {}) | now)     # une famille passée sous SHOW_MIN garde son dernier état connu
    if not isinstance(old, dict):
        return []
    out = []
    for f in sorted(now):
        if now[f] == old.get(f, True):
            continue
        n, w, s = data[f]
        rate = f"réussite {w / n:.0%} pour {s / n:.0%} annoncés".replace("%", " %")
        label = LABELS.get(f, f)
        out.append(f"📏 {label[0].upper() + label[1:]} : " + (f"de retour dans les combinés ({rate})" if now[f] else
                                                              f"retirés des combinés ({rate}) ; ils reviendront quand ils redeviendront fiables"))
    return out


def in_combos(fam, data=None):
    """La famille a-t-elle sa place dans les combinés (combos.data_ok, affichages « écartés des combinés ») : fiabilité suffisante (family_k real_only) et
    verdict du gain réel de la nouvelle règle (new_rule_ok)."""
    data = data if data is not None else load()
    return family_k(fam, data, real_only=True) >= C.VIABLE_MIN_K and new_rule_ok(fam, data)


def viable_factor(market, sport_key=None, side=None, p=None, data=None, shown=None):
    """k qui décide si un marché reste dans les combinés (combos.data_ok) : sur les seuls vrais résultats (jamais le rejeu), et pour
    C.STATS_NEW_FAMILIES sur ceux de la nouvelle règle dès qu'il y en a new_min (les résultats de l'ancienne règle n'écartent plus). Ceux-là
    mesurent la proba annoncée (`shown`, le mélange avec 1xBet), les autres la proba brute `p`."""
    data = data if data is not None else load()
    if judged_on_new(family(market), data):
        return factor(market, new_rule(data), sport_key, side, shown if shown is not None else p, base=1.0)
    return factor(market, data, sport_key, side, p, base=1.0)


def new_rule_note(fam, data=None):
    """« · nouvelle règle 12/30 » ; « · nouvelle règle : 34 · 63 % / 65 % · gain +6 % (± 14 %, 21 matchs) → de retour dans les combinés » ;
    « … → gain pas encore prouvé » ; '' hors C.STATS_NEW_FAMILIES ou sans résultat."""
    data = data if data is not None else load()
    if fam not in C.STATS_NEW_FAMILIES:
        return ""
    acc = new_rule(data).get(fam)
    n = acc[0] if _acc_list(acc) else 0
    bad_before = old_bad(fam, data)
    if n < new_min(fam):
        return f" · nouvelle règle {count(n)}/{new_min(fam)}" if (n >= 0.5 or bad_before) else ""
    txt = f" · nouvelle règle : {count(n)} · {round(acc[1] / n * 100)} % / {round(acc[2] / n * 100)} %"
    r = new_roi(fam, data)
    if r is not None:
        hw = f"± {round(r['hw'] * 100)} %" if math.isfinite(r["hw"]) else "marge inconnue"
        txt += f" · gain théorique aux cotes 1xBet {'+' if r['roi'] >= 0 else '−'}{round(abs(r['roi']) * 100)} % ({hw}, {r['m']} match{'s' if r['m'] > 1 else ''})"
    k_ok = viable_factor(rep_market(fam), data=data) >= C.VIABLE_MIN_K
    if bad_before and k_ok:
        txt += " → de retour dans les combinés" if new_rule_ok(fam, data) else " → gain pas encore prouvé"
    elif not bad_before and k_ok and not new_rule_ok(fam, data):
        txt += " → perte prouvée"
    return txt


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
        k = family_k(fam, data)
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
