// Function.cpp
#include "Function.h"
#include "llvm/IR/Function.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"

using namespace llvm;
using namespace dagtdep;

// Implementation of the Pass run method
PreservedAnalyses FunctionTransformPass::run(llvm::Function &F, FunctionAnalysisManager &AM) {
    errs() << "Running Function Transform Pass on function: " << F.getName() << "\n";
    
    // Perform function transformation
    dagtdep::Function transformer;
    transformer.execute();
    
    // Indicate that all analyses are preserved (this is an analysis pass)
    return PreservedAnalyses::all();
}

// Implementation of legacy methods
void dagtdep::Function::execute() {
    // Implementation for function transformation
    errs() << "Executing Function transformation\n";
}

void dagtdep::Function::registerPass() {
    // Registration logic for the pass
    errs() << "Function Transform Pass registered\n";
}

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
    return {
        LLVM_PLUGIN_API_VERSION, "FunctionTransform", LLVM_VERSION_STRING,
        [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                    if (Name == "function-transform") {
                        FPM.addPass(FunctionTransformPass());
                        return true;
                    }
                    return false;
                }
            );
        }
    };
}
