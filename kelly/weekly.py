"""🧠 Bilan d'apprentissage de la semaine, envoyé le premier matin de chaque semaine (lundi, ou le passage suivant si le PC était
éteint) : résultats des 7 derniers jours, cote finale battue ou non, fiabilité par marché et par championnat (et ce que Kelly a
changé à cause d'elle), ce que le modèle a appris, l'écart de cote de 1xBet. En mots simples ; rien quand il n'y a rien à dire.

data/weekly.json : {"week": "AAAA-Sss"} de la dernière semaine envoyée.
"""
from datetime import datetime, timedelta, timezone

from . import calib, config as C, fair, fmt, learn, money, played, settle, store, track

FILE = "weekly.json"
FAIR_GAP = 0.03            # écart réussite / annonce sous lequel une famille est « fiable »


def _week(now):
    y, w, _ = now.isocalendar()
    return f"{y}-S{w:02d}"


def due(now):
    return store.load(FILE, {}).get("week") != _week(now)


def mark(now):
    store.save(FILE, {"week": _week(now)})


def _pct(x):
    return f"{round(x * 100)} %"


def _family_lines(data):
    """Verdict par famille de marché : fiable, trop optimiste (et la correction appliquée), ou prudente."""
    out = []
    for fam, acc in sorted(((f, a) for f, a in data.items() if not f.startswith("_")), key=lambda kv: -kv[1][0]):
        n, w, s = acc
        if n < calib.SHOW_MIN:
            continue
        real, said = w / n, s / n
        label = calib.LABELS.get(fam, fam)
        if said - real > FAIR_GAP:
            k = calib.factor(next((m for m, f in calib.FAMILIES.items() if f == fam), f"team_{fam}_total"), data)
            fix = " → Kelly baisse ses probabilités et exige des cotes plus hautes" if k < 1 else " → correction dès 60 pronostics"
            out.append(f"⚠️ {label} : trop optimiste, {_pct(real)} réalisés pour {_pct(said)} annoncés ({n}){fix}")
        elif real - said > FAIR_GAP:
            out.append(f"👍 {label} : prudente, {_pct(real)} réalisés pour {_pct(said)} annoncés ({n})")
        else:
            out.append(f"✅ {label} : fiable, {_pct(real)} réalisés pour {_pct(said)} annoncés ({n})")
    return out


def message(now=None):
    """Le message de la semaine (Html), ou None s'il n'y a encore rien à dire."""
    now = now or datetime.now(timezone.utc)
    cut = str((now - timedelta(days=7)).date())
    bets, rows, fair_combos = store.load("bets.json", {"picks": [], "combos": []}), track.load(), fair.load_combos()
    blocks = []
    combos = [c for c in bets["combos"] if c.get("result") in ("win", "loss") and c["date"] >= cut]
    if combos:
        w = sum(1 for c in combos if c["result"] == "win")
        blocks.append([f"🎯 <b>Combinés</b> : {w} gagné{'s' if w > 1 else ''} sur {len(combos)}"])
    n, total, staked = money.result(bets, rows, fair_combos, days=7, today=now.date())
    if n:
        blocks.append([f"💰 <b>Tes paris notés</b> : {fmt.b(fmt.esc(money.signed(total)))} sur {n} pari{'s' if n > 1 else ''}"
                       + (f" ({round(total / staked * 100):+d} % de ce qui a été misé)" if staked else "")])
    pl = played.summary_lines(bets, rows, fair_combos, days=7, today=str(now.date()))
    pl = [x for x in pl if not (n and x.startswith("Joués chez 1xBet"))]
    if pl:
        blocks.append([f"💵 {fmt.esc(x)}" for x in pl])
    week = [r for r in rows if r.get("clv") is not None and (r.get("date") or "") >= cut]
    sent = [r for r in week if r.get("sent")]                      # envoyé ou fantôme : r["sent"] seul, la source de la cote finale n'y change rien
    ghost = [r for r in week if not r.get("sent")]
    clv = []
    free = lambda grp: sum(1 for r in grp if r.get("close_src"))    # noqa: E731
    note = lambda grp: f" ({free(grp)} mesurés gratuitement)" if free(grp) else ""    # noqa: E731
    if sent:
        clv.append(f"Paris envoyés : cote finale battue {sum(r['clv'] > 0 for r in sent)} fois sur {len(sent)}" + note(sent))
    if ghost:
        clv.append(f"Paris suivis en silence : {sum(r['clv'] > 0 for r in ghost)} sur {len(ghost)}" + note(ghost))
    if clv:
        blocks.append(["📉 <b>La cote finale</b> " + fmt.i("(le meilleur juge : le marché lui a-t-il donné raison ?)")]
                      + [f"• {x}" for x in clv])
    fl = settle.fair_line(fair.load(), 7, now.date())
    if fl:
        blocks.append([f"🎲 {fmt.esc(fl)}"])
    data = calib.load()
    fam = _family_lines(data)
    if fam:
        blocks.append(["📏 <b>Fiabilité par marché</b> " + fmt.i("(depuis le début)")] + [fmt.esc(x) for x in fam])
    lg = calib.league_rows(data)
    if len(lg) >= 2:
        best, worst = lg[0], lg[-1]
        blocks.append(["🏟 <b>Par championnat</b>",
                       f"• Le plus fiable : {fmt.esc(best[0])} ({_pct(best[2])} réalisés pour {_pct(best[3])} annoncés, {best[1]})",
                       f"• Le moins fiable : {fmt.esc(worst[0])} ({_pct(worst[2])} réalisés pour {_pct(worst[3])} annoncés, {worst[1]})"])
    if not blocks:
        return None
    model = learn.load()
    if model.get("adopted"):
        src = "tes paris" if model.get("oos_source") == "réel" else "l'historique des grands championnats"
        blocks.append([f"🧠 Le modèle qui classe les paris est appris sur {src} ({model.get('n', 0)} cotes finales réelles, "
                       f"{model.get('boot_n', 0)} historiques)."])
    head = [fmt.b("🧠 Ce que Kelly a appris cette semaine"), fmt.i(f"7 derniers jours, au {fmt.fr_day(now.date())}")]
    return fmt.Html("\n".join(head) + "\n\n" + "\n\n".join("\n".join(b) for b in blocks))
