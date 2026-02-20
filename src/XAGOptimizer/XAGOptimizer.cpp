// XAGOptimizer.cpp
#include "XAGOptimizer.h"
#include "llvm/IR/Function.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"

using namespace llvm;
using namespace dagtdep;

// Implementation of the Pass run method
PreservedAnalyses XAGOptimizerPass::run(Function &F, FunctionAnalysisManager &AM) {
    errs() << "Running XAGOptimizer Pass on function: " << F.getName() << "\n";
    
    // Perform XAG optimization analysis
    XAGOptimizer analyzer;
    analyzer.analyze();
    
    // Indicate that all analyses are preserved (this is an analysis pass)
    return PreservedAnalyses::all();
}

// Implementation of legacy methods
void XAGOptimizer::analyze() {
    // Implementation for XAG optimization
    errs() << "Performing XAG Optimizer analysis\n";
}

void XAGOptimizer::registerPass() {
    // Registration logic for the pass
    errs() << "XAGOptimizer Pass registered\n";
}

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
    return {
        LLVM_PLUGIN_API_VERSION, "XAGOptimizer", LLVM_VERSION_STRING,
        [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name == "xag-optimizer") {
                        FPM.addPass(XAGOptimizerPass());
                        return true;
                    }
                    return false;
                }
            );
        }
    };
}
