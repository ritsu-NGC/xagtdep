# Dockerfile — reproducible build/test environment for xagtdep.
#
# Mirrors .github/workflows/ci.yml (ubuntu-22.04 + LLVM 16 from apt.llvm.org)
# so results inside the container match CI. Use cases:
#   - one-command onboarding (no local LLVM/Qiskit setup)
#   - reproducing CI failures locally, including on macOS hosts
#   - ASP-DAC artifact evaluation: reviewers re-run the sweep + QCEC checks
#
# Build:
#   docker build -t xagtdep .
#
# Run the strict CI lane (same steps as ci.yml):
#   docker run --rm xagtdep ./ci_check.sh
#
# Full 42-XAG sweep + strict QCEC verification:
#   docker run --rm xagtdep bash -c \
#     "cd build && ./QCVerificationTest && python3 ../test/verify_circuits.py --strict verification_data"
#
# Interactive shell (mount your checkout for incremental dev):
#   docker run --rm -it -v "$PWD":/xagtdep xagtdep bash

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Base toolchain + Python. python3-dev is included so the optional embedded
# Qiskit bridge (-DXAGTDEP_ENABLE_PYTHON=ON) also builds in-container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        wget \
        gnupg \
        python3 \
        python3-pip \
        python3-dev \
        python3-z3 \
        zlib1g-dev \
        libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

# LLVM 16 from apt.llvm.org — same source and version as CI (ci.yml).
RUN wget -qO /etc/apt/trusted.gpg.d/llvm.asc https://apt.llvm.org/llvm-snapshot.gpg.key \
    && echo "deb http://apt.llvm.org/jammy/ llvm-toolchain-jammy-16 main" \
         > /etc/apt/sources.list.d/llvm16.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends llvm-16-dev \
    && rm -rf /var/lib/apt/lists/*

# Python verification stack. qiskit is pinned to the version the regression
# gate (test/ci_passing_set.json) was generated with — QCEC/statevector
# results are part of the gate's provenance, so version drift matters.
RUN pip3 install --no-cache-dir \
        qiskit==2.4.1 \
        "mqt.qcec>=2.5"

WORKDIR /xagtdep
COPY . .

# Strip CR from shell scripts in case the build context was checked out on
# Windows (CRLF) — bash in this Linux image cannot run CRLF scripts.
RUN find . -name '*.sh' -not -path './build/*' -exec sed -i 's/\r$//' {} +

# Configure + build (FetchContent clones caterpillar/mockturtle here).
RUN cmake -B build -DLLVM_DIR=/usr/lib/llvm-16/lib/cmake/llvm \
    && cmake --build build -j"$(nproc)"

# Convenience runner replicating the strict CI lane end-to-end.
RUN printf '%s\n' \
      '#!/bin/bash' \
      'set -euxo pipefail' \
      'cd build && ctest -E QCVerificationTest --output-on-failure' \
      './CIQCVerification --data-dir verification_data_ci' \
      'cd .. && python3 test/verify_circuits.py --strict build/verification_data_ci' \
      > /xagtdep/ci_check.sh \
    && chmod +x /xagtdep/ci_check.sh

CMD ["/bin/bash"]
