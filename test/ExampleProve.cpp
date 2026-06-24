// ExampleProve.cpp — build the slide-5 example XAG by hand and run the REAL
// translators on it, to prove the clean-side ancilla count (7 for Proposed).
//
//   f = ( x2 AND (x0 XOR x1) ) XOR ( x1 AND (x0 XOR x3) )
//
// node ids match xag.py::phase0_two_and (n4,n6 = XOR; n5,n7 = AND; n8 = out).
// The dirty-side count (4) comes from the SAT solver run on the same function.

#include "ExistingMethod.h"
#include "ProposedMethod.h"
#include "XAGToGateList.h"
#include "XagContext.h"

#include <cstdio>
#include <kitty/dynamic_truth_table.hpp>
#include <kitty/print.hpp>
#include <mockturtle/algorithms/simulation.hpp>
#include <mockturtle/networks/xag.hpp>

using namespace xagtdep;

int main() {
  mockturtle::xag_network xag;
  auto x0 = xag.create_pi(), x1 = xag.create_pi();
  auto x2 = xag.create_pi(), x3 = xag.create_pi();
  auto n4 = xag.create_xor(x0, x1);   // x0 ^ x1
  auto n6 = xag.create_xor(x0, x3);   // x0 ^ x3
  auto n5 = xag.create_and(x2, n4);   // x2 & (x0^x1)   <- AND
  auto n7 = xag.create_and(x1, n6);   // x1 & (x0^x3)   <- AND
  auto n8 = xag.create_xor(n5, n7);   // output
  xag.create_po(n8);

  // Prove it really is f (truth table over x0,x1,x2,x3).
  auto tts = mockturtle::simulate<kitty::dynamic_truth_table>(
      xag, mockturtle::default_simulator<kitty::dynamic_truth_table>(xag.num_pis()));
  printf("f = ( x2 & (x0^x1) ) ^ ( x1 & (x0^x3) )\n");
  printf("XAG: PIs=%u  gates=%u  POs=%u   truth_table(hex)=%s\n",
         (unsigned)xag.num_pis(), (unsigned)xag.num_gates(),
         (unsigned)xag.num_pos(), kitty::to_hex(tts[0]).c_str());

  XagContext ctx;
  ctx.xag = xag;
  ctx.optimized = true;

  auto cur = XAGToGateList::translate(ctx);
  auto ex = ExistingMethod::translate(ctx);
  auto pr = ProposedMethod::translate(ctx);

  printf("\nmethod            qubits   ancilla\n");
  printf("current (Toffoli)   %4u     %4u\n", cur.num_qubits, cur.num_ancillas);
  printf("existing (Alg 1)    %4u     %4u\n", ex.num_qubits, ex.num_ancillas);
  printf("proposed (Alg 2)    %4u     %4u   <- clean baseline\n",
         pr.num_qubits, pr.num_ancillas);

  // Emit the proposed gate list (JSON) so the slide can render the real circuit.
  printf("PROPOSED_JSON %s\n", pr.toJSON().c_str());
  return 0;
}
