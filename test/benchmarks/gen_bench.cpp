// gen_bench.cpp — persistent benchmark generator for the pebbling pipeline.
//
// Why this exists: RandomXAG's uniform random pairing collapses under
// mockturtle structural hashing (a requested 100-AND graph folds to a
// handful of gates), so it cannot produce genuinely large XAGs. This tool
// builds CHAIN-STRUCTURED graphs — every gate takes the newest node as one
// fanin, so each fanin pair is provably fresh and hashing can never merge
// it. Requested gate count == actual gate count, checked hard.
//
// Flavors:
//   std  — every AND keeps one PI fanin (RandomXAG's constraint): Existing/
//          Proposed can synthesize it.
//   hard — ANDs take two COMPUTED fanins: the shape the DFS methods have no
//          case for; only PebblingMethod (and Current) handle it.
//
// Tiers:
//   verified — ≤ ~20 qubits: emitted via runXagCase (all methods) and meant
//              for the strict statevector+QCEC lane.
//   big      — 40..400 gates: translators driven DIRECTLY, skipping Current
//              (it duplicates shared cones and explodes exponentially on
//              deep chains — 40 chained gates → 450k gates, 100 → OOM).
//              Emits gate lists, metrics meta, structure dump, and the
//              Bennett sequence file for each case.
//
// Usage: gen_bench <output-dir>

#include "QCVerificationCommon.h"

#include "BennettSequence.h"
#include "ExistingMethod.h"
#include "PebblingMethod.h"
#include "ProposedMethod.h"
#include "SequenceJson.h"
#include "XagContext.h"

#include <json.hpp>
#include <kitty/kitty.hpp>
#include <mockturtle/algorithms/simulation.hpp>

#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <sys/stat.h>
#include <vector>

using mockturtle::xag_network;
using json = nlohmann::json;

struct CaseSpec {
  std::string name, tier;
  bool hard;
  uint32_t pis, n_and, n_xor;
  uint64_t seed;
  bool complemented_po;
};

static xag_network buildChain(const CaseSpec &c) {
  xag_network xag;
  std::mt19937_64 rng(c.seed);
  std::vector<xag_network::signal> pis, pool;
  for (uint32_t i = 0; i < c.pis; ++i) {
    auto s = xag.create_pi();
    pis.push_back(s);
    pool.push_back(s);
  }

  std::vector<char> ops(c.n_and, 'A');
  ops.insert(ops.end(), c.n_xor, 'X');
  std::shuffle(ops.begin(), ops.end(), rng);
  if (c.hard) { // seed two XORs first so ANDs always have computed fanins
    int placed = 0;
    for (size_t i = 0; i < ops.size() && placed < 2; ++i)
      if (ops[i] == 'X')
        std::swap(ops[placed++], ops[i]);
  }

  std::vector<xag_network::signal> gate_sigs;
  auto last = pool[0];

  auto pickOther = [&](const std::vector<xag_network::signal> &from) {
    xag_network::signal s;
    do { // same-node pairs fold (XOR(a,a)=0, AND(a,a)=a, complements → const)
      s = from[rng() % from.size()];
    } while (xag.get_node(s) == xag.get_node(last));
    if (rng() % 3 == 0)
      s = !s;
    return s;
  };

  for (char op : ops) {
    xag_network::signal next;
    if (op == 'A')
      next = (c.hard && !gate_sigs.empty())
                 ? xag.create_and(last, pickOther(gate_sigs)) // both computed
                 : xag.create_and(last, pickOther(pis));      // one PI fanin
    else
      next = xag.create_xor(last, pickOther(pool));
    pool.push_back(next);
    gate_sigs.push_back(next);
    last = next;
  }

  xag.create_po(c.complemented_po ? !last : last);
  return xag;
}

// ── Direct emission for the big tier (Current skipped) ────────────────────

static void writeFileStr(const std::string &path, const std::string &s) {
  std::ofstream f(path);
  f << s;
}

static json metricsJson(const xagtdep::QCGateList &gl) {
  uint32_t t = 0, cx = 0;
  for (const auto &g : gl.gates) {
    if (g.type == xagtdep::GateType::T || g.type == xagtdep::GateType::Tdg)
      ++t;
    if (g.type == xagtdep::GateType::CNOT)
      ++cx;
  }
  return {{"t_count", t}, {"cnot_count", cx}, {"qubits", gl.num_qubits},
          {"total", gl.gates.size()}};
}

static std::string xagTruthHex(const xag_network &xag) {
  auto sim = mockturtle::default_simulator<kitty::dynamic_truth_table>(
      xag.num_pis());
  auto vals =
      mockturtle::simulate_nodes<kitty::dynamic_truth_table>(xag, sim);
  std::string hex;
  xag.foreach_po([&](auto sig) {
    if (!hex.empty())
      return;
    auto tt = vals[xag.get_node(sig)];
    if (xag.is_complemented(sig))
      tt = ~tt;
    hex = kitty::to_hex(tt);
  });
  return hex;
}

static void emitBigCase(const xag_network &xag, const CaseSpec &c, int idx,
                        const std::string &dir) {
  xagtdep::XagContext ctx;
  ctx.xag = xag;
  ctx.optimized = true;
  const std::string prefix = dir + "/xag_" + std::to_string(idx);

  json meta = {{"seed", c.seed},          {"config_pis", c.pis},
               {"config_ands", c.n_and},  {"config_xors", c.n_xor},
               {"num_pis", xag.num_pis()}, {"num_pos", xag.num_pos()},
               {"num_gates", xag.num_gates()},
               {"truth_table_hex", xagTruthHex(xag)},
               {"constraint_ok", !c.hard},
               {"current_skipped", "exponential on deep chains"}};

  auto emit = [&](const char *name, xagtdep::QCGateList gl) {
    writeFileStr(prefix + "_" + name + ".json", gl.toJSON());
    meta["metrics_" + std::string(name)] = metricsJson(gl);
    meta["output_qubit_" + std::string(name)] = gl.output_qubit;
    std::cerr << "  " << c.name << " " << name << ": qubits=" << gl.num_qubits
              << " T=" << meta["metrics_" + std::string(name)]["t_count"]
              << " total=" << gl.gates.size() << "\n";
  };

  if (!c.hard) { // DFS methods only understand the std shape
    emit("existing", xagtdep::ExistingMethod::translate(ctx));
    emit("proposed", xagtdep::ProposedMethod::translate(ctx));
  }
  emit("pebbling_proposed", xagtdep::PebblingMethod::translate(ctx));

  writeFileStr(prefix + "_structure.json", xagtdep::xagStructureToJSON(xag));
  writeFileStr(prefix + "_bennett_sequence.json",
               xagtdep::sequenceToJSON(xagtdep::BennettSequence::build(xag)));
  writeFileStr(prefix + "_meta.json", meta.dump());
}

int main(int argc, char **argv) {
  if (argc < 2) {
    std::cerr << "usage: gen_bench <output-dir>\n";
    return 2;
  }
  const std::string out = argv[1];
  mkdir(out.c_str(), 0755);
  mkdir((out + "/verified").c_str(), 0755);
  mkdir((out + "/big").c_str(), 0755);

  const std::vector<CaseSpec> cases = {
      // Sized so EVERY method stays ≤ ~17 qubits (the DFS methods burn ~1.4
      // wires/gate on chains; the brute-force statevector lane is 2^qubits
      // per input row — 21-qubit files take hours).
      {"v_std_4p9g", "verified", false, 4, 3, 6, 0xbe11a001, false},
      {"v_std_4p10g", "verified", false, 4, 4, 6, 0xbe11a002, true},
      {"v_std_5p10g", "verified", false, 5, 4, 6, 0xbe11a003, false},
      {"v_hard_5p9g", "verified", true, 5, 3, 6, 0xbe11a004, true},
      {"b_std_6p40g", "big", false, 6, 15, 25, 0xbe11b001, false},
      {"b_std_8p100g", "big", false, 8, 35, 65, 0xbe11b002, true},
      {"b_std_10p200g", "big", false, 10, 70, 130, 0xbe11b003, false},
      {"b_std_12p400g", "big", false, 12, 140, 260, 0xbe11b004, false},
      {"b_hard_8p80g", "big", true, 8, 30, 50, 0xbe11b005, false},
  };

  json manifest = json::array();
  int idx_verified = 0, idx_big = 0, failures = 0;

  for (const auto &c : cases) {
    xag_network xag = buildChain(c);
    const uint32_t want = c.n_and + c.n_xor;
    if (xag.num_gates() != want) {
      std::cerr << "[gen_bench] " << c.name << ": COLLAPSED — built "
                << xag.num_gates() << " gates, wanted " << want << "\n";
      ++failures;
      continue;
    }

    const bool verified = c.tier == "verified";
    const std::string dir = out + "/" + c.tier;
    const int idx = verified ? idx_verified++ : idx_big++;

    xagtdep::test::XagCaseConfig cfg{};
    cfg.seed = c.seed;
    cfg.pis = c.pis;
    cfg.ands = c.n_and;
    cfg.xors = c.n_xor;

    std::string methods;
    if (verified) {
      if (c.hard) { // DFS methods have no case for both-computed ANDs;
                    // Current stays small here and cross-checks pebbling
        xagtdep::test::runXagCase(xag, cfg, idx, dir, "current");
        xagtdep::test::runXagCase(xag, cfg, idx, dir, "pebbling_proposed");
        methods = "current,pebbling_proposed";
      } else {
        // Current is excluded on std chains: cone duplication inflates it to
        // 28-31 qubits even at 12 gates, which OOMs the statevector lane.
        // Equivalence is per-method against the truth-table oracle anyway.
        xagtdep::test::runXagCase(xag, cfg, idx, dir, "existing");
        xagtdep::test::runXagCase(xag, cfg, idx, dir, "proposed");
        xagtdep::test::runXagCase(xag, cfg, idx, dir, "pebbling_proposed");
        methods = "existing,proposed,pebbling_proposed";
      }
    } else {
      emitBigCase(xag, c, idx, dir);
      methods = c.hard ? "pebbling_proposed" : "existing,proposed,pebbling_proposed";
    }

    manifest.push_back({{"name", c.name},
                        {"tier", c.tier},
                        {"flavor", c.hard ? "hard" : "std"},
                        {"pis", c.pis},
                        {"ands", c.n_and},
                        {"xors", c.n_xor},
                        {"gates_actual", xag.num_gates()},
                        {"seed", c.seed},
                        {"complemented_po", c.complemented_po},
                        {"dir", c.tier},
                        {"idx", idx},
                        {"methods", methods}});
    std::cerr << "[gen_bench] " << c.name << ": " << xag.num_gates()
              << " gates OK -> " << dir << "/xag_" << idx << "_*\n";
  }

  std::ofstream mf(out + "/bench_manifest.json");
  mf << manifest.dump(2) << "\n";
  std::cerr << "[gen_bench] manifest: " << out << "/bench_manifest.json ("
            << manifest.size() << " cases, " << failures << " failures)\n";
  return failures ? 1 : 0;
}
