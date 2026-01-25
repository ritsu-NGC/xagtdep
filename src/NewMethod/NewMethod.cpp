// NewMethod.cpp
#include "NewMethod.h"
#include "llvm/IR/Function.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"

using namespace llvm;
using namespace dagtdep;

// Implementation of the Pass run method
PreservedAnalyses NewMethodPass::run(Function &F, FunctionAnalysisManager &AM) {
    errs() << "Running NewMethod Pass on function: " << F.getName() << "\n";
    
    // Apply new method
    NewMethod method;
    method.apply();
    
    // Indicate that all analyses are preserved (this is an analysis pass)
    return PreservedAnalyses::all();
}

// Implementation of legacy methods
void NewMethod::apply() {
    // Implementation for new method application
    errs() << "Applying New Method\n";
}

void NewMethod::registerPass() {
    // Registration logic for the pass
    errs() << "NewMethod Pass registered\n";
}

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
    return {
        LLVM_PLUGIN_API_VERSION, "NewMethod", LLVM_VERSION_STRING,
        [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name == "new-method") {
                        FPM.addPass(NewMethodPass());
                        return true;
                    }
                    return false;
                }
            );
        }
    };
}
