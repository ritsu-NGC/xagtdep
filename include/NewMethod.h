// NewMethod.h

#ifndef NEWMETHOD_H
#define NEWMETHOD_H

#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// NewMethod Pass for LLVM
class NewMethodPass : public llvm::PassInfoMixin<NewMethodPass> {
public:
    llvm::PreservedAnalyses run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
    
    static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class NewMethod {
public:
    void apply();
    
    // LLVM API entry point
    static void registerPass();
    static const char* getPassName() { return "NewMethod"; }
    static const char* getPassDescription() { return "New Method Application Pass"; }
};

} // namespace dagtdep

#endif // NEWMETHOD_H
