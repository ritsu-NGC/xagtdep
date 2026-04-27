#!/bin/bash
# run_verification.sh — Full verification pipeline for xagtdep synthesis algorithms.
#
# Usage: bash test/run_verification.sh [build_dir]
#   build_dir defaults to "build"

set -e

BUILD_DIR="${1:-build}"
VENV_DIR=".venv"
DATA_DIR="$BUILD_DIR/verification_data"
Q3SAT_DIR="/tmp/xagtdep-deps/q3SATlib/src"

echo "=== xagtdep Verification Pipeline ==="
echo ""

# Step 1: Build
echo "--- Step 1: Building project ---"
cmake --build "$BUILD_DIR" -j$(sysctl -n hw.ncpu 2>/dev/null || nproc)
echo ""

# Step 2: Run C++ tests (generates JSON data)
echo "--- Step 2: Running C++ tests ---"
cd "$BUILD_DIR"
ctest --output-on-failure
cd ..
echo ""

# Step 3: Run Python verification
echo "--- Step 3: Running Python equivalence verification ---"
source "$VENV_DIR/bin/activate"
python test/verify_circuits.py "$DATA_DIR"
echo ""

# Step 4: Run q3SATlib comparison (optional)
echo "--- Step 4: Running q3SATlib metric comparison ---"
if [ -d "$Q3SAT_DIR" ]; then
    python test/compare_metrics.py "$DATA_DIR" "$Q3SAT_DIR"
else
    echo "q3SATlib not found at $Q3SAT_DIR — skipping."
    echo "Clone it: git clone https://github.com/ritsu-NGC/q3SATlib /tmp/xagtdep-deps/q3SATlib"
fi
echo ""

echo "=== Verification Pipeline Complete ==="
