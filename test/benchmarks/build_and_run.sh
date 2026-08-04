#!/bin/bash
# Compile gen_bench.cpp inside the xagtdep Docker image (against the image's
# checked-out repo + FetchContent deps), generate the benchmark set into
# /bench/data, and strict-QCEC-verify the verified tier.
#
# Run from the host:
#   docker run --rm -v "$PWD/test/benchmarks":/bench xagtdep bash /bench/build_and_run.sh
set -uo pipefail

R=/xagtdep
DEPS=$R/build/_deps

INC="-I$R/include -I$R/test -I$R/src/QC"
for d in "$DEPS"/*-src/include; do [ -d "$d" ] && INC+=" -I$d"; done
# caterpillar vendors mockturtle/kitty/json/etc. under lib/<name>, where each
# <name> dir is itself the include root.
for d in "$DEPS"/caterpillar-src/lib/*; do
  [ -d "$d" ] && INC+=" -I$d"
  [ -d "$d/include" ] && INC+=" -I$d/include"
done
INC+=" -I$(llvm-config-16 --includedir)"

LIBS="$(llvm-config-16 --ldflags) $(llvm-config-16 --libs support) $(llvm-config-16 --system-libs)"

echo "== compiling gen_bench =="
g++ -std=c++17 -O2 -pthread -DFMT_HEADER_ONLY=1 $INC \
    /bench/gen_bench.cpp \
    $R/test/QCVerificationCommon.cpp \
    $R/src/QC/ToffoliGadgets.cpp \
    $R/src/QC/XAGToGateList.cpp \
    $R/src/QC/ExistingMethod.cpp \
    $R/src/QC/ProposedMethod.cpp \
    $R/src/QC/PebblingMethod.cpp \
    $R/src/QC/BennettSequence.cpp \
    $R/src/QC/SequenceJson.cpp \
    $R/src/QC/RandomXAG.cpp \
    -o /tmp/gen_bench $LIBS -pthread || { echo "COMPILE FAILED"; exit 1; }

echo "== generating benchmark set =="
rm -rf /bench/data
/tmp/gen_bench /bench/data || { echo "GENERATION FAILED"; exit 1; }

echo "== strict verification of the verified tier =="
cd $R
python3 test/verify_circuits.py --strict /bench/data/verified
V=$?

echo "== big tier: file inventory (synthesis + validator already ran) =="
ls -la /bench/data/big/ | head -30
du -sh /bench/data

exit $V
