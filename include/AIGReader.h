// AIGReader.h — parser for ASCII/binary AIGER and AIG→XAG conversion.

#ifndef AIGREADER_H
#define AIGREADER_H

#include <cstdint>
#include <mockturtle/networks/xag.hpp>
#include <stdexcept>
#include <string>
#include <vector>

namespace xagtdep {

struct AIGAndGate {
  uint32_t lhs = 0;
  uint32_t rhs0 = 0;
  uint32_t rhs1 = 0;
};

struct AIGLatch {
  uint32_t current_lit = 0;
  uint32_t next_lit = 0;
};

struct AIG {
  bool uses_binary = false;
  uint32_t max_var = 0;
  uint32_t num_inputs = 0;
  uint32_t num_latches = 0;
  uint32_t num_outputs = 0;
  uint32_t num_ands = 0;
  std::vector<uint32_t> inputs;
  std::vector<AIGLatch> latches;
  std::vector<uint32_t> outputs;
  std::vector<AIGAndGate> ands;
};

class AIGParseError : public std::runtime_error {
public:
  explicit AIGParseError(const std::string &msg) : std::runtime_error(msg) {}
};

class AIGReader {
public:
  static AIG readFromFile(const std::string &path);
  static AIG readFromData(const std::string &data);
  static mockturtle::xag_network toXagNetwork(const AIG &aig);

private:
  static void validate(const AIG &aig);
};

} // namespace xagtdep

#endif // AIGREADER_H
