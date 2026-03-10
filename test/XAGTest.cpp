// XAGTest.cpp — tests XAG::optimize() directly using a hand-built XAG
#include "XAG.h"
#include "NewMethod.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Type.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace xagtdep;

int main() {
  // Build a minimal LLVM function with 3 args so NewMethod produces a real XAG
  LLVMContext ctx;
  Module mod("test", ctx);
  auto *i32 = Type::getInt32Ty(ctx);
  auto *funcTy = FunctionType::get(i32, {i32, i32, i32}, false);
  auto *F = Function::Create(funcTy, Function::ExternalLinkage, "test_fn", mod);
  auto *bb = BasicBlock::Create(ctx, "entry", F);
  IRBuilder<> builder(bb);
  builder.CreateRet(ConstantInt::get(i32, 0));

  // Step 1: build XAG via NewMethod
  NewMethod nm;
  XagContext xagCtx = nm.build(*F);

  errs() << "[XAGTest] Before optimization: Gates=" << xagCtx.xag.num_gates()
         << " Optimized=" << (xagCtx.optimized ? "yes" : "no") << "\n";

  // Step 2: optimize via XAG
  XAG optimizer;
  optimizer.optimize(xagCtx);

  errs() << "[XAGTest] After optimization: "
         << "Steps=" << xagCtx.steps.size()
         << " Optimized=" << (xagCtx.optimized ? "yes" : "no") << "\n";

  bool pass = xagCtx.optimized && !xagCtx.steps.empty();
  errs() << "[XAGTest] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass ? 0 : 1;
}
