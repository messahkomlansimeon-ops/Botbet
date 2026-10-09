"""Message « 💰 1xBet paie plus que le marché » : les écarts sharp (kelly/sharpgap.py) annoncés le matin (app.build_day) ou aux relevés et passages horaires
(kelly/xalerts.py), avec pour chaque pari son code coupon, sa mise conseillée du budget du jour et son bouton « ✅ Je l'ai joué » (callback « joue|g<n>|<jour> » :
played.find le retrouve dans data/tracked.json, voir track.record_gap). Tout est SUIVI comme les autres paris : numéro « g<n> » et dédoublonnage par jour
(sharpgap.reserve), ligne de suivi ENVOYÉE (clôture relevée dans le budget « closing », règlement gratuit), note et argent (played, money), bilan.

message() prépare le message et les lignes à enregistrer SANS rien écrire dans tracked.json (l'appelant appelle track.record_gap quand les messages sont
prêts : le matin, après track.record, pour ne jamais fausser le « pari du jour »). Seuls sharpgap.reserve (numéros) et la création des codes coupon
(xbet.save_coupon : un échec = pas de code, jamais d'erreur) ont des effets de bord.
"""
from . import chat, config as C, fmt, money, played, sharpgap, telegram
from .sources import xbet


def stake_alone(x):
    """Mise conseillée (FCFA) d'un pari seul à sa cote 1xBet et à sa proba sharp, sur le budget du jour (money.allocate) ; None sans budget du jour, 0 si
    le budget n'en laisse aucune (trop petite pour l'arrondi)."""
    if not money.active():
        return None
    return money.allocate([{"kind": "pari", "tier": "pari", "prob": x["p_sharp"], "odds": x["xodds"]}])[0][0]


def stake_item(x):
    """L'item de money.allocate d'un écart sharp : sa proba est celle des books de référence, sa cote celle de 1xBet (espérance = l'écart)."""
    return {"kind": "pari", "tier": "pari", "prob": x["p_sharp"], "odds": x["xodds"]}


def coupon(pick, x):
    """Code coupon 1xBet du pari seul `pick` à la cote lue `x["xodds"]` (référence `x["xref"]`) ; None sans référence, ou si l'enregistrement échoue."""
    if not x.get("xref") or not x.get("xodds"):
        return None
    try:
        return xbet.save_coupon([pick | {"xodds": x["xodds"], "xref": x["xref"]}])
    except Exception:                                # noqa: BLE001 - un code manquant ne bloque jamais l'annonce
        return None


def stake_txt(stake):
    """La mise conseillée d'un pari, en mots : « 500 FCFA », ou « pas de mise (trop petite pour ton budget) » ; None sans budget du jour."""
    if stake is None:
        return None
    return money.money(stake) if stake else "pas de mise (trop petite pour ton budget)"


def message(cands, picks, day, now, via, stakes=None, when=None):
    """Le message « 💰 1xBet paie plus que le marché » des écarts `cands` (sharpgap.find / sharpgap.check, au plus C.SHARPGAP_MAX_SHOWN) -> (message avec ses boutons
    ou None, lignes à enregistrer [(pari, candidat numéroté, mise, code)]). `picks` : paris réels du jour (data/today.json) d'où vient chaque pari ; `stakes` :
    {clé du pari: mise} déjà répartie sur le budget du jour (le matin), sinon la mise de chaque pari seul ; `via` : « matin », « relevé » ou « alerte » ;
    `when` : heure « HH:MM » de la lecture des cotes 1xBet. Les numéros « g<n> » sont réservés ici (sharpgap.reserve) ; un pari déjà annoncé garde le sien."""
    by_key = {c["key"]: c for c in picks}
    known = sharpgap.sent_keys(day)
    got = sharpgap.reserve([x for x in cands if x["key"] in by_key][:C.SHARPGAP_MAX_SHOWN], day, via, now)
    if not got:
        return None, []
    try:
        blocks, rec, items = [], [], []
        for k, x in enumerate(got, 1):
            pick = by_key[x["key"]]
            st = stake_alone(x) if stakes is None else stakes.get(x["key"], stake_alone(x))
            code = coupon(pick, x)
            blocks.append(fmt.gap_block(k if len(got) > 1 else None, x, sharpgap.describe(x), code, stake_txt(st)))
            rec.append((pick, x, st, code))
            items.append((f"g{x['n']}", played.short_pick(pick), st or 0, None))
        markup = chat.gap_markup(items, day)
        if len(rec) == 1:
            markup["inline_keyboard"] += telegram.copy_row(rec[0][3])
        return telegram.with_buttons(fmt.gap_message(blocks, when, budget=money.active()), markup), rec
    except Exception:
        sharpgap.unreserve([x["key"] for x in got if x["key"] not in known], day)       # rien n'a été annoncé : ces paris restent à annoncer
        raise
