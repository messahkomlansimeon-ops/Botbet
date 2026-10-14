"""Poids du modèle FIGÉ à 0 sur les buts, le 1N2, la double chance, les deux équipes marquent et les buts par équipe (audit du 14/10, règle pré-déclarée), hors ligne, AU RÉGLAGE PAR
DÉFAUT : la proba retenue est celle du marché seul (jamais le modèle, même sans avis de marché), le modèle n'a ni veto ni filtre de ligne, sans avis sharp aucun pari n'est
validé (pas de « probable » : ce serait 1xBet contre 1xBet), le poids du modèle n'est plus appris (celui du sharp continue), le modèle reste journalisé en fantôme avec ce
qu'il aurait décidé, les autres familles (corners, tirs...) sont intactes, et un ensemble vide (ENSEMBLE_FROZEN_MODEL=none) reproduit l'ancien comportement (les tests d'avant,
épinglés à « none », passent). Lancer : python -m tests.test_frozen_model"""
import copy
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-frozen-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-frozen-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "ENSEMBLE_FROZEN_MODEL"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import combos as CB, config as C, ensemble as E, fmt, prematch, store, tune, xalerts, xprice  # noqa: E402

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
FIVE = frozenset({"goals", "h2h", "dc", "btts", "team_goals"})


def fresh():
    for f in (E.FILE, E.LOG_FILE):
        if store.exists(f):
            store.save(f, {} if f == E.FILE else [])
    E._memo.clear()


def test_config_and_weights():
    fresh()
    assert C.ENSEMBLE_FROZEN_MODEL == FIVE, "défaut : les cinq familles de la décision"
    for fam in FIVE:
        w = E.weights(fam)
        assert w["wm"] == 0.0 and w["frozen"] is True and w["wm_raw"] == 0.12, (fam, w)
        assert w["ws"] == 1.0 and w["ws_raw"] == 0.75, "ws = 1 : le marché est le sharp seul (comme sharpgap)"
        assert E.weights(fam, raw=True)["wm"] == 0.12 and E.weights(fam, raw=True)["frozen"] is False
    assert E.weights("corners")["wm"] == 0.25 and E.weights("corners")["ws"] == 0.75 and E.weights("corners")["frozen"] is False, "les familles de stats sont intactes"
    assert E.model_silent("totals") and E.model_silent("double_chance") and E.model_silent("team_totals_home") and E.model_silent("btts") and E.model_silent("h2h")
    assert not E.model_silent("alternate_totals_corners") and not E.model_silent("team_shots_home")
    # même un poids APPRIS élevé ne remonte pas : le gel prime
    E.save({"fam": {"goals": {"m": {"ll": [0.0] * len(E.GRID), "n": 1000.0}}}, "done": {}})
    E._memo.clear()
    assert E.weights("goals")["wm"] == 0.0
    print("ok  défaut = les cinq familles ; wm = 0 quoi que l'apprentissage ait mesuré ; corners / tirs intacts ; poids brut gardé pour le fantôme")


def test_combine():
    fresh()
    p, parts = E.combine("goals", p_model=0.90, p_x=0.50, p_sharp=0.60)
    p_frozen = p
    assert abs(p - 0.60) < 1e-9, "famille figée : le marché est le sharp SEUL (ws = 1), ni le modèle ni 1xBet n'y pèsent"
    assert parts["p_model"] == 0.90 and parts["wm"] == 0.0 and parts["ws"] == 1.0 and parts["frozen"] and parts["w"]["model"] == 0.0 and parts["w"]["x"] == 0.0 and parts["src"] == "sharp"
    assert parts["market_weight"] == 1.0
    assert E.combine("goals", p_model=0.90, p_x=0.50)[0] == 0.50 and E.combine("goals", p_model=0.90, p_sharp=0.60)[0] == 0.60
    p, parts = E.combine("goals", p_model=0.90)
    assert p is None and parts["p_model"] == 0.90, "sans avis de marché : pas de proba (jamais le modèle seul)"
    assert E.combine("corners", p_model=0.90)[0] == 0.90, "une famille non figée garde le repli « modèle seul »"
    assert abs(E.combine("corners", p_model=0.9, p_x=0.5, p_sharp=0.6)[0] - E.pool(0.9, E.pool_market(0.6, 0.5, 0.75)[0], 0.25)) < 1e-9, "corners : ws = 0,75 et wm = 0,25 inchangés"
    q, _ = E.combine("goals", p_model=0.90, p_x=0.50, p_sharp=0.60, raw=True)
    assert abs(q - E.pool(0.90, E.pool_market(0.60, 0.50, 0.75)[0], 0.12)) < 1e-9 and q > p_frozen, "raw = l'ancien mélange (12 % modèle, sharp 75 % / 1xBet 25 %)"
    print("ok  combine : sharp seul (ws = 1, modèle ignoré), pas de proba sans marché, corners inchangés, raw = ancien mélange")


def test_value_rule():
    fresh()
    # avis sharp présent : valeur = cote 1xBet x proba sharp - 1 >= +3 % (seuil de sharpgap), le modèle (même très en désaccord) ne met pas de veto
    v = E.value_of("totals", 0.30, 0.66, 0.74, 1.45)
    assert v["ok"] and v["kind"] == "value" and abs(v["p"] - 0.74) < 1e-9 and abs(v["ev"] - (0.74 * 1.45 - 1)) < 1e-12 and v["gap"] > 0.3 and v["why"] == "", v
    old = E.value_of("totals", 0.30, 0.66, 0.74, 1.45, raw=True)
    assert not old["ok"] and old["why"] == "veto", "avant le gel, un modèle à 0,30 contre un marché à ~0,72 mettait un veto"
    # seuil aligné sur sharpgap (SHARPGAP_EDGE = 0,03) et plafond SHARPGAP_MAX_EDGE : une donnée suspecte n'est pas une valeur
    assert C.SHARPGAP_EDGE == 0.03
    assert E.value_of("totals", None, None, 0.60, 1.74)["kind"] == "value"                              # ev = 0,044
    assert E.value_of("totals", None, None, 0.60, 1.70)["kind"] == "probable", "ev = +2 % < 3 % mais favori sharp >= 60 % : « probable », SANS valeur"
    low = E.value_of("totals", None, None, 0.55, 1.70)
    assert not low["ok"] and low["why"] == "price", "favori sharp sous 60 % et sans valeur : rien"
    far = E.value_of("totals", None, None, 0.60, 1.35)
    assert not far["ok"] and far["why"] == "price", "1xBet paie plus de 10 % sous la cote juste : rien"
    sus = E.value_of("totals", None, None, 0.80, 1.70)
    assert not sus["ok"] and sus["why"] == "suspect" and sus["ev"] > C.SHARPGAP_MAX_EDGE, "écart énorme : donnée suspecte, comme sharpgap"
    # sans avis sharp : jamais de « probable » (1xBet contre 1xBet) ni de « valeur »
    for pm in (0.76, 0.30, 0.95):
        v = E.value_of("totals", pm, 0.70, None, 1.45)
        assert not v["ok"] and v["why"] == "no_sharp" and v["kind"] is None, (pm, v)
    assert E.value_of("totals", 0.76, 0.70, None, 1.45, raw=True)["kind"] == "probable", "avant le gel : favori 1xBet à 70 % = « probable »"
    # aucun avis de marché : aucune proba, aucun pari
    v = E.value_of("totals", 0.90, None, None, 1.45)
    assert not v["ok"] and v["p"] is None and v["why"] == "no_prob", v
    # famille NON figée : inchangée (modèle 25 %, marge de l'ensemble 3 %, veto possible)
    assert E.value_of("alternate_totals_corners", 0.85, 0.60, None, 1.80)["parts"]["wm"] == 0.25
    # fraîcheur du sharp : celle de sharpgap pour une famille figée
    c = {"market": "totals", "psh": {"over|2.5": [0.6, "pinnacle", 1, "2026-10-14T08:00:00+00:00"]}}
    assert E.sharp_for(c, "Over", 2.5, datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)) == 0.6                      # 90 min <= 120
    assert E.sharp_for(c, "Over", 2.5, datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)) is None                     # 150 min > 120 (sharpgap) alors que l'ensemble accepte 180
    assert E.sharp_for(c | {"market": "alternate_totals_corners", "psh": {"over|2.5": c["psh"]["over|2.5"]}}, "Over", 2.5, datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)) == 0.6
    print("ok  value_of : sharp seul, seuil +3 % et plafond comme sharpgap ; favori sharp sans valeur = « probable » ; sans sharp = no_sharp ; sans marché = pas de proba ; corners inchangés")


def test_learn():
    fresh()
    rows = [{"key": f"g{i}|totals||2.5|Over", "id": f"g{i}", "market": "totals", "date": "2026-10-13", "result": "win" if i % 2 else "loss", "pm": 0.6, "px": 0.5, "ps": 0.52}
            for i in range(30)] + [{"key": f"c{i}|alternate_totals_corners||9.5|Over", "id": f"c{i}", "market": "alternate_totals_corners", "date": "2026-10-13",
                                    "result": "win" if i % 2 else "loss", "pm": 0.6, "px": 0.5, "ps": None} for i in range(30)]
    n = E.learn(rows)
    st = E.load()["fam"]
    assert n == 60 and all(r.get("ens") for r in rows)
    assert "goals" not in st or not ({"m", "s"} & set(st["goals"])), "famille figée : ni le poids du modèle ni celui du sharp (ws = 1) ne sont plus appris"
    assert st["corners"]["m"]["n"] > 0, "famille non figée : apprentissage inchangé"
    print("ok  learn : plus d'apprentissage (modèle ni sharp) pour les familles figées, corners inchangés, lignes marquées apprises")


def test_record_shadow():
    fresh()
    base = {"key": "m|totals||2.5|Over", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z", "market": "totals",
            "name": "Over", "point": 2.5, "prob": 0.7, "p0": 0.7, "xodds": 1.55, "mu": None, "xref": [9, 9, 2.5], "p_model": 0.80, "xp": 0.66, "p_sharp": 0.70}
    cor = base | {"key": "m|alternate_totals_corners||9.5|Over", "market": "alternate_totals_corners", "point": 9.5, "xref": [9, 8, 9.5]}
    assert E.record([base, cor], "2026-10-14", NOW) == 2
    rows = {r["market"]: r for r in store.load(E.LOG_FILE, [])}
    sh = rows["totals"]["shadow"]
    assert sh["label"].startswith("modèle seul (fantôme") and abs(sh["ev"] - (0.80 * 1.55 - 1)) < 1e-4 and sh["model_value"] is True, sh
    assert sh["old_ok"] is True and sh["old_kind"] == "value" and sh["old_p"] is not None and "error" not in sh
    assert rows["totals"]["pm"] == 0.8 and rows["totals"]["xref"] == [9, 9, 2.5]
    assert "shadow" not in rows["alternate_totals_corners"], "pas de fantôme pour une famille non figée"
    # sans prix 1xBet : le fantôme existe, sans espérance
    nop = base | {"key": "m2|totals||2.5|Over", "id": "m2", "xodds": None}
    E.record([nop], "2026-10-14", NOW)
    assert "ev" not in {r["key"]: r for r in store.load(E.LOG_FILE, [])}["m2|totals||2.5|Over"]["shadow"]
    print("ok  fantôme : le modèle est journalisé avec son espérance seule, model_value et l'ancienne décision ; rien pour les autres familles")


SCENARIO = {"key": "m|totals||1.5|Over", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z", "market": "totals", "name": "Over",
            "point": 1.5, "who": None, "prob": 0.76, "p0": 0.76, "min_odds": 1.4, "conf": 0.6, "n_min": 8, "score": 0.2, "pick": "Plus de 1,5 buts",
            "alts": [["Over", 1.5, 0.76, None], ["Under", 1.5, 0.24, None]]}
SHARP = {"key": "m|totals||1.5|Over", "id": "m", "market": "totals", "name": "Over", "point": 1.5, "prob": 0.74, "prob_book": 0.74, "prob_src": "pinnacle", "n_sharp": 1,
         "sharp_at": datetime.now(timezone.utc).isoformat()}                       # un vrai pari porte l'heure de son relevé sharp (sans elle, une famille figée refuse l'avis)
REFS = {("totals", "Over", 1.5): [9, 9, 1.5]}


def test_xprice_flow():
    fresh()
    px = {("totals", "Over", 1.5): 1.45, ("totals", "Under", 1.5): 2.9}
    found = {"m": {"gid": 900, "px": px, "refs": REFS}}
    # --- avec avis sharp : p_ens = marché ; le modèle (0,76) n'y pèse rien
    c = copy.deepcopy(SCENARIO)
    sharp = copy.deepcopy(SHARP)
    out = xprice.apply([c], fetch=lambda *a, **k: found, picks=[sharp])
    p_x = E.x_fair(px, "totals", "Over", 1.5)
    assert c["prob"] == 0.74 and c["p_model"] == 0.76 and c["ew"] == 0.0 and c["esrc"] == "sharp" and abs(c["xev"] - (0.74 * 1.45 - 1)) < 1e-4, c      # sharp seul (ws = 1)
    assert CB.viable(c) and c["xkind"] == "value" and [x["key"] for x in CB.fair_legs([c])] == [c["key"]] and CB.best_per_match([c]) == [c], \
        "avec un avis sharp et la cote 1xBet, la ligne reste jouable (jambe de combiné possible)"
    # --- le même pari avec un modèle en grand désaccord : plus de veto
    far = copy.deepcopy(SCENARIO) | {"prob": 0.30, "p0": 0.30, "alts": [["Over", 1.5, 0.30, None]]}
    out = xprice.apply([far], fetch=lambda *a, **k: found, picks=[copy.deepcopy(SHARP)])
    assert out["veto"] == 0 and "xveto" not in far and far["p_model"] == 0.30, (out, far)
    # --- sans avis sharp : plus de « probable » (avant : favori 1xBet à 68 % = 🎯)
    low = copy.deepcopy(SCENARIO)
    r = xprice.price(low, px, REFS)
    assert r == "low" and low.get("xkind") is None and low["xwhy"] == "no_sharp" and not CB.viable(low), (r, low.get("xwhy"))
    assert not CB.fair_legs([low]), "ni jambe de combiné ni pari"
    # --- sans AUCUN avis de marché (cote lue, mais pas la cote complémentaire) : jamais la proba du modèle seule
    none_px = {("totals", "Over", 1.5): 1.45}
    nm = copy.deepcopy(SCENARIO)
    r = xprice.price(nm, none_px, REFS)
    assert r == "low" and nm["xwhy"] == "no_market" and nm["xveto"] == 1 and nm["min_odds"] > nm["xodds"] and nm["p_model"] == 0.76 and nm.get("xkind") is None
    assert not CB.viable(nm) and not CB.fair_legs([nm]) and nm["xref"] == [9, 9, 1.5]
    CB.fair_plan([nm, low, c])                      # rien ne plante avec des lignes sans proba retenue
    assert "xev" not in nm
    got = CB.best_per_match([nm, low, c])           # ne plante pas sur une ligne sans xev (relecture adverse) ; les lignes sans proba retenue ne sont jamais choisies
    assert nm not in got and low not in got and all(x is c for x in got), got
    # 1xBet absent toute la journée : une famille figée n'a ni proba retenue (p_ens) ni droit de décider seule ; une famille de stats garde le comportement d'avant
    solo = copy.deepcopy(SCENARIO) | {"min_odds": 1.30}
    assert not CB.viable(solo) and not CB.fair_legs([solo]) and CB.best_per_match([solo]) == [], "famille figée sans proba du marché : ni jambe ni 👉"
    cs = {"key": "m|alternate_totals_corners||10.5|Under", "id": "m", "market": "alternate_totals_corners", "name": "Under", "point": 10.5, "prob": 0.85, "p0": 0.85,
          "min_odds": 1.3, "conf": 0.6, "n_min": 8, "score": 0.2, "pick": "Moins de 10,5 corners", "sport_key": "soccer_epl", "home": "A", "away": "B"}
    assert CB.viable(cs) is True, "une famille non figée sans p_ens garde le comportement d'avant (viable ne dépend que de ses critères habituels)"
    txt = fmt.pronos([nm, low], limit=5, title="t", when="08:00", intro="i")
    assert txt is not None
    # --- une famille non figée garde le chemin d'avant (corners : modèle 25 %, pas de gel)
    pxc = {("alternate_totals_corners", "Under", 10.5): 1.55, ("alternate_totals_corners", "Over", 10.5): 2.45}
    cor = {"key": "m|alternate_totals_corners||10.5|Under", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z",
           "market": "alternate_totals_corners", "name": "Under", "point": 10.5, "who": None, "prob": 0.85, "p0": 0.85, "min_odds": 1.3, "conf": 0.6, "n_min": 8,
           "score": 0.2, "pick": "Moins de 10,5 corners", "alts": [["Under", 10.5, 0.85, None], ["Over", 10.5, 0.15, None]]}
    xprice.price(cor, pxc, {("alternate_totals_corners", "Under", 10.5): [9, 9, 10.5]})
    assert cor["ew"] == 0.25 and cor["p_model"] == 0.85, cor
    print("ok  xprice : sharp = marché seul sans veto du modèle ; sans sharp = no_sharp ; sans marché = no_market (jamais le modèle seul), rien ne plante ; corners inchangés")


def test_label():
    fresh()
    c = {"market": "totals", "prob": 0.67, "p_x": 0.48}
    assert "retenu 67 %" in fmt.chances_line(c) and "Kelly" not in fmt.chances_line(c)
    assert "Kelly 67 %" in fmt.chances_line({"market": "alternate_totals_corners", "prob": 0.67, "p_x": 0.48})
    assert "figé" in " ".join(E.lines(data={"fam": {"goals": {"m": {"ll": [0.0] * len(E.GRID), "n": 120.0}}}, "done": {}}))
    assert fmt.prob_label({"market": "h2h"}) == "retenu" and fmt.prob_label({"market": "alternate_totals_corners"}) == "Kelly" and fmt.prob_label({}) == "Kelly"
    print("ok  affichage : « retenu X % » pour une famille figée (c'est la proba du marché), « Kelly X % » ailleurs, lignes de poids honnêtes (« figé »)")


def test_goal_weight_and_tuning():
    fresh()
    assert prematch.goal_weight({"market": "totals", "sport_key": "soccer_epl"}) == 0.0 and prematch.btts_weight({"market": "btts"}) == 0.0
    # le mélange de prematch.apply ne bouge plus la proba du pari ni son « pourquoi » : l'avis du modèle reste noté (fantôme)
    c = {"prob": 0.60, "odds": 1.9, "weight": 1.0, "score": 0.1, "why": "cote plus haute qu'ailleurs", "market": "totals"}
    a = {"kind": "goals", "p": 0.80, "w": prematch.goal_weight(c), "tag": "modèle Kelly : 3.1 buts attendus vs ligne 2.5", "flag": 0.05, "exp": 3.1}
    prematch.apply(c | {"point": 2.5, "name": "Over"}, a)
    out = prematch.apply(dict(c, point=2.5, name="Over"), a)
    assert out["prob"] == 0.60 and out["why"] == "cote plus haute qu'ailleurs" and out["pm"]["silent"] == 1 and out["pm"]["pgap"] == 0.2, out
    # le réglage hebdomadaire ne calcule plus de poids de buts, la demi-vie reste réglée
    cur = {"half_life": {"E0": 330}, "goal_weight": {"E0": 0.5}, "report": {"E0": {"chosen": 330, "n": 100, "table": {}}}}
    tune._write(cur)
    assert cur["goal_weight"] == {} and cur["half_life"] == {"E0": 330} and store.load(tune.FILE, {})["goal_weight"] == {}
    # sans gel (ENSEMBLE_FROZEN_MODEL=none) : le poids de config revient (20 %)
    old = C.ENSEMBLE_FROZEN_MODEL
    C.ENSEMBLE_FROZEN_MODEL = frozenset()
    try:
        assert prematch.goal_weight({"market": "totals", "sport_key": "soccer_epl"}) == C.MODEL_WEIGHT["goals"] == prematch.btts_weight({"market": "btts"})
    finally:
        C.ENSEMBLE_FROZEN_MODEL = old
    print("ok  goal_weight / btts_weight = 0, le mélange ne touche plus la proba ni le « pourquoi », le réglage hebdomadaire ne règle plus de poids de buts, retour arrière possible")


def test_combo_model_guard_and_probable_leg():
    fresh()
    # le modèle ne refuse plus une jambe d'une famille figée (écart de proba négatif), mais refuse toujours celle d'une famille non figée
    base = {"edge": 0.12, "prob": 0.70, "n_books": 10, "pm": {"pgap": -0.30, "kind": "goals"}, "odds": 1.8}
    assert CB.leg_reason(base | {"market": "totals"}) == "value"
    assert CB.leg_reason(base | {"market": "alternate_totals_corners", "pm": {"pgap": -0.30, "kind": "corners"}}) is None
    # favori sharp à 70 % dont le prix 1xBet ne dépasse pas le sharp : jambe « probable », SANS valeur, étiquetée comme telle, jamais comptée comme valeur
    px = {("totals", "Over", 1.5): 1.40, ("totals", "Under", 1.5): 3.1}
    sharp = copy.deepcopy(SHARP) | {"prob": 0.72, "prob_book": 0.72}
    c = copy.deepcopy(SCENARIO)
    out = xprice.apply([c], fetch=lambda *a, **k: {"m": {"gid": 900, "px": px, "refs": REFS}}, picks=[sharp])
    assert out["value"] == 1 and c["xkind"] == "probable" and c["p_sharp"] == 0.72 and abs(c["xev"] - (0.72 * 1.40 - 1)) < 1e-4 and c["xev"] < C.SHARPGAP_EDGE, c
    assert CB.viable(c) and CB.probable(c), "jambe de combiné possible (choix de l'utilisateur)"
    txt = str(fmt.pronos([c], limit=5, title="t", when="08:00", intro="i"))
    assert "🎯" in txt and "favori des books de référence, sans valeur" in txt and "💎" not in txt and "retenu 72 %" in txt, txt
    print("ok  jambe de combiné : le modèle ne refuse rien sur une famille figée (corners inchangés) ; favori sharp sans valeur = 🎯 « sans valeur », jamais 💎")


def test_review_followups():
    fresh()
    # D1 : sans modèle approfondi (profil court, football-data) le mélange ne repasse pas par le poids de config à 20 %
    f = {"exp_goals_home": 1.8, "exp_goals_away": 1.4}
    c = {"market": "totals", "name": "Over", "point": 2.5, "sport_key": "soccer_epl"}
    a = prematch.assess(c, f, None)
    assert a is not None and a["kind"] == "goals" and a["w"] == 0.0, a
    b = prematch.assess({"market": "btts", "name": "Yes", "point": None, "sport_key": "soccer_epl"}, f, None)
    assert b is not None and b["w"] == 0.0, b
    # M1 : le « pourquoi » n'est retiré que pour une famille figée ; sans gel, un poids mesuré à 0 laisse l'ancien comportement (argument ajouté)
    old = C.ENSEMBLE_FROZEN_MODEL
    C.ENSEMBLE_FROZEN_MODEL = frozenset()
    try:
        pick = {"prob": 0.60, "odds": 1.9, "weight": 1.0, "score": 0.1, "why": "", "market": "totals", "point": 2.5, "name": "Over"}
        out = prematch.apply(dict(pick), {"kind": "goals", "p": 0.80, "w": 0.0, "tag": "modèle : 3,1 buts", "flag": 0.05, "exp": 3.1})
        assert out["why"] == "modèle : 3,1 buts" and "silent" not in out["pm"], out
    finally:
        C.ENSEMBLE_FROZEN_MODEL = old
    # D3 : une jambe 🎯 « sans valeur » n'est jamais annoncée comme une valeur par l'alerte « hausse de cote »
    base = copy.deepcopy(SCENARIO) | {"xodds0": 1.40, "xodds": 1.50, "xev": 0.02, "p_ens": 0.70, "prob": 0.70, "min_odds": 1.30, "xref": [9, 9, 1.5]}
    assert xalerts.rise_items([base | {"xkind": "value"}], {"m"}, "10:00", [], {}), "une vraie valeur est annoncée"
    assert not xalerts.rise_items([base | {"xkind": "probable"}], {"m"}, "10:00", [], {}), "un favori sans valeur ne l'est pas"
    # M2 : la cote minimale d'une famille figée suit la marge de sharpgap
    oldm = C.SHARPGAP_EDGE
    C.SHARPGAP_EDGE = 0.05
    try:
        assert abs(E.min_price(0.70, market="totals") - 1.05 / 0.70) < 1e-12 and abs(E.min_price(0.70, market="alternate_totals_corners") - 1.03 / 0.70) < 1e-12
    finally:
        C.SHARPGAP_EDGE = oldm
    # M3 : un avis sharp d'âge inconnu est refusé pour une famille figée (comme sharpgap), accepté ailleurs
    hit = {"psh": {"over|2.5": [0.6, "pinnacle", 1]}}
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    assert E.sharp_for({"market": "totals"} | hit, "Over", 2.5, now) is None
    assert E.sharp_for({"market": "alternate_totals_corners"} | hit, "Over", 2.5, now) == 0.6
    print("ok  suites de la relecture : poids 20 % coupé aussi sans modèle approfondi, « pourquoi » seulement pour la famille figée, 🎯 jamais annoncé comme valeur, marge et âge du sharp alignés")


def main():
    test_config_and_weights()
    test_combine()
    test_value_rule()
    test_learn()
    test_record_shadow()
    test_xprice_flow()
    test_label()
    test_goal_weight_and_tuning()
    test_combo_model_guard_and_probable_leg()
    test_review_followups()
    print("tests poids du modèle figé : OK")


if __name__ == "__main__":
    main()
