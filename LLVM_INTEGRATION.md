# LLVM Pass Integration Guide

This document describes how each submodule in the `dagtdep` repository has been extended with LLVM Pass infrastructure.

## Overview

All five submodules have been updated to function as LLVM passes:
- **XAGOptimizer**: Analysis pass for XAG optimization
- **Function**: Function transformation pass
- **NewMethod**: New method application pass
- **QC**: Quality check evaluation pass
- **XAG**: XAG optimization pass

## Architecture

Each module follows LLVM's modern pass infrastructure:

1. **Pass Class**: Implements `llvm::PassInfoMixin<T>` with a `run()` method
2. **Plugin Registration**: Uses `llvmGetPassPluginInfo()` for dynamic loading
3. **Pipeline Integration**: Registers with PassBuilder for command-line usage
4. **Legacy API**: Maintains backward compatibility with original interfaces

## Building

### Prerequisites
- LLVM 14-18 (tested with LLVM 16, 17, and 18)
- CMake 3.10+
- C++17 compiler

### Build Instructions

```bash
# Create build directory
mkdir build && cd build

# Configure with CMake (specify LLVM version if needed)
cmake -DLLVM_DIR=/usr/lib/llvm-16/cmake ..

# Build all modules
make

# Libraries will be in build/lib/
ls build/lib/
# libXAGOptimizerLib.so
# libFunctionLib.so
# libNewMethodLib.so
# libQCLib.so
# libXAGLib.so
```

## Usage

### Command Line Usage with opt

Each pass can be loaded and run using LLVM's `opt` tool:

```bash
# Run XAGOptimizer pass (use opt, opt-16, opt-17, or opt-18 as available)
opt-16 -load-pass-plugin=build/lib/libXAGOptimizerLib.so \
       -passes="xag-optimizer" \
       -disable-output test/test_input.ll

# Run Function Transform pass
opt-16 -load-pass-plugin=build/lib/libFunctionLib.so \
       -passes="function-transform" \
       -disable-output test/test_input.ll

# Run NewMethod pass
opt-16 -load-pass-plugin=build/lib/libNewMethodLib.so \
       -passes="new-method" \
       -disable-output test/test_input.ll

# Run QC pass
opt-16 -load-pass-plugin=build/lib/libQCLib.so \
       -passes="qc" \
       -disable-output test/test_input.ll

# Run XAG pass
opt-16 -load-pass-plugin=build/lib/libXAGLib.so \
       -passes="xag" \
       -disable-output test/test_input.ll
```

### Programmatic Usage

You can also use these passes programmatically in your own LLVM-based tools:

```cpp
#include "llvm/Passes/PassBuilder.h"
#include "llvm/IR/PassManager.h"
#include "XAGOptimizer.h"

using namespace llvm;
using namespace dagtdep;

// In your analysis pipeline
FunctionPassManager FPM;
FPM.addPass(XAGOptimizerPass());
FPM.run(YourFunction, YourAnalysisManager);
```

## Pass Names and Descriptions

| Module | Pass Name | Description |
|--------|-----------|-------------|
| XAGOptimizer | `xag-optimizer` | XAG Optimizer Analysis Pass |
| Function | `function-transform` | Function Transformation Pass |
| NewMethod | `new-method` | New Method Application Pass |
| QC | `qc` | Quality Check Evaluation Pass |
| XAG | `xag` | XAG Optimization Pass |

## API Entry Points

Each module exposes the following API:

### C++ API
```cpp
namespace dagtdep {
    // Pass class for new pass manager
    class XYZPass : public llvm::PassInfoMixin<XYZPass> {
        llvm::PreservedAnalyses run(llvm::Function &F, 
                                   llvm::FunctionAnalysisManager &AM);
    };
    
    // Legacy API
    class XYZ {
        void methodName();
        static void registerPass();
        static const char* getPassName();
        static const char* getPassDescription();
    };
}
```

### Plugin API
Each module exports `llvmGetPassPluginInfo()` for dynamic loading.

## Testing

### Running the Test Suite

A comprehensive test script is provided:

```bash
# Run all passes on test input
./test/run_passes.sh
```

### Individual Tests

Each module has a dedicated test:

```bash
cd build
./XAGOptimizerTest
./FunctionTest
./NewMethodTest
./QCTest
./XAGTest
```

## LLVM Standards Compliance

All modules follow LLVM coding standards:

1. **Naming**: CamelCase for classes, methods
2. **Namespacing**: All classes in `dagtdep` namespace
3. **Documentation**: Inline comments for complex logic
4. **Error Handling**: Uses LLVM's error handling mechanisms
5. **Memory Management**: LLVM-style memory management
6. **Pass Interface**: Modern PassManager infrastructure

## Integration with LLVM Workflows

These passes integrate seamlessly with standard LLVM workflows:

1. **Compilation Pipeline**: Can be inserted into clang/opt pipelines
2. **Analysis Framework**: Uses LLVM's analysis manager
3. **Pass Dependencies**: Can declare dependencies on other passes
4. **Preserved Analyses**: Properly declares which analyses are preserved

## Example Workflow

```bash
# 1. Compile C/C++ to LLVM IR
clang-16 -emit-llvm -S example.c -o example.ll

# 2. Run optimization passes
opt-16 -load-pass-plugin=build/lib/libXAGOptimizerLib.so \
       -passes="xag-optimizer" \
       example.ll -o example_analyzed.bc

# 3. Further processing or compilation
llc-16 example_analyzed.bc -o example.s
```

## Troubleshooting

### LLVM Not Found
```bash
# Specify LLVM directory explicitly
cmake -DLLVM_DIR=/usr/lib/llvm-16/cmake ..
```

### Pass Not Loading
Ensure you're using the correct LLVM version:
```bash
opt-16 --version  # Should match your build LLVM version
```

### Undefined Symbols
Make sure all LLVM components are linked:
```cmake
llvm_map_components_to_libnames(llvm_libs core support)
```

## Contributing

When extending these passes:
1. Maintain LLVM coding standards
2. Update tests for new functionality
3. Document new API entry points
4. Ensure backward compatibility with legacy API

## License

Follows the same license as the parent dagtdep project.
