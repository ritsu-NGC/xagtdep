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

// Analysis pass implementation — builds a mockturtle::xag_network from the IR
XagContext NewMethodAnalysis::run(Function &F, FunctionAnalysisManager &AM) {
  errs() << "[NewMethodAnalysis] Building XAG for function: " << F.getName()
         << "\n";

  XagContext ctx;
  auto &xag = ctx.xag;

  // Create primary inputs — one per function argument
  std::vector<mockturtle::xag_network::signal> pis;
  for (auto &arg : F.args()) {
    (void)arg;
    pis.push_back(xag.create_pi());
  }

  // Build a trivial test XAG: f = (a AND b) XOR c
  // If the function has >= 3 args, use the first three; otherwise just wire
  // through.
  if (pis.size() >= 3) {
    auto and_ab = xag.create_and(pis[0], pis[1]);
    auto xor_result = xag.create_xor(and_ab, pis[2]);
    xag.create_po(xor_result);
    errs()
        << "[NewMethodAnalysis] Built test XAG: f = (arg0 AND arg1) XOR arg2\n";
  } else if (!pis.empty()) {
    // Just pass the first input through
    xag.create_po(pis[0]);
    errs() << "[NewMethodAnalysis] Built trivial pass-through XAG\n";
  } else {
    // No arguments — create a constant output
    xag.create_po(xag.get_constant(false));
    errs() << "[NewMethodAnalysis] Built constant-0 XAG (no function args)\n";
  }

  errs() << "[NewMethodAnalysis] XAG stats: "
         << "PIs=" << xag.num_pis() << " POs=" << xag.num_pos()
         << " Gates=" << xag.num_gates() << "\n";

  return ctx;
}

// Transformation pass wrapper — triggers the analysis
PreservedAnalyses NewMethodPass::run(Function &F, FunctionAnalysisManager &AM) {
  errs() << "Running NewMethod Pass on function: " << F.getName() << "\n";

  // Request the analysis (this triggers NewMethodAnalysis::run if not cached)
  auto &ctx = AM.getResult<NewMethodAnalysis>(F);
  (void)ctx;

  return PreservedAnalyses::all();
}

// Implementation of legacy methods
void NewMethod::apply() { errs() << "Applying New Method\n"; }

void NewMethod::registerPass() { errs() << "NewMethod Pass registered\n"; }

// Plugin registration for new pass manager
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "NewMethod", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            // Register the analysis so AM.getResult<NewMethodAnalysis> works
            PB.registerAnalysisRegistrationCallback(
                [](FunctionAnalysisManager &FAM) {
                  FAM.registerPass([&] { return NewMethodAnalysis(); });
                });

            // Register the pipeline pass name "new-method"
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