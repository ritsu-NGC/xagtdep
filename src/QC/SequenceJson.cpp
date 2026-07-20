// SequenceJson.cpp — see SequenceJson.h for the schema and contract.
//
// Uses nlohmann::json v3.5.0, vendored inside caterpillar and exported as the
// `json` INTERFACE target (flat include). All nlohmann usage is isolated to
// this file so a hand-rolled parser (TupleParser precedent) could be dropped
// in if the vendored include ever breaks.

#include "SequenceJson.h"

#include <fstream>
#include <json.hpp>
#include <sstream>

using nlohmann::json;

namespace xagtdep {

std::string sequenceToJSON(const PebblingSequence &seq) {
  json steps = json::array();
  for (const auto &s : seq) {
    steps.push_back(
        {{"action", s.action == PebbleAction::Pebble ? "pebble" : "unpebble"},
         {"node", s.node},
         {"anc", s.anc}});
  }
  json root = {{"num_ancillas", sequenceNumAncillas(seq)}, {"steps", steps}};
  return root.dump();
}

namespace {

uint32_t requireU32(const json &obj, const char *key, size_t step_idx) {
  auto it = obj.find(key);
  if (it == obj.end() || !it->is_number_unsigned())
    throw PebblingError("[SequenceJson] step " + std::to_string(step_idx) +
                            ": missing or non-unsigned \"" + key + "\"",
                        step_idx);
  uint64_t v = it->get<uint64_t>();
  if (v > 0xffffffffull)
    throw PebblingError("[SequenceJson] step " + std::to_string(step_idx) +
                            ": \"" + key + "\" out of uint32 range",
                        step_idx);
  return static_cast<uint32_t>(v);
}

} // namespace

PebblingSequence sequenceFromJSON(const std::string &json_str) {
  json root;
  try {
    root = json::parse(json_str);
  } catch (const json::exception &e) {
    throw PebblingError(std::string("[SequenceJson] parse error: ") +
                        e.what());
  }

  if (!root.is_object() || root.find("steps") == root.end() ||
      !root["steps"].is_array())
    throw PebblingError(
        "[SequenceJson] root must be an object with a \"steps\" array");

  PebblingSequence seq;
  seq.reserve(root["steps"].size());
  size_t idx = 0;
  for (const auto &s : root["steps"]) {
    if (!s.is_object())
      throw PebblingError("[SequenceJson] step " + std::to_string(idx) +
                              ": not an object",
                          idx);
    auto action_it = s.find("action");
    if (action_it == s.end() || !action_it->is_string())
      throw PebblingError("[SequenceJson] step " + std::to_string(idx) +
                              ": missing or non-string \"action\"",
                          idx);
    std::string action = action_it->get<std::string>();
    PebbleAction a;
    if (action == "pebble")
      a = PebbleAction::Pebble;
    else if (action == "unpebble")
      a = PebbleAction::Unpebble;
    else
      throw PebblingError("[SequenceJson] step " + std::to_string(idx) +
                              ": unknown action \"" + action + "\"",
                          idx);
    seq.push_back({a, requireU32(s, "node", idx), requireU32(s, "anc", idx)});
    ++idx;
  }

  auto na_it = root.find("num_ancillas");
  if (na_it != root.end()) {
    if (!na_it->is_number_unsigned())
      throw PebblingError("[SequenceJson] \"num_ancillas\" must be unsigned");
    uint64_t declared = na_it->get<uint64_t>();
    uint32_t derived = sequenceNumAncillas(seq);
    if (declared != derived)
      throw PebblingError("[SequenceJson] num_ancillas mismatch: declared " +
                          std::to_string(declared) + ", derived " +
                          std::to_string(derived) + " from steps");
  }

  return seq;
}

bool writeSequenceFile(const std::string &path, const PebblingSequence &seq) {
  std::ofstream f(path);
  if (!f.is_open())
    return false;
  f << sequenceToJSON(seq);
  return f.good();
}

PebblingSequence readSequenceFile(const std::string &path) {
  std::ifstream f(path);
  if (!f.is_open())
    throw PebblingError("[SequenceJson] cannot open file: " + path);
  std::ostringstream ss;
  ss << f.rdbuf();
  return sequenceFromJSON(ss.str());
}

std::string xagStructureToJSON(const mockturtle::xag_network &xag) {
  json pos = json::array();
  xag.foreach_po([&](auto sig) {
    pos.push_back(
        {{"node", xag.node_to_index(xag.get_node(sig))},
         {"complemented", xag.is_complemented(sig)}});
  });

  json gates = json::array();
  xag.foreach_gate([&](auto node) {
    json fanins = json::array();
    xag.foreach_fanin(node, [&](auto sig) {
      fanins.push_back(
          {{"node", xag.node_to_index(xag.get_node(sig))},
           {"complemented", xag.is_complemented(sig)}});
    });
    gates.push_back({{"node", xag.node_to_index(node)},
                     {"type", xag.is_and(node) ? "and" : "xor"},
                     {"fanins", fanins}});
  });

  json root = {{"num_pis", xag.num_pis()},
               {"num_gates", xag.num_gates()},
               {"pos", pos},
               {"gates", gates}};
  return root.dump();
}

} // namespace xagtdep
