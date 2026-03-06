// NewMethod.h

#ifndef NEWMETHOD_H
#define NEWMETHOD_H

#include "XagContext.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

/// Core algorithm class: builds a mockturtle::xag_network from an LLVM
/// Function. This is what the test (NewMethodTest) exercises directly.
class NewMethod {
public:
  /// Build an XAG from the given LLVM function's arguments.
  /// Returns an XagContext containing the constructed network.
  XagContext build(llvm::Function &F);

  static const char *getPassName() { return "NewMethod"; }
  static const char *getPassDescription() {
    return "New Method Application Pass";
  }
};

/// LLVM Analysis pass wrapper — delegates to NewMethod::build().
/// Downstream passes fetch the result via AM.getResult<NewMethodAnalysis>(F).
class NewMethodAnalysis : public llvm::AnalysisInfoMixin<NewMethodAnalysis> {
  friend llvm::AnalysisInfoMixin<NewMethodAnalysis>;
  static llvm::AnalysisKey Key;

public:
  using Result = XagContext;
  Result run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
};

/// LLVM Transform pass wrapper — triggers the analysis.
class NewMethodPass : public llvm::PassInfoMixin<NewMethodPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &F,
                              llvm::FunctionAnalysisManager &AM);
  static bool isRequired() { return true; }
};

} // namespace dagtdep

#endif // NEWMETHOD_H
