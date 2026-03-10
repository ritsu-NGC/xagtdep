#!/bin/bash
# Script to demonstrate LLVM pass integration
# This script shows how to load and run each pass on LLVM IR

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow overriding BUILD_DIR via environment variable or argument
if [ -n "${1:-}" ]; then
    BUILD_DIR="$1"
elif [ -n "${BUILD_DIR:-}" ]; then
    : # use BUILD_DIR from environment
elif [ -f "$SCRIPT_DIR/lib/libDavioDecompositionLib.so" ]; then
    # Running from inside the build directory (e.g. copied by CMake)
    BUILD_DIR="$SCRIPT_DIR"
else
    BUILD_DIR="$SCRIPT_DIR/../build"
fi

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

# LLVM pass plugins are CMake MODULE libraries, which use .so on all platforms
# (including macOS, where only SHARED libraries use .dylib)
LIB_EXT="so"

# Check if pass libraries exist
PASS_LIBS=(
    "$BUILD_DIR/lib/libDavioDecompositionLib.$LIB_EXT"
    "$BUILD_DIR/lib/libFunctionLib.$LIB_EXT"
    "$BUILD_DIR/lib/libNewMethodLib.$LIB_EXT"
    "$BUILD_DIR/lib/libQCLib.$LIB_EXT"
    "$BUILD_DIR/lib/libXAGLib.$LIB_EXT"
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
echo "1. Running DavioDecomposition Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libDavioDecompositionLib.$LIB_EXT" \
    -passes="davio-decomposition" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "2. Running Function Transform Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libFunctionLib.$LIB_EXT" \
    -passes="function-transform" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "3. Running NewMethod Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libNewMethodLib.$LIB_EXT" \
    -passes="new-method" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "4. Running QC Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libQCLib.$LIB_EXT" \
    -passes="qc" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "5. Running XAG Pass..."
$OPT_CMD -load-pass-plugin="$BUILD_DIR/lib/libXAGLib.$LIB_EXT" \
    -passes="xag" \
    -disable-output "$TEST_INPUT" 2>&1 | head -20
echo ""

echo "=== All passes completed successfully! ==="
