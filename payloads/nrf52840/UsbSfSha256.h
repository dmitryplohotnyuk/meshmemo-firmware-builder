#pragma once

#include <cstddef>
#include <cstdint>

#if defined(ARCH_NRF52) && defined(NRF52840_XXAA)
#include <SHA256.h>
inline bool usbSfSha256(const uint8_t *input, size_t length, uint8_t output[32])
{
    SHA256 hash;
    hash.update(input, length);
    hash.finalize(output, 32);
    hash.clear();
    return true;
}
#else
#error MeshMemo nRF52840 SHA-256 adapter requires nRF52840
#endif
