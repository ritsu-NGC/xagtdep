// DavioDecompositionTest.cpp
#include "DavioDecomposition.h"

using namespace dagtdep;

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
