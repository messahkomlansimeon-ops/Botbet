"""Décision 5 (hors ligne) : distribution quotidienne des écarts 1xBet / sharp par heure de lecture (kelly/gapdist.py), fantômes +1,5..+3 % suivis avec leur dernière lecture, document
séparé de tracked.json, rapport tools/audit_gaps.py (limite horaire). Aucun effet sur un pari. Lancer : python -m tests.test_gapdist"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-gapdist-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-gapdist-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, gapdist as G, store  # noqa: E402
from tools import audit_gaps as A  # noqa: E402

D0 = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)


def sel(i, xodds=2.0, p=0.5, age_min=10, start_in_min=400, src="pinnacle", n_sharp=1, now=D0, **kw):
    """Pari réel (avis sharp dans prob_book) remis au prix 1xBet."""
    return {"key": f"k{i}|totals||2.5|Over", "id": f"m{i}", "sport_key": "soccer_epl", "market": "totals", "name": "Over", "point": 2.5, "xodds": xodds, "xref": [i, 1, 2.5],
            "prob_book": p, "prob_src": src, "n_sharp": n_sharp, "sharp_at": (now - timedelta(minutes=age_min)).isoformat(),
            "commence": (now + timedelta(minutes=start_in_min)).isoformat()} | kw


def test_observe():
    store.save("tracked.json", [])
    before = store.load("tracked.json", [])
    rows = [sel(1, 2.0, 0.50),                            # écart 0 % (tranche 0..1.5)
            sel(2, 2.06, 0.50),                           # +3 % : au seuil, tranche 3..5
            sel(3, 2.04, 0.50),                           # +2 % : fantôme (1.5..3), sharp frais
            sel(4, 2.04, 0.50, age_min=200),              # +2 % mais sharp vieux : pas de fantôme
            sel(5, 2.04, 0.50, start_in_min=10),          # +2 % mais match imminent (< SHARPGAP_SOON_MIN) : pas de fantôme
            sel(6, 2.04, 0.50, src="sharp", n_sharp=1),   # un seul book sharp sans Pinnacle : pas un avis
            sel(7, 2.30, 0.50, start_in_min=60),          # +15 % avec le match dans ≤ 90 min, sharp frais
            sel(8, None, 0.50),                           # pas de prix 1xBet : ignoré
            sel(9, 2.0, 0.5, commence="2026-10-14T03:00:00Z")]   # match commencé : ignoré
    assert G.observe(rows, D0) == 6
    doc = store.load(G.DOC, {})
    cell = doc["days"]["2026-10-14"]["04"]
    assert cell["n"] == 6 and cell["weak"] == 1 and cell["bins"] == {"0..1.5": 1, "3..5": 1, "1.5..3": 3, "10..25": 1}, cell
    assert cell["age"] == {"≤30": 5, "91-240": 1}, cell["age"]
    assert cell["fresh"] == {"0..1.5": 1, "3..5": 1, "1.5..3": 2, "10..25": 1} and cell["soon"] == {"1.5..3": 1, "10..25": 1}, cell
    gk = set(doc["ghost"])
    assert gk == {sel(3)["key"]}, gk
    # une 2e lecture dans la même heure ne recompte rien ; une lecture à une autre heure compte et met à jour la dernière lecture du fantôme
    assert G.observe(rows, D0 + timedelta(minutes=20)) == 0, "une 2e lecture dans la même heure ne recompte rien dans la distribution"
    gk3 = sel(3)["key"]
    assert store.load(G.DOC, {})["ghost"][gk3]["last"]["at"].startswith("2026-10-14T04:20"), "mais elle met à jour la dernière lecture du fantôme"
    later = D0 + timedelta(hours=7)
    assert G.observe([sel(3, 2.00, 0.505, now=later, start_in_min=50)], later) == 1
    doc = store.load(G.DOC, {})
    g = doc["ghost"][sel(3)["key"]]
    assert g["xodds"] == 2.04 and g["p"] == 0.5 and g["edge"] == 0.02, "la première lecture ne change jamais"
    assert g["last"]["x"] == 2.0 and g["last"]["p"] == 0.505 and g["last"]["lead"] == 50, g["last"]
    assert g["last_fresh"] == g["last"], "sharp frais (10 min) : c'est aussi la dernière lecture à sharp frais"
    assert len(doc["days"]["2026-10-14"]["04"]["keys"]) >= 7 and "keys" in doc["days"]["2026-10-14"]["11"]
    assert store.load("tracked.json", []) == before, "aucune ligne fantôme dans tracked.json"
    print("ok  distribution : tranches, âge du sharp, avance sur le match, un seul book sharp écarté, fantômes +1,5..+3 % (sharp frais, match pas imminent), dernière lecture, rien dans tracked.json")


def test_prune_and_robust():
    old = (D0 - timedelta(days=G.KEEP_DAYS + 5)).date().isoformat()
    doc = store.load(G.DOC, {})
    doc["days"][old] = {"03": {"n": 1, "bins": {}, "age": {}, "fresh": {}, "soon": {}, "weak": 0, "keys": ["abc"]}}
    doc["ghost"]["old"] = {"day": "2000-01-01"}
    store.save(G.DOC, doc)
    G.observe([sel(20)], D0 + timedelta(days=1))
    doc = store.load(G.DOC, {})
    assert old not in doc["days"] and "old" not in doc["ghost"], "élagage des vieux jours et des vieux fantômes"
    assert all("keys" not in c for c in doc["days"]["2026-10-14"].values()), "les clés de lecture ne sont gardées que pour le jour courant"
    store.save(G.DOC, "abîmé")
    assert G.observe([sel(21)], D0) == 1 and isinstance(store.load(G.DOC, {}), dict), "document abîmé : repart de zéro, ne lève pas"
    assert G.observe([{"pas": "une sélection"}, {"key": "x"}], D0) == 0
    print("ok  élagage, document abîmé, sélections incomplètes")


def test_report():
    store.save(G.DOC, {})
    G.observe([sel(1, 2.0, 0.5), sel(2, 2.06, 0.5), sel(3, 2.04, 0.5), sel(4, 2.30, 0.5, start_in_min=60)], D0)
    G.observe([sel(3, 2.0, 0.505, now=D0 + timedelta(hours=7), start_in_min=50)], D0 + timedelta(hours=7))
    out = A.report(store.load(G.DOC, {}))
    assert "1. DISTRIBUTION" in out and "2. FRAÎCHEUR" in out and "3. FANTÔMES" in out and "4. CE QUE CE RAPPORT" in out
    assert "04h" in out and "11h" in out and "De +3 % à +25 %" in out and "1 suivis" in out, out
    assert "n=1  " in out and "trop peu pour conclure" in out, "un seul fantôme : jamais présenté comme une mesure"
    assert "moy +3.0 %   (trop peu pour conclure)   (à la prise, mêmes paris : moy +2.0 %)" in out, out     # clôture sharp approchée : 2,04 x 0,505 - 1 ; à la prise : 2,04 x 0,5 - 1
    assert "dernier prix) : n=1    moy +2.0 %" in out, out                                                       # ln(2,04 / 2,00)
    empty = A.report({})
    assert "Aucune donnée" in empty and "Aucun encore" in empty
    assert G.summary_line("2026-10-14") == "écarts 1xBet / sharp : 5 lectures aujourd'hui, 1 fantômes +1,5..+3 % suivis (tools/audit_gaps.py)", G.summary_line("2026-10-14")
    store.save(G.DOC, {})
    assert G.summary_line() == ""
    print("ok  rapport : distribution, fraîcheur par heure (limite horaire), fantômes, limites ; vide = dit")


def test_ghost_closing_rules():
    """Revue du 14/10 : un fantôme jamais relu n'a pas de clôture (sinon CLV nul par construction) ; un âge de sharp inconnu n'est pas frais ; la clôture sharp est la dernière lecture à
    sharp FRAIS, distincte de la dernière lecture."""
    store.save(G.DOC, {})
    # a : créé à 100 min du match, jamais relu ; b : relu avec sharp frais ; c : relu mais le sharp a vieilli ; d : relu, âge du sharp inconnu
    first = [sel(i, 2.04, 0.50, start_in_min=100) for i in "abcd"]
    G.observe(first, D0)
    t1 = D0 + timedelta(minutes=40)
    G.observe([sel("b", 2.0, 0.51, now=t1, start_in_min=60, age_min=15),
               sel("c", 2.0, 0.51, now=t1, start_in_min=60, age_min=300),
               {k: v for k, v in sel("d", 2.0, 0.51, now=t1, start_in_min=60).items() if k != "sharp_at"}], t1)
    doc = store.load(G.DOC, {})
    gh = {k.split("|")[0]: v for k, v in doc["ghost"].items()}
    assert "last" not in gh["ka"] and gh["kb"]["last_fresh"]["p"] == 0.51
    assert gh["kc"]["last"]["age"] == 300 and "last_fresh" not in gh["kc"], "sharp vieux : dernière lecture, mais pas une clôture sharp"
    assert gh["kd"]["last"]["age"] is None and "last_fresh" not in gh["kd"], "âge inconnu : jamais frais"
    out = A.section_ghost(doc)
    txt = "\n".join(out)
    assert "3 relus au moins une fois" in txt and "1 avec une dernière lecture à sharp frais" in txt and "biais de survie" in txt, txt
    assert "n=1 " in txt, txt
    print("ok  fantômes : pas de clôture sans relecture, âge inconnu ou vieux écarté, clôture sharp = dernière lecture fraîche, biais de survie dit")


def test_hourly_fresh_sharp_reaches_the_distribution():
    """Le passage horaire rafraîchit le sharp des paris réels (xalerts._fresh_sharp) : cette lecture à sharp FRAIS entre dans les colonnes « frais » même si la lecture du matin (sharp
    vieux) du même pari a déjà été comptée dans l'heure ; la mesure se fait AVANT les gardes (voie sharpgap coupée ou SHARPGAP_ENABLED éteint : elle continue) et ne change ni `picks` ni les annonces."""
    import copy
    from kelly import config as C, xalerts
    store.save(G.DOC, {})
    t = datetime(2026, 10, 14, 13, 5, tzinfo=timezone.utc)
    old = sel("h1", 2.06, 0.5, now=t, age_min=540, start_in_min=70)                  # lu à 04:00 : sharp de 9 h
    fresh = dict(old, prob_book=0.51, sharp_at=(t - timedelta(minutes=5)).isoformat())      # relevé de clôture de 13:05
    today = {"day": "2026-10-14", "picks": [old], "picks_at": "04:30"}
    G.observe([old], t, today)
    snapshot = copy.deepcopy(today)

    class Reader:                                                                  # relecture 1xBet en direct : 1,70 = l'écart a disparu au prix réel
        calls = 0

        def get(self, x):
            Reader.calls += 1
            return None
    out = xalerts.gap_items(today, t, Reader(), closing=[fresh])
    assert out == [] and today == snapshot, "aucune annonce, picks et today intacts"
    cell = store.load(G.DOC, {})["days"]["2026-10-14"]["13"]
    assert cell["n"] == 1 and cell["bins"] == {"3..5": 1} and cell["age"] == {">240": 1}, cell          # la lecture du matin, comptée une fois
    assert cell["fresh"] == {"5..10": 1} and cell["soon"] == {"5..10": 1}, cell                          # la lecture à sharp frais (2,06 x 0,51 - 1 = +5,1 %) est comptée à part
    assert cell["xage"] == {">240": 1}, cell["xage"]
    # même mesure quand la voie sharpgap est coupée ou l'option éteinte : l'annonce s'arrête, pas la mesure
    for kw in ({"SHARPGAP_ENABLED": False}, {"STRATEGY_SHARPGAP": "ghost"}):
        old_vals = {k: getattr(C, k) for k in kw}
        for k, v in kw.items():
            setattr(C, k, v)
        try:
            from kelly import lanes
            lanes.reset()
            store.save(G.DOC, {})
            assert xalerts.gap_items(today, t + timedelta(minutes=1), Reader(), closing=[fresh]) == []
            c2 = store.load(G.DOC, {})["days"]["2026-10-14"]["13"]
            assert c2["fresh"] == {"5..10": 1}, (kw, c2)
        finally:
            for k, v in old_vals.items():
                setattr(C, k, v)
            lanes.reset()
    # un 2e passage dans la même heure ne recompte rien
    assert G.observe([fresh], t + timedelta(minutes=30), today) == 0
    print("ok  passage horaire : la lecture à sharp frais est comptée à côté de celle du matin, avant les gardes, sans toucher picks ni annonces")


def main():
    test_observe()
    test_prune_and_robust()
    test_ghost_closing_rules()
    test_hourly_fresh_sharp_reaches_the_distribution()
    test_report()
    print("tests gapdist : OK")


if __name__ == "__main__":
    main()
