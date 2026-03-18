// QCTest.cpp — tests QC::evaluate() using an optimized XagContext
#include "QC.h"
#include "NewMethod.h"
#include "XAG.h"
#include "XAGToGateList.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Type.h"
#include "llvm/Support/raw_ostream.h"
#include <mockturtle/networks/xag.hpp>

using namespace llvm;
using namespace xagtdep;

/// Test 1: Full pipeline test (existing) — exercises AND Case 1.
static bool testPipeline() {
  LLVMContext ctx;
  Module mod("test", ctx);
  auto *i32 = Type::getInt32Ty(ctx);
  auto *funcTy = FunctionType::get(i32, {i32, i32, i32}, false);
  auto *F = Function::Create(funcTy, Function::ExternalLinkage, "test_fn", mod);
  auto *bb = BasicBlock::Create(ctx, "entry", F);
  IRBuilder<> builder(bb);
  builder.CreateRet(ConstantInt::get(i32, 0));

  NewMethod nm;
  XagContext xagCtx = nm.build(*F);

  XAG optimizer;
  optimizer.optimize(xagCtx);

  QC synthesizer;
  synthesizer.evaluate(xagCtx);

  bool pass = xagCtx.optimized && !xagCtx.steps.empty() &&
              !synthesizer.getQASM().empty();
  errs() << "[QCTest::Pipeline] Output: " << synthesizer.getQASM() << "\n";
  errs() << "[QCTest::Pipeline] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// Test 2: AND Case 2 — a AND (b XOR c) exercises compute/uncompute.
static bool testAndCase2() {
  // Build XAG directly: a AND (b XOR c)
  mockturtle::xag_network xag;
  auto a = xag.create_pi();
  auto b = xag.create_pi();
  auto c = xag.create_pi();
  auto bxc = xag.create_xor(b, c);
  auto result = xag.create_and(a, bxc);
  xag.create_po(result);

  // Wrap in XagContext (no LLVM pipeline needed).
  XagContext xagCtx;
  xagCtx.xag = xag;
  xagCtx.optimized = true;
  xagCtx.steps = {}; // No steps needed — XAGToGateList does its own traversal.

  // Translate to gate list directly.
  QCGateList gateList = XAGToGateList::translate(xagCtx);

  errs() << "[QCTest::AndCase2] num_qubits=" << gateList.num_qubits
         << " num_pis=" << gateList.num_pis
         << " num_ancillas=" << gateList.num_ancillas
         << " gates=" << gateList.gates.size() << "\n";

  // Print each gate for debugging.
  for (size_t i = 0; i < gateList.gates.size(); ++i) {
    const auto &g = gateList.gates[i];
    const char *name = g.type == GateType::X        ? "X"
                       : g.type == GateType::CNOT    ? "CNOT"
                       : g.type == GateType::Toffoli ? "Toffoli"
                                                     : "?";
    errs() << "  [" << i << "] " << name << "(";
    for (size_t j = 0; j < g.controls.size(); ++j) {
      if (j > 0)
        errs() << ",";
      errs() << "q" << g.controls[j];
    }
    errs() << " -> q" << g.target << ")\n";
  }

  // Expected: 3 PIs, 2 ancillas (XOR output + AND output) = 5 qubits.
  // Expected gate sequence (5 gates):
  //   CNOT(q_b, q_xor)      — compute XOR
  //   CNOT(q_c, q_xor)
  //   Toffoli(q_a, q_xor, q_and) — AND
  //   CNOT(q_c, q_xor)      — uncompute XOR
  //   CNOT(q_b, q_xor)
  bool pass = true;

  if (gateList.num_qubits != 5) {
    errs() << "[QCTest::AndCase2] FAIL: expected 5 qubits, got "
           << gateList.num_qubits << "\n";
    pass = false;
  }
  if (gateList.num_pis != 3) {
    errs() << "[QCTest::AndCase2] FAIL: expected 3 PIs, got "
           << gateList.num_pis << "\n";
    pass = false;
  }
  if (gateList.gates.size() != 5) {
    errs() << "[QCTest::AndCase2] FAIL: expected 5 gates, got "
           << gateList.gates.size() << "\n";
    pass = false;
  }

  // Verify compute/uncompute symmetry: gate[0] matches gate[4], gate[1]
  // matches gate[3] (reversed).
  if (gateList.gates.size() == 5) {
    auto matchGate = [&](size_t a, size_t b) {
      const auto &ga = gateList.gates[a];
      const auto &gb = gateList.gates[b];
      return ga.type == gb.type && ga.controls == gb.controls &&
             ga.target == gb.target;
    };
    if (!matchGate(0, 4) || !matchGate(1, 3)) {
      errs() << "[QCTest::AndCase2] FAIL: compute/uncompute not symmetric\n";
      pass = false;
    }
    if (gateList.gates[2].type != GateType::Toffoli) {
      errs() << "[QCTest::AndCase2] FAIL: middle gate should be Toffoli\n";
      pass = false;
    }
  }

  errs() << "[QCTest::AndCase2] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

int main() {
  bool allPass = true;
  allPass &= testPipeline();
  allPass &= testAndCase2();
  return allPass ? 0 : 1;
}
