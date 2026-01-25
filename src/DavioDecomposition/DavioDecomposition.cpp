// DavioDecomposition.cpp
#include "DavioDecomposition.h"
#include "llvm/IR/Function.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"

using namespace llvm;
using namespace dagtdep;

// Implementation of the Pass run method
PreservedAnalyses DavioDecompositionPass::run(Function &F, FunctionAnalysisManager &AM) {
    errs() << "Running DavioDecomposition Pass on function: " << F.getName() << "\n";
    
    // Perform Davio decomposition analysis
    DavioDecomposition analyzer;
    analyzer.analyze();
    
    // Indicate that all analyses are preserved (this is an analysis pass)
    return PreservedAnalyses::all();
}

// Implementation of legacy methods
void DavioDecomposition::analyze() {
    // Implementation for Davio decomposition
    errs() << "Performing Davio Decomposition analysis\n";
}

void DavioDecomposition::registerPass() {
    // Registration logic for the pass
    errs() << "DavioDecomposition Pass registered\n";
}

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
    return {
        LLVM_PLUGIN_API_VERSION, "DavioDecomposition", LLVM_VERSION_STRING,
        [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name == "davio-decomposition") {
                        FPM.addPass(DavioDecompositionPass());
                        return true;
                    }
                    return false;
                }
            );
        }
    };
}
