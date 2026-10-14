#!/usr/bin/env bash
# Suite complète de Kelly : un processus par fichier tests/test_*.py, JOBS en parallèle (8 par défaut ; test_tune, le plus long, borne la durée),
# puis le contrôle qu'AUCUNE écriture n'a eu lieu dans le vrai cache des sources (.cache/sources). Même script pour la session de travail et pour la CI.
# Sortie : « FAIL <test> » + les dernières lignes de chaque échec, puis « pass=N fail=N » et « cache réel écrit: N » ; code 1 au moindre échec ou écriture.
cd "$(dirname "$0")/.." || exit 2
JOBS="${JOBS:-8}"
MARK=$(mktemp); touch "$MARK"
LOGS=$(mktemp -d)
export LOGS
run1() {
    n=$(basename "$1" .py)
    if ${PYTHON:-python} -m "tests.$n" > "$LOGS/$n.log" 2>&1; then echo PASS > "$LOGS/$n.status"; else echo FAIL > "$LOGS/$n.status"; fi
}
export -f run1
ls tests/test_*.py | xargs -P "$JOBS" -I{} bash -c 'run1 {}'
pass=0; fail=0
for s in "$LOGS"/*.status; do
    n=$(basename "$s" .status)
    if [ "$(cat "$s")" = PASS ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL $n"; tail -n 20 "$LOGS/$n.log"; fi
done
echo "pass=$pass fail=$fail"
written=$(find .cache/sources -newer "$MARK" -type f 2>/dev/null | wc -l)
echo "cache réel écrit: $written"
rm -rf "$LOGS" "$MARK"
[ "$fail" -eq 0 ] && [ "$written" -eq 0 ]
