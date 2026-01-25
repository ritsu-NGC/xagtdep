# CMakeLists.txt for QC module

# Create shared library (LLVM plugin)
add_library(QCLib MODULE
    QC.cpp
)

# For MODULE libraries, don't link LLVM libraries (they're already in opt)
# Just use include directories
target_include_directories(QCLib PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${LLVM_INCLUDE_DIRS}
)

# Set output directory - no visibility preset for plugin exports
set_target_properties(QCLib PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

# Create object library for tests
add_library(QCObj OBJECT
    QC.cpp
)

llvm_map_components_to_libnames(llvm_libs core support)
target_link_libraries(QCObj ${llvm_libs})
target_include_directories(QCObj PRIVATE
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}
)
