#pragma once

#include <cstdint>
#include <string>

namespace xagtdep {
namespace test {

bool runOneXag(uint32_t pis, uint32_t ands, uint32_t xors, uint64_t seed,
               int idx, const std::string &dataDir,
               const std::string &method);

}
}
