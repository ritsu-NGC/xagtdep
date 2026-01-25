# CMakeLists.txt for Function module

# Create shared library (LLVM plugin)
add_library(FunctionLib MODULE
    Function.cpp
)

# For MODULE libraries, don't link LLVM libraries (they're already in opt)
# Just use include directories
target_include_directories(FunctionLib PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${LLVM_INCLUDE_DIRS}
)

# Set output directory - no visibility preset for plugin exports
set_target_properties(FunctionLib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

# Create object library for tests
add_library(FunctionObj OBJECT
    Function.cpp
)

llvm_map_components_to_libnames(llvm_libs core support)
target_link_libraries(FunctionObj ${llvm_libs})
target_include_directories(FunctionObj PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
)
