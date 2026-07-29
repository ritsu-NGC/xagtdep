// QCVerificationCommon.cpp — Shared per-XAG runner used by both
// QCVerificationTest (full sweep) and CIQCVerification (passing-set subset).

#include "QCVerificationCommon.h"

#include "ExistingMethod.h"
#include "PebblingMethod.h"
#include "ProposedMethod.h"
#include "QCGateList.h"
#include "RandomXAG.h"
#include "SequenceJson.h"
#include "XAGToGateList.h"
#include "XagContext.h"
#include "llvm/Support/raw_ostream.h"
#include <algorithm>
#include <cstdio>
#include <fstream>
#include <kitty/dynamic_truth_table.hpp>
#include <kitty/print.hpp>
#include <mockturtle/algorithms/simulation.hpp>

using namespace llvm;

namespace {

struct Metrics {
  uint32_t t_count = 0;
  uint32_t t_depth = 0;
  uint32_t cnot_count = 0;
  uint32_t h_count = 0;
  uint32_t total = 0;
  uint32_t qubits = 0;
};

Metrics computeMetrics(const xagtdep::QCGateList &gl) {
  Metrics m;
  m.total = gl.gates.size();
  m.qubits = gl.num_qubits;
  // ASAP T-depth: every gate is scheduled at the max layer of its qubits;
  // T/Tdg start a new T-layer (max+1), all other gates only propagate the
  // max. Counts explicit T/Tdg only — abstract Toffolis (Current method)
  // contribute 0 here; their decomposed cost is reported separately.
  std::vector<uint32_t> layer(gl.num_qubits, 0);
  auto touch = [&](const xagtdep::GateOp &g, bool isT) {
    uint32_t mx = layer[g.target];
    for (uint32_t c : g.controls)
      mx = std::max(mx, layer[c]);
    if (isT)
      mx += 1;
    layer[g.target] = mx;
    for (uint32_t c : g.controls)
      layer[c] = mx;
  };
  for (const auto &g : gl.gates) {
    switch (g.type) {
    case xagtdep::GateType::T:
    case xagtdep::GateType::Tdg:
      m.t_count++;
      touch(g, true);
      break;
    case xagtdep::GateType::CNOT:
      m.cnot_count++;
      touch(g, false);
      break;
    case xagtdep::GateType::H:
      m.h_count++;
      touch(g, false);
      break;
    default:
      touch(g, false);
      break;
    }
  }
  for (uint32_t l : layer)
    m.t_depth = std::max(m.t_depth, l);
  return m;
}

std::string metricsJSON(const Metrics &m) {
  return "{\"t_count\":" + std::to_string(m.t_count) +
         ",\"t_depth\":" + std::to_string(m.t_depth) +
         ",\"cnot_count\":" + std::to_string(m.cnot_count) +
         ",\"h_count\":" + std::to_string(m.h_count) +
         ",\"total\":" + std::to_string(m.total) +
         ",\"qubits\":" + std::to_string(m.qubits) + "}";
}

bool writeFile(const std::string &path, const std::string &content) {
  std::ofstream f(path);
  if (!f.is_open())
    return false;
  f << content;
  return f.good();
}

} // namespace

namespace xagtdep {
namespace test {

bool runXagCase(const mockturtle::xag_network &xag, const XagCaseConfig &cfg,
                int idx, const std::string &dataDir,
                const std::string &method, const PebblingSequence *pebbling) {
  try {
    bool constraint_ok = validate_and_constraint(xag);

    mockturtle::default_simulator<kitty::dynamic_truth_table> sim(
        xag.num_pis());
    auto node_values =
        mockturtle::simulate_nodes<kitty::dynamic_truth_table>(xag, sim);

    std::string tt_hex;
    xag.foreach_po([&](auto signal) {
      if (!tt_hex.empty())
        return;
      auto node = xag.get_node(signal);
      kitty::dynamic_truth_table tt = node_values[node];
      if (xag.is_complemented(signal))
        tt = ~tt;
      tt_hex = kitty::to_hex(tt);
    });

    XagContext ctx;
    ctx.xag = xag;
    ctx.optimized = true;
    if (pebbling)
      ctx.pebbling = *pebbling;

    // Run all translators (synthesis is cheap; metadata stays complete).
    // Pebbling runs over both gadget families: the default ω (== Proposed) and
    // the phase-exact Existing family, so the QCEC lane verifies both.
    QCGateList gl_cur = XAGToGateList::translate(ctx);
    QCGateList gl_ex = ExistingMethod::translate(ctx);
    QCGateList gl_pr = ProposedMethod::translate(ctx);
    QCGateList gl_pb = PebblingMethod::translate(ctx);
    QCGateList gl_pb_ex = PebblingMethod::translate(ctx, GadgetFamily::Exact);

    Metrics m_cur = computeMetrics(gl_cur);
    Metrics m_ex = computeMetrics(gl_ex);
    Metrics m_pr = computeMetrics(gl_pr);
    Metrics m_pb = computeMetrics(gl_pb);
    Metrics m_pb_ex = computeMetrics(gl_pb_ex);

    bool ok = !gl_cur.gates.empty() && !gl_ex.gates.empty() &&
              !gl_pr.gates.empty() && !gl_pb.gates.empty() &&
              !gl_pb_ex.gates.empty() && constraint_ok;

    auto printRow = [&](const char *m_name, const Metrics &m) {
      char buf[256];
      snprintf(buf, sizeof(buf),
               "  %3d| %3u | %4u | %4u | %10s | %-9s | %5u | %5u | %6u | "
               "%5u\n",
               idx, cfg.pis, cfg.ands, cfg.xors, constraint_ok ? "OK" : "FAIL",
               m_name, m.t_count, m.cnot_count, m.qubits, m.total);
      errs() << buf;
    };
    if (method == "all" || method == "current")
      printRow("Current", m_cur);
    if (method == "all" || method == "existing")
      printRow("Existing", m_ex);
    if (method == "all" || method == "proposed")
      printRow("Proposed", m_pr);
    if (method == "all" || method == "pebbling_proposed")
      printRow("Peb-Prop", m_pb);
    if (method == "all" || method == "pebbling_existing")
      printRow("Peb-Exist", m_pb_ex);

    std::string prefix = dataDir + "/xag_" + std::to_string(idx);
    if (method == "all" || method == "current")
      writeFile(prefix + "_current.json", gl_cur.toJSON());
    if (method == "all" || method == "existing")
      writeFile(prefix + "_existing.json", gl_ex.toJSON());
    if (method == "all" || method == "proposed")
      writeFile(prefix + "_proposed.json", gl_pr.toJSON());
    if (method == "all" || method == "pebbling_proposed")
      writeFile(prefix + "_pebbling_proposed.json", gl_pb.toJSON());
    if (method == "all" || method == "pebbling_existing")
      writeFile(prefix + "_pebbling_existing.json", gl_pb_ex.toJSON());

    // Structure dump: the exact graph + node indices for the SAT-solver side
    // (random XAGs are not reproducible across platforms from params alone).
    writeFile(prefix + "_structure.json", xagStructureToJSON(xag));

    std::string meta =
        "{\"seed\":" + std::to_string(cfg.seed) +
        ",\"config_pis\":" + std::to_string(cfg.pis) +
        ",\"config_ands\":" + std::to_string(cfg.ands) +
        ",\"config_xors\":" + std::to_string(cfg.xors) +
        ",\"num_pis\":" + std::to_string(xag.num_pis()) +
        ",\"num_pos\":" + std::to_string(xag.num_pos()) +
        ",\"num_gates\":" + std::to_string(xag.num_gates()) +
        ",\"truth_table_hex\":\"" + tt_hex + "\"" +
        ",\"constraint_ok\":" + (constraint_ok ? "true" : "false") +
        ",\"output_qubit_current\":" + std::to_string(gl_cur.output_qubit) +
        ",\"output_qubit_existing\":" + std::to_string(gl_ex.output_qubit) +
        ",\"output_qubit_proposed\":" + std::to_string(gl_pr.output_qubit) +
        ",\"output_qubit_pebbling_proposed\":" +
        std::to_string(gl_pb.output_qubit) +
        ",\"output_qubit_pebbling_existing\":" +
        std::to_string(gl_pb_ex.output_qubit) +
        ",\"metrics_current\":" + metricsJSON(m_cur) +
        ",\"metrics_existing\":" + metricsJSON(m_ex) +
        ",\"metrics_proposed\":" + metricsJSON(m_pr) +
        ",\"metrics_pebbling_proposed\":" + metricsJSON(m_pb) +
        ",\"metrics_pebbling_existing\":" + metricsJSON(m_pb_ex) + "}";
    writeFile(prefix + "_meta.json", meta);

    return ok;
  } catch (const std::exception &e) {
    errs() << "  " << idx << " | EXCEPTION: " << e.what()
           << " (pis=" << cfg.pis << " ands=" << cfg.ands
           << " xors=" << cfg.xors << " seed=" << cfg.seed << ")\n";
    return false;
  }
}

bool runOneXag(uint32_t pis, uint32_t ands, uint32_t xors, uint64_t seed,
               int idx, const std::string &dataDir, const std::string &method,
               const PebblingSequence *pebbling) {
  RandomXAGParams params;
  params.num_pis = pis;
  params.num_and_gates = ands;
  params.num_xor_gates = xors;
  params.seed = seed;

  XagCaseConfig cfg{seed, pis, ands, xors};
  try {
    auto xag = generate_random_xag(params);
    return runXagCase(xag, cfg, idx, dataDir, method, pebbling);
  } catch (const std::exception &e) {
    errs() << "  " << idx << " | EXCEPTION: " << e.what() << " (pis=" << pis
           << " ands=" << ands << " xors=" << xors << " seed=" << seed
           << ")\n";
    return false;
  }
}

} // namespace test
} // namespace xagtdep
