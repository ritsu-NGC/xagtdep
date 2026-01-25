// FunctionTest.cpp
#include "Function.h"

using namespace dagtdep;

void testFunction() {
    // Test the legacy API
    Function transformer;
    transformer.execute();
    
    // Test the registration
    Function::registerPass();
}

int main() {
    testFunction();
    return 0;
}
