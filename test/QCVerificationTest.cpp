// QCVerificationTest.cpp — Generates random XAGs, runs all synthesis methods,
// exports gate lists and truth tables as JSON for Python verification.

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
#include <sys/stat.h>
#include <vector>

using namespace llvm;
using namespace xagtdep;

// ── Helpers ──────────────────────────────────────────────────────────────

struct Metrics {
  uint32_t t_count = 0;
  uint32_t cnot_count = 0;
  uint32_t h_count = 0;
  uint32_t total = 0;
  uint32_t qubits = 0;
};

static Metrics computeMetrics(const QCGateList &gl) {
  Metrics m;
  m.total = gl.gates.size();
  m.qubits = gl.num_qubits;
  for (const auto &g : gl.gates) {
    switch (g.type) {
    case GateType::T:
    case GateType::Tdg:
      m.t_count++;
      break;
    case GateType::CNOT:
      m.cnot_count++;
      break;
    case GateType::H:
      m.h_count++;
      break;
    default:
      break;
    }
  }
  return m;
}

static std::string metricsJSON(const Metrics &m) {
  return "{\"t_count\":" + std::to_string(m.t_count) +
         ",\"cnot_count\":" + std::to_string(m.cnot_count) +
         ",\"h_count\":" + std::to_string(m.h_count) +
         ",\"total\":" + std::to_string(m.total) +
         ",\"qubits\":" + std::to_string(m.qubits) + "}";
}

static bool writeFile(const std::string &path, const std::string &content) {
  std::ofstream f(path);
  if (!f.is_open())
    return false;
  f << content;
  return f.good();
}

// Find the output qubit for a method: it's the qubit mapped to the PO node.
static uint32_t findOutputQubit(const QCGateList &gl) {
  // For our translators, the last CNOT's target is typically the output qubit.
  // More reliable: the output qubit is num_pis (for XAGToGateList which puts
  // AND result on an ancilla then XORs), or we look for the pattern.
  // Conservative: return num_qubits - 1 for proposed (last allocated),
  // or trace through gates. For now, use a heuristic.
  //
  // All our translators allocate PIs first (0..num_pis-1), then ancillas.
  // For simple XAGs with one PO, the output is on one of the ancilla qubits.
  // The last gate's target is usually the output qubit.
  if (gl.gates.empty())
    return 0;
  return gl.gates.back().target;
}

// ── Main ─────────────────────────────────────────────────────────────────

int main() {
  const std::string dataDir = "verification_data";
  mkdir(dataDir.c_str(), 0755);

  // Test configurations: {num_pis, num_and, num_xor}
  struct Config {
    uint32_t pis, ands, xors;
  };
  std::vector<Config> configs = {
      {3, 1, 1}, {3, 1, 2}, {3, 2, 1}, {3, 2, 2},
      {4, 1, 1}, {4, 1, 2}, {4, 2, 1}, {4, 2, 2},
      {5, 1, 1}, {5, 1, 2}, {5, 2, 2},
      {6, 1, 1}, {6, 1, 2}, {6, 2, 2},
  };

  // Run 3 seeds per config to get variety.
  const uint64_t seeds[] = {42, 12345, 0xdeadbeef};

  int idx = 0;
  int passed = 0;
  int failed = 0;

  errs() << "=== QC Verification Test ===\n";
  errs() << "Generating random XAGs and exporting gate lists...\n\n";

  // Print header.
  errs() << "  #  | PIs | ANDs | XORs | Constraint | Method    | T-cnt | "
            "CNOTs | Qubits | Total\n";
  errs() << "  ---|-----|------|------|------------|-----------|-------|"
            "-------|--------|------\n";

  for (const auto &cfg : configs) {
    for (uint64_t seed : seeds) {
      try {

      RandomXAGParams params;
      params.num_pis = cfg.pis;
      params.num_and_gates = cfg.ands;
      params.num_xor_gates = cfg.xors;
      params.seed = seed;

      auto xag = generate_random_xag(params);
      bool constraint_ok = validate_and_constraint(xag);

      // Simulate truth table using simulate_nodes.
      mockturtle::default_simulator<kitty::dynamic_truth_table> sim(
          xag.num_pis());
      auto node_values =
          mockturtle::simulate_nodes<kitty::dynamic_truth_table>(xag, sim);

      // Get the truth table for the first PO.
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

      // Build XagContext.
      XagContext ctx;
      ctx.xag = xag;
      ctx.optimized = true; // ExistingMethod/ProposedMethod don't need Meuli

      // Run all 3 methods.
      QCGateList gl_cur, gl_ex, gl_pr;
      gl_cur = XAGToGateList::translate(ctx);
      gl_ex = ExistingMethod::translate(ctx);
      gl_pr = ProposedMethod::translate(ctx);

      Metrics m_cur = computeMetrics(gl_cur);
      Metrics m_ex = computeMetrics(gl_ex);
      Metrics m_pr = computeMetrics(gl_pr);

      bool ok = !gl_cur.gates.empty() && !gl_ex.gates.empty() &&
                !gl_pr.gates.empty() && constraint_ok;

      // Print summary rows.
      auto printRow = [&](const char *method, const Metrics &m) {
        char buf[256];
        snprintf(buf, sizeof(buf),
                 "  %3d| %3u | %4u | %4u | %10s | %-9s | %5u | %5u | %6u | "
                 "%5u\n",
                 idx, cfg.pis, cfg.ands, cfg.xors,
                 constraint_ok ? "OK" : "FAIL", method, m.t_count,
                 m.cnot_count, m.qubits, m.total);
        errs() << buf;
      };
      printRow("Current", m_cur);
      printRow("Existing", m_ex);
      printRow("Proposed", m_pr);

      // Write JSON files.
      std::string prefix = dataDir + "/xag_" + std::to_string(idx);
      writeFile(prefix + "_current.json", gl_cur.toJSON());
      writeFile(prefix + "_existing.json", gl_ex.toJSON());
      writeFile(prefix + "_proposed.json", gl_pr.toJSON());

      // Write metadata.
      std::string meta = "{\"num_pis\":" + std::to_string(xag.num_pis()) +
                         ",\"num_pos\":" + std::to_string(xag.num_pos()) +
                         ",\"num_gates\":" + std::to_string(xag.num_gates()) +
                         ",\"truth_table_hex\":\"" + tt_hex + "\"" +
                         ",\"constraint_ok\":" +
                         (constraint_ok ? "true" : "false") +
                         ",\"output_qubit_current\":" +
                         std::to_string(findOutputQubit(gl_cur)) +
                         ",\"output_qubit_existing\":" +
                         std::to_string(findOutputQubit(gl_ex)) +
                         ",\"output_qubit_proposed\":" +
                         std::to_string(findOutputQubit(gl_pr)) +
                         ",\"metrics_current\":" + metricsJSON(m_cur) +
                         ",\"metrics_existing\":" + metricsJSON(m_ex) +
                         ",\"metrics_proposed\":" + metricsJSON(m_pr) + "}";
      writeFile(prefix + "_meta.json", meta);

      if (ok)
        passed++;
      else
        failed++;
      idx++;

      } catch (const std::exception &e) {
        errs() << "  " << idx << " | EXCEPTION: " << e.what()
               << " (pis=" << cfg.pis << " ands=" << cfg.ands
               << " xors=" << cfg.xors << ")\n";
        failed++;
        idx++;
      }
    }
  }

  errs() << "\n=== Summary: " << passed << " passed, " << failed
         << " failed out of " << idx << " XAGs ===\n";
  errs() << "JSON files written to " << dataDir << "/\n";

  return failed > 0 ? 1 : 0;
}
