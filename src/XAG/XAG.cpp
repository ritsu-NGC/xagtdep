// XAG.cpp
#include "XAG.h"
#include "NewMethod.h"
#include "llvm/IR/Function.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace dagtdep;

// Implementation of the Pass run method
PreservedAnalyses XAGPass::run(Function &F, FunctionAnalysisManager &AM) {
  errs() << "Running XAG Pass on function: " << F.getName() << "\n";

  // Fetch the XAG built by NewMethodAnalysis
  auto &ctx = AM.getResult<NewMethodAnalysis>(F);

  errs() << "[XAGPass] Received XAG: "
         << "PIs=" << ctx.xag.num_pis() << " POs=" << ctx.xag.num_pos()
         << " Gates=" << ctx.xag.num_gates()
         << " Optimized=" << (ctx.optimized ? "yes" : "no") << "\n";

  // Phase 3 will add caterpillar optimization here.
  // For now, just pass it through unchanged.

  return PreservedAnalyses::all();
}

// Implementation of legacy methods
void XAG::optimize() { errs() << "Optimizing with XAG\n"; }

void XAG::registerPass() { errs() << "XAG Pass registered\n"; }

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "XAG", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            // Register NewMethodAnalysis so AM.getResult works from this plugin
            // too
            PB.registerAnalysisRegistrationCallback(
                [](FunctionAnalysisManager &FAM) {
                  FAM.registerPass([&] { return NewMethodAnalysis(); });
                });

            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name == "xag") {
                    FPM.addPass(XAGPass());
                    return true;
                  }
                  return false;
                });
          }};
}