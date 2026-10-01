#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

inline size_t usbSfVarint(uint8_t *out, uint32_t n) {
    size_t used = 0;
    do {
        out[used++] = (n & 127) | (n > 127 ? 128 : 0);
        n >>= 7;
    } while (n);
    return used;
}

// Caller supplies at least 205 bytes. This is the same encoder exercised by the native harness.
inline size_t usbSfText(uint8_t *out, const uint8_t *text, size_t size, uint32_t originalId) {
    if (!originalId || !size || size > 192) return 0;
    out[0] = 8; out[1] = 9; out[2] = 42; // rr=ROUTER_TEXT_BROADCAST, field 5=text
    size_t used = 3 + usbSfVarint(out + 3, static_cast<uint32_t>(size));
    memcpy(out + used, text, size);
    used += size;
    out[used++] = 48; // field 6=original_id, absent in pinned generated schema
    return used + usbSfVarint(out + used, originalId);
}
