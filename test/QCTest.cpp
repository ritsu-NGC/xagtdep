// QCTest.cpp — tests QC::evaluate() using an optimized XagContext
#include "QC.h"
#include "NewMethod.h"
#include "XAG.h"
#include "ExistingMethod.h"
#include "ProposedMethod.h"
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

/// Helper: return gate type name string.
static const char *gateTypeName(GateType t) {
  switch (t) {
  case GateType::X:       return "X";
  case GateType::CNOT:    return "CNOT";
  case GateType::Toffoli: return "Toffoli";
  case GateType::H:       return "H";
  case GateType::T:       return "T";
  case GateType::Tdg:     return "Tdg";
  }
  return "?";
}

/// Helper: print gate list for debugging.
static void printGateList(const char *label, const QCGateList &gl) {
  errs() << "[" << label << "] num_qubits=" << gl.num_qubits
         << " num_pis=" << gl.num_pis
         << " num_ancillas=" << gl.num_ancillas
         << " gates=" << gl.gates.size() << "\n";
  for (size_t i = 0; i < gl.gates.size(); ++i) {
    const auto &g = gl.gates[i];
    errs() << "  [" << i << "] " << gateTypeName(g.type) << "(";
    for (size_t j = 0; j < g.controls.size(); ++j) {
      if (j > 0) errs() << ",";
      errs() << "q" << g.controls[j];
    }
    errs() << " -> q" << g.target << ")\n";
  }
}

/// Helper: check that a gate list contains only decomposed gates (H,T,Tdg,CNOT,X).
static bool hasOnlyDecomposedGates(const QCGateList &gl) {
  for (const auto &g : gl.gates) {
    if (g.type != GateType::H && g.type != GateType::T &&
        g.type != GateType::Tdg && g.type != GateType::CNOT &&
        g.type != GateType::X) {
      return false;
    }
  }
  return true;
}

/// Test 3: Existing Method on (a AND b) XOR c.
static bool testExistingMethodPipeline() {
  // Build XAG: (a AND b) XOR c
  mockturtle::xag_network xag;
  auto a = xag.create_pi();
  auto b = xag.create_pi();
  auto c = xag.create_pi();
  auto ab = xag.create_and(a, b);
  auto result = xag.create_xor(ab, c);
  xag.create_po(result);

  XagContext xagCtx;
  xagCtx.xag = xag;
  xagCtx.optimized = true;

  QCGateList gateList = ExistingMethod::translate(xagCtx);
  printGateList("ExistingMethod::Pipeline", gateList);

  bool pass = true;

  if (gateList.gates.empty()) {
    errs() << "[ExistingMethod::Pipeline] FAIL: gate list is empty\n";
    pass = false;
  }

  if (!hasOnlyDecomposedGates(gateList)) {
    errs() << "[ExistingMethod::Pipeline] FAIL: contains non-decomposed gates\n";
    pass = false;
  }

  if (gateList.num_pis != 3) {
    errs() << "[ExistingMethod::Pipeline] FAIL: expected 3 PIs, got "
           << gateList.num_pis << "\n";
    pass = false;
  }

  errs() << "[ExistingMethod::Pipeline] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// Test 4: Proposed Method on (a AND b) XOR c.
static bool testProposedMethodPipeline() {
  mockturtle::xag_network xag;
  auto a = xag.create_pi();
  auto b = xag.create_pi();
  auto c = xag.create_pi();
  auto ab = xag.create_and(a, b);
  auto result = xag.create_xor(ab, c);
  xag.create_po(result);

  XagContext xagCtx;
  xagCtx.xag = xag;
  xagCtx.optimized = true;

  QCGateList gateList = ProposedMethod::translate(xagCtx);
  printGateList("ProposedMethod::Pipeline", gateList);

  bool pass = true;

  if (gateList.gates.empty()) {
    errs() << "[ProposedMethod::Pipeline] FAIL: gate list is empty\n";
    pass = false;
  }

  if (!hasOnlyDecomposedGates(gateList)) {
    errs() << "[ProposedMethod::Pipeline] FAIL: contains non-decomposed gates\n";
    pass = false;
  }

  if (gateList.num_pis != 3) {
    errs() << "[ProposedMethod::Pipeline] FAIL: expected 3 PIs, got "
           << gateList.num_pis << "\n";
    pass = false;
  }

  errs() << "[ProposedMethod::Pipeline] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// Test 5: Existing Method on a AND (b XOR c) — exercises "one PI" (Fig 11).
static bool testExistingMethodAndOnePI() {
  mockturtle::xag_network xag;
  auto a = xag.create_pi();
  auto b = xag.create_pi();
  auto c = xag.create_pi();
  auto bxc = xag.create_xor(b, c);
  auto result = xag.create_and(a, bxc);
  xag.create_po(result);

  XagContext xagCtx;
  xagCtx.xag = xag;
  xagCtx.optimized = true;

  QCGateList gateList = ExistingMethod::translate(xagCtx);
  printGateList("ExistingMethod::AndOnePI", gateList);

  bool pass = true;

  if (gateList.gates.empty()) {
    errs() << "[ExistingMethod::AndOnePI] FAIL: gate list is empty\n";
    pass = false;
  }

  if (!hasOnlyDecomposedGates(gateList)) {
    errs() << "[ExistingMethod::AndOnePI] FAIL: contains non-decomposed gates\n";
    pass = false;
  }

  errs() << "[ExistingMethod::AndOnePI] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// Test 6: Proposed Method on a AND (b XOR c) — exercises "one PI" (Fig 12).
static bool testProposedMethodAndOnePI() {
  mockturtle::xag_network xag;
  auto a = xag.create_pi();
  auto b = xag.create_pi();
  auto c = xag.create_pi();
  auto bxc = xag.create_xor(b, c);
  auto result = xag.create_and(a, bxc);
  xag.create_po(result);

  XagContext xagCtx;
  xagCtx.xag = xag;
  xagCtx.optimized = true;

  QCGateList gateList = ProposedMethod::translate(xagCtx);
  printGateList("ProposedMethod::AndOnePI", gateList);

  bool pass = true;

  if (gateList.gates.empty()) {
    errs() << "[ProposedMethod::AndOnePI] FAIL: gate list is empty\n";
    pass = false;
  }

  if (!hasOnlyDecomposedGates(gateList)) {
    errs() << "[ProposedMethod::AndOnePI] FAIL: contains non-decomposed gates\n";
    pass = false;
  }

  errs() << "[ProposedMethod::AndOnePI] " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// Helper: count gates by type and print a summary line.
struct GateCounts {
  uint32_t t = 0, tdg = 0, cnot = 0, h = 0, x = 0, toffoli = 0;
  uint32_t total = 0;
  uint32_t tCount() const { return t + tdg; }
};

static GateCounts countGates(const QCGateList &gl) {
  GateCounts c;
  c.total = gl.gates.size();
  for (const auto &g : gl.gates) {
    switch (g.type) {
    case GateType::T:       c.t++; break;
    case GateType::Tdg:     c.tdg++; break;
    case GateType::CNOT:    c.cnot++; break;
    case GateType::H:       c.h++; break;
    case GateType::X:       c.x++; break;
    case GateType::Toffoli: c.toffoli++; break;
    }
  }
  return c;
}

static void printRow(const char *name, const QCGateList &gl) {
  GateCounts c = countGates(gl);
  // For Current algorithm: each abstract Toffoli decomposes to 7 T-gates + 6 CNOTs
  uint32_t effectiveT = c.tCount() + c.toffoli * 7;
  uint32_t effectiveCNOT = c.cnot + c.toffoli * 6;
  uint32_t effectiveTotal = c.total + c.toffoli * 12; // replace 1 Toffoli with 13 gates

  errs() << "  " << name;
  // Pad to 20 chars
  for (size_t i = strlen(name); i < 20; i++) errs() << " ";
  errs() << "| " << gl.num_qubits << "\t| "
         << effectiveT << "\t| " << effectiveCNOT << "\t| "
         << c.h << "\t| " << effectiveTotal << "\n";
}

/// Comparison test: run all 3 algorithms on the same XAGs and print table.
static bool testComparison() {
  errs() << "\n========== ALGORITHM COMPARISON ==========\n";

  // --- Circuit 1: (a AND b) XOR c ---
  {
    mockturtle::xag_network xag;
    auto a = xag.create_pi();
    auto b = xag.create_pi();
    auto c = xag.create_pi();
    auto ab = xag.create_and(a, b);
    auto result = xag.create_xor(ab, c);
    xag.create_po(result);

    XagContext ctx;
    ctx.xag = xag;
    ctx.optimized = true;

    QCGateList gl_cur = XAGToGateList::translate(ctx);
    QCGateList gl_ex  = ExistingMethod::translate(ctx);
    QCGateList gl_pr  = ProposedMethod::translate(ctx);

    errs() << "\n  Circuit: (a AND b) XOR c\n";
    errs() << "  Algorithm           | Qubits\t| T-count\t| CNOTs\t| H\t| Total\n";
    errs() << "  --------------------|-------|---------|-------|---|------\n";
    printRow("Current", gl_cur);
    printRow("Existing Method", gl_ex);
    printRow("Proposed Method", gl_pr);
  }

  // --- Circuit 2: a AND (b XOR c) ---
  {
    mockturtle::xag_network xag;
    auto a = xag.create_pi();
    auto b = xag.create_pi();
    auto c = xag.create_pi();
    auto bxc = xag.create_xor(b, c);
    auto result = xag.create_and(a, bxc);
    xag.create_po(result);

    XagContext ctx;
    ctx.xag = xag;
    ctx.optimized = true;

    QCGateList gl_cur = XAGToGateList::translate(ctx);
    QCGateList gl_ex  = ExistingMethod::translate(ctx);
    QCGateList gl_pr  = ProposedMethod::translate(ctx);

    errs() << "\n  Circuit: a AND (b XOR c)\n";
    errs() << "  Algorithm           | Qubits\t| T-count\t| CNOTs\t| H\t| Total\n";
    errs() << "  --------------------|-------|---------|-------|---|------\n";
    printRow("Current", gl_cur);
    printRow("Existing Method", gl_ex);
    printRow("Proposed Method", gl_pr);
  }

  errs() << "\n==========================================\n\n";
  return true; // comparison always passes
}

int main() {
  bool allPass = true;
  allPass &= testPipeline();
  allPass &= testAndCase2();
  allPass &= testExistingMethodPipeline();
  allPass &= testProposedMethodPipeline();
  allPass &= testExistingMethodAndOnePI();
  allPass &= testProposedMethodAndOnePI();
  allPass &= testComparison();
  return allPass ? 0 : 1;
}
