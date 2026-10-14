"""Mise en forme des messages Telegram : français lisible, virgule décimale, HTML échappé, pas de jargon.

Les messages sont du HTML Telegram (balises <b>, <i>) : tout texte venu de l'extérieur (équipes, joueurs, ligues) passe par
esc(). Les fonctions renvoient un `telegram.Html` (envoyé en parse_mode HTML, repli en texte brut si Telegram refuse).
"""
import html
import math
import re
from datetime import date, datetime

from . import config as C
from .telegram import Html, plain as telegram_plain

DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
          "décembre")
# Mots internes qui ne doivent jamais apparaître dans un message du jour (testé).
JARGON = ("consensus", "désaccord", "edge", "steam", "clv", "brier", "score", "xg", "crédits", "kelly fraction")


def esc(s):
    return html.escape(str(s), quote=False)


def b(s):
    return f"<b>{s}</b>"


def i(s):
    return f"<i>{s}</i>"


def num(x, d=2):
    """1.55 -> « 1,55 »."""
    return f"{x:.{d}f}".replace(".", ",")


def trim(x, d=2):
    """Décimales inutiles retirées : 2.50 -> « 2,5 », 3.0 -> « 3 »."""
    s = f"{x:.{d}f}"
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return s.replace(".", ",")


def odds_txt(x):
    """Cote : 2 décimales, 1 au-delà de 100 (« 156,7 »)."""
    return num(x, 2) if x < 100 else num(x, 1)


def pct(p):
    """0.314 -> « 31 % » ; sous 1 % une décimale (« 0,7 % ») ; sous 0,1 % « < 0,1 % »."""
    v = p * 100
    if 0 < v < 0.1:
        return "< 0,1 %"
    return f"{v:.1f} %".replace(".", ",") if 0 < v < 1 else f"{round(v)} %"


def thousands(n):
    """12345.6 -> « 12 346 » (entier arrondi, espace entre les milliers)."""
    return f"{int(round(n)):,}".replace(",", " ")


def units(x):
    """Montant en unités : « 2 u », « 0,2 u ». Plus utilisé dans les messages (tout est en FCFA) : gardé comme outil de format."""
    return f"{trim(x, 1 if abs(x) >= 1 else 2)} u"


NO_BUDGET = "fixe ton capital avec « 💼 Mon capital »"      # à la place d'une mise, quand il n'y a pas de budget du jour (jamais d'unités)


NO_EDGE = "Kelly ne conseille pas de miser dessus (espérance négative)"      # à la place d'une mise, quand le pari perd en moyenne à sa cote


def stake_line(stake_txt, suffix=""):
    """Ligne de mise d'un message : « 💰 Mise : 1 500 FCFA » (`suffix` en italique après), « 💰 Kelly ne conseille pas de miser dessus (espérance
    négative). » (NO_EDGE : aucune ligne « Mise : ») ou, sans mise (`stake_txt` vide : pas de budget du jour), de quoi régler le budget."""
    if stake_txt == NO_EDGE:
        return i("💰 " + esc(NO_EDGE) + ".")
    if not stake_txt:
        return no_stake_line()
    return f"💰 Mise : {b(esc(stake_txt))}" + (f" {i(suffix)}" if suffix else "")


def no_stake_line():
    """Une ligne (une fois par message) à la place de la mise conseillée quand il n'y a pas de budget du jour."""
    return i("💰 Mise : " + esc(NO_BUDGET))


def signed(x, suffix=" u"):
    """+10,2 u / −0,2 u (vrai signe moins)."""
    return ("+" if x >= 0 else "−") + trim(abs(x), 1 if abs(x) >= 1 else 2) + suffix


def hhmm(t):
    """« 21:00 » (UTC = Abidjan) -> « 21h00 »."""
    t = str(t or "")
    return t.replace(":", "h", 1) if re.fullmatch(r"\d\d:\d\d", t) else t


def fr_day(d):
    """« jeudi 8 octobre »."""
    d = d if isinstance(d, date) else (d.date() if isinstance(d, datetime) else date.fromisoformat(str(d)[:10]))
    return f"{DAYS[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {MONTHS[d.month - 1]}"


def tier_name(title):
    """« 🛡️ SÛR (cote 5 à 7) » -> « 🛡️ SÛR »."""
    return title.split(" (")[0]


def tier_word(title):
    """« 🛡️ SÛR (cote 5 à 7) » -> « Sûr » ; « 🎯 SPÉCIAL CORNERS/CARTONS… » -> « Spécial »."""
    words = tier_name(title).split()
    return words[1].capitalize() if len(words) > 1 else words[0].capitalize()


plain = telegram_plain


# ---------- libellés de marché, comme on les lit chez le bookmaker ----------

SINGULAR = {"tirs cadrés": "tir cadré", "tirs": "tir", "fautes": "faute", "points": "point", "rebonds": "rebond",
            "passes": "passe", "paniers à 3 pts": "panier à 3 pts"}


TEAM_MARKET = re.compile(r"^team_(shots|sot|fouls|throws|offsides|corners|yellow)_(total|home|away)$")
# mots de 1xBet (« Tirs vers le but », « Tirs cadrés ») : le pari se retrouve directement chez le bookmaker
TEAM_UNITS = {"shots": "tirs vers le but", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu", "corners": "corners",
              "yellow": "cartons jaunes"}


def label(market, name, point, who, group):
    """« Plus de 2,5 buts », « Moins de 9,5 corners », « Total cartons : plus de 3,5 », « Mbappé : 2 tirs cadrés ou plus »,
    « Les deux équipes marquent : oui », « Double chance : Nul ou Flamengo », « Flamengo gagne »."""
    if market in ("team_totals_home", "team_totals_away"):                 # buts d'une équipe : « Inter : plus de 1,5 buts »
        who_txt = who or ("Équipe à domicile" if market.endswith("_home") else "Équipe extérieure")
        return f"{who_txt} : {'plus' if name == 'Over' else 'moins'} de {trim(point)} but{'s' if point and float(point) > 1 else ''}"
    tm = TEAM_MARKET.match(market)
    if tm:                                                         # tirs, tirs cadrés, fautes, touches, hors-jeu : total ou par équipe
        u = TEAM_UNITS[tm[1]]
        if market.endswith("_total"):
            return f"{'Plus' if name == 'Over' else 'Moins'} de {trim(point)} {u} au total"
        return f"{who or ('Équipe à domicile' if market.endswith('_home') else 'Équipe extérieure')} : " \
               f"{'plus' if name == 'Over' else 'moins'} de {trim(point)} {u}"
    unit = C.UNITS.get(market)
    if isinstance(unit, dict):
        unit = unit.get(group, "")
    over = name == "Over"
    if market.startswith("player_") and unit and point is not None:
        n = int(point) + 1 if float(point) % 1 else int(point)
        u = SINGULAR.get(unit, unit) if n == 1 else unit
        who_txt = f"{who} : " if who else ""
        return f"{who_txt}{n} {u} ou plus" if over else f"{who_txt}moins de {n} {unit}"
    if market == "alternate_totals_cards":
        return f"Total cartons : {'plus' if over else 'moins'} de {trim(point)}"
    if unit:
        return f"{'Plus' if over else 'Moins'} de {trim(point)} {unit}"
    if market == "btts":
        return "Les deux équipes marquent : " + ("oui" if name == "Yes" else "non")
    if market == "spreads":
        return f"{name} {trim(point, 2) if point < 0 else '+' + trim(point, 2)} (handicap)"
    if market == "double_chance":
        return "Double chance : " + name.replace(" or ", " ou ").replace("Draw", "Nul")
    return "Match nul" if name == "Draw" else f"{name} gagne"


# ---------- « pourquoi » en mots simples ----------

_STATS = [
    (re.compile(r"stats réelles : ([\d.]+) (corners|cartons) attendus vs ligne ([\d.]+)"),
     lambda m: f"Kelly s'attend à {m[1].replace('.', ',')} {m[2]} ; la ligne est à {m[3].replace('.', ',')}"),
    (re.compile(r"(?:xG|moyennes de buts) : ([\d.]+) buts attendus vs ligne ([\d.]+)"),
     lambda m: f"Kelly s'attend à {m[1].replace('.', ',')} buts ; la ligne est à {m[2].replace('.', ',')}"),
    (re.compile(r"(?:xG|moyennes de buts) : ([\d.]+)-([\d.]+) buts attendus"),
     lambda m: f"Kelly s'attend à {m[1].replace('.', ',')} buts pour l'un et {m[2].replace('.', ',')} pour l'autre"),
    (re.compile(r"stats réelles : ([\d.]+) (tirs cadrés|tirs|fautes)/90( \(estimé\))? vs ligne ([\d.]+)"),
     lambda m: f"Il fait {m[1].replace('.', ',')} {m[2]} par match en moyenne ; la ligne est à {m[4].replace('.', ',')}"),
]


def _kind(piece):
    if piece.startswith(("cote plus haute", "valeur +")):
        return 1
    if piece.startswith("la cote baisse"):
        return 2
    if piece.startswith("les bookmakers"):
        return 3
    return 0                                   # texte du modèle d'avant-match


def why_line(c, reason=None):
    """Une phrase courte et claire sur la raison du pari (priorité : stats, valeur, mouvement de cote, bookmakers)."""
    pieces = [p.strip() for p in str(c.get("why") or "").split(", ") if p.strip()]
    pieces.sort(key=_kind)
    for p in pieces:
        for rx, fn in _STATS:
            m = rx.search(p)
            if m:
                return fn(m)
        if _kind(p):
            return p[0].upper() + p[1:]
    pm = c.get("pm") or {}
    if reason == "stats":
        if (pm.get("pgap") or 0) >= C.LEG_PM_GAP:
            return f"Kelly le juge plus probable que les bookmakers ({round(pm['pgap'] * 100)} points de plus)"
        if (pm.get("xgd") or 0) >= C.LEG_XGD:
            return f"D'après les stats, cette équipe devrait marquer {num(pm['xgd'], 1)} but de plus que l'autre"
    if reason == "safe" or (c.get("prob") or 0) >= C.LEG_SAFE_PROB:
        return f"Très probable : {pct(c['prob'])} de chances"
    if reason in ("proche", "force"):
        return f"Repêché statistiquement : {pct(c['prob'])} de chances (proche du seuil)"
    return ""


def best_note(c):
    """« jusqu'à 4,60 chez Coolbet » quand la meilleure cote dépasse nettement la cote typique, sinon ''."""
    best = c.get("best_odds")
    if best and best >= c["odds"] * (1 + C.BEST_NOTE_GAP):
        return f"jusqu'à {num(best)} chez {esc(c.get('book') or '?')}"
    return ""


def event_name(c):
    return f"{esc(c['home'])} – {esc(c['away'])}" if c.get("home") and c.get("away") else esc(c.get("event", ""))


# ---------- repères visuels : drapeau du pays de la ligue, icône du marché, pastille solide / moins sûr ----------

# pays lu dans la clé de compétition (« soccer_spain_la_liga », « soccer_xb_argentina_2 ») ; les plus longs d'abord (costa_rica avant…)
FLAGS = {"argentina": "🇦🇷", "australia": "🇦🇺", "austria": "🇦🇹", "belgium": "🇧🇪", "brazil": "🇧🇷", "chile": "🇨🇱", "china": "🇨🇳",
         "colombia": "🇨🇴", "costa_rica": "🇨🇷", "denmark": "🇩🇰", "ecuador": "🇪🇨", "england": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
         "epl": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f", "efl": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
         "fa_cup": "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f", "finland": "🇫🇮", "france": "🇫🇷", "germany": "🇩🇪",
         "greece": "🇬🇷", "italy": "🇮🇹", "japan": "🇯🇵", "korea": "🇰🇷", "league_of_ireland": "🇮🇪", "ireland": "🇮🇪", "mexico": "🇲🇽",
         "netherlands": "🇳🇱", "norway": "🇳🇴", "paraguay": "🇵🇾", "peru": "🇵🇪", "poland": "🇵🇱", "portugal": "🇵🇹", "qatar": "🇶🇦",
         "russia": "🇷🇺", "saudi": "🇸🇦", "spain": "🇪🇸", "spl": "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
         "scotland": "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f", "sweden": "🇸🇪", "switzerland": "🇨🇭", "turkey": "🇹🇷",
         "uae": "🇦🇪", "uruguay": "🇺🇾", "usa": "🇺🇸", "usl": "🇺🇸", "algeria": "🇩🇿", "bolivia": "🇧🇴", "bulgaria": "🇧🇬", "canada": "🇨🇦",
         "croatia": "🇭🇷", "cyprus": "🇨🇾", "czech": "🇨🇿", "guatemala": "🇬🇹", "hungary": "🇭🇺", "indonesia": "🇮🇩", "iran": "🇮🇷",
         "israel": "🇮🇱", "northern_ireland": "🇬🇧", "romania": "🇷🇴", "serbia": "🇷🇸", "slovakia": "🇸🇰", "slovenia": "🇸🇮", "ukraine": "🇺🇦",
         "venezuela": "🇻🇪", "egypt": "🇪🇬", "morocco": "🇲🇦", "tunisia": "🇹🇳", "nigeria": "🇳🇬", "south_africa": "🇿🇦", "ivory_coast": "🇨🇮",
         "wales": "🏴\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f", "iceland": "🇮🇸", "estonia": "🇪🇪", "latvia": "🇱🇻",
         "lithuania": "🇱🇹", "india": "🇮🇳", "thailand": "🇹🇭", "vietnam": "🇻🇳", "honduras": "🇭🇳", "el_salvador": "🇸🇻", "panama": "🇵🇦"}
WORLD = "🌍"                                   # coupes d'Europe, sélections, compétition inconnue


def flag(c):
    """Drapeau du pays de la ligue d'un pari (« 🇪🇸 »), d'après sa clé de compétition ; 🌍 pour une coupe internationale ou une clé inconnue."""
    key = str(c.get("sport_key") or "")
    rest = key.split("_", 1)[1] if "_" in key else ""
    rest = rest[3:] if rest.startswith("xb_") else rest
    for name in sorted(FLAGS, key=len, reverse=True):
        if rest == name or rest.startswith(name + "_"):
            return FLAGS[name]
    return WORLD


# préfixes de marché -> icône (mêmes familles que combos.KINDS) ; le premier préfixe qui correspond l'emporte
ICONS = (("alternate_totals_corners", "🚩"), ("team_corners_", "🚩"), ("alternate_totals_cards", "🟨"), ("team_yellow_", "🟨"),
         ("team_offsides_", "🏁"), ("team_shots_", "🎯"), ("team_sot_", "🎯"), ("team_fouls_", "🛑"), ("team_throws_", "🙌"),
         ("player_", "👤"), ("double_chance", "🛡️"), ("h2h", "🏆"), ("spreads", "🏆"), ("totals", "⚽"), ("team_totals", "⚽"),
         ("alternate_totals", "⚽"), ("btts", "⚽"))
SOLID, WEAK, SHARED = "🟢", "🟠", "🔗"


def market_icon(c):
    """Icône du type de pari : 🚩 corners, 🟨 cartons, 🏁 hors-jeu, 🎯 tirs, 🛑 fautes, 🙌 touches, ⚽ buts, 🛡️ double chance, 🏆 résultat ; 👉 sinon."""
    m = str(c.get("market") or "")
    return next((ic for p, ic in ICONS if m.startswith(p)), "👉")


def legend(weak=False, shared=False):
    """Légende des repères d'un combiné (« 🟢 solide · 🟠 moins sûr · 🔗 déjà dans tes tickets »), en italique ; '' quand il n'y a rien à expliquer."""
    parts = ([f"{SOLID} solide", f"{WEAK} moins sûr"] if weak else []) + ([f"{SHARED} déjà dans N de tes tickets"] if shared else [])
    return i(" · ".join(parts)) if parts else ""


def bar(part, cells=10):
    """« ▰▰▰▰▰▰▰▰▰▱ » : jauge de `cells` cases pour une part de 0 à 1 (arrondi inférieur : une case reste vide tant qu'on n'est pas au bout)."""
    part = max(0.0, min(1.0, part or 0.0))
    full = min(cells, math.floor(part * cells + 1e-9))
    return "▰" * full + "▱" * (cells - full)


RULE = "━━━━━━━━━━━━"                         # séparateur court : ne déborde pas sur un écran de téléphone


def _match_line(n, c, mark=""):
    """« 🇫🇷 <i>14h00 · Ligue 1</i> » puis « <b>1. PSG – Lyon</b> » : le pays, l'heure et la ligue d'une jambe, puis le match. `mark` : repère
    ajouté après le match (« · 🔗3 » : déjà dans 3 de tes tickets)."""
    when = " · ".join(x for x in (hhmm(c.get("start")), esc(c.get("league") or "")) if x)
    name = b(f"{'' if n is None else f'{n}. '}{event_name(c)}") + mark
    return f"{flag(c)} {i(when)}\n{name}" if when else f"{flag(c)} {name}"


def _shared_mark(c, shared):
    """« · 🔗3 » quand le match de `c` est déjà dans 3 de tes tickets joués (`shared` : {id du match: nombre}) ; '' sinon."""
    k = (shared or {}).get(c.get("id"))
    return f" · {SHARED}{k}" if k else ""


def price_txt(c, real=False, when=None):
    """Cote d'un pari réel, honnête sur son origine : « cote 1xBet 1,50 (09:30) » quand Kelly l'a lue chez 1xBet (c["xodds"], `when` :
    heure « HH:MM » du relevé) ; « pas proposé chez 1xBet » pour un pari absent de 1xBet (xnone, xmiss, ou `real` : 1xBet a chiffré
    d'autres paris du jour) ; sinon « cote de référence … (pas 1xBet) » : la cote typique des autres bookmakers."""
    if c.get("xodds"):
        return f"cote 1xBet {b(odds_txt(c['xodds']))}" + (f" ({esc(when)})" if when else "")
    if real or c.get("xnone") or c.get("xmiss"):
        return i("pas proposé chez 1xBet")
    return f"cote de référence {b(odds_txt(c['odds']))} " + i("(pas 1xBet)")


def opinion_line(c):
    """Ligne d'avis sous un pari (pronostic 💎 ou jambe d'un combiné) : « 📊 1xBet : 49 % · Kelly : 71 % » (+ « · retenu : 61 % » là où l'ensemble
    décide) ; '' sans avis de 1xBet. Italique, HTML échappé."""
    from . import ensemble                           # import tardif : ensemble importe odds, qui importe fmt
    try:
        txt = ensemble.explain_c(c)
    except (KeyError, TypeError, ValueError):
        txt = ""
    return i(f"📊 {esc(txt)}") if txt else ""


def x_prob(c):
    """Probabilité de 1xBet sans marge d'un pari : p_x (ensemble), sinon xp (relevé 1xBet), sinon px (live) ; None si inconnue."""
    return next((c[k] for k in ("p_x", "xp", "px") if c.get(k) is not None), None)


def prob_label(c):
    """« retenu » quand c["prob"] est la proba du MARCHÉ (famille dont le poids du modèle est figé à 0, audit du 14/10), « Kelly » quand c'est celle du modèle."""
    from . import ensemble                           # import tardif (voir opinion_line)
    return "retenu" if ensemble.model_silent(c.get("market") or "") else "Kelly"


def chances_line(c, weak=False, extra=""):
    """« 🟢 <i>Kelly 67 % · 1xBet 48 % (+19)</i> » : pastille (🟢 solide, 🟠 moins sûr : `weak`), les chances retenues par Kelly et, si on la connaît,
    celle de 1xBet sans marge, avec l'écart en points dès 3 points. `extra` : précision ajoutée en fin de ligne (« mal payé »)."""
    px = x_prob(c)
    gap = round((c["prob"] - px) * 100) if px is not None else 0
    txt = (f"{prob_label(c)} {pct(c['prob'])}" + (f" · 1xBet {pct(px)}" if px is not None else "")
           + (f" ({'+' if gap > 0 else '−'}{abs(gap)})" if abs(gap) >= 3 else "") + (f" · {extra}" if extra else ""))
    return f"{WEAK if weak else SOLID} {i(esc(txt))}"


def leg(n, c, reason=None, price=None, weak=False, shared=None, extra=""):
    """Bloc d'une jambe : pays, heure et ligue ; le match ; l'icône du marché, le pari et sa cote ; la pastille et les chances (Kelly / 1xBet) ;
    la raison s'il y en a une. `price` : libellé de la cote (price_txt) à la place de la cote en gras. `weak` : pari moins sûr (🟠),
    `shared` : {id du match: nombre de tes tickets où il est déjà}, `extra` : voir chances_line."""
    note = best_note(c)
    lines = [_match_line(n, c, _shared_mark(c, shared)),
             f"{market_icon(c)} {esc(c['pick'])} · {price or b(odds_txt(c['odds']))}" + (f" {i('(' + note + ')')}" if note else ""),
             chances_line(c, weak, extra)]
    why = why_line(c, reason)
    if why:
        lines.append(f"💡 {i(esc(why))}")
    return "\n".join(lines)


def expect(prob, odds_total):
    """« espérance +6 % » : ce que rend la mise en moyenne à cette cote, d'après les probabilités de Kelly."""
    return f"espérance {signed((prob * odds_total - 1) * 100, ' %')}"


def code(c):
    """Un code coupon en <code> : un appui dessus le copie dans toutes les applications Telegram."""
    return f"<code>{esc(c)}</code>"


def coupon_block(coupon, retry=None):
    """Code coupon 1xBet d'un combiné ou d'un pari (xbet.save_coupon), à charger dans l'appli (en <code>, à copier d'un appui) ; '' sans code. `retry` : pari qui devait
    avoir son code (toutes les jambes lues chez 1xBet) mais dont l'enregistrement a échoué -> une ligne dit quoi faire (« touche … »)."""
    if coupon:
        return f"\n\n🎟️ Code : {code(coupon)} " + i("(1xBet › Ticket › Charger le coupon)")
    return "\n\n" + i(f"🎟️ Code indisponible pour l'instant : {esc(retry)}") if retry else ""


def xbet_note(legs, when=None):
    """Pied d'un pari réel dont toutes les cotes sont celles de 1xBet (xprice.price_picks) ; '' sinon. `when` : heure « HH:MM » du relevé."""
    if not legs or not all(c.get("xodds") for c in legs):
        return ""
    return "\n" + i(f"🕒 Cotes 1xBet de {esc(when)} : vérifie-les avant de valider." if when else "🕒 Cotes 1xBet : vérifie-les avant de valider.")


def coupon_top(coupon):
    """Ligne du code coupon en haut d'un message (« 🎟️ SKU6L touche pour copier ») ; '' sans code. Le code est aussi rappelé en bas (coupon_block)."""
    return f"🎟️ {code(coupon)}  " + i("touche pour copier") if coupon else ""


def _foot(stake, weak, shared, tail):
    """Pied d'un combiné : séparateur, légende des repères, ligne de mise (quand elle n'est pas un montant : budget à régler, pas de mise) et `tail`."""
    rows = [x for x in (legend(weak, shared), stake) if x]
    return "\n\n" + RULE + "".join("\n" + x for x in rows) + ("\n" + tail.lstrip("\n") if tail.strip() else "")


def one_in(p):
    """« 1 chance sur 170 » : 1 / p arrondi (2 chiffres significatifs au-delà de 100 : « 1 chance sur 3 300 »)."""
    n = 1 / p
    if n >= 100:
        n = round(n, -int(math.floor(math.log10(n))) + 1)
    return f"1 chance sur {thousands(n)}"


def is_fill_leg(c):
    """Jambe de remplissage : strategy « fill », ou jambe `saison` sans valeur (sval absent ou nul)."""
    return c.get("strategy") == "fill" or (c.get("strategy") == "saison" and not c.get("sval"))


def combo_ev_line(picks, prob, odds_total, mini=False):
    """« 📈 Espérance du combiné : +4 % (valeur 3/5, remplissage 2/5) » : Π(p x cote) − 1, soit la proba retenue du combiné (prudence par jambe comprise) x sa cote − 1 ; toujours dite, même négative.
    « valeur » : jambes qui ne sont pas du remplissage (strategy « fill ») ; à la cote MINI (jambes sans cote 1xBet) la ligne le dit. '' sans cote ou sans proba."""
    if not odds_total or prob is None or not picks:
        return ""
    fill = sum(1 for c in picks if is_fill_leg(c))
    mini = " à la cote mini" if mini else ""
    return f"📈 Espérance du combiné{mini} : {signed((prob * odds_total - 1) * 100, ' %')} (valeur {len(picks) - fill}/{len(picks)}, remplissage {fill}/{len(picks)})"


def combo_chances(picks, prob, odds_total, ev=True, mini=False):
    """Lignes des chances d'un combiné (HTML) : « 🎯 3 paris · 29 % selon Kelly · 15 % selon 1xBet » puis « 📊 Espérance : +54 % selon Kelly · −18 % selon
    1xBet » quand toutes ses jambes ont leur chance selon 1xBet sans sa marge (combos.market_prob : l'avis du marché ; audit du 13/10, les résultats réels
    tombaient entre les deux, plus près de 1xBet) ; sinon « 🎯 3 paris · 29 % de chances · espérance +12 % ». `ev` : avec l'espérance. Ticket « loterie »
    (money.is_lottery) : à la place de l'espérance, « 🎰 Ticket loterie : 1 chance sur 170 selon 1xBet. En moyenne il rend 46 % de moins que la mise :
    100 FCFA au plus. »"""
    from . import combos, money
    pm = combos.market_prob(picks)
    lottery = money.is_lottery(prob, odds_total, pm)
    if pm is None:
        txt = f"🎯 {len(picks)} paris · {pct(prob)} de chances" + (f" · {expect(prob, odds_total)}" if ev else "")
    else:
        txt = f"🎯 {len(picks)} paris · {pct(prob)} selon Kelly · {pct(pm)} selon 1xBet"
        if ev and not lottery:
            txt += (f"\n📊 Espérance : {signed((prob * odds_total - 1) * 100, ' %')} selon Kelly · "
                    f"{signed((pm * odds_total - 1) * 100, ' %')} selon 1xBet")
    if lottery:
        p, who = (pm, "selon 1xBet") if pm is not None else (prob, "selon Kelly")
        back = p * odds_total - 1 if p and odds_total else None
        cap = money.money(money.LOTTERY_STAKE)
        txt += "\n" + i(esc(f"🎰 Ticket loterie : {one_in(p)} {who}." if p else "🎰 Ticket loterie.")
                        + esc(f" En moyenne il rend {pct(-back)} de moins que la mise : {cap} au plus." if back is not None and back < 0
                              else f" Mise petite : {cap} au plus."))
    line = combo_ev_line(picks, prob, odds_total, mini)
    return txt + ("\n" + line if line else "")


def _header(title, chances, stake, note, coupon):
    """En-tête d'un combiné : le titre en gras (avec sa cote), les chances, la mise quand c'est un montant, `note` (lignes courtes déjà en HTML), le code
    coupon. -> (texte, ligne de mise à mettre dans le pied ou '')."""
    amount = stake if stake.startswith("💰 Mise : <b>") else ""
    head = [b(title), chances] + ([amount] if amount else []) + ([note] if note else []) + ([coupon_top(coupon)] if coupon else [])
    return "\n".join(head), ("" if amount else stake)


def _shared_ids(shared):
    return bool(shared) and any(shared.values())


def combo(title, picks, odds_total, prob, stake=None, reasons=None, stake_txt=None, coupon=None, when=None, retry=None, weak=None, shared=None, note="",
          extra=None):
    """Message d'un combiné : titre et cote ; nombre de paris, chances et espérance ; la mise (montant) ; `note` ; le code coupon ; puis les jambes,
    séparées par une ligne vide (drapeau, icône du marché, pastille) ; un pied (légende, mise à régler, heure des cotes, code). `stake_txt` : la mise
    conseillée sur le budget du jour (« 1 500 FCFA ») ; sans elle (pas de budget du jour), une ligne dit de régler le budget (jamais de mise en unités ;
    `stake`, ancienne mise en unités, est ignoré). `coupon` : code coupon 1xBet (toutes les jambes aux cotes 1xBet), `when` : heure « HH:MM » de ces
    cotes, `retry` : voir coupon_block. `weak` : clés des paris moins sûrs (🟠), `shared` : {id du match: nombre de tes tickets où il est déjà},
    `extra` : {clé: précision en fin de ligne de chances}."""
    reasons, weak, extra = reasons or {}, set(weak or ()), extra or {}
    head, stake_foot = _header(f"{esc(tier_name(title))} — cote {odds_txt(odds_total)}", combo_chances(picks, prob, odds_total), stake_line(stake_txt),
                               note, coupon)
    legs = [leg(k, c, reasons.get(c["key"]), weak=c.get("key") in weak, shared=shared, extra=extra.get(c.get("key"), "")) for k, c in enumerate(picks, 1)]
    return Html(head + "\n\n" + "\n\n".join(legs)
                + _foot(stake_foot, bool(weak), _shared_ids(shared), coupon_block(coupon, retry) + xbet_note(picks, when)))


def single(c, reason=None, title="⭐ PARI DU JOUR", stake_txt=None, coupon=None, when=None, retry=None):
    """Pari du jour : une seule jambe (et sa mise conseillée sur le budget du jour, `stake_txt`, sinon la ligne « fixe ton capital »).
    `coupon` / `when` / `retry` : voir combo."""
    stake = stake_line(stake_txt)
    amount = stake if stake.startswith("💰 Mise : <b>") else ""
    head = [b(f"{title} — cote {odds_txt(c['odds'])}"), f"🎯 {pct(c['prob'])} de chances"] + ([amount] if amount else []) \
        + ([coupon_top(coupon)] if coupon else [])
    return Html("\n".join(head) + "\n\n" + leg(None, c, reason)
                + _foot("" if amount else stake, False, False, coupon_block(coupon, retry) + xbet_note([c], when)))


def _legend():
    """Fin de la légende 💎 : ce que Kelly suit (cote 1xBet sur les buts et le résultat, ses statistiques ailleurs) quand l'ensemble décide ; '' sinon."""
    from . import ensemble
    t = ensemble.legend()
    return esc(t) + " " if t else ""


def pronos(items, limit=10, title=None, intro=None, when=None):
    """Pronostics sans cote de bookmaker, regroupés par match : le match, puis un pari par ligne avec sa cote minimale (ou sa cote
    1xBet, 💎). `title` / `intro` : autre titre et autre phrase d'introduction (relevé 1xBet de l'après-midi). `when` : heure « HH:MM »
    du relevé des cotes 1xBet, rappelée dans l'introduction (sinon « aujourd'hui »). Chaque pronostic peut porter l'heure de SA lecture (c["xat"]) :
    quand elles diffèrent (relevé partiel : seuls certains matchs ont été relus), l'introduction le dit et chaque ligne donne son heure."""
    shown = items[:limit]
    if not shown:
        return Html(b("🎲 Pronostics Kelly") + "\n" + i("Aucun pronostic assez solide : Kelly ne montre que des pronostics "
                                                                 "viables (assez de chances et de données)."))
    priced = any(c.get("xodds") for c in shown)
    unpriced = not all(c.get("xodds") for c in shown)
    stamps = {c.get("xat") or when for c in shown if c.get("xodds")} - {None}
    partial = len(stamps) > 1                          # des cotes lues à des heures différentes : l'heure du dernier relevé ne vaut pas pour toutes
    if len(stamps) == 1:
        when = next(iter(stamps))                      # toutes lues à la même heure : c'est elle, même si un relevé plus récent a eu lieu ailleurs
    stamp = f" Cotes 1xBet de {when}." if when and priced and not partial else ""
    maybe = any(c.get("xodds") and c.get("xkind") == "probable" for c in shown)
    value = any(c.get("xodds") and c.get("xkind") != "probable" for c in shown)            # pronostics de buts sans avis d'un book de référence : « probables », pas « valeur »
    when_txt = "(relevé partiel : heure sur chaque pari)" if partial else f"de {esc(when)}" if when else "du jour"
    note = ("🎯 = probable, pas une valeur (aucun book de référence ne confirme la cote, ou elle ne dépasse pas celle des books de référence) : pas de mise conseillée." if maybe else "")
    lines = [b(title or "🎲 Pronostics Kelly"),
             i(esc(intro) + esc(stamp) if intro else
               ((f"💎 = cote 1xBet {when_txt} assez haute pour jouer." if value else f"Cotes 1xBet {when_txt}.") if priced else "")
               + (" " + note if note else "")
               + (" « mini » : joue seulement si 1xBet paie au moins cette cote." if unpriced else "")).strip()]
    by = {}
    for c in shown:
        by.setdefault(c["id"], []).append(c)
    for cs in by.values():
        lines.append("")
        lines.append(_match_line(None, cs[0]))
        for c in cs:
            q = c.get("conf")
            data = " · données moyennes" if q is not None and q < 0.4 else ""
            px = x_prob(c)
            odds_vs = f" · 1xBet {pct(px)}" if px is not None else ""
            if c.get("xodds"):
                who = prob_label(c)
                at = f" (lue à {esc(c.get('xat') or when)})" if partial and (c.get("xat") or when) else ""
                if c.get("xkind") == "probable":
                    lines.append(f"🎯 {esc(c['pick'])} · {b(num(c['xodds']))}{at}")
                    note = ("favori des books de référence, sans valeur" if c.get("p_sharp") is not None else "probable, pas une valeur (aucun book de référence)")
                    lines.append(i(f"   {who} {pct(c['prob'])}{odds_vs} · {note}{data}"))
                    continue
                lines.append(f"💎 {esc(c['pick'])} · {b(num(c['xodds']))}{at}")
                ev_txt = f" · valeur {c['xev'] * 100:+.0f} %" if c.get("xev") is not None else ""       # famille figée sans avis de marché : pas d'espérance
                lines.append(i(f"   {who} {pct(c['prob'])}{odds_vs}{ev_txt}{data}"))
            else:
                lines.append(f"👉 {esc(c['pick'])} · mini {b(num(c['min_odds']))}")
                lines.append(i(f"   Kelly {pct(c['prob'])}{data}"))
    if len(items) > len(shown):
        lines += ["", i(f"+ {len(items) - len(shown)} autres : écris un nom d'équipe pour voir un match")]
    return Html("\n".join(lines))


# ---------- « 💰 1xBet paie plus que le marché » (kelly/sharpgap.py) ----------

GAP_TITLE = "💰 1xBet paie plus que le marché"
GAP_INTRO = ("La cote 1xBet est au-dessus de la cote juste des books de référence (Pinnacle, marge retirée) : ce n'est pas un avis de Kelly sur le match, "
             "c'est un prix en retard chez 1xBet. Vérifie la cote avant de valider.")


def gap_block(n, x, d, coupon=None, stake_txt=None):
    """Un pari « 1xBet paie plus que le marché » : le match, le pari avec la cote 1xBet et la cote juste, l'âge du relevé, la mise conseillée (`stake_txt`, FCFA)
    et son code coupon en <code>. `x` : candidat de sharpgap, `d` : sharpgap.describe(x), `n` : numéro affiché dans le message (None : sans)."""
    lines = [_match_line(n, x), f"👉 {esc(d['title'])} — {esc(d['line'])}", i(f"   {esc(d['age'])}")]
    if stake_txt == NO_EDGE:
        lines.append(i("💰 " + esc(NO_EDGE) + "."))
    elif stake_txt:
        lines.append(f"💰 Mise conseillée : {b(esc(stake_txt))}")
    if coupon:
        lines.append(f"🎟️ Code coupon 1xBet : {code(coupon)}")
    return "\n".join(lines)


def gap_message(blocks, when=None, budget=True, intro=True):
    """Le message « 💰 1xBet paie plus que le marché » : titre (heure `when` du relevé), phrase d'explication, un bloc par pari (gap_block), et, sans budget du
    jour (`budget` faux), une ligne pour le régler."""
    head = [b(GAP_TITLE + (f" · {esc(when)}" if when else ""))] + ([i(esc(GAP_INTRO))] if intro else [])
    return Html("\n".join(head) + "\n\n" + "\n\n".join(blocks) + ("" if budget else "\n\n" + no_stake_line()))


def combo_changes(removed, added, changed=()):
    """Paris retirés d'un combiné remis aux cotes du moment (retirés par 1xBet ou devenus mal payés), leurs remplaçants, et les paris
    dont la ligne ou le côté a changé (`changed` : [(ancien, nouveau)])."""
    lines = [f"❌ Retiré : {esc(c.get('home', '?'))} – {esc(c.get('away', '?'))} ({esc(c.get('pick', ''))})" for c in removed]
    lines += [f"🔁 Changé : {esc(n.get('home', '?'))} – {esc(n.get('away', '?'))} : {esc(o.get('pick', ''))} → {esc(n.get('pick', ''))}"
              for o, n in changed]
    lines += [f"➕ Ajouté : {esc(c['home'])} – {esc(c['away'])} ({esc(c['pick'])})" for c in added]
    return "\n".join(lines)


def fair_leg(n, c, weak=False, shared=None, extra=""):
    """Jambe d'un combiné à cote juste : pays, heure et ligue ; le match ; l'icône du marché, le pari et sa cote 1xBet (ou la cote mini à exiger) ;
    la pastille et les chances (Kelly / 1xBet). `weak` / `shared` / `extra` : voir leg."""
    price = b(num(c["xodds"])) if c.get("xodds") else f"joue si ≥ {b(num(c['min_odds']))}"
    return "\n".join([_match_line(n, c, _shared_mark(c, shared)), f"{market_icon(c)} {esc(c['pick'])} · {price}", chances_line(c, weak, extra)])


def fair_combo(min_total, picks, prob, stake=None, stake_txt=None, title=None, coupon=None, when=None, retry=None, stale=False, weak=None, shared=None,
               note="", extra=None):
    """Message d'un combiné à cote juste : toujours clairement annoncé « à vérifier chez 1xBet », chaque jambe avec sa cote minimale ;
    la mise conseillée vient du budget du jour (`stake_txt`), sinon une ligne dit de régler le budget (jamais d'unités ; `stake`, ancienne
    mise indicative en unités, est ignoré). `title` : combinés « spécial tirs » (« 🎯 COMBINÉ TIRS CADRÉS »).
    `when` : heure « HH:MM » du relevé des cotes 1xBet (combiné réel), rappelée en bas du message ; `retry` : voir coupon_block ;
    `stale` : cotes du DERNIER relevé (1xBet injoignable à l'instant) : le titre et le pied le disent, avec l'heure `when` du relevé si on
    la connaît. `weak` / `shared` / `note` / `extra` : voir combo."""
    weak, extra = set(weak or ()), extra or {}
    real = all(c.get("xodds") for c in picks)          # toutes les cotes relevées chez 1xBet par Kelly : la cote totale est réelle
    tail = "COTES 1XBET" if real else "À VÉRIFIER CHEZ 1XBET"
    old = real and stale
    if old:                                          # jamais le titre « aux cotes 1xBet » sans dire de quand elles datent
        tail = "COTES 1XBET DU DERNIER RELEVÉ" + (f" ({when})" if when else "")
    name = title or ("💎 COMBINÉ" if real else "🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET")
    head, stake_foot = _header(f"{name} — {'cote' if real else 'cote mini'} {odds_txt(min_total)}" + (" · " + tail if old or (title and not real) else ""),
                               combo_chances(picks, prob, min_total, ev=False, mini=not real), stake_line(stake_txt, "" if real else "(si 1xBet paie la cote mini)"),
                               note, coupon)
    foot = i(f"🕒 Cotes du dernier relevé ({esc(when)}) : 1xBet ne répond pas, vérifie-les avant de valider." if old and when else
             "🕒 Cotes du dernier relevé : 1xBet ne répond pas, vérifie-les avant de valider." if old else
             f"🕒 Cotes 1xBet de {esc(when)} : vérifie-les avant de valider." if real and when else
             "🕒 Cotes 1xBet : vérifie-les avant de valider." if real else
             f"⚠️ Joue seulement si chaque pari atteint sa cote mini (au total, au moins {odds_txt(min_total)}).")
    legs = [fair_leg(k, c, c.get("key") in weak, shared, extra.get(c.get("key"), "")) for k, c in enumerate(picks, 1)]
    return Html(head + "\n\n" + "\n\n".join(legs) + _foot(stake_foot, bool(weak), _shared_ids(shared), coupon_block(coupon, retry) + "\n" + foot))
