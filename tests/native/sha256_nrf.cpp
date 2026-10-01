#include "UsbSfSha256.h"
#include <cstdio>
#include <cstring>
#include <string>

int main()
{
    const char *expected[] = {
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"};
    const std::string messages[] = {"", "abc", std::string(1000000, 'a')};
    for (unsigned i = 0; i < 3; ++i) {
        uint8_t digest[32];
        char hex[65];
        if (!usbSfSha256(reinterpret_cast<const uint8_t *>(messages[i].data()), messages[i].size(), digest))
            return 1;
        for (unsigned j = 0; j < 32; ++j)
            std::sprintf(hex + j * 2, "%02x", unsigned(digest[j]));
        if (std::strcmp(hex, expected[i]))
            return 2;
    }
    std::puts("nRF52840 SHA-256 adapter: 3 known-answer vectors passed");
}
