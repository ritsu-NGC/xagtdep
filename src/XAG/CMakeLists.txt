# CMakeLists.txt for XAG module

# Create shared library (LLVM plugin)
add_library(XAGLib MODULE
    XAG.cpp
)

# For MODULE libraries, don't link LLVM libraries (they're already in opt)
# Just use include directories
target_include_directories(XAGLib PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${LLVM_INCLUDE_DIRS}
)

# Set output directory - no visibility preset for plugin exports
set_target_properties(XAGLib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

# Create object library for tests
add_library(XAGObj OBJECT
    XAG.cpp
)

llvm_map_components_to_libnames(llvm_libs core support)
target_link_libraries(XAGObj ${llvm_libs})
target_include_directories(XAGObj PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
)
