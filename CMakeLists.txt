# CMakeLists.txt
cmake_minimum_required(VERSION 3.14) # FetchContent is better supported in 3.14+
project(dagtdep)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_CXX_EXTENSIONS False)

include(FetchContent)

set(CATERPILLAR_TEST OFF CACHE BOOL "" FORCE)
set(CATERPILLAR_EXAMPLES OFF CACHE BOOL "" FORCE)
set(CATERPILLAR_EXPERIMENTS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  caterpillar
  GIT_REPOSITORY https://github.com/gmeuli/caterpillar.git
  GIT_TAG        master
)
FetchContent_MakeAvailable(caterpillar)

# caterpillar's interface target links mockturtle, tweedledum, json —
# but not 'ez', which is needed when including xag_mapping_strategy.hpp.
target_link_libraries(caterpillar INTERFACE ez)

add_subdirectory(src)
add_subdirectory(test)