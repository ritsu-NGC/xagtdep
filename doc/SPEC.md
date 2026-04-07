# xagtdep

LLVM-integrated analysis and optimization modules for DAG-based transformations.

## Overview

This project contains five LLVM-integrated modules for the synthesis of quantum oracles
with a focus on T-count and T-depth optimization

- **DavioDecomposition**: Davio decomposition analysis
- **Function**: Function transformation operations
- **NewMethod**: New method to generate input XAGs to optimization
- **QC**: Quantum Circuit generation from XAG
- **XAG**: XAG optimization algorithms

All modules are implemented as dynamically loadable LLVM passes that can be used with LLVM's `opt` tool or integrated into custom LLVM-based workflows.

## XAG

XAG implements the Meuli low-depth strategy from [Meuli et al. 2022](https://doi.org/10.1038/s41534-021-00514-y) to optimize
XAG for AND depth, which optimizes for T-depth. This is implemented in the [caterpillar library](https://github.com/gmeuli/caterpillar)

Inputs and Outputs from this module will be in mockturtle::xag_network format that caterpillar uses

## QC

Input is an XAG and output is a QASM file for a quantum circuit. Uses Qiskit to process quantum circuits.

Conversion from an XAG to a quantum circuit will occur according to the following algorithm

## Pseudocode
```text
function XAG2QC(xag):
    QC = new() //full circuit
    A  = new() //A branch circuit
    B = new()  //B branch circuit
    Traverse the XAG from the output, depth-first to the inputs
        if XOR node:
            Append the Below Circuit
	            +---+     +----+
	            |   |     |    |
	      ------|   |-----|    |------
	            | B |     | A  |      
	      ------|   |-----|    |------
	            |   |     |    |      
	      ------|   |-----|    |------
	            +---+     +----+
	             |          |
	             |          |
	      ------(+)--------(+)--------
	                                 (circled plus)	    
	else if AND node:
	     if A and B inputs are both primary inputs or ancilla:
	     	append the below circuit
	            
	      A      ------*---
	                   |
	      B      ------*---
	                   |      
	                   |      
	      a_out  -----(+)-----

	      if one of A or B is a primary input, and the other is
	      an input from a node, where A is the circuit you get from the
	      primary node, add ancilla a_k, and generate the following circuit
	      and append it to QC
		
                           +----+        +----+      
	     --------------|    |--------|    |-----
                           |    |        |    |     
	     --------------| A  |--------| A  |-----
	                   |    |        |    |
	                   +----+        +----+
	  B  --------*------ | -----*----- | -------
	             |       |      |      |
	             |       |      |      |        
	             |       |      |      |        
	 a_k --------* -----(+)-----*-----(+)-------
	             |              |  	            
	             |              |  	            
	             |              |  	            
	a_out -------(+)------------(+)--------------
	     
    return QC
```

##QC_Prop


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.
