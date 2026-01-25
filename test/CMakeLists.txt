# CMakeLists.txt for test directory

# Find LLVM
find_package(LLVM REQUIRED CONFIG)
include_directories(${LLVM_INCLUDE_DIRS})
include_directories(${CMAKE_SOURCE_DIR}/include)

# Get LLVM libraries
llvm_map_components_to_libnames(llvm_libs core support)

# Helper function to create test executables
function(add_module_test test_name module_name)
    add_executable(${test_name} ${test_name}.cpp)
    target_link_libraries(${test_name} ${module_name}Obj ${llvm_libs})
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
add_module_test(QCTest QC)
add_module_test(XAGTest XAG)

# Copy test files to build directory
configure_file(test_input.ll ${CMAKE_BINARY_DIR}/test_input.ll COPYONLY)
configure_file(run_passes.sh ${CMAKE_BINARY_DIR}/run_passes.sh COPYONLY)
