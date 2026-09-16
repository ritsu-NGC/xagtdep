#include "AIGToQC.h"
#include "XAGToGateList.h"

namespace xagtdep {

XagContext AIGToQC::contextFromAIGFile(const std::string &path) {
  return contextFromAIG(AIGReader::readFromFile(path));
}

XagContext AIGToQC::contextFromAIGData(const std::string &data) {
  return contextFromAIG(AIGReader::readFromData(data));
}

XagContext AIGToQC::contextFromAIG(const AIG &aig) {
  XagContext ctx;
  ctx.xag = AIGReader::toXagNetwork(aig);
  ctx.optimized = true;
  return ctx;
}

QCGateList AIGToQC::synthesize(const std::string &path) {
  return XAGToGateList::translate(contextFromAIGFile(path));
}

QCGateList AIGToQC::synthesizeData(const std::string &data) {
  return XAGToGateList::translate(contextFromAIGData(data));
}

} // namespace xagtdep
