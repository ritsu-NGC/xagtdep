// QC.h

#ifndef QC_H
#define QC_H

#include "XagContext.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// QC Pass for LLVM — synthesizes quantum circuit from optimized xag_network
class QCPass : public llvm::PassInfoMixin<QCPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &F,
                              llvm::FunctionAnalysisManager &AM);

  static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class QC {
public:
  void evaluate();

  // LLVM API entry point
  static void registerPass();
  static const char *getPassName() { return "QC"; }
  static const char *getPassDescription() {
    return "Quantum Circuit Synthesis Pass";
  }
};

} // namespace dagtdep

#endif // QC_H