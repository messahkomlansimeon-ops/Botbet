"""Tes tickets joués du jour et les matchs qu'ils partagent : un même match dans plusieurs tickets, c'est un seul pari raté qui les fait tous
tomber ensemble. Utilisé par :
  - « combi » (app.custom_combo, app._shared_note) : rien n'est retiré, le message signale les matchs déjà dans tes tickets joués ;
  - « 🔀 Un autre sans mes matchs » : aucun match de tes tickets joués ni du combiné affiché ;
  - « ✅ Je le joue » / « ✅ Je l'ai joué » : alerte si le ticket partage des matchs avec tes autres tickets du jour (overlap_note) ;
  - « 🧾 Mes paris » : les matchs dont dépendent plusieurs tickets, avec l'argent en jeu (summary_lines).
Un ticket = un pari noté joué (x["played"], pas « ❌ Pas joué ») du jour : combiné du matin (bets.json), pari du jour et paris 💰 (tracked.json),
combiné à vérifier ou sur mesure (fair_combos.json). Ne lève jamais."""
import math
import random
import sys

from . import fair, fmt, store, track

DEFAULT_P = 0.65                    # chances d'une jambe dont la proba est inconnue (ancienne ligne)
SIMS = 20000                        # tirages pour « chances qu'au moins un de tes tickets passe » (matchs supposés indépendants)


def _leg(id_, key, prob, home="", away=""):
    return {"id": id_, "key": key, "prob": prob if prob is not None else DEFAULT_P, "home": home, "away": away}


def tickets(day):
    """Tickets joués du jour `day` : [{"what", "name", "legs": [{id, key, prob, home, away}], "stake", "odds"}]."""
    from . import played
    day = str(day)[:10]
    out = []
    try:
        bets = store.load("bets.json", {"picks": [], "combos": []})
        rows = track.load()
        by_key = {r["key"]: r for r in rows if r.get("date") == day}
        for c in bets.get("combos", []):
            if c.get("date") == day and c.get("played") and not c.get("skipped"):
                legs = [_leg(k.split("|")[0], k, (by_key.get(k) or {}).get("prob"), (by_key.get(k) or {}).get("home", ""),
                             (by_key.get(k) or {}).get("away", "")) for k in c.get("keys") or []]
                out.append({"what": played.norm(fmt.tier_word(c["tier"])), "name": fmt.tier_word(c["tier"]),
                            "legs": legs, "stake": c["played"]["stake"], "odds": c["played"]["odds"]})
        singles = played.single_rows(rows, bets.get("combos", [])) + played.gap_rows(rows)
        for r in singles:
            if r.get("date") == day and r.get("played") and not r.get("skipped"):
                what = played.gap_what(r) if r.get("sharpgap") and r.get("gap_n") and r.get("single") is not True else "pari"
                out.append({"what": what, "name": r.get("pick") or "pari du jour", "stake": r["played"]["stake"], "odds": r["played"]["odds"],
                            "legs": [_leg(r.get("id") or r["key"].split("|")[0], r["key"], r.get("prob"), r.get("home", ""), r.get("away", ""))]})
        for c in fair.load_combos():
            if c.get("date") == day and c.get("played") and not c.get("skipped"):
                out.append({"what": f"v{c['tier']}", "name": played.custom_name(c) if played.is_custom(c) else f"combiné {c['tier']}",
                            "stake": c["played"]["stake"], "odds": c["played"]["odds"],
                            "legs": [_leg(l.get("id"), l.get("key"), l.get("prob"), l.get("home", ""), l.get("away", "")) for l in c.get("legs") or []]})
    except Exception as e:                          # noqa: BLE001 - jamais bloquant : sans tickets, rien ne change
        print(f"[tickets] lecture des tickets joués impossible : {type(e).__name__}: {e}", file=sys.stderr)
    return out


def played_ids(day):
    """Matchs de tes tickets joués du jour."""
    return {l["id"] for t in tickets(day) for l in t["legs"] if l.get("id")}


def names(legs, ids):
    """« Lorient – Paris FC, Brest – Angers » : les matchs `ids` vus dans `legs` (sans doublon, dans l'ordre)."""
    seen, out = set(), []
    for c in legs:
        if c.get("id") in ids and c["id"] not in seen:
            seen.add(c["id"])
            out.append(f"{c.get('home', '')} – {c.get('away', '')}".strip(" –") or str(c["id"]))
    return out


def chance_any(ts):
    """Chances qu'au moins un des tickets `ts` passe (tirages, chaque match indépendant, un même match partagé entre tickets) ; None sans ticket."""
    if not ts:
        return None
    probs = {}
    for t in ts:
        for l in t["legs"]:
            probs[l["key"]] = l["prob"]
    rnd = random.Random(7)
    hits = 0
    for _ in range(SIMS):
        ok = {k: rnd.random() < p for k, p in probs.items()}
        hits += any(all(ok[l["key"]] for l in t["legs"]) for t in ts)
    return hits / SIMS


def overlap_note(day, what):
    """Après la note du ticket `what` : « ⚠️ Ce ticket a 4 matchs en commun avec 5 de tes tickets du jour… » ; '' s'il n'en partage aucun."""
    try:
        ts = tickets(day)
        me = next((t for t in ts if t["what"] == what), None)
        if not me:
            return ""
        mine = {l["id"] for l in me["legs"]}
        others = [t for t in ts if t is not me and mine & {l["id"] for l in t["legs"]}]
        if not others:
            return ""
        shared = mine & {l["id"] for t in others for l in t["legs"]}
        alone = math.prod(l["prob"] for l in me["legs"])
        anyp = chance_any(ts)
        listed = names(me["legs"], shared)
        return (f"🔗 {len(shared)} match{'s' if len(shared) > 1 else ''} en commun avec {len(others)} autre{'s' if len(others) > 1 else ''} ticket"
                f"{'s' if len(others) > 1 else ''} ({', '.join(listed[:2])}{' +' + str(len(listed) - 2) if len(listed) > 2 else ''}) : s'ils ratent, tout tombe ensemble.\n"
                f"🎯 Ce ticket : {fmt.pct(alone)} · au moins un de tes {len(ts)} tickets : {fmt.pct(anyp)}")
    except Exception as e:                          # noqa: BLE001
        print(f"[tickets] alerte de matchs partagés ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return ""


def summary_lines(day, top=4):
    """Lignes « 🔗 Matchs partagés » de « 🧾 Mes paris » : les matchs dans au moins 2 tickets joués, l'argent qui en dépend, et les chances qu'au moins
    un ticket passe ; [] sinon."""
    try:
        ts = tickets(day)
        by = {}
        for t in ts:
            for l in {l["id"]: l for l in t["legs"] if l.get("id")}.values():
                e = by.setdefault(l["id"], {"leg": l, "n": 0, "stake": 0.0})
                e["n"] += 1
                e["stake"] += t["stake"] or 0
        shared = sorted((e for e in by.values() if e["n"] >= 2), key=lambda e: (-e["n"], -e["stake"]))
        if not shared:
            return []
        from . import money
        lines = [f"🔗 Matchs dans plusieurs tickets (au moins un ticket gagnant : {fmt.pct(chance_any(ts))}) :"]
        for e in shared[:top]:
            l = e["leg"]
            lines.append(f"• {l.get('home', '')} – {l.get('away', '')} : {e['n']} tickets · {money.money(e['stake'])}")
        if len(shared) > top:
            lines.append(f"• +{len(shared) - top} autre{'s' if len(shared) - top > 1 else ''}")
        return lines
    except Exception as e:                          # noqa: BLE001
        print(f"[tickets] résumé des matchs partagés ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return []
