// XAG.h

#ifndef XAG_H
#define XAG_H

#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// XAG Pass for LLVM
class XAGPass : public llvm::PassInfoMixin<XAGPass> {
public:
    llvm::PreservedAnalyses run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
    
    static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class XAG {
public:
    void optimize();
    
    // LLVM API entry point
    static void registerPass();
    static const char* getPassName() { return "XAG"; }
    static const char* getPassDescription() { return "XAG Optimization Pass"; }
};

} // namespace dagtdep

#endif // XAG_H
