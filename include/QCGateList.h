// QCGateList.h — Intermediate representation between C++ XAG traversal and
// Python/Qiskit quantum circuit construction.

#ifndef QCGATELIST_H
#define QCGATELIST_H

#include <cstdint>
#include <string>
#include <vector>

namespace xagtdep {

enum class GateType { X, CNOT, Toffoli, H, T, Tdg };

struct GateOp {
  GateType type;
  std::vector<uint32_t> controls;
  uint32_t target;
};

struct QCGateList {
  uint32_t num_qubits = 0;
  uint32_t num_pis = 0;
  uint32_t num_ancillas = 0;
  std::vector<GateOp> gates;

  /// Serialize to JSON for passing to the Python/Qiskit bridge.
  inline std::string toJSON() const {
    std::string json = "{\"num_qubits\":" + std::to_string(num_qubits) +
                       ",\"num_pis\":" + std::to_string(num_pis) +
                       ",\"num_ancillas\":" + std::to_string(num_ancillas) +
                       ",\"gates\":[";
    for (size_t i = 0; i < gates.size(); ++i) {
      if (i > 0)
        json += ",";
      const auto &g = gates[i];
      json += "{\"type\":\"";
      switch (g.type) {
      case GateType::X:
        json += "x";
        break;
      case GateType::CNOT:
        json += "cx";
        break;
      case GateType::Toffoli:
        json += "ccx";
        break;
      case GateType::H:
        json += "h";
        break;
      case GateType::T:
        json += "t";
        break;
      case GateType::Tdg:
        json += "tdg";
        break;
      }
      json += "\",\"controls\":[";
      for (size_t j = 0; j < g.controls.size(); ++j) {
        if (j > 0)
          json += ",";
        json += std::to_string(g.controls[j]);
      }
      json += "],\"target\":" + std::to_string(g.target) + "}";
    }
    json += "]}";
    return json;
  }
};

} // namespace xagtdep

#endif // QCGATELIST_H
