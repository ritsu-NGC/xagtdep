#!/usr/bin/env bash
# Dump OpenQASM for every benchmark XAG plus the dirty-pebbling comparison data.
# Runs INSIDE the container; output is written to /out, so mount a host folder
# onto /out to collect the files (works the same on Windows/macOS/Linux):
#
#   docker build -t xagtdep .
#   docker run --rm -v "${PWD}/qasm_out:/out" xagtdep bash experiments/dirty-pebbling/dump_qasm.sh
#
# Result on the host in ./qasm_out/ :
#   <p_a_x_seed>.qasm   one OpenQASM 2.0 file per XAG (proposed-method circuit)
#   summary.tsv         pis ands xors seed gates T tr_anc solver_anc note qasm
#
# Env vars:
#   SOLVE=0            skip the slow dirty solver (dump QASM + translator metrics only)
#   METHOD=existing    use the Existing method instead of Proposed (default proposed)
#
# --complex (or -e COMPLEX=1): dump QASM for a random + hand-picked set of LARGER,
# equivalence-checked XAGs instead of the 42 (which are left untouched).
set -e
cd /xagtdep
OUT="${OUT:-/out}"
METHOD="${METHOD:-proposed}"
SOLVE="${SOLVE:-1}"
COMPLEX="${COMPLEX:-0}"
[ "${1:-}" = "--complex" ] && COMPLEX=1
mkdir -p "$OUT"

# z3 (baked into the image; self-install on older images). Needed whenever the
# solver runs or the complex flow imports it.
python3 -c "import z3" 2>/dev/null || {
  apt-get update -qq && apt-get install -y -qq python3-z3 >/dev/null
}

if [ "$COMPLEX" = "1" ]; then
  SOLVEFLAG=--solve
  [ "$SOLVE" = "0" ] && SOLVEFLAG=--no-solve
  ( cd experiments/dirty-pebbling && \
    python3 -u random_run.py --n "${NRAND:-6}" --timeout "${TIMEOUT:-180}" \
      --method "$METHOD" --out "$OUT" $SOLVEFLAG )
  echo "complex-set QASM + summary.tsv written to the mounted /out folder."
  exit 0
fi

# 1. translator metrics + per-XAG gate lists (source of the QASM)
( cd build && ./QCVerificationTest --data-dir verification_data >/dev/null 2>&1 )

# 2. export the 42 benchmark XAGs
mkdir -p experiments/dirty-pebbling/xags
for c in "3 1 1" "3 1 2" "3 2 1" "3 2 2" "4 1 1" "4 1 2" "4 2 1" "4 2 2" \
         "5 1 1" "5 1 2" "5 2 2" "6 1 1" "6 1 2" "6 2 2"; do
  set -- $c
  for s in 42 12345 0xdeadbeef; do
    ./build/XAGExport --pis $1 --ands $2 --xors $3 --seed $s \
      > experiments/dirty-pebbling/xags/p$1_a$2_x$3_s$s.json
  done
done

# 3. dump QASM + comparison summary
SOLVEFLAG=--solve
[ "$SOLVE" = "0" ] && SOLVEFLAG=--no-solve
( cd experiments/dirty-pebbling && \
  python3 -u dump_qasm.py /xagtdep/build/verification_data xags/ \
    --method "$METHOD" --out "$OUT" $SOLVEFLAG )

echo "QASM + summary.tsv written to the mounted /out folder."
