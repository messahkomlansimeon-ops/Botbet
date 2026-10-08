"""Tour du monde : relevé gratuit des matchs du jour (cache data/monde.json), messages du matin, /monde et /ligues.

Le cache est écrit par le passage de 08:00 (run_daily) à partir de /events (gratuit) : une entrée par ligue en
saison avec ses matchs [domicile, extérieur, "HH:MM" UTC], les ligues payées en cotes réelles, les matchs africains
d'API-Football (sans cotes) et, les jours sans match, les prochains jours. Petit : une journée chargée tient dans
~15 Ko. Les passages horaires et les commandes ne le relisent que, sans appel réseau.
"""
from datetime import date

from . import catalogue, config as C, store

FILE = "monde.json"
MAX_EVENTS = 600           # matchs gardés dans le cache (les ligues les moins prioritaires sont coupées)
DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
FLAG_PAID = "💳"


def fr_date(d):
    d = d if isinstance(d, date) else date.fromisoformat(str(d))
    return f"{DAYS[d.weekday()]} {d.strftime('%d/%m')}"


def build(scan, day, now, paid=(), africa=(), ahead=None, spent=None, allowance=None):
    """Cache compact du jour à partir d'un relevé odds.scan_today (voir l'en-tête du module)."""
    counts = {k: len(v) for k, v in scan.get("leagues", {}).items()}
    leagues, total = {}, 0
    for key in catalogue.prioritise(counts, C.SPORTS):
        evs = sorted(scan["leagues"][key], key=lambda e: e["commence_time"])[:max(0, MAX_EVENTS - total)]
        if evs:
            leagues[key] = [[e["home_team"], e["away_team"], e["commence_time"][11:16]] for e in evs]
            total += len(evs)
    data = {"day": str(day), "at": now.strftime("%H:%M"), "brk": int(catalogue.is_break_day(counts)),
            "complete": int(scan.get("complete", True)), "act": list(scan.get("sports", [])),
            "paid": [k for k in paid if k in leagues], "leagues": leagues,
            "africa": [[f["lg"], f["home"], f["away"], _hhmm(f.get("ts"))] for f in africa][:40]}
    if scan.get("error"):
        data["error"] = scan["error"][:160]
    if spent is not None:
        data["spent"], data["allow"] = spent, allowance
    if ahead:
        data["nxt"] = {d: [sum(v.values()), [k for k, _ in sorted(v.items(), key=lambda kv: -kv[1])[:3]]]
                       for d, v in sorted(ahead.items())[:4]}
    return data


def _hhmm(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M") if ts else "?"


def save(data):
    store.save(FILE, data)


def load(day=None):
    """Cache du jour `day` (« AAAA-MM-JJ » ou date) ; None s'il manque ou s'il date d'un autre jour."""
    d = store.load(FILE, {})
    if not isinstance(d, dict) or not d.get("day") or (day is not None and d["day"] != str(day)):
        return None
    return d


def total(data):
    return sum(len(v) for v in data.get("leagues", {}).values()) + len(data.get("africa", []))


def groups(data):
    """[(région, [(clé, nom, matchs)])] dans l'ordre d'affichage : l'international en tête les jours de trêve."""
    by = {}
    for key, evs in data.get("leagues", {}).items():
        by.setdefault(catalogue.region(key), []).append((key, catalogue.name(key), evs))
    brk = bool(data.get("brk"))
    order = sorted(by, key=lambda r: catalogue.region_rank(r, brk))
    return [(r, by[r]) for r in order]


def _match(m):
    return f"{m[2]} {m[0]} - {m[1]}"


def _league_line(name, evs, paid, limit):
    shown = " · ".join(_match(m) for m in evs[:limit])
    more = f" (+{len(evs) - limit})" if len(evs) > limit else ""
    return f"   {FLAG_PAID if paid else '•'} {name} ({len(evs)}) : {shown}{more}"


def _africa_lines(data, limit=4):
    af = data.get("africa") or []
    if not af:
        return []
    lines = [f"{catalogue.region_label('afrique')} : {len(af)} match(s) (API-Football, sans cotes ; pronostic Kelly quand les stats des équipes sont prêtes)"]
    for lg in dict.fromkeys(m[0] for m in af):
        mine = [m for m in af if m[0] == lg]
        lines.append(f"   • {lg} ({len(mine)}) : " + " · ".join(f"{m[3]} {m[1]} - {m[2]}" for m in mine[:limit])
                     + (f" (+{len(mine) - limit})" if len(mine) > limit else ""))
    return lines


def _ahead(data):
    nxt = data.get("nxt") or {}
    if not nxt:
        return "Aucun match annoncé dans les 7 prochains jours sur les ligues suivies."
    return "Prochains matchs : " + " ; ".join(
        f"{fr_date(d)} : {n} match(s) ({', '.join(catalogue.name(k) for k in ks)})" for d, (n, ks) in nxt.items())


def tour_du_monde(data, n_prono=0, why_no_odds=None):
    """Message « 🌍 TOUR DU MONDE » : matchs du jour par région, affiches notables, ce qui est payé en cotes réelles et
    ce qui est analysé gratuitement. Jamais vide : même sans aucun match, il le dit avec les prochains jours."""
    d = date.fromisoformat(data["day"])
    head = f"🌍 TOUR DU MONDE — {fr_date(d)}/{d.year}"
    if data.get("error") and not data.get("leagues"):
        return (f"{head}\n⚠️ Impossible de lister les matchs du jour ({data['error']}). Kelly réessaiera au prochain passage.")
    n_tot = total(data)
    if not n_tot:
        return (f"{head}\n😴 Aucun match aujourd'hui dans les {len(data.get('act', []))} ligues en saison que Kelly suit "
                f"(Europe, Amériques, Asie, Afrique, sélections).\n{_ahead(data)}")
    paid = set(data.get("paid", []))
    n_lg = len(data["leagues"]) + len({m[0] for m in data.get("africa", [])})
    lines = [head, f"{n_tot} matchs dans {n_lg} ligues"
                   + (" — trêve internationale : pas de grande affiche en Europe, les sélections passent d'abord"
                      if data.get("brk") and "monde" in dict(groups(data)) else "")
             + (" (relevé partiel : délai atteint)" if not data.get("complete", 1) else "")]
    for r, leagues in groups(data):
        n = sum(len(e) for _, _, e in leagues)
        lines.append(f"{catalogue.region_label(r)} : {n} match(s) en {len(leagues)} ligue(s)")
        for key, name, evs in leagues[:3]:
            lines.append(_league_line(name, evs, key in paid, 3))
        if len(leagues) > 3:
            lines.append("   … " + ", ".join(nm for _, nm, _ in leagues[3:8]) + (" …" if len(leagues) > 8 else "")
                         + "  (/monde pour tout voir)")
    lines += _africa_lines(data)
    if paid:
        sp = f" ({data['spent']}/{data['allow']} crédits)" if data.get("spent") is not None and data.get("allow") is not None else ""
        lines.append(f"{FLAG_PAID} Cotes réelles aujourd'hui{sp} : " + ", ".join(catalogue.name(k) for k in data["leagues"] if k in paid)
                     + " → combinés ci-dessous")
    else:
        lines.append(f"{FLAG_PAID} Pas de cote payée aujourd'hui" + (f" — {why_no_odds}" if why_no_odds else "")
                     + ". Kelly analyse gratuitement.")
    if n_prono:
        lines.append(f"🎲 {n_prono} pronostic(s) Kelly sans cote : voir plus bas.")
    elif not paid:
        lines.append("ℹ️ Pas assez de stats gratuites pour chiffrer ces matchs (sélections, ligues sans historique) : "
                     "liste seulement. Regarde-les chez 1xBet, sans pronostic Kelly.")
    return "\n".join(lines)


def fmt_monde(data, arg=""):
    """Texte de /monde : tous les matchs du jour par région et par ligue (cache du matin). `arg` : région à filtrer
    (europe, ameriques, asie, afrique, international)."""
    if not data:
        return "Pas encore de relevé aujourd'hui (Kelly le fait à 08:00 UTC). Réessaie plus tard."
    want = catalogue.norm_region(arg) if arg else None
    d = date.fromisoformat(data["day"])
    paid = set(data.get("paid", []))
    lines = [f"🌍 MATCHS DU {fr_date(d)} ({total(data)} matchs, relevé de {data.get('at', '?')} UTC)"
             + (" — relevé partiel" if not data.get("complete", 1) else "")]
    for r, leagues in groups(data):
        if want and r != want:
            continue
        lines.append(f"\n{catalogue.region_label(r)}")
        for key, name, evs in leagues:
            lines.append(_league_line(name, evs, key in paid, 15))
    if not want or want == "afrique":
        af = _africa_lines(data, limit=15)
        lines += ([""] + af) if af else []
    if len(lines) == 1:
        lines.append("Aucun match dans cette région aujourd'hui." if want else "Aucun match aujourd'hui."
                     + f"\n{_ahead(data)}")
    lines.append(f"\n{FLAG_PAID} = cotes réelles payées aujourd'hui · /ligues pour le catalogue")
    return "\n".join(lines)


def fmt_ligues(data):
    """Texte de /ligues : le catalogue par région, avec les ligues payées en cotes réelles aujourd'hui (💳), celles qui
    jouent (⚽, analyse gratuite), celles en saison sans match (·) et celles hors saison (💤)."""
    paid = set((data or {}).get("paid", []))
    played = (data or {}).get("leagues", {})
    act = set((data or {}).get("act", []))
    lines = [f"📚 LIGUES SUIVIES ({len(C.LEAGUE_KEYS)} dans le catalogue, au plus {C.MAX_SPORTS} payées par jour)",
             f"{FLAG_PAID} payée en cotes réelles aujourd'hui · ⚽ joue aujourd'hui (analyse gratuite) · · en saison, pas de "
             "match · 💤 hors saison"]
    if not data:
        lines.append("(pas encore de relevé aujourd'hui : le statut apparaît après 08:00 UTC)")
    elif data.get("spent") is not None and data.get("allow") is not None:
        lines.append(f"Crédits du matin dépensés : {data['spent']} (allocation du jour : {data['allow']})")
    for r in catalogue.REGION_ORDER:
        mine = [(k, n) for k, n in C.CATALOGUE[r]]
        lines.append(f"\n{catalogue.region_label(r)}")
        for k, n in mine:
            if k.endswith("_"):                     # préfixe : les clés réellement jouées aujourd'hui
                keys = [x for x in played if x.startswith(k)]
                if not keys:
                    lines.append(f"   · {n} (préfixe {k}…)" if act else f"   · {n}")
                for x in keys:
                    lines.append(f"   {FLAG_PAID if x in paid else '⚽'} {catalogue.name(x)} ({len(played[x])})")
                continue
            if k in paid:
                mark = FLAG_PAID
            elif k in played:
                mark = "⚽"
            elif not data or k in act:
                mark = "·"
            else:
                mark = "💤"
            lines.append(f"   {mark} {n}" + (f" ({len(played[k])})" if k in played else ""))
    return "\n".join(lines)
