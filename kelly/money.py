"""Budget du jour : un montant fixé une fois (« budget 5000 »), modifiable quand tu veux, réparti chaque matin entre le pari du jour,
les combinés et les combinés à vérifier chez 1xBet, selon la confiance de Kelly. Il remplace la bankroll et la saisie des paris :
Kelly suppose que tu joues ce qu'elle propose, à la mise affichée ; « ❌ Pas joué » retire un pari du bilan, « ✏️ Autre cote »
corrige la cote obtenue.

Répartition (allocate) : chaque pari a une part maximale du budget selon son palier (SHARE), multipliée par sa confiance =
espérance (proba x cote - 1) rapportée à CONF_FULL, bornée à [CONF_MIN, 1]. Journée riche (la somme dépasse le budget) : tout est
réduit dans la même proportion, le budget est entièrement misé. Journée pauvre : Kelly mise moins et garde le reste. Mises arrondies
(round_step) ; une mise plus petite que l'arrondi est abandonnée.

data/mybudget.json : {"daily": montant, "cur": "FCFA"}. Les paris misés sur le budget portent « cur » (devise) : seuls eux comptent
dans le bilan en argent (gains).
"""
import math
import re
from datetime import date, timedelta

from . import fmt, store

FILE = "mybudget.json"
CUR = "FCFA"
MAX_AMOUNT = 1e9                      # au-delà : montant absurde (refusé)
CUR_RE = re.compile(r"(?i)(?!(?:inf|infinity|nan)$)[^\W\d_]{1,8}|[€$£¥]")                     # une devise : « FCFA », « € »
SHARE = {"pari": 0.25, "🛡️": 0.30, "⚖️": 0.18, "🎯": 0.15, "🔥": 0.08, "💀": 0.04}   # part max du budget par palier
FAIR_SHARE = ((8, 0.12), (20, 0.08), (math.inf, 0.05))                            # combinés à vérifier, selon la cote mini
CONF_FULL, CONF_MIN = 0.10, 0.3       # espérance de +10 % ou plus : part entière ; jamais moins de 30 % de la part


def load():
    d = store.load(FILE, {})
    return d if isinstance(d, dict) else {}


def _amount(x):
    """Montant fini, entre 0 et MAX_AMOUNT ; 0 pour toute valeur illisible (jamais d'exception)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    return v if not isinstance(x, bool) and math.isfinite(v) and 0 < v <= MAX_AMOUNT else 0


def active():
    return bool(daily())


def daily():
    return _amount(load().get("daily"))


def safe_cur(c):
    """Devise lisible (lettres ou symbole), « FCFA » sinon : une valeur étrangère ne casse ni l'affichage ni le HTML."""
    c = str(c or "").strip()
    return c if CUR_RE.fullmatch(c) else CUR


def cur():
    return safe_cur(load().get("cur"))


def set_daily(amount, currency=None):
    store.save(FILE, {"daily": round(float(amount), 2), "cur": safe_cur(currency) if currency else cur()})


def round_step(budget):
    """Arrondi des mises : 50 pour un budget de 5 000, 500 pour 50 000, 0,5 pour 10."""
    budget = _amount(budget)
    return 10 ** max(0, len(str(int(budget))) - 2) / 2 if budget >= 1 else 0.01


def money(x, currency=None):
    """« 1 500 FCFA »."""
    txt = f"{x:,.0f}".replace(",", " ") if float(x).is_integer() or abs(x) >= 100 else fmt.trim(x, 2)
    return f"{txt} {safe_cur(currency) if currency else cur()}"


def signed(x, currency=None):
    return ("+" if x >= 0 else "−") + money(abs(x), currency)


def _share(kind, tier):
    if kind == "fair":
        try:
            return next((s for limit, s in FAIR_SHARE if tier < limit), FAIR_SHARE[-1][1])
        except TypeError:
            return FAIR_SHARE[-1][1]
    return SHARE.get("pari" if kind == "pari" else next((k for k in SHARE if str(tier).startswith(k)), "🎯"), 0.15)


def _confidence(prob, odds):
    return max(CONF_MIN, min(1.0, (prob * odds - 1) / CONF_FULL))


def _raw(it):
    """Part du budget d'un pari (palier x confiance) ; 0 si ses données sont illisibles."""
    try:
        r = _share(it["kind"], it["tier"]) * _confidence(float(it["prob"]), float(it["odds"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return 0.0
    return r if math.isfinite(r) and r > 0 else 0.0


def allocate(items, budget=None, detail=False):
    """items = [{kind: pari|combo|fair, tier: titre du palier (cote mini visée pour fair), prob, odds}] -> (mises, misé, gardé), dans
    l'ordre des items ; une mise de 0 = pari laissé de côté (plus petit que l'arrondi). Ne lève jamais (valeur illisible : mise 0).
    `detail` : un 4e élément, « journée pauvre » = la somme des parts reste sous le budget (rien n'a été réduit), à ne pas confondre
    avec le reste d'un arrondi."""
    budget = _amount(daily() if budget is None else budget)
    items = list(items)
    if not budget or not items:
        out = ([0] * len(items), 0, budget)
        return out + (True,) if detail else out
    raw = [_raw(it) for it in items]
    total = sum(raw)
    scale = min(1.0, 1.0 / total) if total else 0
    step = round_step(budget)
    stakes = [math.floor(budget * r * scale / step) * step for r in raw]
    spent = round(sum(stakes), 2)
    out = (stakes, spent, round(budget - spent, 2))
    return out + (total < 1,) if detail else out


def budget_line(spent, kept, poor=None):
    """« 💰 Budget du jour : 5 000 FCFA · 3 800 misés, 1 200 gardés (journée pauvre) » (HTML). `poor` (allocate, detail) : Kelly mise moins
    que le budget parce que ses paris sont peu sûrs ; sinon (journée riche, tout a été réparti) le reste n'est qu'un arrondi."""
    head = f"💰 Budget du jour : {fmt.b(fmt.esc(money(daily())))}"
    if not spent:
        return head + " · rien de misé aujourd'hui"
    if poor is None:
        poor = True
    return head + (f" · {fmt.esc(money(spent))} misés, {fmt.esc(money(kept))} gardés " + fmt.i("(journée pauvre)")
                   if poor and kept >= round_step(daily()) else " · tout est misé")


# ---------- lecture des montants ----------

_GROUP = re.compile(r"\d{3}(?:[.,]\d+)?")                  # « 000 », « 000,50 » : un groupe de milliers écrit après une espace


def split_tokens(args):
    """Mots d'une saisie : espaces insécables comprises, devise collée au montant séparée (« 5000FCFA », « 50€ »)."""
    txt = " ".join(map(str, args)).replace(" ", " ").replace(" ", " ")
    return re.sub(r"(?<=\d)(?=[^\W\d_]|[€$£¥])", " ", txt).split()


def _digits(s):
    """« 5000 », « 5,40 », « 5.000 », « 1.234.567 », « 1.234,56 » -> nombre ; None si le format n'est pas clair. Un séparateur suivi
    d'exactement 3 chiffres est un séparateur de milliers (« 5.000 », « 5,000 » = 5000), sinon c'est une virgule décimale."""
    if re.fullmatch(r"\d+", s):
        v = int(s)
    elif not re.fullmatch(r"\d+(?:[.,]\d+)+", s):
        return None
    else:
        seps = re.findall(r"[.,]", s)
        if len(set(seps)) == 1:
            sep = seps[0]
            if len(seps) > 1:                                  # 1.234.567
                if not re.fullmatch(rf"\d{{1,3}}(?:\{sep}\d{{3}})+", s):
                    return None
                v = int(s.replace(sep, ""))
            elif re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", s):    # 5.000 / 5,000
                v = int(s.replace(sep, ""))
            else:
                v = float(s.replace(",", "."))
        else:                                                  # 1.234,56 / 1,234.56 : le dernier séparateur est la virgule
            dec = seps[-1]
            th = "," if dec == "." else "."
            if not re.fullmatch(rf"\d{{1,3}}(?:\{th}\d{{3}})+\{dec}\d+", s):
                return None
            v = float(s.replace(th, "").replace(",", "."))
    return v if math.isfinite(v) and v <= MAX_AMOUNT else None


def take_amount(tokens):
    """Montant écrit au début de `tokens` -> (valeur | None, nombre de mots lus). Les groupes de milliers séparés par des espaces sont
    réunis (« 5 », « 000 » = 5000) ; un décimal (« 5,40 ») ne se réunit pas au mot suivant. Jamais négatif, infini ni NaN."""
    if not tokens or not re.fullmatch(r"\d[\d.,]*", tokens[0]):
        return None, 0
    txt, n = tokens[0], 1
    while n < len(tokens) and _GROUP.fullmatch(tokens[n]) and re.fullmatch(r"\d{1,3}(?: \d{3})*", txt):
        txt += " " + tokens[n]
        n += 1
    v = _digits(txt.replace(" ", ""))
    return (v, n) if v is not None else (None, 0)


def parse_amount(args):
    """« 5 000 », « 5.000 », « 5,5 », « 50 € » -> (montant, devise | None) ; (None, None) si illisible ou absurde. La devise est un
    mot (ou un symbole) après le montant."""
    tokens = split_tokens(args)
    v, n = take_amount(tokens)
    rest = tokens[n:]
    if v is None or len(rest) > 1 or (rest and not CUR_RE.fullmatch(rest[0])):
        return None, None
    return v, (rest[0].upper() if rest else None)


# ---------- commande « budget » ----------

QUICK = (2000, 5000, 10000, 20000)


def command(args):
    """« budget » : le budget actuel et des boutons pour le changer ; « budget 7500 » (ou « budget 50 € ») : le fixe ;
    « budget 0 » : revient aux mises en unités sur la bankroll."""
    from . import telegram
    if not args:
        txt = (f"💰 Ton budget du jour : {money(daily())}.\nKelly le répartit chaque matin entre ses paris, selon sa confiance."
               if active() else "💰 Pas encore de budget du jour : Kelly donne ses mises en unités.")
        return telegram.with_buttons(txt + "\nChoisis un montant, ou écris par exemple : budget 7500",
                                     telegram.inline([[(f"{q:,}".replace(",", " "), f"budget|{q}") for q in QUICK]]))
    amount, currency = parse_amount(args)
    if amount is None:
        return "Montant invalide. Exemple : budget 5000"
    if amount <= 0:
        store.save(FILE, {})
        return "Budget du jour supprimé : Kelly redonne ses mises en unités sur ta bankroll."
    set_daily(amount, currency)
    return (f"✅ Budget du jour : {money(amount)}. Les mises de demain matin seront calculées dessus "
            "(tu peux le changer quand tu veux).")


# ---------- « ❌ Pas joué » ----------

def skip(what, day, undo=False):
    """Retire (ou remet, `undo`) un pari du jour du bilan en argent. -> réponse."""
    from . import played
    t = played.find(what, day)
    if t is None or isinstance(t, str):
        return t or "Ce pari n'existe plus."
    if t.item.get("result") is not None:             # réglé : le retirer (ou le remettre) changerait un bilan déjà annoncé
        return f"Le {t.label} est déjà réglé : trop tard pour le {'remettre dans' if undo else 'retirer du'} bilan."
    if undo:
        t.item.pop("skipped", None)
    else:
        t.item["skipped"] = True
    t.save()
    return f"↩️ {t.label} : compté comme joué." if undo else f"❌ {t.label} : retiré de ton bilan (pas joué)."


# ---------- bilan en argent ----------

def _won_odds(legs, key="min_odds"):
    won = [leg[key] for leg in legs if leg.get("result") == "win"]
    return math.prod(won) if won else 1.0


def gain(x):
    """Gain (devise) d'un pari misé sur le budget et réglé (gagné / perdu) ; None s'il ne compte pas (pas sur le budget, pas joué,
    pas réglé, non vérifiable)."""
    if not x.get("cur") or x.get("skipped") or x.get("result") not in ("win", "loss"):
        return None
    if x.get("played"):
        from .bankroll import played_gain
        return played_gain(x)
    if x["result"] == "loss":
        return -x["stake"]
    if "legs" in x:                                   # combiné à vérifier : compté à la cote mini (1xBet a payé au moins ça)
        return x["stake"] * (_won_odds(x["legs"]) - 1)
    return x["stake"] * (x.get("odds_final", x["odds"]) - 1)


def items(bets, rows, fair_rows):
    from .played import single_rows
    return list(bets.get("combos", [])) + single_rows(rows, bets.get("combos", [])) + list(fair_rows)


def result(bets, rows, fair_rows, days=None, today=None):
    """(nombre de paris comptés, gain total, total misé) des paris misés sur le budget, sur `days` jours (tout sinon)."""
    cut = str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days)) if days else ""
    n = total = staked = 0
    for x in items(bets, rows, fair_rows):
        g = gain(x)
        if g is None or (x.get("date") or "") < cut:
            continue
        n, total, staked = n + 1, total + g, staked + (x.get("played") or {}).get("stake", x["stake"])
    return n, total, staked


def summary_lines(bets, rows, fair_rows, today=None):
    """Lignes du bilan en argent (texte brut) : 7 derniers jours et depuis le début. [] si rien de réglé sur le budget."""
    out = []
    for days, label in ((7, "7 derniers jours"), (None, "depuis le début")):
        n, total, staked = result(bets, rows, fair_rows, days, today)
        if n:
            roi = f", {round(total / staked * 100):+d} % de ce qui a été misé" if staked else ""
            out.append(f"{label} : {signed(total)} sur {n} pari{'s' if n > 1 else ''}{roi}")
    return out
