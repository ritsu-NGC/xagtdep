// QC.h

#ifndef QC_H
#define QC_H

#include "XagContext.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

/// Core algorithm class: consumes the XagContext produced by XAGPass
/// and synthesizes a quantum circuit.
/// This is what QCTest exercises directly.
class QC {
public:
  /// Print stats from ctx and invoke logic_network_synthesis.
  void evaluate(const XagContext &ctx);

  static const char *getPassName() { return "QC"; }
  static const char *getPassDescription() {
    return "Quantum Circuit Synthesis Pass";
  }
};

/// LLVM Transform pass wrapper — delegates to QC::evaluate().
class QCPass : public llvm::PassInfoMixin<QCPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &F,
                              llvm::FunctionAnalysisManager &AM);
  static bool isRequired() { return true; }
};

} // namespace dagtdep

#endif // QC_H
