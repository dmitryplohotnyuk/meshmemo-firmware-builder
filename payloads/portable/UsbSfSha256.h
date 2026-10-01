#pragma once

#include <cstddef>
#include <cstdint>

#if defined(ARCH_ESP32)
#include "mbedtls/sha256.h"
inline bool usbSfSha256(const uint8_t *input, size_t length, uint8_t output[32])
{
    return mbedtls_sha256_ret(input, length, output, 0) == 0;
}
#else
#error MeshMemo SHA-256 adapter is not yet validated for this architecture
#endif
