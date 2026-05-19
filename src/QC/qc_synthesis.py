"""Convert a JSON gate list to OpenQASM 2.0 via Qiskit."""

import json

from qiskit import QuantumCircuit
from qiskit.qasm2 import dumps


def gates_to_qasm(json_str: str) -> str:
    """Build a QuantumCircuit from the JSON gate list and return QASM."""
    data = json.loads(json_str)
    qc = QuantumCircuit(data["num_qubits"])

    for gate in data["gates"]:
        t = gate["type"]
        tgt = gate["target"]
        ctrls = gate.get("controls", [])

        if t == "x":
            qc.x(tgt)
        elif t == "cx":
            qc.cx(ctrls[0], tgt)
        elif t == "ccx":
            qc.ccx(ctrls[0], ctrls[1], tgt)
        elif t == "h":
            qc.h(tgt)
        elif t == "t":
            qc.t(tgt)
        elif t == "tdg":
            qc.tdg(tgt)

    return dumps(qc)


def json_to_circuit(data: dict) -> QuantumCircuit:
    """Build a QuantumCircuit from a parsed JSON gate list dict."""
    qc = QuantumCircuit(data["num_qubits"])

    for gate in data["gates"]:
        t = gate["type"]
        tgt = gate["target"]
        ctrls = gate.get("controls", [])

        if t == "x":
            qc.x(tgt)
        elif t == "cx":
            qc.cx(ctrls[0], tgt)
        elif t == "ccx":
            qc.ccx(ctrls[0], ctrls[1], tgt)
        elif t == "h":
            qc.h(tgt)
        elif t == "t":
            qc.t(tgt)
        elif t == "tdg":
            qc.tdg(tgt)

    return qc
