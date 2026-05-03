// QCVerificationTest.cpp — Generates random XAGs, runs all synthesis methods,
// exports gate lists and truth tables as JSON for Python verification.
//
// Two modes:
//   - Sweep mode (no args): runs 14 configs × 3 seeds = 42 XAGs.
//   - Single-XAG mode (--seed/--pis/--ands/--xors): rerun one XAG for debug.

#include "ExistingMethod.h"
#include "ProposedMethod.h"
#include "QCGateList.h"
#include "RandomXAG.h"
#include "XAGToGateList.h"
#include "XagContext.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <kitty/dynamic_truth_table.hpp>
#include <kitty/print.hpp>
#include <mockturtle/algorithms/simulation.hpp>
#include <string>
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

static uint32_t findOutputQubit(const QCGateList &gl) {
  if (gl.gates.empty())
    return 0;
  return gl.gates.back().target;
}

// ── CLI parsing ──────────────────────────────────────────────────────────

struct CliArgs {
  bool single_mode = false;
  bool have_seed = false, have_pis = false, have_ands = false, have_xors = false;
  uint64_t seed = 0;
  uint32_t pis = 0, ands = 0, xors = 0;
  std::string method = "all"; // current | existing | proposed | all
  std::string data_dir = "";
  bool verify = false;
  bool help = false;
};

static void printUsage() {
  errs() << "Usage: QCVerificationTest [OPTIONS]\n"
            "  Without flags: runs the full 14-config x 3-seed sweep.\n"
            "  With single-XAG flags: reruns one XAG (all 4 numeric flags "
            "required).\n\n"
            "Options:\n"
            "  --seed <N>      random seed (decimal or 0x... hex)\n"
            "  --pis <N>       number of primary inputs\n"
            "  --ands <N>      number of AND gates\n"
            "  --xors <N>      number of XOR gates\n"
            "  --method <M>    current|existing|proposed|all (default: all)\n"
            "  --data-dir <P>  output directory (default: verification_data\n"
            "                  in sweep mode, verification_data_single in\n"
            "                  single-XAG mode)\n"
            "  --verify        run python test/verify_circuits.py after\n"
            "  -h, --help      print this help\n\n"
            "Example:\n"
            "  QCVerificationTest --seed 42 --pis 4 --ands 1 --xors 2 "
            "--verify\n";
}

static bool parseArgs(int argc, char **argv, CliArgs &out) {
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto need = [&](const char *flag) -> const char * {
      if (i + 1 >= argc) {
        errs() << "Missing value for " << flag << "\n";
        return nullptr;
      }
      return argv[++i];
    };
    if (a == "-h" || a == "--help") {
      out.help = true;
      return true;
    } else if (a == "--seed") {
      auto v = need("--seed");
      if (!v) return false;
      out.seed = std::strtoull(v, nullptr, 0);
      out.have_seed = true;
    } else if (a == "--pis") {
      auto v = need("--pis");
      if (!v) return false;
      out.pis = (uint32_t)std::strtoul(v, nullptr, 0);
      out.have_pis = true;
    } else if (a == "--ands") {
      auto v = need("--ands");
      if (!v) return false;
      out.ands = (uint32_t)std::strtoul(v, nullptr, 0);
      out.have_ands = true;
    } else if (a == "--xors") {
      auto v = need("--xors");
      if (!v) return false;
      out.xors = (uint32_t)std::strtoul(v, nullptr, 0);
      out.have_xors = true;
    } else if (a == "--method") {
      auto v = need("--method");
      if (!v) return false;
      out.method = v;
    } else if (a == "--data-dir") {
      auto v = need("--data-dir");
      if (!v) return false;
      out.data_dir = v;
    } else if (a == "--verify") {
      out.verify = true;
    } else {
      errs() << "Unknown flag: " << a << "\n";
      return false;
    }
  }

  out.single_mode =
      out.have_seed || out.have_pis || out.have_ands || out.have_xors;
  if (out.single_mode &&
      !(out.have_seed && out.have_pis && out.have_ands && out.have_xors)) {
    errs() << "Single-XAG mode requires all of: --seed, --pis, --ands, --xors\n";
    return false;
  }
  if (out.method != "all" && out.method != "current" &&
      out.method != "existing" && out.method != "proposed") {
    errs() << "Invalid --method: " << out.method
           << " (expected: current|existing|proposed|all)\n";
    return false;
  }
  return true;
}

// ── Per-XAG runner ───────────────────────────────────────────────────────
// Generates one XAG, runs all 3 translators, writes JSONs, prints summary.
// Returns true if the XAG passed all sanity checks (gate lists nonempty,
// constraint satisfied).

static bool runOneXag(uint32_t pis, uint32_t ands, uint32_t xors, uint64_t seed,
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

// ── Main ─────────────────────────────────────────────────────────────────

int main(int argc, char **argv) {
  CliArgs args;
  if (!parseArgs(argc, argv, args)) {
    printUsage();
    return 2;
  }
  if (args.help) {
    printUsage();
    return 0;
  }

  std::string dataDir =
      !args.data_dir.empty()
          ? args.data_dir
          : (args.single_mode ? "verification_data_single"
                              : "verification_data");
  mkdir(dataDir.c_str(), 0755);

  errs() << "=== QC Verification Test ===\n";
  errs() << (args.single_mode ? "Single-XAG mode\n\n" : "Full sweep\n\n");
  errs() << "  #  | PIs | ANDs | XORs | Constraint | Method    | T-cnt | "
            "CNOTs | Qubits | Total\n";
  errs() << "  ---|-----|------|------|------------|-----------|-------|"
            "-------|--------|------\n";

  int passed = 0, failed = 0, idx = 0;

  if (args.single_mode) {
    bool ok = runOneXag(args.pis, args.ands, args.xors, args.seed,
                        /*idx=*/0, dataDir, args.method);
    (ok ? passed : failed)++;
    idx = 1;

    errs() << "\nJSONs written to " << dataDir << "/xag_0_*.json\n";
    errs() << "Verify with: python3 test/verify_circuits.py " << dataDir << "\n";

    if (args.verify) {
      std::string cmd = "python3 ../test/verify_circuits.py " + dataDir;
      errs() << "\n--- Running: " << cmd << " ---\n";
      int rc = std::system(cmd.c_str());
      if (rc != 0)
        failed++;
    }
  } else {
    // Full sweep mode.
    struct Config {
      uint32_t pis, ands, xors;
    };
    std::vector<Config> configs = {
        {3, 1, 1}, {3, 1, 2}, {3, 2, 1}, {3, 2, 2},
        {4, 1, 1}, {4, 1, 2}, {4, 2, 1}, {4, 2, 2},
        {5, 1, 1}, {5, 1, 2}, {5, 2, 2},
        {6, 1, 1}, {6, 1, 2}, {6, 2, 2},
    };
    const uint64_t seeds[] = {42, 12345, 0xdeadbeef};

    for (const auto &cfg : configs) {
      for (uint64_t seed : seeds) {
        bool ok = runOneXag(cfg.pis, cfg.ands, cfg.xors, seed, idx++, dataDir,
                            "all");
        (ok ? passed : failed)++;
      }
    }
  }

  errs() << "\n=== Summary: " << passed << " passed, " << failed
         << " failed out of " << idx << " XAGs ===\n";
  errs() << "JSON files written to " << dataDir << "/\n";

  return failed > 0 ? 1 : 0;
}
