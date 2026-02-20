# CMakeLists.txt for XAGOptimizer module

# Create shared library (LLVM plugin)
add_library(XAGOptimizerLib MODULE
    XAGOptimizer.cpp
)

# For MODULE libraries, don't link LLVM libraries (they're already in opt)
# Just use include directories
target_include_directories(XAGOptimizerLib PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${LLVM_INCLUDE_DIRS}
)

# Set output directory - no visibility preset for plugin exports
set_target_properties(XAGOptimizerLib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

# Create object library for tests
add_library(XAGOptimizerObj OBJECT
    XAGOptimizer.cpp
)

llvm_map_components_to_libnames(llvm_libs core support)
target_link_libraries(XAGOptimizerObj ${llvm_libs})
target_include_directories(XAGOptimizerObj PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
)
