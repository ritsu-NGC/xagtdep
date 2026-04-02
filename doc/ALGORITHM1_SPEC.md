# Algorithm 1: Parse XAG to Quantum Circuit

## Pseudocode

```
 1:  QC <- empty quantum circuit
 2:  xag <- XOR-And-Inverter Graph of the function
 3:  (compute, uncompute) <- PARSEXAG(xag)
 4:  QC <- compute || uncompute
 5:
 6:  function PARSEXAG(xag):
 7:      node <- top node of xag
 8:      if node A-child is primary input then
 9:          A <- node A-child
10:      else
11:          A <- PARSEXAG(subgraph of A-child)
12:      end if
13:      if node B-child is primary input then
14:          B <- node B-child
15:      else
16:          B <- PARSEXAG(subgraph of B-child)
17:      end if
18:      if node is XOR node then
19:          subQC <- generate circuit from Fig. 4
20:      else if node is AND node then
21:          if A and B are primary inputs then
22:              subQC <- generate circuit (AND case) Fig. 6
23:          else if node output is overall output then
24:              subQC <- generate circuit (AND case) Fig. 1
25:          else if one of A, B is primary input then
26:              subQC <- generate circuit (XOR case) Fig. 8
27:          else
28:              error
29:          end if
30:      end if
31:      return subQC
32:  end function
```

---

## XAG Node Types

### Fig. 2: AND Node

```
        F
        ^
        |
      /---\
     | AND |
      \---/
      /   \
     A     B
```

Output F = A AND B. Maps to a Toffoli-class operation (4 T-gates in decomposed mode).

### Fig. 3: XOR Node

```
        F
        ^
        |
      /---\
     | XOR |
      \---/
      /   \
     A     B
```

Output F = A XOR B. Maps to two CNOT gates (0 T-gates, "free").

---

## Circuit Diagrams

### Fig. 4: XOR Circuit

XOR node circuit: two CNOTs from each child to a fresh output qubit.

```
     ----+--------+----
         |        |
     ----|-+------|-+--
         | |      | |
     ----|-|------|-|--
         | |      | |
         | |      | |
     ----B-|------A-|--
         | |      | |
         | |      | |
  out ---[+]-----[+]---
```

Simplified view (3 qubit lines):

```
  A  --------*-----------
             |
  B  -----*--|-----------
          |  |
  out ----+--+-----  (out = A XOR B)
         CNOT CNOT
```

The output qubit starts at |0> and accumulates: out ^= B, then out ^= A, giving out = A XOR B.

---

### Fig. 5: Relative-Phase Toffoli — iZ Decomposition

Left side: CCZ gate with iZ phase on target. Right side: decomposition into Clifford+T.

```
                                  +-----+
  a  ---*---------    ---*---*---[+]--T--*---[+]---
        |                |   |         |  |
  b  ---*---------  = ---|-[+]---*--T'+--[+]---*---
        |                |       |            |
  c  --iZ---------    --T'----[+]--------T--[+]---
```

4 T-gates total: T, T', T, T' (where T' = Tdg).

---

### Fig. 6: Relative-Phase Toffoli — iZ-dagger Decomposition

AND case: both A and B are primary inputs (Algorithm 1, line 22).

```
                                       +-----+
  a  ---*---------    ----*----[+]--T'--*---[+]---
        |                 |          |   |
  b  ---*---------  = --[+]---*---T-[+]--*--------
        |                      |          |
  c  --iZ'--------    --------[+]-----T'-[+]---T--
```

4 T-gates total. This is the cheapest AND decomposition (both inputs are simple).

---

### Fig. 7: Relative-Phase Toffoli — i*omega*Z Decomposition

```
                                  +-----+
  a  ---*---------    ---*---*---[+]--T--*---[+]---
        |                |   |         |  |
  b  ---*---------  = ---|-[+]---*--T'+--[+]---*---
        |                |       |             |
  c  --iwZ--------    --[+]------T---------[+]---T--
```

4 T-gates total.

---

### Fig. 8: Relative-Phase Toffoli — i*omega*Z-dagger Decomposition

AND case: one of A, B is primary input (Algorithm 1, line 25).

```
                                      +-----+
  a  ---*---------    ---[+]---*---T'--[+]---*---[+]---
        |                       |        |    |
  b  ---*---------  = ---*---[+]-----T-[+]--[+]---*----
        |                |                         |
  c  --iwZ'-------    --[+]---T'---------[+]-----T'---
```

4 T-gates total. Used for internal AND nodes where one child is a PI and the other is a sub-circuit.

---

### Fig. 1: AND Case — Output is Overall Circuit Output

When the AND node's output is the overall primary output, relative phase must be
corrected exactly. Uses an ancilla |0>, Hadamard gates, and Z/Z-dagger phase corrections.

```
  x_0   -----+---------+---------+---------+-------
  x_1   -----|---------|---------|---------+-------
  a_0   -----|---------|---------|---------+-------
              |         |         |         |
  ...        +---------+---------+---------+
              |  A      |         | A-dgr  |
  x_n-2 -----+---------+---------+---------+-------
                        |         |
  B      ---------------*---------*-----------------
                        |         |
  a_i    ----[Z]-------[+]------[Z']------[+]------
                        |         |
  |0>    ----[H]--------*---------*--------[H]-----
```

Reading left to right:

1. `H` on the |0> ancilla qubit (creates |+>)
2. `Z` on the output qubit a_i (phase correction)
3. `A` block: compute the complex sub-circuit on x_0..x_n-2 lines
4. Toffoli: B and |0> ancilla control a_i (uses Fig. 6 since both are simple)
5. `Z-dagger` on a_i (phase correction)
6. `A-dagger` block: uncompute the complex sub-circuit
7. Toffoli: B and |0> ancilla control a_i (second application)
8. CNOT from complex child output to a_i
9. `H` on the |0> ancilla (back to computational basis)

The two Toffolis inside this circuit use Fig. 6 decomposition (both controls — B and |0> ancilla — are simple/available qubits).

---

### Fig. 9: Alternative AND Construction (F with G Control)

```
  x_0  ------+---------+-------
  x_1  ------|---------+-------
              |  F      |
  ...        +---------+
              |         |
  x_n  ------+---------+-------
                        |
  G    ---------*-------*-------*-----------
                |       |       |
  a_0  ---[iZ]-[+]----[+]----[iZ']---------[+]---
                |       |                    |
  |0>  ---[H]--*-------*-----------[H]------*-----
```

This shows a variant construction where:
- F is a sub-circuit block on input qubits
- G is a control input (primary input)
- a_0 has iZ and iZ-dagger gates around controlled operations
- |0> ancilla with H gates provides the phase kickback mechanism

---

## Summary

| Condition | Figure | T-count | Extra qubits | Notes |
|-----------|--------|---------|--------------|-------|
| XOR node | Fig. 4 | 0 | 1 (output) | Two CNOTs, "free" |
| AND, both PIs | Fig. 6 | 4 | 1 (ancilla) | Cheapest AND case |
| AND, overall output | Fig. 1 | 8+ | 2 (ancilla + |0>) | Phase-exact, uses two Fig. 6 inside |
| AND, one PI (internal) | Fig. 8 | 4 | 1 (ancilla) | + compute/uncompute of complex child |
| AND, both complex, not output | — | — | — | Error (Algorithm 1, line 27) |
