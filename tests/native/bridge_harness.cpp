#include "UsbSfBridge.h"
#include "UsbSfPayload.h"
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

static unsigned sends = 0;
// Fault injection at the radio-adapter boundary; no physical radio is used.
static uint8_t sendResult = 0;
static uint8_t transmit(const uint8_t *, size_t) { ++sends; return sendResult; }

int main(int argc, char **argv)
{
    UsbSfBridge bridge;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "count") { std::cout << std::dec << sends << '\n'; continue; }
        if (line.rfind("result ", 0) == 0) {
            const unsigned result = static_cast<unsigned>(std::stoul(line.substr(7)));
            if (result > 255) return 2;
            sendResult = static_cast<uint8_t>(result);
            std::cout << "result\n";
            continue;
        }
        if (line == "reset") {
            bridge.reset();
            std::cout << "reset\n";
            continue;
        }
        bool hold = line.rfind("hold ", 0) == 0;
        bool payload = line.rfind("payload ", 0) == 0;
        if (payload) line.erase(0, 8);
        if (hold)
            line.erase(0, 5);
        if (line != "take") {
            std::vector<uint8_t> data;
            for (size_t i = 0; i + 1 < line.size(); i += 2)
                data.push_back(static_cast<uint8_t>(std::stoul(line.substr(i, 2), nullptr, 16)));
            if (payload && data.size() > 60) {
                uint8_t out[233];
                uint32_t originalId = uint32_t(data[20]) | uint32_t(data[21]) << 8 | uint32_t(data[22]) << 16 | uint32_t(data[23]) << 24;
                auto size = usbSfText(out, data.data() + 60, data.size() - 60, originalId);
                for (size_t i = 0; i < size; ++i) std::cout << std::hex << std::setw(2) << std::setfill('0') << unsigned(out[i]);
                std::cout << '\n';
                continue;
            }
            bridge.handle(data.data(), data.size(), argc > 1 && std::string(argv[1]) == "--replay" ? transmit : nullptr);
        }
        if (hold || !bridge.available()) {
            std::cout << "none\n";
            continue;
        }
        uint8_t reply[UsbSfBridge::RESPONSE_SIZE];
        bridge.takeReply(reply);
        for (auto byte : reply)
            std::cout << std::hex << std::setw(2) << std::setfill('0') << unsigned(byte);
        std::cout << '\n';
    }
}
