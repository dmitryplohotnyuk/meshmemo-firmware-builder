#include "UsbSfTransport.h"
static_assert(usbSfLocalTransportAllowed() == bool(EXPECT_ALLOWED), "Incorrect local transport gate");
int main() { return 0; }
