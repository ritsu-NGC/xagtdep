// CIQCVerification.cpp — strict-lane runner.
//
// Reads test/ci_passing_set.json and runs each (pis, ands, xors, seed) tuple
// through the shared per-XAG synthesis pipeline. Writes per-XAG JSONs to
// verification_data_ci/. Exits non-zero if any synthesis step fails.
//
// QCEC-level enforcement happens at the next stage:
//   python test/verify_circuits.py --strict <data-dir>

#include "QCVerificationCommon.h"
#include "llvm/Support/raw_ostream.h"
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <vector>

#ifndef CI_PASSING_SET_DEFAULT
#define CI_PASSING_SET_DEFAULT "test/ci_passing_set.json"
#endif

using namespace llvm;

namespace {

struct Tuple {
  uint32_t pis = 0, ands = 0, xors = 0;
  uint64_t seed = 0;
};

// Parse the JSON config. The format is fixed and shallow:
//   { ..., "tuples": [ { "pis": N, "ands": N, "xors": N, "seed": <int|"0x.."> }, ... ] }
// We scan for { ... } objects inside the "tuples" array and pull each known
// numeric/seed key. Unknown keys are ignored. Whitespace-insensitive.
class TupleParser {
public:
  bool parse(const std::string &text, std::vector<Tuple> &out) {
    auto arr_start = text.find("\"tuples\"");
    if (arr_start == std::string::npos) {
      errs() << "config error: missing \"tuples\" key\n";
      return false;
    }
    auto bracket = text.find('[', arr_start);
    if (bracket == std::string::npos) {
      errs() << "config error: \"tuples\" is not followed by an array\n";
      return false;
    }
    size_t i = bracket + 1;
    while (i < text.size()) {
      while (i < text.size() && std::isspace((unsigned char)text[i]))
        ++i;
      if (i >= text.size())
        break;
      if (text[i] == ']')
        return true;
      if (text[i] == ',') {
        ++i;
        continue;
      }
      if (text[i] != '{') {
        errs() << "config error: expected '{' at offset " << i << "\n";
        return false;
      }
      auto end = findMatching(text, i, '{', '}');
      if (end == std::string::npos) {
        errs() << "config error: unterminated object starting at " << i << "\n";
        return false;
      }
      Tuple t;
      if (!parseObject(text.substr(i + 1, end - i - 1), t))
        return false;
      out.push_back(t);
      i = end + 1;
    }
    errs() << "config error: unterminated tuples array\n";
    return false;
  }

private:
  static size_t findMatching(const std::string &s, size_t start, char open,
                             char close) {
    int depth = 0;
    bool in_string = false;
    for (size_t i = start; i < s.size(); ++i) {
      char c = s[i];
      if (in_string) {
        if (c == '\\' && i + 1 < s.size()) {
          ++i;
          continue;
        }
        if (c == '"')
          in_string = false;
        continue;
      }
      if (c == '"') {
        in_string = true;
        continue;
      }
      if (c == open)
        ++depth;
      else if (c == close && --depth == 0)
        return i;
    }
    return std::string::npos;
  }

  static std::string extractStringValue(const std::string &body,
                                        const std::string &key, bool &found) {
    found = false;
    std::string needle = "\"" + key + "\"";
    auto k = body.find(needle);
    if (k == std::string::npos)
      return "";
    auto colon = body.find(':', k);
    if (colon == std::string::npos)
      return "";
    size_t p = colon + 1;
    while (p < body.size() && std::isspace((unsigned char)body[p]))
      ++p;
    if (p >= body.size())
      return "";
    // Quoted string value.
    if (body[p] == '"') {
      size_t q = body.find('"', p + 1);
      if (q == std::string::npos)
        return "";
      found = true;
      return body.substr(p + 1, q - p - 1);
    }
    // Bare numeric token.
    size_t q = p;
    while (q < body.size() && (std::isalnum((unsigned char)body[q]) ||
                               body[q] == '-' || body[q] == '+' ||
                               body[q] == 'x' || body[q] == 'X'))
      ++q;
    if (q == p)
      return "";
    found = true;
    return body.substr(p, q - p);
  }

  static bool parseUint(const std::string &tok, uint64_t &out) {
    if (tok.empty())
      return false;
    char *endp = nullptr;
    out = std::strtoull(tok.c_str(), &endp, 0); // 0 = auto-detect base (0x...)
    return endp && *endp == '\0';
  }

  bool parseObject(const std::string &body, Tuple &t) {
    bool ok = true;
    for (const auto &key : {"pis", "ands", "xors"}) {
      bool found = false;
      std::string tok = extractStringValue(body, key, found);
      if (!found) {
        errs() << "config error: missing \"" << key << "\" in tuple\n";
        return false;
      }
      uint64_t v = 0;
      if (!parseUint(tok, v)) {
        errs() << "config error: bad value for " << key << ": " << tok << "\n";
        return false;
      }
      if (std::string(key) == "pis") t.pis = (uint32_t)v;
      else if (std::string(key) == "ands") t.ands = (uint32_t)v;
      else t.xors = (uint32_t)v;
    }
    bool found_seed = false;
    std::string seed_tok = extractStringValue(body, "seed", found_seed);
    if (!found_seed) {
      errs() << "config error: missing \"seed\" in tuple\n";
      return false;
    }
    if (!parseUint(seed_tok, t.seed)) {
      errs() << "config error: bad value for seed: " << seed_tok << "\n";
      return false;
    }
    return ok;
  }
};

bool readFile(const std::string &path, std::string &out) {
  std::ifstream f(path);
  if (!f.is_open())
    return false;
  std::stringstream ss;
  ss << f.rdbuf();
  out = ss.str();
  return true;
}

} // namespace

int main(int argc, char **argv) {
  std::string config_path = CI_PASSING_SET_DEFAULT;
  std::string data_dir = "verification_data_ci";

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if ((a == "--config" || a == "-c") && i + 1 < argc) {
      config_path = argv[++i];
    } else if ((a == "--data-dir" || a == "-d") && i + 1 < argc) {
      data_dir = argv[++i];
    } else if (a == "-h" || a == "--help") {
      errs() << "Usage: CIQCVerification [--config <path>] [--data-dir <path>]\n"
                "  --config <path>   passing-set JSON (default: "
             << CI_PASSING_SET_DEFAULT << ")\n"
                "  --data-dir <path> output directory (default: verification_data_ci)\n";
      return 0;
    } else {
      errs() << "Unknown argument: " << a << "\n";
      return 2;
    }
  }

  std::string text;
  if (!readFile(config_path, text)) {
    errs() << "ERROR: cannot read " << config_path << "\n";
    return 2;
  }

  std::vector<Tuple> tuples;
  TupleParser parser;
  if (!parser.parse(text, tuples)) {
    errs() << "ERROR: failed to parse " << config_path << "\n";
    return 2;
  }
  if (tuples.empty()) {
    errs() << "ERROR: no tuples found in " << config_path << "\n";
    return 2;
  }

  mkdir(data_dir.c_str(), 0755);

  errs() << "=== CI QC Verification (strict lane) ===\n";
  errs() << "Config:    " << config_path << "\n";
  errs() << "Data dir:  " << data_dir << "\n";
  errs() << "Tuples:    " << tuples.size() << "\n\n";

  errs() << "  #  | PIs | ANDs | XORs | Constraint | Method    | T-cnt | "
            "CNOTs | Qubits | Total\n";
  errs() << "  ---|-----|------|------|------------|-----------|-------|"
            "-------|--------|------\n";

  int passed = 0, failed = 0;
  for (size_t i = 0; i < tuples.size(); ++i) {
    const Tuple &t = tuples[i];
    bool ok = xagtdep::test::runOneXag(t.pis, t.ands, t.xors, t.seed,
                                       (int)i, data_dir, "all");
    (ok ? passed : failed)++;
  }

  errs() << "\n=== Summary: " << passed << " passed, " << failed
         << " failed out of " << tuples.size() << " XAGs ===\n";
  errs() << "JSON files written to " << data_dir << "/\n";
  errs() << "Run formal verification with:\n"
         << "  python3 test/verify_circuits.py --strict " << data_dir << "\n";

  return failed > 0 ? 1 : 0;
}
