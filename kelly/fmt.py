"""Mise en forme des messages Telegram : français lisible, virgule décimale, HTML échappé, pas de jargon.

Les messages sont du HTML Telegram (balises <b>, <i>) : tout texte venu de l'extérieur (équipes, joueurs, ligues) passe par
esc(). Les fonctions renvoient un `telegram.Html` (envoyé en parse_mode HTML, repli en texte brut si Telegram refuse).
"""
import html
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
    """0.314 -> « 31 % » ; sous 1 % une décimale (« 0,7 % »)."""
    v = p * 100
    return f"{v:.1f} %".replace(".", ",") if v < 1 else f"{round(v)} %"


def units(x):
    """Mise en unités : « 2 u », « 0,2 u », « 10,2 u »."""
    return f"{trim(x, 1 if abs(x) >= 1 else 2)} u"


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


TEAM_MARKET = re.compile(r"^team_(shots|sot|fouls|throws|offsides)_(total|home|away)$")
TEAM_UNITS = {"shots": "tirs", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu"}


def label(market, name, point, who, group):
    """« Plus de 2,5 buts », « Moins de 9,5 corners », « Total cartons : plus de 3,5 », « Mbappé : 2 tirs cadrés ou plus »,
    « Les deux équipes marquent : oui », « Double chance : Nul ou Flamengo », « Flamengo gagne »."""
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
    if piece.startswith("cote plus haute"):
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
    return ""


def best_note(c):
    """« jusqu'à 4,60 chez Coolbet » quand la meilleure cote dépasse nettement la cote typique, sinon ''."""
    best = c.get("best_odds")
    if best and best >= c["odds"] * (1 + C.BEST_NOTE_GAP):
        return f"jusqu'à {num(best)} chez {esc(c.get('book') or '?')}"
    return ""


def event_name(c):
    return f"{esc(c['home'])} – {esc(c['away'])}" if c.get("home") and c.get("away") else esc(c.get("event", ""))


def leg(n, c, reason=None):
    """Bloc d'une jambe : titre du match, pari et cote, raison."""
    note = best_note(c)
    pad = "" if n is None else "   "
    lines = [f"{'' if n is None else f'{n}. '}{b(event_name(c))} · {hhmm(c.get('start'))} · {esc(c.get('league', ''))}",
             f"{pad}{esc(c['pick'])} — {b(odds_txt(c['odds']))}" + (f" {i('(' + note + ')')}" if note else "")]
    why = why_line(c, reason)
    if why:
        lines.append(f"{pad}↳ {esc(why)}")
    return "\n".join(lines)


def expect(prob, odds_total):
    """« espérance +6 % » : ce que rend la mise en moyenne à cette cote, d'après les probabilités de Kelly."""
    return f"espérance {signed((prob * odds_total - 1) * 100, ' %')}"


def played_hint(hint):
    return f"\n\n{i(f'Joué chez 1xBet ? Note-le : {esc(hint)}')}" if hint else ""


def combo(title, picks, odds_total, prob, stake, reasons=None, hint=None):
    """Message d'un combiné : titre et cote, chances, espérance et mise, puis les jambes (ligne vide entre deux) et la commande
    /joue pour le noter."""
    reasons = reasons or {}
    head = [b(f"{esc(tier_name(title))} — cote {odds_txt(odds_total)}"),
            i(f"{len(picks)} paris · {pct(prob)} de chances · {expect(prob, odds_total)} · mise conseillée : {units(stake)}")]
    legs = [leg(k, c, reasons.get(c["key"])) for k, c in enumerate(picks, 1)]
    return Html("\n".join(head) + "\n\n" + "\n\n".join(legs) + played_hint(hint))


def single(c, reason=None, title="⭐ PARI DU JOUR", hint=None):
    """Pari du jour : une seule jambe."""
    head = [b(f"{title} — cote {odds_txt(c['odds'])}"), i(f"{pct(c['prob'])} de chances")]
    return Html("\n".join(head) + "\n\n" + leg(None, c, reason) + played_hint(hint))


def pronos(items, limit=10):
    """Bloc des pronostics sans cote de bookmaker : titre clair, une ligne par pronostic."""
    lines = [b("🎲 Pronostics sans cote (à vérifier chez 1xBet)"), ""]
    seen, shown = [], items[:limit]
    for c in shown:
        if c["id"] not in seen:
            seen.append(c["id"])
        lines.append(f"{esc(c['home'])} – {esc(c['away'])} : {esc(c['pick'])} · joue si la cote est au moins "
                     f"{b(num(c['min_odds']))}")
    if len(items) > len(shown):
        lines.append(i(f"+ {len(items) - len(shown)} autres : /match <équipe> pour un match précis"))
    lines.append(i("Kelly donne sa probabilité, pas une cote : si le bookmaker paie moins, passe ton chemin."))
    return Html("\n".join(lines))


def fair_leg(n, c):
    """Jambe d'un combiné à cote juste : match, pari, cote minimale à exiger chez 1xBet et chances selon Kelly."""
    return "\n".join([f"{n}. {b(event_name(c))} · {hhmm(c.get('start'))} · {esc(c.get('league', ''))}",
                      f"   {esc(c['pick'])} · joue si la cote est au moins {b(num(c['min_odds']))}",
                      f"   ↳ Kelly lui donne {pct(c['prob'])} de chances"])


def fair_combo(min_total, picks, prob, stake, hint=None):
    """Message d'un combiné à cote juste : toujours clairement annoncé « à vérifier chez 1xBet », jamais de mise de bankroll
    (mise indicative seulement), chaque jambe avec sa cote minimale."""
    head = [b(f"🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET — cote mini {odds_txt(min_total)}"),
            i(f"{len(picks)} paris · {pct(prob)} de chances · mise indicative : {units(stake)} au plus (pas calculée sur ta bankroll)"),
            i("Kelly n'a pas vu les cotes de 1xBet : ces cotes sont ses minimums. Joue seulement si 1xBet paie au moins la cote "
              "indiquée sur CHAQUE pari (la cote totale sera alors d'au moins " + odds_txt(min_total) + "), sinon passe ton chemin.")]
    return Html("\n".join(head) + "\n\n" + "\n\n".join(fair_leg(k, c) for k, c in enumerate(picks, 1)) + played_hint(hint))
