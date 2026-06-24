#!/usr/bin/env bash
# Dirty-ancilla pebbling Phase-1 test runner. Runs INSIDE the project container
# (always Linux), so the host needs only portable docker commands — works the
# same on Windows, macOS, and Linux:
#
#   docker build -t xagtdep .
#   docker run --rm xagtdep bash experiments/dirty-pebbling/run_tests.sh
#
# Save the output on any OS by redirecting on the host:
#   docker run --rm xagtdep bash experiments/dirty-pebbling/run_tests.sh > results.txt
#
# Run a quick subset (the instances that show wins) via the GLOB env var:
#   docker run --rm -e GLOB='p*_a1_x2_*' xagtdep bash experiments/dirty-pebbling/run_tests.sh
#
# --complex (or -e COMPLEX=1): run a random + hand-picked set of LARGER,
# equivalence-checked XAGs instead of the 42. The hardcoded 42 (used by the
# other CI) are NOT touched either way.
set -e
cd /xagtdep
GLOB="${GLOB:-*.json}"
COMPLEX="${COMPLEX:-0}"
[ "${1:-}" = "--complex" ] && COMPLEX=1

# z3 is baked into the image; self-install if running on an older image.
python3 -c "import z3" 2>/dev/null || {
  apt-get update -qq && apt-get install -y -qq python3-z3 >/dev/null
}

echo "===== 1. regression (expect 126 passed) ====="
./ci_check.sh 2>&1 | tail -2

echo "===== 2. Phase-1 anchors ====="
( cd experiments/dirty-pebbling && python3 -u test_anchors.py )

if [ "$COMPLEX" = "1" ]; then
  echo "===== 3. COMPLEX set: random + hand-picked, equivalence-checked ====="
  COUT="${OUT:-/out}"
  ( cd experiments/dirty-pebbling && \
    python3 -u random_run.py --n "${NRAND:-6}" --timeout "${TIMEOUT:-300}" \
      --method "${METHOD:-proposed}" --out "$COUT" )
  echo ">> complex QASM + summary.tsv written to $COUT (inside the container)."
  echo ">> To keep them on the host, re-run with a mount: -v \"\$PWD/complex_out:/out\""
else
  echo "===== 3. translator metrics (42 XAGs) ====="
  ( cd build && ./QCVerificationTest --data-dir verification_data >/dev/null 2>&1 )

  echo "===== 4. export the 42 benchmark XAGs ====="
  mkdir -p experiments/dirty-pebbling/xags
  for c in "3 1 1" "3 1 2" "3 2 1" "3 2 2" "4 1 1" "4 1 2" "4 2 1" "4 2 2" \
           "5 1 1" "5 1 2" "5 2 2" "6 1 1" "6 1 2" "6 2 2"; do
    set -- $c
    for s in 42 12345 0xdeadbeef; do
      ./build/XAGExport --pis $1 --ands $2 --xors $3 --seed $s \
        > experiments/dirty-pebbling/xags/p$1_a$2_x$3_s$s.json
    done
  done

  echo "===== 5. dirty solver vs translator (glob=$GLOB) ====="
  ( cd experiments/dirty-pebbling && \
    python3 -u compare.py xags/ /xagtdep/build/verification_data \
      --method proposed --timeout 20 --glob "$GLOB" )
fi
