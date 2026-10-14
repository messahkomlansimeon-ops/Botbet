"""CONFIG_ACTIVE.md (kelly/configactive.py, tools/config_active.py) : chaque réglage de config.py avec sa valeur effective, groupé par voie, jamais un secret, « ce qui est figé » en tête, date reprise par status.
Lancer : python -m tests.test_config_active"""
import os
import tempfile
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-cfgactive-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-cfgactive-cache-")

from kelly import config as C, configactive as A  # noqa: E402

SRC = '''
import os
# nombre de jambes maximal d'un combiné
MAX_LEGS = _int("MAX_LEGS", 25)
SEASON_MODEL_WEIGHT = _float("SEASON_MODEL_WEIGHT", 0.2)   # poids du modèle
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
TELEGRAM_TOKEN = _str("TELEGRAM_TOKEN", "")
XBET_PROXY = os.environ.get("XBET_PROXY")
SHARPGAP_EDGE = 0.05
LEAGUE_KEYS = ["a", "b"]
WATCH_FAIL_PASSES = 3
lower = 1
'''


def test_parse_and_render_without_secrets():
    rows = {n: (e, c) for n, e, c in A.parse(SRC)}
    assert list(rows) == ["MAX_LEGS", "SEASON_MODEL_WEIGHT", "ODDS_API_KEY", "TELEGRAM_TOKEN", "XBET_PROXY", "SHARPGAP_EDGE", "LEAGUE_KEYS", "WATCH_FAIL_PASSES"], list(rows)
    assert rows["MAX_LEGS"] == ("MAX_LEGS", "nombre de jambes maximal d'un combiné") and rows["SEASON_MODEL_WEIGHT"][1] == "poids du modèle" and rows["SHARPGAP_EDGE"][0] is None
    cfg = SimpleNamespace(MAX_LEGS=7, SEASON_MODEL_WEIGHT=0.2, ODDS_API_KEY="sk-SECRETVALUE1234567890abcdef", TELEGRAM_TOKEN="123:ABC-DEF-secret", XBET_PROXY="http://user:pw@proxy.test:80",
                          SHARPGAP_EDGE=0.05, LEAGUE_KEYS=["a", "b"], WATCH_FAIL_PASSES=3, ENSEMBLE_FROZEN_MODEL=frozenset({"goals", "h2h"}), ENSEMBLE_ACTIVE_FAMILIES=frozenset({"goals"}))
    text = A.render(cfg, SRC, {"MAX_LEGS": "7", "ODDS_API_KEY": "sk-SECRETVALUE1234567890abcdef"}, root=tempfile.mkdtemp())
    for secret in ("SECRETVALUE", "123:ABC", "user:pw", "proxy.test"):
        assert secret not in text, secret
    assert "| `ODDS_API_KEY` | `défini` | env |" in text and "| `TELEGRAM_TOKEN` | `défini` | défaut |" in text and "| `XBET_PROXY` | `défini` |" in text
    assert "| `MAX_LEGS` | `7` | env | nombre de jambes maximal d'un combiné |" in text and "| `SEASON_MODEL_WEIGHT` | `0.2` | défaut | poids du modèle |" in text
    assert "`LEAGUE_KEYS` | `['a', 'b']`" in text and "`WATCH_FAIL_PASSES` | `3`" in text, "KEYS (pluriel) et PASSES ne sont pas des secrets"
    assert text.index("## Ce qui est figé") < text.index("## saison") and "## sharpgap" in text and "## fill / combinés" in text
    print("ok  réglages avec valeur effective, source (défaut / env) et commentaire ; jamais un secret (clé, jeton, proxy, valeur opaque), KEYS / PASSES non masqués")


def test_real_config_and_frozen():
    text = A.render(C)
    parsed = {n for n, _, _ in A.parse(open(os.path.join(A.ROOT, "kelly", "config.py"), encoding="utf-8").read())}
    listed = {l.split("`")[1] for l in text.splitlines() if l.startswith("| `")}
    assert listed == parsed and len(parsed) > 300, (len(listed), len(parsed))
    assert "Poids du modèle de buts à 0 (gelé, non réappris) sur 5 familles : btts, dc, goals, h2h, team_goals" in text
    for ledger in A.LEDGERS:
        assert f"Ledger {ledger}" in text, ledger
    assert "Coupures en cours" in text
    for g in ("sharpgap", "stats", "saison", "fill / combinés", "mises", "sources", "quotas", "mesure"):
        assert f"\n## {g}\n" in text, g
    row = next(l for l in text.splitlines() if l.startswith("| `COMBO_MAX_FILL_SHARE`"))
    assert "fill / combinés" in text.split(row)[0].rsplit("## ", 1)[1].split("\n")[0], "COMBO_MAX_FILL_SHARE est rangé avec les combinés"
    print("ok  tous les réglages de config.py listés par voie, section « ce qui est figé » (modèle de buts gelé, ledgers, coupures)")


def test_write_and_stamp():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "CONFIG_ACTIVE.md")
    assert A.stamp(path).startswith("réglages : CONFIG_ACTIVE.md absent")
    from datetime import datetime, timezone
    A.write(path, now=datetime(2026, 10, 14, 9, 5, tzinfo=timezone.utc))
    assert A.stamp(path) == "réglages : CONFIG_ACTIVE.md du 14/10" and not os.path.exists(path + ".tmp")
    from kelly import status
    out = str(status.report())
    assert "réglages : CONFIG_ACTIVE.md" in out, out[-600:]
    print("ok  écriture atomique, date reprise par status (« réglages : CONFIG_ACTIVE.md du 14/10 »)")


def main():
    test_parse_and_render_without_secrets()
    test_real_config_and_frozen()
    test_write_and_stamp()
    print("tests réglages actifs : OK")


if __name__ == "__main__":
    main()
