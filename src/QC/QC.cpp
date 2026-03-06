// QC.cpp
#include "QC.h"
#include "NewMethod.h"
#include "llvm/IR/Function.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace dagtdep;

// ── Core algorithm ────────────────────────────────────────────────────────
// Consume the optimized XagContext and synthesize a quantum circuit.
// will add caterpillar::logic_network_synthesis here.
void QC::evaluate(const XagContext &ctx) {
  errs() << "[QC] Received XAG: "
         << "PIs=" << ctx.xag.num_pis() << " POs=" << ctx.xag.num_pos()
         << " Gates=" << ctx.xag.num_gates()
         << " Optimized=" << (ctx.optimized ? "yes" : "no")
         << " Steps=" << ctx.steps.size() << "\n";

  if (!ctx.optimized) {
    errs() << "[QC] WARNING: XAG was not optimized. Skipping synthesis.\n";
    return;
  }

  // placeholder — caterpillar::logic_network_synthesis goes here.
  errs() << "[QC] Quantum circuit synthesis placeholder. "
         << "Will iterate " << ctx.steps.size()
         << " compute/uncompute steps in Phase 4.\n";
}

// ── LLVM Transform pass — delegates to QC::evaluate() ────────────────────
PreservedAnalyses QCPass::run(Function &F, FunctionAnalysisManager &AM) {
  errs() << "Running QC Pass on function: " << F.getName() << "\n";
  auto &ctx = AM.getResult<NewMethodAnalysis>(F);
  QC synthesizer;
  synthesizer.evaluate(ctx);
  return PreservedAnalyses::all();
}

// ── Plugin registration ───────────────────────────────────────────────────
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "QC", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
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
