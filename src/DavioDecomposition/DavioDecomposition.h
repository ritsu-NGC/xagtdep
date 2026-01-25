// DavioDecomposition.h

#ifndef DAVIODECOMPOSITION_H
#define DAVIODECOMPOSITION_H

#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace dagtdep {

// DavioDecomposition Pass for LLVM
class DavioDecompositionPass : public llvm::PassInfoMixin<DavioDecompositionPass> {
public:
    llvm::PreservedAnalyses run(llvm::Function &F, llvm::FunctionAnalysisManager &AM);
    
    static bool isRequired() { return true; }
};

// Legacy class for backward compatibility
class DavioDecomposition {
public:
    void analyze();
    
    // LLVM API entry point
    static void registerPass();
    static const char* getPassName() { return "DavioDecomposition"; }
    static const char* getPassDescription() { return "Davio Decomposition Analysis Pass"; }
};

} // namespace dagtdep

#endif // DAVIODECOMPOSITION_H
