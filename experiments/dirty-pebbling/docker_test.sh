#!/usr/bin/env bash
# One-command Docker test for the dirty-ancilla pebbling Phase 1.
#
#   bash experiments/dirty-pebbling/docker_test.sh
#
# Builds the project image, runs the existing regression (proves nothing broke),
# the Phase-1 anchors, and the dirty-solver-vs-translator comparison over the
# 42-XAG benchmark. Console output streams live; copies are saved to
# experiments/dirty-pebbling/out/ on the host so you can read them afterwards.
#
# Heads-up: the in-container solver uses z3 4.8.12 (apt), which is slow on the
# UNSAT optimality proofs — the comparison step can take ~10-15 min. Pass a
# smaller subset via XAG_GLOB to go faster, e.g.:
#   XAG_GLOB='p*_a1_x2_*' bash experiments/dirty-pebbling/docker_test.sh
set -euo pipefail

cd "$(dirname "$0")/../.."          # repo root
mkdir -p experiments/dirty-pebbling/out
GLOB="${XAG_GLOB:-*.json}"

echo ">> building image (first time downloads LLVM; later builds are cached)"
docker build -t xagtdep .

echo ">> running tests in container"
docker run --rm \
  -e "GLOB=$GLOB" \
  -v "$PWD/experiments/dirty-pebbling/out":/out \
  xagtdep bash -c '
    set -e
    apt-get update -qq && apt-get install -y -qq python3-z3 >/dev/null

    echo "===== 1. regression (expect 126/126 equivalent) ====="
    ./ci_check.sh 2>&1 | tee /out/regression.txt | tail -2

    echo "===== 2. Phase-1 anchors ====="
    ( cd experiments/dirty-pebbling && python3 -u test_anchors.py ) | tee /out/anchors.txt

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
        --method proposed --timeout 20 --glob "$GLOB" ) | tee /out/compare.txt
  '

echo
echo ">> done. results saved on the host:"
echo "   experiments/dirty-pebbling/out/regression.txt"
echo "   experiments/dirty-pebbling/out/anchors.txt"
echo "   experiments/dirty-pebbling/out/compare.txt"
