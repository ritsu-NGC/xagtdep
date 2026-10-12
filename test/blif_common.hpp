// Shared BLIF reader for test/blif_to_tcount.cpp and test/blif_optimize.cpp.
//
// Factored out so BOTH shims parse the IDENTICAL BLIF subset the same way --
// this avoids a second confound (reader divergence) on top of circuit-level
// differences when comparing "ours" vs caterpillar on the same BLIF file.
//
// Supported BLIF subset (must match pebbling_solver.write_blif()):
//   .model <name> / .inputs / .outputs
//   .names <fanin1> [<fanin2>] <output>   (exactly 1 or 2 fanins)
//     2-input cover rows: "11 1" -> AND, "01 1"/"10 1" -> XOR
//     1-input cover rows: "1 1"  -> buffer, "0 1" -> NOT (inverter)
//   .end
#pragma once

#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include <mockturtle/networks/xag.hpp>

namespace blif_common {

inline std::string jsonEscape(const std::string &s) {
  std::string out;
  for (char c : s) {
    if (c == '"' || c == '\\')
      out += '\\';
    out += c;
  }
  return out;
}

inline std::vector<std::string> splitWs(const std::string &s) {
  std::istringstream iss(s);
  std::vector<std::string> tokens;
  std::string tok;
  while (iss >> tok)
    tokens.push_back(tok);
  return tokens;
}

inline std::string rstrip(std::string s) {
  while (!s.empty() && (s.back() == '\n' || s.back() == '\r'))
    s.pop_back();
  return s;
}

// True for a canonical 2-input XOR/XNOR cover (matches
// pebbling_solver._is_xor_cover exactly).
inline bool isXorCover2(const std::vector<std::string> &rows) {
  if (rows.size() != 2)
    return false;
  std::vector<std::string> sorted_rows = rows;
  std::sort(sorted_rows.begin(), sorted_rows.end());
  static const std::vector<std::string> xor_pattern = {"01 1", "10 1"};
  static const std::vector<std::string> xnor_pattern = {"00 1", "11 1"};
  return sorted_rows == xor_pattern || sorted_rows == xnor_pattern;
}

// True for a canonical 2-input AND cover: single row "11 1".
inline bool isAndCover2(const std::vector<std::string> &rows) {
  return rows.size() == 1 && rows[0] == "11 1";
}

// 1-input cover rows: "1 1" -> buffer (identity), "0 1" -> NOT (inverter).
inline bool isBufferCover1(const std::vector<std::string> &rows) {
  return rows.size() == 1 && rows[0] == "1 1";
}

inline bool isNotCover1(const std::vector<std::string> &rows) {
  return rows.size() == 1 && rows[0] == "0 1";
}

struct ParsedNamesNode {
  std::vector<std::string> fanins;
  std::string output;
  std::vector<std::string> rows;
};

// Reads a BLIF file (the supported subset above) and returns:
//  - ordered list of PI names (as declared by .inputs)
//  - ordered list of PO names (as declared by .outputs)
//  - ordered list of .names nodes, in file order (each with its fanins,
//    output name, and raw cover rows) -- NOT yet built into any network,
//    so callers can build whichever mockturtle network type they need.
struct ParsedBlif {
  std::vector<std::string> pi_names;
  std::vector<std::string> po_names;
  std::vector<ParsedNamesNode> names_nodes;
};

inline ParsedBlif read_blif_raw(const std::string &path) {
  std::ifstream f(path);
  if (!f.is_open())
    throw std::runtime_error("could not open BLIF file: " + path);

  ParsedBlif parsed;

  std::string line;
  std::vector<std::string> joined_lines;
  std::string buf;
  while (std::getline(f, line)) {
    line = rstrip(line);
    if (!line.empty() && line.back() == '\\') {
      buf += line.substr(0, line.size() - 1) + " ";
    } else {
      buf += line;
      joined_lines.push_back(buf);
      buf.clear();
    }
  }
  if (!buf.empty())
    joined_lines.push_back(buf);

  bool have_pending = false;
  std::string pending_output;
  std::vector<std::string> pending_fanins;
  std::vector<std::string> pending_rows;

  auto flush_pending = [&]() {
    if (!have_pending)
      return;
    parsed.names_nodes.push_back(
        ParsedNamesNode{pending_fanins, pending_output, pending_rows});
    have_pending = false;
    pending_output.clear();
    pending_fanins.clear();
    pending_rows.clear();
  };

  for (const auto &raw : joined_lines) {
    std::string stripped = raw;
    // trim leading/trailing whitespace
    size_t start = stripped.find_first_not_of(" \t");
    if (start == std::string::npos) {
      stripped.clear();
    } else {
      size_t end = stripped.find_last_not_of(" \t");
      stripped = stripped.substr(start, end - start + 1);
    }

    if (stripped.empty() || stripped[0] == '#')
      continue;

    if (stripped.rfind(".model", 0) == 0)
      continue;

    if (stripped.rfind(".inputs", 0) == 0) {
      flush_pending();
      auto tokens = splitWs(stripped);
      for (size_t i = 1; i < tokens.size(); ++i)
        parsed.pi_names.push_back(tokens[i]);
      continue;
    }

    if (stripped.rfind(".outputs", 0) == 0) {
      flush_pending();
      auto tokens = splitWs(stripped);
      for (size_t i = 1; i < tokens.size(); ++i)
        parsed.po_names.push_back(tokens[i]);
      continue;
    }

    if (stripped.rfind(".names", 0) == 0) {
      flush_pending();
      auto tokens = splitWs(stripped);
      if (tokens.size() < 2)
        continue;
      std::vector<std::string> names(tokens.begin() + 1, tokens.end());
      std::string out_name = names.back();
      names.pop_back();
      have_pending = true;
      pending_fanins = names;
      pending_output = out_name;
      pending_rows.clear();
      continue;
    }

    if (stripped.rfind(".latch", 0) == 0 || stripped.rfind(".gate", 0) == 0 ||
        stripped.rfind(".subckt", 0) == 0) {
      throw std::runtime_error(
          "blif_common::read_blif_raw only supports combinational "
          ".names-based BLIF files");
    }

    if (stripped.rfind(".end", 0) == 0) {
      flush_pending();
      continue;
    }

    if (have_pending) {
      pending_rows.push_back(stripped);
    }
  }

  flush_pending();
  return parsed;
}

// Builds a mockturtle::xag_network from a ParsedBlif. Matches
// pebbling_solver.read_blif()'s node classification: 2-input "11 1" -> AND,
// 2-input XOR/XNOR cover -> XOR, 1-input "1 1"/"0 1" -> buffer/NOT. Buffer
// and NOT are modeled via XOR with a constant (matching blif_to_tcount.cpp's
// original parseBlif convention exactly: buffer == a ^ 0, NOT == a ^ 1 --
// NOT via xag.create_not(a) would be semantically equivalent but must NOT
// be used here, to keep both shims' XAG node identities byte-for-byte
// consistent).
inline mockturtle::xag_network build_xag_from_parsed(const ParsedBlif &parsed) {
  using namespace mockturtle;
  xag_network xag;
  std::unordered_map<std::string, xag_network::signal> name_to_signal;

  auto get_or_create_pi = [&](const std::string &name) -> xag_network::signal {
    auto it = name_to_signal.find(name);
    if (it != name_to_signal.end())
      return it->second;
    auto sig = xag.create_pi();
    name_to_signal[name] = sig;
    return sig;
  };

  for (const auto &pi : parsed.pi_names)
    get_or_create_pi(pi);

  for (const auto &node : parsed.names_nodes) {
    for (const auto &fi : node.fanins)
      get_or_create_pi(fi); // no-op if already present/created elsewhere

    xag_network::signal result;
    if (node.fanins.size() == 2) {
      auto a = name_to_signal.at(node.fanins[0]);
      auto b = name_to_signal.at(node.fanins[1]);
      if (isAndCover2(node.rows)) {
        result = xag.create_and(a, b);
      } else if (isXorCover2(node.rows)) {
        result = xag.create_xor(a, b);
      } else {
        throw std::runtime_error(
            "blif_common::build_xag_from_parsed: unsupported 2-input cover "
            "for node " +
            node.output);
      }
    } else if (node.fanins.size() == 1) {
      auto a = name_to_signal.at(node.fanins[0]);
      if (isBufferCover1(node.rows)) {
        // Matches blif_to_tcount.cpp's original parseBlif exactly: XAG has
        // no dedicated buffer primitive, so model buffer as a ^ 0.
        result = xag.create_xor(a, xag.get_constant(false));
      } else if (isNotCover1(node.rows)) {
        // Matches blif_to_tcount.cpp's original parseBlif exactly: NOT is
        // modeled as a ^ 1, not a dedicated create_not call.
        result = xag.create_xor(a, xag.get_constant(true));
      } else {
        throw std::runtime_error(
            "blif_common::build_xag_from_parsed: unsupported 1-input cover "
            "for node " +
            node.output);
      }
    } else {
      throw std::runtime_error(
          "blif_common::build_xag_from_parsed: unsupported fanin count (" +
          std::to_string(node.fanins.size()) + ") for node " + node.output);
    }

    name_to_signal[node.output] = result;
  }

  for (const auto &po_name : parsed.po_names) {
    auto it = name_to_signal.find(po_name);
    if (it == name_to_signal.end())
      throw std::runtime_error(
          "blif_common::build_xag_from_parsed: output '" + po_name +
          "' never defined by any .names node or .inputs declaration");
    xag.create_po(it->second);
  }

  return xag;
}

inline mockturtle::xag_network read_blif_as_xag(const std::string &path) {
  return build_xag_from_parsed(read_blif_raw(path));
}

} // namespace blif_common
