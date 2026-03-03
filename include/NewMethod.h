// NewMethod.h

#ifndef NEWMETHOD_H
#define NEWMETHOD_H

#include "XagContext.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// Analysis pass that builds a mockturtle::xag_network from LLVM IR.
// Downstream passes (XAG, QC) consume this via
// AM.getResult<NewMethodAnalysis>(F).
class NewMethodAnalysis : public llvm::AnalysisInfoMixin<NewMethodAnalysis> {
  friend llvm::AnalysisInfoMixin<NewMethodAnalysis>;
  static llvm::AnalysisKey Key;

public:
  using Result = XagContext;

  Result run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
};

// Transformation pass wrapper — triggers the analysis and prints debug info
class NewMethodPass : public llvm::PassInfoMixin<NewMethodPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &F,
                              llvm::FunctionAnalysisManager &AM);

  static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class NewMethod {
public:
  void apply();

  // LLVM API entry point
  static void registerPass();
  static const char *getPassName() { return "NewMethod"; }
  static const char *getPassDescription() {
    return "New Method Application Pass";
  }
};

} // namespace dagtdep

#endif // NEWMETHOD_H
