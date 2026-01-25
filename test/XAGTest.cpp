// XAGTest.cpp
#include "XAG.h"

using namespace dagtdep;

void testXAG() {
    // Test the legacy API
    XAG optimizer;
    optimizer.optimize();
    
    // Test the registration
    XAG::registerPass();
}

int main() {
    testXAG();
    return 0;
}
