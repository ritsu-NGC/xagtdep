# dagtdep

LLVM-integrated analysis and optimization modules for DAG-based transformations.

## Overview

This project contains five LLVM-integrated modules for various analysis and optimization tasks:

- **XAGOptimizer**: XAG optimizer analysis
- **Function**: Function transformation operations
- **NewMethod**: New method application techniques
- **QC**: Quality check evaluation
- **XAG**: XAG optimization algorithms

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
./XAGOptimizerTest
./FunctionTest
./NewMethodTest
./QCTest
./XAGTest

# Run all LLVM passes on test input
./run_passes.sh
```

### Use with LLVM

```bash
# Example: Run XAGOptimizer pass on LLVM IR
opt-16 -load-pass-plugin=build/lib/libXAGOptimizerLib.so \
       -passes="xag-optimizer" \
       input.ll -o output.bc
```

## Documentation

See [LLVM_INTEGRATION.md](LLVM_INTEGRATION.md) for comprehensive documentation on:
- Building and installation
- LLVM pass usage
- API reference
- Integration examples
- Troubleshooting

## Requirements

- LLVM 14-18 (tested with 16, 17, and 18)
- CMake 3.10+
- C++17 compatible compiler

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.