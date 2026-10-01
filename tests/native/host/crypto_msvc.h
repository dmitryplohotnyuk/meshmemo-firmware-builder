#pragma once
#include <cstdlib>
#include "utility/RotateUtil.h"

// Replace GCC statement expressions with equivalent MSVC rotate intrinsics.
#undef leftRotate
#undef rightRotate
#define leftRotate(a, bits) _rotl((a), (bits))
#define rightRotate(a, bits) _rotr((a), (bits))
