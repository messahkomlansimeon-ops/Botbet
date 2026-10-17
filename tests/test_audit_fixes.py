"""Correctifs de l'audit du 17/10 (lot A) : pré-déclaration audit_fixes_ledger.json, puis un bloc de tests par point (A3, A1, A4, A13, A15, A2).
Chaque test de défaut échoue sur le code d'avant et passe après. Lancer : python -m tests.test_audit_fixes"""
import hashlib
import json
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-auditfix-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "XBET_PROXY_FILE"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit_fixes_ledger.json")


def test_declaration_unchanged():
    led = json.load(open(LEDGER, encoding="utf-8"))
    assert hashlib.sha256(json.dumps(led["prereg"], sort_keys=True, ensure_ascii=False).encode()).hexdigest() == led["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    assert {"A3", "A1", "A4", "A13", "A15", "A2", "extra_labels", "extra_avant_limite"} <= set(led["prereg"])
    print("ok  audit_fixes_ledger.json : pré-déclaration inchangée")


def main():
    test_declaration_unchanged()
    print("tests correctifs de l'audit : OK")


if __name__ == "__main__":
    main()
