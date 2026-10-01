#pragma once

constexpr bool usbSfLocalTransportAllowed()
{
#if defined(ARCH_NRF52) && defined(NRF52840_XXAA) && defined(USE_TINYUSB) && MESHMEMO_LOCAL_SERIAL == 1 && \
    !defined(USER_DEBUG_PORT) && !defined(RP2040_SLOW_CLOCK)
    return true;
#else
    return false;
#endif
}
