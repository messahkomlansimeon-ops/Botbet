"""Alertes centrées sur 1xBet, le seul bookmaker où l'utilisateur joue : chaque alerte dit quoi faire, avec la cote 1xBet et l'heure de sa lecture.

  💰 1xBet est en retard : le MARCHÉ (clôture The Odds API, passage horaire run_track) bouge fort sur un pari envoyé, mais 1xBet paie encore
     un prix qui a de la valeur face à la nouvelle probabilité du marché (proba x cote - 1 >= C.XALERT_LAG_EDGE) -> « joue maintenant »,
     avec une mise et un code coupon du pari seul. 1xBet relu en direct pour les seuls matchs concernés (C.XALERT_LIVE_MAX par passage).
  ⚠️ Mes paris en danger : à chaque relevé 1xBet (15:00, mise à jour manuelle), un combiné réel, un combiné sur mesure suivi (« ✅ Je le joue »,
     kelly/custom.py) ou le pari du jour (noté ou non : tout ce qui est suivi) dont un pari a quitté 1xBet ou ne paie plus assez pour avoir sa place -> « n'est plus
     jouable tel quel » et le bouton « 🎟️ Vérifier ce combiné ».
  📈 Mieux payé qu'au matin : un pari 💎 (ou un pari réel montré) dont la cote 1xBet a monté de C.XALERT_RISE depuis le matin (xodds0) et a
     toujours de la valeur -> « c'est le moment », avec un code coupon.
  ✅ Bonne nouvelle : un pari que tu as joué (noté : « ✅ Je l'ai joué », « ✅ Je le joue », /joue) dont le marché a baissé de
     C.ALERT_ODDS_DROP depuis le matin (tu as pris une meilleure cote).
  💰 1xBet paie plus que le marché (gap) : un écart sharp NEUF (kelly/sharpgap.py : la cote 1xBet dépasse la cote juste d'un book de référence frais),
     relevé aux relevés 1xBet (15:00, mise à jour manuelle) ou aux passages horaires (la clôture qui vient d'être relevée donne le prix frais du book de référence).
     Le prix 1xBet est RELU en direct (Reader, mêmes plafonds que les autres alertes) et l'écart recalculé à ce prix avant d'envoyer ; jamais deux fois le même
     pari dans la journée (data/sharpgap_sent.json). Le pari part dans SON message (kelly/gapbets.py : code coupon, mise, « ✅ Je l'ai joué »), suivi comme le
     pari de la section du matin.

Un seul message par passage (💰 d'abord, puis ⚠️, 📈, ✅), C.XALERT_MAX_ITEMS alertes au plus ; data/alerts_sent.json retient la dernière cote
alertée de chaque pari (pas deux fois la même alerte sans nouveau mouvement). Les relectures 1xBet sont plafonnées et ne lèvent jamais.
"""
import math
import sys
from datetime import datetime, timedelta, timezone

from . import chat, combos as CB, config as C, ensemble, fair, fmt, gapbets, gapdist, lanes, money, played, quiet, sharpgap, store, telegram, track, xprice
from .sources import state, xbet
from .telegram import Html

FILE = "alerts_sent.json"                   # {"day", "items": {"clé du pari|type": dernière cote alertée}}
TYPES = ("lag", "gap", "danger", "rise", "good")   # ordre d'affichage
TITLES = {"lag": "💰 1xBet est en retard : joue maintenant", "gap": fmt.GAP_TITLE, "danger": "⚠️ Mes paris en danger",
          "rise": "📈 Mieux payé chez 1xBet qu'au premier relevé : c'est le moment", "good": "✅ Bonne nouvelle"}
LAST = {"gap": 0}                           # écarts sharp annoncés par le dernier passage (réponse de la mise à jour manuelle)
ADVICE = "Si tu ne l'as pas encore joué : touche « 🎟️ Vérifier ce combiné » ou demande un combi."


# ---------- outils ----------

def _ko(c):
    return datetime.fromisoformat(c["commence"].replace("Z", "+00:00"))


def _open(c, now):
    """Match encore ouvert chez 1xBet : pas commencé, et à plus de C.XBET_SOON_MIN min (1xBet le retire de l'avant-match avant)."""
    return _ko(c) > now + timedelta(minutes=C.XBET_SOON_MIN)


def _match(c):
    """« <b>PSG – Lyon</b> · 21h00 · Ligue 1 » : le match d'une alerte, son heure et sa ligue."""
    return " · ".join([fmt.b(fmt.event_name(c))] + [x for x in (fmt.hhmm(c.get("start")), fmt.esc(c.get("league") or "")) if x])


def _clock(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def _read_time(ref, now):
    """Heure « HH:MM » de la lecture 1xBet du pari `ref` (cache du disque), sinon celle du passage."""
    t0 = xbet.odds_time([ref[0]]) if ref else None
    return _clock(t0) if t0 else now.strftime("%H:%M")


def _at(c, price):
    """Le pari réel `c` à la cote 1xBet `price` (copie) : sa valeur est recalculée, comme xprice._pick_price."""
    return c | {"odds": price, "xodds": price, "edge": c["prob"] * price - 1}


def _x_now(m, c):
    """(cote, référence) du pari `c` dans la lecture 1xBet `m` (xbet.fetch_day) ; (None, None) si 1xBet ne le propose plus."""
    px = m["px"]
    key = xbet.resolve(px, c["market"], c["name"], c["point"])
    return (px[key], (m.get("refs") or {}).get(key)) if key else (None, None)


def _suspended(m, c):
    """Le pari `c` est-il listé mais SUSPENDU un moment chez 1xBet dans la lecture `m` (cote affichée, pas jouable pour l'instant) ? Un marché suspendu
    n'est pas « retiré » : pas d'alerte « n'est plus jouable »."""
    blocked = (m or {}).get("blocked")
    return bool(blocked) and xbet.resolve({k: 1 for k in blocked}, c["market"], c["name"], c["point"]) is not None


def _item(kind, key, odds, text, rank=0.0, coupon=None, buttons=(), **extra):
    """Une alerte : `odds` = dernière cote alertée (dédoublonnage), `coupon` = pari seul à enregistrer chez 1xBet ({xref, xodds}). `extra` : données propres
    au type (« 💰 gap » : le pari et son candidat, voir gap_items)."""
    return {"type": kind, "key": key, "odds": odds, "text": text, "rank": rank, "coupon": coupon, "buttons": [b for b in buttons if b]} | extra


# ---------- relectures 1xBet plafonnées ----------

class Reader:
    """Relectures 1xBet en direct d'un passage d'alertes : au plus C.XALERT_LIVE_MAX matchs (un match déjà relu sert à tous ses paris), id
    1xBet du match (xgid) obligatoire ; 1xBet muet ou en panne = None, jamais d'erreur. `fetch` : injectable pour les tests."""

    def __init__(self, fetch=None, cap=None):
        self.fetch, self.cap = fetch, C.XALERT_LIVE_MAX if cap is None else cap
        self.tried, self.found = set(), {}

    def get(self, c):
        """Lecture 1xBet {"gid", "px", "refs"...} du match du pari `c` (dict de pari avec "xgid"), ou None."""
        if c["id"] in self.tried:
            return self.found.get(c["id"])
        if len(self.tried) >= self.cap or not c.get("xgid"):
            return None
        self.tried.add(c["id"])
        m = xprice.live_found([c], self.fetch).get(c["id"])
        if m:
            self.found[c["id"]] = m
        return m


class Prices:
    """Cote 1xBet du moment des paris réels du jour (today.json) : SANS requête quand le cache du disque a une lecture de moins de
    C.XBET_COMBI_FRESH_MIN min (le relevé qui vient d'avoir lieu), sinon relue en direct par le Reader (plafonné)."""

    def __init__(self, reader, now):
        self.reader, self.now, self.memo = reader, now, {}

    def get(self, c):
        """-> ("ok", cote, référence, "HH:MM") | ("gone", None, None, None) : 1xBet ne propose plus le pari | None : inconnu (ou marché suspendu un
        moment : pas une disparition, donc aucune alerte)."""
        if c["key"] not in self.memo:
            self.memo[c["key"]] = self._read(c)
        return self.memo[c["key"]]

    def _read(self, c):
        hit = xbet.cached_cell(c["xref"]) if c.get("xref") else None
        if hit and state.clock() - (hit[1] or 0) <= C.XBET_COMBI_FRESH_MIN * 60:
            if getattr(hit, "blocked", False):
                return None
            return ("ok", hit[0], c["xref"], _clock(hit[1])) if hit[0] else ("gone", None, None, None)
        m = self.reader.get(c)
        if not m:
            return None
        x, ref = _x_now(m, c)
        if not x and _suspended(m, c):
            return None
        return ("ok", x, ref, _read_time(ref, self.now)) if x else ("gone", None, None, None)


# ---------- paris joués ----------

def _took(x):
    """Pari compté comme joué : NOTÉ (« ✅ Je l'ai joué », « ✅ Je le joue », /joue) et pas retiré. Une mise conseillée seule (budget du jour)
    n'est pas un pari joué."""
    return not x.get("skipped") and bool(x.get("played"))


def _custom_rows(day, noted=False):
    """Combinés sur mesure suivis (« ✅ Je le joue ») du jour `day`, pas encore réglés ni retirés (fair_combos.json, marque « custom », voir
    kelly/custom.py). `noted` : seulement ceux que tu as notés joués (pour « tu l'as joué ») ; sans, tous les combinés suivis, comme tous
    les paris du matin : les alertes ⚠️ / 📈 veillent sur tout ce qui est suivi, noté ou non."""
    return [x for x in fair.load_combos() if played.is_custom(x) and x.get("date") == day and x.get("result") is None
            and not x.get("skipped") and not x.get("dead") and x.get("legs") and (x.get("played") or not noted)]


def took_bets(rows, combos, day):
    """({clé du pari: cote prise chez 1xBet ou None}, {clés notées}) des paris du jour que tu as NOTÉS : pari du jour (ligne envoyée de
    tracked.json), jambes des combinés réels (bets.json) et jambes des combinés sur mesure. Cote prise d'un combiné : celle de la jambe.
    Seuls les paris notés comptent (ni « joue maintenant » ni « mieux payé » pour un pari que tu as déjà pris) : les deux ensembles ont les
    mêmes clés."""
    took = {}
    for r in played.single_rows(rows, combos):
        if r.get("date") == day and r.get("result") is None and _took(r):
            took[r["key"]] = r["played"].get("odds") or r.get("xodds") or r.get("odds")
    for r in played.gap_rows(rows):                  # paris « 1xBet paie plus que le marché » notés (g<n>)
        if r.get("date") == day and r.get("result") is None and _took(r):
            took[r["key"]] = r["played"].get("odds") or r.get("xodds") or r.get("odds")
    for x in combos:
        if x.get("date") == day and x.get("result") is None and _took(x):
            legs = {l["key"]: l.get("xodds") for l in x.get("xlegs") or []}
            for k in x.get("keys") or []:
                took.setdefault(k, legs.get(k))
    for x in _custom_rows(day, noted=True):
        for l in x["legs"]:
            took.setdefault(l["key"], l.get("xodds"))
    return took, set(took)


# ---------- ✅ bonne nouvelle ----------

def good_items(closing, rows, took):
    """✅ Les paris joués (`took`) dont le marché (cote typique, relevé de clôture) a baissé d'au moins C.ALERT_ODDS_DROP depuis le matin
    (cote de leur ligne de suivi) : « tu as pris 2,10, le marché est maintenant à 1,85 (+13 % de valeur prise) ». Informatif."""
    by_row = {r["key"]: r for r in rows}
    out = []
    for c in closing:
        r = by_row.get(c["key"])
        if c["key"] not in took or not r or not r.get("odds"):
            continue
        mine = took[c["key"]] or r.get("xodds") or r["odds"]
        drop, gain = (r["odds"] - c["odds"]) / r["odds"], mine / c["odds"] - 1
        if drop < C.ALERT_ODDS_DROP or gain <= 0:
            continue
        out.append(_item("good", c["key"], c["odds"],
                         f"{_match(c)}\n👉 {fmt.esc(c['pick'])} : tu as pris {fmt.b(fmt.odds_txt(mine))}, le marché est maintenant à "
                         f"{fmt.b(fmt.odds_txt(c['odds']))} ({fmt.signed(round(gain * 100), ' %')} de valeur prise)\n"
                         + fmt.i(f"Le marché a baissé de {round(drop * 100)} % depuis ce matin : tu as pris mieux que le prix d'aujourd'hui. "
                                 "Rien à faire."), rank=gain))
    return out


# ---------- 💰 1xBet en retard ----------

def _ref_event(c, today):
    """Le pari alerté `c` (marché) a-t-il une référence chez 1xBet dans data/today.json (même match, marché, côté et ligne dans les paris ou
    les pronostics, id 1xBet du match connu) ? -> `c` + "xgid" (de quoi relire le match sans menu), ou None."""
    cands = [x for x in (today.get("picks") or []) + (today.get("pronos") or []) if x.get("id") == c["id"]]
    same = [x for x in cands if x.get("key") == c["key"] or xbet.same_bet(x, c)]
    gid = next((x["xgid"] for x in same + cands if x.get("xgid")), None)
    return c | {"xgid": gid} if gid and any(x.get("xref") or x.get("xgid") for x in same) else None


def _stake_line(p, x):
    """Ligne « mise » d'un pari seul de proba `p` à la cote `x` : mise conseillée sur le budget du jour (money.allocate), ou, sans budget du
    jour, de quoi le régler (jamais d'unités) ; None si le budget n'en laisse aucune (pari trop petit pour l'arrondi : rien à jouer)."""
    if money.active():
        st = money.allocate([{"kind": "pari", "tier": "pari", "prob": p, "odds": x}])[0][0]
        return f"💰 Mise conseillée : {fmt.b(fmt.esc(money.money(st) + ' (sur ton budget du jour)'))}" if st else None
    return f"💡 Mise conseillée : {fmt.esc(money.NO_BUDGET)}"


def lag_items(alerts, today, reader, now, noted=(), skip=()):
    """💰 Pour chaque mouvement fort du marché (find_alerts : [(score, pari de clôture, ancienne lecture)]) sur un pari qui a une référence
    1xBet et que tu n'as pas noté : 1xBet relu en direct (Reader) ; s'il paie encore avec de la valeur face à la NOUVELLE proba du
    marché (proba x cote - 1 >= C.XALERT_LAG_EDGE) : « joue maintenant ». 1xBet déjà ajusté : rien (voir good_items). `skip` : clés déjà
    traitées par ✅."""
    out = []
    for _, c, prev in alerts:
        if c["key"] in noted or c["key"] in skip or not _open(c, now) or not lanes.ticket_ok(c):       # famille hors voies : jamais un « joue maintenant » avec code coupon
            continue
        ev = _ref_event(c, today)
        m = reader.get(ev) if ev else None
        x, ref = _x_now(m, c) if m else (None, None)
        if not x:
            continue
        p = c.get("prob_book", c["prob"])
        value = p * x - 1
        stake = _stake_line(p, x) if value >= C.XALERT_LAG_EDGE else None
        if not stake:                               # 1xBet a déjà suivi le marché, ou pas de mise sur le budget : rien à jouer
            continue
        chances = f" ({fmt.pct(prev['pb'])} ➜ {fmt.pct(p)} de chances)" if prev.get("pb") else ""
        out.append(_item("lag", c["key"], x,
                         f"{_match(c)}\n👉 {fmt.esc(c['pick'])} — 1xBet paie {fmt.b(fmt.odds_txt(x))} "
                         f"{fmt.i('(lu à ' + _read_time(ref, now) + ')')}\n"
                         f"📊 Le marché a bougé : {fmt.odds_txt(prev['o'])} ➜ {fmt.b(fmt.odds_txt(c['odds']))}{chances}\n"
                         f"💎 1xBet n'a pas suivi : valeur {fmt.b(fmt.signed(round(value * 100), ' %'))} avec la nouvelle probabilité du marché\n"
                         f"{stake}", rank=value, coupon={"xref": ref, "xodds": x} if ref else None))
    return out


# ---------- 💰 1xBet paie plus que le marché ----------

def _fresh_sharp(picks, closing, now):
    """Les paris réels du jour avec, quand la clôture qui vient d'être relevée (`closing`, odds.build_picks) les couvre, LEUR proba sharp fraîche (prob_book,
    prob_src, n_sharp) et l'heure de sa lecture : le prix du book de référence d'il y a quelques minutes, pas celui du matin."""
    fresh = {c["key"]: c for c in closing or [] if ensemble.sharp_of(c)}
    out = []
    for c in picks:
        f = fresh.get(c["key"])
        out.append(c if f is None else c | {k: f[k] for k in ("prob_book", "prob_src", "n_sharp") if k in f}
                   | {"sharp_at": f.get("sharp_at") or now.isoformat()})
    return out


def gap_items(today, now, reader, closing=(), skip=()):
    """💰 Les écarts sharp NEUFS du jour : paris réels de `today` (data/today.json) dont la proba sharp est FRAÎCHE (sharpgap.scan ; `closing` : la clôture du passage
    horaire rafraîchit la proba sharp) et dont la cote 1xBet du matin est à moins de C.SHARPGAP_PRESCREEN du seuil, pas encore annoncés aujourd'hui
    (data/sharpgap_sent.json), pas dans `skip` (notés, pari du jour), dans la limite des annonces du jour (sharpgap.room). Le prix 1xBet est RELU en direct (`reader`,
    plafonné comme les autres alertes) puis l'écart recalculé à CE prix (sharpgap.check) : sous le seuil, périmé ou pari retiré = rien. Sans lecture possible :
    rien (jamais un prix non vérifié)."""
    day = today.get("day")
    if day and today.get("picks"):
        gapdist.observe(_fresh_sharp(today["picks"], closing, now), now, today)      # décision 5 : la lecture à sharp FRAIS du passage horaire entre dans la distribution (jamais une alerte)
    if not C.SHARPGAP_ENABLED or not day or not today.get("picks") or not lanes.gaps_allowed():       # voie « off » / « ghost » : aucune alerte, rien de réservé ni de suivi
        return []
    picks = _fresh_sharp([c for c in today["picks"] if lanes.usable(c)], closing, now)       # ni un écart sur un marché de stats d'une ligue en fantôme
    seen, room = sharpgap.sent_keys(day), sharpgap.room(day)
    res = sharpgap.scan(today | {"picks": picks}, now, min_edge=C.SHARPGAP_EDGE - C.SHARPGAP_PRESCREEN, x_fresh=False)
    new = [x for x in res["cands"] if x["key"] not in seen and x["key"] not in skip][:room]
    by_key = {c["key"]: c for c in picks}
    out = []
    for x in new:
        pick = by_key[x["key"]]
        m = reader.get(x)
        price, ref = _x_now(m, x) if m else (None, None)
        if not price:
            continue
        cand, _ = sharpgap.check(pick, price, ref, m.get("gid") or x.get("xgid"), now)
        if cand is None:
            continue
        cand = cand | {"xcid": m.get("cid") or x.get("xcid")}
        d = sharpgap.describe(cand)
        out.append(_item("gap", cand["key"], cand["xodds"], f"{d['title']} — {d['line']}", rank=cand["edge"], pick=pick, cand=cand))
    return out


def _send_gap(entries, day, now):
    """Envoie le message des écarts sharp `entries` ([(pari, candidat)]) : gapbets.message (numéros, codes, mises, boutons) puis le suivi (track.record_gap).
    -> le message, ou None."""
    msg, rec = gapbets.message([x for _, x in entries], [p for p, _ in entries], day, now, "alerte", when=now.strftime("%H:%M"))
    if msg is None:
        return None
    track.record_gap(rec, day, now)                   # retenus avant l'envoi : un message mis en attente (réseau) n'est pas doublé
    LAST["gap"] += len(rec)
    quiet.send(msg, now)                              # heures calmes : gardé pour le réveil
    return msg


# ---------- ⚠️ mes paris en danger / 📈 mieux payé ----------

def real_targets(today, rows, combos, day, now):
    """Paris réels du jour encore jouables (ni réglés, ni retirés par « ❌ Pas joué », aucun match commencé ni imminent), aux cotes 1xBet du
    matin : [(nom, nom court du bouton « Vérifier », mot du pari pour ce bouton, ligne de suivi, jambes de today.json)] : le pari du jour, les
    combinés de bets.json et les combinés sur mesure suivis (leurs jambes : celles de leur dernière version suivie, avec les champs du pari de
    today.json quand c'est un pari réel)."""
    by_key = {c["key"]: c for c in today.get("picks") or []}
    singles = [("Le pari du jour", "pari du jour", "pari", r, [r["key"]]) for r in played.single_rows(rows, combos) if r.get("xodds")]
    combos_ = [(f"Le combiné {fmt.tier_word(x['tier'])}", fmt.tier_word(x["tier"]), played.norm(fmt.tier_word(x["tier"])), x, x["keys"])
               for x in combos if x.get("xlegs")]
    out = []
    for name, short, what, x, keys in singles + combos_:
        legs = [by_key.get(k) for k in keys]
        if (x.get("date") == day and x.get("result") is None and not x.get("skipped") and all(legs)
                and all(l.get("xodds") and _open(l, now) for l in legs)):
            out.append((name, short, what, x, legs))
    for x in _custom_rows(day):
        legs = [(by_key.get(l["key"]) or {}) | {k: v for k, v in l.items() if v is not None and k != "result"} for l in x["legs"]]
        if all(l.get("xodds") and _open(l, now) for l in legs):
            out.append((f"Le {played.custom_name(x).lower()}", played.custom_name(x), f"v{x['tier']}", x, legs))
    return out


def _worth(c, price):
    """Le pari `c` payé `price` chez 1xBet a-t-il encore sa raison d'être ? Pari réel : un avantage clair (CB.leg_reason) ; pronostic 💎 d'un
    combiné sur mesure (il a une cote mini) : au moins sa cote mini, celle de la proba de Kelly."""
    if c.get("min_odds"):
        return price >= c["min_odds"]
    return bool(CB.leg_reason(_at(c, price)))


def danger_items(targets, prices, day):
    """⚠️ Combinés réels, combinés sur mesure suivis et pari du jour dont un pari n'est plus chez 1xBet, ou paie moins qu'à son choix au point
    de perdre sa raison d'être (_worth). Un pari dont la cote a baissé mais garde son avantage ne déclenche rien."""
    out = []
    for name, short, what, x, legs in targets:
        if x.get("played"):                         # ticket déjà placé : rien à faire de cette alerte (le suivi des tickets, kelly/watch.py, dit le reste)
            continue
        lines, cur, gone = [], [], False
        for c in legs:
            st = prices.get(c)
            now_odds = st[1] if st and st[0] == "ok" else c["xodds"]
            cur.append(now_odds)
            label = f"{fmt.event_name(c)} ({fmt.hhmm(c.get('start'))} · {fmt.esc(c.get('league') or '')}) — {fmt.esc(c['pick'])}"
            if st and st[0] == "gone":
                lines.append(f"❌ Plus chez 1xBet : {label}")
                gone = True
            elif st and now_odds < c["xodds"] and not _worth(c, now_odds):
                lines.append(f"📉 Moins bien payé : {label} : {fmt.odds_txt(c['xodds'])} ➜ {fmt.b(fmt.odds_txt(now_odds))} chez 1xBet "
                             f"(plus d'avantage clair, lu à {st[3]})")
        if lines:
            button = chat.refresh_button(what, day, f"🎟️ Vérifier ce combiné : {short}")
            out.append(_item("danger", f"{x.get('date')}|{what}", 0.0 if gone else round(math.prod(cur), 3),
                             fmt.b(fmt.esc(name) + " n'est plus jouable tel quel") + "\n" + "\n".join(lines) + "\n" + fmt.i(ADVICE),
                             rank=len(lines), buttons=[button]))
    return out


def _rise_text(c, old, new, value, at):
    """💎 seulement si le prix bat vraiment le marché ; une jambe jouable à valeur négative (stats / sûre : plancher −5 % / −9 %) est dite telle quelle."""
    last = (f"💎 Valeur {fmt.signed(round(value * 100), ' %')} à ce prix" if value > 0
            else f"⚖️ Valeur {fmt.signed(round(value * 100), ' %')} à ce prix : jambe jouable, mais elle ne bat pas le marché")
    return (f"{_match(c)}\n👉 {fmt.esc(c['pick'])} : 1xBet payait {fmt.odds_txt(old)} au premier relevé du jour, il paie maintenant "
            f"{fmt.b(fmt.odds_txt(new))} ({fmt.signed(round((new / old - 1) * 100), ' %')}, lu à {at})\n"
            + last)


def rise_items(pronos, got, at, targets, prices, noted=()):
    """📈 Les paris montrés ce matin (pronostics 💎 : CB.shown_pronos, relus à ce relevé `at` ; paris réels : pari du jour et jambes des
    combinés non notés joués) dont la cote 1xBet a monté d'au moins C.XALERT_RISE depuis le matin (xodds0) et qui ont toujours de la valeur."""
    out, seen = [], set()
    for c in CB.shown_pronos(pronos):
        old, new = c.get("xodds0"), c.get("xodds")
        if c["id"] in got and old and new and new / old - 1 >= C.XALERT_RISE and (c.get("xev") or 0) > 0 and c.get("xkind") != "probable" and c["key"] not in noted:       # 🎯 « sans valeur » : jamais annoncé comme une valeur
            seen.add(c["key"])
            out.append(_item("rise", c["key"], new, _rise_text(c, old, new, c["xev"], at), rank=new / old,
                             coupon={"xref": c["xref"], "xodds": new} if c.get("xref") else None))
    for _, _, _, _, legs in targets:
        for c in legs:
            if c["key"] in seen or c["key"] in noted:
                continue
            seen.add(c["key"])
            st, old = prices.get(c), c.get("xodds0") or c["xodds"]
            if st and st[0] == "ok" and st[1] / old - 1 >= C.XALERT_RISE and CB.leg_reason(_at(c, st[1])):
                out.append(_item("rise", c["key"], st[1], _rise_text(c, old, st[1], c["prob"] * st[1] - 1, st[3]), rank=st[1] / old,
                                 coupon={"xref": st[2], "xodds": st[1]} if st[2] else None))
    return out


# ---------- envoi : un message par passage ----------

def _load(day):
    d = store.load(FILE, {})
    return d if isinstance(d, dict) and d.get("day") == day and isinstance(d.get("items"), dict) else {"day": day, "items": {}}


def _fresh(sent, it):
    """Alerte à envoyer : jamais envoyée pour ce pari, ou sa cote a encore bougé d'au moins le seuil depuis la dernière alerte."""
    last = sent["items"].get(f"{it['key']}|{it['type']}")
    thr = C.ALERT_ODDS_DROP if it["type"] == "good" else C.XALERT_RISE
    return last is None or (abs(it["odds"] / last - 1) >= thr if last else bool(it["odds"]))


RING = ("lag", "gap", "danger")                # les alertes qui sonnent (niveaux de bruit) ; « rise » et « good » arrivent en silence
PERISHABLE = ("lag", "rise")      # « joue maintenant » et « c'est le moment » : périmées au réveil, jamais gardées pour la fin des heures calmes


def send(items, day, now):
    """Dédoublonne (data/alerts_sent.json), classe (💰, ⚠️, 📈, ✅), garde C.XALERT_MAX_ITEMS alertes, crée le code coupon des paris seuls à
    jouer et envoie UN message. Pendant les heures calmes (quiet.is_quiet), les alertes qui ne valent que sur le moment (💰 « joue maintenant »,
    📈 « c'est le moment ») sont ABANDONNÉES (sans être retenues comme envoyées : au réveil, un passage les recalcule avec les cotes du moment) ;
    les autres sont gardées pour le réveil. -> le message envoyé, ou None."""
    sent = _load(day)
    if quiet.is_quiet(now):
        items = [it for it in items if it["type"] not in PERISHABLE]
    items = sorted((it for it in items if _fresh(sent, it)), key=lambda it: (TYPES.index(it["type"]), -it["rank"]))
    shown, more = items[:C.XALERT_MAX_ITEMS], len(items) - C.XALERT_MAX_ITEMS
    if not shown:
        return None
    gaps = [it for it in shown if it["type"] == "gap"]
    gap_msg = None
    if gaps:                                        # 💰 écarts sharp : leur propre message (boutons « ✅ Je l'ai joué » refaits pour tout le message à chaque note)
        try:
            gap_msg = _send_gap([(it["pick"], it["cand"]) for it in gaps], day, now)
        except Exception as e:                      # noqa: BLE001 - un écart en échec ne bloque pas les autres alertes
            print(f"[alertes] écarts sharp ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        for it in gaps:
            sent["items"][f"{it['key']}|{it['type']}"] = it["odds"]
        shown = [it for it in shown if it["type"] != "gap"]
        if not shown:
            store.save(FILE, sent)
            return gap_msg
    blocks, buttons, codes = [f"🔔 {fmt.b('Alertes Kelly')} · {now.strftime('%H:%M')}"], [], []
    for kind in TYPES:
        group = [it for it in shown if it["type"] == kind]
        if not group:
            continue
        blocks.append(fmt.b(TITLES[kind]))
        for it in group:
            text = it["text"]
            if it["coupon"]:
                code = xbet.save_coupon([it["coupon"]])
                text += fmt.coupon_block(code, "cherche ce pari dans l'appli 1xBet").replace("\n\n", "\n", 1)
                codes += [code] if code else []
            blocks.append(text)
            buttons += it["buttons"]
    blocks.append(fmt.i("Vérifie la cote dans l'appli avant de valider : 1xBet a pu bouger depuis sa lecture."
                        + (f" + {more} autre{'s' if more > 1 else ''} alerte{'s' if more > 1 else ''} au prochain passage." if more > 0 else "")))
    msg = telegram.topic(Html("\n\n".join(blocks)), "alerts")
    if not any(it["type"] in RING for it in shown):       # seulement 📈 / ✅ : visibles, sans sonnerie
        telegram.silent(msg)
    rows = telegram.inline([[b] for b in buttons])["inline_keyboard"] + (telegram.copy_row(codes[0]) if len(codes) == 1 else [])   # « 📋 Copier » : un seul code
    if rows:
        msg = telegram.with_buttons(msg, {"inline_keyboard": rows})
    for it in shown:                                # retenues avant l'envoi : un message mis en attente (réseau) ne sera pas doublé
        sent["items"][f"{it['key']}|{it['type']}"] = it["odds"]
    store.save(FILE, sent)
    quiet.send(msg, now)                            # heures calmes : gardé pour le réveil
    return msg


def _singles(rows, combos, day):
    """Clés du pari du jour (ligne envoyée seule, played.single_rows) : il a déjà son message et son bouton, jamais repris dans « 1xBet paie plus que le marché »."""
    return {r["key"] for r in played.single_rows(rows, combos) if r.get("date") == day}


def _gap_items_safe(today, now, reader, closing, skip):
    """gap_items, sans jamais lever : un écart sharp en échec ne bloque ni les autres alertes ni le passage."""
    try:
        return gap_items(today, now, reader, closing, skip)
    except Exception as e:                          # noqa: BLE001
        print(f"[alertes] écarts sharp ignorés : {type(e).__name__}: {e}", file=sys.stderr)
        return []


def on_market(alerts, closing, today, rows, combos, day, now, fetch=None):
    """Passage horaire (app.run_track, app.run_alert) : 💰 et ✅ à partir des mouvements du marché `alerts` (app.find_alerts) et des paris
    de clôture `closing`. `today` : data/today.json du jour en cours ({} sinon). -> le message envoyé ou None. Ne lève jamais."""
    if not C.XALERTS_ENABLED:
        return None
    try:
        LAST["gap"] = 0
        took, noted = took_bets(rows, combos, day)
        good = good_items(closing, rows, took)
        reader = Reader(fetch)
        lag = lag_items(alerts, today, reader, now, noted, {it["key"] for it in good})
        return send(lag + good + _gap_items_safe(today, now, reader, closing, noted | _singles(rows, combos, day)), day, now)
    except Exception as e:                          # noqa: BLE001 - une alerte en échec ne bloque jamais le passage horaire
        print(f"[alertes] ignorées : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def on_refresh(today, now, got, upcoming, fetch=None):
    """Relevé 1xBet (app.run_xbet_refresh : 15:00, mise à jour manuelle) : ⚠️ et 📈. `got` : matchs relus par ce relevé, `upcoming` : pronostics
    à venir remis à leurs cotes. Les paris réels sont rechiffrés par le cache du relevé, sinon relus en direct (C.XALERT_LIVE_MAX matchs au
    plus). -> le message envoyé ou None. Ne lève jamais."""
    if not C.XALERTS_ENABLED or not got:
        return None
    try:
        day = today["day"]
        rows, combos = track.load(), store.load("bets.json", {"combos": []}).get("combos", [])
        LAST["gap"] = 0
        reader = Reader(fetch)
        prices = Prices(reader, now)
        targets = real_targets(today, rows, combos, day, now)
        noted = took_bets(rows, combos, day)[1]
        items = (danger_items(targets, prices, day)
                 + rise_items(upcoming, got, today.get("xbet_at") or now.strftime("%H:%M"), targets, prices, noted)
                 + _gap_items_safe(today, now, reader, (), noted | _singles(rows, combos, day)))
        return send(items, day, now)
    except Exception as e:                          # noqa: BLE001 - une alerte en échec ne bloque jamais le relevé
        print(f"[alertes] ignorées : {type(e).__name__}: {e}", file=sys.stderr)
        return None
