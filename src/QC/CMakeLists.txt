# CMakeLists.txt for QC module

option(XAGTDEP_ENABLE_PYTHON "Enable Qiskit Python integration for QASM output" OFF)

# Source files
set(QC_SOURCES
    QC.cpp
    XAGToGateList.cpp
    ExistingMethod.cpp
    ProposedMethod.cpp
    RandomXAG.cpp
)

if(XAGTDEP_ENABLE_PYTHON)
    find_package(Python3 REQUIRED COMPONENTS Development)
    list(APPEND QC_SOURCES PythonBridge.cpp)
endif()

# Create shared library (LLVM plugin)
add_library(QCLib MODULE ${QC_SOURCES})

# For MODULE libraries, don't link LLVM libraries (they're already in opt)
# Just use include directories
target_link_libraries(QCLib PRIVATE caterpillar mockturtle)
target_include_directories(QCLib PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${LLVM_INCLUDE_DIRS}
)

if(XAGTDEP_ENABLE_PYTHON)
    target_link_libraries(QCLib PRIVATE Python3::Python)
    target_compile_definitions(QCLib PRIVATE
        XAGTDEP_ENABLE_PYTHON
        QC_PYTHON_SCRIPT_DIR="${CMAKE_CURRENT_SOURCE_DIR}"
    )
endif()

# Set output directory - no visibility preset for plugin exports
set_target_properties(QCLib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

# Create object library for tests
add_library(QCObj OBJECT ${QC_SOURCES})

llvm_map_components_to_libnames(llvm_libs core support)
target_link_libraries(QCObj ${llvm_libs} caterpillar mockturtle)
target_include_directories(QCObj PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
)

if(XAGTDEP_ENABLE_PYTHON)
    target_link_libraries(QCObj Python3::Python)
    target_compile_definitions(QCObj PRIVATE
        XAGTDEP_ENABLE_PYTHON
        QC_PYTHON_SCRIPT_DIR="${CMAKE_CURRENT_SOURCE_DIR}"
    )
endif()
