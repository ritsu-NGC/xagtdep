# xagtdep

LLVM-integrated analysis and optimization modules for DAG-based transformations.

## Overview

This project contains five LLVM-integrated modules for various analysis and optimization tasks:

- **DavioDecomposition**: Davio decomposition analysis
- **Function**: Function transformation operations
- **NewMethod**: New method application techniques
- **QC**: Quality check evaluation
- **XAG**: XAG optimization algorithms

It also provides an AIGER reader/converter for EPFL/ABC benchmark flows:
- `AIGReader` parses `aag` (ASCII) and `aig` (binary) files.
- `AIGToQC` converts AIGER → `mockturtle::xag_network` (`XagContext`) → `QCGateList`.

All modules are implemented as dynamically loadable LLVM passes that can be used with LLVM's `opt` tool or integrated into custom LLVM-based workflows.

## Quick Start

### Build

```bash
mkdir build && cd build
cmake -DLLVM_DIR=/usr/lib/llvm-16/cmake ..
make
```

### Run Tests

```bash
# Run individual module tests
./DavioDecompositionTest
./FunctionTest
./NewMethodTest
./QCTest
./XAGTest
./AIGReaderTest

# Run all LLVM passes on test input
./run_passes.sh
```

### Use with LLVM

```bash
# Example: Run XAG pass on LLVM IR
opt-16 -load-pass-plugin=build/lib/libXAG.so \
       -passes="xag" \
       input.ll -o output.bc
```

## Documentation

See [LLVM_INTEGRATION.md](LLVM_INTEGRATION.md) for comprehensive documentation on:
- Building and installation
- LLVM pass usage
- API reference
- Integration examples
- Troubleshooting

### AIG benchmark flow (EPFL/ABC)

```cpp
#include "AIGToQC.h"

// Parse AIGER, build XAG context for QC, and synthesize gates.
xagtdep::QCGateList gates = xagtdep::AIGToQC::synthesize("benchmark.aig");
```

Data flow:

`AIG(AIGER) -> mockturtle::xag_network -> XagContext -> QC gate list (-> QASM via QC pipeline)`

## Requirements

- LLVM 14-18 (tested with 16, 17, and 18)
- CMake 3.10+
- C++17 compatible compiler

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.