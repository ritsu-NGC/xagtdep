// NewMethodTest.cpp — tests NewMethod::build() directly using LLVM IR
#include "NewMethod.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Type.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace dagtdep;

int main() {
  // Build a minimal LLVM function: int f(int a, int b, int c)
  LLVMContext ctx;
  Module mod("test", ctx);
  auto *i32 = Type::getInt32Ty(ctx);
  auto *funcTy = FunctionType::get(i32, {i32, i32, i32}, false);
  auto *F = Function::Create(funcTy, Function::ExternalLinkage, "test_fn", mod);
  auto *bb = BasicBlock::Create(ctx, "entry", F);
  IRBuilder<> builder(bb);
  builder.CreateRet(ConstantInt::get(i32, 0));

  // Exercise the real algorithm
  NewMethod nm;
  XagContext xagCtx = nm.build(*F);

  errs() << "[NewMethodTest] Result: "
         << "PIs=" << xagCtx.xag.num_pis() << " POs=" << xagCtx.xag.num_pos()
         << " Gates=" << xagCtx.xag.num_gates() << "\n";

  // 3 PIs (a, b, c), 1 PO, 2 gates (1 AND + 1 XOR)
  bool pass = xagCtx.xag.num_pis() == 3 && xagCtx.xag.num_pos() == 1 &&
              xagCtx.xag.num_gates() == 2;
  errs() << "[NewMethodTest] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass ? 0 : 1;
}
