// test/blif_optimize.cpp
//
// Reads a BLIF file (same supported subset / same reader as
// blif_to_tcount.cpp, factored into blif_common.hpp so both shims never
// silently diverge on parsing), runs ONE mockturtle XAG optimization pass,
// and writes the resulting network back out as BLIF.
//
// This exists so run_caterpillar_experiments.py's --pre-optimize mode can
// feed the EXACT SAME optimized BLIF into BOTH run_our_pipeline and
// run_caterpillar, keeping the T-count comparison symmetric (see
// pebbling_solver.py / run_caterpillar_experiments.py module docstrings for
// why asymmetric inputs would invalidate the comparison).
//
// Supported --pass values:
//   const_fanin    : xag_constant_fanin_optimization (safe, structure-
//                    preserving; removes AND gates whose transitive linear
//                    fanin cone cancels to a constant or a bare signal)
//   cut_rewriting  : mockturtle::cut_rewriting with an XAG-aware
//                    resynthesis function (more aggressive; may restructure
//                    node identities significantly)
//   resub          : mockturtle resubstitution with free-XOR awareness
//                    (also restructures node identities)
//
// Build: registered in test/CMakeLists.txt --
//   add_executable(blif_optimize blif_optimize.cpp)
//   target_link_libraries(blif_optimize caterpillar mockturtle)
//   set_target_properties(blif_optimize PROPERTIES
//       RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
//
// Usage: ./blif_optimize <in.blif> <out.blif> --pass=<const_fanin|cut_rewriting|resub>
// Output (stdout): single-line JSON, e.g.
//   {"ok":true,"pass":"const_fanin","nodes_before":3336,"and_before":2080,
//    "nodes_after":2991,"and_after":1735}
//   or: {"ok":false,"error":"<message>"}
//
// IMPORTANT FOR THE IMPLEMENTER: the exact mockturtle API for cut_rewriting
// and resubstitution on xag_network (header names, function signatures,
// required view wrappers such as depth_view/fanout_view, and the correct
// resynthesis functor type) may differ from what is sketched below depending
// on the exact vendored mockturtle commit in this repo's lib/mockturtle.
// Please verify against the actual vendored headers under lib/mockturtle
// and adjust includes/calls as needed so the project builds cleanly. The
// goal/contract that must be preserved is:
//   - read BLIF via blif_common::read_blif_as_xag
//   - run exactly one of three named optimization passes on the resulting
//     xag_network, producing an optimized xag_network
//   - write the optimized network back out via mockturtle's BLIF writer
//   - print the JSON summary shown above (ok, pass, nodes_before,
//     and_before, nodes_after, and_after) on success, or
//     {"ok":false,"error":"..."} on failure, matching blif_to_tcount.cpp's
//     error-reporting style and exit codes (0 on success, 1 on a caught
//     exception, 2 on bad CLI args).

#include <iostream>
#include <string>

#include <mockturtle/algorithms/cleanup.hpp>
#include <mockturtle/algorithms/cut_rewriting.hpp>
#include <mockturtle/algorithms/node_resynthesis/xag_npn.hpp>
#include <mockturtle/algorithms/resubstitution.hpp>
#include <mockturtle/algorithms/xag_optimization.hpp>
#include <mockturtle/algorithms/xag_resub_withDC.hpp>
#include <mockturtle/io/write_blif.hpp>
#include <mockturtle/networks/xag.hpp>

#include "blif_common.hpp"

namespace {

using namespace mockturtle;

size_t count_and_gates(xag_network const &xag) {
  size_t count = 0;
  xag.foreach_gate([&](auto const &n) {
    if (xag.is_and(n))
      ++count;
  });
  return count;
}

// const_fanin: direct 1:1 call, already returns a new xag_network.
xag_network run_const_fanin(xag_network const &xag) {
  return xag_constant_fanin_optimization(xag);
}

// cut_rewriting: this mockturtle version returns a new network rather
// than modifying the input, using XAG NPN-based resynthesis.
xag_network run_cut_rewriting(xag_network xag) {
  xag_npn_resynthesis<xag_network> resyn;
  cut_rewriting_params ps;
  ps.cut_enumeration_ps.cut_size = 4;
  return cleanup_dangling(cut_rewriting(xag, resyn, ps));
}

// resub: free-XOR-aware resubstitution, operates in place on a copy.
// This entry point creates the depth/fanout views internally.
xag_network run_resub(xag_network xag) {
  resubstitution_params ps;
  ps.max_pis = 8;
  ps.max_inserts = 2;
  resubstitution_minmc_withDC(xag, ps);
  return cleanup_dangling(xag);
}

struct ParsedArgs {
  std::string in_path;
  std::string out_path;
  std::string pass_name;
};

ParsedArgs parse_args(int argc, char **argv) {
  ParsedArgs args;
  std::vector<std::string> positional;
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    const std::string pass_prefix = "--pass=";
    if (arg.rfind(pass_prefix, 0) == 0) {
      args.pass_name = arg.substr(pass_prefix.size());
    } else {
      positional.push_back(arg);
    }
  }
  if (positional.size() < 2)
    throw std::runtime_error(
        "usage: blif_optimize <in.blif> <out.blif> --pass=<const_fanin|"
        "cut_rewriting|resub>");
  args.in_path = positional[0];
  args.out_path = positional[1];
  if (args.pass_name.empty())
    args.pass_name = "const_fanin";
  return args;
}

} // namespace

int main(int argc, char **argv) {
  ParsedArgs args;
  try {
    args = parse_args(argc, argv);
  } catch (const std::exception &e) {
    std::cout << "{\"ok\":false,\"error\":\""
               << blif_common::jsonEscape(e.what()) << "\"}" << std::endl;
    return 2;
  }

  try {
    xag_network xag = blif_common::read_blif_as_xag(args.in_path);

    size_t nodes_before = xag.num_gates();
    size_t and_before = count_and_gates(xag);

    xag_network optimized;
    if (args.pass_name == "const_fanin") {
      optimized = run_const_fanin(xag);
    } else if (args.pass_name == "cut_rewriting") {
      optimized = run_cut_rewriting(xag);
    } else if (args.pass_name == "resub") {
      optimized = run_resub(xag);
    } else {
      throw std::runtime_error("unknown --pass value: " + args.pass_name +
                                " (expected const_fanin, cut_rewriting, or "
                                "resub)");
    }

    size_t nodes_after = optimized.num_gates();
    size_t and_after = count_and_gates(optimized);

    write_blif(optimized, args.out_path);

    std::cout << "{\"ok\":true,\"pass\":\""
               << blif_common::jsonEscape(args.pass_name)
               << "\",\"nodes_before\":" << nodes_before
               << ",\"and_before\":" << and_before
               << ",\"nodes_after\":" << nodes_after
               << ",\"and_after\":" << and_after << "}" << std::endl;
    return 0;
  } catch (const std::exception &e) {
    std::cout << "{\"ok\":false,\"error\":\""
               << blif_common::jsonEscape(e.what()) << "\"}" << std::endl;
    return 1;
  }
}
