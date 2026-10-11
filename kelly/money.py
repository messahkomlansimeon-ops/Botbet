"""Budget du jour : le montant que tu es prêt à miser AUJOURD'HUI (« budget 5000 »), modifiable n'importe quel jour (il reste tel quel
jusqu'à ce que tu le changes). Kelly le répartit chaque matin entre le pari du jour, les combinés et les combinés à vérifier chez 1xBet,
selon sa confiance : ce sont des MISES CONSEILLÉES en FCFA. Seuls les paris que tu notes (« ✅ Je l'ai joué », « ✅ Je le joue », /joue :
x["played"] = {odds, stake, at}) comptent dans tes résultats, au bilan et pour les alertes « tu as joué » ; « ✏️ Autre cote » corrige la
cote obtenue et « ↩️ Annuler » retire la note. Sans budget, Kelly ne donne aucune mise conseillée (jamais d'unités).

Répartition (allocate) : chaque pari a une part maximale du budget selon son palier (SHARE), multipliée par sa confiance =
espérance (proba x cote - 1) rapportée à CONF_FULL, bornée à [CONF_MIN, 1]. Journée riche (la somme dépasse le budget) : tout est
réduit dans la même proportion, le budget est entièrement misé. Journée pauvre : Kelly mise moins et garde le reste. Mises arrondies
(round_step) ; une mise plus petite que l'arrondi est abandonnée.

data/mybudget.json : {"daily": montant, "cur": "FCFA"}. « cur » sur la ligne d'un pari = devise de sa mise (stake) : la mise conseillée du
matin, ou celle de la note. Un pari ne compte dans l'argent (gain, bilan) que s'il est NOTÉ (x["played"]) et pas retiré.
"""
import math
import re
from datetime import date, timedelta

from . import fmt, store

FILE = "mybudget.json"
CUR = "FCFA"
MAX_AMOUNT = 1e9                      # au-delà : montant absurde (refusé)
CUR_RE = re.compile(r"(?i)(?!(?:inf|infinity|nan)$)[^\W\d_]{1,8}|[€$£¥]")                     # un mot d'unité après un montant : « FCFA », « k », « € »
FCFA_WORDS = {"fcfa", "cfa", "xof", "f", "fr", "frs", "franc", "francs"}                   # les écritures du FCFA ; « k » = fois mille ; tout autre mot est refusé
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
    """La devise de Kelly : toujours le FCFA (verrouillé : « budget 5k » = 5 000 FCFA, jamais une devise « K » ; voir FCFA_WORDS)."""
    return CUR


def set_daily(amount, currency=None):
    """Fixe le budget du jour en FCFA (`currency` est ignoré : tout est en FCFA)."""
    store.save(FILE, {"daily": round(float(amount), 2), "cur": CUR})


def round_step(budget):
    """Arrondi des mises : 50 pour un budget de 5 000, 500 pour 50 000, 0,5 pour 10."""
    budget = _amount(budget)
    return 10 ** max(0, len(str(int(budget))) - 2) / 2 if budget >= 1 else 0.01


def money(x, currency=None):
    """« 1 500 FCFA » ; un montant non entier garde ses décimales (« 1 500,5 FCFA »), toujours le même séparateur de milliers."""
    if float(x).is_integer():
        txt = f"{x:,.0f}".replace(",", " ")
    else:
        txt = fmt.trim(x, 2)
        whole, _, dec = txt.partition(",")
        digits = re.sub(r"\D", "", whole)
        txt = whole[:len(whole) - len(digits)] + re.sub(r"\B(?=(\d{3})+(?!\d))", " ", digits) + ("," + dec if dec else "")
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


def row_item(x):
    """Ligne de suivi d'un pari -> l'item de `allocate` (kind, tier, prob, odds) qui le décrit ; None si ses données sont illisibles.
    Combiné à vérifier / aux cotes 1xBet / sur mesure (« legs », fair_combos.json) : kind « fair » à sa cote ; combiné réel (« keys »,
    bets.json) : kind « combo » et son palier ; pari du jour (tracked.json) : kind « pari » à sa cote 1xBet quand il en a une."""
    try:
        if "legs" in x:
            return {"kind": "fair", "tier": x["min_odds"], "prob": float(x["prob"]), "odds": float(x["min_odds"])}
        if "keys" in x:
            return {"kind": "combo", "tier": x["tier"], "prob": float(x["prob"]), "odds": float(x["odds"])}
        return {"kind": "pari", "tier": "pari", "prob": float(x["prob"]), "odds": float(x.get("xodds") or x["odds"])}
    except (KeyError, TypeError, ValueError):
        return None


def suggested(x):
    """Mise conseillée (devise du budget) d'un pari : celle décidée le matin (ligne avec sa devise « cur » et une mise > 0) ; 0 si le matin la
    jugeait trop petite pour le budget (« stake » = 0) ; sinon (pas de mise du matin : budget fixé après coup, ancienne ligne) calculée sur le
    budget d'aujourd'hui (allocate, ce pari seul). 0 sans budget du jour : Kelly ne donne alors AUCUNE mise conseillée (jamais d'unités).
    Ne lève jamais."""
    if not active():
        return 0
    st = x.get("stake")
    if x.get("cur") and _amount(st) > 0:
        return st
    if st == 0 and not isinstance(st, bool):
        return 0
    item = row_item(x)
    return allocate([item])[0][0] if item else 0


NO_BUDGET = fmt.NO_BUDGET                  # à la place d'une mise, quand il n'y a pas de budget du jour


def budget_line(spent, kept, poor=None):
    """« 💰 Budget du jour : 5 000 FCFA · 3 800 répartis, 1 200 gardés (journée pauvre) » (HTML). `poor` (allocate, detail) : Kelly conseille
    moins que le budget parce que ses paris sont peu sûrs ; sinon (journée riche, tout a été réparti) le reste n'est qu'un arrondi."""
    head = f"💰 Budget du jour : {fmt.b(fmt.esc(money(daily())))}"
    if not spent:
        return head + " · aucune mise conseillée aujourd'hui"
    if poor is None:
        poor = True
    return head + (f" · {fmt.esc(money(spent))} répartis, {fmt.esc(money(kept))} gardés " + fmt.i("(journée pauvre)")
                   if poor and kept >= round_step(daily()) else " · tout est réparti")


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


def strip_unit(tokens):
    """Retire l'unité écrite après un montant -> (mots sans l'unité, multiplicateur, mot étranger | None) : « k » vaut ×1000 (« 5k » = 5 000,
    « 2k » = 2 000), les écritures du FCFA (FCFA_WORDS) sont ignorées, tout autre mot (« € », « euros »...) est rendu en 3e position : Kelly
    ne compte qu'en FCFA. Un montant seul (un seul mot) n'a pas d'unité."""
    if len(tokens) > 1 and CUR_RE.fullmatch(tokens[-1]):
        word = tokens[-1].casefold()
        if word == "k":
            return tokens[:-1], 1000, None
        if word in FCFA_WORDS:
            return tokens[:-1], 1, None
        return tokens[:-1], 1, tokens[-1]
    return tokens, 1, None


def parse_amount(args):
    """« 5 000 », « 5.000 », « 5,5 », « 5k », « 50 € » -> (montant, devise | None) ; (None, None) si illisible ou absurde. L'unité est un mot (ou
    un symbole) après le montant : « k » multiplie par 1000 (pas une devise), « FCFA » est rendu en capitales, un autre mot est rendu tel quel
    (la commande « budget » le refuse : tout est en FCFA)."""
    tokens = split_tokens(args)
    v, n = take_amount(tokens)
    rest = tokens[n:]
    if v is None or len(rest) > 1 or (rest and not CUR_RE.fullmatch(rest[0])):
        return None, None
    if rest and rest[0].casefold() == "k":
        v *= 1000
        return (int(v) if float(v).is_integer() else v, None) if v <= MAX_AMOUNT else (None, None)
    return v, (rest[0].upper() if rest else None)


# ---------- commande « budget » ----------

QUICK = (2000, 5000, 10000, 20000)
EXPLAIN = ("C'est le montant que tu es prêt à miser AUJOURD'HUI : Kelly s'en sert pour calculer ses mises conseillées. Tu peux le changer "
           "n'importe quel jour, il reste tel quel jusqu'à ce que tu le changes. Seuls les paris que tu notes (« ✅ Je l'ai joué ») "
           "comptent dans tes résultats.")


def command(args):
    """« budget » : le budget actuel et des boutons pour le changer ; « budget 7500 » (ou « budget 7,5k ») : le fixe, en FCFA ;
    « budget 0 » : le supprime (plus de mise conseillée)."""
    from . import telegram
    if not args:
        txt = (f"💰 Ton budget du jour : {money(daily())}.\n{EXPLAIN}"
               if active() else f"💰 Pas encore de budget du jour : Kelly ne donne pas de mise conseillée.\n{EXPLAIN}")
        return telegram.with_buttons(txt + "\nChoisis un montant, ou écris par exemple : budget 7500",
                                     telegram.inline([[(f"{q:,}".replace(",", " "), f"budget|{q}") for q in QUICK]]))
    amount, currency = parse_amount(args)
    if amount is None:
        return "Montant invalide. Exemple : budget 5000"
    if currency and currency.casefold() not in FCFA_WORDS:
        return ("Kelly compte tout en FCFA : écris ton budget en FCFA, par exemple budget 5000 (ou budget 5k pour 5 000 FCFA). "
                f"La devise « {currency} » n'est pas gérée.")
    if amount <= 0:
        store.save(FILE, {})
        return "Budget du jour supprimé : Kelly ne donne plus de mise conseillée (tes paris notés restent comptés dans tes résultats)."
    set_daily(amount, currency)
    return (f"✅ Budget du jour : {money(amount)}. Kelly en tire ses mises conseillées (demain matin, et tout de suite pour les paris "
            "sans mise). Tu peux le changer quand tu veux ; seuls les paris que tu notes comptent dans tes résultats.")


# ---------- « ❌ Pas joué » ----------

def skip(what, day, undo=False):
    """Ancien bouton « ❌ Pas joué » (messages d'avant le mode « je note ce que je joue ») : retire (ou remet, `undo`) un pari du jour du bilan
    en argent. Plus aucun message neuf n'en propose ; le bouton d'un ancien message marche toujours. -> réponse."""
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

def gain(x):
    """Gain (devise) d'un pari NOTÉ (x["played"]) et réglé (gagné / perdu), à la cote et à la mise notées ; None s'il ne compte pas (pas
    noté, retiré par l'ancien « ❌ Pas joué », pas réglé, non vérifiable). Une mise conseillée seule (« cur » + « stake » sans note) ne
    compte jamais."""
    if not x.get("played") or x.get("skipped") or x.get("result") not in ("win", "loss"):
        return None
    from .bankroll import played_gain
    return played_gain(x)


def items(bets, rows, fair_rows):
    from .played import gap_rows, single_rows
    return list(bets.get("combos", [])) + single_rows(rows, bets.get("combos", [])) + gap_rows(rows) + list(fair_rows)


def _counted(bets, rows, fair_rows, days=None, today=None):
    """[(pari, gain)] des paris NOTÉS et réglés, sur `days` jours (tout sinon)."""
    cut = str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days)) if days else ""
    out = []
    for x in items(bets, rows, fair_rows):
        g = gain(x)
        if g is not None and (x.get("date") or "") >= cut:
            out.append((x, g))
    return out


def result(bets, rows, fair_rows, days=None, today=None):
    """(nombre de paris comptés, gain total, total misé) des paris notés et réglés, sur `days` jours (tout sinon)."""
    done = _counted(bets, rows, fair_rows, days, today)
    return len(done), sum(g for _, g in done), sum(x["played"]["stake"] for x, _ in done)


def summary_lines(bets, rows, fair_rows, today=None):
    """Lignes du bilan en argent (texte brut) : 7 derniers jours et depuis le début, avec les paris gagnés. [] si aucun pari noté n'est réglé."""
    out = []
    for days, label in ((7, "7 derniers jours"), (None, "depuis le début")):
        done = _counted(bets, rows, fair_rows, days, today)
        if done:
            n, total, staked = len(done), sum(g for _, g in done), sum(x["played"]["stake"] for x, _ in done)
            won = sum(1 for x, _ in done if x["result"] == "win")
            roi = f", {round(total / staked * 100):+d} % de ce qui a été misé" if staked else ""
            out.append(f"{label} : {signed(total)} sur {n} pari{'s' if n > 1 else ''} ({won} gagné{'s' if won > 1 else ''}){roi}")
    return out
