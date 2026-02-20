# dagtdep

LLVM-integrated analysis and optimization modules for DAG-based transformations.

## Overview

This project contains five LLVM-integrated modules for the synthesis of quantum oracles
with a focus on T-count and T-depth optimization

- **DavioDecomposition**: Davio decomposition analysis
- **Function**: Function transformation operations
- **NewMethod**: New method to generate input XAGs to optimization
- **QC**: Quantum Circuit generation from XAG
- **XAG**: XAG optimization algorithms

All modules are implemented as dynamically loadable LLVM passes that can be used with LLVM's `opt` tool or integrated into custom LLVM-based workflows.

## XAG

XAG implements the algorithm in Meuli et al [2022](https://doi.org/10.1038/s41534-021-00514-y) to optimize
XAG for AND depth, which optimizes for T-depth. These are implemented in the [caterpillar library](https://github.com/gmeuli/caterpillar)

Inputs and Outputs from this module will be in mockturtle::xag_network format that caterpillar uses

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.