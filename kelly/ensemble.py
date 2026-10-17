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
wm = 0,25 (non mesuré ; 0,5 avant l'audit du 13/10) ; ws = 0,75 quand sharp ET 1xBet existent.

Règle de décision (value) : le pari est « de la valeur » ssi cote 1xBet x p_ens >= 1 + ENSEMBLE_MARGIN (3 %) ET pas de veto ET, dans les familles où le marché pèse
lourd (buts, 1N2...), un avis SHARP existe (Pinnacle, Betfair Exchange, Matchbook : config.SHARP_BOOKS). Sans avis sharp, 1xBet seul, devigé, rend 0,93-0,94 (marge de 6-7 %) :
le seuil n'est jamais atteint sans que le modèle l'y pousse, et le modèle n'a pas à décider seul. Le pari est alors « probable » (kind « probable », jamais « valeur ») : proba
retenue >= ENSEMBLE_PROBABLE_MIN_P et 1xBet ne paie pas plus de ENSEMBLE_PROBABLE_MIN_EV sous elle ; ev_x (proba 1xBet sans marge x cote - 1) dit s'il reste une espérance au prix
du marché (combos.stake_prob : aucune mise conseillée tant qu'elle n'est pas positive). Veto : dans les
familles où le marché a été MESURÉ meilleur que le modèle (VETO_FAMILIES : buts, 1N2, double chance, les 2 marquent) et y pèse >= ENSEMBLE_VETO_WEIGHT
(0,7), un écart |p_model - p_mkt| > ENSEMBLE_MAX_DISAGREE (0,12) écarte le pari (dans ces familles, c'est presque toujours Kelly qui se trompe). Corners,
cartons, tirs, fautes : pas de veto (le modèle n'y pèse que 25 %, et sans aucun écart permis il ne pourrait jamais montrer s'il sait quelque chose). Sans aucun avis de marché (ligne dont l'autre issue manque chez 1xBet, pas de sharp), rien
n'est validé (« no_market ») : une valeur jugée sur le seul modèle est celle que l'audit a démentie (ENSEMBLE_REQUIRE_MARKET = faux pour l'autoriser
hors des familles à marché lourd).

Apprentissage des poids (learn) : par famille, DEUX poids appris, wm (modèle face au marché) et ws (sharp face à 1xBet). Pour chacun, une
GRILLE de 40 valeurs de [0, 1] et, pour chaque valeur, la log-vraisemblance cumulée des résultats réglés (proba retenue avec ce poids ;
l'opposé du log-loss). Le poids utilisé est la MOYENNE a posteriori : a priori Beta de moyenne le poids a priori et d'écart-type
ENSEMBLE_PRIOR_SD (0,07 là où l'audit a mesuré, 0,20 ailleurs : un rappel fort vers le poids a priori), fois la vraisemblance. Un écart de
quelques points entre le modèle et le marché apporte très peu d'information par résultat : la moyenne a posteriori ne bouge donc que
lentement (plusieurs centaines de résultats par famille pour déplacer un poids de 0,1), ce qui est voulu. Garde-fous : (1) l'effectif compte des
MATCHS : les k lignes d'une même famille dans un même match comptent chacune 1/k (effectif et vraisemblance), car elles sont corrélées ; (2) rien ne
bouge avant ENSEMBLE_MIN_N matchs (ENSEMBLE_MIN_N_OTHER = 300 dans les familles sans a priori mesuré : corners, cartons, tirs, fautes...) comportant les
avis comparés (modèle et un avis de marché pour wm ; sharp et 1xBet pour ws), puis le passage est progressif sur autant de plus ; (3) la vraisemblance
est atténuée (ENSEMBLE_TEMP = 0,5) car le choix des pronostics n'est pas aléatoire ; (4) oubli exponentiel (demi-vie ENSEMBLE_HALF_LIFE résultats) pour
suivre un marché qui change. L'état tient dans data/ensemble.json ({"v": 2, "fam": {famille: {"m": {"ll": [40 nombres], "n": effectif}, "s": {...}}},
"done": {clé du pronostic: jour}}) : l'apprentissage est idempotent par clé de pronostic, que le résultat vienne de data/ensemble_log.json ou de
data/fair.json. Version 1 (effectif en lignes, a priori 0,5 des familles non mesurées) : le poids appris du modèle (« m ») de ces familles est oublié à
la lecture (_migrate), les autres restent.

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

from . import calib, config as C, families, odds, store
from .sources import xbet

FILE = "ensemble.json"
LOG_FILE = "ensemble_log.json"
ROW = ("key", "id", "sport_key", "home", "away", "commence", "market", "name", "point", "prob", "p0", "xodds", "mu")     # = fair.ROW (testé)
EPS = 1e-4                                  # bornes des probas dans les logits
GRID = tuple((k + 0.5) / 40 for k in range(40))      # poids candidats : 1,25 % ... 98,75 % (jamais exactement 0 ni 1)
OVERROUND = (0.98, 1.30)                    # somme des 1/cote d'un marché : en dehors, cotes douteuses (cellule manquante, marché suspendu...)
DC_OVERROUND = (1.96, 2.60)
DONE_DAYS = 30                              # jours de mémoire des pronostics déjà appris (plus que ENSEMBLE_LOG_DAYS)
_OTHER = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}
# Familles où un avis sharp peut exister (buts, 1N2, double chance, les 2 marquent) : seules elles ont le chemin « probable » sans sharp. Corners, cartons,
# tirs, fautes n'ont jamais d'avis sharp : même quand le marché y pèse lourd (poids appris), une ligne doit valoir C.ENSEMBLE_MARGIN au prix 1xBet.
PROBABLE_FAMILIES = frozenset(("goals", "btts", "dc", "h2h", "team_goals"))
VETO_FAMILIES = PROBABLE_FAMILIES           # familles où le marché a été mesuré meilleur que le modèle : seul endroit où un gros désaccord écarte le pari
VERSION = 2                                 # ensemble.json : effectif compté en matchs (voir le haut du module)
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


def frozen(fam):
    """Le poids du modèle de la famille `fam` est-il figé à 0 (config.ENSEMBLE_FROZEN_MODEL, audit du 14/10) ? Alors la proba retenue est celle du marché seul."""
    return bool(C.ENSEMBLE_ENABLED) and fam in C.ENSEMBLE_FROZEN_MODEL


# Les familles de chaque voie ne sont plus listées ici : families.lane(famille) (états de data/families.json, valeurs initiales = les anciennes listes :
# sharpgap = buts, 1N2, double chance, les 2 marquent, buts d'équipe ; stats = corners, cartons, jaunes, tirs, tirs cadrés, fautes).
STRATEGIES = ("sharpgap", "stats", "fill", "live", "saison")                # « live » : jambes des combinés LIVE, hors voies (ni retard 1xBet ni modèle de stats) ; dimensionnement d'avant


def strategy_of(c):
    """Voie d'un pronostic ou d'un pari réel PRIX 1xBet posé (c["xodds"]), à poser UNE fois quand il est construit (xprice.price / _pick_price) :
    « sharpgap » = une des cinq familles figées avec de la valeur (cote 1xBet x proba sharp - 1 >= +3 %, ensemble.value kind « value »),
    « stats » = corners, cartons, jaunes, tirs, tirs cadrés, fautes avec de la valeur (le modèle mélangé au marché), « fill » = tout le reste, c'est-à-dire
    une ligne qui ne peut servir que SANS valeur (favori « probable » 🎯, stat sous le seuil, touches, hors-jeu, joueurs...). None sans cote 1xBet (rien à jouer).
    L'essai des tirs n'est pas une voie : c'est « stats » avec la famille shots / sot et le drapeau d'essai de calib."""
    if not c.get("xodds"):
        return None
    fam = family(c["market"])
    lane = families.lane(fam)
    if lane is None:
        return "fill"
    kind = c.get("xkind")
    if kind is None and is_active(c["market"]):
        try:
            kind = value(c).get("kind")             # pari réel (The Odds API) : pas d'xkind posé par xprice, on le rejoue avec ses avis
        except Exception:                           # noqa: BLE001 - avis incomplets : pas de valeur
            kind = None
    if kind != "value":
        return "fill"
    return lane


def strategy_label(row):
    """Voie lue d'une ligne ou d'une jambe : son étiquette, ou « legacy » pour une ligne d'avant les étiquettes (jamais ré-étiquetée après coup)."""
    return (row or {}).get("strategy") or "legacy"


def has_value(legs):
    """Un combiné n'a pas de voie propre : il porte celles de ses jambes. True = au moins une jambe à valeur (sharpgap / stats), False = toutes les jambes sont
    « fill » (combiné SANS valeur), None = voies inconnues (jambes d'avant les étiquettes)."""
    legs = list(legs or [])
    labels = [(l or {}).get("strategy") for l in legs]
    if any(x in ("sharpgap", "stats") for x in labels) or any(x == "saison" and (l or {}).get("sval") for x, l in zip(labels, legs)):
        return True                                 # une jambe `saison` est « à valeur » (sval) ou sans valeur (jambe de remplissage de la voie)
    if labels and all(x == "fill" or (x == "saison" and not (l or {}).get("sval")) for x, l in zip(labels, legs)):
        return False
    return None


def unsent(legs):
    """Un combiné construit automatiquement reste-t-il fantôme (toutes ses jambes `fill`, voies connues) ? Faux si NOVALUE_SEND ou ensemble éteint."""
    if legs and C.SEASON_SEND_FILL and all((l or {}).get("lane") == "saison" for l in legs):
        return False                                # voie `saison` : un combiné de remplissage est envoyé (« sans valeur »), jamais fantôme (SEASON_SEND_FILL)
    return bool(C.ENSEMBLE_ENABLED and not C.NOVALUE_SEND and legs and has_value(legs) is False)


def model_silent(market):
    """Le modèle n'a-t-il AUCUNE voix sur ce marché (famille figée) : ni poids, ni veto, ni filtre « ligne plus faible qu'un match moyen » (xprice, combos.viable) ?"""
    return frozen(family(market))


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
    max_age = C.SHARPGAP_MAX_AGE_MIN if frozen(family(c["market"])) else C.ENSEMBLE_SHARP_MAX_AGE_MIN       # famille figée : même fraîcheur que sharpgap
    if now is not None and not t and frozen(family(c["market"])):
        return None                                  # famille figée : comme sharpgap, un relevé sharp d'âge inconnu est refusé
    if now is not None and t:
        try:
            if (now - datetime.fromisoformat(str(t).replace("Z", "+00:00"))).total_seconds() / 60 > max_age:
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
            if a[2] is not None and not c.get("cup"):          # un pronostic de stats de coupe n'est jamais « l'avis du modèle » d'un pari réel
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
        _migrate(d)
        _memo.clear()
        _memo.update(data=d, ver=ver)
    return _memo["data"]


def _migrate(d):
    """État de la version 1 (effectif en lignes, a priori 0,5 des familles non mesurées) -> version 2 : le poids appris du modèle (« m ») des familles sans
    a priori mesuré (corners, cartons, tirs, fautes...) est oublié (appris en lignes sur quelques jours, sous l'ancien a priori) ; buts, 1N2, double chance,
    les 2 marquent et la part du sharp (« s ») restent. En mémoire seulement : la prochaine sauvegarde (learn) l'écrit avec « v »: 2."""
    if d.get("v", 1) >= VERSION:
        return
    for fam, st in d["fam"].items():
        if fam not in C.ENSEMBLE_PRIOR_MODEL and isinstance(st, dict):
            st.pop("m", None)
    d["v"] = VERSION


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


def min_n(fam, kind="m"):
    """Matchs requis avant que le poids appris bouge : ENSEMBLE_MIN_N, ENSEMBLE_MIN_N_OTHER pour le poids du modèle d'une famille sans a priori mesuré."""
    return C.ENSEMBLE_MIN_N if kind == "s" or fam in C.ENSEMBLE_PRIOR_MODEL else C.ENSEMBLE_MIN_N_OTHER


def _weight(acc, prior, sd, need=None):
    """Poids utilisé : l'a priori tant que l'effectif (en matchs) est sous `need` (ENSEMBLE_MIN_N par défaut), puis le posterior, introduit
    progressivement sur `need` de plus."""
    need = C.ENSEMBLE_MIN_N if need is None else need
    if not acc or acc.get("n", 0) < need:
        return prior
    r = min(1.0, (acc["n"] - need) / max(need, 1e-9))
    return prior + r * (_post_mean(acc, prior, sd) - prior)


def weights(fam, data=None, raw=False):
    """Poids de la famille : {"wm" (modèle face au marché), "ws" (sharp face à 1xBet), "prior_wm", "prior_ws", "n_m", "n_s" (effectifs appris), "frozen"}.
    Famille figée (voir frozen) : wm = 0 quoi que l'apprentissage ait mesuré ; "wm_raw" garde le poids qu'il aurait eu (le journal fantôme s'en sert). `raw` : le poids
    d'avant, sans gel (le fantôme rejoue l'ancienne règle)."""
    cache = data is None and not raw
    d = load() if data is None else data           # load() vide le cache des poids si le document a changé
    if cache and fam in _memo.get("w", {}):
        return _memo["w"][fam]
    st = (d.get("fam") or {}).get(fam) or {}
    out = {"wm": _weight(st.get("m"), prior_model(fam), prior_sd(fam, "m"), min_n(fam)),
           "ws": _weight(st.get("s"), C.ENSEMBLE_PRIOR_SHARP, prior_sd(fam, "s"), min_n(fam, "s")),
           "prior_wm": prior_model(fam), "prior_ws": C.ENSEMBLE_PRIOR_SHARP,
           "n_m": round((st.get("m") or {}).get("n", 0.0), 1), "n_s": round((st.get("s") or {}).get("n", 0.0), 1), "frozen": False}
    if not raw and frozen(fam):
        out |= {"wm_raw": out["wm"], "wm": 0.0, "ws_raw": out["ws"], "ws": 1.0, "frozen": True}       # ws = 1 : le marché est le sharp seul, comme sharpgap (une seule définition de la valeur)
    if cache:
        _memo.setdefault("w", {})[fam] = out
    return out


def _loglik(p, y):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p if y else 1 - p)


def _add(acc, lls, w=1.0):
    """Un résultat de plus dans le cumul, de poids `w` (1 / nombre de lignes de sa famille dans le même match : l'effectif compte des matchs) : oubli
    exponentiel (demi-vie ENSEMBLE_HALF_LIFE résultats), vraisemblance atténuée de ENSEMBLE_TEMP."""
    lam = 0.5 ** (w / max(C.ENSEMBLE_HALF_LIFE, 1.0))
    acc["ll"] = [lam * a + C.ENSEMBLE_TEMP * w * x for a, x in zip(acc["ll"], lls)]
    acc["n"] = lam * acc.get("n", 0.0) + w


def learn(rows):
    """Apprend des lignes RÉGLÉES (result « win » / « loss ») portant leurs avis : « pm » (modèle), « px » (1xBet), « ps » (sharp). Le poids du modèle
    apprend des lignes ayant le modèle et au moins un avis de marché, le poids du sharp de celles ayant sharp ET 1xBet (un pari réel sans
    avis du modèle y sert) ; une ligne sans l'un ni l'autre n'apprend rien. Idempotent par clé (data/ensemble.json garde les clés apprises DONE_DAYS jours) et
    marque les lignes apprises (« ens »). Enregistre si quelque chose a changé. -> nombre de lignes apprises."""
    data = load()
    n = 0

    def learnable(r):
        key = r.get("key")
        if r.get("result") not in ("win", "loss") or not key or key in data["done"] or r.get("ens"):
            return False
        pm, px, ps = r.get("pm"), r.get("px"), r.get("ps")
        return (pm is not None and (px is not None or ps is not None)) or (px is not None and ps is not None)
    todo = sorted((r for r in rows if learnable(r)), key=lambda r: (str(r.get("date") or ""), str(r.get("key") or "")))
    per_match = {}                                                 # lignes d'une même famille dans un même match : corrélées, elles comptent pour un match
    for r in todo:
        k = (family(r["market"]), str(r.get("date") or ""), r.get("id") or r["key"].split("|")[0])
        per_match[k] = per_match.get(k, 0) + 1
    for r in todo:
        key = r["key"]
        pm, px, ps = r.get("pm"), r.get("px"), r.get("ps")
        y = 1 if r["result"] == "win" else 0
        fam = family(r["market"])
        wt = 1.0 / per_match[(fam, str(r.get("date") or ""), r.get("id") or key.split("|")[0])]
        st = data["fam"].setdefault(fam, {})
        if pm is not None and not frozen(fam):                     # famille figée : le poids du modèle n'est plus appris
            w = weights(fam, data)                                 # ws courant : l'avis du marché dont on mesure le poids du modèle
            p_mkt = pool_market(ps, px, w["ws"])[0]
            _add(st.setdefault("m", {"ll": [0.0] * len(GRID), "n": 0.0}), [_loglik(pool(pm, p_mkt, g), y) for g in GRID], wt)
        if ps is not None and px is not None and not frozen(fam):  # famille figée : ws = 1 (sharp seul), plus rien à apprendre
            _add(st.setdefault("s", {"ll": [0.0] * len(GRID), "n": 0.0}), [_loglik(pool_market(ps, px, g)[0], y) for g in GRID], wt)
        data["done"][key] = str(r.get("date") or date.today())
        r["ens"] = True
        n += 1
    if n:
        save(data)
    return n


# ---------- proba retenue et décision ----------

def combine(fam, p_model=None, p_x=None, p_sharp=None, data=None, raw=False):
    """Proba retenue et détail : (p_ens ou None, parts). parts : famille, les trois avis, p_mkt, poids appris (wm, ws), poids EFFECTIFS
    {"model", "sharp", "x"} (somme 1), sources utilisées (« sharp+x+model »...). Famille figée (voir frozen) : la proba retenue est celle du MARCHÉ seul, et SANS avis de
    marché il n'y a pas de proba (None) : jamais le modèle seul ; parts["p_model"] reste l'avis du modèle, pour l'affichage et le journal. `raw` : sans gel."""
    w = weights(fam, data, raw)
    silent = w["frozen"]
    p_mkt, msrc = pool_market(p_sharp, p_x, w["ws"])
    p = pool(None if silent else p_model, p_mkt, w["wm"])
    wm = 0.0 if silent else (w["wm"] if (p_model is not None and p_mkt is not None) else (1.0 if p_model is not None else 0.0))
    ws = w["ws"] if (p_sharp is not None and p_x is not None) else (1.0 if p_sharp is not None else 0.0)
    eff = {"model": wm, "sharp": (1 - wm) * ws, "x": (1 - wm) * (1 - ws) if p_mkt is not None else 0.0}
    src = "+".join(n for n, v in (("sharp", p_sharp), ("x", p_x), ("model", None if silent else p_model)) if v is not None and not (silent and n == "x" and ws == 1.0))
    return p, {"family": fam, "p_model": p_model, "p_x": p_x, "p_sharp": p_sharp, "p_mkt": p_mkt, "wm": w["wm"], "ws": w["ws"],
               "w": eff, "src": src, "n": w["n_m"], "market_weight": 1 - w["wm"], "frozen": silent}


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


def value_of(market, p_model, p_x, p_sharp, price, margin=None, data=None, x_approx_used=False, raw=False):
    """Décision sur une ligne : {"ok", "why", "p", "ev", "ev_x", "gap", "kind", "parts"}. ok : cote x p_ens >= 1 + marge et pas de veto (« valeur »), ou « probable » sans avis sharp (voir plus haut). why si pas ok :
    « no_prob » (aucun avis), « no_market » (aucun avis de marché pour confirmer le modèle), « veto » (écart modèle-marché
    trop grand), « price » (1xBet paie trop peu), « no_sharp » / « not_probable » (sans avis sharp : « probable » éteint, ou proba retenue trop basse). ev = p_ens x cote - 1.
    gap = |p_model - p_mkt| ou None. kind : « value » (avis sharp ou famille légère) | « probable » (sans avis sharp) | None. ev_x = proba 1xBet sans marge x cote - 1 ou None."""
    p, parts = combine(family(market), p_model, p_x, p_sharp, data, raw)
    parts["x_approx"] = x_approx_used
    silent = parts["frozen"]
    if margin is None:
        margin = C.SHARPGAP_EDGE if silent else C.ENSEMBLE_MARGIN             # famille figée : le même seuil que sharpgap (+3 %), un seul producteur de paris sur les buts
    out = {"ok": False, "why": "", "p": p, "ev": None, "gap": None, "parts": parts, "kind": None, "ev_x": None}
    if p is None:
        out["why"] = "no_prob"
        return out
    out["ev"] = p * price - 1
    out["ev_x"] = None if p_x is None else p_x * price - 1
    heavy = parts["market_weight"] >= C.ENSEMBLE_VETO_WEIGHT and family(market) in VETO_FAMILIES     # marché mesuré meilleur et pesant lourd
    if p_model is not None and parts["p_mkt"] is not None:
        out["gap"] = abs(p_model - parts["p_mkt"])
    if parts["p_mkt"] is None and (heavy or C.ENSEMBLE_REQUIRE_MARKET):
        out["why"] = "no_market"
    elif heavy and not silent and out["gap"] is not None and out["gap"] > C.ENSEMBLE_MAX_DISAGREE:       # famille figée : le modèle n'a pas de veto
        out["why"] = "veto"
    elif heavy and p_sharp is None and family(market) in PROBABLE_FAMILIES:     # pas d'avis sharp : jamais « valeur », au mieux « probable »
        if silent or not C.ENSEMBLE_PROBABLE:                                    # famille figée : sans sharp, rien (« probable » serait 1xBet contre 1xBet)
            out["why"] = "no_sharp"
        elif p < C.ENSEMBLE_PROBABLE_MIN_P:
            out["why"] = "not_probable"
        elif out["ev"] < C.ENSEMBLE_PROBABLE_MIN_EV:
            out["why"] = "price"
        else:
            out.update(ok=True, kind="probable")
    elif silent and out["ev"] > C.SHARPGAP_MAX_EDGE:                           # comme sharpgap : un écart énorme est une donnée suspecte, pas une valeur
        out["why"] = "suspect"
    elif p * price < 1 + margin:
        if silent and C.ENSEMBLE_PROBABLE and p >= C.ENSEMBLE_PROBABLE_MIN_P and out["ev"] >= C.ENSEMBLE_PROBABLE_MIN_EV:
            out.update(ok=True, kind="probable")                              # favori des books de référence, SANS valeur : jambe de combiné possible, jamais comptée comme valeur
        else:
            out["why"] = "price"
    else:
        out.update(ok=True, kind="value")
    return out


def value(c, price=None, margin=None, approx=False, data=None):
    """Décision sur le pronostic ou pari `c` à la cote 1xBet `price` (par défaut c["xodds"]) ; voir value_of."""
    price = price if price is not None else c.get("xodds")
    pm, px, ps, used = opinions(c, price, approx)
    return value_of(c["market"], pm, px, ps, price, margin, data, used)


def min_price(p, margin=None, market=None):
    """Cote 1xBet minimale pour que le pari passe la règle de valeur à la proba retenue `p` : (1 + marge) / p. `market` : la marge d'une famille figée est celle de
    sharpgap (C.SHARPGAP_EDGE), comme dans value_of."""
    if margin is None:
        margin = C.SHARPGAP_EDGE if market is not None and model_silent(market) else C.ENSEMBLE_MARGIN
    return (1 + margin) / p


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


def _arrow(w, prior):
    """« ↑ » / « ↓ » quand le poids appris s'écarte de plus de 2 points du poids prévu ; '' sinon."""
    return " ↑" if w > prior + 0.02 else " ↓" if w < prior - 0.02 else ""


def lines(data=None, short=False):
    """Une ligne par famille apprise, en mots simples : « buts : Kelly compte pour 12 % (prévu 12 %), Pinnacle 75 % du marché (prévu 75 %) — 130 résultats »
    (« Kelly » : poids du modèle face au marché ; « Pinnacle » : part des books de référence dans l'avis du marché face à 1xBet). `short` (bilan détaillé) :
    « Corners · 53 % ↑ · 75 % · 165 », les familles les plus suivies d'abord (↑ / ↓ : plus ou moins que prévu au départ)."""
    d = load() if data is None else data
    out = []
    if short:
        ws = sorted(((fam, weights(fam, d)) for fam in d.get("fam") or {}), key=lambda x: -x[1]["n_m"])
        return [f"{calib.LABELS.get(fam, fam).capitalize()} · {w['wm'] * 100:.0f} %{' (figé)' if w['frozen'] else _arrow(w['wm'], w['prior_wm'])} · "
                f"{w['ws'] * 100:.0f} %{_arrow(w['ws'], w['prior_ws'])} · {w['n_m']:.0f}" for fam, w in ws]
    for fam in sorted(d.get("fam") or {}):
        w = weights(fam, d)
        if w["frozen"]:
            out.append(f"{calib.LABELS.get(fam, fam)} : Kelly compte pour 0 % (figé par l'audit du 14/10 : le modèle ne bat pas Pinnacle), Pinnacle "
                       f"{w['ws'] * 100:.0f} % du marché (prévu {w['prior_ws'] * 100:.0f} %) — {w['n_m']:.0f} résultats")
            continue
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


def record(rows, day, now=None, name=LOG_FILE, shadow=True):
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
            if old.get("result") is None and not old.get("xref") and c.get("xref"):
                old["xref"] = c["xref"]                    # audit : la référence 1xBet sert à lire son dernier prix (mark_x_close) ; le prix de la prise ne change pas
            if old.get("result") is None and old.get("sval") is None and c.get("sval") is not None:
                old["sval"] = c["sval"]                    # voie `saison` : une ligne d'abord écrite comme simple référence (ou refusée) devient la jambe jouée à un passage suivant
                old.pop("swhy", None)
                old.pop("refused", None)                   # ... et n'est plus une ligne écartée
            if old.get("result") is None:                  # la lecture de la décision reste ; la relecture tardive est gardée à part
                old["late"] = {"at": now.isoformat(timespec="minutes")} | {k: round(v, 4) for k, v in (("pm", pm), ("px", px), ("ps", ps)) if v is not None}
            continue
        row = {k: c.get(k) for k in ROW} | {"prob": None if pm is None else round(pm, 4), "pm": None if pm is None else round(pm, 4),
                                            "px": None if px is None else round(px, 4), "ps": None if ps is None else round(ps, 4),
                                            "date": str(day), "at": now.isoformat(timespec="minutes"), "tk": round(now.timestamp(), 3), "result": None}
        if c.get("xref"):
            row["xref"] = c["xref"]                        # audit : [GameId, Type, Param] 1xBet de la prise (hors ROW)
        if c.get("strategy"):
            row["strategy"] = c["strategy"]                # voie posée à la construction du pronostic (jamais recalculée ici)
        for k in ("sval", "ref"):
            if c.get(k) is not None:
                row[k] = c[k]                              # voie `saison` : jambe à valeur (1) ou de remplissage (0) ; ligne de référence indépendante du prix (1)
        for k in ("swhy", "refused", "cup"):
            if c.get(k):
                row[k] = c[k]                              # voie `saison` : raison du refus (veto / suspect) et ligne refusée, journalisées pour être comptées
        if c.get("lane") == "saison" and c.get("p_ens") is not None:
            row["pe"] = round(c["p_ens"], 4)               # voie `saison` : la proba RETENUE annoncée (réalisé / annoncé du bilan) ; « prob » reste celle du modèle
        if shadow and frozen(family(c["market"])):
            row["shadow"] = shadow_of(c, pm, px, ps)       # le modèle, en fantôme : ce qu'il aurait décidé (jamais joué, jamais appris)
        log.append(row)
        by_key[row["key"]] = row
        n += 1
    store.save(name, prune_log(log, date.fromisoformat(str(day))))
    return n


def shadow_of(c, pm, px, ps):
    """Le modèle d'une famille FIGÉE, en fantôme (étiquette « modèle seul ») : son espérance seule au prix 1xBet de la prise, « model_value » (espérance >= marge, sans
    veto ni mélange) et ce que l'ancienne règle (poids appris, veto, « probable ») aurait décidé. Sans effet sur un pari, le bilan, l'apprentissage ou le règlement :
    c'est la seule façon de rouvrir la question, plus tard, sur le vrai modèle de production, contre le dernier prix 1xBet connu. Ne lève jamais."""
    out = {"label": "modèle seul (fantôme, figé le 2026-10-14)"}
    try:
        price = c.get("xodds")
        if pm is not None and price:
            ev = pm * price - 1
            out |= {"ev": round(ev, 4), "model_value": ev >= C.ENSEMBLE_MARGIN}
        if price and (pm is not None or px is not None or ps is not None):
            v = value_of(c["market"], pm, px, ps, price, raw=True)
            out |= {"old_ok": bool(v["ok"]), "old_kind": v["kind"], "old_why": v["why"], "old_p": None if v["p"] is None else round(v["p"], 4)}
    except Exception as e:                          # noqa: BLE001 - le fantôme ne bloque jamais le journal
        out["error"] = type(e).__name__
    return out


def ts_of(v):
    """Instant (secondes UTC) d'un nombre ou d'un horodatage ISO ; None sinon (jamais d'exception)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError):
        return None


def take_ts(r):
    """Instant de la lecture qui a servi à PRENDRE le prix de la ligne : « tk » (ensemble.record, depuis le correctif A3 de l'audit du 17/10) ou « first_at » (voie cotes) ; None pour une
    ligne plus ancienne."""
    t = ts_of(r.get("tk"))
    return t if t is not None else ts_of(r.get("first_at"))


def close_read_ts(xc):
    """Instant de la lecture d'un `xclose` : « read_ts » (cache) ou « read » (voie cotes) ; None s'il n'en porte pas (xclose de track.py, lignes d'archive)."""
    if not isinstance(xc, dict):
        return None
    t = ts_of(xc.get("read_ts"))
    return t if t is not None else ts_of(xc.get("read"))


def not_a_close(r, xc=None):
    """Vrai si le `xclose` de la ligne n'est PAS une clôture : lu à l'instant de la prise ou avant (il compare le prix pris à la lecture même qui l'a donné : CLV 0 au lieu de « non mesuré »).
    Ligne qui porte l'instant de sa prise : comparaison exacte. Ligne plus ancienne (sans « tk » ni « first_at ») : un xclose de source « cache » au même prix que la prise est
    traité comme non mesuré (règle du superviseur du 17/10 ; perte assumée de vrais zéros, chiffrée dans audit_fixes_ledger.json)."""
    xc = r.get("xclose") if xc is None else xc
    if not isinstance(xc, dict) or xc.get("ok"):                # « ok » : déjà vérifiée avant l'archivage (voir _pack)
        return False
    take, seen = take_ts(r), close_read_ts(xc)
    if take is not None:
        return seen is not None and seen <= take
    return xc.get("src") == "cache" and xc.get("odds") is not None and r.get("xodds") is not None and xc.get("odds") == r.get("xodds")


X_CLOSE_WINDOW_MIN = 150          # on relit le cache 1xBet des lignes dont le match commence dans moins de 150 min (et jusqu'à X_CLOSE_AFTER_MIN min après le coup d'envoi)
X_CLOSE_AFTER_MIN = 240           # un relevé de clôture fait à KO - 35 min n'est vu par le passage horaire qu'après le coup d'envoi : la ligne reste lue tant qu'elle n'est pas réglée ; seul un relevé d'AVANT le coup d'envoi compte


def mark_x_close(now=None, name=LOG_FILE, window_min=X_CLOSE_WINDOW_MIN):
    """Audit du CLV des marchés de stats (sans prix sharp : la seule référence est 1xBet) : pour chaque ligne du journal non réglée, avec une référence
    1xBet (`xref`) et un match proche, note `xclose` = le DERNIER prix 1xBet lu dans le CACHE DISQUE (xbet.cached_cell, jamais de réseau) : {odds, read (ISO),
    read_ts, lead_min (minutes entre ce relevé et le coup d'envoi), susp}. Un relevé plus récent remplace l'ancien ; un pari retiré par 1xBet ne remplace PAS
    le dernier prix connu, il pose `xgone` (1xBet ferme les sous-matchs de stats ≈ 30 min avant le match, Kelly ne les relit plus à moins de 45 min : le
    « dernier prix » date donc de 45 à 105 min avant le coup d'envoi au mieux ; lead_min le dit). Ne lève jamais. -> nombre de lignes modifiées."""
    try:
        from .sources import xbet
        now = now or datetime.now(timezone.utc)
        log = store.load(name, [])
        if not isinstance(log, list):
            return 0
        n = 0
        from . import closeread
        idx = closeread.index()                            # relevés de clôture faits par kelly/closeread.py : marqués src = "close_read"
        for r in log:
            if r.get("result") is not None or not r.get("xref") or not r.get("commence"):
                continue
            try:
                ko = datetime.fromisoformat(str(r["commence"]).replace("Z", "+00:00"))
                if ko.tzinfo is None:
                    ko = ko.replace(tzinfo=timezone.utc)
                ahead = (ko - now).total_seconds() / 60
            except (ValueError, TypeError):
                continue
            if not -X_CLOSE_AFTER_MIN <= ahead <= window_min:
                continue
            cell = xbet.cached_cell(r["xref"])
            if cell is None or not isinstance(cell.at, (int, float)):
                continue
            lead = round((ko.timestamp() - cell.at) / 60)
            take = take_ts(r)
            if take is not None and cell.at <= take:
                continue                                   # relevé de l'instant de la prise ou d'avant : ce n'est pas une clôture (A3, audit du 17/10)
            if lead < 0:
                continue                                   # relevé d'après le coup d'envoi (suivi en direct dans le même cache) : pas une clôture
            if cell.cf is None:                            # le relevé en cache ne propose plus ce pari : on garde le dernier prix connu
                if not r.get("xgone"):
                    r["xgone"] = {"lead_min": lead}
                    n += 1
                continue
            old = r.get("xclose") or {}
            if old.get("read_ts") is not None and old["read_ts"] >= cell.at:
                continue                                   # rien de plus récent que ce qu'on a déjà
            r["xclose"] = {"odds": cell.cf, "read": datetime.fromtimestamp(cell.at, timezone.utc).isoformat(timespec="minutes"), "read_ts": cell.at,
                           "lead_min": lead, "susp": bool(cell.blocked), "src": closeread.src_of(r["xref"], cell.at, idx)}
            r.pop("xgone", None)                           # le pari est de nouveau listé (suspendu puis rouvert) : un prix plus récent existe
            n += 1
        if n:
            store.save(name, log)
        return n
    except Exception as e:                          # noqa: BLE001 - un champ d'audit ne bloque jamais le passage
        print(f"[ensemble] prix 1xBet de clôture ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


ARCH_PREFIX = "arch/ens/"          # un document par jour de la ligne : {"cols": ARCH_COLS, "rows": {clé: [valeurs dans l'ordre de cols]}} ; lu par les outils d'audit SEULEMENT
ARCH_COLS = ("market", "id", "name", "point", "commence", "pm", "px", "ps", "xodds", "xclose", "xgone", "result", "shadow", "strategy", "xref", "sport_key", "sval", "ref", "pe", "swhy", "refused", "cup", "mu", "val", "final")
# mu / val / final : nombre attendu, nombre réel et score final (collecte des coupes ; une colonne ajoutée en fin reste compatible avec les anciennes lignes)
# xclose : [cote, avance en minutes sur le coup d'envoi, prix suspendu 0/1, source « close_read » / « cache » ou None (anciennes lignes), vérifiée 0/1 (A3 : relevé postérieur à la prise, contrôlé avant l'archivage)] ; xgone : avance en minutes ou None ; shadow : [espérance du modèle seul, model_value 0/1, ancienne
# règle ok 0/1, ancienne règle kind, ancienne proba] ; strategy : étiquette de la voie (posée plus tard, None d'ici là) ; xref : référence 1xBet de la prise


def _pack(r):
    xc = r.get("xclose") or {}
    if not_a_close(r, xc):
        xc = {}                                         # lu à l'instant de la prise ou avant : pas une clôture, rien à archiver (A3, audit du 17/10)
    verified = take_ts(r) is not None and close_read_ts(xc) is not None       # tk / read_ts ne sont pas archivés : la vérification est faite ICI et gardée (5e élément de xclose)
    sh = r.get("shadow") or {}
    return [r.get("market"), r.get("id"), r.get("name"), r.get("point"), r.get("commence"), r.get("pm"), r.get("px"), r.get("ps"), r.get("xodds"),
            [xc.get("odds"), xc.get("lead_min"), int(bool(xc.get("susp"))), xc.get("src"), int(verified)] if xc.get("odds") else None, (r.get("xgone") or {}).get("lead_min") if r.get("xgone") else None,
            r.get("result"), [sh.get("ev"), int(bool(sh.get("model_value"))), int(bool(sh.get("old_ok"))), sh.get("old_kind"), sh.get("old_p")] if sh else None,
            r.get("strategy"), r.get("xref"), r.get("sport_key"), r.get("sval"), r.get("ref"), r.get("pe"), r.get("swhy"), r.get("refused"), r.get("cup"), r.get("mu"), r.get("val"), r.get("final")]


def unpack_archive_row(key, day, vals):
    """Ligne d'archive -> dict de même forme que celle du journal (pm, px, ps, xodds, xclose {odds, lead_min}, xgone {lead_min}, result, shadow, date) : les outils
    d'audit lisent l'une ou l'autre sans distinction."""
    d = dict(zip(ARCH_COLS, list(vals) + [None] * (len(ARCH_COLS) - len(vals))))      # une ligne archivée avant l'ajout d'une colonne reste lisible
    out = {"key": key, "date": day, "market": d["market"], "id": d["id"], "name": d["name"], "point": d["point"], "commence": d["commence"], "pm": d["pm"],
           "px": d["px"], "ps": d["ps"], "xodds": d["xodds"], "result": d["result"], "archived": True}
    if d["xclose"]:
        out["xclose"] = {"odds": d["xclose"][0], "lead_min": d["xclose"][1], "susp": bool(d["xclose"][2]) if len(d["xclose"]) > 2 else False}
        if len(d["xclose"]) > 3 and d["xclose"][3]:                     # src : « close_read » / « cache » (absent des lignes d'avant le 17/10)
            out["xclose"]["src"] = d["xclose"][3]
        if len(d["xclose"]) > 4 and d["xclose"][4]:                     # vérifiée avant l'archivage (instant de la prise connu et relevé postérieur)
            out["xclose"]["ok"] = True
    if d["xref"]:
        out["xref"] = d["xref"]
    if d["xgone"] is not None:
        out["xgone"] = {"lead_min": d["xgone"]}
    if d["shadow"]:
        sh = d["shadow"]
        out["shadow"] = {"ev": sh[0], "model_value": bool(sh[1]), "old_ok": bool(sh[2]), "old_kind": sh[3], "old_p": sh[4]}
    if d["strategy"]:
        out["strategy"] = d["strategy"]
    if d["sport_key"]:                                  # la ligue : la coupure automatique de la voie stats se juge par ligue
        out["sport_key"] = d["sport_key"]
    for k in ("sval", "ref", "pe", "swhy", "refused", "cup", "mu", "val", "final"):  # voie `saison` : jambe à valeur / de remplissage, ligne de référence, proba retenue
        if d.get(k) is not None:
            out[k] = d[k]
    return out


def archive(log, today=None, prefix=ARCH_PREFIX):
    """Copie, une seule fois chacune, les lignes RÉGLÉES (win / loss) du journal dans l'archive compacte (un document par jour de la ligne), marque `arch` sur les
    lignes copiées, et efface les jours de plus de C.ENSEMBLE_ARCHIVE_DAYS. Sans effet sur un pari, le bilan, l'apprentissage ou le règlement : rien d'autre que les
    outils d'audit ne la lit. Ne lève jamais (une panne d'écriture laisse les lignes non marquées : elles seront copiées au prochain règlement). -> nombre de lignes copiées."""
    if not C.ENSEMBLE_ARCHIVE:
        return 0
    try:
        todo = [r for r in log if r.get("result") in ("win", "loss") and r.get("key") and r.get("date") and not r.get("arch")]
        by_day = {}
        for r in todo:
            by_day.setdefault(r["date"], []).append(r)
        docs = {}
        for day, rows in by_day.items():
            doc = store.load(prefix + day, None)
            doc = doc if isinstance(doc, dict) and isinstance(doc.get("rows"), dict) else {"cols": list(ARCH_COLS), "rows": {}}
            for r in rows:
                doc["rows"][r["key"]] = _pack(r)
            docs[prefix + day] = doc
        if docs:
            store.save_many(docs)
            for r in todo:
                r["arch"] = True
        keep_from = str((today or date.today()) - timedelta(days=C.ENSEMBLE_ARCHIVE_DAYS))
        for nm in store.names(prefix):
            if nm[len(prefix):] < keep_from:
                store.remove(nm)
        return len(todo)
    except Exception as e:                          # noqa: BLE001 - l'archive ne bloque jamais le règlement
        print(f"[ensemble] archive ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def archive_rows(since=None, prefix=ARCH_PREFIX):
    """Lignes archivées (dicts de même forme que celles du journal, voir unpack_archive_row), des plus anciennes aux plus récentes ; `since` : jour ISO minimal."""
    out = []
    for nm in store.names(prefix):
        day = nm[len(prefix):]
        if since and day < since:
            continue
        doc = store.load(nm, None)
        if isinstance(doc, dict) and isinstance(doc.get("rows"), dict):
            out += [unpack_archive_row(k, day, v) for k, v in doc["rows"].items()]
    return out


def settle_log(settle_one, name=LOG_FILE, learner=None, arch_prefix=ARCH_PREFIX):
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
    learned = (learner or learn)(log)
    archive(log, prefix=arch_prefix)                                    # copie compacte des lignes réglées (90 jours) AVANT que prune_log ne les retire du journal
    dates = [r["date"] for r in log if r.get("date")]
    if dates:
        log = prune_log(log, date.fromisoformat(max(dates)))
    store.save(name, log)
    return settled, learned
