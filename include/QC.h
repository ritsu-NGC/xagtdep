// QC.h

#ifndef QC_H
#define QC_H

#include "XagContext.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"
#include <string>

namespace xagtdep {

/// Core algorithm class: consumes the XagContext produced by XAGPass
/// and synthesizes a quantum circuit.
/// This is what QCTest exercises directly.
class QC {
public:
  /// Traverse ctx.xag, build a gate list, and (optionally) produce QASM.
  void evaluate(const XagContext &ctx);

  /// Access the output (QASM string when Python enabled, JSON gate list
  /// otherwise).
  const std::string &getQASM() const { return qasm_output_; }

  static const char *getPassName() { return "QC"; }
  static const char *getPassDescription() {
    return "Quantum Circuit Synthesis Pass";
  }

private:
  std::string qasm_output_;
};

/// LLVM Transform pass wrapper — delegates to QC::evaluate().
class QCPass : public llvm::PassInfoMixin<QCPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &F,
                              llvm::FunctionAnalysisManager &AM);
  static bool isRequired() { return true; }
};

} // namespace xagtdep

#endif // QC_H
