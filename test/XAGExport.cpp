// XAGExport.cpp — dump the structural XAG produced by generate_random_xag() as
// JSON, so the Python dirty-pebbling solver (experiments/dirty-pebbling/) can
// schedule the *exact* same graph the C++ translators see.
//
// generate_random_xag() is NOT deterministic across platforms (the determinism
// fix was reverted, commit d8fe8c0), so the XAG must be generated once here and
// dumped — never regenerated independently in Python.
//
// Usage:  XAGExport --pis N --ands N --xors N --seed S
// Emits one JSON object on stdout, matching experiments/dirty-pebbling/xag.py:
//   { "pis":[...], "constant":0,
//     "nodes":[ {"id":I,"op":"and|xor","fanin":[a,b],"compl":[bool,bool]} ],
//     "pos":[ {"node":I,"compl":bool} ] }

#include "RandomXAG.h"

#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>

using namespace xagtdep;

int main(int argc, char **argv) {
  RandomXAGParams p;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> uint64_t { return std::strtoull(argv[++i], nullptr, 0); };
    if (a == "--pis")
      p.num_pis = static_cast<uint32_t>(next());
    else if (a == "--ands")
      p.num_and_gates = static_cast<uint32_t>(next());
    else if (a == "--xors")
      p.num_xor_gates = static_cast<uint32_t>(next());
    else if (a == "--seed")
      p.seed = next();
  }

  auto xag = generate_random_xag(p);

  std::string out = "{\"pis\":[";
  bool first = true;
  xag.foreach_pi([&](auto n) {
    if (!first) out += ",";
    first = false;
    out += std::to_string(xag.node_to_index(n));
  });
  out += "],\"constant\":0,\"nodes\":[";

  first = true;
  xag.foreach_gate([&](auto n) {
    if (!first) out += ",";
    first = false;
    out += "{\"id\":" + std::to_string(xag.node_to_index(n));
    out += ",\"op\":\"";
    out += (xag.is_and(n) ? "and" : "xor");
    out += "\",\"fanin\":[";
    std::string compl_s;
    bool ff = true;
    xag.foreach_fanin(n, [&](auto sig) {
      if (!ff) {
        out += ",";
        compl_s += ",";
      }
      ff = false;
      out += std::to_string(xag.node_to_index(xag.get_node(sig)));
      compl_s += (xag.is_complemented(sig) ? "true" : "false");
    });
    out += "],\"compl\":[" + compl_s + "]}";
  });
  out += "],\"pos\":[";

  first = true;
  xag.foreach_po([&](auto sig) {
    if (!first) out += ",";
    first = false;
    out += "{\"node\":" + std::to_string(xag.node_to_index(xag.get_node(sig)));
    out += ",\"compl\":";
    out += (xag.is_complemented(sig) ? "true" : "false");
    out += "}";
  });
  out += "]}";

  std::cout << out << std::endl;
  return 0;
}
