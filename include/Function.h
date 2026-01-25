// Function.h

#ifndef FUNCTION_H
#define FUNCTION_H

#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// Function Pass for LLVM
class FunctionTransformPass : public llvm::PassInfoMixin<FunctionTransformPass> {
public:
    llvm::PreservedAnalyses run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
    
    static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class Function {
public:
    void execute();
    
    // LLVM API entry point
    static void registerPass();
    static const char* getPassName() { return "FunctionTransform"; }
    static const char* getPassDescription() { return "Function Transformation Pass"; }
};

} // namespace dagtdep

#endif // FUNCTION_H
