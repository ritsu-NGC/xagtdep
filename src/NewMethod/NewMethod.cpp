// NewMethod.cpp
#include "NewMethod.h"
#include "llvm/IR/Function.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace dagtdep;

// Required: define the AnalysisKey
AnalysisKey NewMethodAnalysis::Key;

// ── Core algorithm ────────────────────────────────────────────────────────
// Build a mockturtle::xag_network from an LLVM Function.
// One PI per function argument; constructs f = (a AND b) XOR c as a test
// until the real IR-to-XAG translation is implemented.
XagContext NewMethod::build(Function &F) {
  errs() << "[NewMethod] Building XAG for function: " << F.getName() << "\n";

  XagContext ctx;
  auto &xag = ctx.xag;

  std::vector<mockturtle::xag_network::signal> pis;
  for (auto &arg : F.args()) {
    (void)arg;
    pis.push_back(xag.create_pi());
  }

  if (pis.size() >= 3) {
    auto and_ab = xag.create_and(pis[0], pis[1]);
    auto xor_result = xag.create_xor(and_ab, pis[2]);
    xag.create_po(xor_result);
    errs() << "[NewMethod] Built XAG: f = (arg0 AND arg1) XOR arg2\n";
  } else if (!pis.empty()) {
    xag.create_po(pis[0]);
    errs() << "[NewMethod] Built pass-through XAG (< 3 args)\n";
  } else {
    xag.create_po(xag.get_constant(false));
    errs() << "[NewMethod] Built constant-0 XAG (no args)\n";
  }

  errs() << "[NewMethod] XAG stats: "
         << "PIs=" << xag.num_pis() << " POs=" << xag.num_pos()
         << " Gates=" << xag.num_gates() << "\n";

  return ctx;
}

// ── LLVM Analysis pass — delegates to NewMethod::build() ──────────────────
XagContext NewMethodAnalysis::run(Function &F, FunctionAnalysisManager &) {
  NewMethod nm;
  return nm.build(F);
}

// ── LLVM Transform pass — triggers the analysis ───────────────────────────
PreservedAnalyses NewMethodPass::run(Function &F, FunctionAnalysisManager &AM) {
  auto &ctx = AM.getResult<NewMethodAnalysis>(F);
  (void)ctx;
  return PreservedAnalyses::all();
}

// ── Plugin registration ───────────────────────────────────────────────────
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "NewMethod", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            PB.registerAnalysisRegistrationCallback(
                [](FunctionAnalysisManager &FAM) {
                  FAM.registerPass([&] { return NewMethodAnalysis(); });
                });
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name == "new-method") {
                    FPM.addPass(NewMethodPass());
                    return true;
                  }
                  return false;
                });
          }};
}
