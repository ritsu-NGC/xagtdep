#include "AIGReader.h"
#include <fstream>
#include <limits>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

namespace xagtdep {

namespace {

bool readLine(const std::string &data, size_t &pos, std::string &line) {
  if (pos >= data.size())
    return false;
  const size_t start = pos;
  size_t end = data.find('\n', start);
  if (end == std::string::npos) {
    line = data.substr(start);
    pos = data.size();
  } else {
    line = data.substr(start, end - start);
    pos = end + 1;
  }
  if (!line.empty() && line.back() == '\r')
    line.pop_back();
  return true;
}

uint32_t parseU32Token(const std::string &tok, const char *what) {
  if (tok.empty())
    throw AIGParseError(std::string("[AIGReader] empty token for ") + what);
  size_t idx = 0;
  uint64_t v = 0;
  try {
    v = std::stoull(tok, &idx);
  } catch (const std::exception &) {
    throw AIGParseError(std::string("[AIGReader] invalid integer for ") + what +
                        ": \"" + tok + "\"");
  }
  if (idx != tok.size()) {
    throw AIGParseError(std::string("[AIGReader] invalid integer for ") + what +
                        ": \"" + tok + "\"");
  }
  if (v > std::numeric_limits<uint32_t>::max()) {
    throw AIGParseError(std::string("[AIGReader] integer out of range for ") +
                        what + ": \"" + tok + "\"");
  }
  return static_cast<uint32_t>(v);
}

uint32_t parseSingleLiteralLine(const std::string &line, const char *what) {
  std::istringstream iss(line);
  std::string tok;
  if (!(iss >> tok)) {
    throw AIGParseError(std::string("[AIGReader] missing ") + what + " line");
  }
  return parseU32Token(tok, what);
}

uint32_t readVarUInt(const std::string &data, size_t &pos) {
  uint32_t x = 0;
  uint32_t shift = 0;
  while (true) {
    if (pos >= data.size())
      throw AIGParseError("[AIGReader] unexpected EOF in binary AND payload");
    const uint8_t ch = static_cast<uint8_t>(data[pos++]);
    x |= static_cast<uint32_t>(ch & 0x7fu) << shift;
    if ((ch & 0x80u) == 0)
      break;
    shift += 7;
    if (shift > 28)
      throw AIGParseError("[AIGReader] binary delta overflow");
  }
  return x;
}

} // namespace

AIG AIGReader::readFromFile(const std::string &path) {
  std::ifstream f(path, std::ios::binary);
  if (!f.is_open())
    throw AIGParseError("[AIGReader] cannot open file: " + path);
  std::ostringstream ss;
  ss << f.rdbuf();
  return readFromData(ss.str());
}

AIG AIGReader::readFromData(const std::string &data) {
  size_t pos = 0;
  std::string header;
  if (!readLine(data, pos, header))
    throw AIGParseError("[AIGReader] empty input");

  std::istringstream hs(header);
  std::string fmt;
  std::string mTok, iTok, lTok, oTok, aTok;
  if (!(hs >> fmt >> mTok >> iTok >> lTok >> oTok >> aTok)) {
    throw AIGParseError("[AIGReader] malformed header");
  }
  if (fmt != "aag" && fmt != "aig")
    throw AIGParseError("[AIGReader] unsupported format (expected aag/aig)");

  AIG aig;
  aig.uses_binary = (fmt == "aig");
  aig.max_var = parseU32Token(mTok, "M");
  aig.num_inputs = parseU32Token(iTok, "I");
  aig.num_latches = parseU32Token(lTok, "L");
  aig.num_outputs = parseU32Token(oTok, "O");
  aig.num_ands = parseU32Token(aTok, "A");

  if (aig.uses_binary) {
    aig.inputs.reserve(aig.num_inputs);
    for (uint32_t i = 0; i < aig.num_inputs; ++i)
      aig.inputs.push_back(2u * (i + 1u));

    aig.latches.reserve(aig.num_latches);
    for (uint32_t i = 0; i < aig.num_latches; ++i) {
      std::string line;
      if (!readLine(data, pos, line))
        throw AIGParseError("[AIGReader] missing latch line");
      std::istringstream ls(line);
      std::string nextTok;
      if (!(ls >> nextTok))
        throw AIGParseError("[AIGReader] malformed latch line");
      AIGLatch latch;
      latch.current_lit = 2u * (aig.num_inputs + i + 1u);
      latch.next_lit = parseU32Token(nextTok, "latch next literal");
      aig.latches.push_back(latch);
    }

    aig.outputs.reserve(aig.num_outputs);
    for (uint32_t i = 0; i < aig.num_outputs; ++i) {
      std::string line;
      if (!readLine(data, pos, line))
        throw AIGParseError("[AIGReader] missing output line");
      aig.outputs.push_back(parseSingleLiteralLine(line, "output literal"));
    }

    aig.ands.reserve(aig.num_ands);
    for (uint32_t i = 0; i < aig.num_ands; ++i) {
      const uint32_t lhs = 2u * (aig.num_inputs + aig.num_latches + i + 1u);
      const uint32_t d1 = readVarUInt(data, pos);
      if (d1 > lhs)
        throw AIGParseError("[AIGReader] invalid binary delta (d1)");
      const uint32_t rhs0 = lhs - d1;
      const uint32_t d2 = readVarUInt(data, pos);
      if (d2 > rhs0)
        throw AIGParseError("[AIGReader] invalid binary delta (d2)");
      const uint32_t rhs1 = rhs0 - d2;
      aig.ands.push_back({lhs, rhs0, rhs1});
    }
  } else {
    aig.inputs.reserve(aig.num_inputs);
    for (uint32_t i = 0; i < aig.num_inputs; ++i) {
      std::string line;
      if (!readLine(data, pos, line))
        throw AIGParseError("[AIGReader] missing input line");
      aig.inputs.push_back(parseSingleLiteralLine(line, "input literal"));
    }

    aig.latches.reserve(aig.num_latches);
    for (uint32_t i = 0; i < aig.num_latches; ++i) {
      std::string line;
      if (!readLine(data, pos, line))
        throw AIGParseError("[AIGReader] missing latch line");
      std::istringstream ls(line);
      std::string curTok, nextTok;
      if (!(ls >> curTok >> nextTok))
        throw AIGParseError("[AIGReader] malformed latch line");
      AIGLatch latch;
      latch.current_lit = parseU32Token(curTok, "latch literal");
      latch.next_lit = parseU32Token(nextTok, "latch next literal");
      aig.latches.push_back(latch);
    }

    aig.outputs.reserve(aig.num_outputs);
    for (uint32_t i = 0; i < aig.num_outputs; ++i) {
      std::string line;
      if (!readLine(data, pos, line))
        throw AIGParseError("[AIGReader] missing output line");
      aig.outputs.push_back(parseSingleLiteralLine(line, "output literal"));
    }

    aig.ands.reserve(aig.num_ands);
    for (uint32_t i = 0; i < aig.num_ands; ++i) {
      std::string line;
      if (!readLine(data, pos, line))
        throw AIGParseError("[AIGReader] missing AND line");
      std::istringstream as(line);
      std::string lhsTok, rhs0Tok, rhs1Tok;
      if (!(as >> lhsTok >> rhs0Tok >> rhs1Tok))
        throw AIGParseError("[AIGReader] malformed AND line");
      aig.ands.push_back({parseU32Token(lhsTok, "AND lhs"),
                          parseU32Token(rhs0Tok, "AND rhs0"),
                          parseU32Token(rhs1Tok, "AND rhs1")});
    }
  }

  validate(aig);
  return aig;
}

void AIGReader::validate(const AIG &aig) {
  if (aig.max_var != aig.num_inputs + aig.num_latches + aig.num_ands) {
    throw AIGParseError("[AIGReader] header mismatch: M must equal I+L+A");
  }
  if (aig.inputs.size() != aig.num_inputs || aig.latches.size() != aig.num_latches ||
      aig.outputs.size() != aig.num_outputs || aig.ands.size() != aig.num_ands) {
    throw AIGParseError("[AIGReader] section length mismatch");
  }

  const uint64_t max_lit = static_cast<uint64_t>(2u) * aig.max_var + 1u;
  auto requireBoundedLit = [&](uint32_t lit, const char *what) {
    if (static_cast<uint64_t>(lit) > max_lit)
      throw AIGParseError(std::string("[AIGReader] literal out of range in ") +
                          what);
  };

  std::unordered_set<uint32_t> input_vars;
  for (uint32_t lit : aig.inputs) {
    requireBoundedLit(lit, "inputs");
    if ((lit & 1u) != 0u || lit == 0u)
      throw AIGParseError("[AIGReader] input literals must be positive and even");
    const uint32_t v = lit >> 1;
    if (!input_vars.insert(v).second)
      throw AIGParseError("[AIGReader] duplicate input variable");
  }

  for (size_t i = 0; i < aig.latches.size(); ++i) {
    const auto &l = aig.latches[i];
    requireBoundedLit(l.current_lit, "latches");
    requireBoundedLit(l.next_lit, "latches");
    const uint32_t expected_var = aig.num_inputs + static_cast<uint32_t>(i) + 1u;
    if (l.current_lit != 2u * expected_var) {
      throw AIGParseError("[AIGReader] latch literal order mismatch");
    }
  }

  std::unordered_set<uint32_t> and_vars;
  for (size_t i = 0; i < aig.ands.size(); ++i) {
    const auto &g = aig.ands[i];
    requireBoundedLit(g.lhs, "AND lhs");
    requireBoundedLit(g.rhs0, "AND rhs0");
    requireBoundedLit(g.rhs1, "AND rhs1");
    if ((g.lhs & 1u) != 0u || g.lhs == 0u)
      throw AIGParseError("[AIGReader] AND lhs must be positive and even");
    const uint32_t lhs_var = g.lhs >> 1;
    const uint32_t expected_var =
        aig.num_inputs + aig.num_latches + static_cast<uint32_t>(i) + 1u;
    if (lhs_var != expected_var)
      throw AIGParseError("[AIGReader] AND lhs variable order mismatch");
    if (!and_vars.insert(lhs_var).second)
      throw AIGParseError("[AIGReader] duplicate AND lhs variable");

    if ((g.rhs0 >> 1) >= lhs_var || (g.rhs1 >> 1) >= lhs_var)
      throw AIGParseError("[AIGReader] non-topological AND fanin detected");
  }

  for (uint32_t lit : aig.outputs)
    requireBoundedLit(lit, "outputs");
}

mockturtle::xag_network AIGReader::toXagNetwork(const AIG &aig) {
  if (aig.num_latches != 0u) {
    throw AIGParseError(
        "[AIGReader] latch-containing AIGs are not supported by XAG conversion");
  }

  mockturtle::xag_network xag;
  std::unordered_map<uint32_t, mockturtle::xag_network::signal> var_to_signal;
  var_to_signal.reserve(aig.num_inputs + aig.num_ands);

  for (uint32_t lit : aig.inputs) {
    const uint32_t v = lit >> 1;
    var_to_signal[v] = xag.create_pi();
  }

  auto signalFromLiteral = [&](uint32_t lit) -> mockturtle::xag_network::signal {
    const uint32_t v = lit >> 1;
    mockturtle::xag_network::signal s;
    if (v == 0u) {
      s = xag.get_constant(false);
    } else {
      auto it = var_to_signal.find(v);
      if (it == var_to_signal.end())
        throw AIGParseError("[AIGReader] undefined fanin variable in AND/PO");
      s = it->second;
    }
    if ((lit & 1u) != 0u)
      s = !s;
    return s;
  };

  for (const auto &g : aig.ands) {
    const uint32_t lhs_var = g.lhs >> 1;
    if (var_to_signal.find(lhs_var) != var_to_signal.end())
      throw AIGParseError("[AIGReader] multiply defined variable");
    const auto rhs0 = signalFromLiteral(g.rhs0);
    const auto rhs1 = signalFromLiteral(g.rhs1);
    var_to_signal[lhs_var] = xag.create_and(rhs0, rhs1);
  }

  for (uint32_t lit : aig.outputs) {
    xag.create_po(signalFromLiteral(lit));
  }

  return xag;
}

} // namespace xagtdep
