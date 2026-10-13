"""📈 Tendances : ce que Kelly sait déjà des équipes et des championnats, en clair, pour t'aider à choisir tes propres paris (aucun calcul ne change).

« tendances » (corners par défaut), « tendances cartons », « tendances tirs premier » : pour une famille de stats (corners, cartons, jaunes, tirs, tirs
cadrés, fautes, touches, hors-jeu, buts),
  - sans championnat : les championnats où il y en a le plus et le moins par match, puis les équipes qui en obtiennent et en concèdent le plus ;
  - avec un championnat (un bout de son nom ; ldc, c3…) : sa moyenne par match, puis ses équipes qui en obtiennent / concèdent le plus et le moins.
Sources : pour les 18 championnats aux CSV complets (football-data), le MODÈLE de Kelly lui-même (kelly/strength.py : force de chaque équipe pour
chaque stat, moyenne du championnat, avantage du terrain ; valeurs face à un adversaire moyen, domicile et extérieur mêlés) ; ailleurs, les profils
FotMob (moyennes des ~10 derniers matchs : kelly/sources/fmmatch.py). Une équipe sans match depuis STALE_DAYS jours ou avec moins de MIN_N matchs n'est pas
montrée. Une tendance ne fait pas un pari : Kelly la combine déjà avec l'adversaire, le championnat, l'arbitre et la cote."""
from datetime import date, timedelta

from . import calib, catalogue, config as C, fmt, played, strength
from .sources import footballdata as fd, fmmatch

STATS = ("corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides", "goals")
WORDS = {"corners": "corners", "corner": "corners", "cartons": "cards", "carton": "cards", "jaunes": "yellow", "tirs": "shots", "tir": "shots",
         "cadres": "sot", "fautes": "fouls", "faute": "fouls", "touches": "throws", "horsjeu": "offsides", "hors-jeu": "offsides", "buts": "goals",
         "but": "goals"}
UNIT = {"corners": "corners", "cards": "cartons", "yellow": "jaunes", "shots": "tirs", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches",
        "offsides": "hors-jeu", "goals": "buts"}
LEAGUE_ALIAS = {"ldc": "champions", "c1": "champions", "c3": "europa", "c4": "conference"}
MIN_N = 6                       # matchs d'une équipe pour la montrer
MIN_TEAMS = 6                   # équipes d'un championnat pour donner sa moyenne
STALE_DAYS = 75                 # sans match depuis : équipe oubliée (reléguée, profil ancien)
TOP = 5


def _fresh(last, today):
    try:
        return date.fromisoformat(str(last)[:10]) >= today - timedelta(days=STALE_DAYS)
    except ValueError:
        return False


def teams(today=None, model_for=None):
    """{équipe: {"sk": championnat, "n": matchs, stat: (pour, contre)}} : le modèle de Kelly pour les championnats football-data (équipes jouant cette
    saison), les profils FotMob ailleurs. `model_for(div)` : le modèle d'un championnat (strength.get par défaut ; tests)."""
    today = today or date.today()
    model_for = model_for or strength.get
    out, divs = {}, {}
    for name, p in ((fd.profiles() or {}).get("teams") or {}).items():
        if p.get("div") in fd.DIV_TO_SPORT and _fresh(p.get("last"), today):
            divs.setdefault(p["div"], []).append(name)
    for div, names in divs.items():
        try:
            model = model_for(div)
        except Exception:                           # noqa: BLE001 - un championnat illisible n'empêche pas les autres
            model = None
        if not model:
            continue
        for stat, st in (model.get("stats") or {}).items():
            if stat not in STATS or not st.get("m") or not isinstance(st.get("t"), dict):
                continue
            hf = (1 + float(st.get("h") or 1)) / 2                # domicile et extérieur mêlés
            for name in names:
                tp = st["t"].get(name)
                if tp and tp[3] >= MIN_N:
                    cur = out.setdefault(name, {"sk": fd.DIV_TO_SPORT[div], "n": tp[3]})
                    cur[stat] = (round(st["m"] * tp[0] * hf, 2), round(st["m"] * tp[1] * hf, 2))
    covered = {fd.DIV_TO_SPORT[d] for d in divs}
    for name, p in ((fmmatch.profiles() or {}).get("teams") or {}).items():
        a = p.get("all") or {}
        home = [sk for sk in (p.get("sk") or []) if not str(sk).startswith(C.CROSS_LEAGUE_PREFIXES)]     # son championnat, jamais une coupe d'Europe
        if a.get("n", 0) < MIN_N or not home or home[-1] in covered or not _fresh(p.get("last"), today) or name in out:
            continue
        out[name] = {"sk": home[-1], "n": a["n"]} | {s: (a[s + "_for"], a.get(s + "_against")) for s in STATS if a.get(s + "_for") is not None}
    return out


def _num(x):
    return f"{x:.1f}".replace(".", ",")


def report(words=(), today=None, data=None):
    """Le message « 📈 Tendances » (HTML) ; voir le haut du module."""
    from .telegram import Html
    stat, lg = "corners", []
    for w in words or ():
        n = played.norm(w)
        if n in WORDS:
            stat = WORDS[n]
        elif n:
            lg.append(LEAGUE_ALIAS.get(n, n))
    ts = data if data is not None else teams(today)
    rows = {name: t for name, t in ts.items() if t.get(stat) and t[stat][0] is not None}
    icon = fmt.market_icon({"market": calib.rep_market(stat)}) if stat != "goals" else "⚽"
    unit = UNIT[stat]
    head = f"📈 {fmt.b('Tendances')} · {icon} {fmt.esc(unit.capitalize())}"
    hint = fmt.i(fmt.esc("Autres : tendances cartons, jaunes, tirs, cadrés, fautes, touches, hors-jeu, buts · un championnat : tendances corners "
                         "premier · Une tendance ne fait pas un pari : Kelly la combine déjà avec l'adversaire, le championnat et la cote."))
    by_lg = {}
    for name, t in rows.items():
        by_lg.setdefault(t["sk"], []).append((name, t))
    if lg:
        keys = [sk for sk in by_lg if all(x in played.norm(catalogue.name(sk)) or x in sk for x in lg)]
        if not keys:
            return Html(head + "\n" + fmt.esc(f"Aucun championnat ne correspond à « {' '.join(words)} » (ou pas assez de matchs).") + "\n\n" + hint)
        sk = max(keys, key=lambda k: len(by_lg[k]))
        group = by_lg[sk]
        avg = sum(t[stat][0] + (t[stat][1] or 0) for _, t in group) / len(group)
        lines = [head + fmt.esc(f" · {catalogue.name(sk)}"), fmt.esc(f"{_num(avg)} {unit} par match en moyenne ({len(group)} équipes)"), ""]
        both = len(group) >= 2 * TOP                                   # assez d'équipes pour un haut ET un bas sans doublon
        for title, idx, rev in (("⬆️ En obtiennent le plus", 0, True), ("⬇️ En obtiennent le moins", 0, False),
                                ("🛡️ En concèdent le plus", 1, True), ("🧱 En concèdent le moins", 1, False)):
            if not rev and not both:
                continue
            best = sorted((x for x in group if x[1][stat][idx] is not None), key=lambda x: x[1][stat][idx], reverse=rev)[:TOP]
            if best:
                lines.append(fmt.b(title) + fmt.esc(" : " + " · ".join(f"{name} {_num(t[stat][idx])}" for name, t in best)))
        return Html("\n".join(lines + ["", hint]))
    leagues = [(sk, sum(t[stat][0] + (t[stat][1] or 0) for _, t in g) / len(g)) for sk, g in by_lg.items() if len(g) >= MIN_TEAMS]
    if not leagues:
        return Html(head + "\n" + fmt.esc("Pas encore assez de matchs dans les profils pour cette stat.") + "\n\n" + hint)
    leagues.sort(key=lambda x: -x[1])
    lines = [head + fmt.esc(f" · {len(rows)} équipes, {len(leagues)} championnats"), "",
             fmt.b("🏟 Le plus par match") + fmt.esc(" : " + " · ".join(f"{catalogue.name(sk)} {_num(v)}" for sk, v in leagues[:TOP])),
             fmt.b("🏟 Le moins par match") + fmt.esc(" : " + " · ".join(f"{catalogue.name(sk)} {_num(v)}" for sk, v in leagues[-3:][::-1])), ""]
    for title, idx in (("⬆️ En obtiennent le plus", 0), ("🛡️ En concèdent le plus", 1)):
        best = sorted((x for x in rows.items() if x[1][stat][idx] is not None), key=lambda x: x[1][stat][idx], reverse=True)[:TOP + 1]
        lines.append(fmt.b(title) + fmt.esc(" : " + " · ".join(f"{name} {_num(t[stat][idx])} ({catalogue.name(t['sk'])})" for name, t in best)))
    return Html("\n".join(lines + ["", hint]))
