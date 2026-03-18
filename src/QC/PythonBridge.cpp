// PythonBridge.cpp — Python C API bridge to Qiskit.
//
// Python.h must be included before any standard headers on some platforms.
#include <Python.h>

#include "PythonBridge.h"
#include "llvm/Support/raw_ostream.h"

using namespace xagtdep;

namespace {

/// Static singleton — calls Py_Initialize() once, never Py_Finalize()
/// (avoids conflicts with LLVM's own atexit handlers).
class PythonGuard {
public:
  static PythonGuard &instance() {
    static PythonGuard guard;
    return guard;
  }
  bool initialized() const { return initialized_; }

private:
  PythonGuard() {
    Py_Initialize();
    initialized_ = Py_IsInitialized();
  }
  ~PythonGuard() = default;
  bool initialized_ = false;
};

} // namespace

std::string PythonBridge::callQiskitSynthesis(const std::string &json_str) {
  auto &guard = PythonGuard::instance();
  if (!guard.initialized()) {
    llvm::errs() << "[QC] WARNING: Python interpreter not initialized\n";
    return "";
  }

  // Add the script directory to sys.path so we can import qc_synthesis.
  PyObject *sys = PyImport_ImportModule("sys");
  if (!sys) {
    llvm::errs() << "[QC] WARNING: Could not import sys\n";
    return "";
  }
  PyObject *path = PyObject_GetAttrString(sys, "path");
  PyObject *script_dir = PyUnicode_FromString(QC_PYTHON_SCRIPT_DIR);
  PyList_Insert(path, 0, script_dir);
  Py_DECREF(script_dir);
  Py_DECREF(path);
  Py_DECREF(sys);

  // Import qc_synthesis module.
  PyObject *module = PyImport_ImportModule("qc_synthesis");
  if (!module) {
    llvm::errs() << "[QC] WARNING: Could not import qc_synthesis module\n";
    PyErr_Print();
    return "";
  }

  // Get the gates_to_qasm function.
  PyObject *func = PyObject_GetAttrString(module, "gates_to_qasm");
  if (!func || !PyCallable_Check(func)) {
    llvm::errs() << "[QC] WARNING: gates_to_qasm function not found\n";
    Py_XDECREF(func);
    Py_DECREF(module);
    return "";
  }

  // Call gates_to_qasm(json_str).
  PyObject *json_py = PyUnicode_FromString(json_str.c_str());
  PyObject *args = PyTuple_Pack(1, json_py);
  Py_DECREF(json_py);

  PyObject *result = PyObject_CallObject(func, args);
  Py_DECREF(args);
  Py_DECREF(func);
  Py_DECREF(module);

  if (!result) {
    llvm::errs() << "[QC] WARNING: gates_to_qasm call failed\n";
    PyErr_Print();
    return "";
  }

  std::string qasm;
  if (PyUnicode_Check(result)) {
    qasm = PyUnicode_AsUTF8(result);
  }
  Py_DECREF(result);

  return qasm;
}
