// XAGOptimizerTest.cpp
#include "XAGOptimizer.h"

using namespace dagtdep;

void testXAGOptimizer() {
    // Test the legacy API
    XAGOptimizer analyzer;
    analyzer.analyze();
    
    // Test the registration
    XAGOptimizer::registerPass();
}

int main() {
    testXAGOptimizer();
    return 0;
}
