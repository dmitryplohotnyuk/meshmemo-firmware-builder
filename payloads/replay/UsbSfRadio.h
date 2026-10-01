#pragma once
#include <cstddef>
#include <cstdint>

// Only passed to the USB PhoneAPI bridge, never BLE/WiFi or received radio data.
uint8_t usbSfSend(const uint8_t *command, size_t size);
