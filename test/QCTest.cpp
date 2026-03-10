// QCTest.cpp — tests QC::evaluate() using an optimized XagContext
#include "QC.h"
#include "NewMethod.h"
#include "XAG.h"
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
  // Build a minimal LLVM function with 3 args
  LLVMContext ctx;
  Module mod("test", ctx);
  auto *i32 = Type::getInt32Ty(ctx);
  auto *funcTy = FunctionType::get(i32, {i32, i32, i32}, false);
  auto *F = Function::Create(funcTy, Function::ExternalLinkage, "test_fn", mod);
  auto *bb = BasicBlock::Create(ctx, "entry", F);
  IRBuilder<> builder(bb);
  builder.CreateRet(ConstantInt::get(i32, 0));

  // Full pipeline: NewMethod → XAG → QC
  NewMethod nm;
  XagContext xagCtx = nm.build(*F);

  XAG optimizer;
  optimizer.optimize(xagCtx);

  QC synthesizer;
  synthesizer.evaluate(xagCtx);

  bool pass = xagCtx.optimized && !xagCtx.steps.empty();
  errs() << "[QCTest] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass ? 0 : 1;
}
