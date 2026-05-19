// QCVerificationTest.cpp — Generates random XAGs, runs all synthesis methods,
// exports gate lists and truth tables as JSON for Python verification.
//
// Two modes:
//   - Sweep mode (no args): runs 14 configs × 3 seeds = 42 XAGs.
//   - Single-XAG mode (--seed/--pis/--ands/--xors): rerun one XAG for debug.

#include "QCVerificationCommon.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdlib>
#include <cstring>
#include <string>
#include <sys/stat.h>
#include <vector>

using namespace llvm;

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
    bool ok = xagtdep::test::runOneXag(args.pis, args.ands, args.xors, args.seed,
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
        bool ok = xagtdep::test::runOneXag(cfg.pis, cfg.ands, cfg.xors, seed, idx++, dataDir,
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
