"""Inventaire des réglages actifs : CONFIG_ACTIVE.md à la racine (python tools/config_active.py).

Chaque réglage de kelly/config.py avec sa valeur EFFECTIVE sur la machine (défaut du code ou variable d'environnement / .env, chargés par l'appelant), sa provenance, et la ligne de commentaire de
config.py en face, groupés par voie. Jamais de secret : un nom qui contient KEY, TOKEN, SECRET, PASS ou PROXY (mot entier, séparé par « _ ») est écrit « défini » ou « non défini », et toute valeur qui
ressemble à une adresse avec identifiants aussi. En tête : « ce qui est figé » (modèle de buts gelé, ledgers pré-déclarés, coupures en cours). Régénéré par « kelly update » et par « python bot.py --check » ;
sa date est reprise par status."""
import ast
import io
import os
import re
import sys
import tokenize
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE = os.path.join(ROOT, "CONFIG_ACTIVE.md")
TITLE = "# Réglages actifs de Kelly"
SECRET_WORDS = {"KEY", "TOKEN", "SECRET", "PASS", "PASSWORD", "PASSWD", "PROXY", "APIKEY"}
LEDGERS = ("audit_reserve_ledger.json", "season_ledger.json", "step1_ledger.json", "combos_ledger.json", "edgemap_ledger.json", "oddsonly_ledger.json", "live_ledger.json", "families_ledger.json")
# (groupe, motifs sur le nom) : le premier qui correspond gagne
GROUPS = (
    ("sharpgap", r"^(SHARPGAP|STRATEGY_SHARPGAP|KELLY_FRACTION_SHARPGAP|SHARP_|XALERT|GAP)"),
    ("saison", r"^(SEASON|STRATEGY_SEASON|KELLY_FRACTION_SEASON)"),
    ("stats", r"^(STATS_|STRATEGY_STATS|KELLY_FRACTION_STATS|SHOTS_|FAIR_STATS|FAIR_SHOTS|FAIR_TRACK_STATS|FAIR_TRACK_SHOTS|CTX_|CROSS_LEAGUE|XG_|GOAL|MEANS|PLAYER|SOT_|PM_FLAG)"),
    ("fill / combinés", r"^(COMBO|COMBOS|COMBI|FILL|LEG_|MAX_LEGS|CUSTOM_|DP_BIN|FAIR_COMBO|FAIR_P_|FAIR_MIN|FAIR_SHRINK|FAIR_MARGIN|SPECIAL|VIABLE|NEW_RULE|LIVE_COMBO|MARKET_WEIGHT|TYPICAL|BEST_NOTE|DISAGREE|FEW_BOOKS)"),
    ("mises", r"^(KELLY_FRACTION|NOVALUE|STAKE|MIN_STAKE|UNITS|STREAK|FAIR_COMBO_STAKE|BUDGET|FEATURED_SHARE)"),
    ("quotas", r"(MONTH|MONTHLY|CREDITS|_MB$|MAX_BYTES|PLAN_MB|QUOTA|RESET_DAY|RESERVE|RATE_WAIT|MIN_GAP|CACHE_HOURS|_SECONDS$|MAX_MATCHES|MAX_TEAMS|PAGES_PER_RUN|PER_RUN|_REQUESTS$)"),
    ("sources", r"^(ODDS_API|S365|XBET|FM_|APISTATS|STATS_DATE|REGIONS|SPORTS|CATALOGUE|LEAGUE_KEYS|XB_|TOP_LEAGUES|SECOND_TIER|BOOT_|TOP_BOOKS|SOURCES|PROP_|FEATURED|FIXTURES|EVENTS_CACHE|MIN_BOOKMAKERS|ODDS_RANGE|XWORLD)"),
    ("mesure", r"^(EDGEMAP|ODDSONLY|STRATEGY_ODDSONLY|ENSEMBLE|CLV|CALIB|LEARN|TUNE|REPLAY|FREE_CLOSE|RIDGE|STRENGTH|TRACK|CLOSE|FAIR_CALIB|FAIR_TRACK|FAIR_KEEP|MODEL_WEIGHT|COMBO_LEG|X_MARGIN|WATCH|LIVE|ALERT|QUIET|EVENING|MARKET_LABELS|RULE)"),
)


def is_secret(name):
    return bool(SECRET_WORDS & set(name.split("_")))


def looks_secret(value):
    """Une adresse avec identifiants (« http://u:p@hôte ») ou une longue chaîne opaque."""
    if not isinstance(value, str):
        return False
    return bool(re.search(r"://[^/\s]*@", value) or re.fullmatch(r"[A-Za-z0-9_\-]{32,}", value))


def group_of(name):
    for g, pat in GROUPS:
        if re.search(pat, name):
            return g
    return "autres"


def parse(source):
    """[(nom, nom de variable d'environnement ou None, commentaire)] des réglages de niveau module de config.py, dans l'ordre du fichier."""
    tree = ast.parse(source)
    lines = source.splitlines()
    inline = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            inline[tok.start[0]] = tok.string.lstrip("# ").strip()
    out = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if not name.isupper():
            continue
        env = None
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Call) and sub.args and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", sub.args[0].value):
                env = sub.args[0].value
                break
        notes = [inline[l] for l in range(node.lineno, (node.end_lineno or node.lineno) + 1) if l in inline]
        l = node.lineno - 2
        above = []
        while l >= 0 and lines[l].lstrip().startswith("#"):
            above.insert(0, lines[l].lstrip("# ").strip())
            l -= 1
        out.append((name, env, " ".join(notes) if notes else " ".join(above)))
    return out


def show(value, limit=160):
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def effective(config, name, env_name, environ):
    """(texte de la valeur, provenance) sans jamais écrire un secret."""
    value = getattr(config, name, None)
    where = "env" if env_name and (environ.get(env_name) or "").strip() else "défaut"
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)      # PROXY_PLAN_MB est un nombre de Mo, pas une adresse
    if (not numeric and (is_secret(name) or (env_name and is_secret(env_name)))) or looks_secret(value) or (is_secret(name) and isinstance(value, str)):
        return ("défini" if value not in (None, "", 0, False, ()) else "non défini"), where
    return show(value), where


def frozen_section(config, root=ROOT):
    out = ["## Ce qui est figé", ""]
    fm = sorted(getattr(config, "ENSEMBLE_FROZEN_MODEL", ()) or ())
    none = "aucune (comportement d'avant)"
    out.append(f"- Poids du modèle de buts à 0 (gelé, non réappris) sur {len(fm)} familles : {', '.join(fm) if fm else none}.")
    act = sorted(getattr(config, "ENSEMBLE_ACTIVE_FAMILIES", ()) or ())
    out.append(f"- Familles où l'ensemble décide : {', '.join(act) if act else 'aucune (mode fantôme seul)'}.")
    import json
    for name in LEDGERS:
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                d = json.load(f)
            declared = (d.get("prereg") or {}).get("declared_on") or d.get("declared_on") or "?"
            written = d.get("final") if d.get("final") is not None else d.get("result")
            out.append(f"- Ledger {name} : pré-déclaré le {declared} ; " + ("résultat écrit (ne plus relancer --final pour audit_reserve et season)" if written is not None else "pas de résultat écrit"))
        except (OSError, ValueError, AttributeError):
            out.append(f"- Ledger {name} : absent ou illisible")
    try:
        from . import lanestats
        cuts = lanestats.cut_lines()
    except Exception as e:                      # noqa: BLE001
        cuts = [f"(coupures illisibles : {type(e).__name__})"]
    out.append("- Coupures en cours : " + ("aucune" if not cuts else ""))
    out += [f"  - {c}" for c in cuts]
    return out


def render(config=None, source=None, environ=None, now=None, root=ROOT):
    """Le texte de CONFIG_ACTIVE.md."""
    if config is None:
        from . import config
    environ = os.environ if environ is None else environ
    if source is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"), encoding="utf-8") as f:
            source = f.read()
    now = now or datetime.now(timezone.utc)
    rows = {}
    for name, env_name, note in parse(source):
        value, where = effective(config, name, env_name, environ)
        rows.setdefault(group_of(name), []).append((name, value, where, note))
    out = [TITLE, "", f"Généré le {now.strftime('%d/%m/%Y')} à {now.strftime('%H:%M')} UTC (python tools/config_active.py ; régénéré par « kelly update » et « python bot.py --check »). "
           "Valeur effective sur cette machine : défaut du code ou variable d'environnement / .env. Jamais de secret : les noms avec KEY, TOKEN, SECRET, PASS ou PROXY sont écrits « défini » ou « non défini ».", ""]
    out += frozen_section(config, root) + [""]
    for g in [x for x, _ in GROUPS] + ["autres"]:
        if not rows.get(g):
            continue
        out += [f"## {g}", "", "| réglage | valeur | source | commentaire |", "|---|---|---|---|"]
        for name, value, where, note in rows[g]:
            cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")      # noqa: E731
            out.append(f"| `{name}` | `{cell(value)}` | {where} | {cell(note)[:240]} |")
        out.append("")
    return "\n".join(out)


def write(path=None, **kw):
    path = path or FILE
    text = render(**kw)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def stamp(path=None):
    """« réglages : CONFIG_ACTIVE.md du 14/10 » (date de la première ligne « Généré le »), ou « réglages : CONFIG_ACTIVE.md absent (python tools/config_active.py) »."""
    path = path or FILE
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"Généré le (\d\d/\d\d)/\d{4}", line)
                if m:
                    return f"réglages : CONFIG_ACTIVE.md du {m.group(1)}"
    except OSError:
        pass
    return "réglages : CONFIG_ACTIVE.md absent (python tools/config_active.py)"


def refresh():
    """Réécrit CONFIG_ACTIVE.md sans jamais lever (appelé par bot.py --check)."""
    try:
        write()
        return True
    except Exception as e:                      # noqa: BLE001
        print(f"[réglages] CONFIG_ACTIVE.md non écrit : {type(e).__name__}: {e}", file=sys.stderr)
        return False
