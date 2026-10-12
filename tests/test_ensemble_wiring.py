"""Phase 2 de l'ensemble (kelly/ensemble.py) et des écarts sharp (kelly/sharpgap.py), branchés dans Kelly, hors ligne :
  - mode « mixte » : l'ensemble DÉCIDE pour les buts / 1N2 / double chance / les 2 marquent, la règle d'avant reste ailleurs (décisions identiques, testé), avis fantômes ;
  - affichage « 1xBet : 49 % · Kelly : 71 % · retenu : 61 % » sous les pronostics et les jambes, légende 💎 ;
  - journal des avis (record / règlement / apprentissage) : idempotent, jamais d'erreur ; poids appris dans la semaine et /bilan détail ;
  - heure de lecture de The Odds API (sharp_at) ;
  - « 💰 1xBet paie plus que le marché » : section du matin avec code coupon, ligne de suivi, bouton « ✅ Je l'ai joué », note et argent ; relevé de l'après-midi
    (prix relu en direct, écart recalculé, périmé refusé, dédoublonné) ; alerte du passage horaire ; clôture contre Pinnacle ; bilan.
Lancer : python -m tests.test_ensemble_wiring"""
import contextlib
import copy
import io
import os
import random
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-ensw-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ensw-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-ensw-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ.pop("XBET_PROXY", None)
for _k in ("ENSEMBLE_ENABLED", "ENSEMBLE_ACTIVE_FAMILIES", "ENSEMBLE_LOG", "SHARPGAP_ENABLED"):
    os.environ.pop(_k, None)

from kelly import (app, cache, calib, chat, combos as CB, config as C, ensemble as E, fair, fmt, freeclose, gapbets, learn, migrate, money, odds,  # noqa: E402
                   played, settle, sharpgap, stats, store, telegram, track, weekly, xalerts, xprice, xworld)
from kelly.sources import xbet  # noqa: E402

UTC = timezone.utc
DAY = "2030-01-01"
KO = f"{DAY}T19:00:00Z"
NOW = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)


def at(h, m=0):
    return datetime(2030, 1, 1, h, m, tzinfo=UTC)


def reset():
    """Données vides, ensemble au réglage par défaut (mixte), pas de budget, aucune alerte ni annonce déjà envoyée."""
    for f in ("tracked.json", "fair.json", fair.COMBOS_FILE, E.LOG_FILE):
        store.save(f, [])
    store.save("bets.json", {"picks": [], "combos": []})
    for f in (app.COMBO_UPDATES, money.FILE, calib.FILE, E.FILE, xalerts.FILE, sharpgap.SENT_FILE, chat.PENDING_FILE, "odds_history.json",
              migrate.FILE, "weekly.json", "today.json", "bankroll.json"):
        store.save(f, {})
    store.save("bankroll.json", {"bankroll": 100.0, "initial": 100.0})
    calib._memo.clear()
    E.reset()
    chat.forget_taps()
    C.ENSEMBLE_ENABLED, C.ENSEMBLE_LOG, C.SHARPGAP_ENABLED = True, True, True
    C.ENSEMBLE_ACTIVE_FAMILIES = frozenset({"goals", "h2h", "dc", "btts", "team_goals"})
    C.XALERTS_ENABLED, C.XALERT_LIVE_MAX = True, 3
    C.SHARPGAP_MAX_SHOWN, C.SHARPGAP_MAX_DAY = 3, 6


class Fakes:
    """Messages, boutons modifiés et codes coupon capturés : aucun réseau (xbet.save_coupon n'est JAMAIS appelé pour de vrai)."""

    def __init__(self, fail_coupon=False):
        self.sent, self.edits, self.coupons, self.fail_coupon = [], [], [], fail_coupon

    def __enter__(self):
        self.old = (xbet.save_coupon, telegram.send, telegram.edit_buttons, telegram.flush_outbox)

        def coupon(legs):
            if self.fail_coupon:
                return None
            self.coupons.append([(c.get("id"), c.get("xodds")) for c in legs])
            return f"CODE{len(self.coupons)}"
        xbet.save_coupon = coupon
        telegram.send = lambda text, *a, **k: (self.sent.append(text), True)[1]
        telegram.edit_buttons = lambda mid, markup, chat_id=None: (self.edits.append((mid, markup)), True)[1]
        telegram.flush_outbox = lambda *a, **k: 0
        return self

    def __exit__(self, *a):
        xbet.save_coupon, telegram.send, telegram.edit_buttons, telegram.flush_outbox = self.old

    def gap_msgs(self):
        return [m for m in self.sent if "1xBet paie plus que le marché" in str(m)]


def cbs(msg_or_markup):
    """Données de rappel (callback_data) de tous les boutons d'un message ou d'un clavier."""
    mk = msg_or_markup if isinstance(msg_or_markup, dict) else (getattr(msg_or_markup, "markup", None) or {"inline_keyboard": []})
    return [b["callback_data"] for row in mk["inline_keyboard"] for b in row if "callback_data" in b]


def labels(msg_or_markup):
    mk = msg_or_markup if isinstance(msg_or_markup, dict) else (getattr(msg_or_markup, "markup", None) or {"inline_keyboard": []})
    return [b["text"] for row in mk["inline_keyboard"] for b in row]


def quiet():
    return contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())


class Quiet:
    def __enter__(self):
        self.a, self.b = quiet()
        self.a.__enter__(), self.b.__enter__()
        return self

    def __exit__(self, *e):
        self.b.__exit__(*e), self.a.__exit__(*e)


# ---------- pronostics et paris réels d'essai ----------

def prono(market="totals", name="Under", point=3.5, p0=0.76, i=1, alts=None, **kw):
    c = {"key": f"m{i}|{market}||{point if point is not None else ''}|{name}", "id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}",
         "event": f"H{i} - A{i}", "commence": KO, "start": "19:00", "league": "Premier League", "market": market, "name": name, "point": point, "who": None,
         "prob": p0, "p0": p0, "min_odds": round(1 / p0 * 1.05, 2), "conf": 0.6, "n_min": 8, "score": 0.2 - i / 1000,
         "pick": fmt.label(market, name, point, None, "soccer"), "group": "soccer"}
    c["alts"] = alts if alts is not None else [[name, point, p0, None]]
    return c | kw


def pair(market, name, point, a, b):
    """Les deux cotes (plus/moins, oui/non) d'une ligne chez 1xBet : `name` à `a`, l'autre issue à `b`."""
    other = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}[name]
    return {(market, name, point): a, (market, other, point): b}


def refs_of(px, gid=900):
    return {k: [gid, 1 + i, 0] for i, k in enumerate(px)}


SHADOW = {"p_model", "p_x", "p_sharp", "p_ens", "ew", "esrc"}


# ---------- 1. réglages ----------

def test_flags_and_environment():
    reset()
    assert C.ENSEMBLE_ENABLED and C.ENSEMBLE_LOG and C.SHARPGAP_ENABLED
    assert C.ENSEMBLE_ACTIVE_FAMILIES == frozenset({"goals", "h2h", "dc", "btts", "team_goals"})
    for m in ("totals", "h2h", "double_chance", "btts"):
        assert E.is_active(m), m
    for m in ("alternate_totals_corners", "alternate_totals_cards", "team_shots_total", "team_sot_home", "team_fouls_away", "team_throws_total",
              "team_offsides_total", "player_shots", "spreads"):
        assert not E.is_active(m), m
    assert E.logging_on() and E.legend() == E.LEGEND
    # variables d'environnement : liste, « none » (aucune famille), casse et espaces, vide = défaut
    for raw, want in (("goals,corners", {"goals", "corners"}), ("none", set()), (" Goals , h2h ", {"goals", "h2h"}), ("", {"x"}), ("0", set())):
        os.environ["ENSEMBLE_ACTIVE_FAMILIES"] = raw
        assert C._set("ENSEMBLE_ACTIVE_FAMILIES", ("x",)) == frozenset(want), raw
    os.environ.pop("ENSEMBLE_ACTIVE_FAMILIES")
    for raw, want in (("0", False), ("1", True), ("", True), ("non", False)):
        os.environ["ENSEMBLE_ENABLED"] = raw
        assert C._bool("ENSEMBLE_ENABLED", True) is want, raw
    os.environ.pop("ENSEMBLE_ENABLED")
    # interrupteurs : tout éteint / pur fantôme / sans journal
    C.ENSEMBLE_ENABLED = False
    assert not E.is_active("totals") and not E.logging_on() and E.legend() == ""
    C.ENSEMBLE_ENABLED, C.ENSEMBLE_ACTIVE_FAMILIES = True, frozenset()
    assert not E.is_active("totals") and E.logging_on() and E.legend() == ""
    C.ENSEMBLE_ACTIVE_FAMILIES, C.ENSEMBLE_LOG = frozenset({"goals"}), False
    assert E.is_active("totals") and not E.is_active("h2h") and not E.logging_on()
    reset()
    print("ok  réglages : défaut mixte (4 familles), variables d'environnement, interrupteurs (tout éteint, fantôme seul, sans journal)")


# ---------- 2. décisions du mode mixte ----------

def test_active_family_is_decided_by_the_ensemble():
    reset()
    base = prono(p0=0.76, alts=[["Over", 1.5, 0.76, None], ["Under", 1.5, 0.24, None]], name="Over", point=1.5)
    px = pair("totals", "Over", 1.5, 1.45, 2.9)
    refs = refs_of(px)
    p_x = E.x_fair(px, "totals", "Over", 1.5)
    sharp = {"over|1.5": [0.74, "pinnacle", 1]}
    c = base | {"psh": sharp}
    assert xprice.price(c, px, refs) == "value"
    p_ens = E.combine("goals", 0.76, p_x, 0.74)[0]
    assert c["prob"] == c["p_ens"] == round(p_ens, 4) and c["p_model"] == 0.76 and c["p_sharp"] == 0.74 and c["p_x"] == round(p_x, 4)
    assert c["p_ens"] < 0.76 and c["xodds"] == 1.45 and c["min_odds"] <= 1.45 and CB.viable(c)
    assert E.explain_c(c) == "1xBet : 67 % · Sharp : 74 % · Kelly : 76 % · retenu : 73 %", E.explain_c(c)
    # sans avis sharp, un marché à 88 % : la valeur du modèle seul (0,76 x 1,45 = 1,10) n'est plus de la valeur
    low = copy.deepcopy(base)
    assert xprice.price(low, px, refs) == "low" and low["xwhy"] == "price" and not CB.viable(low) and low["prob"] < 0.76 and low["min_odds"] > low["xodds"]
    # écart modèle-marché énorme : veto, même à cote généreuse
    far = prono(p0=0.88, name="Over", point=1.5, alts=[["Over", 1.5, 0.88, None]]) | {"psh": sharp}
    assert xprice.price(far, px, refs) == "low" and far["xveto"] == 1 and far["xwhy"] == "veto" and not CB.viable(far) and far["p_model"] == 0.88
    # un seul côté de la ligne chez 1xBet et pas de sharp : aucun avis de marché -> écarté
    one = copy.deepcopy(base)
    assert xprice.price(one, {("totals", "Over", 1.5): 1.45}) == "low" and one["xveto"] == 1 and one["xwhy"] == "no_market" and not CB.viable(one)
    # 1N2 et les 2 marquent : les autres familles actives
    h = prono(market="h2h", name="H1", point=None, p0=0.62, alts=[["H1", None, 0.62, None]])
    h3 = {("h2h", "H1", None): 1.8, ("h2h", "Draw", None): 3.7, ("h2h", "A1", None): 4.6}
    assert xprice.price(h, h3, refs_of(h3)) == "weak" and h["p_model"] == 0.62 and h["prob"] == h["p_ens"] < C.VIABLE_MIN_P and not CB.viable(h)
    # le même pronostic, ensemble éteint ou famille retirée : le chemin d'avant, la proba du modèle
    for off in ("flag", "family"):
        d = copy.deepcopy(base)
        if off == "flag":
            C.ENSEMBLE_ENABLED = False
        else:
            C.ENSEMBLE_ACTIVE_FAMILIES = frozenset({"corners"})
        try:
            assert xprice.price(d, px, refs) == "value" and d["prob"] == 0.76 and d["xodds"] == 1.45 and "xveto" not in d
        finally:
            reset()
    print("ok  famille décidée par l'ensemble : prob = proba retenue, p_model gardée, veto, pas de valeur sans avis de marché ; éteinte : chemin d'avant")


def inactive_scenario(rng):
    market = rng.choice(["alternate_totals_corners", "alternate_totals_cards", "team_shots_total", "team_sot_home", "team_fouls_total"])
    lines = [("Over", 7.5), ("Over", 9.5), ("Under", 9.5), ("Under", 11.5)]
    alts = [[n, pt, round(rng.uniform(0.45, 0.85), 4), rng.choice([None, round(rng.uniform(0.4, 0.7), 4)])] for n, pt in lines if rng.random() < 0.8]
    name, point = rng.choice(lines)
    px = {(market, n, pt): round(rng.uniform(1.15, 2.3), 2) for n, pt in lines if rng.random() < 0.85}
    c = prono(market=market, name=name, point=point, p0=round(rng.uniform(0.5, 0.85), 4), alts=alts or None)
    if not alts:
        c.pop("alts")
    return c, px, refs_of(px)


def test_mixed_defaults_and_inactive_families():
    """Familles non décidées par l'ensemble (corners, cartons, tirs, fautes...) : décisions IDENTIQUES à l'ensemble éteint, avis fantômes en plus."""
    reset()
    rng = random.Random(41)
    seen = {"value": 0, "low": 0, "weak": 0, None: 0}
    for i in range(500):
        c, px, refs = inactive_scenario(rng)
        a, b = copy.deepcopy(c), copy.deepcopy(c)
        C.ENSEMBLE_ENABLED = False
        try:
            ra = xprice.price(a, px, refs)
            ca = xprice.choose(copy.deepcopy(c), px)
        finally:
            C.ENSEMBLE_ENABLED = True
        rb = xprice.price(b, px, refs)
        assert ra == rb and xprice.choose(copy.deepcopy(c), px) == ca, (i, ra, rb)
        assert {k: v for k, v in b.items() if k not in SHADOW} == a, (i, a, b)
        seen[ra] += 1
        if rb is not None:                             # avis fantômes : le modèle (prob), 1xBet sans marge, la proba qu'aurait retenue l'ensemble
            assert b["p_model"] == b["prob"] and b["p_ens"] is not None
            p_x = E.x_fair(px, b["market"], b["name"], b["point"])
            assert (b.get("p_x") == (None if p_x is None else round(p_x, 4))) and "p_sharp" not in b
            assert abs(b["p_ens"] - E.combine(E.family(b["market"]), b["prob"], p_x)[0]) < 1e-4
            assert E.explain_c(b) == ("" if p_x is None else E.explain_c(b)) and "retenu" not in E.explain_c(b), "corners & co : jamais de « retenu »"
    assert all(seen[k] > 30 for k in ("value", "low", "weak")) and seen[None] > 5, seen
    # une décision d'avant qui n'aurait JAMAIS passé l'ensemble (écart énorme, aucun avis sharp) reste « value »
    c = prono(market="alternate_totals_corners", name="Under", point=10.5, p0=0.75, alts=[["Under", 10.5, 0.75, None]])
    px = pair("alternate_totals_corners", "Under", 10.5, 1.55, 2.45)
    assert xprice.price(c, px, refs_of(px)) == "value" and c["prob"] == 0.75 and "xveto" not in c and c["p_ens"] < 0.75 and CB.viable(c)
    # apply() : « veto » n'existe que s'il y a des pronostics dont l'ensemble décide
    only = [prono(market="alternate_totals_corners", name="Under", point=10.5, p0=0.75, alts=[["Under", 10.5, 0.75, None]])]
    found = {"m1": {"gid": 900, "px": px, "refs": refs_of(px)}}
    with Quiet():
        out = xprice.apply(only, fetch=lambda *a, **k: found)
        assert set(out) == {"matches", "value", "low", "weak", "none"} and out["value"] == 1
        mixed = xprice.apply(only + [prono(i=2)], fetch=lambda *a, **k: found)
    assert "veto" in mixed
    # le relevé suivant ne cumule pas les avis : un pronostic relu perd ses avis du relevé précédent avant d'être revalorisé
    again = copy.deepcopy(only[0])
    again |= {"p_sharp": 0.9, "p_ens": 0.9}
    with Quiet():
        xprice.apply([again], fetch=lambda *a, **k: found)
    assert "p_sharp" not in again and again["p_ens"] < 0.75
    reset()
    print("ok  mode mixte : 500 scénarios sur corners / cartons / tirs / fautes = décisions identiques à l'ensemble éteint, avis fantômes seulement")


def test_viable_reads_the_ensemble_outcome():
    reset()
    # famille décidée : VIABLE_MIN_P s'applique à la proba retenue ; la référence d'un match moyen se compare à la proba du MODÈLE
    c = prono(i=1, p0=0.80) | {"prob": 0.66, "p_ens": 0.66, "p_model": 0.80, "base": 0.70, "xodds": 1.6, "min_odds": 1.56}
    assert CB.viable(c), "modèle 0,80 >= référence 0,70 : p_ens (0,66) n'a pas à passer la référence du modèle"
    assert not CB.viable(c | {"p_model": 0.68}), "le modèle lui-même sous la référence : écarté comme avant"
    assert not CB.viable(c | {"prob": 0.57, "p_ens": 0.57}), "VIABLE_MIN_P (0,58) sur la proba retenue"
    assert not CB.viable(c | {"xveto": 1, "min_odds": 1.7}), "veto / cote mini relevée : écarté"
    # famille non décidée : prob EST la proba du modèle
    k = prono(market="alternate_totals_corners", name="Under", point=10.5, p0=0.75, i=2) | {"base": 0.8, "p_model": 0.75, "p_ens": 0.9}
    assert not CB.viable(k), "corners : prob (0,75) sous la référence (0,80), p_ens (fantôme) n'y change rien"
    C.ENSEMBLE_ENABLED = False
    assert not CB.viable(c | {"p_model": 0.68, "prob": 0.66}) and E.model_prob(c) == 0.66
    reset()
    print("ok  combos.viable : seuil de proba sur la proba retenue, référence comparée au modèle, veto et cote mini refusés")


# ---------- 3. affichage ----------

def test_display_lines_and_legend():
    reset()
    act = prono(i=1, p0=0.71, name="Over", point=2.5, alts=[["Over", 2.5, 0.71, None]]) | {
        "xodds": 1.95, "xev": 0.19, "prob": 0.61, "p_ens": 0.61, "p_model": 0.71, "p_x": 0.49, "min_odds": 1.69, "xat": "09:10"}
    ina = prono(market="alternate_totals_corners", name="Under", point=9.5, i=2, p0=0.71) | {
        "xodds": 1.91, "xev": 0.36, "p_model": 0.71, "p_x": 0.41, "p_ens": 0.55, "xat": "09:10"}
    bare = prono(market="team_fouls_total", name="Over", point=22.5, i=3, p0=0.66) | {"xodds": 1.8, "xev": 0.18}           # aucun avis de 1xBet
    sharp_only = prono(i=4, p0=0.70) | {"xodds": 1.6, "xev": 0.12, "p_sharp": 0.7, "p_model": 0.7}                            # pas de proba 1xBet : pas de ligne
    text = str(fmt.pronos([act, ina, bare, sharp_only], when="09:10"))
    # une ligne de chances sous chaque pari : Kelly (proba retenue) et 1xBet sans marge s'il a un avis ; plus de ligne « 📊 » à part ni de légende
    assert "<i>   Kelly 61 % · 1xBet 49 % · valeur +19 %</i>" in text, text
    assert "<i>   Kelly 71 % · 1xBet 41 % · valeur +36 %</i>" in text, text
    assert "<i>   Kelly 66 % · valeur +18 %</i>" in text and "<i>   Kelly 70 % · valeur +12 %</i>" in text, text      # sans avis de 1xBet : Kelly seul
    assert "📊" not in text and "retenu" not in text and "55 %" not in text, "corners : la proba retenue fantôme n'est jamais montrée"
    assert text.index("Plus de 2,5 buts") < text.index("1xBet 49 %") < text.index("corners")
    assert "Sur les buts et le résultat" not in text and text.count("<i>") == text.count("</i>"), text
    # 5 pronostics 💎 : le message reste court
    assert len(str(fmt.pronos([act | {"id": f"x{i}", "key": f"x{i}"} for i in range(10)]))) < 3500
    # sharp + 1xBet + modèle (avec « retenu ») ; ordre fixe : 1xBet, Sharp, Kelly
    both = act | {"p_sharp": 0.52}
    assert E.explain_c(both) == "1xBet : 49 % · Sharp : 52 % · Kelly : 71 % · retenu : 61 %"
    # jambes d'un combiné à cote juste (💎) et d'un combiné réel, pari du jour, /top
    fl = fmt.fair_combo(3.0, [act, ina | {"id": "m9"}], 0.3, 0.5)
    assert "   <i>Kelly 61 % · 1xBet 49 %</i>" in fl and "   <i>Kelly 71 % · 1xBet 41 %</i>" in fl and "📊" not in fl, fl
    real = {"key": "e1|h2h||Home1", "id": "e1", "sport_key": "soccer_epl", "group": "soccer", "event": "Home1 - Away1", "home": "Home1", "away": "Away1",
            "league": "EPL", "start": "19:00", "commence": KO, "market": "h2h", "who": None, "point": None, "name": "Home1", "pick": "Home1 gagne",
            "prob": 0.5, "prob_book": 0.5, "prob_src": "pinnacle", "n_sharp": 1, "odds": 2.1, "xodds": 2.1, "best_odds": 2.1, "book": "1xBet", "edge": 0.05,
            "spread": 0, "steam": 0, "weight": 1.0, "n_books": 10, "score": 0.1, "why": "", "p_x": 0.46, "p_sharp": 0.5}
    assert "<i>Kelly 50 % · 1xBet 46 %</i>" in str(fmt.single(real, "value")) and "<i>Kelly 50 % · 1xBet 46 %</i>" in str(fmt.combo("🛡️ SÛR", [real, real | {"key": "k2", "id": "e2"}], 4.4, 0.25))
    top = str(app.top_reply([real], [act | {"id": "m7"}, ina | {"id": "m8"}], "09:10", "09:10"))
    assert top.count(" · 1xBet ") == 3 and "📊" not in top, top
    # l'affichage ne dépend plus de l'interrupteur de l'ensemble : jamais de « 📊 » ni de « retenu », mêmes lignes de chances
    C.ENSEMBLE_ENABLED = False
    off = str(fmt.pronos([act, ina], when="09:10"))
    assert "📊" not in off and "Sur les buts" not in off and "retenu" not in off and "Kelly 61 % · 1xBet 49 %" in off, off
    C.ENSEMBLE_ENABLED, C.ENSEMBLE_ACTIVE_FAMILIES = True, frozenset()
    ghost = str(fmt.pronos([act, ina], when="09:10"))
    assert ghost == off, ghost
    reset()
    print("ok  affichage : ligne de chances sous chaque pari (Kelly, 1xBet s'il a un avis), plus de ligne 📊 ni de « retenu » ni de légende")



# ---------- 4. heure de lecture de The Odds API ----------

def sharp_event(i, home_price=2.0, start=KO, read_at=None, books=None):
    """Événement The Odds API du match e<i> : 1X2 chez Pinnacle (référence) et trois books ordinaires."""
    def bk(key, title, h, d, a):
        return {"key": key, "title": title, "markets": [{"key": "h2h", "outcomes": [
            {"name": f"Home{i}", "price": h}, {"name": "Draw", "price": d}, {"name": f"Away{i}", "price": a}]}]}
    ev = {"id": f"e{i}", "sport_key": "soccer_epl", "home_team": f"Home{i}", "away_team": f"Away{i}", "commence_time": start,
          "bookmakers": books or [bk("pinnacle", "Pinnacle", home_price, 3.6, 4.0), bk("bk1", "Book1", 1.95, 3.4, 3.9),
                                  bk("bk2", "Book2", 1.9, 3.5, 3.8), bk("bk3", "Book3", 1.92, 3.3, 3.95)]}
    return ev | ({"read_at": read_at} if read_at else {})


def test_odds_records_the_real_read_time():
    reset()
    base = float(int(time.time()))                          # horloge réelle : le nettoyage du cache disque (prune) supprimerait tout fichier plus vieux que l'horloge simulée
    stamp0 = odds.read_stamp(base)
    clock = [base]
    old = (cache.clock, odds.get, odds.model_covered)
    cache.clock = lambda: clock[0]
    paid = []

    def fake_get(path, use=None, **params):
        if path.endswith("/odds") and "/events/" not in path:
            paid.append(path)
            return [sharp_event(1)]
        if "/events/" in path:                             # marché détaillé d'un match
            paid.append(path)
            return {"bookmakers": [{"key": "pinnacle", "title": "Pinnacle", "markets": [{"key": params["markets"], "outcomes": [
                {"name": "Over", "point": 9.5, "price": 1.9}, {"name": "Under", "point": 9.5, "price": 1.95}]}]},
                {"key": "bk1", "title": "Book1", "markets": [{"key": params["markets"], "outcomes": [
                    {"name": "Over", "point": 9.5, "price": 1.85}, {"name": "Under", "point": 9.5, "price": 2.0}]}]},
                {"key": "bk2", "title": "Book2", "markets": [{"key": params["markets"], "outcomes": [
                    {"name": "Over", "point": 9.5, "price": 1.88}, {"name": "Under", "point": 9.5, "price": 1.98}]}]}]}
        raise AssertionError(path)
    odds.get, odds.model_covered = fake_get, lambda ev: True
    scan = {"day": DAY, "sports": ["soccer_epl"], "leagues": {"soccer_epl": [sharp_event(1)]}, "complete": True, "error": None, "failed": [], "paid": []}
    day = date(2030, 1, 1)
    try:
        with Quiet():
            evs = odds.fetch_events(day, props=False, scan=dict(scan, paid=[]))
        assert len(paid) == 1 and evs and evs[0]["read_at"] == stamp0, (paid, evs[0].get("read_at"))
        picks = odds.build_picks(evs, day)
        assert picks and {c["sharp_at"] for c in picks} == {stamp0}
        t0 = datetime.fromtimestamp(base, tz=UTC)
        assert sharpgap.sharp_age(picks[0], t0 + timedelta(minutes=60), history={}) == 60, "l'âge d'un pari vient de sharp_at, pas de l'historique"
        assert E.sharp_for({"market": "h2h", "psh": {"home1|None": [0.5, "pinnacle", 1, picks[0]["sharp_at"]]}}, "Home1", None, t0 + timedelta(minutes=60)) == 0.5
        assert E.sharp_for({"market": "h2h", "psh": {"home1|None": [0.5, "pinnacle", 1, picks[0]["sharp_at"]]}}, "Home1", None, t0 + timedelta(hours=4)) is None
        # relu du cache trois heures plus tard : l'heure est celle de la VRAIE lecture (04:30), pas celle de la relecture
        clock[0] = base + 3 * 3600
        with Quiet():
            again = odds.fetch_events(day, props=False, scan=dict(scan, paid=[]))
        assert len(paid) == 1, "relu du cache : aucun crédit"
        assert {c["sharp_at"] for c in odds.build_picks(again, day)} == {stamp0}
        # marchés détaillés : leur propre heure de lecture
        clock[0] = base + 4 * 3600
        store.save("budget.json", {})
        with Quiet():
            full = odds.fetch_events(day, props=True, scan=dict(scan, paid=[]))
        assert len(paid) > 1 and full[0].get("read_m"), (paid, full[0].get("read_m"))
        m, stamp = next(iter(full[0]["read_m"].items()))
        assert stamp == odds.read_stamp(base + 4 * 3600), stamp
        pk = [c for c in odds.build_picks(full, day) if c["market"] == m]
        assert pk and {c["sharp_at"] for c in pk} == {stamp} and {c["sharp_at"] for c in odds.build_picks(full, day) if c["market"] == "h2h"} == {stamp0}
    finally:
        cache.clock, odds.get, odds.model_covered = old
    # un événement sans heure (pas de lecture connue) : pas de sharp_at, l'historique reste le repli
    assert all("sharp_at" not in c for c in odds.build_picks([sharp_event(2)], day))
    reset()
    print("ok  odds : sharp_at = heure de la vraie lecture (cache compris, marchés détaillés à part), absente quand elle est inconnue")


# ---------- 5. journal des avis, règlement, apprentissage ----------

class StubFootballData:
    """Règlement gratuit simulé : 2-1, 12 corners (aucun CSV ni réseau)."""

    def match(self, b):
        return None

    def score(self, b):
        return {"id": b["id"], "completed": True, "scores": [{"name": b["home"], "score": "2"}, {"name": b["away"], "score": "1"}]}

    def stats(self, b):
        return {"corners": 12.0, "cards": 3.0}

    def fotmob_score(self, b):
        return None


def priced_pronos():
    """Un pronostic de buts (famille décidée) et un de corners (fantôme), remis au prix 1xBet : leurs avis sont posés."""
    g = prono(i=1, p0=0.76, name="Under", point=3.5, alts=[["Under", 3.5, 0.76, None]]) | {"psh": {"under|3.5": [0.72, "pinnacle", 1]}}
    c = prono(market="alternate_totals_corners", name="Over", point=9.5, i=2, p0=0.70, alts=[["Over", 9.5, 0.70, None]])
    pg, pc = pair("totals", "Under", 3.5, 1.5, 2.7), pair("alternate_totals_corners", "Over", 9.5, 1.9, 1.95)
    assert xprice.price(g, pg, refs_of(pg)) == "value" and xprice.price(c, pc, refs_of(pc)) == "value"
    return [g, c]


def test_journal_settlement_and_learning():
    reset()
    pronos = priced_pronos()
    bare = prono(i=3, p0=0.7) | {"xodds": 1.5}                                  # aucun avis de marché : rien à apprendre
    with Quiet():
        n = app.log_opinions(pronos + [bare], DAY, NOW)
    log = store.load(E.LOG_FILE, [])
    assert n == 2 and len(log) == 2 and {r["market"] for r in log} == {"totals", "alternate_totals_corners"}, log      # toutes les familles, pas seulement les décidées
    g = next(r for r in log if r["market"] == "totals")
    assert g["pm"] == 0.76 and g["ps"] == 0.72 and g["px"] is not None and g["result"] is None and g["date"] == DAY and all(k in g for k in fair.ROW)
    c = next(r for r in log if r["market"] == "alternate_totals_corners")
    assert c["pm"] == 0.70 and c["px"] is not None and c["ps"] is None
    # relevé de 15:00 : mêmes clés ; la lecture qui a servi la décision reste (px d'origine), la relecture tardive va dans « late » ; un match commencé n'est plus touché
    px0 = g["px"]
    pronos[0]["p_x"] = 0.6
    with Quiet():
        assert app.log_opinions(pronos, DAY, at(14)) == 0
    g = next(r for r in store.load(E.LOG_FILE, []) if r["market"] == "totals")
    assert g["px"] == px0 and g["ps"] == 0.72 and g["pm"] == 0.76 and g["late"]["px"] == 0.6 and len(store.load(E.LOG_FILE, [])) == 2, g
    # un avis absent de la nouvelle lecture (1xBet muet, Pinnacle encore là) ne ternit pas l'ancien
    pronos[0].pop("p_x")
    pronos[0]["xp"] = None
    with Quiet():
        assert app.log_opinions(pronos, DAY, at(15)) == 0
    g = next(r for r in store.load(E.LOG_FILE, []) if r["market"] == "totals")
    assert g["px"] == px0 and g["ps"] == 0.72, g
    pronos[0]["p_x"] = 0.3
    with Quiet():
        assert app.log_opinions([pronos[0]], DAY, datetime(2030, 1, 1, 19, 30, tzinfo=UTC)) == 0
    assert next(r for r in store.load(E.LOG_FILE, []) if r["market"] == "totals")["px"] == px0, "match commencé : journal figé"
    pronos[0]["p_x"] = 0.6
    # règlement gratuit le lendemain, apprentissage, idempotence
    old_fd = settle.FootballData
    settle.FootballData = StubFootballData
    try:
        later = datetime(2030, 1, 2, 4, 0, tzinfo=UTC)
        with Quiet():
            done, learned = settle.settle_ensemble(later)
            again = settle.settle_ensemble(later)
        assert len(done) == 2 and learned == 2 and again == ([], 0), (done, learned, again)
        res = {r["market"]: r["result"] for r in store.load(E.LOG_FILE, [])}
        assert res == {"totals": "win", "alternate_totals_corners": "win"}, res                    # 3 buts < 3,5 ; 12 corners > 9,5
        assert all(r.get("ens") for r in store.load(E.LOG_FILE, []))
        lines = E.lines()
        assert len(lines) == 2 and any(l.startswith("buts : Kelly compte pour 12 %") for l in lines) and any(l.startswith("corners : Kelly compte pour 50 %") for l in lines), lines
        # statistiques : le règlement du journal ne demande JAMAIS rien de plus à API-Football (cache seulement), quota intact
        reset()
        pronos = priced_pronos()
        with Quiet():
            app.log_opinions(pronos, DAY, NOW)
        asked, fx = [], {"id": 77, "home_id": 1, "away_id": 2, "status": "FT"}
        real_api = (stats.enabled, stats.find_fixture, stats.cached_stats, stats.fixture_stats, settle.football_score, settle.fmmatch.match_stats)
        stats.enabled, stats.find_fixture = (lambda: True), (lambda *a, **k: fx)
        stats.cached_stats = lambda fid: {"corners": 12.0, "teams": {"1": {"corners": 7.0}, "2": {"corners": 5.0}}}
        stats.fixture_stats = lambda *a, **k: asked.append(a) or None
        settle.football_score, settle.fmmatch.match_stats = (lambda b: None), (lambda b: None)

        class NoCsv(StubFootballData):
            def stats(self, b):
                return None
        settle.FootballData = NoCsv
        try:
            with Quiet():
                done, learned = settle.settle_ensemble(later)
            assert asked == [] and {r["market"]: r["result"] for r in store.load(E.LOG_FILE, [])} == {"totals": "win", "alternate_totals_corners": "win"}, (asked, store.load(E.LOG_FILE, []))
        finally:
            stats.enabled, stats.find_fixture, stats.cached_stats, stats.fixture_stats, settle.football_score, settle.fmmatch.match_stats = real_api
            settle.FootballData = StubFootballData
        # une ligne qui plante (stats introuvables avec une source en panne) ne bloque ni les autres ni le passage
        reset()
        pronos = priced_pronos()
        with Quiet():
            app.log_opinions(pronos, DAY, NOW)

            def boom(b):
                raise RuntimeError("source en panne")
            done, learned = settle.settle_ensemble(later, stats_for=boom)
        assert [r["market"] for r in done] == ["totals"] and learned == 1, (done, learned)
        assert [r["result"] for r in store.load(E.LOG_FILE, []) if r["market"] != "totals"] == [None], "la ligne en échec reste en attente"
        # journal éteint : rien n'est écrit, rien n'est appris
        reset()
        C.ENSEMBLE_LOG = False
        with Quiet():
            assert app.log_opinions(pronos, DAY, NOW) == 0 and settle.settle_ensemble(later) == ([], 0)
        assert store.load(E.LOG_FILE, []) == []
        C.ENSEMBLE_LOG = True
        # jamais d'exception : le journal en panne ne casse ni le relevé ni le matin
        real_record = E.record
        E.record = lambda *a, **k: 1 / 0
        try:
            with Quiet():
                assert app.log_opinions(pronos, DAY, NOW) == 0
        finally:
            E.record = real_record
        real_settle = settle.settle_ensemble
        settle.settle_ensemble = lambda *a, **k: 1 / 0
        try:
            with Fakes(), Quiet():
                msgs, model = app.morning_report(at(8))
            assert isinstance(msgs, list)
        finally:
            settle.settle_ensemble = real_settle
    finally:
        settle.FootballData = old_fd
    reset()
    print("ok  journal des avis : toutes les familles, règlement gratuit + apprentissage idempotents, ligne en échec isolée, journal éteint, jamais d'exception")


def test_closing_measures_the_edge_against_pinnacle():
    reset()
    e = [sharp_event(1)]
    picks = odds.build_picks(e, date(2030, 1, 1))
    home = next(c for c in picks if c["name"] == "Home1")
    row_pick = home | {"xodds": 2.4, "xref": [901, 1, 0], "typ_odds": home["odds"], "typ_edge": home["edge"], "typ_book": "Book1", "typ_best": 2.0,
                       "typ_heur": home["score"]}
    cand = {"key": home["key"], "n": 1, "batch": "b1", "edge": 0.1, "p_sharp": home["prob_book"], "src": "pinnacle", "n_sharp": 1,
            "fair_odds": 2.0, "xodds": 2.4, "xref": [901, 1, 0]}
    other = next(c for c in picks if c["name"] == "Away1")                    # un pari ordinaire suivi : pas de mesure « gap »
    track.record([row_pick, other], {row_pick["key"], other["key"]}, DAY, NOW, single_key=other["key"])
    track.record_gap([(row_pick, cand, 500, "ABC")], DAY, NOW)
    rows = track.load()
    r = next(x for x in rows if x["key"] == home["key"])
    assert r["sharpgap"] and r["gap_n"] == 1 and r["sent"] and r["single"] is False and r["xodds"] == 2.4 and r["stake"] == 500 and r["coupon"] == "ABC", r
    assert r["gap"]["edge"] == 0.1 and r["cur"] == money.cur()
    assert not next(x for x in rows if x["key"] == other["key"]).get("sharpgap") and next(x for x in rows if x["key"] == other["key"])["single"] is True
    # clôture payante : cote prise x proba de clôture de Pinnacle - 1
    closing = odds.build_picks([sharp_event(1, home_price=2.2)], date(2030, 1, 1))
    assert track.close(rows, closing, at(18)) == 2
    r = next(x for x in rows if x["key"] == home["key"])
    p_close = next(c for c in closing if c["name"] == "Home1")["prob_book"]
    assert r["gap_close_src"] == "pinnacle" and abs(r["gap_edge_close"] - (2.4 * p_close - 1)) < 1e-3 and r["clv"] is not None
    assert "gap_edge_close" not in next(x for x in rows if x["key"] == other["key"]), "seuls les paris 1xBet en retard mesurent contre Pinnacle"
    # clôture gratuite (football-data, Pinnacle) d'un pari manqué par le relevé payant
    r2 = dict(r) | {"key": "e1|h2h||Home1b", "clv": None, "close_odds": None, "close_prob": None, "closed_at": "missed", "gap_edge_close": None,
                    "gap_close_src": None, "commence": "2029-12-30T19:00:00Z", "date": "2029-12-30", "prob_book": 0.5}
    r2.pop("gap_edge_close"), r2.pop("gap_close_src")

    class FD:
        def match(self, row):
            return {"odds": {"1x2": {"close": {"ps": [2.0, 3.6, 4.0]}}}}
    assert freeclose.fill([r2], datetime(2030, 1, 2, 8, tzinfo=UTC), fdata=FD(), bx={}) == 1
    p_free = E.devig([2.0, 3.6, 4.0])[0]
    assert r2["gap_close_src"] == "pinnacle" and abs(r2["gap_edge_close"] - (2.4 * p_free - 1)) < 1e-3
    # moyenne de football-data (pas Pinnacle) : la ligne est clôturée mais ne compte pas comme « contre Pinnacle »
    r3 = dict(r2) | {"key": "x3", "clv": None, "closed_at": None, "close_prob": None, "prob_src": "consensus"}     # pris au consensus : comparable à la moyenne de clôture
    r3.pop("gap_edge_close", None), r3.pop("gap_close_src", None)

    class FDavg:
        def match(self, row):
            return {"odds": {"1x2": {"close": {"avg": [2.0, 3.6, 4.0]}}}}
    assert freeclose.fill([r3], datetime(2030, 1, 2, 8, tzinfo=UTC), fdata=FDavg(), bx={}) == 1 and "gap_edge_close" not in r3
    reset()
    print("ok  clôture : l'écart contre Pinnacle (payante et gratuite) est mesuré sur les seuls paris 1xBet en retard")



# ---------- 6. « 💰 1xBet paie plus que le marché » : le matin ----------

HOME_P = None
ODDS_AT = "08:55"                                  # heure de la lecture des cotes 1xBet du matin (avant NOW)


def home_p():
    """Proba sans marge de l'équipe à domicile pour le consensus de sharp_event (la même pour tous les matchs d'essai)."""
    global HOME_P
    if HOME_P is None:
        HOME_P = next(c for c in odds.build_picks([sharp_event(1)], date(2030, 1, 1)) if c["name"] == "Home1")["prob_book"]
    return HOME_P


def home_price(edge):
    """Cote 1xBet de l'équipe à domicile qui donne l'écart `edge` face à la proba des books de référence."""
    return round((1 + edge) / home_p(), 2)


def px_of(i, price):
    return {("h2h", f"Home{i}", None): price, ("h2h", "Draw", None): 3.5, ("h2h", f"Away{i}", None): 3.9}


def x_fetch(prices, calls=None):
    """1xBet simulé : prices = {id Kelly: cote de l'équipe à domicile} (les matchs absents sont introuvables)."""
    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        if calls is not None:
            calls.append(sorted(ev["id"] for _, ev in events))
        out = {}
        for _, ev in events:
            if ev["id"] in prices:
                i = int(ev["id"][1:])
                px = px_of(i, prices[ev["id"]])
                out[ev["id"]] = {"gid": 9000 + i, "px": px, "refs": refs_of(px, 9000 + i), "cid": 70000 + i}
        return out
    return fetch


def gap_world(edges=(0.10, 0.07, 0.05, 0.04), read_at=None, read_minutes=20):
    """Un match par écart de `edges` : 1X2 chez Pinnacle et trois books lus il y a `read_minutes` min, puis remis au prix 1xBet. -> paris."""
    stamp = read_at or odds.read_stamp(NOW.timestamp() - read_minutes * 60)
    events = [sharp_event(i, read_at=stamp) for i in range(1, len(edges) + 1)]
    picks = odds.build_picks(events, date(2030, 1, 1))
    with Quiet():
        out = xprice.price_picks(picks, fetch=x_fetch({f"e{i}": home_price(e) for i, e in enumerate(edges, 1)}))
    assert out["priced"] == 3 * len(edges), out
    return picks


def morning(picks, gaps=None, gap_out=None, **kw):
    msgs, sent_picks, sent_combos = app.build_day(picks, DAY, None, [], None, [], {}, ODDS_AT, {}, gaps=gaps, gap_out=gap_out, **kw)
    return msgs, sent_picks


def ids(entries):
    return [(e[1] if isinstance(e, tuple) else e)["key"].split("|")[0] for e in entries]


def test_morning_gap_section_and_tracking():
    reset()
    money.set_daily(10000)
    picks = gap_world()
    with Fakes() as f:
        cands = sharpgap.find({"day": DAY, "picks": picks, "pronos": [], "picks_at": ODDS_AT}, NOW)
        assert ids(cands) == ["e1", "e2", "e3", "e4"] and [round(x["edge"], 2) for x in cands] == [0.10, 0.07, 0.05, 0.04], [(x["key"], x["edge"]) for x in cands]
        top = max(CB.make_pool(picks), key=lambda c: c["score"])
        assert top["key"] == cands[0]["key"], "le plus gros écart est aussi le pari du jour : il garde son message"
        rec = []
        msgs, sent_picks = morning(picks, cands, rec)
        gm = [m for m in msgs if "1xBet paie plus que le marché" in str(m)]
        assert len(gm) == 1, [str(m)[:60] for m in msgs]
        msg, text = gm[0], fmt.plain(str(gm[0]))
        # contenu : l'explication, un bloc par pari (match, cotes, âge du relevé, mise, code coupon), les boutons « ✅ Je l'ai joué » g1..g3
        assert text.startswith("💰 1xBet paie plus que le marché · 08:55\nLa cote 1xBet est au-dessus de la cote juste des books de référence (Pinnacle, marge retirée)")
        assert "Home2 – Away2" in text and "Home2 gagne — 1xBet 2,18 · juste 2,04 (Pinnacle) · +7,0 %" in text and "relevé sharp il y a 20 min · match dans 600 min" in text
        assert "Home1" not in text, "le pari du jour (e1) n'est pas repris"
        assert text.count("🎟️ Code coupon 1xBet : CODE") == 3 and text.count("💰 Mise conseillée : ") == 3 and "pas de mise" not in text
        assert [("e2", cands[1]["xodds"])] in f.coupons and [("e4", cands[3]["xodds"])] in f.coupons, "un code coupon par pari seul, à la cote 1xBet lue"
        assert cbs(msg) == [f"joue|g1|{DAY}", f"joue|g2|{DAY}", f"joue|g3|{DAY}"] and all(len(c.encode()) <= 64 for c in cbs(msg))
        assert all(l.startswith("✅ Je l'ai joué : Home") and " — " in l and "FCFA" in l for l in labels(msg)), labels(msg)
        for word in fmt.JARGON:
            assert word not in text.lower(), word
        assert msg.count("<i>") == msg.count("</i>") and msg.count("<b>") == msg.count("</b>") and len(msg) < 3500
        # place dans le matin : juste après l'en-tête, avant le pari du jour
        order = [i for i, m in enumerate(msgs) if "1xBet paie plus que le marché" in str(m) or "PARI DU JOUR" in str(m) or str(m).startswith("<b>🔥 Kelly")]
        assert str(msgs[order[0]]).startswith("<b>🔥 Kelly") and "1xBet paie plus que le marché" in str(msgs[order[1]]) and "PARI DU JOUR" in str(msgs[order[2]]), order
        # lignes à suivre : mise du budget du jour (répartie avec les autres paris), code ; le pari du jour est seulement marqué
        assert ids(rec) == ["e1", "e2", "e3", "e4"] and rec[0][1].get("n") is None and rec[0][3] is None
        shown = rec[1:]
        assert [(x["n"], x["batch"]) for _, x, _, _ in shown] == [(1, "b1"), (2, "b1"), (3, "b1")]
        assert all(st and st > 0 for _, _, st, _ in shown) and [code for *_, code in shown] == ["CODE1", "CODE2", "CODE3"]
        assert sum(st for _, _, st, _ in shown) + sent_picks[0]["stake"] <= money.daily(), "les mises sont prises sur le MÊME budget du jour que les autres paris"
        # suivi : après track.record (le pari du jour garde sa marque), les écarts deviennent des lignes ENVOYÉES numérotées
        track.record(picks, {c["key"] for c in sent_picks}, DAY, NOW, single_key=app.single_key(sent_picks))
        assert track.record_gap(rec, DAY, NOW, read_at=ODDS_AT) == 4
        rows = track.load()
        gaps = played.gap_rows(rows)
        assert [(r["gap_n"], r["key"].split("|")[0]) for r in gaps] == [(1, "e2"), (2, "e3"), (3, "e4")] and all(r["sent"] and r["result"] is None for r in gaps)
        assert all(r["single"] is False and r["sharpgap"] and r["gap"]["batch"] == "b1" and r["gap"]["p_sharp"] and r["gap"]["src"] == "pinnacle" for r in gaps)
        assert all(r["stake"] > 0 and r["cur"] == money.cur() and r["coupon"] and r["xodds"] and r["xref"] for r in gaps)
        single = next(r for r in rows if r.get("single") is True)
        assert single["key"] == top["key"] and single["sharpgap"] and "gap_n" not in single and single["gap"]["edge"] == cands[0]["edge"], "pari du jour : marqué, pas numéroté"
        assert single not in gaps and len({r["key"] for r in rows}) == len(rows)
        assert sharpgap.sent_keys(DAY) == {x["key"] for x in cands[1:]}
        # leurs clôtures sont relevées par le passage horaire, dans le budget « closing » et avec la priorité des paris envoyés
        due, missed = track.pending_closing(rows, datetime(2030, 1, 1, 18, 0, tzinfo=UTC))
        assert {r["key"] for r in gaps} <= {r["key"] for r in due}, "match dans 60 min : clôture due"
        calls = app.plan_closing(due, [])
        assert calls and all(c["cost"] <= 3 for c in calls) and min(c["rank"] for c in calls) == (-1, 0)
        # les mises des écarts ne se mêlent pas à celles des combinés à vérifier (fair_stakes)
        fs = {}
        app.build_day(picks, DAY, None, [], None, [], fs, ODDS_AT, {}, gaps=cands, gap_out=[])
        assert fs == {}, fs
        # relance du matin : mêmes numéros, même message, pas de ligne en double
        rec2 = []
        msgs2, _ = morning(picks, cands, rec2)
        assert cbs(next(m for m in msgs2 if "1xBet paie plus que le marché" in str(m))) == cbs(msg) and [x.get("n") for _, x, _, _ in rec2] == [None, 1, 2, 3]
        track.record_gap(rec2, DAY, NOW, read_at=ODDS_AT)
        assert len(played.gap_rows(track.load())) == 3 and len(track.load()) == len(rows)
    reset()
    print("ok  matin : section « 1xBet paie plus que le marché » (code, mise, boutons g1..g3) après l'en-tête, pari du jour marqué sans doublon, lignes suivies et numérotées, relance stable")


def test_morning_gap_edge_cases():
    reset()
    picks = gap_world()
    cands = sharpgap.find({"day": DAY, "picks": picks, "pronos": [], "picks_at": ODDS_AT}, NOW)
    with Fakes() as f:
        # sans budget du jour : aucune mise inventée, une ligne dit de le régler
        rec = []
        msgs, _ = morning(picks, cands, rec)
        gmsg = next(m for m in msgs if "1xBet paie plus que le marché" in str(m))
        gm = fmt.plain(str(gmsg))
        assert "💰 Mise conseillée" not in gm and gm.count("Mise : règle ton budget du jour") == 1 and all(st is None for _, _, st, _ in rec[1:])
        assert all(" — " not in l for l in labels(gmsg)), "pas de mise dans le bouton"
        # au plus C.SHARPGAP_MAX_SHOWN, les plus gros écarts d'abord
        reset()
        C.SHARPGAP_MAX_SHOWN = 2
        rec = []
        morning(picks, cands, rec)
        assert ids(rec) == ["e1", "e2", "e3"], ids(rec)                       # le pari du jour (marqué) puis les 2 plus gros écarts restants
        C.SHARPGAP_MAX_SHOWN = 3
        # au plus C.SHARPGAP_MAX_DAY par jour : le reste n'est pas annoncé
        reset()
        C.SHARPGAP_MAX_DAY = 2
        rec = []
        morning(picks, cands, rec)
        assert len([1 for _, x, _, _ in rec if x.get("n")]) == 2
        C.SHARPGAP_MAX_DAY = 6
    # code coupon refusé par 1xBet : le pari est annoncé quand même, sans code, et suivi sans code
    reset()
    with Fakes(fail_coupon=True) as f:
        rec = []
        msgs, _ = morning(picks, cands, rec)
        gm = fmt.plain(str(next(m for m in msgs if "1xBet paie plus que le marché" in str(m))))
        assert "Code coupon" not in gm and all(code is None for *_, code in rec) and len(rec) == 4
        track.record(picks, {c["key"] for c in picks[:1]}, DAY, NOW, single_key=picks[0]["key"])
        track.record_gap(rec, DAY, NOW, read_at=ODDS_AT)
        assert all(not r.get("coupon") for r in played.gap_rows(track.load())) and len(played.gap_rows(track.load())) == 3
    # une panne pendant la construction du message rend les numéros : rien n'a été annoncé, ces paris restent à annoncer
    reset()
    real_fmt = fmt.gap_message
    fmt.gap_message = lambda *a, **k: 1 / 0
    try:
        with Fakes(), Quiet():
            msgs, _ = morning(picks, cands, [])
        assert not any("1xBet paie plus que le marché" in str(m) for m in msgs) and sharpgap.sent_keys(DAY) == set(), sharpgap.sent_keys(DAY)
    finally:
        fmt.gap_message = real_fmt
    # une panne de la section ne casse JAMAIS le reste du matin
    reset()
    real = gapbets.message
    gapbets.message = lambda *a, **k: 1 / 0
    try:
        with Fakes(), Quiet():
            rec = []
            msgs, sent = morning(picks, cands, rec)
        assert msgs and not any("1xBet paie plus que le marché" in str(m) for m in msgs) and ids(rec) == ["e1"]
    finally:
        gapbets.message = real
    # SHARPGAP_ENABLED=0 / aucun écart : aucune section
    reset()
    C.SHARPGAP_ENABLED = False
    assert app.morning_gaps(picks, [], DAY, ODDS_AT) == []
    C.SHARPGAP_ENABLED = True
    assert app.morning_gaps([], [], DAY, ODDS_AT) == []
    with Fakes():
        assert not any("1xBet paie plus que le marché" in str(m) for m in morning(picks, [])[0])
    reset()
    print("ok  matin : sans budget, au plus N écarts (par message et par jour), coupon refusé, panne de la section isolée, option éteinte")



# ---------- 7. note, argent, « Mes paris », résultats ----------

def morning_state(budget=10000, edges=(0.10, 0.07, 0.05, 0.04)):
    """Le matin joué jusqu'au suivi : -> (paris, messages, ligne de chaque écart annoncé). Doit être appelé sous Fakes()."""
    reset()
    if budget:
        money.set_daily(budget)
    picks = gap_world(edges)
    cands = sharpgap.find({"day": DAY, "picks": picks, "pronos": [], "picks_at": ODDS_AT}, NOW)
    rec = []
    msgs, sent = morning(picks, cands, rec)
    track.record(picks, {c["key"] for c in sent}, DAY, NOW, single_key=app.single_key(sent))
    track.record_gap(rec, DAY, NOW, read_at=ODDS_AT)
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": ODDS_AT})
    return picks, msgs, rec


def test_notes_money_and_lists():
    with Fakes() as f:
        picks, msgs, rec = morning_state()
        gm = next(m for m in msgs if "1xBet paie plus que le marché" in str(m))
        row = lambda n: next(r for r in track.load() if r.get("gap_n") == n)
        # « ✅ Je l'ai joué » sous le message : noté tout de suite à la cote 1xBet lue, à la mise conseillée ; les boutons de TOUT le message sont refaits
        r1 = row(1)
        out = chat.on_callback(f"joue|g1|{DAY}", 77)
        assert out.startswith("✅ Noté") and f"joué à {fmt.odds_txt(r1['xodds'])} chez 1xBet, mise {money.money(r1['stake'])}" in out, out
        p = row(1)["played"]
        assert p["odds"] == r1["xodds"] and p["stake"] == r1["stake"] and row(1)["cur"] == money.cur()
        mid, markup = f.edits[-1]
        assert mid == 77
        lab = labels(markup)
        assert lab[0].startswith("✅ Home2 gagne · joué à ") and "✏️ Autre cote" in lab and "↩️ Annuler" in lab and sum(l.startswith("✅ Je l'ai joué") for l in lab) == 2, lab
        assert set(cbs(markup)) == {"noop", f"joue|g1|{DAY}", f"annule|g1|{DAY}", f"joue|g2|{DAY}", f"joue|g3|{DAY}"} and all(len(c.encode()) <= 64 for c in cbs(markup))
        both = [{"sharpgap": True, "sent": True, "gap_n": 1, "single": True}, {"sharpgap": True, "sent": True, "gap_n": 2, "single": False},
                {"sharpgap": True, "sent": False, "gap_n": 3}, {"sharpgap": True, "sent": True}]
        assert played.gap_rows(both) == [both[1]], "un écart qui est aussi le pari du jour reste noté comme « pari » ; ni non envoyé, ni sans numéro"
        # un double appui juste après ne redemande rien
        assert chat.on_callback(f"joue|g1|{DAY}", 77) == ""
        # « ✏️ Autre cote » puis la cote obtenue ; « ↩️ Annuler » retire la note et remet le bouton
        chat.forget_taps()
        ask = chat.on_callback(f"joue|g1|{DAY}", 77)
        assert "À quelle cote as-tu finalement joué" in ask and chat.get_pending()["what"] == "g1"
        assert chat.route("2,30")[1].startswith("✅ Noté") and row(1)["played"]["odds"] == 2.3 and row(1)["played"]["stake"] == p["stake"]
        assert chat.on_callback(f"annule|g1|{DAY}", 77).startswith("Note annulée") and "played" not in row(1)
        assert sum(l.startswith("✅ Je l'ai joué") for l in labels(f.edits[-1][1])) == 3
        # /joue g2 2,10 1000 : la commande écrite marche aussi
        assert played.command(["g2", "2,10", "1000"], DAY).startswith("✅ Noté") and row(2)["played"] == {"odds": 2.1, "stake": 1000, "at": row(2)["played"]["at"]}
        # « 🧾 Mes paris » : l'écart avec son code coupon, son état et ses boutons ; jamais mélangé au pari du jour
        todo = played.todo(DAY)
        txt = fmt.plain(str(todo))
        assert "▫️ 1xBet en retard : Home2 gagne · cote 1xBet 2,18 (08:55) · mise 1 500 FCFA" in txt and "✅ 1xBet en retard : Home3 gagne · joué à 2,10 · 1 000 FCFA" in txt and "🎟️ CODE" in txt, txt
        assert "Pari du jour" in txt and f"joue|g3|{DAY}|l0" in cbs(todo) and f"annule|g2|{DAY}|l0" in cbs(todo) and f"joue|g1|{DAY}|l0" in cbs(todo)
        assert txt.count("1xBet en retard : ") == 3
        # l'argent : seuls les écarts NOTÉS comptent, au résultat réglé
        rows = track.load()
        for r in rows:
            if r.get("gap_n") == 2:
                r["result"] = "win"
            if r.get("gap_n") == 3:
                r["result"] = "loss"
        track.save(rows)
        bets = store.load("bets.json", {"combos": []})
        n, total, staked = money.result(bets, track.load(), fair.load_combos())
        assert n == 1 and abs(total - 1000 * (2.1 - 1)) < 1e-6 and staked == 1000, (n, total, staked)
        r3 = next(r for r in track.load() if r.get("gap_n") == 3)
        track.save([dict(r, played={"odds": r["xodds"], "stake": 500, "at": "x"}) if r.get("gap_n") == 3 else r for r in track.load()])
        n, total, staked = money.result(bets, track.load(), fair.load_combos())
        assert n == 2 and abs(total - (1000 * 1.1 - 500)) < 1e-6, (n, total)
        # le bilan « Tes paris notés » et l'écart de cote comptent les écarts
        assert any(x[1] == "1xBet en retard" for x in played.items(bets, track.load(), [])) and "Pari du jour" not in [x[1] for x in played.items(bets, track.load(), []) if x[2].get("gap_n")]
        # résultats du lendemain : annoncés une fois, avec le gain des paris notés
        settled = [r for r in track.load() if r.get("gap_n") in (2, 3)]
        for r in settled:
            r["final"] = "2-1"
        track.save(track.load())
        rows_ = track.load()
        res = app.results_message(bets, rows_, settled)
        track.save(rows_)                                   # comme morning_report : la marque « gap_reported » est enregistrée avec le suivi
        plain = fmt.plain(str(res))
        assert "✅ 1xBet paie plus que le marché — cote 2,10 chez 1xBet · +1 100 FCFA" in plain and "❌ 1xBet paie plus que le marché" in plain and "−500 FCFA" in plain, plain
        assert app.results_message(bets, track.load(), settled) is None, "annoncés une seule fois"
        # pas de budget : la mise est demandée (jamais inventée), la cote 1xBet lue est retenue
    with Fakes() as f:
        picks, msgs, rec = morning_state(budget=None)
        out = chat.on_callback(f"joue|g1|{DAY}", 5)
        assert out.startswith("Avec quelle mise en FCFA ?") and "g1" not in out and chat.get_pending()["odds"], out
        assert chat.route("1000")[1].startswith("✅ Noté")
        r = next(r for r in track.load() if r.get("gap_n") == 1)
        assert r["played"]["stake"] == 1000 and r["played"]["odds"] == r["xodds"]
        assert any(l.startswith("✅ Home2 gagne") for l in labels(f.edits[-1][1]))
    # un message d'un seul pari : le bouton « 📋 Copier le code » reste après la note
    with Fakes() as f:
        reset()
        money.set_daily(5000)
        picks = gap_world((0.05, 0.08))
        cands = sharpgap.find({"day": DAY, "picks": picks, "pronos": [], "picks_at": ODDS_AT}, NOW)
        rec = []
        msgs, sent = morning(picks, cands, rec)
        track.record(picks, {c["key"] for c in sent}, DAY, NOW, single_key=app.single_key(sent))
        track.record_gap(rec, DAY, NOW, read_at=ODDS_AT)
        gm = next(m for m in msgs if "1xBet paie plus que le marché" in str(m))
        assert len(cbs(gm)) == 1 and any("copy_text" in b for row in gm.markup["inline_keyboard"] for b in row), gm.markup
        chat.on_callback(f"joue|g1|{DAY}", 9)
        assert any("copy_text" in b for row in f.edits[-1][1]["inline_keyboard"] for b in row)
    reset()
    print("ok  note : bouton « ✅ Je l'ai joué » (clavier du message refait), autre cote / annuler, /joue g2, Mes paris, argent des seuls paris notés, résultats annoncés une fois")



# ---------- 8. le matin complet (run_daily) ----------

def test_run_daily_end_to_end():
    reset()
    money.set_daily(10000)
    edges = (0.10, 0.07, 0.05, 0.04)
    stamp = odds.read_stamp(NOW.timestamp() - 20 * 60)
    events = [sharp_event(i, read_at=stamp) for i in range(1, len(edges) + 1)]
    prices = {f"e{i}": home_price(e) for i, e in enumerate(edges, 1)}
    seen = {}
    names = ("now_utc", "refresh_sources", "morning_report", "fill_profiles", "run_track", "world_report")
    old_app = {n: getattr(app, n) for n in names}
    old = (odds.scan_today, odds.fetch_events, stats.enrich, xworld.extend_scan, xbet.fetch_day, app.sources_message)
    app.now_utc = lambda: NOW
    app.refresh_sources = lambda: {}
    app.morning_report = lambda now: ([], learn.load())
    app.fill_profiles = lambda day, scan: None
    app.run_track = lambda *a, **k: seen.setdefault("track", True)
    app.world_report = lambda scan, picks, day, now: (None, [])
    app.sources_message = lambda picks: ""
    odds.scan_today = lambda day, after=None, **k: {"leagues": {}, "sports": [], "paid": ["soccer_epl"], "complete": True, "error": None, "failed": []}
    odds.fetch_events = lambda day, after=None, **k: events
    stats.enrich = lambda picks, day: picks
    xworld.extend_scan = lambda *a, **k: None
    xbet.fetch_day = x_fetch(prices)
    try:
        with Fakes() as f, Quiet():
            out = app.run_daily()
        assert seen.get("track")
        gm = [m for m in f.sent if "1xBet paie plus que le marché" in str(m)]
        assert len(gm) == 1 and gm == [m for m in out if "1xBet paie plus que le marché" in str(m)], [str(m)[:50] for m in f.sent]
        assert cbs(gm[0]) == [f"joue|g1|{DAY}", f"joue|g2|{DAY}", f"joue|g3|{DAY}"], cbs(gm[0])
        # les paris du jour portent l'heure de VRAIE lecture du sharp et les avis de l'ensemble ; today.json garde de quoi relire
        today = store.load("today.json", {})
        assert today["day"] == DAY and today["picks_at"] == "09:00"
        home = [c for c in today["picks"] if c["name"].startswith("Home")]
        assert len(home) == 4 and all(c["sharp_at"] == stamp and c["p_sharp"] and c["p_x"] and c["xref"] for c in home), home[0]
        # rien payé de plus ; suivi : le pari du jour, les 3 écarts annoncés, numérotés, avec leur code
        rows = track.load()
        gaps = played.gap_rows(rows)
        assert [r["gap_n"] for r in gaps] == [1, 2, 3] and all(r["sent"] and r["coupon"] for r in gaps) and sum(1 for r in rows if r.get("single") is True) == 1
        assert store.load("daily_done.json", {}).get("day") == DAY
        # journal des avis : un 1N2 avec les avis sharp et 1xBet (apprend le poids du sharp) pour chaque issue chiffrée
        log = store.load(E.LOG_FILE, [])
        assert log and all(r["ps"] is not None and r["px"] is not None and r["market"] == "h2h" for r in log), log[:1]
        # le sharp est trop vieux (lecture de 3 h) : plus d'écart annoncé, mais le matin part quand même
        reset()
        money.set_daily(10000)
        old_events = [sharp_event(i, read_at=odds.read_stamp(NOW.timestamp() - 3 * 3600)) for i in range(1, 5)]
        odds.fetch_events = lambda day, after=None, **k: old_events
        with Fakes() as f, Quiet():
            out = app.run_daily()
        assert not any("1xBet paie plus que le marché" in str(m) for m in f.sent) and f.sent and not played.gap_rows(track.load())
        # SHARPGAP_ENABLED=0 : pareil
        reset()
        C.SHARPGAP_ENABLED = False
        odds.fetch_events = lambda day, after=None, **k: events
        with Fakes() as f, Quiet():
            app.run_daily()
        assert not any("1xBet paie plus que le marché" in str(m) for m in f.sent) and f.sent
    finally:
        for n, v in old_app.items():
            setattr(app, n, v)
        odds.scan_today, odds.fetch_events, stats.enrich, xworld.extend_scan, xbet.fetch_day, app.sources_message = old
    reset()
    print("ok  run_daily : lecture sharp datée, section 💰 envoyée, suivi + journal des avis écrits, sharp périmé ou option éteinte = pas de section")


def test_world_report_passes_the_real_picks():
    """Les pronostics reçoivent l'avis sharp des paris réels du jour (xprice.apply(pronos, picks=picks)) : sans cela l'avis sharp ne servirait jamais."""
    reset()
    got = {}
    old = (xprice.apply, fair.make, fair.record)
    xprice.apply = lambda pronos, fetch=None, picks=None: got.update(picks=picks, n=len(pronos))
    fair.make = lambda *a, **k: [prono(i=1)]
    fair.record = lambda *a, **k: 0
    real_xb = (C.SOURCES_REFRESH, C.XBET_ENABLED)
    C.SOURCES_REFRESH = C.XBET_ENABLED = True
    try:
        pk = [{"id": "x", "key": "k"}]
        with Quiet():
            app.world_report({"leagues": {}, "sports": [], "paid": [], "error": "test"}, pk, date(2030, 1, 1), NOW)
        assert got == {"picks": pk, "n": 1}, got
    finally:
        xprice.apply, fair.make, fair.record = old
        C.SOURCES_REFRESH, C.XBET_ENABLED = real_xb
    reset()
    print("ok  world_report : les paris réels du jour sont passés à xprice.apply (avis sharp des pronostics)")



# ---------- 9. relevé de l'après-midi et passage horaire ----------

def afternoon(edges_morning=(0.02, 0.02, 0.02, 0.02), sharp_ages=(30, 30, 30, 30)):
    """today.json de 15:00 : les paris du matin (cote 1xBet du matin à `edges_morning` d'écart), leur relevé sharp lu il y a `sharp_ages` min."""
    now = at(15)
    picks = gap_world(edges_morning, read_at=odds.read_stamp(now.timestamp() - 30 * 60))
    for c, age in zip([c for c in picks if c["name"].startswith("Home")], sharp_ages):
        c["sharp_at"] = odds.read_stamp(now.timestamp() - age * 60)
    today = {"day": DAY, "picks": picks, "pronos": [], "picks_at": "04:30", "xbet_at": "04:30"}
    store.save("today.json", today)
    return today, now


def test_refresh_rechecks_the_live_price():
    reset()
    money.set_daily(10000)
    today, now = afternoon(sharp_ages=(30, 30, 30, 300))               # e4 : lecture sharp de 5 h -> refusé (périmé), jamais relu en direct
    calls = []
    live = {"e1": home_price(0.10), "e2": home_price(0.015)}            # e1 : l'écart est là ; e2 : sous le seuil au prix du moment ; e3 : 1xBet muet
    with Fakes() as f, Quiet():
        xalerts.LAST["gap"] = 0
        msg = xalerts.on_refresh(today, now, {"e1", "e2", "e3"}, [], x_fetch(live, calls))
        assert msg is not None and xalerts.LAST["gap"] == 1
        gm = f.gap_msgs()
        assert len(gm) == 1 and cbs(gm[0]) == [f"joue|g1|{DAY}"], [str(m)[:60] for m in f.sent]
        text = fmt.plain(str(gm[0]))
        assert text.startswith("💰 1xBet paie plus que le marché · 15:00") and f"1xBet {fmt.num(live['e1'])}" in text and "+9,9 %" in text and "Home1 – Away1" in text, text
        assert "Home2" not in text and "Home3" not in text and "Home4" not in text and "Code coupon 1xBet : CODE" in text
        assert any("copy_text" in b for row in gm[0].markup["inline_keyboard"] for b in row), "un seul pari : « 📋 Copier le code »"
        # les relectures en direct sont celles des candidats seulement (le périmé n'en reçoit pas), dans le plafond XALERT_LIVE_MAX
        read = {i for c in calls for i in c}
        assert read <= {"e1", "e2", "e3"} and "e4" not in read and len(read) <= C.XALERT_LIVE_MAX, calls
        # le pari annoncé est suivi : relu en direct, donc SON prix du moment (pas celui du matin), mise du budget, code
        row = next(r for r in track.load() if r.get("gap_n") == 1)
        assert row["key"].startswith("e1|") and row["xodds"] == live["e1"] and row["sent"] and row["stake"] > 0 and row["coupon"] and row["xref"][0] == 9001 and row["xat"] == "15:00"
        assert row["gap"]["edge"] >= 0.099 and sharpgap.sent_keys(DAY) == {row["key"]}
        assert f.coupons[-1] == [("e1", live["e1"])]
        # deuxième passage : e1 n'est jamais ré-annoncé ni relu ; e2 au même prix : toujours rien
        calls.clear()
        n_sent = len(f.sent)
        assert xalerts.on_refresh(today, at(15, 30), {"e1", "e2", "e3"}, [], x_fetch(live, calls)) is None and len(f.sent) == n_sent
        assert "e1" not in {i for c in calls for i in c}, "un pari déjà annoncé n'est pas relu"
        # e2 passe au-dessus du seuil : annoncé, numéro suivant
        live["e2"] = home_price(0.06)
        assert xalerts.on_refresh(today, at(15, 45), {"e2"}, [], x_fetch(live, calls)) is not None
        assert cbs(f.gap_msgs()[-1]) == [f"joue|g2|{DAY}"] and sharpgap.sent_keys(DAY) >= {row["key"], next(r for r in track.load() if r.get("gap_n") == 2)["key"]}
        # plafond d'annonces du jour
        live["e3"] = home_price(0.08)
        C.SHARPGAP_MAX_DAY = 2
        n_sent = len(f.sent)
        assert xalerts.on_refresh(today, at(16), {"e3"}, [], x_fetch(live, calls)) is None and len(f.sent) == n_sent
        C.SHARPGAP_MAX_DAY = 6
        # plafond de relectures du passage : un seul match relu, les autres candidats attendent le passage suivant
        calls.clear()
        C.XALERT_LIVE_MAX = 1
        live["e3"] = home_price(0.08)
        xalerts.on_refresh(today, at(16, 30), {"e3"}, [], x_fetch(live, calls))
        assert len({i for c in calls for i in c}) <= 1
        C.XALERT_LIVE_MAX = 3
        # pas de lecture possible (1xBet muet) : jamais un prix non vérifié
    with Fakes() as f, Quiet():
        reset()
        today, now = afternoon()
        assert xalerts.on_refresh(today, now, {"e1"}, [], x_fetch({})) is None and not f.sent and not sharpgap.sent_keys(DAY)
        # options éteintes
        C.SHARPGAP_ENABLED = False
        assert xalerts.on_refresh(today, now, {"e1"}, [], x_fetch({"e1": home_price(0.10)})) is None
        C.SHARPGAP_ENABLED, C.XALERTS_ENABLED = True, False
        assert xalerts.on_refresh(today, now, {"e1"}, [], x_fetch({"e1": home_price(0.10)})) is None and not f.sent
        C.XALERTS_ENABLED = True
        # un pari que tu as DÉJÀ noté (ou le pari du jour) n'est pas proposé
        picks = today["picks"]
        track.record(picks, {picks[0]["key"]}, DAY, at(4, 30), single_key=picks[0]["key"])          # e1 = pari du jour
        rows = track.load()
        xalerts.on_refresh(today, now, {"e1", "e2"}, [], x_fetch({"e1": home_price(0.10), "e2": home_price(0.015)}))
        assert not f.gap_msgs() and not played.gap_rows(track.load()), "e1 = pari du jour (son message existe, l'alerte « mieux payé » le suit déjà), e2 sous le seuil"
    reset()
    print("ok  relevé : prix 1xBet relu en direct, écart recalculé (sous le seuil, périmé, muet = rien), suivi au prix relu, dédoublonné, plafonds, options éteintes")


def test_manual_refresh_reports_the_gap_bets():
    reset()
    money.set_daily(10000)
    today, now = afternoon()
    prono_ = prono(i=9, p0=0.72, name="Under", point=3.5, alts=[["Under", 3.5, 0.72, None]]) | {
        "psh": {"under|3.5": [0.70, "pinnacle", 1]}, "commence": f"{DAY}T21:00:00Z"}
    today["pronos"] = [prono_]
    store.save("today.json", today)
    base = x_fetch({"e1": home_price(0.10)})

    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        out = base(events, deadline=deadline, wanted=wanted, live=live, fresh_min=fresh_min)
        for _, ev in events:
            if ev["id"] == "m9":
                px = {("totals", "Under", 3.5): 1.55}
                out["m9"] = {"gid": 909, "px": px, "refs": refs_of(px, 909)}
        return out
    with Fakes() as f, Quiet():
        reply = app.manual_refresh(now, fetch)
    assert reply.startswith("✅ Cotes 1xBet actualisées à 15:00") and "1 pari(s) 💰 « 1xBet paie plus que le marché » envoyé(s)." in reply and "Rien de nouveau" not in reply, reply
    assert len(f.gap_msgs()) == 1 and played.gap_rows(track.load())
    # rien de neuf : la réponse le dit comme avant
    reset()
    money.set_daily(10000)
    today, now = afternoon()
    today["pronos"] = [prono_]
    store.save("today.json", today)
    with Fakes() as f, Quiet():
        reply = app.manual_refresh(now, lambda events, **k: {"m9": {"gid": 909, "px": {("totals", "Under", 3.5): 1.55}, "refs": {}}} if any(e["id"] == "m9" for _, e in events) else {})
    assert reply.startswith("✅ Cotes 1xBet actualisées") and "💰" not in reply and "1xBet paie plus que le marché" not in reply, reply
    reset()
    print("ok  « 🔄 Mettre à jour tous les paris » : les paris 1xBet en retard envoyés sont comptés dans la réponse")


def test_hourly_pass_uses_the_fresh_closing_sharp():
    reset()
    money.set_daily(10000)
    today, now = afternoon(sharp_ages=(600, 600, 600, 600))           # les relevés sharp du matin sont périmés à 18:00
    now = at(18)
    stamp = odds.read_stamp(now.timestamp() - 60)
    fresh = odds.build_picks([sharp_event(1, read_at=stamp)], date(2030, 1, 1))
    with Fakes() as f, Quiet():
        # sans clôture fraîche : le sharp du matin est refusé (périmé)
        assert xalerts.on_market([], [], today, [], [], DAY, now, x_fetch({"e1": home_price(0.10)})) is None and not f.gap_msgs()
        # avec la clôture qui vient d'être relevée (sharp lu il y a 1 min) : l'écart est mesuré à SON prix, 1xBet relu en direct
        msg = xalerts.on_market([], fresh, today, [], [], DAY, now, x_fetch({"e1": home_price(0.10)}))
        assert msg is not None and len(f.gap_msgs()) == 1, [str(m)[:50] for m in f.sent]
        text = fmt.plain(str(f.gap_msgs()[0]))
        assert "relevé sharp il y a 1 min" in text and "+9,9 %" in text, text
        assert [r["gap_n"] for r in played.gap_rows(track.load())] == [1]
        # le marché a bougé CONTRE le pari (Pinnacle allonge le favori) : au prix relu, l'écart du matin n'existe plus
        reset()
        money.set_daily(10000)
        today, now = afternoon(edges_morning=(0.05, 0.05, 0.05, 0.05), sharp_ages=(600, 600, 600, 600))
        worse = odds.build_picks([sharp_event(1, home_price=2.4, read_at=stamp)], date(2030, 1, 1))
        assert next(c for c in worse if c["name"] == "Home1")["prob_book"] < home_p() - 0.02
        n_before = len(f.sent)
        xalerts.on_market([], worse, today, [], [], DAY, at(18), x_fetch({"e1": home_price(0.05)}))
        assert len(f.sent) == n_before and not played.gap_rows(track.load()), "plus d'écart au sharp du moment : rien n'est annoncé"
    # run_track pose la vraie heure de lecture sur la clôture qu'il achète (fraîcheur du sharp)
    reset()
    seen = []
    real = odds.build_picks
    one = gap_world((0.05,))
    odds.build_picks = lambda evs, day, *a, **k: seen.append(evs[0].get("read_at")) or []
    try:
        track.record(one, {"e1|h2h|||Home1"}, DAY, at(4), single_key="e1|h2h|||Home1")
        with Quiet(), Fakes():
            app.run_track(events_for=lambda sk, eid, m: {"commence_time": KO, "id": eid}, now=at(18), xfetch=x_fetch({}))
    finally:
        odds.build_picks = real
    assert seen == ["2030-01-01T18:00:00+00:00"], seen
    reset()
    print("ok  passage horaire : la clôture fraîche donne le sharp du moment, 1xBet relu en direct ; run_track date sa lecture")



# ---------- 10. bilan, semaine, résultats du matin ----------

def settled_gap_rows(clv=(0.03, -0.01, 0.05)):
    """Trois écarts annoncés hier, réglés, avec leur clôture mesurée contre Pinnacle ; le premier est noté."""
    with Fakes():
        picks, msgs, rec = morning_state()
    rows = track.load()
    gaps = [r for r in rows if r.get("gap_n")]
    for r, e, res in zip(gaps, clv, ("win", "loss", "win")):
        r.update(result=res, gap_edge_close=e, gap_close_src="pinnacle", final="2-1", closed_at="2030-01-01T18:00", clv=0.01)
    gaps[0]["played"] = {"odds": gaps[0]["xodds"], "stake": 1000, "at": "x"}
    track.save(rows)
    return gaps


def test_weekly_bilan_and_morning_results():
    reset()
    gaps = settled_gap_rows()
    line = sharpgap.summary_line(track.load())
    assert line == "Paris 1xBet en retard : 4 suivis (1 joué), CLV moyen contre Pinnacle +2,3 % (cote prise face à la cote finale de Pinnacle, 3 mesurés)", line
    assert sharpgap.summary_line([]) == "" and "CLV" not in sharpgap.summary_line([r | {"gap_edge_close": None} for r in track.load()])
    assert sharpgap.summary_line(track.load(), days=7, today="2030-02-01") == "", "hors de la période"
    # poids appris : une ligne par famille, en mots simples
    E.learn([{"key": f"w{i}", "date": DAY, "market": "totals", "result": "win" if i % 2 else "loss", "pm": 0.7, "px": 0.55, "ps": 0.6} for i in range(40)])
    lines = E.lines()
    assert lines and lines[0].startswith("buts : Kelly compte pour 12 % (prévu 12 %), Pinnacle 75 % du marché (prévu 75 %) — 4"), lines
    # la semaine
    msg = fmt.plain(str(weekly.message(datetime(2030, 1, 2, 4, 0, tzinfo=UTC))))
    assert "⚖️ Qui Kelly écoute (appris sur les résultats" in msg and "buts : Kelly compte pour 12 %" in msg, msg
    assert "💰 Paris 1xBet en retard : 4 suivis (1 joué), CLV moyen contre Pinnacle +2,3 %" in msg, msg
    # /bilan : la ligne en mots simples, le détail avec les poids appris
    bets = store.load("bets.json", {"picks": [], "combos": []})
    simple = fmt.plain(str(settle.bilan(bets)))
    assert "💰 Paris 1xBet en retard : 4 suivis (1 joué), CLV moyen contre Pinnacle +2,3 %" in simple, simple
    detail = settle.bilan(bets, detail=True)
    assert "Paris 1xBet en retard : 4 suivis" in detail and "Qui Kelly écoute, appris sur les résultats" in detail and "• buts : Kelly compte pour 12 %" in detail, detail
    # rien de tel sans données : pas de ligne
    reset()
    store.save("bets.json", {"picks": [], "combos": []})
    assert "Qui Kelly" not in str(weekly.message(datetime(2030, 1, 2, 4, 0, tzinfo=UTC))) and E.lines() == []
    # résultats du matin (morning_report réel) : les écarts réglés sont annoncés UNE fois, la marque est enregistrée avec le suivi
    gaps = settled_gap_rows()
    tr = track.load()
    settled = [r for r in tr if r.get("gap_n")]
    real = (settle.settle_pending, app.freeclose.fill)
    settle.settle_pending = lambda now=None, **k: (store.load("bets.json", {"picks": [], "combos": []}), settled)
    app.freeclose.fill = lambda rows, now: 0
    try:
        with Fakes() as f, Quiet():
            msgs, _ = app.morning_report(datetime(2030, 1, 2, 4, 0, tzinfo=UTC))
            again, _ = app.morning_report(datetime(2030, 1, 2, 4, 5, tzinfo=UTC))
    finally:
        settle.settle_pending, app.freeclose.fill = real
    res = [m for m in msgs if str(m).startswith("<b>📋 Résultats d'hier</b>")]
    assert len(res) == 1 and fmt.plain(str(res[0])).count("1xBet paie plus que le marché") == 3 and "+1" in fmt.plain(str(res[0])), [str(m)[:60] for m in msgs]
    assert not [m for m in again if "1xBet paie plus que le marché" in str(m) and str(m).startswith("<b>📋")], "déjà annoncés"
    assert all(r.get("gap_reported") for r in track.load() if r.get("gap_n"))
    reset()
    print("ok  semaine / bilan / résultats du matin : poids appris par famille, « Paris 1xBet en retard : N suivis, CLV moyen contre Pinnacle », écarts annoncés une fois")


def main():
    test_flags_and_environment()
    test_active_family_is_decided_by_the_ensemble()
    test_mixed_defaults_and_inactive_families()
    test_viable_reads_the_ensemble_outcome()
    test_display_lines_and_legend()
    test_odds_records_the_real_read_time()
    test_journal_settlement_and_learning()
    test_closing_measures_the_edge_against_pinnacle()
    test_morning_gap_section_and_tracking()
    test_morning_gap_edge_cases()
    test_notes_money_and_lists()
    test_run_daily_end_to_end()
    test_world_report_passes_the_real_picks()
    test_refresh_rechecks_the_live_price()
    test_manual_refresh_reports_the_gap_bets()
    test_hourly_pass_uses_the_fresh_closing_sharp()
    test_weekly_bilan_and_morning_results()
    print("tests ensemble (câblage) : OK")


if __name__ == "__main__":
    main()
