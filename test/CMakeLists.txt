# CMakeLists.txt for test directory

enable_testing()

# Find LLVM
find_package(LLVM REQUIRED CONFIG)
include_directories(${LLVM_INCLUDE_DIRS})
include_directories(${CMAKE_SOURCE_DIR}/include)

# Get LLVM libraries
llvm_map_components_to_libnames(llvm_libs core support)

# Helper function to create test executables
# Usage: add_module_test(TestName ModuleName [extra_obj_libs...])
function(add_module_test test_name module_name)
    add_executable(${test_name} ${test_name}.cpp)
    target_link_libraries(${test_name} ${module_name}Obj ${ARGN} ${llvm_libs})
    target_include_directories(${test_name} PRIVATE
        ${CMAKE_SOURCE_DIR}/include
        ${CMAKE_SOURCE_DIR}/src/${module_name}
    )
    set_target_properties(${test_name} PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
    )
endfunction()

# Create test executables
add_module_test(DavioDecompositionTest DavioDecomposition)
add_module_test(FunctionTest Function)
add_module_test(NewMethodTest NewMethod)
add_module_test(QCTest QC NewMethodObj XAGObj)
add_module_test(XAGTest XAG NewMethodObj)

add_test(NAME DavioDecompositionTest COMMAND DavioDecompositionTest)
add_test(NAME FunctionTest COMMAND FunctionTest)
add_test(NAME NewMethodTest COMMAND NewMethodTest)
add_test(NAME QCTest COMMAND QCTest)
add_test(NAME XAGTest COMMAND XAGTest)

# Shared per-XAG runner used by QCVerificationTest and CIQCVerification.
add_library(QCVerificationCommonObj OBJECT QCVerificationCommon.cpp)
target_include_directories(QCVerificationCommonObj PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/src/QC
)
target_link_libraries(QCVerificationCommonObj PRIVATE caterpillar mockturtle)

# Verification test — generates random XAGs and exports JSON for Python verification
add_executable(QCVerificationTest
    QCVerificationTest.cpp
    $<TARGET_OBJECTS:QCVerificationCommonObj>
)
target_link_libraries(QCVerificationTest QCObj NewMethodObj XAGObj ${llvm_libs})
target_include_directories(QCVerificationTest PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/src/QC
)
set_target_properties(QCVerificationTest PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)
add_test(NAME QCVerificationTest COMMAND QCVerificationTest
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

# Strict-lane runner — only runs configs in test/ci_passing_set.json.
# Not registered with ctest; the CI workflow invokes it directly so it can
# chain verify_circuits.py --strict after.
add_executable(CIQCVerification
    CIQCVerification.cpp
    $<TARGET_OBJECTS:QCVerificationCommonObj>
)
target_link_libraries(CIQCVerification QCObj NewMethodObj XAGObj ${llvm_libs})
target_include_directories(CIQCVerification PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/src/QC
)
target_compile_definitions(CIQCVerification PRIVATE
    CI_PASSING_SET_DEFAULT="${CMAKE_SOURCE_DIR}/test/ci_passing_set.json"
)
set_target_properties(CIQCVerification PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
)

# Copy test files to build directory
configure_file(test_input.ll ${CMAKE_BINARY_DIR}/test_input.ll COPYONLY)
configure_file(run_passes.sh ${CMAKE_BINARY_DIR}/run_passes.sh COPYONLY)
