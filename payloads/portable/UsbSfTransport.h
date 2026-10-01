#pragma once

constexpr bool usbSfLocalTransportAllowed()
{
#if defined(USER_DEBUG_PORT) || defined(RP2040_SLOW_CLOCK)
    return false;
#elif MESHMEMO_LOCAL_SERIAL == 1 && defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
    return true;
#elif MESHMEMO_LOCAL_SERIAL == 2 && (!defined(ARDUINO_USB_CDC_ON_BOOT) || !ARDUINO_USB_CDC_ON_BOOT)
    return true;
#else
    return false;
#endif
}
