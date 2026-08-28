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

#include "AIGReader.h"
#include "AIGToQC.h"
#include "ExistingMethod.h"
#include "ProposedMethod.h"
#include "QCGateList.h"
#include "XAGToGateList.h"
#include "XagContext.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

using namespace xagtdep;

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

// Returns false on any failure and populates the result struct.
static bool runOne(const Circuit &c, Result &result) {
  result.name = c.name;
  result.passed = true;
  result.synth_skipped = false;
  result.num_qubits_current = 0;
  result.t_count_current = 0;
  result.num_qubits_existing = 0;
  result.t_count_existing = 0;
  result.num_qubits_proposed = 0;
  result.t_count_proposed = 0;

  // Step 1 & 2: parse + structural checks.
  AIG aig;
  try {
    aig = AIGReader::readFromFile(c.path);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": PARSE ERROR — "
                 << e.what() << "\n";
    result.passed = false;
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
    return false;
  }

  result.passed &= (xag.num_pis() == aig.num_inputs);
  result.passed &= (xag.num_pos() > 0);
  result.passed &= (xag.num_gates() > 0);

  // Step 4: QC synthesis (skipped for very large circuits).
  if (!skipSynth(c.name)) {
    try {
      // Create XagContext for synthesis
      XagContext ctx;
      ctx.xag = xag;
      ctx.optimized = true;

      // Current method (XAGToGateList)
      {
        QCGateList gl_current = XAGToGateList::translate(ctx);
        result.num_qubits_current = gl_current.num_qubits;
        result.t_count_current = countTgates(gl_current);
        result.passed &= (gl_current.num_pis > 0 && !gl_current.gates.empty());
        gl_current.gates.clear();  // Explicit memory cleanup
      }

      // Existing method
      {
        QCGateList gl_existing = ExistingMethod::translate(ctx);
        result.num_qubits_existing = gl_existing.num_qubits;
        result.t_count_existing = countTgates(gl_existing);
        result.passed &= (gl_existing.num_pis > 0 && !gl_existing.gates.empty());
        gl_existing.gates.clear();  // Explicit memory cleanup
      }

      // Proposed method
      {
        QCGateList gl_proposed = ProposedMethod::translate(ctx);
        result.num_qubits_proposed = gl_proposed.num_qubits;
        result.t_count_proposed = countTgates(gl_proposed);
        result.passed &= (gl_proposed.num_pis > 0 && !gl_proposed.gates.empty());
        gl_proposed.gates.clear();  // Explicit memory cleanup
      }

    } catch (const std::exception &e) {
      llvm::errs() << "[EPFLBenchmark] " << c.name << ": SYNTH ERROR — "
                   << e.what() << "\n";
      result.passed = false;
      return false;
    }
  } else {
    result.synth_skipped = true;
  }

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
      << "synth_skipped,passed\n";

  // Data rows
  for (const auto &r : results) {
    out << r.name << "," << r.num_inputs << "," << r.num_outputs << ","
        << r.num_ands << "," << r.num_qubits_current << "," << r.t_count_current
        << "," << r.num_qubits_existing << "," << r.t_count_existing << ","
        << r.num_qubits_proposed << "," << r.t_count_proposed << ","
        << (r.synth_skipped ? "1" : "0") << "," << (r.passed ? "1" : "0") << "\n";
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

    if (r.synth_skipped) {
      out << "— & — & — & — & — & — & — & — & \\textit{skipped}";
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

  llvm::errs() << "[EPFLBenchmark] Overall: " << (all_pass ? "PASSED" : "FAILED")
               << "\n";
  return all_pass ? 0 : 1;
}
