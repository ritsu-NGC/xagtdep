// XAG.cpp
#include "XAG.h"
#include "NewMethod.h"
#include "llvm/IR/Function.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"
#include <caterpillar/synthesis/strategies/xag_mapping_strategy.hpp>

using namespace llvm;
using namespace dagtdep;

// ── Core algorithm ────────────────────────────────────────────────────────
// Run caterpillar's T-depth minimization strategy on ctx.xag.
// Populates ctx.steps and sets ctx.optimized = true on success.
void XAG::optimize(XagContext &ctx) {
  errs() << "[XAG] BEFORE: PIs=" << ctx.xag.num_pis()
         << " POs=" << ctx.xag.num_pos() << " Gates=" << ctx.xag.num_gates()
         << "\n";

  // Algorithm 2 from Meuli et al. (2022) — T-depth minimization.
  // Processes the XAG level-by-level (ASAP), copies shared AND inputs to
  // fresh ancilla qubits so all ANDs in a level execute in one T-stage.
  // T-depth of resulting circuit = AND-depth of XAG.
  caterpillar::xag_low_depth_mapping_strategy strategy;
  bool success = strategy.compute_steps(ctx.xag);

  if (success) {
    strategy.foreach_step(
        [&](auto node, auto action) { ctx.steps.emplace_back(node, action); });
    ctx.optimized = true;
    errs() << "[XAG] Optimization complete. Steps=" << ctx.steps.size() << "\n";
  } else {
    errs() << "[XAG] WARNING: compute_steps() returned false.\n";
  }
}

// ── LLVM Transform pass — delegates to XAG::optimize() ───────────────────
PreservedAnalyses XAGPass::run(Function &F, FunctionAnalysisManager &AM) {
  errs() << "Running XAG Pass on function: " << F.getName() << "\n";
  auto &ctx = AM.getResult<NewMethodAnalysis>(F);
  XAG optimizer;
  optimizer.optimize(ctx);
  return PreservedAnalyses::all();
}

// ── Plugin registration ───────────────────────────────────────────────────
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "XAG", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
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
