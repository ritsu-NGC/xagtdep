// blif_to_tcount.cpp
//
// Minimal CLI shim: reads a combinational BLIF file (hand-rolled parser
// -- mockturtle::blif_reader cannot be used here, since it requires
// generic ntk.create_node(inputs, truth_table), which xag_network does
// NOT implement -- it only exposes create_and/create_xor directly),
// builds a mockturtle::xag_network by hand, runs
// caterpillar::logic_network_synthesis with a selectable mapping
// strategy (see "STRATEGY SELECTION" below; defaults to the Z3-based
// xag_pebbling_mapping_strategy), then
// counts the exact gate statistics that caterpillar::decompose_with_ands
// would produce, and prints a single-line JSON object with T-count etc.
// to stdout. This avoids materializing tweedledum::mcmt_gate for large
// circuits: the vendored mcmt_gate bit-packs qubit ids into 32-bit masks
// and hard-codes network_max_num_qubits = 32, which aborts on large EPFL
// benchmarks even though logic_network_synthesis itself supports many more
// lazily allocated qubits.
//
// STRATEGY SELECTION (-s / --strategy, or BLIF_TO_TCOUNT_STRATEGY env var):
//   "pebbling" (DEFAULT): caterpillar::xag_pebbling_mapping_strategy --
//     solves the reversible pebbling game via Z3 (see caterpillar's
//     z3_pebble_solver), searching for a pebble-count-minimal schedule.
//     This is the SAME strategy family as this repo's own
//     pebbling_solver.py GatePebbleSolver, just operating node-by-node
//     on the raw XAG rather than on pre-grouped gate clusters. On
//     large, gate-dense circuits (e.g. EPFL's sqrt/hyp/div/
//     multiplier), this can be SLOW or fail to converge within
//     ps.search_timeout -- see "PEBBLING STRATEGY PARAMETERS" below
//     for the knobs controlling this.
//   "structural": caterpillar::xag_mapping_strategy -- a single linear
//     topological traversal with a fixed, local compute/uncompute
//     rule (no SAT solving at all, see MSC19 in caterpillar's
//     bibliography). Always O(n), but does not attempt to minimize
//     pebble count -- qubit usage is whatever the circuit's own peak
//     concurrent liveness structurally demands.
//
// PEBBLING STRATEGY PARAMETERS (only apply when --strategy=pebbling):
//   --pebble-limit <N>      caterpillar::pebbling_mapping_strategy_params::
//                           pebble_limit (0 == no limit, auto-grows if
//                           --increment-on-failure is set). Default 0.
//   --search-timeout <S>    ::search_timeout, seconds, caps the TOTAL
//                           iterative Z3 search across all incremental
//                           step counts within ONE pebble_limit attempt.
//                           Default 30 (caterpillar's own default).
//   --solver-timeout <MS>   ::solver_timeout, milliseconds, Z3's own
//                           per-check() timeout. Default 0 (no limit).
//   --conflict-limit <N>    ::conflict_limit, Z3 conflict cap per
//                           check(). Default 0 (no limit).
//   --increment-on-failure  ::increment_pebbles_on_failure -- if the
//                           search fails/times out at the current
//                           pebble_limit, retry with pebble_limit+1
//                           instead of giving up immediately.
//
// IMPORTANT (verified against real upstream gmeuli/caterpillar, which
// this project's CMakeLists.txt fetches at GIT_TAG master):
//   - caterpillar::logic_network_synthesis(circ, xag, strategy) alone
//     produces a netlist<stg_gate> where AND nodes are 2-controlled
//     gates and XOR nodes are CNOTs -- it does NOT contain any T
//     gates yet. T-gates only appear after running
//     caterpillar::decompose_with_ands(qcirc, circ), which expands
//     each AND into the actual Hadamard/T/CNOT synthesis sequence,
//     producing a netlist<mcmt_gate>.
//   - caterpillar::stg_gate derives from tweedledum::gate_base. There
//     is NO stg_gate::Op enum. The operation kind is retrieved via
//     the METHOD `gate.operation()`, returning a
//     `tweedledum::gate_set` enum value (gate_set::t,
//     gate_set::t_dagger, gate_set::cx, gate_set::mcx,
//     gate_set::hadamard, etc.). Both netlist<stg_gate> and
//     netlist<mcmt_gate> are iterated via
//     `net.foreach_cgate([&](auto const& node){ auto const& gate =
//     node.gate; ... })`, not a plain range-for.
//   - xag_pebbling_mapping_strategy requires caterpillar to be built
//     WITH Z3 (USE_Z3). If USE_Z3 is not defined, "pebbling" is
//     unavailable and this binary falls back to "structural"
//     automatically, printing a one-line stderr notice.
//
// "and_pos" field: count of primary outputs (net.pos in the Python
// pipeline / xag.foreach_po here) whose driving node is itself an AND
// gate (mockturtle::xag_network::is_and(node)). This is exposed so
// downstream analysis can correctly identify how many T-counted AND
// gates are "output-driving" (and therefore need a FULL Toffoli
// decomposition, cost 7, rather than the cheaper relative-phase
// AND-with-free-uncompute trick, cost 4) instead of using the total
// primary-output count (net.pos/xag.num_pos()), which may include
// non-AND-driven outputs (e.g. XOR- or PI-driven) and would otherwise
// give an incorrect correction term.
//
// Supported BLIF subset (must match pebbling_solver.write_blif()):
//   .model <name> / .inputs / .outputs
//   .names <fanin1> [<fanin2>] <output>   (exactly 1 or 2 fanins)
//     2-input cover rows: "11 1" -> AND, "01 1"/"10 1" -> XOR
//     1-input cover rows: "1 1"  -> buffer, "0 1" -> NOT (inverter)
//   .end
//
// Build: registered in test/CMakeLists.txt --
//   add_executable(blif_to_tcount blif_to_tcount.cpp)
//   target_link_libraries(blif_to_tcount caterpillar mockturtle)
//   set_target_properties(blif_to_tcount PROPERTIES
//       RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
//
// Usage: ./blif_to_tcount [options] <path/to/file.blif>
//   -s, --strategy <pebbling|structural>   default: pebbling
//       --pebble-limit <N>                 default: 0 (no limit)
//       --search-timeout <seconds>         default: 30
//       --solver-timeout <ms>              default: 0 (no limit)
//       --conflict-limit <N>               default: 0 (no limit)
//       --increment-on-failure             default: off
//   Env var BLIF_TO_TCOUNT_STRATEGY=pebbling|structural overrides the
//   default when -s/--strategy is not passed explicitly.
// Output: {"ok":true,"t_count":12,"cnot_count":34,"h_count":5,"total":51,
//          "qubits":9,"and_pos":2,"strategy":"pebbling"}
//     or: {"ok":false,"error":"<message>"}

#include <fstream>
#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <optional>
#include <tuple>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include <mockturtle/networks/xag.hpp>

#include <caterpillar/caterpillar.hpp>
#include <caterpillar/details/utils.hpp>
#include <tweedledum/networks/netlist.hpp>

#include "blif_common.hpp"

namespace {

struct DecomposedCounts {
  uint32_t t_count = 0;
  uint32_t cnot_count = 0;
  uint32_t h_count = 0;
  uint32_t total = 0;
};

DecomposedCounts countAsDecomposeWithAnds(
    const tweedledum::netlist<caterpillar::stg_gate> &circ) {
  DecomposedCounts counts;
  std::vector<std::tuple<uint32_t, uint32_t, uint32_t>> seen_ands;

  circ.foreach_cgate([&](auto const &node) {
    auto const &gate = node.gate;
    auto const cs = gate.controls();
    auto const ts = gate.targets();

    if (gate.num_controls() == 1 && gate.num_targets() == 1) {
      counts.cnot_count++;
      counts.total++;
      return true;
    }

    if (gate.num_controls() == 2 && gate.num_targets() == 1) {
      auto triple = std::make_tuple(cs[0].index(), cs[1].index(), ts[0].index());
      if (std::find(seen_ands.begin(), seen_ands.end(), triple) == seen_ands.end()) {
        counts.t_count += 6;
        counts.cnot_count += 6;
        counts.h_count += 2;
        counts.total += 14;
        seen_ands.push_back(triple);
      } else {
        counts.h_count += 1;
        counts.total += 3;
      }
    }

    return true;
  });

  return counts;
}

struct CliOptions {
  std::string blif_path;
  std::string strategy = "pebbling";
  uint32_t pebble_limit = 0;
  uint32_t search_timeout = 30;
  uint32_t solver_timeout = 0;
  uint32_t conflict_limit = 0;
  bool increment_on_failure = false;
};

std::optional<CliOptions> parseArgs(int argc, char **argv, std::string &errorOut) {
  CliOptions opts;

  if (const char *envStrategy = std::getenv("BLIF_TO_TCOUNT_STRATEGY")) {
    opts.strategy = envStrategy;
  }

  std::vector<std::string> args(argv + 1, argv + argc);
  std::vector<std::string> positional;

  try {
    for (size_t i = 0; i < args.size(); ++i) {
      const std::string &a = args[i];
      auto needValue = [&](const char *flagName) -> std::string {
        if (i + 1 >= args.size())
          throw std::runtime_error(std::string(flagName) + " requires a value");
        return args[++i];
      };

      if (a == "-s" || a == "--strategy") {
        opts.strategy = needValue("--strategy");
      } else if (a == "--pebble-limit") {
        opts.pebble_limit = static_cast<uint32_t>(std::stoul(needValue("--pebble-limit")));
      } else if (a == "--search-timeout") {
        opts.search_timeout = static_cast<uint32_t>(std::stoul(needValue("--search-timeout")));
      } else if (a == "--solver-timeout") {
        opts.solver_timeout = static_cast<uint32_t>(std::stoul(needValue("--solver-timeout")));
      } else if (a == "--conflict-limit") {
        opts.conflict_limit = static_cast<uint32_t>(std::stoul(needValue("--conflict-limit")));
      } else if (a == "--increment-on-failure") {
        opts.increment_on_failure = true;
      } else if (!a.empty() && a[0] == '-') {
        errorOut = "unknown option: " + a;
        return std::nullopt;
      } else {
        positional.push_back(a);
      }
    }
  } catch (const std::exception &e) {
    errorOut = e.what();
    return std::nullopt;
  }

  if (positional.size() != 1) {
    errorOut = "usage: blif_to_tcount [options] <file.blif>";
    return std::nullopt;
  }
  opts.blif_path = positional[0];

  if (opts.strategy != "pebbling" && opts.strategy != "structural") {
    errorOut = "unknown --strategy: '" + opts.strategy +
               "' (expected 'pebbling' or 'structural')";
    return std::nullopt;
  }

  return opts;
}

} // namespace

int main(int argc, char **argv) {
  std::string argError;
  auto maybeOpts = parseArgs(argc, argv, argError);
  if (!maybeOpts) {
    std::cout << "{\"ok\":false,\"error\":\"" << blif_common::jsonEscape(argError) << "\"}"
              << std::endl;
    return 2;
  }
  const CliOptions opts = *maybeOpts;

  std::string effectiveStrategy = opts.strategy;
#ifndef USE_Z3
  if (effectiveStrategy == "pebbling") {
    std::cerr << "[blif_to_tcount] NOTE: built without USE_Z3; "
                 "xag_pebbling_mapping_strategy is unavailable, falling "
                 "back to 'structural' (xag_mapping_strategy)."
              << std::endl;
    effectiveStrategy = "structural";
  }
#endif

  mockturtle::xag_network xag;
  try {
    xag = blif_common::read_blif_as_xag(opts.blif_path);
  } catch (const std::exception &e) {
    std::cout << "{\"ok\":false,\"error\":\"" << blif_common::jsonEscape(e.what()) << "\"}"
              << std::endl;
    return 1;
  }

  try {
    // Count primary outputs whose driving node is an AND gate --
    // needed downstream to distinguish "output-driving ANDs" (which
    // must be a full Toffoli, cost 7) from ordinary internal ANDs
    // (cost 4 via the relative-phase/free-uncompute trick), rather
    // than using the total PO count, which may include non-AND
    // (XOR- or PI-driven) outputs.
    uint32_t and_pos = 0;
    xag.foreach_po([&](auto const &f) {
      auto node = xag.get_node(f);
      if (xag.is_and(node))
        and_pos++;
    });

    tweedledum::netlist<caterpillar::stg_gate> circ;

    if (effectiveStrategy == "pebbling") {
#ifdef USE_Z3
      caterpillar::pebbling_mapping_strategy_params ps;
      ps.pebble_limit = opts.pebble_limit;
      ps.search_timeout = opts.search_timeout;
      ps.solver_timeout = opts.solver_timeout;
      ps.conflict_limit = opts.conflict_limit;
      ps.increment_pebbles_on_failure = opts.increment_on_failure;

      caterpillar::xag_pebbling_mapping_strategy strategy(ps);
      caterpillar::logic_network_synthesis(circ, xag, strategy);
#else
      // Unreachable: effectiveStrategy was already forced to
      // "structural" above when USE_Z3 is undefined.
      throw std::runtime_error("internal error: pebbling strategy "
                                "selected without USE_Z3");
#endif
    } else {
      caterpillar::xag_mapping_strategy strategy;
      caterpillar::logic_network_synthesis(circ, xag, strategy);
    }

    // decompose_with_ands would produce the final T/H/CNOT counts, but the
    // vendored mcmt_gate representation aborts above 32 qubits. Count the
    // exact same decomposition pattern directly from the stg netlist instead.
    const auto counts = countAsDecomposeWithAnds(circ);

    std::cout << "{"
              << "\"ok\":true,"
              << "\"t_count\":" << counts.t_count << ","
              << "\"cnot_count\":" << counts.cnot_count << ","
              << "\"h_count\":" << counts.h_count << ","
              << "\"total\":" << counts.total << ","
              << "\"qubits\":" << circ.num_qubits() << ","
              << "\"and_pos\":" << and_pos << ","
              << "\"strategy\":\"" << effectiveStrategy << "\""
              << "}" << std::endl;
    return 0;
  } catch (const std::exception &e) {
    std::cout << "{\"ok\":false,\"error\":\"" << blif_common::jsonEscape(e.what()) << "\"}"
              << std::endl;
    return 1;
  }
}
