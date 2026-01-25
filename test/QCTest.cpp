// QCTest.cpp
#include "QC.h"

using namespace dagtdep;

void testQC() {
    // Test the legacy API
    QC evaluator;
    evaluator.evaluate();
    
    // Test the registration
    QC::registerPass();
}

int main() {
    testQC();
    return 0;
}
