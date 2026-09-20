// blif_to_tcount.cpp
//
// Minimal CLI shim: reads a combinational BLIF file (hand-rolled parser
// -- mockturtle::blif_reader cannot be used here, since it requires
// generic ntk.create_node(inputs, truth_table), which xag_network does
// NOT implement -- it only exposes create_and/create_xor directly),
// builds a mockturtle::xag_network by hand, runs
// caterpillar::logic_network_synthesis with xag_mapping_strategy, then
// caterpillar::decompose_with_ands to actually expand AND gates into
// their T-gate decomposition, and prints a single-line JSON object
// with T-count etc. to stdout.
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
// Usage: ./blif_to_tcount <path/to/file.blif>
// Output: {"ok":true,"t_count":12,"cnot_count":34,"h_count":5,"total":51,
//          "qubits":9,"and_pos":2}
//     or: {"ok":false,"error":"<message>"}

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include <mockturtle/networks/xag.hpp>

#include <caterpillar/caterpillar.hpp>
#include <caterpillar/synthesis/decompose_with_ands.hpp>
#include <caterpillar/details/utils.hpp>
#include <tweedledum/gates/mcmt_gate.hpp>
#include <tweedledum/networks/netlist.hpp>

namespace {

std::string jsonEscape(const std::string &s) {
  std::string out;
  for (char c : s) {
    if (c == '"' || c == '\\')
      out += '\\';
    out += c;
  }
  return out;
}

std::vector<std::string> splitWs(const std::string &s) {
  std::istringstream iss(s);
  std::vector<std::string> tokens;
  std::string tok;
  while (iss >> tok)
    tokens.push_back(tok);
  return tokens;
}

std::string rstrip(std::string s) {
  while (!s.empty() && (s.back() == '\n' || s.back() == '\r'))
    s.pop_back();
  return s;
}

// True for a canonical 2-input XOR/XNOR cover (matches
// pebbling_solver._is_xor_cover exactly).
bool isXorCover2(const std::vector<std::string> &rows) {
  if (rows.size() != 2)
    return false;
  auto has = [&](const std::string &r) {
    for (const auto &row : rows)
      if (row == r)
        return true;
    return false;
  };
  bool xorPattern = has("01 1") && has("10 1");
  bool xnorPattern = has("00 1") && has("11 1");
  return xorPattern || xnorPattern;
}

// True for a canonical 1-input NOT cover (matches write_blif's
// single-fanin convention: "0 1" == NOT, "1 1" == buffer).
bool isNotCover1(const std::vector<std::string> &rows) {
  if (rows.size() != 1)
    return false;
  return rows[0] == "0 1";
}

// Minimal, strict BLIF parser for the subset write_blif() emits: 1- or
// 2-input .names blocks only, no latches/subckts/multi-output rows.
// Throws std::runtime_error with a descriptive message on any
// unsupported construct.
mockturtle::xag_network parseBlif(const std::string &path) {
  std::ifstream in(path);
  if (!in.is_open())
    throw std::runtime_error("could not open BLIF file: " + path);

  mockturtle::xag_network xag;
  std::unordered_map<std::string, mockturtle::xag_network::signal> signals;
  std::vector<std::string> outputNames;

  bool havePending = false;
  std::vector<std::string> pendingFanins;
  std::string pendingOutput;
  std::vector<std::string> pendingRows;

  auto getOrCreatePi = [&](const std::string &name) {
    auto it = signals.find(name);
    if (it != signals.end())
      return it->second;
    auto sig = xag.create_pi();
    signals.emplace(name, sig);
    return sig;
  };

  auto flushPending = [&]() {
    if (!havePending)
      return;

    if (pendingFanins.size() == 1) {
      auto aSig = getOrCreatePi(pendingFanins[0]);
      mockturtle::xag_network::signal outSig;
      if (isNotCover1(pendingRows)) {
        // a XOR 1 == NOT a
        outSig = xag.create_xor(aSig, xag.get_constant(true));
      } else {
        // buffer / identity: XAG has no dedicated buffer primitive, so
        // model it as XOR with constant 0 (a ^ 0 == a), which costs
        // nothing extra in the synthesized circuit.
        outSig = xag.create_xor(aSig, xag.get_constant(false));
      }
      signals[pendingOutput] = outSig;
    } else if (pendingFanins.size() == 2) {
      auto aSig = getOrCreatePi(pendingFanins[0]);
      auto bSig = getOrCreatePi(pendingFanins[1]);
      mockturtle::xag_network::signal outSig;
      if (isXorCover2(pendingRows)) {
        outSig = xag.create_xor(aSig, bSig);
      } else {
        outSig = xag.create_and(aSig, bSig);
      }
      signals[pendingOutput] = outSig;
    } else {
      throw std::runtime_error(
          "blif_to_tcount only supports 1- or 2-input .names blocks; "
          "got " + std::to_string(pendingFanins.size()) +
          " fanin(s) for output '" + pendingOutput + "'");
    }

    havePending = false;
    pendingFanins.clear();
    pendingOutput.clear();
    pendingRows.clear();
  };

  std::string rawLine, buf;
  std::vector<std::string> joinedLines;
  while (std::getline(in, rawLine)) {
    std::string line = rstrip(rawLine);
    if (!line.empty() && line.back() == '\\') {
      buf += line.substr(0, line.size() - 1) + " ";
    } else {
      buf += line;
      joinedLines.push_back(buf);
      buf.clear();
    }
  }
  if (!buf.empty())
    joinedLines.push_back(buf);

  for (const auto &raw : joinedLines) {
    std::string line = raw;
    size_t start = line.find_first_not_of(" \t");
    size_t end = line.find_last_not_of(" \t");
    line = (start == std::string::npos) ? "" : line.substr(start, end - start + 1);

    if (line.empty() || line[0] == '#')
      continue;

    if (line.rfind(".model", 0) == 0)
      continue;

    if (line.rfind(".inputs", 0) == 0) {
      flushPending();
      auto tokens = splitWs(line);
      for (size_t i = 1; i < tokens.size(); ++i)
        getOrCreatePi(tokens[i]);
      continue;
    }

    if (line.rfind(".outputs", 0) == 0) {
      flushPending();
      auto tokens = splitWs(line);
      for (size_t i = 1; i < tokens.size(); ++i)
        outputNames.push_back(tokens[i]);
      continue;
    }

    if (line.rfind(".names", 0) == 0) {
      flushPending();
      auto tokens = splitWs(line);
      if (tokens.size() < 2)
        continue;
      pendingFanins.assign(tokens.begin() + 1, tokens.end() - 1);
      pendingOutput = tokens.back();
      for (const auto &f : pendingFanins)
        if (signals.find(f) == signals.end())
          getOrCreatePi(f);
      havePending = true;
      pendingRows.clear();
      continue;
    }

    if (line.rfind(".latch", 0) == 0 || line.rfind(".gate", 0) == 0 ||
        line.rfind(".subckt", 0) == 0) {
      throw std::runtime_error(
          "blif_to_tcount only supports combinational .names-based BLIF "
          "files (found: '" + line + "')");
    }

    if (line.rfind(".end", 0) == 0) {
      flushPending();
      continue;
    }

    if (havePending)
      pendingRows.push_back(line);
  }

  flushPending();

  for (const auto &outName : outputNames) {
    auto it = signals.find(outName);
    if (it == signals.end())
      throw std::runtime_error("output '" + outName +
                                "' was never defined by a .names block");
    xag.create_po(it->second);
  }

  return xag;
}

} // namespace

int main(int argc, char **argv) {
  if (argc != 2) {
    std::cout << "{\"ok\":false,\"error\":\"usage: blif_to_tcount <file.blif>\"}"
              << std::endl;
    return 2;
  }

  mockturtle::xag_network xag;
  try {
    xag = parseBlif(argv[1]);
  } catch (const std::exception &e) {
    std::cout << "{\"ok\":false,\"error\":\"" << jsonEscape(e.what()) << "\"}"
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
    caterpillar::xag_mapping_strategy strategy;
    caterpillar::logic_network_synthesis(circ, xag, strategy);

    // logic_network_synthesis alone only produces AND-as-2-controlled
    // gates and XOR-as-CNOT -- no T gates yet. decompose_with_ands
    // expands each AND into its actual Hadamard/T/CNOT sequence.
    tweedledum::netlist<tweedledum::mcmt_gate> qcirc;
    caterpillar::decompose_with_ands(qcirc, circ);

    uint32_t t_count = 0, cnot_count = 0, h_count = 0, total = 0;
    qcirc.foreach_cgate([&](auto const &node) {
      auto const &gate = node.gate;
      total++;
      switch (gate.operation()) {
      case tweedledum::gate_set::t:
      case tweedledum::gate_set::t_dagger:
        t_count++;
        break;
      case tweedledum::gate_set::cx:
      case tweedledum::gate_set::mcx:
        cnot_count++;
        break;
      case tweedledum::gate_set::hadamard:
        h_count++;
        break;
      default:
        break;
      }
      return true;
    });

    std::cout << "{"
              << "\"ok\":true,"
              << "\"t_count\":" << t_count << ","
              << "\"cnot_count\":" << cnot_count << ","
              << "\"h_count\":" << h_count << ","
              << "\"total\":" << total << ","
              << "\"qubits\":" << qcirc.num_qubits() << ","
              << "\"and_pos\":" << and_pos << "}" << std::endl;
    return 0;
  } catch (const std::exception &e) {
    std::cout << "{\"ok\":false,\"error\":\"" << jsonEscape(e.what()) << "\"}"
              << std::endl;
    return 1;
  }
}
