"""Fenêtre des stats : les paris tirs / tirs cadrés / fautes / touches / hors-jeu, chiffrés chez 1xBet quand 1xBet les ouvre.

1xBet n'ouvre les sous-matchs « statistiques » (types 95 tirs, 12 tirs cadrés, 14 fautes, 55 touches, 13 hors-jeu) que quelques heures avant le coup d'envoi et
les ferme ~30 minutes avant. Au relevé de 04:00 ils sont presque tous fermés : les pronostics stats du matin (fair.make) n'ont donc pas de cote 1xBet (c["xnone"]),
et, dès que 1xBet a chiffré la journée, combos.fair_legs les écarte. Ce module les rattrape dans la journée, au passage horaire (`bot.py --track`, :05 de 05h à 23h) :

  1. SÉLECTION  les matchs du jour Kelly qui commencent dans la fenêtre [C.STATS_WINDOW_MIN_MIN min ; C.STATS_WINDOW_MAX_H h] et dont un pronostic stats n'a pas de
                cote 1xBet fraîche (c["xat"], C.STATS_WINDOW_FRESH_MIN min), connus de 1xBet (c["xgid"]), pas déjà jambe d'un combiné du jour (app._sent_keys),
                dans la limite de C.STATS_WINDOW_MAX_MATCHES par passage, C.STATS_WINDOW_DAY_READS par jour et C.STATS_WINDOW_TRIES relectures par match ; ceux qui ont le
                plus de pronostics stats d'abord. Jamais au-delà de C.STATS_WINDOW_MONTH_PCT % du plafond mensuel de données (le matin passe avant).
  2. RELECTURE  en direct (xbet.fetch_day live) de ces seuls matchs, `wanted` = leurs marchés stats seulement : le match (pour trouver les sous-matchs ouverts) puis un
                sous-match par famille stats demandée (≈ 6,4 Ko la requête), C.STATS_WINDOW_SECONDS secondes au plus, session / proxy / plafonds habituels de xbet.
                Les pronostics sont remis à la cote 1xBet (xprice.apply sur ce sous-ensemble : mêmes règles qu'au relevé du matin, familles de l'ensemble inactives = règle
                du modèle) ; les avis de l'ensemble sont journalisés (app.log_opinions).
  3. FUSION     dans data/today.json (rechargé : rien d'écrit entre-temps n'est perdu). Les pronostics stats portent alors c["xodds"] / c["xref"] : « combi N »
                (combos.fair_custom) les utilise, la vérification en direct (xprice.live_check) relit leurs sous-matchs.
  4. COMBINÉ    UN « 🧮 COMBINÉ STATS » par passage au plus et C.STATS_WINDOW_MAX_COMBOS par jour (palier « ts5w1 », « ts5w2 » : combos.window_tier), cote 5 à 7, un
                pari par match, mêmes règles de réutilisation que les combinés du matin (un seul Usage avec eux), code coupon, mise sur le budget du jour, boutons
                habituels ; suivi dans fair_combos.json comme un combiné du matin (règlement, « Mes paris », /joue vts5w1). Jamais deux fois les mêmes jambes.
  5. 💎         les nouveaux paris stats jouables (un par match, une seule fois par match et par jour, C.STATS_WINDOW_ANNOUNCE par message, C.STATS_WINDOW_DAY_ANNOUNCE par jour) dans
                UN message « 🧮 Stats ouvertes chez 1xBet ».

Verrou des données : en mode `bot.py --track --own-lock` (KELLY_OWN_LOCK) le verrou n'est pris que pour la sélection puis la fusion / les envois (quelques
millisecondes chacun) ; les requêtes 1xBet (relecture, code coupon) se font SANS verrou. Sans ce mode (unité systemd sous `flock`) le verrou est celui du processus :
aucun verrou n'est repris ici (inter-blocage). Le suivi du jour (relectures faites, combinés et 💎 annoncés) est dans data/stats_window.json.
À la main : `python bot.py --stats-window` (même passage, verrou pris par lui-même sauf --no-lock).
Ne lève jamais : un passage en échec ne casse pas le passage horaire."""
import contextlib
import copy
import os
import sys
import time
from datetime import datetime, timedelta

from . import combos as CB, config as C, fair, fmt, money, store, telegram, xprice
from .sources import fmmatch, xbet

FILE = "stats_window.json"          # {"day", "reads": {id: {n, miss, at, priced}}, "combos": [{tier, keys, at, coupon}], "announced": [id|marché], "passes", "bytes", "requests"}
KB_PER_REQUEST = 6.4                # taille mesurée d'une réponse 1xBet (gameEvents marketType=2, compressée)


def _app():
    from . import app                # import tardif : app importe ce module
    return app


def locked():
    """Verrou des données pour une lecture-modification-écriture, seulement en mode `--own-lock` (comme fmmatch._locked) ; sinon le `flock` de l'unité systemd
    tient déjà le verrou pour tout le processus et en reprendre un ici bloquerait."""
    if not os.environ.get(fmmatch.OWN_LOCK_ENV):
        return contextlib.nullcontext()
    return store.data_lock(wait=C.LOCK_WAIT_MIN * 60)


# ---------- outils purs ----------

def is_stat(c):
    """Pronostic d'un marché « stats » (tirs, tirs cadrés, fautes, touches, hors-jeu)."""
    return str(c.get("market") or "").startswith(C.STATS_WINDOW_PREFIX)


def _ko(c):
    return datetime.fromisoformat(c["commence"].replace("Z", "+00:00"))


def in_window(c, now):
    """Le match du pronostic `c` commence dans la fenêtre [C.STATS_WINDOW_MIN_MIN min ; C.STATS_WINDOW_MAX_H h] après `now`."""
    try:
        ko = _ko(c)
    except (KeyError, ValueError, AttributeError, TypeError):
        return False
    return now + timedelta(minutes=C.STATS_WINDOW_MIN_MIN) <= ko <= now + timedelta(hours=C.STATS_WINDOW_MAX_H)


def age_min(c, now):
    """Minutes écoulées depuis la lecture 1xBet du pronostic (c["xat"], « HH:MM » UTC) ; None si inconnue. Une heure « dans le futur » est celle de la veille."""
    try:
        h, m = str(c.get("xat")).split(":")
        d = (now.hour * 60 + now.minute) - (int(h) * 60 + int(m))
    except (ValueError, AttributeError, TypeError):
        return None
    return d + 1440 if d < 0 else d


def fresh(c, now):
    """Le pronostic a une cote 1xBet lue il y a moins de C.STATS_WINDOW_FRESH_MIN minutes."""
    age = age_min(c, now)
    return bool(c.get("xodds")) and age is not None and age <= C.STATS_WINDOW_FRESH_MIN


def window_ids(pronos, now):
    """Ids des matchs qui ont un pronostic stats et commencent dans la fenêtre (le relevé de 15:00 / manuel les relit avant les autres)."""
    return {c["id"] for c in pronos if is_stat(c) and in_window(c, now)}


def load(day):
    """Suivi du jour (remis à zéro quand le jour change)."""
    d = store.load(FILE, {})
    d = d if isinstance(d, dict) and d.get("day") == str(day) else {"day": str(day)}
    for k, v in (("reads", {}), ("combos", []), ("announced", []), ("passes", 0), ("bytes", 0), ("requests", 0)):
        d.setdefault(k, v)
    return d


def used_reads(doc):
    return sum(int(r.get("n", 0)) + int(r.get("miss", 0)) for r in doc["reads"].values())


def month_ok():
    """Le plafond mensuel de données 1xBet (C.XBET_MONTH_MB) laisse de la place à la fenêtre (C.STATS_WINDOW_MONTH_PCT %)."""
    return xbet.usage()["bytes"] < C.STATS_WINDOW_MONTH_PCT / 100 * C.XBET_MONTH_MB * 1e6


def select(pronos, now, doc, protected=frozenset()):
    """Pronostics stats à relire ce passage : [(id du match, [pronostics])], les matchs qui ont le plus de pronostics stats d'abord, puis le coup d'envoi le plus proche.
    Un match n'est pas relu s'il n'est pas connu de 1xBet (pas de c["xgid"]), s'il est hors fenêtre, si tous ses pronostics stats ont une cote fraîche ou sont jambes d'un
    combiné du jour (`protected` : clés), s'il a déjà été relu C.STATS_WINDOW_TRIES fois ou n'a pas répondu C.STATS_WINDOW_MISSES fois aujourd'hui, ou s'il a été relu il y a
    moins de C.STATS_WINDOW_RETRY_MIN min. Au plus C.STATS_WINDOW_MAX_MATCHES matchs, et pas plus que ce qu'il reste de C.STATS_WINDOW_DAY_READS aujourd'hui."""
    by = {}
    for c in pronos:
        if is_stat(c) and c.get("xgid") and c.get("key") not in protected and in_window(c, now) and not fresh(c, now):
            by.setdefault(c["id"], []).append(c)
    out = []
    for mid, cs in by.items():
        r = doc["reads"].get(mid) or {}
        if r.get("n", 0) >= C.STATS_WINDOW_TRIES or r.get("miss", 0) >= C.STATS_WINDOW_MISSES:
            continue
        try:
            if r.get("at") and (now - datetime.fromisoformat(r["at"])).total_seconds() < C.STATS_WINDOW_RETRY_MIN * 60:
                continue
        except ValueError:
            pass
        out.append((mid, cs))
    out.sort(key=lambda it: (-len(it[1]), min(_ko(c) for c in it[1])))
    room = max(0, C.STATS_WINDOW_DAY_READS - used_reads(doc))
    return out[:max(0, min(C.STATS_WINDOW_MAX_MATCHES, room))]


def estimate_kb(matches, families):
    """Données (Ko) d'une relecture de `matches` matchs qui demandent chacun `families` familles stats ouvertes : une requête du match + une par famille."""
    return matches * (1 + families) * KB_PER_REQUEST


def morning_note(pronos):
    """Ligne du message du matin quand des pronostics stats existent mais n'ont pas encore de cote 1xBet (1xBet les ouvre tard) ; None sinon. Il faut que 1xBet ait chiffré
    la journée (sinon la raison n'est pas là) et que le pronostic soit sur un match connu de 1xBet."""
    if not (C.STATS_WINDOW_ENABLED and C.STATS_WINDOW_NOTE and C.XBET_ENABLED):
        return None
    pronos = list(pronos or [])
    if not any(c.get("xodds") for c in pronos):
        return None
    if not any(is_stat(c) and c.get("xgid") and not c.get("xodds") for c in pronos):
        return None
    return "🧮 Les paris tirs / fautes / touches arrivent quelques heures avant les matchs (1xBet les ouvre tard)."


# ---------- le passage ----------

def run(now=None, fetch=None, seconds=None):
    """Passage de la fenêtre des stats (voir la docstring du module). `fetch(events, deadline=, wanted=)` : injectable pour les tests (sinon xbet.fetch_day en
    direct). Ne lève jamais. -> {"selected", "read", "priced", "combo" (palier ou None), "announced"}."""
    rep = {"selected": 0, "read": 0, "priced": 0, "combo": None, "announced": 0}
    try:
        return _run(now or _app().now_utc(), fetch, seconds, rep)
    except store.LockTimeout:
        print("[stats] verrou des données occupé : fenêtre des stats reportée au prochain passage", file=sys.stderr)
    except Exception as e:                          # noqa: BLE001 - facultatif : ne casse jamais le passage horaire
        print(f"[stats] fenêtre des stats ignorée : {type(e).__name__}: {e}", file=sys.stderr)
    return rep


def _today(app, now):
    today = store.load("today.json", {})
    if not isinstance(today, dict) or not app._current_day(today, now) or not today.get("pronos"):
        return None
    return today


def _run(now, fetch, seconds, rep):
    app = _app()
    if not (C.STATS_WINDOW_ENABLED and C.XBET_ENABLED):
        return rep
    hhmm = now.strftime("%H:%M")
    got, spent, plan = set(), (0, 0), None
    # 1. sélection (sous le verrou en mode --own-lock) ; rien à relire : la fusion, les annonces et le combiné se décident dans ce même passage du verrou
    with locked():
        today = _today(app, now)
        if today is None:
            print("[stats] pas de pronostics du jour Kelly en cours : fenêtre des stats sautée", file=sys.stderr)
            return rep
        day = today["day"]
        picked = select(today["pronos"], now, load(day), app._sent_keys(day)) if month_ok() else []
        jobs = [(c["key"], copy.deepcopy(c)) for _, cs in picked for c in cs]
        rep["selected"] = len(picked)
        if not jobs:
            plan = _settle(app, now, hhmm, day, picked, jobs, got, spent, rep)
    # 2. relecture 1xBet SANS verrou
    if jobs:
        limit = time.monotonic() + (seconds or C.STATS_WINDOW_SECONDS)

        def run_fetch(events, deadline=None, wanted=None):
            deadline = min(deadline, limit) if deadline else limit
            found = fetch(events, deadline=deadline, wanted=wanted) if fetch else xbet.fetch_day(events, deadline=deadline, wanted=wanted, live=True)
            got.update(found)
            return found
        u0 = xbet.usage()
        xprice.apply([c for _, c in jobs], fetch=run_fetch)
        u1 = xbet.usage()
        spent = (max(0, u1["bytes"] - u0["bytes"]), max(0, u1["requests"] - u0["requests"]))
        for _, c in jobs:                           # chaque pronostic garde l'heure de SA lecture
            if c["id"] in got and c.get("xodds"):
                c["xat"] = hhmm
            elif c["id"] in got:
                c.pop("xat", None)
        # 3. fusion + suivi + annonces (sous le verrou)
        with locked():
            plan = _settle(app, now, hhmm, day, picked, jobs, got, spent, rep)
        print(f"[stats] fenêtre : {len(picked)} match(s) à relire, {rep['read']} relu(s), {rep['priced']} pronostic(s) stats remis aux cotes 1xBet, "
              f"{rep['announced']} 💎 annoncé(s), ≈ {spent[0] / 1e3:.0f} Ko via le proxy ({spent[1]} requête(s))", file=sys.stderr)
    # 4. combiné (code coupon SANS verrou, enregistrement + envoi sous le verrou)
    if plan:
        rep["combo"] = send_combo(app, plan, day, now)
    return rep


def _settle(app, now, hhmm, day, picked, jobs, got, spent, rep):
    """Sous le verrou : today.json et le suivi du jour sont RECHARGÉS, les pronostics relus (`jobs` : [(clé d'origine, copie remise à la cote)], matchs lus : `got`)
    y sont fusionnés, puis les nouveaux 💎 sont annoncés et le prochain combiné décidé. Un pronostic devenu jambe d'un combiné entre-temps (app._sent_keys) garde ses
    cotes. -> le plan du combiné (plan_combo) ou None."""
    today = _today(app, now)
    if today is None or today["day"] != day:
        print("[stats] le jour Kelly a changé pendant la relecture : rien d'écrit", file=sys.stderr)
        return None
    doc = load(day)
    protected = app._sent_keys(day)
    if "announced" not in today:                    # premier relevé du jour : les 💎 du matin sont déjà connus (comme run_xbet_refresh)
        soon = now + timedelta(minutes=C.XBET_SOON_MIN)
        today["announced"] = [app._slot(c) for c in CB.best_per_match(
            [c for c in today["pronos"] if (c.get("commence") or "9")[:16] > soon.isoformat()[:16]]) if c.get("xodds")]
    changed = []
    if jobs:
        index = {c["key"]: i for i, c in enumerate(today["pronos"])}
        for key, c in jobs:
            i = index.get(key)
            if c["id"] in got and i is not None and key not in protected:       # un combiné envoyé entre-temps garde ses jambes telles quelles
                today["pronos"][i] = c
                changed.append(c)
        for mid, cs in picked:
            r = doc["reads"].setdefault(mid, {})
            r["at"] = now.isoformat(timespec="seconds")
            if mid in got:
                r["n"] = r.get("n", 0) + 1
                r["priced"] = any(c.get("xodds") for _, c in jobs if c["id"] == mid)
            else:
                r["miss"] = r.get("miss", 0) + 1
        doc["passes"] += 1
        doc["bytes"] += spent[0]
        doc["requests"] += spent[1]
        rep["read"], rep["priced"] = len(got), sum(1 for c in changed if c.get("xodds"))
        if got:
            today["xbet_at"] = hhmm
    new = announce_list(today, doc, now)
    if new:
        slots = [app._slot(c) for c in new]
        today["announced"] += slots
        doc["announced"] += slots
        have = {(r.get("date"), r.get("id"), r.get("market")) for r in fair.load()}
        rec = [c for c in new if (day, c["id"], c["market"]) not in have]          # suivi : seulement de NOUVELLES lignes (jamais une ligne existante)
        if rec:
            fair.record(rec, day, shown=rec)
    if jobs or new:
        store.save("today.json", today)
        store.save(FILE, doc)
    if changed:
        app.log_opinions(changed, day, now)         # derniers avis de l'ensemble avant le match (journal d'apprentissage)
    if new:
        rep["announced"] = len(new)
        telegram.send(fmt.pronos(new, limit=C.STATS_WINDOW_ANNOUNCE, title="🧮 Stats ouvertes chez 1xBet", when=hhmm,
                                 intro="Paris tirs / fautes / touches / hors-jeu devenus jouables avec les cotes 1xBet du moment (1xBet les ouvre quelques heures "
                                       "avant le match et les ferme ~30 min avant). Vérifie la cote avant de valider."))
    return plan_combo(app, today, doc, fair.load_combos(), now)


def _value(c):
    """Valeur x proba d'un pronostic à cote 1xBet (le classement de xprice.choose / combos.best_per_match)."""
    return (c.get("xev") or 0) * c.get("prob", 0)


def announce_list(today, doc, now):
    """Nouveaux 💎 stats à annoncer : pronostics stats de la fenêtre, à cote 1xBet fraîche et viables (combos.viable : jamais sous la cote mini), le meilleur de chaque match
    (valeur x proba), pas déjà annoncés : un match qui a déjà eu un 💎 stats aujourd'hui n'en a pas un deuxième (ni une ligne qui change dans la journée, ni un autre marché
    stats du même match qui s'ouvre plus tard : « écris un nom d'équipe » les montre), dans la limite du jour."""
    if C.STATS_WINDOW_ANNOUNCE <= 0:
        return []
    seen = {str(s) for s in doc["announced"]} | {str(s) for s in today.get("announced") or []}
    done = {s.partition("|")[0] for s in seen if s.partition("|")[2].startswith(C.STATS_WINDOW_PREFIX)}      # matchs dont un 💎 stats a déjà été annoncé aujourd'hui
    room = C.STATS_WINDOW_DAY_ANNOUNCE - len(doc["announced"])
    best = {}
    for c in today["pronos"]:
        if not (is_stat(c) and in_window(c, now) and fresh(c, now) and CB.viable(c)):
            continue
        if _app()._slot(c) in seen or c["id"] in done:
            continue
        if c["id"] not in best or _value(c) > _value(best[c["id"]]):
            best[c["id"]] = c
    new = sorted(best.values(), key=lambda c: -_value(c))
    return new[:max(0, min(C.STATS_WINDOW_ANNOUNCE, room))]


def plan_combo(app, today, doc, rows, now):
    """Prochain « 🧮 COMBINÉ STATS » de la fenêtre : {"tier", "legs", "total", "prob", "when", "keys"} ou None (plafond du jour atteint, pas assez de jambes à cote fraîche, mêmes
    jambes qu'un combiné déjà envoyé). Jambes : pronostics stats de la fenêtre à cote 1xBet fraîche ET référence de pari (pour le code coupon), réutilisation des matchs
    selon les règles du matin (Usage de tous les combinés du jour). Fourchette de cote C.STATS_WINDOW_COMBO, un pari par match."""
    day = today["day"]
    if C.STATS_WINDOW_MAX_COMBOS <= 0 or len(doc["combos"]) >= C.STATS_WINDOW_MAX_COMBOS:
        return None
    used = {c["tier"] for c in doc["combos"]} | {str(r["tier"]) for r in rows if r.get("date") == day}
    tier = next((CB.window_tier(n) for n in range(1, C.STATS_WINDOW_MAX_COMBOS + 1) if CB.window_tier(n) not in used), None)
    if tier is None:
        return None
    usage = app._others_usage(rows, None, day, app._load_updates(day))
    combo = CB.fair_stats_window(app.upcoming(today["pronos"], now), usage=usage, accept=lambda c: in_window(c, now) and fresh(c, now))
    if not combo:
        return None
    legs, total, prob = combo
    keys = {l["key"] for l in legs}
    if any(keys == set(c["keys"]) for c in doc["combos"]) or any(keys == set(r.get("keys") or []) for r in rows if r.get("date") == day):
        return None                                 # jamais deux fois les mêmes jambes
    ages = [(age_min(l, now), l.get("xat")) for l in legs]
    when = max(ages, key=lambda a: a[0] if a[0] is not None else 9999)[1] or now.strftime("%H:%M")      # l'heure de la cote la plus ancienne : honnête
    return {"tier": tier, "legs": legs, "total": total, "prob": prob, "when": when, "keys": sorted(keys)}


def send_combo(app, plan, day, now):
    """Enregistre (fair_combos.json, suivi du jour) puis envoie le combiné `plan` (plan_combo) : message « 🧮 COMBINÉ STATS » avec son code coupon, la mise du budget du jour et
    les boutons habituels (« ✅ Je l'ai joué », « 🎟️ Vérifier ce combiné », « 📋 Copier le code »). Le code est demandé à 1xBet SANS verrou ; l'enregistrement est écrit AVANT
    l'envoi (une relance ne le double pas). -> le palier envoyé, ou None."""
    legs, total, prob, tier, when = plan["legs"], plan["total"], plan["prob"], plan["tier"], plan["when"]
    coupon = app._coupon(legs)                      # réseau 1xBet (ticket sans mise) : hors verrou
    stake = app._budget_stake(prob, total)
    with locked():
        doc = load(day)
        rows = fair.load_combos()
        if (any(c["tier"] == tier for c in doc["combos"]) or any(r.get("date") == day and str(r["tier"]) == tier for r in rows)
                or len(doc["combos"]) >= C.STATS_WINDOW_MAX_COMBOS):
            print(f"[stats] combiné {tier} déjà enregistré : rien envoyé", file=sys.stderr)
            return None
        if not fair.record_combos([(tier, (legs, total, prob))], day, {tier: stake}, money.cur() if stake else None, coupons={tier: coupon}, at=when):
            return None
        doc["combos"].append({"tier": tier, "keys": plan["keys"], "at": now.strftime("%H:%M"), "coupon": coupon})
        store.save(FILE, doc)
        kos = sorted(_ko(l) for l in legs)
        span = f"de {kos[0]:%Hh%M} à {kos[-1]:%Hh%M}" if kos[0] != kos[-1] else f"à {kos[0]:%Hh%M}"
        head = (f"🧮 Les paris stats (tirs, fautes, touches...) viennent de s'ouvrir chez 1xBet pour des matchs {span} : "
                f"voici un combiné aux cotes de {when}.")
        msg = app._fair_reply((legs, total, prob), head, title=C.STATS_WINDOW_TITLE, stake_txt=app._stake_words(stake), when=when, code=coupon,
                              retry=app.RETRY_BUTTON)
        telegram.send(app.chat.with_played_button(msg, f"v{tier}", day, stake=stake, refresh=tier, code=coupon))
    print(f"[stats] combiné {tier} envoyé : cote {total:.2f}, {len(legs)} pari(s), code {'oui' if coupon else 'non'}", file=sys.stderr)
    return tier
