// XAG.h

#ifndef XAG_H
#define XAG_H

#include "XagContext.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

/// Core algorithm class: runs caterpillar's T-depth optimization strategy
/// on the xag_network inside XagContext.
class XAG {
public:
  void optimize(XagContext &ctx);

  static const char *getPassName() { return "XAG"; }
  static const char *getPassDescription() { return "XAG Optimization Pass"; }
};

/// LLVM Transform pass wrapper — delegates to XAG::optimize().
class XAGPass : public llvm::PassInfoMixin<XAGPass> {
public:
  llvm::PreservedAnalyses run(llvm::Function &F,
                              llvm::FunctionAnalysisManager &AM);
  static bool isRequired() { return true; }
};

} // namespace dagtdep

#endif // XAG_H
