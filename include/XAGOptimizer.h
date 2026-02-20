// XAGOptimizer.h

#ifndef XAGOPTIMIZER_H
#define XAGOPTIMIZER_H

#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// XAGOptimizer Pass for LLVM
class XAGOptimizerPass : public llvm::PassInfoMixin<XAGOptimizerPass> {
public:
    llvm::PreservedAnalyses run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
    
    static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class XAGOptimizer {
public:
    void analyze();
    
    // LLVM API entry point
    static void registerPass();
    static const char* getPassName() { return "XAGOptimizer"; }
    static const char* getPassDescription() { return "XAG Optimizer Analysis Pass"; }
};

} // namespace dagtdep

#endif // XAGOPTIMIZER_H
