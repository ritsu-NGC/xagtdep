// PythonBridge.h — Calls Python/Qiskit to convert a JSON gate list into QASM.

#ifndef PYTHONBRIDGE_H
#define PYTHONBRIDGE_H

#include <string>

namespace xagtdep {

class PythonBridge {
public:
  /// Pass a JSON gate list to qc_synthesis.gates_to_qasm() and return QASM.
  /// Returns empty string on failure.
  static std::string callQiskitSynthesis(const std::string &json_str);
};

} // namespace xagtdep

#endif // PYTHONBRIDGE_H
