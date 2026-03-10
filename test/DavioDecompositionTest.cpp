// DavioDecompositionTest.cpp
#include "DavioDecomposition.h"

using namespace xagtdep;

void testDavioDecomposition() {
    // Test the legacy API
    DavioDecomposition analyzer;
    analyzer.analyze();
    
    // Test the registration
    DavioDecomposition::registerPass();
}

int main() {
    testDavioDecomposition();
    return 0;
}
