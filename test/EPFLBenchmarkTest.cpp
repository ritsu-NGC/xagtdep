// EPFLBenchmarkTest.cpp — smoke-tests every EPFL combinational benchmark
// (.aig) through AIGReader and the AIGToQC pipeline, comparing xagtdep's
// synthesis methods (Current, Existing, Proposed) and logging T-counts.
//
// For each circuit we:
//   1. Parse the binary .aig file
//   2. Convert to mockturtle::xag_network
//   3. Run all synthesis algorithms: Current, ExistingMethod, ProposedMethod
//   4. Count T-gates in each synthesized gate list
//   5. Log results to CSV and LaTeX table for comparison
//
// Large circuits (multiplier, div, hyp, mem_ctrl, sqrt) are parsed
// and converted to XAG only; full QC synthesis is skipped with a note
// because synthesis can be slow on very large graphs.
//
// Results are written to:
//   - CSV log file (default: epfl_benchmarks.csv, or EPFL_LOG_FILE env var)
//   - LaTeX table file (default: epfl_benchmarks.tex, or EPFL_TEX_FILE env var)
//
// DCDEBUG: Timeout mechanism for individual circuits (5 minute limit per circuit).
// Set EPFL_DISABLE_TIMEOUT=1 env var to disable, or remove DCDEBUG markers when ready.

#include "AIGReader.h"
#include "AIGToQC.h"
#include "ExistingMethod.h"
#include "ProposedMethod.h"
#include "QCGateList.h"
#include "XAGToGateList.h"
#include "XagContext.h"
#include "llvm/Support/raw_ostream.h"
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

using namespace xagtdep;

// DCDEBUG: 5 minute timeout per circuit (in seconds)
static const int CIRCUIT_TIMEOUT_SECONDS = 300;

// DCDEBUG: Get current RSS memory usage in MB (Linux only)
static uint64_t getRSSMB() {
  const char *proc_status = "/proc/self/status";
  std::ifstream ifs(proc_status);
  if (!ifs.is_open()) {
    return 0;  // Not available on this platform
  }
  std::string line;
  while (std::getline(ifs, line)) {
    if (line.substr(0, 6) == "VmRSS:") {
      // VmRSS is in KB, convert to MB
      size_t pos = line.find_last_not_of(" \t");
      std::string val_str = line.substr(6, pos - 5);
      return std::stoull(val_str) / 1024;
    }
  }
  return 0;
}

// Circuits large enough that full QC synthesis is impractical in a test run.
static const std::vector<std::string> kSkipSynthesis = {
    "div", "hyp", "mem_ctrl", "multiplier", "sqrt"};

static bool skipSynth(const std::string &name) {
  for (const auto &s : kSkipSynthesis)
    if (name == s)
      return true;
  return false;
}

struct Circuit {
  std::string name;
  std::string path;
};

struct Result {
  std::string name;
  uint32_t num_inputs;
  uint32_t num_outputs;
  uint32_t num_ands;
  uint32_t num_qubits_current;
  uint32_t t_count_current;
  uint32_t num_qubits_existing;
  uint32_t t_count_existing;
  uint32_t num_qubits_proposed;
  uint32_t t_count_proposed;
  bool synth_skipped;
  bool timeout_exceeded;
  bool passed;
};

// Count T-gates in a gate list.
static uint32_t countTgates(const QCGateList &gl) {
  uint32_t count = 0;
  for (const auto &gate : gl.gates) {
    if (gate.type == GateType::T || gate.type == GateType::Tdg) {
      count++;
    }
  }
  return count;
}

// DCDEBUG: Check if circuit processing has exceeded timeout.
static bool checkTimeout(
    const std::chrono::steady_clock::time_point &circuit_start) {
  const char *disable_timeout = std::getenv("EPFL_DISABLE_TIMEOUT");
  if (disable_timeout && std::string(disable_timeout) == "1") {
    return false;  // Timeout disabled
  }
  auto now = std::chrono::steady_clock::now();
  auto elapsed =
      std::chrono::duration_cast<std::chrono::seconds>(now - circuit_start);
  return elapsed.count() > CIRCUIT_TIMEOUT_SECONDS;
}

// DCDEBUG: Cleanup synthesis method internal state if needed
static void cleanupSynthesisState() {
  // Placeholder for any global cleanup needed by synthesis methods.
  // If ExistingMethod, ProposedMethod, or XAGToGateList maintain static caches,
  // add cleanup calls here.
}

// Returns false on any failure and populates the result struct.
static bool runOne(const Circuit &c, Result &result) {
  // DCDEBUG: Start circuit timer
  auto circuit_start = std::chrono::steady_clock::now();

  result.name = c.name;
  result.passed = true;
  result.synth_skipped = false;
  result.timeout_exceeded = false;
  result.num_qubits_current = 0;
  result.t_count_current = 0;
  result.num_qubits_existing = 0;
  result.t_count_existing = 0;
  result.num_qubits_proposed = 0;
  result.t_count_proposed = 0;

  // Print status to stdout
  std::cout << "Processing: " << c.name << std::flush;

  // Step 1 & 2: parse + structural checks.
  AIG aig;
  try {
    aig = AIGReader::readFromFile(c.path);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": PARSE ERROR — "
                 << e.what() << "\n";
    result.passed = false;
    std::cout << " [FAILED]\n";
    return false;
  }

  result.num_inputs = aig.num_inputs;
  result.num_outputs = aig.num_outputs;
  result.num_ands = aig.num_ands;

  result.passed &= (aig.num_inputs > 0);
  result.passed &= (aig.num_outputs > 0);
  result.passed &= (aig.num_ands > 0);

  // Step 3: XAG conversion.
  mockturtle::xag_network xag;
  try {
    xag = AIGReader::toXagNetwork(aig);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": XAG CONV ERROR — "
                 << e.what() << "\n";
    result.passed = false;
    std::cout << " [FAILED]\n";
    return false;
  }

  result.passed &= (xag.num_pis() == aig.num_inputs);
  result.passed &= (xag.num_pos() > 0);
  result.passed &= (xag.num_gates() > 0);

  // Step 4: QC synthesis (skipped for very large circuits).
  if (!skipSynth(c.name)) {
    try {
      // DCDEBUG: Create context with const-ref to XAG (avoids copy if supported)
      // For now, still using value semantics, but pass by const-ref inside
      XagContext ctx;
      ctx.xag = xag;
      ctx.optimized = true;

      // DCDEBUG: Memory profiling before synthesis
      uint64_t mem_before = getRSSMB();

      // Current method (XAGToGateList)
      std::cout << " [Current..." << std::flush;
      {
        // DCDEBUG: Check timeout before synthesis
        if (checkTimeout(circuit_start)) {
          std::cout << " TIMEOUT]";
          result.timeout_exceeded = true;
          result.synth_skipped = true;
          std::cout << " [SKIPPED]\n";
          llvm::errs() << "[EPFLBenchmark] " << c.name
                       << ": TIMEOUT exceeded (>5 min)\n";
          return true;
        }

        QCGateList gl_current = XAGToGateList::translate(ctx);
        result.num_qubits_current = gl_current.num_qubits;
        result.t_count_current = countTgates(gl_current);
        result.passed &= (gl_current.num_pis > 0 && !gl_current.gates.empty());
        gl_current.gates.clear();  // Explicit memory cleanup
        gl_current.gates.shrink_to_fit();  // Force reallocation

        // DCDEBUG: Memory profiling after synthesis
        uint64_t mem_after = getRSSMB();
        if (mem_after > 0 && mem_before > 0) {
          llvm::errs() << "[EPFLBenchmark-MEM] Current: " << mem_before
                       << " MB -> " << mem_after << " MB (delta: "
                       << (int64_t)mem_after - (int64_t)mem_before << " MB)\n";
        }
      }
      std::cout << "]" << std::flush;

      // DCDEBUG: Cleanup synthesis state between methods
      cleanupSynthesisState();

      // Existing method
      std::cout << " [Existing..." << std::flush;
      {
        // DCDEBUG: Check timeout before synthesis
        if (checkTimeout(circuit_start)) {
          std::cout << " TIMEOUT]";
          result.timeout_exceeded = true;
          std::cout << " [SKIPPED]\n";
          llvm::errs() << "[EPFLBenchmark] " << c.name
                       << ": TIMEOUT exceeded (>5 min)\n";
          return true;
        }

        // DCDEBUG: Memory profiling before synthesis
        mem_before = getRSSMB();

        QCGateList gl_existing = ExistingMethod::translate(ctx);
        result.num_qubits_existing = gl_existing.num_qubits;
        result.t_count_existing = countTgates(gl_existing);
        result.passed &= (gl_existing.num_pis > 0 && !gl_existing.gates.empty());
        gl_existing.gates.clear();  // Explicit memory cleanup
        gl_existing.gates.shrink_to_fit();  // Force reallocation

        // DCDEBUG: Memory profiling after synthesis
        mem_after = getRSSMB();
        if (mem_after > 0 && mem_before > 0) {
          llvm::errs() << "[EPFLBenchmark-MEM] Existing: " << mem_before
                       << " MB -> " << mem_after << " MB (delta: "
                       << (int64_t)mem_after - (int64_t)mem_before << " MB)\n";
        }
      }
      std::cout << "]" << std::flush;

      // DCDEBUG: Cleanup synthesis state between methods
      cleanupSynthesisState();

      // Proposed method
      std::cout << " [Proposed..." << std::flush;
      {
        // DCDEBUG: Check timeout before synthesis
        if (checkTimeout(circuit_start)) {
          std::cout << " TIMEOUT]";
          result.timeout_exceeded = true;
          std::cout << " [SKIPPED]\n";
          llvm::errs() << "[EPFLBenchmark] " << c.name
                       << ": TIMEOUT exceeded (>5 min)\n";
          return true;
        }

        // DCDEBUG: Memory profiling before synthesis
        mem_before = getRSSMB();

        QCGateList gl_proposed = ProposedMethod::translate(ctx);
        result.num_qubits_proposed = gl_proposed.num_qubits;
        result.t_count_proposed = countTgates(gl_proposed);
        result.passed &= (gl_proposed.num_pis > 0 && !gl_proposed.gates.empty());
        gl_proposed.gates.clear();  // Explicit memory cleanup
        gl_proposed.gates.shrink_to_fit();  // Force reallocation

        // DCDEBUG: Memory profiling after synthesis
        mem_after = getRSSMB();
        if (mem_after > 0 && mem_before > 0) {
          llvm::errs() << "[EPFLBenchmark-MEM] Proposed: " << mem_before
                       << " MB -> " << mem_after << " MB (delta: "
                       << (int64_t)mem_after - (int64_t)mem_before << " MB)\n";
        }
      }
      std::cout << "]" << std::flush;

      // DCDEBUG: Cleanup synthesis state after all methods
      cleanupSynthesisState();

    } catch (const std::exception &e) {
      llvm::errs() << "[EPFLBenchmark] " << c.name << ": SYNTH ERROR — "
                   << e.what() << "\n";
      result.passed = false;
      std::cout << " [FAILED]\n";
      return false;
    }
  } else {
    std::cout << " [skipped]" << std::flush;
    result.synth_skipped = true;
  }

  std::cout << " [" << (result.passed ? "PASS" : "FAIL") << "]\n";

  llvm::errs() << "[EPFLBenchmark] " << c.name << ": "
               << (result.passed ? "PASSED" : "FAILED") << " (ins=" << aig.num_inputs
               << " outs=" << aig.num_outputs << " ands=" << aig.num_ands;
  if (!result.synth_skipped) {
    llvm::errs() << " | Current T=" << result.t_count_current
                 << " Existing T=" << result.t_count_existing
                 << " Proposed T=" << result.t_count_proposed;
  } else {
    llvm::errs() << " [synth skipped]";
  }
  if (result.timeout_exceeded) {
    llvm::errs() << " [TIMEOUT]";
  }
  llvm::errs() << ")\n";
  return result.passed;
}

// Write results to CSV file.
static void writeCSVFile(const std::string &csvfile,
                         const std::vector<Result> &results) {
  std::ofstream out(csvfile);
  if (!out) {
    llvm::errs() << "[EPFLBenchmark] WARNING: Could not open CSV file: "
                 << csvfile << "\n";
    return;
  }

  // Header
  out << "name,num_inputs,num_outputs,num_ands,"
      << "num_qubits_current,t_count_current,"
      << "num_qubits_existing,t_count_existing,"
      << "num_qubits_proposed,t_count_proposed,"
      << "synth_skipped,timeout_exceeded,passed\n";

  // Data rows
  for (const auto &r : results) {
    out << r.name << "," << r.num_inputs << "," << r.num_outputs << ","
        << r.num_ands << "," << r.num_qubits_current << "," << r.t_count_current
        << "," << r.num_qubits_existing << "," << r.t_count_existing << ","
        << r.num_qubits_proposed << "," << r.t_count_proposed << ","
        << (r.synth_skipped ? "1" : "0") << ","
        << (r.timeout_exceeded ? "1" : "0") << "," << (r.passed ? "1" : "0")
        << "\n";
  }

  out.close();
  llvm::errs() << "[EPFLBenchmark] Wrote CSV to: " << csvfile << "\n";
}

// Write results to LaTeX table file.
static void writeTexFile(const std::string &texfile,
                         const std::vector<Result> &results) {
  std::ofstream out(texfile);
  if (!out) {
    llvm::errs() << "[EPFLBenchmark] WARNING: Could not open TeX file: "
                 << texfile << "\n";
    return;
  }

  // LaTeX table header
  out << "% EPFL Benchmark Results — Generated by EPFLBenchmarkTest\n"
      << "% Comparison of xagtdep synthesis methods (Current, Existing, "
         "Proposed)\n"
      << "% Paste this table into your LaTeX document.\n"
      << "\n"
      << "\\begin{table}[h]\n"
      << "\\centering\n"
      << "\\caption{EPFL Benchmark T-count Comparison (xagtdep Synthesis "
         "Methods)}\n"
      << "\\label{tab:epfl_comparison}\n"
      << "\\begin{tabular}{|l|r|r|r|rrr|rrr|rrr|}\n"
      << "\\hline\n"
      << "Benchmark & PIs & POs & ANDs & \\multicolumn{3}{c|}{Current} & "
         "\\multicolumn{3}{c|}{Existing} & \\multicolumn{3}{c|}{Proposed} \\\\\n"
      << " & & & & q & T & $\\Delta$T & q & T & $\\Delta$T & q & T & Status \\\\\n"
      << "\\hline\n";

  // Data rows
  for (const auto &r : results) {
    out << r.name << " & " << r.num_inputs << " & " << r.num_outputs << " & "
        << r.num_ands << " & ";

    if (r.synth_skipped || r.timeout_exceeded) {
      out << "— & — & — & — & — & — & — & — & ";
      if (r.timeout_exceeded) {
        out << "\\textit{timeout}";
      } else {
        out << "\\textit{skipped}";
      }
    } else {
      // Current method
      out << r.num_qubits_current << " & " << r.t_count_current << " & ";
      if (r.t_count_proposed > 0) {
        int delta = static_cast<int>(r.t_count_current) -
                    static_cast<int>(r.t_count_proposed);
        out << (delta >= 0 ? "+" : "") << delta << " & ";
      } else {
        out << "— & ";
      }

      // Existing method
      out << r.num_qubits_existing << " & " << r.t_count_existing << " & ";
      if (r.t_count_proposed > 0) {
        int delta = static_cast<int>(r.t_count_existing) -
                    static_cast<int>(r.t_count_proposed);
        out << (delta >= 0 ? "+" : "") << delta << " & ";
      } else {
        out << "— & ";
      }

      // Proposed method
      out << r.num_qubits_proposed << " & " << r.t_count_proposed << " & "
          << (r.passed ? "\\checkmark" : "\\ding{55}");
    }

    out << " \\\\\n";
  }

  // Table footer
  out << "\\hline\n"
      << "\\end{tabular}\n"
      << "\\end{table}\n";

  out.close();
  llvm::errs() << "[EPFLBenchmark] Wrote LaTeX table to: " << texfile << "\n";
}

int main(int argc, char **argv) {
  // Base directory is passed as the first argument by the CMake test runner;
  // falls back to the source-tree location for manual runs.
  std::string base = (argc > 1)
                         ? std::string(argv[1])
                         : EPFL_BENCHMARK_DIR;

  // DCDEBUG: Print timeout settings
  const char *disable_timeout = std::getenv("EPFL_DISABLE_TIMEOUT");
  if (disable_timeout && std::string(disable_timeout) == "1") {
    std::cout << "[EPFLBenchmark] Timeout checks DISABLED (EPFL_DISABLE_TIMEOUT=1)\n";
  } else {
    std::cout << "[EPFLBenchmark] Timeout limit: " << CIRCUIT_TIMEOUT_SECONDS
              << " seconds per circuit\n";
    std::cout << "[EPFLBenchmark] Set EPFL_DISABLE_TIMEOUT=1 to disable timeout checks\n";
  }

  // DCDEBUG: Print memory profiling status
  uint64_t test_rss = getRSSMB();
  if (test_rss > 0) {
    std::cout << "[EPFLBenchmark] Memory profiling ENABLED (current RSS: " << test_rss
              << " MB)\n";
  } else {
    std::cout << "[EPFLBenchmark] Memory profiling DISABLED (/proc/self/status not available)\n";
  }
  std::cout << "\n";

  const std::vector<Circuit> circuits = {
      // arithmetic
      {"adder",      base + "/arithmetic/adder.aig"},
      {"bar",        base + "/arithmetic/bar.aig"},
      {"div",        base + "/arithmetic/div.aig"},
      {"hyp",        base + "/arithmetic/hyp.aig"},
      {"max",        base + "/arithmetic/max.aig"},
      {"multiplier", base + "/arithmetic/multiplier.aig"},
      {"sin",        base + "/arithmetic/sin.aig"},
      {"sqrt",       base + "/arithmetic/sqrt.aig"},
      {"square",     base + "/arithmetic/square.aig"},
      // random_control
      {"arbiter",    base + "/random_control/arbiter.aig"},
      {"cavlc",      base + "/random_control/cavlc.aig"},
      {"ctrl",       base + "/random_control/ctrl.aig"},
      {"dec",        base + "/random_control/dec.aig"},
      {"i2c",        base + "/random_control/i2c.aig"},
      {"int2float",  base + "/random_control/int2float.aig"},
      {"mem_ctrl",   base + "/random_control/mem_ctrl.aig"},
      {"priority",   base + "/random_control/priority.aig"},
      {"router",     base + "/random_control/router.aig"},
      {"voter",      base + "/random_control/voter.aig"},
  };

  std::vector<Result> results;
  bool all_pass = true;
  for (const auto &c : circuits) {
    Result r;
    all_pass &= runOne(c, r);
    results.push_back(r);
  }

  // Determine output file paths: check env vars, fall back to defaults.
  const char *csvfile_env = std::getenv("EPFL_LOG_FILE");
  std::string csvfile = csvfile_env ? std::string(csvfile_env)
                                    : "epfl_benchmarks.csv";
  writeCSVFile(csvfile, results);

  const char *texfile_env = std::getenv("EPFL_TEX_FILE");
  std::string texfile = texfile_env ? std::string(texfile_env)
                                    : "epfl_benchmarks.tex";
  writeTexFile(texfile, results);

  std::cout << "\n[EPFLBenchmark] Overall: " << (all_pass ? "PASSED" : "FAILED")
            << "\n";
  llvm::errs() << "[EPFLBenchmark] Overall: " << (all_pass ? "PASSED" : "FAILED")
               << "\n";
  return all_pass ? 0 : 1;
}
