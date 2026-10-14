"""Variantes du constructeur de combinés, comparées en FANTÔME (lot du 14/10 ; pré-déclaration dans combos_ledger.json).

Chaque matin, après les combinés envoyés :
 1. le POOL complet de jambes de chaque voie (reels, fair, saison : toutes les jambes éligibles avec p_ens, cote, étiquette, espérance 1xBet) est gardé, compact, dans data/pool_<jour>.json, puis dans
    l'archive arch/pool/<jour>.json (90 jours) : c'est ce qui manquait pour tout rejeu ;
 2. les paliers de chaque voie sont reconstruits à partir de CE pool pour quatre variantes (objectif prob | ev x plafond de remplissage 1 | 0,5), chacune avec son Usage neuf. La variante « prob » sans
    plafond est la production, reconstruite dans le même cadre pour comparer à règles égales (la production réellement envoyée reste celle du matin). Les variantes sont journalisées dans
    data/combo_variants.json : jamais envoyées, jamais misées, hors Usage de production, hors fair_combos / fair_ghost / P&L / CLV par jambe existants.
Le bilan (bloc « variantes du constructeur ») joint chaque jambe au journal par sa clé (résultat, dernier prix 1xBet) : aucun règlement séparé."""
import contextlib
import sys
import time
from datetime import date, datetime, timedelta

from . import combos as CB, config as C, fmt, lanestats, store

POOL_PREFIX = "pool_"
POOL_ARCH = "arch/pool/"
POOL_ARCH_DAYS = 90
FILE = "combo_variants.json"
KEEP_DAYS = 120
VARIANTS = (("prob", "prob", 1.0), ("prob+cap", "prob", 0.5), ("ev", "ev", 1.0), ("ev+cap", "ev", 0.5))
PRODUCTION = "prob"
VOIES = ("reels", "fair", "saison")
POOL_COLS = ("key", "id", "market", "name", "point", "prob", "odds", "xodds", "strategy", "lane", "ev_x", "p0")
MIN_ROWS_RULE, MIN_DAYS_RULE = 150, 21
TIME_BUDGET = 90.0                      # secondes pour les quatre variantes ensemble


def _num(x, nd=4):
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def compact(c):
    """Une jambe du pool : [clé, match, marché, sélection, ligne, p_ens, cote, cote 1xBet, étiquette, voie, espérance 1xBet, proba avant ensemble] (POOL_COLS)."""
    x, p = c.get("xodds"), c.get("prob")
    ev_x = _num(p * x - 1) if x and p else None
    return [c.get("key"), c.get("id"), c.get("market"), c.get("name"), c.get("point"), _num(p), _num(c.get("odds")), _num(x), c.get("strategy"), c.get("lane"), ev_x, _num(c.get("p0"))]


def pools(picks, pronos, season_legs):
    """{voie: jambes éligibles} : exactement ce que les constructeurs du matin reçoivent."""
    out = {}
    try:
        out["reels"] = CB.make_pool(picks) if picks else []
    except Exception as e:                      # noqa: BLE001
        print(f"[variantes] pool des paris réels ignoré : {type(e).__name__}", file=sys.stderr)
    try:
        out["fair"] = CB.fair_legs(pronos) if pronos else []
    except Exception as e:                      # noqa: BLE001
        print(f"[variantes] pool à cote juste ignoré : {type(e).__name__}", file=sys.stderr)
    from . import lanes
    out["saison"] = [c for c in (season_legs or []) if lanes.usable(c)] if lanes.season_mode() == "on" else []      # comme season_plan_all : voie « on » et jambes jouables
    return out


def record_pool(day, by_voie):
    """data/pool_<jour>.json (compact) puis son exemplaire d'archive arch/pool/<jour>.json ; les pools de plus de 3 jours sortent de data/, les archives de plus de 90 jours sont supprimées. -> nombre de jambes."""
    day = str(day)[:10]
    doc = {"day": day, "cols": list(POOL_COLS), "voies": {v: [compact(c) for c in legs] for v, legs in by_voie.items()}}
    store.save(f"{POOL_PREFIX}{day}.json", doc, compact=True)
    store.save(f"{POOL_ARCH}{day}.json", doc, compact=True)
    try:
        today = date.fromisoformat(day)
        for name in store.names(POOL_PREFIX):
            d = name[len(POOL_PREFIX):-len(".json")]
            if len(d) == 10 and d < (today - timedelta(days=3)).isoformat():
                store.remove(name)
        for name in store.names(POOL_ARCH):
            d = name[len(POOL_ARCH):-len(".json")]
            if len(d) == 10 and d < (today - timedelta(days=POOL_ARCH_DAYS)).isoformat():
                store.remove(name)
    except (ValueError, OSError):
        pass
    return sum(len(v) for v in doc["voies"].values())


@contextlib.contextmanager
def variant_config(objective, share):
    old = C.COMBO_OBJECTIVE, C.COMBO_MAX_FILL_SHARE
    C.COMBO_OBJECTIVE, C.COMBO_MAX_FILL_SHARE = objective, share
    try:
        yield
    finally:
        C.COMBO_OBJECTIVE, C.COMBO_MAX_FILL_SHARE = old


def _leg(c):
    return {"key": c.get("key"), "id": c.get("id"), "strategy": c.get("strategy"), "lane": c.get("lane"), "prob": _num(c.get("prob")), "sval": c.get("sval"), "xodds": _num(c.get("xodds") or c.get("odds")),
            "market": c.get("market")}


def _row(day, voie, variant, tier, combo):
    base = {"date": day, "voie": voie, "variant": variant, "tier": str(tier)}
    if not combo:
        return base | {"built": False}
    legs, odds, prob = combo
    return base | {"built": True, "odds": _num(odds, 3), "prob": _num(prob), "legs": [_leg(c) for c in legs]}


def build(day, by_voie, deadline=None):
    """Lignes des quatre variantes (Usage neuf par variante ; reels, puis fair, puis saison comme en production). Un palier qui ne se construit pas est une ligne `built` = False."""
    rows = []
    for name, objective, share in VARIANTS:
        usage = CB.Usage()
        with variant_config(objective, share):
            plans = (("reels", lambda: CB.plan(by_voie.get("reels") or [], usage=usage) if by_voie.get("reels") else []),
                     ("fair", lambda: (CB.fair_plan(by_voie["fair"], usage=usage) + CB.fair_shots_plan(by_voie["fair"], usage=usage)) if by_voie.get("fair") else []),
                     ("saison", lambda: CB.season_plan(by_voie["saison"], usage=usage) if by_voie.get("saison") else []))
            for voie, fn in plans:
                if deadline and time.monotonic() > deadline:
                    return rows
                try:
                    rows += [_row(day, voie, name, tier, combo) for tier, combo in fn()]
                except Exception as e:                  # noqa: BLE001
                    print(f"[variantes] {voie} / {name} ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return rows


def run(day, picks, pronos, season_legs):
    """Le travail du matin : pool gardé, variantes reconstruites et journalisées (les lignes du même jour sont remplacées : une relance ne double rien). Ne lève jamais. -> (jambes du pool, lignes)."""
    day = str(day)[:10]
    try:
        by_voie = pools(picks, pronos, season_legs)
        n = record_pool(day, by_voie)
        rows = build(day, by_voie, time.monotonic() + TIME_BUDGET)
        old = [r for r in store.load(FILE, []) if isinstance(r, dict)]
        cut = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
        store.save(FILE, [r for r in old if r.get("date") != day and (r.get("date") or "") >= cut] + rows, compact=True)
        return n, len(rows)
    except Exception as e:                              # noqa: BLE001
        print(f"[variantes] ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0, 0


# ---------- mesure ----------

def _journal():
    from . import cupstats, seasonlane
    by_key = {r.get("key"): r for r in lanestats.journal_rows() if r.get("key")}
    by_key |= {r.get("key"): r for r in seasonlane.journal_rows() if r.get("key")}
    by_key |= {r.get("key"): r for r in cupstats.journal_rows() if r.get("key") and r.get("key") not in by_key}
    return by_key


def status(row, by_key):
    """'win' (toutes les jambes gagnées), 'loss' (une jambe perdue), None (jambes pas toutes réglées ou introuvables)."""
    results = [(by_key.get(l.get("key")) or {}).get("result") for l in row.get("legs") or []]
    if any(r == "loss" for r in results):
        return "loss"
    return "win" if results and all(r == "win" for r in results) else None


def _since(days, today):
    today = today or date.today()
    return (today - timedelta(days=days)).isoformat() if days else ""


def summarize(rows, by_key):
    """{variante: {built, tried, settled, wins, gain, fill, legs, clv (n, moyenne, demi-marge)}} sur des lignes."""
    out = {}
    for name, _, _ in VARIANTS:
        mine = [r for r in rows if r.get("variant") == name]
        built = [r for r in mine if r.get("built")]
        st = [(r, status(r, by_key)) for r in built]
        done = [(r, s) for r, s in st if s]
        legs = [l for r in built for l in r["legs"]]
        clvs, groups = [], []
        for r in built:
            for l in r["legs"]:
                row = by_key.get(l.get("key"))
                v = lanestats.x_clv(row, l.get("xodds")) if row and l.get("xodds") else None
                if v is not None:
                    clvs.append(v)
                    groups.append(l.get("id") or r["date"])
        n, m, h = lanestats.stats(clvs, groups)
        out[name] = {"tried": len(mine), "built": len(built), "settled": len(done), "wins": sum(1 for _, s in done if s == "win"),
                     "gain": sum((r["odds"] if s == "win" else 0.0) - 1 for r, s in done), "legs": len(legs),
                     "fill": sum(1 for l in legs if fmt.is_fill_leg(l)), "clv": (n, m, h)}
    return out


def verdict(summ, rows):
    """Règle pré-déclarée (combos_ledger.json) : -> {variante: texte}. Lisible seulement après MIN_ROWS_RULE paliers construits ou MIN_DAYS_RULE jours."""
    prod = summ[PRODUCTION]
    days = sorted({r["date"] for r in rows})
    age = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1 if days else 0
    out = {}
    for name, _, _ in VARIANTS:
        if name == PRODUCTION:
            continue
        s = summ[name]
        if s["built"] < MIN_ROWS_RULE and age < MIN_DAYS_RULE:
            out[name] = f"pas encore lisible ({s['built']}/{MIN_ROWS_RULE} paliers, {age}/{MIN_DAYS_RULE} jours)"
            continue
        (n1, m1, h1), (n0, m0, h0) = s["clv"], prod["clv"]
        if m1 is None or m0 is None or h1 is None or h0 is None:
            out[name] = "CLV non mesurable (trop peu de jambes mesurées)"
            continue
        diff, hw = m1 - m0, (h1 ** 2 + h0 ** 2) ** 0.5
        few = prod["built"] and s["built"] < 0.7 * prod["built"]
        ok = diff >= 0 and diff - hw >= 0 and not few
        out[name] = (f"REMPLACE la production (CLV {diff * 100:+.1f} % ± {hw * 100:.1f} %, paliers {s['built']} contre {prod['built']})" if ok else
                     f"ne remplace pas (CLV {diff * 100:+.1f} % ± {hw * 100:.1f} %" + (f", {s['built']} paliers contre {prod['built']} : plus de 30 % de moins" if few else "") + ")")
    return out


def _pc(x):
    return "—" if x is None else f"{x * 100:+.1f} %"


def lines(days=None, today=None):
    """Bloc « variantes du constructeur » du bilan : par variante (fantômes, hors P&L), par voie puis par palier. [] sans ligne."""
    cut = _since(days, today)
    rows = [r for r in store.load(FILE, []) if isinstance(r, dict) and (r.get("date") or "") >= cut]
    if not rows:
        return []
    by_key = _journal()
    out = ["fantômes comparés à pool égal (la production n'est pas modifiée) ; objectif prob | ev, plafond de remplissage 1 | 0,5 ; règle de remplacement : combos_ledger.json"]

    def block(label, sub):
        summ = summarize(sub, by_key)
        out.append(label)
        for name, _, _ in VARIANTS:
            s = summ[name]
            n, m, h = s["clv"]
            out.append(f"  {name}{' (production)' if name == PRODUCTION else ''} : {s['built']}/{s['tried']} paliers construits · "
                       + (f"réussite {s['wins']}/{s['settled']} ({round(s['wins'] / s['settled'] * 100)} %) · gain {s['gain']:+.1f} u" if s["settled"] else "aucun réglé")
                       + f" · CLV {_pc(m)}" + (f" ± {h * 100:.1f} %" if h is not None else "") + f" sur {n} jambes"
                       + (f" · remplissage {round(s['fill'] / s['legs'] * 100)} %" if s["legs"] else "") + ("" if n >= 30 else " (trop peu pour conclure)"))
        return summ

    total = block("toutes voies :", rows)
    for voie in VOIES:
        sub = [r for r in rows if r.get("voie") == voie]
        if sub:
            block(f"voie {voie} :", sub)
            for tier in sorted({r["tier"] for r in sub}):
                t = summarize([r for r in sub if r["tier"] == tier], by_key)
                out.append(f"    palier {tier} : " + " · ".join(f"{name} {t[name]['built']}/{t[name]['tried']}" + (f" ({t[name]['wins']}/{t[name]['settled']} gagnés)" if t[name]["settled"] else "")
                                                               for name, _, _ in VARIANTS))
    for name, text in verdict(total, rows).items():
        out.append(f"règle pré-déclarée — {name} : {text}")
    return out
