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
# mots de 1xBet (« Tirs vers le but », « Tirs cadrés ») : le pari se retrouve directement chez le bookmaker
TEAM_UNITS = {"shots": "tirs vers le but", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu"}


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


def _match_line(n, c):
    """« <b>1. PSG – Lyon</b>  <i>14h00 · Ligue 1</i> » : le match d'une jambe, son heure et sa ligue."""
    when = " · ".join(x for x in (hhmm(c.get("start")), esc(c.get("league") or "")) if x)
    return b(f"{'' if n is None else f'{n}. '}{event_name(c)}") + (f"  {i(when)}" if when else "")


def leg(n, c, reason=None):
    """Bloc d'une jambe : le match, le pari et sa cote, la raison (une information par ligne)."""
    note = best_note(c)
    lines = [_match_line(n, c), f"👉 {esc(c['pick'])} — {b(odds_txt(c['odds']))}" + (f" {i('(' + note + ')')}" if note else "")]
    why = why_line(c, reason)
    if why:
        lines.append(f"💡 {i(esc(why))}")
    return "\n".join(lines)


def expect(prob, odds_total):
    """« espérance +6 % » : ce que rend la mise en moyenne à cette cote, d'après les probabilités de Kelly."""
    return f"espérance {signed((prob * odds_total - 1) * 100, ' %')}"


def played_hint(hint):
    return f"\n\n{i(f'Joué chez 1xBet ? Note-le : {esc(hint)}')}" if hint else ""


def combo(title, picks, odds_total, prob, stake, reasons=None, hint=None, stake_txt=None):
    """Message d'un combiné : titre et cote ; chances, espérance et mise sur deux lignes ; puis les jambes, séparées par une ligne
    vide. `stake_txt` : la mise prise sur le budget du jour (« 1 500 FCFA »), à la place de la mise en unités."""
    reasons = reasons or {}
    head = [b(f"{esc(tier_name(title))} — cote {odds_txt(odds_total)}"),
            f"🎯 {len(picks)} paris · {pct(prob)} de chances",
            f"📈 {expect(prob, odds_total).capitalize()} · 💰 " + (f"mise : {b(esc(stake_txt))}" if stake_txt else
                                                                   f"mise conseillée : {b(units(stake))}")]
    legs = [leg(k, c, reasons.get(c["key"])) for k, c in enumerate(picks, 1)]
    return Html("\n".join(head) + "\n\n" + "\n\n".join(legs) + played_hint(hint))


def single(c, reason=None, title="⭐ PARI DU JOUR", hint=None, stake_txt=None):
    """Pari du jour : une seule jambe (et sa mise sur le budget du jour, `stake_txt`)."""
    head = [b(f"{title} — cote {odds_txt(c['odds'])}"), f"🎯 {pct(c['prob'])} de chances" + (f" · 💰 mise : {b(esc(stake_txt))}" if stake_txt else "")]
    return Html("\n".join(head) + "\n\n" + leg(None, c, reason) + played_hint(hint))


def pronos(items, limit=10):
    """Pronostics sans cote de bookmaker, regroupés par match : le match, puis un pari par ligne avec sa cote minimale."""
    shown = items[:limit]
    lines = [b("🎲 Pronostics Kelly sans cote"),
             i("À comparer chez 1xBet : joue seulement si la cote est au moins celle indiquée.")]
    by = {}
    for c in shown:
        by.setdefault(c["id"], []).append(c)
    for cs in by.values():
        lines.append("")
        lines.append(_match_line(None, cs[0]))
        lines += [f"👉 {esc(c['pick'])} — mini {b(num(c['min_odds']))}" for c in cs]
    if len(items) > len(shown):
        lines += ["", i(f"+ {len(items) - len(shown)} autres : écris un nom d'équipe pour voir un match")]
    return Html("\n".join(lines))


def fair_leg(n, c):
    """Jambe d'un combiné à cote juste : match, pari, cote minimale à exiger chez 1xBet et chances selon Kelly."""
    chances = i(f"({pct(c['prob'])} de chances)")
    return "\n".join([_match_line(n, c), f"👉 {esc(c['pick'])}", f"✔️ Joue si 1xBet paie au moins {b(num(c['min_odds']))}  {chances}"])


def fair_combo(min_total, picks, prob, stake, hint=None, stake_txt=None, title=None):
    """Message d'un combiné à cote juste : toujours clairement annoncé « à vérifier chez 1xBet », jamais de mise de bankroll
    (mise indicative seulement), chaque jambe avec sa cote minimale. `title` : combinés « spécial tirs » (« 🎯 COMBINÉ TIRS CADRÉS »)."""
    head = [b(f"{title} — À VÉRIFIER CHEZ 1XBET" if title else "🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET"),
            f"🎯 Cote mini {b(odds_txt(min_total))} · {len(picks)} paris · {pct(prob)} de chances",
            (f"💰 Mise : {b(esc(stake_txt))} {i('(seulement si 1xBet paie la cote mini)')}" if stake_txt else
             f"💰 Mise indicative : {units(stake)} au plus {i('(pas calculée sur ta bankroll)')}")]
    foot = i("⚠️ Kelly n'a pas vu les cotes de 1xBet : joue seulement si CHAQUE pari atteint sa cote mini "
             f"(la cote totale sera alors d'au moins {odds_txt(min_total)}), sinon passe ton chemin.")
    return Html("\n".join(head) + "\n\n" + "\n\n".join(fair_leg(k, c) for k, c in enumerate(picks, 1)) + "\n\n" + foot
                + played_hint(hint))
