#!/bin/bash
# Script to demonstrate LLVM pass integration
# This script shows how to load and run each pass on LLVM IR

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/../build"

echo "=== LLVM Pass Integration Test ==="
echo ""

# Check if build directory exists
if [ ! -d "$BUILD_DIR" ]; then
    echo "Error: Build directory not found. Please build the project first."
    echo "Run: mkdir build && cd build && cmake -DLLVM_DIR=/usr/lib/llvm-16/cmake .. && make"
    exit 1
fi

# Find available opt version
OPT_CMD=""
for ver in opt opt-18 opt-17 opt-16 opt-15 opt-14; do
    if command -v $ver &> /dev/null; then
        OPT_CMD=$ver
        break
    fi
done

if [ -z "$OPT_CMD" ]; then
    echo "Error: No opt command found. Please install LLVM."
    exit 1
fi

echo "Using: $OPT_CMD"
echo ""

# Check if pass libraries exist
PASS_LIBS=(
    "$BUILD_DIR/lib/libXAGOptimizerLib.so"
    "$BUILD_DIR/lib/libFunctionLib.so"
    "$BUILD_DIR/lib/libNewMethodLib.so"
    "$BUILD_DIR/lib/libQCLib.so"
    "$BUILD_DIR/lib/libXAGLib.so"
)

echo "Checking for pass libraries..."
for lib in "${PASS_LIBS[@]}"; do
    if [ ! -f "$lib" ]; then
        echo "Error: Pass library not found: $lib"
        exit 1
    fi
    echo "  ✓ Found: $(basename $lib)"
done
echo ""

# Test input file
TEST_INPUT="$SCRIPT_DIR/test_input.ll"
if [ ! -f "$TEST_INPUT" ]; then
    echo "Error: Test input file not found: $TEST_INPUT"
    exit 1
fi

echo "Running passes on test input..."
echo ""

# Run each pass using opt
echo "1. Running XAGOptimizer Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libXAGOptimizerLib.so" \
    -passes="xag-optimizer" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "2. Running Function Transform Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libFunctionLib.so" \
    -passes="function-transform" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "3. Running NewMethod Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libNewMethodLib.so" \
    -passes="new-method" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "4. Running QC Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libQCLib.so" \
    -passes="qc" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "5. Running XAG Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libXAGLib.so" \
    -passes="xag" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "=== All passes completed successfully! ==="
