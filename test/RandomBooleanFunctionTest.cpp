// RandomBooleanFunctionTest.cpp - Generates random Boolean functions, compares
// Bennett pebbling against the caterpillar-backed proposed path, and writes
// CSV/LaTeX summaries.

#include "BennettSequence.h"
#include "PebblingMethod.h"
#include "QCGateList.h"
#include "RandomXAG.h"
#include "XagContext.h"
#include "llvm/Support/raw_ostream.h"
#include <caterpillar/details/utils.hpp>
#include <caterpillar/structures/stg_gate.hpp>
#include <caterpillar/synthesis/lhrs.hpp>
#include <caterpillar/synthesis/strategies/xag_mapping_strategy.hpp>
#include <tweedledum/gates/gate_set.hpp>
#include <tweedledum/networks/netlist.hpp>
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <map>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <vector>

using namespace llvm;
using namespace xagtdep;

namespace {

struct CliArgs {
  bool help = false;
  bool have_qubit_count = false;
  uint32_t qubit_count = 5;
  uint32_t num_functions = 10;
  uint64_t seed = 0xcafeaffeULL;
  std::string output_csv = "random_bool_test.csv";
  std::string output_latex = "random_bool_test.tex";
  std::string circuits_dir = "random_bool_circuits";
};

struct ResultRow {
  uint32_t qubit_count = 0;
  uint64_t seed = 0;
  uint32_t function_id = 0;
  uint32_t bennett_t_count = 0;
  uint32_t caterpillar_t_count = 0;
  int difference = 0;
  std::string notes;
  bool passed = false;
};

struct SummaryStats {
  uint32_t count = 0;
  uint64_t bennett_sum = 0;
  uint64_t caterpillar_sum = 0;
  int64_t diff_sum = 0;
  uint32_t bennett_min = std::numeric_limits<uint32_t>::max();
  uint32_t bennett_max = 0;
  uint32_t caterpillar_min = std::numeric_limits<uint32_t>::max();
  uint32_t caterpillar_max = 0;
  int diff_min = std::numeric_limits<int>::max();
  int diff_max = std::numeric_limits<int>::min();
};

static void printUsage() {
  errs() << "Usage: RandomBooleanFunctionTest [OPTIONS]\n"
            "  Without --qubit-count: tests qubit counts 5 through 8.\n"
            "  With --qubit-count <N>: tests only that qubit count.\n\n"
            "Options:\n"
            "  --qubit-count <N>   number of input qubits / PIs (range: 5-12)\n"
            "  --num-functions <N> random functions per qubit count (default: 10)\n"
            "  --seed <N>          base seed for reproducibility (default: 0xcafeaffe)\n"
            "  --output-csv <P>    CSV output path (default: random_bool_test.csv)\n"
            "  --output-latex <P>  LaTeX output path (default: random_bool_test.tex)\n"
            "  --circuits-dir <P>  directory for per-function circuit JSON files "
            "(default: random_bool_circuits)\n"
            "  -h, --help          print this help\n";
}

static bool parseUnsigned(const char *text, uint64_t &out) {
  char *end = nullptr;
  out = std::strtoull(text, &end, 0);
  return end && *end == '\0';
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
    } else if (a == "--qubit-count") {
      auto v = need("--qubit-count");
      uint64_t parsed = 0;
      if (!v || !parseUnsigned(v, parsed)) {
        errs() << "Invalid value for --qubit-count\n";
        return false;
      }
      out.qubit_count = static_cast<uint32_t>(parsed);
      out.have_qubit_count = true;
    } else if (a == "--num-functions") {
      auto v = need("--num-functions");
      uint64_t parsed = 0;
      if (!v || !parseUnsigned(v, parsed)) {
        errs() << "Invalid value for --num-functions\n";
        return false;
      }
      out.num_functions = static_cast<uint32_t>(parsed);
    } else if (a == "--seed") {
      auto v = need("--seed");
      if (!v || !parseUnsigned(v, out.seed)) {
        errs() << "Invalid value for --seed\n";
        return false;
      }
    } else if (a == "--output-csv") {
      auto v = need("--output-csv");
      if (!v)
        return false;
      out.output_csv = v;
    } else if (a == "--output-latex") {
      auto v = need("--output-latex");
      if (!v)
        return false;
      out.output_latex = v;
    } else if (a == "--circuits-dir") {
      auto v = need("--circuits-dir");
      if (!v)
        return false;
      out.circuits_dir = v;
    } else {
      errs() << "Unknown flag: " << a << "\n";
      return false;
    }
  }

  if (out.have_qubit_count && (out.qubit_count < 5 || out.qubit_count > 12)) {
    errs() << "--qubit-count must be in the range [5, 12]\n";
    return false;
  }
  if (out.num_functions == 0) {
    errs() << "--num-functions must be greater than zero\n";
    return false;
  }

  return true;
}

static uint32_t countTgates(const QCGateList &gl) {
  uint32_t count = 0;
  for (const auto &gate : gl.gates)
    if (gate.type == GateType::T || gate.type == GateType::Tdg)
      ++count;
  return count;
}

static uint32_t countAndPebbles(const mockturtle::xag_network &xag,
                                const PebblingSequence &seq) {
  uint32_t count = 0;
  for (const auto &step : seq) {
    if (step.action == PebbleAction::Pebble && xag.is_and(xag.index_to_node(step.node)))
      ++count;
  }
  return count;
}

static uint64_t mixSeed(uint64_t base, uint32_t qubits, uint32_t function_id) {
  uint64_t z = base + 0x9e3779b97f4a7c15ULL +
               (static_cast<uint64_t>(qubits) << 32) + function_id;
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
  return z ^ (z >> 31);
}

static std::string csvEscape(const std::string &value) {
  if (value.find_first_of(",\"\n") == std::string::npos)
    return value;
  std::string out = "\"";
  for (char c : value) {
    if (c == '"')
      out += "\"\"";
    else
      out += c;
  }
  out += "\"";
  return out;
}

static bool writeCSV(const std::string &path, const std::vector<ResultRow> &rows) {
  std::ofstream out(path);
  if (!out)
    return false;

  out << "qubit_count,seed,function_id,bennett_t_count,caterpillar_t_count,"
         "difference,notes\n";
  for (const auto &row : rows) {
    out << row.qubit_count << "," << row.seed << "," << row.function_id << ","
        << row.bennett_t_count << "," << row.caterpillar_t_count << ","
        << row.difference << "," << csvEscape(row.notes) << "\n";
  }
  return out.good();
}

static std::string formatMean(double value) {
  std::ostringstream oss;
  oss.setf(std::ios::fixed);
  oss.precision(2);
  oss << value;
  return oss.str();
}

static bool writeLatex(const std::string &path,
                       const std::map<uint32_t, SummaryStats> &stats) {
  std::ofstream out(path);
  if (!out)
    return false;

  out << "% Random Boolean function comparison - generated by "
         "RandomBooleanFunctionTest\n"
      << "\\begin{table}[h]\n"
      << "\\centering\n"
      << "\\caption{Random Boolean function T-count comparison by qubit count}\n"
      << "\\label{tab:random_bool_comparison}\n"
      << "\\begin{tabular}{|r|r|rrr|rrr|rrr|}\n"
      << "\\hline\n"
      << "Qubits & N & \\multicolumn{3}{c|}{Bennett} & "
         "\\multicolumn{3}{c|}{Caterpillar} & "
         "\\multicolumn{3}{c|}{$\\Delta$ (B-C)} \\\\\n"
      << " & & mean & min & max & mean & min & max & mean & min & max \\\\\n"
      << "\\hline\n";

  for (const auto &[qubits, s] : stats) {
    if (s.count == 0)
      continue;
    out << qubits << " & " << s.count << " & "
        << formatMean(static_cast<double>(s.bennett_sum) / s.count) << " & "
        << s.bennett_min << " & " << s.bennett_max << " & "
        << formatMean(static_cast<double>(s.caterpillar_sum) / s.count) << " & "
        << s.caterpillar_min << " & " << s.caterpillar_max << " & "
        << formatMean(static_cast<double>(s.diff_sum) / s.count) << " & "
        << s.diff_min << " & " << s.diff_max << " \\\\\n";
  }

  out << "\\hline\n"
      << "\\end{tabular}\n"
      << "\\end{table}\n";
  return out.good();
}

static bool ensureDir(const std::string &path) {
  // Create all path components recursively.
  for (size_t pos = 1; pos <= path.size(); ++pos) {
    if (pos == path.size() || path[pos] == '/') {
      std::string sub = path.substr(0, pos);
      if (mkdir(sub.c_str(), 0755) != 0 && errno != EEXIST)
        return false;
    }
  }
  return true;
}

static std::string
caterpillarNetlistToJSON(const tweedledum::netlist<caterpillar::stg_gate> &qnet) {
  std::string json =
      "{\"num_qubits\":" + std::to_string(qnet.num_qubits()) + ",\"gates\":[";
  bool first = true;
  qnet.foreach_cgate([&](auto &cgate) {
    if (!first)
      json += ",";
    first = false;

    const auto &gate = cgate.gate;
    const char *type_str = "unknown";
    auto op = gate.operation();
    if (op == tweedledum::gate_set::t)
      type_str = "t";
    else if (op == tweedledum::gate_set::t_dagger)
      type_str = "tdg";
    else if (op == tweedledum::gate_set::pauli_x)
      type_str = "x";
    else if (op == tweedledum::gate_set::cx)
      type_str = "cx";
    else if (op == tweedledum::gate_set::mcx)
      type_str = "ccx";
    else if (op == tweedledum::gate_set::hadamard)
      type_str = "h";

    json += std::string("{\"type\":\"") + type_str + "\",\"controls\":[";
    bool first_ctrl = true;
    gate.foreach_control([&](auto c) {
      if (!first_ctrl)
        json += ",";
      first_ctrl = false;
      json += std::to_string(c.index());
    });
    json += "],\"target\":" +
            std::to_string(gate.targets()[0].index()) + "}";
  });
  json += "]}";
  return json;
}

static bool writeCircuit(const std::string &path, const std::string &json) {
  std::ofstream f(path);
  if (!f.is_open())
    return false;
  f << json;
  return f.good();
}

static ResultRow runOne(uint32_t qubits, uint32_t function_id, uint64_t base_seed,
                        const std::string &circuits_dir) {
  ResultRow row;
  row.qubit_count = qubits;
  row.function_id = function_id;
  uint32_t attempt = 0;
  bool constraint_ok = false;
  bool success = false;
  uint32_t and_pebbles = 0;
  size_t bennett_steps = 0;
  QCGateList final_bennett_gl;
  tweedledum::netlist<caterpillar::stg_gate> final_qnet;

  for (; attempt < 32; ++attempt) {
    row.seed = mixSeed(base_seed, qubits, function_id * 32u + attempt);

    RandomXAGParams params;
    params.num_pis = qubits;
    params.num_and_gates = qubits;
    params.num_xor_gates = std::max<uint32_t>(1, qubits / 2);
    params.seed = row.seed;

    auto xag = generate_random_xag(params);
    constraint_ok = validate_and_constraint(xag);

    PebblingSequence bennett = BennettSequence::build(xag);
    and_pebbles = countAndPebbles(xag, bennett);
    bennett_steps = bennett.size();

    XagContext bennett_ctx;
    bennett_ctx.xag = xag;
    bennett_ctx.optimized = true;
    QCGateList bennett_gl = PebblingMethod::translate(bennett_ctx, bennett);

    tweedledum::netlist<caterpillar::stg_gate> qnet;
    caterpillar::logic_network_synthesis_params cat_ps;
    caterpillar::logic_network_synthesis_stats cat_st;
    caterpillar::xag_mapping_strategy cat_strategy;
    caterpillar::logic_network_synthesis(qnet, xag, cat_strategy, {}, cat_ps,
                                         &cat_st);
    auto [cnot_count, cat_t_count, cat_t_depth] =
        caterpillar::detail::qc_stats(qnet, false);
    (void)cnot_count;
    (void)cat_t_depth;

    row.bennett_t_count = countTgates(bennett_gl);
    row.caterpillar_t_count = cat_t_count;
    row.difference = static_cast<int>(row.bennett_t_count) -
                     static_cast<int>(row.caterpillar_t_count);

    if (constraint_ok && and_pebbles > 0 && row.bennett_t_count > 0 &&
        row.caterpillar_t_count > 0) {
      final_bennett_gl = std::move(bennett_gl);
      final_qnet = std::move(qnet);
      success = true;
      break;
    }
  }

  const uint32_t attempts_used = std::min<uint32_t>(attempt + 1, 32);

  std::vector<std::string> notes;
  notes.push_back(constraint_ok ? "constraint_ok" : "constraint_failed");
  notes.push_back("bennett_steps=" + std::to_string(bennett_steps));
  notes.push_back("and_pebbles=" + std::to_string(and_pebbles));
  notes.push_back("attempts=" + std::to_string(attempts_used));
  notes.push_back(success ? "selection=success" : "selection=exhausted");
  notes.push_back("caterpillar_path=logic_network_synthesis");
  if (!success) {
    row.bennett_t_count = 0;
    row.caterpillar_t_count = 0;
    row.difference = 0;
    notes.push_back("no_valid_function_found");
  }

  for (size_t i = 0; i < notes.size(); ++i) {
    if (i)
      row.notes += "; ";
    row.notes += notes[i];
  }

  if (success && !circuits_dir.empty()) {
    std::string base = circuits_dir + "/circuit_q" + std::to_string(qubits) +
                       "_f" + std::to_string(function_id);
    writeCircuit(base + "_bennett.json", final_bennett_gl.toJSON());
    writeCircuit(base + "_caterpillar.json",
                 caterpillarNetlistToJSON(final_qnet));
  }

  row.passed = success;
  return row;
}

} // namespace

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

  std::vector<uint32_t> qubit_counts;
  if (args.have_qubit_count) {
    qubit_counts.push_back(args.qubit_count);
  } else {
    qubit_counts = {5, 6, 7, 8};
  }

  errs() << "=== Random Boolean Function Test ===\n";
  errs() << "Qubit counts:";
  for (uint32_t q : qubit_counts)
    errs() << " " << q;
  errs() << "\nFunctions per qubit count: " << args.num_functions << "\n"
         << "Base seed: " << args.seed << "\n"
         << "CSV output: " << args.output_csv << "\n"
         << "LaTeX output: " << args.output_latex << "\n"
         << "Circuits dir: " << args.circuits_dir << "\n\n";

  if (!ensureDir(args.circuits_dir)) {
    errs() << "Failed to create circuits directory: " << args.circuits_dir << "\n";
    return 2;
  }

  errs() << "qubits | function | seed               | Bennett T | Caterpillar T | "
            "Diff | Notes\n";
  errs() << "-------|----------|--------------------|-----------|----------------|"
            "------|------------------------------\n";

  std::vector<ResultRow> rows;
  std::map<uint32_t, SummaryStats> summaries;
  bool all_pass = true;

  for (uint32_t qubits : qubit_counts) {
    for (uint32_t function_id = 0; function_id < args.num_functions; ++function_id) {
      try {
        ResultRow row = runOne(qubits, function_id, args.seed, args.circuits_dir);
        rows.push_back(row);

        if (row.passed) {
          auto &summary = summaries[qubits];
          summary.count++;
          summary.bennett_sum += row.bennett_t_count;
          summary.caterpillar_sum += row.caterpillar_t_count;
          summary.diff_sum += row.difference;
          summary.bennett_min = std::min(summary.bennett_min, row.bennett_t_count);
          summary.bennett_max = std::max(summary.bennett_max, row.bennett_t_count);
          summary.caterpillar_min =
              std::min(summary.caterpillar_min, row.caterpillar_t_count);
          summary.caterpillar_max =
              std::max(summary.caterpillar_max, row.caterpillar_t_count);
          summary.diff_min = std::min(summary.diff_min, row.difference);
          summary.diff_max = std::max(summary.diff_max, row.difference);
        }

        errs() << qubits << "\t| " << function_id << "\t | " << row.seed << "\t | "
               << row.bennett_t_count << "\t    | " << row.caterpillar_t_count
               << "\t       | " << row.difference << "\t | " << row.notes
               << "\n";
        all_pass &= row.passed;
      } catch (const std::exception &e) {
        errs() << qubits << "\t| " << function_id << "\t | EXCEPTION: "
               << e.what() << "\n";
        all_pass = false;
      }
    }
  }

  errs() << "\nSummary by qubit count:\n";
  errs() << "qubits | N | Bennett mean/min/max | Caterpillar mean/min/max | "
            "Diff mean/min/max\n";
  errs() << "-------|---|----------------------|---------------------------|"
            "------------------\n";
  for (const auto &[qubits, s] : summaries) {
    if (s.count == 0)
      continue;
    errs() << qubits << "\t| " << s.count << " | "
           << formatMean(static_cast<double>(s.bennett_sum) / s.count) << " / "
           << s.bennett_min << " / " << s.bennett_max << " | "
           << formatMean(static_cast<double>(s.caterpillar_sum) / s.count) << " / "
           << s.caterpillar_min << " / " << s.caterpillar_max << " | "
           << formatMean(static_cast<double>(s.diff_sum) / s.count) << " / "
           << s.diff_min << " / " << s.diff_max << "\n";
  }

  if (!writeCSV(args.output_csv, rows)) {
    errs() << "Failed to write CSV file: " << args.output_csv << "\n";
    return 2;
  }
  if (!writeLatex(args.output_latex, summaries)) {
    errs() << "Failed to write LaTeX file: " << args.output_latex << "\n";
    return 2;
  }

  errs() << "\nWrote CSV report to " << args.output_csv << "\n";
  errs() << "Wrote LaTeX table to " << args.output_latex << "\n";
  errs() << "Circuit JSON output directory: " << args.circuits_dir << "/\n";
  errs() << (all_pass ? "RandomBooleanFunctionTest PASSED\n"
                      : "RandomBooleanFunctionTest FAILED\n");
  return all_pass ? 0 : 1;
}
