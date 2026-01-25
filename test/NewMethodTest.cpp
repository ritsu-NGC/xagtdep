// NewMethodTest.cpp
#include "NewMethod.h"

using namespace dagtdep;

void testNewMethod() {
    // Test the legacy API
    NewMethod method;
    method.apply();
    
    // Test the registration
    NewMethod::registerPass();
}

int main() {
    testNewMethod();
    return 0;
}
