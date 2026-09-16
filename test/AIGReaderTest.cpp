#include "AIGReader.h"
#include "AIGToQC.h"
#include "llvm/Support/raw_ostream.h"
#include <fstream>

using namespace xagtdep;

static std::string sampleAsciiAag() {
  return "aag 3 2 0 1 1\n"
         "2\n"
         "4\n"
         "7\n"
         "6 5 2\n";
}

static std::string sampleBinaryAig() {
  std::string s = "aig 3 2 0 1 1\n7\n";
  s.push_back(static_cast<char>(0x01)); // d1 = 1 (rhs0 = 5)
  s.push_back(static_cast<char>(0x03)); // d2 = 3 (rhs1 = 2)
  return s;
}

static bool testAsciiParseAndConvert() {
  const AIG aig = AIGReader::readFromData(sampleAsciiAag());
  bool pass = true;

  pass &= !aig.uses_binary;
  pass &= (aig.max_var == 3 && aig.num_inputs == 2 && aig.num_outputs == 1 &&
           aig.num_ands == 1 && aig.num_latches == 0);
  pass &= (aig.inputs.size() == 2 && aig.inputs[0] == 2 && aig.inputs[1] == 4);
  pass &= (aig.outputs.size() == 1 && aig.outputs[0] == 7);
  pass &= (aig.ands.size() == 1 && aig.ands[0].lhs == 6 && aig.ands[0].rhs0 == 5 &&
           aig.ands[0].rhs1 == 2);

  const auto xag = AIGReader::toXagNetwork(aig);
  pass &= (xag.num_pis() == 2);
  pass &= (xag.num_pos() == 1);
  pass &= (xag.num_gates() == 1);

  bool po_complemented = false;
  bool po_is_and = false;
  xag.foreach_po([&](auto sig) {
    po_complemented = xag.is_complemented(sig);
    po_is_and = xag.is_and(xag.get_node(sig));
  });
  pass &= po_complemented && po_is_and;

  llvm::errs() << "[AIGReaderTest::AsciiParseAndConvert] "
               << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

static bool testBinaryParse() {
  const AIG aig = AIGReader::readFromData(sampleBinaryAig());
  bool pass = true;

  pass &= aig.uses_binary;
  pass &= (aig.max_var == 3 && aig.num_inputs == 2 && aig.num_outputs == 1 &&
           aig.num_ands == 1 && aig.num_latches == 0);
  pass &= (aig.inputs.size() == 2 && aig.inputs[0] == 2 && aig.inputs[1] == 4);
  pass &= (aig.outputs.size() == 1 && aig.outputs[0] == 7);
  pass &= (aig.ands.size() == 1 && aig.ands[0].lhs == 6 && aig.ands[0].rhs0 == 5 &&
           aig.ands[0].rhs1 == 2);

  llvm::errs() << "[AIGReaderTest::BinaryParse] " << (pass ? "PASSED" : "FAILED")
               << "\n";
  return pass;
}

static bool testAigToQcSynthesis() {
  const QCGateList gl = AIGToQC::synthesizeData(sampleAsciiAag());
  bool pass = true;
  pass &= (gl.num_pis == 2);
  pass &= (gl.num_ancillas == 1);
  pass &= (gl.num_qubits == 3);
  pass &= (gl.gates.size() == 4);

  bool has_toffoli = false;
  for (const auto &g : gl.gates) {
    if (g.type == GateType::Toffoli)
      has_toffoli = true;
  }
  pass &= has_toffoli;
  pass &= !gl.gates.empty() && gl.gates.back().type == GateType::X;

  llvm::errs() << "[AIGReaderTest::AigToQcSynthesis] "
               << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

static bool testValidationFailure() {
  bool threw = false;
  try {
    (void)AIGReader::readFromData("aag 4 2 0 1 1\n2\n4\n6\n6 4 2\n");
  } catch (const AIGParseError &) {
    threw = true;
  }
  llvm::errs() << "[AIGReaderTest::ValidationFailure] "
               << (threw ? "PASSED" : "FAILED") << "\n";
  return threw;
}

static bool testFilePathFlow() {
  const std::string path = "/tmp/xagtdep-aig-reader-test.aag";
  {
    std::ofstream out(path, std::ios::binary);
    out << sampleAsciiAag();
  }
  const QCGateList gl = AIGToQC::synthesize(path);
  const bool pass = (gl.num_pis == 2 && gl.num_qubits == 3 && !gl.gates.empty());
  llvm::errs() << "[AIGReaderTest::FilePathFlow] " << (pass ? "PASSED" : "FAILED")
               << "\n";
  return pass;
}

int main() {
  bool all_pass = true;
  all_pass &= testAsciiParseAndConvert();
  all_pass &= testBinaryParse();
  all_pass &= testAigToQcSynthesis();
  all_pass &= testValidationFailure();
  all_pass &= testFilePathFlow();
  return all_pass ? 0 : 1;
}
