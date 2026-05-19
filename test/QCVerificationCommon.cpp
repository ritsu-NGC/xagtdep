// QCVerificationCommon.cpp — Shared per-XAG runner used by both
// QCVerificationTest (full sweep) and CIQCVerification (passing-set subset).

#include "QCVerificationCommon.h"

#include "ExistingMethod.h"
#include "ProposedMethod.h"
#include "QCGateList.h"
#include "RandomXAG.h"
#include "XAGToGateList.h"
#include "XagContext.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdio>
#include <fstream>
#include <kitty/dynamic_truth_table.hpp>
#include <kitty/print.hpp>
#include <mockturtle/algorithms/simulation.hpp>

using namespace llvm;

namespace {

struct Metrics {
  uint32_t t_count = 0;
  uint32_t cnot_count = 0;
  uint32_t h_count = 0;
  uint32_t total = 0;
  uint32_t qubits = 0;
};

Metrics computeMetrics(const xagtdep::QCGateList &gl) {
  Metrics m;
  m.total = gl.gates.size();
  m.qubits = gl.num_qubits;
  for (const auto &g : gl.gates) {
    switch (g.type) {
    case xagtdep::GateType::T:
    case xagtdep::GateType::Tdg:
      m.t_count++;
      break;
    case xagtdep::GateType::CNOT:
      m.cnot_count++;
      break;
    case xagtdep::GateType::H:
      m.h_count++;
      break;
    default:
      break;
    }
  }
  return m;
}

std::string metricsJSON(const Metrics &m) {
  return "{\"t_count\":" + std::to_string(m.t_count) +
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

uint32_t findOutputQubit(const xagtdep::QCGateList &gl) {
  if (gl.gates.empty())
    return 0;
  return gl.gates.back().target;
}

} // namespace

namespace xagtdep {
namespace test {

bool runOneXag(uint32_t pis, uint32_t ands, uint32_t xors, uint64_t seed,
               int idx, const std::string &dataDir,
               const std::string &method) {
  try {
    RandomXAGParams params;
    params.num_pis = pis;
    params.num_and_gates = ands;
    params.num_xor_gates = xors;
    params.seed = seed;

    auto xag = generate_random_xag(params);
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

    // Run all 3 translators (synthesis is cheap; metadata stays complete).
    QCGateList gl_cur = XAGToGateList::translate(ctx);
    QCGateList gl_ex = ExistingMethod::translate(ctx);
    QCGateList gl_pr = ProposedMethod::translate(ctx);

    Metrics m_cur = computeMetrics(gl_cur);
    Metrics m_ex = computeMetrics(gl_ex);
    Metrics m_pr = computeMetrics(gl_pr);

    bool ok = !gl_cur.gates.empty() && !gl_ex.gates.empty() &&
              !gl_pr.gates.empty() && constraint_ok;

    auto printRow = [&](const char *m_name, const Metrics &m) {
      char buf[256];
      snprintf(buf, sizeof(buf),
               "  %3d| %3u | %4u | %4u | %10s | %-9s | %5u | %5u | %6u | "
               "%5u\n",
               idx, pis, ands, xors, constraint_ok ? "OK" : "FAIL", m_name,
               m.t_count, m.cnot_count, m.qubits, m.total);
      errs() << buf;
    };
    if (method == "all" || method == "current")
      printRow("Current", m_cur);
    if (method == "all" || method == "existing")
      printRow("Existing", m_ex);
    if (method == "all" || method == "proposed")
      printRow("Proposed", m_pr);

    std::string prefix = dataDir + "/xag_" + std::to_string(idx);
    if (method == "all" || method == "current")
      writeFile(prefix + "_current.json", gl_cur.toJSON());
    if (method == "all" || method == "existing")
      writeFile(prefix + "_existing.json", gl_ex.toJSON());
    if (method == "all" || method == "proposed")
      writeFile(prefix + "_proposed.json", gl_pr.toJSON());

    std::string meta =
        "{\"seed\":" + std::to_string(seed) +
        ",\"config_pis\":" + std::to_string(pis) +
        ",\"config_ands\":" + std::to_string(ands) +
        ",\"config_xors\":" + std::to_string(xors) +
        ",\"num_pis\":" + std::to_string(xag.num_pis()) +
        ",\"num_pos\":" + std::to_string(xag.num_pos()) +
        ",\"num_gates\":" + std::to_string(xag.num_gates()) +
        ",\"truth_table_hex\":\"" + tt_hex + "\"" +
        ",\"constraint_ok\":" + (constraint_ok ? "true" : "false") +
        ",\"output_qubit_current\":" + std::to_string(findOutputQubit(gl_cur)) +
        ",\"output_qubit_existing\":" + std::to_string(findOutputQubit(gl_ex)) +
        ",\"output_qubit_proposed\":" + std::to_string(findOutputQubit(gl_pr)) +
        ",\"metrics_current\":" + metricsJSON(m_cur) +
        ",\"metrics_existing\":" + metricsJSON(m_ex) +
        ",\"metrics_proposed\":" + metricsJSON(m_pr) + "}";
    writeFile(prefix + "_meta.json", meta);

    return ok;
  } catch (const std::exception &e) {
    errs() << "  " << idx << " | EXCEPTION: " << e.what()
           << " (pis=" << pis << " ands=" << ands << " xors=" << xors
           << " seed=" << seed << ")\n";
    return false;
  }
}

} // namespace test
} // namespace xagtdep
