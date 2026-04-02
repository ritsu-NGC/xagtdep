// QC.cpp
#include "QC.h"
#include "NewMethod.h"
#include "QCGateList.h"
#include "XAGToDecomposed.h"
#include "XAGToGateList.h"
#include "llvm/IR/Function.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

#ifdef XAGTDEP_ENABLE_PYTHON
#include "PythonBridge.h"
#endif

using namespace llvm;
using namespace xagtdep;

// ── Core algorithm ────────────────────────────────────────────────────────
// Consume the optimized XagContext and synthesize a quantum circuit.
void QC::evaluate(const XagContext &ctx, SynthesisMode mode) {
  errs() << "[QC] Received XAG: "
         << "PIs=" << ctx.xag.num_pis() << " POs=" << ctx.xag.num_pos()
         << " Gates=" << ctx.xag.num_gates()
         << " Optimized=" << (ctx.optimized ? "yes" : "no")
         << " Steps=" << ctx.steps.size() << "\n";
  errs() << "[QC] Synthesis mode: "
         << (mode == SynthesisMode::Abstract ? "abstract" : "decomposed")
         << "\n";

  if (!ctx.optimized) {
    errs() << "[QC] WARNING: XAG was not optimized. Skipping synthesis.\n";
    return;
  }

  // Convert XAG to gate list via depth-first traversal.
  QCGateList gateList;
  if (mode == SynthesisMode::Abstract)
    gateList = XAGToGateList::translate(ctx);
  else
    gateList = XAGToDecomposed::translate(ctx);
  std::string json = gateList.toJSON();
  errs() << "[QC] Gate list: " << json << "\n";

#ifdef XAGTDEP_ENABLE_PYTHON
  std::string qasm = PythonBridge::callQiskitSynthesis(json);
  if (!qasm.empty()) {
    qasm_output_ = qasm;
    errs() << "[QC] QASM output:\n" << qasm_output_ << "\n";
  } else {
    errs() << "[QC] WARNING: Qiskit synthesis failed, storing raw gate list.\n";
    qasm_output_ = json;
  }
#else
  qasm_output_ = json;
  errs() << "[QC] Python disabled, raw gate list stored as output.\n";
#endif
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
