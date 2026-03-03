// QC.cpp
#include "QC.h"
#include "NewMethod.h"
#include "llvm/IR/Function.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace dagtdep;

// Implementation of the Pass run method
PreservedAnalyses QCPass::run(Function &F, FunctionAnalysisManager &AM) {
  errs() << "Running QC Pass on function: " << F.getName() << "\n";

  // Fetch the XAG (built by NewMethodAnalysis, potentially optimized by
  // XAGPass)
  auto &ctx = AM.getResult<NewMethodAnalysis>(F);

  errs() << "[QCPass] Received XAG: "
         << "PIs=" << ctx.xag.num_pis() << " POs=" << ctx.xag.num_pos()
         << " Gates=" << ctx.xag.num_gates()
         << " Optimized=" << (ctx.optimized ? "yes" : "no") << "\n";

  // Phase 4 will add caterpillar::logic_network_synthesis here.
  // For now, just confirm reception.
  errs() << "[QCPass] Quantum circuit synthesis placeholder complete.\n";

  return PreservedAnalyses::all();
}

// Implementation of legacy methods
void QC::evaluate() { errs() << "Evaluating Quantum Circuit\n"; }

void QC::registerPass() { errs() << "QC Pass registered\n"; }

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "QC", LLVM_VERSION_STRING,
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
                  if (Name == "qc") {
                    FPM.addPass(QCPass());
                    return true;
                  }
                  return false;
                });
          }};
}