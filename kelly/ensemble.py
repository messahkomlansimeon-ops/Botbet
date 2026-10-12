"""Ensemble adaptatif : la proba retenue d'un pari est une COMBINAISON des avis disponibles, pas la seule proba du modèle de Kelly.

Pourquoi (audit) : sur les buts et le 1N2, le modèle de Kelly (force Dixon-Coles + profils) perd face au marché (log-loss modèle > marché,
mêler le modèle ne l'améliore jamais, poids optimal 0) et les paris où il s'écarte le plus du marché perdent 9 à 16 %. Sur corners, cartons, tirs,
fautes, touches et hors-jeu, aucune cote historique n'existe : rien n'est mesuré.

Trois avis possibles pour un pari (chacun peut manquer) :
  p_sharp  proba « vraie » des books de référence (The Odds API, Pinnacle pesé, marge de Shin retirée : c["prob_book"] d'un pari réel, voir
           odds.consensus) ; seulement pour les matchs et marchés couverts. Posée sur les pronostics par attach().
  p_x      proba de 1xBet SANS sa marge (devig) pour cette ligne : paire de cotes (plus/moins, oui/non), trio du 1N2 (Shin), double chance
           (somme des deux issues du 1N2 sans marge, sinon les 3 cotes ramenées à une masse de 2). x_fair().
  p_model  proba de Kelly (calibrée par calib.py : c["prob"] d'un pronostic avant l'ensemble, ensuite c["p_model"]).

Combinaison en log-cotes (« logit pooling », poids de somme 1), en deux étages :
  1. avis du marché  p_mkt = expit(ws x logit(p_sharp) + (1 - ws) x logit(p_x))      (un seul des deux : lui seul)
  2. proba retenue   p_ens = expit(wm x logit(p_model) + (1 - wm) x logit(p_mkt))    (pas de marché : le modèle seul ; pas de modèle : le marché)
Un avis manquant est donc retiré et les poids restants renormalisés PAR ÉTAGE : le poids du modèle (wm) ne grimpe pas quand le « sharp »
manque (sinon un match sans Pinnacle redonnerait au modèle 30 % au lieu de 12 %). Priors (config.ENSEMBLE_PRIOR_*) : buts, 1N2, les 2 marquent,
double chance wm = 0,12 (le marché pèse 88 %, sharp si présent sinon 1xBet) ; corners, cartons, tirs, tirs cadrés, fautes, touches, hors-jeu
wm = 0,5 (non mesuré) ; ws = 0,75 quand sharp ET 1xBet existent.

Règle de décision (value) : le pari est « de la valeur » ssi cote 1xBet x p_ens >= 1 + ENSEMBLE_MARGIN (3 %) ET pas de veto ET, dans les familles où le marché pèse
lourd (buts, 1N2...), un avis SHARP existe (Pinnacle, Betfair Exchange, Matchbook : config.SHARP_BOOKS). Sans avis sharp, 1xBet seul, devigé, rend 0,93-0,94 (marge de 6-7 %) :
le seuil n'est jamais atteint sans que le modèle l'y pousse, et le modèle n'a pas à décider seul. Le pari est alors « probable » (kind « probable », jamais « valeur ») : proba
retenue >= ENSEMBLE_PROBABLE_MIN_P et 1xBet ne paie pas plus de ENSEMBLE_PROBABLE_MIN_EV sous elle ; ev_x (proba 1xBet sans marge x cote - 1) dit s'il reste une espérance au prix
du marché (combos.stake_prob : aucune mise conseillée tant qu'elle n'est pas positive). Veto : dans les
familles où le marché pèse >= ENSEMBLE_VETO_WEIGHT (0,7), un écart |p_model - p_mkt| > ENSEMBLE_MAX_DISAGREE (0,12) écarte le pari (dans ces
familles, c'est presque toujours Kelly qui se trompe). Sans aucun avis de marché (ligne dont l'autre issue manque chez 1xBet, pas de sharp), rien
n'est validé (« no_market ») : une valeur jugée sur le seul modèle est celle que l'audit a démentie (ENSEMBLE_REQUIRE_MARKET = faux pour l'autoriser
hors des familles à marché lourd).

Apprentissage des poids (learn) : par famille, DEUX poids appris, wm (modèle face au marché) et ws (sharp face à 1xBet). Pour chacun, une
GRILLE de 40 valeurs de [0, 1] et, pour chaque valeur, la log-vraisemblance cumulée des résultats réglés (proba retenue avec ce poids ;
l'opposé du log-loss). Le poids utilisé est la MOYENNE a posteriori : a priori Beta de moyenne le poids a priori et d'écart-type
ENSEMBLE_PRIOR_SD (0,07 là où l'audit a mesuré, 0,20 ailleurs : un rappel fort vers le poids a priori), fois la vraisemblance. Un écart de
quelques points entre le modèle et le marché apporte très peu d'information par résultat : la moyenne a posteriori ne bouge donc que
lentement (plusieurs centaines de résultats par famille pour déplacer un poids de 0,1), ce qui est voulu. Garde-fous : (1) rien ne bouge avant
ENSEMBLE_MIN_N résultats comportant les avis comparés (modèle et un avis de marché pour wm ; sharp et 1xBet pour ws), puis le passage est
progressif sur ENSEMBLE_MIN_N de plus ; (2) la vraisemblance est
atténuée (ENSEMBLE_TEMP = 0,5) car les lignes d'un même match sont corrélées et le choix des pronostics n'est pas aléatoire ; (3) oubli
exponentiel (demi-vie ENSEMBLE_HALF_LIFE résultats) pour suivre un marché qui change. L'état tient dans data/ensemble.json
({"fam": {famille: {"m": {"ll": [40 nombres], "n": effectif}, "s": {...}}}, "done": {clé du pronostic: jour}}) : l'apprentissage est
idempotent par clé de pronostic, que le résultat vienne de data/ensemble_log.json ou de data/fair.json.

Mode « mixte » (phase 2, config.ENSEMBLE_ENABLED / ENSEMBLE_ACTIVE_FAMILIES / ENSEMBLE_LOG) : l'ensemble DÉCIDE seulement dans les familles
actives (is_active : buts, 1N2, double chance, les 2 marquent par défaut), là où le marché a prouvé qu'il vaut mieux que le modèle. Ailleurs la règle
d'avant reste, mais les avis (1xBet, sharp, modèle, proba retenue) y sont calculés (xprice._shadow), affichés (explain_c, sans « retenu ») et
journalisés comme partout, pour que les poids apprennent aussi dans les familles non actives avant d'y être jamais utilisés.

Journal des avis (record / settle_log) : data/ensemble_log.json garde, pour chaque pronostic ou pari (montré ou non), les avis de la LECTURE QUI A SERVI
LA DÉCISION (la première enregistrée : « pm » modèle, « px » 1xBet, « ps » sharp, tous trois lus ensemble, heure « at ») dans les MÊMES champs que
data/fair.json (ROW) pour être réglé par le même code (settle._settle_fair_row) ; une relecture plus tardive (cotes 1xBet du soir) ne remplace
rien : elle va dans « late » (information seulement, jamais apprise). Les poids n'apprennent donc pas la fraîcheur des avis (Pinnacle du matin
contre 1xBet du soir) mais leur qualité. Un avis sharp plus vieux que ENSEMBLE_SHARP_MAX_AGE_MIN minutes au moment de l'enregistrement exclut la
ligne de l'apprentissage. N'y entrent que les lignes ayant le modèle ET au moins un avis de marché, ou sharp ET 1xBet (les autres n'apprennent
rien). Les proba « px » approchées (double chance sans ses 3 cotes) n'y entrent jamais. Le journal reste petit : voir prune_log.
"""
import math
import sys
from datetime import date, datetime, timedelta, timezone

from . import calib, config as C, odds, store
from .sources import xbet

FILE = "ensemble.json"
LOG_FILE = "ensemble_log.json"
ROW = ("key", "id", "sport_key", "home", "away", "commence", "market", "name", "point", "prob", "p0", "xodds")     # = fair.ROW (testé)
EPS = 1e-4                                  # bornes des probas dans les logits
GRID = tuple((k + 0.5) / 40 for k in range(40))      # poids candidats : 1,25 % ... 98,75 % (jamais exactement 0 ni 1)
OVERROUND = (0.98, 1.30)                    # somme des 1/cote d'un marché : en dehors, cotes douteuses (cellule manquante, marché suspendu...)
DC_OVERROUND = (1.96, 2.60)
DONE_DAYS = 30                              # jours de mémoire des pronostics déjà appris (plus que ENSEMBLE_LOG_DAYS)
_OTHER = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}
_memo = {}


# ---------- maths ----------

def logit(p):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


def family(market):
    """Famille d'un marché (calib.family) : goals, btts, dc, h2h, corners, cards, shots, sot, fouls, throws, offsides..."""
    return calib.family(market)


def prior_model(fam):
    return C.ENSEMBLE_PRIOR_MODEL.get(fam, C.ENSEMBLE_PRIOR_OTHER)


def enabled():
    """L'ensemble est-il allumé (config.ENSEMBLE_ENABLED) ? Faux : comportement d'avant, rien de calculé, affiché ni journalisé."""
    return bool(C.ENSEMBLE_ENABLED)


def is_active(market):
    """L'ensemble DÉCIDE-t-il pour ce marché ? Allumé et famille (calib.family) dans config.ENSEMBLE_ACTIVE_FAMILIES : alors c["prob"] d'un pronostic
    est la proba retenue. Sinon (corners, cartons, tirs...) la règle d'avant s'applique et les avis ne servent qu'à l'affichage et au journal."""
    return bool(C.ENSEMBLE_ENABLED) and family(market) in C.ENSEMBLE_ACTIVE_FAMILIES


def logging_on():
    """Le journal des avis et l'apprentissage sont-ils allumés (ENSEMBLE_ENABLED et ENSEMBLE_LOG) ?"""
    return bool(C.ENSEMBLE_ENABLED and C.ENSEMBLE_LOG)


def model_prob(c):
    """Proba du MODÈLE de Kelly du pronostic `c` : c["p_model"] quand l'ensemble a décidé de sa proba (famille active : c["prob"] est alors la proba
    retenue p_ens, pas celle du modèle), sinon c["prob"]. Les comparaisons entre modèles (référence c["base"] d'un match moyen) lisent celle-ci."""
    if C.ENSEMBLE_ENABLED and c.get("p_ens") is not None and c.get("p_model") is not None and is_active(c["market"]):
        return c["p_model"]
    return c["prob"]


LEGEND = "Sur les buts et le résultat, Kelly suit surtout la cote 1xBet ; ailleurs, ses statistiques."      # fin de la légende 💎 (fmt.pronos)


def legend():
    """Phrase de la légende 💎 qui dit ce que Kelly suit, ou '' quand aucune famille n'est décidée par l'ensemble."""
    return LEGEND if C.ENSEMBLE_ENABLED and C.ENSEMBLE_ACTIVE_FAMILIES else ""


def pool_market(p_sharp, p_x, ws):
    """Avis du marché : sharp et 1xBet en log-cotes (poids ws pour sharp), l'un des deux seul s'il manque l'autre. -> (proba ou None, source)."""
    if p_sharp is not None and p_x is not None:
        return expit(ws * logit(p_sharp) + (1 - ws) * logit(p_x)), "sharp+x"
    if p_sharp is not None:
        return p_sharp, "sharp"
    if p_x is not None:
        return p_x, "x"
    return None, ""


def pool(p_model, p_mkt, wm):
    """Proba retenue : modèle (poids wm) et marché en log-cotes ; l'un seul s'il manque l'autre ; None sans aucun des deux."""
    if p_model is not None and p_mkt is not None:
        return expit(wm * logit(p_model) + (1 - wm) * logit(p_mkt))
    return p_mkt if p_model is None else p_model


# ---------- proba de 1xBet sans sa marge ----------

def devig(prices):
    """Probas sans marge d'un marché complet de cotes décimales `prices` (2 ou 3 issues) par la méthode de Shin (odds.shin_probs : retire plus de
    marge aux outsiders qu'aux favoris) ; None si une cote manque ou si la somme des 1/cote sort de OVERROUND (cellule manquante : le résultat
    serait faux)."""
    if not prices or any(not p or p <= 1 for p in prices):
        return None
    s = sum(1 / p for p in prices)
    return odds.shin_probs(prices) if OVERROUND[0] <= s <= OVERROUND[1] else None


def devig_mass(prices, mass=2.0):
    """Double chance : trois issues qui somment à 2 (chaque match gagne par deux d'entre elles). Normalisation proportionnelle de masse `mass`
    (comme odds.build_picks) ; None si la somme des 1/cote est incohérente ou si une proba dépasse 1."""
    if not prices or any(not p or p <= 1 for p in prices):
        return None
    s = sum(1 / p for p in prices)
    if not DC_OVERROUND[0] <= s <= DC_OVERROUND[1]:
        return None
    ps = [mass / p / s for p in prices]
    return ps if max(ps) < 1 else None


def _dc_parts(name, home, away):
    """Issues du 1N2 couvertes par une double chance (« A or Draw » dans un sens ou l'autre), ou None."""
    for a, b in ((home, "Draw"), (home, away), (away, "Draw")):
        if name in (f"{a} or {b}", f"{b} or {a}"):
            return a, b
    return None


def x_fair(px, market, name, point, home=None, away=None):
    """Proba de 1xBet SANS sa marge pour un pari Kelly (marché, côté, ligne) à partir de ses cotes `px` (xbet.prices) ; None si elle n'est pas
    calculable (issue complémentaire absente chez 1xBet). Plus/moins et oui/non : la paire (Shin à deux issues) ; 1N2 : les trois issues
    (Shin) ; double chance : somme des deux issues du 1N2 sans marge si les trois cotes du 1N2 sont là (`home` / `away` requis), sinon les
    trois cotes de double chance ramenées à une masse de 2."""
    pt = None if point is None else float(point)
    key = xbet.resolve(px, market, name, pt)
    if key is None:
        return None
    if market == "h2h":
        keys = [k for k in px if k[0] == "h2h" and k[2] is None]
        ps = devig([px[k] for k in keys]) if len(keys) == 3 else None
        return None if ps is None else ps[keys.index(key)]
    if market == "double_chance":
        h2h = {k[1]: v for k, v in px.items() if k[0] == "h2h" and k[2] is None}
        parts = _dc_parts(key[1], home, away) if home and away else None
        if parts and len(h2h) == 3 and all(x in h2h for x in (home, away, "Draw")):
            ps = devig([h2h[home], h2h["Draw"], h2h[away]])
            if ps is not None:
                by = {home: ps[0], "Draw": ps[1], away: ps[2]}
                return by[parts[0]] + by[parts[1]]
        keys = [k for k in px if k[0] == "double_chance" and k[2] is None]
        ps = devig_mass([px[k] for k in keys]) if len(keys) == 3 else None
        return None if ps is None else ps[keys.index(key)]
    other = _OTHER.get(key[1])
    if other is None:
        return None
    ps = devig([px[key], px.get((market, other, key[2]))])
    return None if ps is None else ps[0]


def x_approx(price, margin=None):
    """Proba approchée de 1xBet sans sa marge quand seule la cote du pari est connue (double chance sans ses deux autres cotes) : 1 / cote
    retirée d'une marge supposée (C.ENSEMBLE_X_MARGIN). Approximation : à marquer comme telle, jamais utilisée pour apprendre."""
    return None if not price or price <= 1 else min(0.99, 1 / price / (1 + (C.ENSEMBLE_X_MARGIN if margin is None else margin)))


# ---------- avis sharp et modèle des paris : attach ----------

def _pt(point):
    return None if point is None else float(point)


def _canon(market, name):
    """Nom d'une issue comparable entre sources (casse ; double chance : sans ordre)."""
    n = str(name).strip().casefold()
    return " or ".join(sorted(p.strip() for p in n.split(" or "))) if market == "double_chance" else n


def _kid(c, name=None, point="_"):
    """Identité d'un pari (match, marché, issue, ligne) comparable entre un pronostic et un pari réel."""
    return (c["id"], c["market"], _canon(c["market"], c["name"] if name is None else name), _pt(c["point"] if point == "_" else point))


def sharp_of(c):
    """(proba, source, nombre de books de référence) du pari réel `c` (The Odds API) si elle vient d'un book de référence, sinon None : seule une
    proba « pinnacle » ou « sharp » (odds.consensus) est un avis sharp ; un consensus de books ordinaires n'en est pas un."""
    if c.get("prob_book") is not None and c.get("prob_src") in ("pinnacle", "sharp"):
        return c["prob_book"], c["prob_src"], c.get("n_sharp") or 1
    return None


def sharp_for(c, name, point, now=None):
    """Proba sharp attachée (attach) à la ligne (`name`, `point`) du pronostic `c`, ou None. Avec `now` (instant), une proba dont le relevé a plus
    de C.ENSEMBLE_SHARP_MAX_AGE_MIN minutes est ignorée (cotes du matin périmées l'après-midi) ; relevé d'heure inconnue : acceptée."""
    hit = (c.get("psh") or {}).get(f"{_canon(c['market'], name)}|{_pt(point)}")
    if not hit:
        return None
    t = hit[3] if len(hit) > 3 else None
    if now is not None and t:
        try:
            if (now - datetime.fromisoformat(str(t).replace("Z", "+00:00"))).total_seconds() / 60 > C.ENSEMBLE_SHARP_MAX_AGE_MIN:
                return None
        except (ValueError, TypeError):
            pass
    return hit[0]


def attach(pronos, picks, history=None):
    """Rapproche pronostics (avis du modèle, toutes leurs lignes c["alts"]) et paris réels (avis sharp), en place : chaque pronostic dont une
    ligne est couverte par un book de référence reçoit c["psh"] = {"issue|ligne": [proba, source, n books, heure ISO du relevé ou None]}
    (heure : c["sharp_at"] du pari, sinon sa dernière date dans `history` = odds_history.json) ; chaque pari réel dont la ligne existe chez un
    pronostic reçoit c["p_model"] (proba du modèle, calibrée). -> (pronostics avec avis sharp, paris avec avis du modèle)."""
    sharp = {}
    for c in picks or []:
        s = sharp_of(c)
        if s:
            h = (history or {}).get(c.get("key"))
            sharp[_kid(c)] = s + (c.get("sharp_at") or (h.get("t") if isinstance(h, dict) else None),)
    model, n_s, n_m = {}, 0, 0
    for c in pronos:
        psh = {}
        for a in c.get("alts") or [[c["name"], c["point"], c.get("p0", c.get("p_model", c.get("prob"))), None]]:
            kid = _kid(c, a[0], a[1])
            if a[2] is not None:
                model[kid] = calib.calibrate(c["market"], a[2], sport_key=c.get("sport_key"), side=calib.side(c | {"name": a[0]}))
            if kid in sharp:
                psh[f"{kid[2]}|{kid[3]}"] = [round(sharp[kid][0], 4), sharp[kid][1], sharp[kid][2], sharp[kid][3]]
        if psh:
            c["psh"] = psh
            n_s += 1
        else:
            c.pop("psh", None)
    for c in picks or []:
        p = model.get(_kid(c))
        if p is not None:
            c["p_model"] = round(p, 4)
            n_m += 1
        else:
            c.pop("p_model", None)
    return n_s, n_m


# ---------- poids appris ----------

def load():
    """État appris (data/ensemble.json), gardé en mémoire et relu dès qu'un autre processus (le passage du matin, le règlement) l'a réécrit
    (store.version) : le bot Telegram, qui reste lancé des semaines, voit les nouveaux poids. reset() force la relecture."""
    ver = store.version(FILE)
    if "data" not in _memo or _memo.get("ver") != ver:
        d = store.load(FILE, {})
        d = d if isinstance(d, dict) else {}
        d.setdefault("fam", {})
        d.setdefault("done", {})
        _memo.clear()
        _memo.update(data=d, ver=ver)
    return _memo["data"]


def reset():
    """Oublie la copie en mémoire (relit le document au prochain appel) : tests, ou après une écriture par un autre processus."""
    _memo.clear()


def save(data=None):
    data = load() if data is None else data
    cut = str(date.today() - timedelta(days=DONE_DAYS))
    data["done"] = {k: v for k, v in data["done"].items() if str(v) >= cut}
    data["at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    store.save(FILE, data, compact=True)
    _memo.clear()
    _memo.update(data=data, ver=store.version(FILE))


def prior_sd(fam, kind):
    """Écart-type a priori du poids : "m" (modèle face au marché) selon la famille, "s" (sharp face à 1xBet)."""
    return C.ENSEMBLE_PRIOR_SD_SHARP if kind == "s" else C.ENSEMBLE_PRIOR_SD.get(fam, C.ENSEMBLE_PRIOR_SD_OTHER)


def _post_mean(acc, prior, sd):
    """Moyenne a posteriori du poids sur GRID : a priori Beta(a, b) de moyenne `prior` et d'écart-type `sd` (k = a + b = prior x (1 - prior) /
    sd^2 - 1, au moins 2 ; planchers a, b >= 1 : jamais plus pointu qu'un pic en 0 ou 1) fois exp(log-vraisemblance cumulée)."""
    k = max(2.0, prior * (1 - prior) / (sd * sd) - 1)
    a, b = max(1.0, k * prior), max(1.0, k * (1 - prior))
    lp = [(a - 1) * math.log(g) + (b - 1) * math.log(1 - g) + ll for g, ll in zip(GRID, acc["ll"])]
    top = max(lp)
    w = [math.exp(x - top) for x in lp]
    return sum(g * x for g, x in zip(GRID, w)) / sum(w)


def _weight(acc, prior, sd):
    """Poids utilisé : l'a priori tant que le compte est sous ENSEMBLE_MIN_N, puis le posterior, introduit progressivement sur MIN_N de plus."""
    if not acc or acc.get("n", 0) < C.ENSEMBLE_MIN_N:
        return prior
    r = min(1.0, (acc["n"] - C.ENSEMBLE_MIN_N) / max(C.ENSEMBLE_MIN_N, 1e-9))
    return prior + r * (_post_mean(acc, prior, sd) - prior)


def weights(fam, data=None):
    """Poids de la famille : {"wm" (modèle face au marché), "ws" (sharp face à 1xBet), "prior_wm", "prior_ws", "n_m", "n_s" (effectifs appris)}."""
    cache = data is None
    d = load() if cache else data                  # load() vide le cache des poids si le document a changé
    if cache and fam in _memo.get("w", {}):
        return _memo["w"][fam]
    st = (d.get("fam") or {}).get(fam) or {}
    out = {"wm": _weight(st.get("m"), prior_model(fam), prior_sd(fam, "m")), "ws": _weight(st.get("s"), C.ENSEMBLE_PRIOR_SHARP, prior_sd(fam, "s")),
           "prior_wm": prior_model(fam), "prior_ws": C.ENSEMBLE_PRIOR_SHARP,
           "n_m": round((st.get("m") or {}).get("n", 0.0), 1), "n_s": round((st.get("s") or {}).get("n", 0.0), 1)}
    if cache:
        _memo.setdefault("w", {})[fam] = out
    return out


def _loglik(p, y):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p if y else 1 - p)


def _add(acc, lls):
    """Un résultat de plus dans le cumul : oubli exponentiel (demi-vie ENSEMBLE_HALF_LIFE résultats), vraisemblance atténuée de ENSEMBLE_TEMP."""
    lam = 0.5 ** (1 / max(C.ENSEMBLE_HALF_LIFE, 1.0))
    acc["ll"] = [lam * a + C.ENSEMBLE_TEMP * x for a, x in zip(acc["ll"], lls)]
    acc["n"] = lam * acc.get("n", 0.0) + 1.0


def learn(rows):
    """Apprend des lignes RÉGLÉES (result « win » / « loss ») portant leurs avis : « pm » (modèle), « px » (1xBet), « ps » (sharp). Le poids du modèle
    apprend des lignes ayant le modèle et au moins un avis de marché, le poids du sharp de celles ayant sharp ET 1xBet (un pari réel sans
    avis du modèle y sert) ; une ligne sans l'un ni l'autre n'apprend rien. Idempotent par clé (data/ensemble.json garde les clés apprises DONE_DAYS jours) et
    marque les lignes apprises (« ens »). Enregistre si quelque chose a changé. -> nombre de lignes apprises."""
    data = load()
    n = 0
    for r in sorted(rows, key=lambda r: (str(r.get("date") or ""), str(r.get("key") or ""))):
        key = r.get("key")
        if r.get("result") not in ("win", "loss") or not key or key in data["done"] or r.get("ens"):
            continue
        pm, px, ps = r.get("pm"), r.get("px"), r.get("ps")
        if not ((pm is not None and (px is not None or ps is not None)) or (px is not None and ps is not None)):
            continue
        y = 1 if r["result"] == "win" else 0
        fam = family(r["market"])
        st = data["fam"].setdefault(fam, {})
        if pm is not None:
            w = weights(fam, data)                                 # ws courant : l'avis du marché dont on mesure le poids du modèle
            p_mkt = pool_market(ps, px, w["ws"])[0]
            _add(st.setdefault("m", {"ll": [0.0] * len(GRID), "n": 0.0}), [_loglik(pool(pm, p_mkt, g), y) for g in GRID])
        if ps is not None and px is not None:
            _add(st.setdefault("s", {"ll": [0.0] * len(GRID), "n": 0.0}), [_loglik(pool_market(ps, px, g)[0], y) for g in GRID])
        data["done"][key] = str(r.get("date") or date.today())
        r["ens"] = True
        n += 1
    if n:
        save(data)
    return n


# ---------- proba retenue et décision ----------

def combine(fam, p_model=None, p_x=None, p_sharp=None, data=None):
    """Proba retenue et détail : (p_ens ou None, parts). parts : famille, les trois avis, p_mkt, poids appris (wm, ws), poids EFFECTIFS
    {"model", "sharp", "x"} (somme 1), sources utilisées (« sharp+x+model »...)."""
    w = weights(fam, data)
    p_mkt, msrc = pool_market(p_sharp, p_x, w["ws"])
    p = pool(p_model, p_mkt, w["wm"])
    wm = w["wm"] if (p_model is not None and p_mkt is not None) else (1.0 if p_model is not None else 0.0)
    ws = w["ws"] if (p_sharp is not None and p_x is not None) else (1.0 if p_sharp is not None else 0.0)
    eff = {"model": wm, "sharp": (1 - wm) * ws, "x": (1 - wm) * (1 - ws) if p_mkt is not None else 0.0}
    src = "+".join(n for n, v in (("sharp", p_sharp), ("x", p_x), ("model", p_model)) if v is not None)
    return p, {"family": fam, "p_model": p_model, "p_x": p_x, "p_sharp": p_sharp, "p_mkt": p_mkt, "wm": w["wm"], "ws": w["ws"],
               "w": eff, "src": src, "n": w["n_m"], "market_weight": 1 - w["wm"]}


def opinions(c, price=None, approx=False):
    """(p_model, p_x, p_sharp, approx utilisée) lus dans le pronostic ou pari `c` : c["p_model"] sinon c["prob"] (sauf pour un pari réel, dont
    la proba est celle des books : il n'a pas d'avis du modèle tant qu'attach() n'a pas posé c["p_model"]), c["p_x"] sinon c["xp"], c["p_sharp"]
    sinon la proba de référence du pari réel. `approx` : pour une double chance sans proba 1xBet, une approximation depuis la cote (x_approx)."""
    pm = c["p_model"] if "p_model" in c else (None if "prob_book" in c else c.get("prob"))
    px = c["p_x"] if c.get("p_x") is not None else c.get("xp")
    ps = c["p_sharp"] if c.get("p_sharp") is not None else (sharp_of(c) or (None,))[0]
    used = False
    if approx and px is None and family(c["market"]) == "dc":
        px, used = x_approx(price if price is not None else c.get("xodds")), True
    return pm, px, ps, used


def prob(c, price=None, approx=False, data=None):
    """Proba retenue du pronostic ou pari `c` -> (p_ens, parts) ; voir combine et opinions."""
    pm, px, ps, used = opinions(c, price, approx)
    p, parts = combine(family(c["market"]), pm, px, ps, data)
    parts["x_approx"] = used
    return p, parts


def value_of(market, p_model, p_x, p_sharp, price, margin=None, data=None, x_approx_used=False):
    """Décision sur une ligne : {"ok", "why", "p", "ev", "ev_x", "gap", "kind", "parts"}. ok : cote x p_ens >= 1 + marge et pas de veto (« valeur »), ou « probable » sans avis sharp (voir plus haut). why si pas ok :
    « no_prob » (aucun avis), « no_market » (aucun avis de marché pour confirmer le modèle), « veto » (écart modèle-marché
    trop grand), « price » (1xBet paie trop peu), « no_sharp » / « not_probable » (sans avis sharp : « probable » éteint, ou proba retenue trop basse). ev = p_ens x cote - 1.
    gap = |p_model - p_mkt| ou None. kind : « value » (avis sharp ou famille légère) | « probable » (sans avis sharp) | None. ev_x = proba 1xBet sans marge x cote - 1 ou None."""
    margin = C.ENSEMBLE_MARGIN if margin is None else margin
    p, parts = combine(family(market), p_model, p_x, p_sharp, data)
    parts["x_approx"] = x_approx_used
    out = {"ok": False, "why": "", "p": p, "ev": None, "gap": None, "parts": parts, "kind": None, "ev_x": None}
    if p is None:
        out["why"] = "no_prob"
        return out
    out["ev"] = p * price - 1
    out["ev_x"] = None if p_x is None else p_x * price - 1
    heavy = parts["market_weight"] >= C.ENSEMBLE_VETO_WEIGHT
    if p_model is not None and parts["p_mkt"] is not None:
        out["gap"] = abs(p_model - parts["p_mkt"])
    if parts["p_mkt"] is None and (heavy or C.ENSEMBLE_REQUIRE_MARKET):
        out["why"] = "no_market"
    elif heavy and out["gap"] is not None and out["gap"] > C.ENSEMBLE_MAX_DISAGREE:
        out["why"] = "veto"
    elif heavy and p_sharp is None:                  # pas d'avis sharp : jamais « valeur », au mieux « probable »
        if not C.ENSEMBLE_PROBABLE:
            out["why"] = "no_sharp"
        elif p < C.ENSEMBLE_PROBABLE_MIN_P:
            out["why"] = "not_probable"
        elif out["ev"] < C.ENSEMBLE_PROBABLE_MIN_EV:
            out["why"] = "price"
        else:
            out.update(ok=True, kind="probable")
    elif p * price < 1 + margin:
        out["why"] = "price"
    else:
        out.update(ok=True, kind="value")
    return out


def value(c, price=None, margin=None, approx=False, data=None):
    """Décision sur le pronostic ou pari `c` à la cote 1xBet `price` (par défaut c["xodds"]) ; voir value_of."""
    price = price if price is not None else c.get("xodds")
    pm, px, ps, used = opinions(c, price, approx)
    return value_of(c["market"], pm, px, ps, price, margin, data, used)


def min_price(p, margin=None):
    """Cote 1xBet minimale pour que le pari passe la règle de valeur à la proba retenue `p` : (1 + marge) / p."""
    return (1 + (C.ENSEMBLE_MARGIN if margin is None else margin)) / p


# ---------- texte ----------

def pct(p):
    return "—" if p is None else f"{p * 100:.0f} %"


def explain(parts, p=None):
    """« 1xBet : 54 % · Sharp : 57 % · Kelly : 64 % · retenu : 56 % » (les avis disponibles, puis la proba retenue `p`, sinon recalculée depuis
    `parts` de combine / prob)."""
    bits = [f"{n} : {pct(v)}" for n, v in (("1xBet", parts.get("p_x")), ("Sharp", parts.get("p_sharp")), ("Kelly", parts.get("p_model"))) if v is not None]
    if p is None:
        p = pool(parts.get("p_model"), parts.get("p_mkt"), parts.get("wm", 0.0))
    return " · ".join(bits + ([f"retenu : {pct(p)}"] if p is not None else []))


def explain_c(c):
    """Ligne d'avis d'un pronostic ou d'un pari réel remis au prix 1xBet : « 1xBet : 49 % · Kelly : 71 % » (+ « Sharp : 52 % » si un book de référence
    couvre le pari, + « retenu : 61 % » dans une famille où l'ensemble décide, voir is_active). '' sans avis de 1xBet (c["p_x"]) ou ensemble éteint :
    pas de ligne là où il n'y a rien à comparer."""
    if not C.ENSEMBLE_ENABLED or c.get("p_x") is None:
        return ""
    p = c.get("p_ens") if is_active(c["market"]) else None
    bits = [f"{n} : {pct(v)}" for n, v in (("1xBet", c.get("p_x")), ("Sharp", c.get("p_sharp")), ("Kelly", c.get("p_model"))) if v is not None]
    return " · ".join(bits + ([f"retenu : {pct(p)}"] if p is not None else []))


def lines(data=None):
    """Une ligne par famille apprise, en mots simples : « buts : Kelly compte pour 12 % (prévu 12 %), Pinnacle 75 % du marché (prévu 75 %) — 130 résultats »
    (« Kelly » : poids du modèle face au marché ; « Pinnacle » : part des books de référence dans l'avis du marché face à 1xBet)."""
    d = load() if data is None else data
    out = []
    for fam in sorted(d.get("fam") or {}):
        w = weights(fam, d)
        out.append(f"{calib.LABELS.get(fam, fam)} : Kelly compte pour {w['wm'] * 100:.0f} % (prévu {w['prior_wm'] * 100:.0f} %), Pinnacle "
                   f"{w['ws'] * 100:.0f} % du marché (prévu {w['prior_ws'] * 100:.0f} %) — {w['n_m']:.0f} résultats")
    return out


# ---------- journal des avis (apprentissage différé) ----------

def _started(c, now):
    try:
        return datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) <= now
    except (KeyError, ValueError, AttributeError):
        return False


def _sharp_age_min(c, now):
    """Âge (minutes) du relevé sharp du pronostic ou pari `c` à l'instant `now`, ou None s'il est inconnu : heure de sa ligne dans c["psh"]
    (ensemble.attach) pour un pronostic, c["sharp_at"] pour un pari réel."""
    t = None
    hit = (c.get("psh") or {}).get(f"{_canon(c['market'], c['name'])}|{_pt(c.get('point'))}")
    if hit and len(hit) > 3:
        t = hit[3]
    t = t or c.get("sharp_at")
    try:
        return (now - datetime.fromisoformat(str(t).replace("Z", "+00:00"))).total_seconds() / 60 if t else None
    except (ValueError, TypeError):
        return None


def sharp_stale(c, now):
    """Le sharp de `c` est-il périmé (relevé de plus de C.ENSEMBLE_SHARP_MAX_AGE_MIN minutes) ? Âge inconnu : non (acceptée, comme sharp_for)."""
    age = _sharp_age_min(c, now)
    return age is not None and age > C.ENSEMBLE_SHARP_MAX_AGE_MIN


def prune_log(log, ref_day):
    """Journal borné : ni lignes de plus de C.ENSEMBLE_LOG_DAYS jours avant `ref_day` (date), ni lignes réglées ET apprises de plus de
    C.ENSEMBLE_LOG_LEARNED_DAYS jours (le cumul de data/ensemble.json les résume), ni plus de C.ENSEMBLE_LOG_MAX_ROWS lignes (partent d'abord les apprises,
    puis les réglées, puis les plus vieilles en attente). Ordre des lignes conservé."""
    cut = str(ref_day - timedelta(days=C.ENSEMBLE_LOG_DAYS))
    learned_cut = str(ref_day - timedelta(days=C.ENSEMBLE_LOG_LEARNED_DAYS))
    log = [r for r in log if r["date"] >= cut and not (r.get("ens") and r["date"] < learned_cut)]
    extra = len(log) - C.ENSEMBLE_LOG_MAX_ROWS
    if extra > 0:
        def rank(i):
            r = log[i]
            return (0 if r.get("ens") else 1 if r.get("result") is not None else 2, r["date"], i)
        drop = set(sorted(range(len(log)), key=rank)[:extra])
        log = [r for i, r in enumerate(log) if i not in drop]
    return log


def record(rows, day, now=None, name=LOG_FILE):
    """Garde dans data/ensemble_log.json les avis de chaque pronostic ou pari de `rows` (montré ou non) pour apprendre quand il sera réglé :
    ses champs ROW (réglable par settle._settle_fair_row comme data/fair.json) + « pm » (modèle), « px » (1xBet sans marge), « ps » (sharp),
    « date », « at » (heure de la lecture). Les trois avis sont ceux de la lecture qui a servi la DÉCISION : un pronostic déjà gardé n'est plus
    modifié par une relecture (la relecture tardive va dans « late » : heure et avis, jamais appris). Une ligne dont l'avis sharp est périmé
    (plus de C.ENSEMBLE_SHARP_MAX_AGE_MIN minutes au moment de l'enregistrement) n'entre pas : ses avis ne datent pas du même moment. Un match
    commencé n'est plus enregistré. N'entrent que les pronostics ayant le modèle ET un avis de marché exact (jamais une proba 1xBet approchée).
    Journal borné (prune_log). -> nombre de lignes ajoutées."""
    now = now or datetime.now(timezone.utc)
    log = store.load(name, [])
    log = log if isinstance(log, list) else []
    by_key = {r["key"]: r for r in log}
    n = 0
    for c in rows:
        pm, px, ps, _ = opinions(c)
        if not ((pm is not None and (px is not None or ps is not None)) or (px is not None and ps is not None)):
            continue
        if c.get("x_approx") or not c.get("key") or _started(c, now):
            continue
        if ps is not None and sharp_stale(c, now):
            continue
        old = by_key.get(c["key"])
        if old is not None:
            if old.get("result") is None:                  # la lecture de la décision reste ; la relecture tardive est gardée à part
                old["late"] = {"at": now.isoformat(timespec="minutes")} | {k: round(v, 4) for k, v in (("pm", pm), ("px", px), ("ps", ps)) if v is not None}
            continue
        row = {k: c.get(k) for k in ROW} | {"prob": None if pm is None else round(pm, 4), "pm": None if pm is None else round(pm, 4),
                                            "px": None if px is None else round(px, 4), "ps": None if ps is None else round(ps, 4),
                                            "date": str(day), "at": now.isoformat(timespec="minutes"), "result": None}
        log.append(row)
        by_key[row["key"]] = row
        n += 1
    store.save(name, prune_log(log, date.fromisoformat(str(day))))
    return n


def settle_log(settle_one, name=LOG_FILE):
    """Règle les lignes en attente du journal avec `settle_one(ligne) -> vrai si elle vient d'être réglée` (phase 2 : le règlement gratuit de
    settle.py, le même que data/fair.json), apprend des lignes réglées et enregistre. -> (lignes réglées cette fois, lignes apprises)."""
    log = store.load(name, [])
    log = log if isinstance(log, list) else []

    def safe(r):                                    # une ligne bancale (nom d'équipe introuvable, source en panne...) ne bloque pas les autres
        try:
            return settle_one(r)
        except Exception as e:                      # noqa: BLE001
            print(f"[ensemble] ligne {r.get('key')} non réglée : {type(e).__name__}: {e}", file=sys.stderr)
            return False
    settled = [r for r in log if r.get("result") is None and safe(r)]
    learned = learn(log)
    dates = [r["date"] for r in log if r.get("date")]
    if dates:
        log = prune_log(log, date.fromisoformat(max(dates)))
    store.save(name, log)
    return settled, learned
