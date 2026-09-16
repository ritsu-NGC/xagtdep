// AIGToQC.h — convenience API: AIGER input to XagContext/QCGateList.

#ifndef AIGTOQC_H
#define AIGTOQC_H

#include "AIGReader.h"
#include "QCGateList.h"
#include "XagContext.h"
#include <string>

namespace xagtdep {

class AIGToQC {
public:
  static XagContext contextFromAIGFile(const std::string &path);
  static XagContext contextFromAIGData(const std::string &data);
  static XagContext contextFromAIG(const AIG &aig);

  static QCGateList synthesize(const std::string &path);
  static QCGateList synthesizeData(const std::string &data);
};

} // namespace xagtdep

#endif // AIGTOQC_H
