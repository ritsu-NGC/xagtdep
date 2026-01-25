; Simple test function for LLVM pass testing
; This file can be compiled with: clang -emit-llvm -S test_input.c -o test_input.ll

define i32 @simple_function(i32 %x, i32 %y) {
entry:
  %add = add i32 %x, %y
  %mul = mul i32 %add, 2
  ret i32 %mul
}

define i32 @factorial(i32 %n) {
entry:
  %cmp = icmp sle i32 %n, 1
  br i1 %cmp, label %return, label %recurse

recurse:
  %sub = sub i32 %n, 1
  %call = call i32 @factorial(i32 %sub)
  %mul = mul i32 %n, %call
  ret i32 %mul

return:
  ret i32 1
}

define i32 @main() {
entry:
  %result1 = call i32 @simple_function(i32 5, i32 10)
  %result2 = call i32 @factorial(i32 5)
  %final = add i32 %result1, %result2
  ret i32 %final
}
