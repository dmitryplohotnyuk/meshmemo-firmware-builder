#include "configuration.h"
#ifdef MESHTASTIC_USB_SF_BRIDGE
#include "UsbSfRadio.h"
#include "UsbSfPayload.h"
#include "Channels.h"
#include "MeshService.h"
#include "NodeDB.h"
#include "Router.h"
#include "airtime.h"
#include "mesh/generated/meshtastic/storeforward.pb.h"
#include "UsbSfSha256.h"
#include <cstring>

namespace {
uint32_t read32(const uint8_t *p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
bool validContext(uint32_t index, const uint8_t *expected) {
    if (index >= channels.getNumChannels())
        return false;
    const auto &ch = channels.getByIndex(index);
    const uint8_t publicPrefix[15] = {0xd4,0xf1,0xbb,0x3a,0x20,0x29,0x07,0x59,0xf0,0xbc,0xff,0xab,0xcf,0x4e,0x69};
    if (!ch.role || (ch.settings.psk.size != 16 && ch.settings.psk.size != 32) ||
        (ch.settings.psk.size == 16 && !memcmp(ch.settings.psk.bytes, publicPrefix, 15)))
        return false;
    uint8_t material[4 + meshtastic_ChannelSettings_size + meshtastic_Config_LoRaConfig_size] = {};
    size_t chSize = pb_encode_to_bytes(material + 4, meshtastic_ChannelSettings_size,
                                      &meshtastic_ChannelSettings_msg, &ch.settings);
    if (!chSize) return false;
    for (unsigned i = 0; i < 4; ++i) material[i] = (chSize >> (8 * i)) & 255;
    size_t loSize = pb_encode_to_bytes(material + 4 + chSize, meshtastic_Config_LoRaConfig_size,
                                      &meshtastic_Config_LoRaConfig_msg, &config.lora);
    uint8_t digest[32];
    bool ok = usbSfSha256(material, 4 + chSize + loSize, digest) && !memcmp(digest, expected, 16);
    memset(material, 0, sizeof(material));
    return ok;
}
} // namespace

// USB-only host-encrypted replies. Keys remain on the host; the firmware cannot
// inspect ciphertext. The authenticated local service is the authorization boundary.
// No broadcast, no ACK requests, bounded payload and live LoRa context binding.
static uint8_t sendServerChannel(const uint8_t *command, size_t size) {
    if (size <= 48 || size > 256 || !router || !airTime || !config.lora.tx_enabled || moduleConfig.store_forward.enabled)
        return 7;
    const uint32_t sender = read32(command + 16), target = read32(command + 20);
    if (!sender || sender == UINT32_MAX || !target || target == UINT32_MAX || target == nodeDB->getNodeNum() ||
        !read32(command + 24) || read32(command + 28) > 255) return 1;
    uint8_t encoded[meshtastic_Config_LoRaConfig_size] = {}, digest[32];
    const size_t length = pb_encode_to_bytes(encoded, sizeof(encoded), &meshtastic_Config_LoRaConfig_msg, &config.lora);
    if (!usbSfSha256(encoded, length, digest) || memcmp(digest, command + 32, 16)) return 1;
    if (!airTime->isTxAllowedChannelUtil(true) || !airTime->isTxAllowedAirUtil() || router->getQueueStatus().free < 2) return 6;
    auto *p = router->allocForSending();
    if (!p) return 6;
    p->from = sender; p->to = target; p->id = read32(command + 24); p->channel = read32(command + 28);
    p->which_payload_variant = meshtastic_MeshPacket_encrypted_tag;
    p->encrypted.size = size - 48;
    memcpy(p->encrypted.bytes, command + 48, size - 48);
    p->priority = meshtastic_MeshPacket_Priority_BACKGROUND;
    p->want_ack = false;
    // Skip sendLocal's channel-index inference: this field already holds an RF hash.
    return router->send(p) == ERRNO_OK ? 0 : 8;
}

uint8_t usbSfSend(const uint8_t *command, size_t size) {
    if (command[4] == 5) return sendServerChannel(command, size);
    const bool replay = command[4] == 2;
    const uint32_t target = read32(command + (replay ? 24 : 16));
    const uint32_t channel = read32(command + (replay ? 28 : 20));
    const uint32_t outerId = read32(command + (replay ? 40 : 24));
    const uint8_t *context = command + (replay ? 44 : 28);
    if (target == nodeDB->getNodeNum() || !validContext(channel, context))
        return 1;
    // A native S&F instance must not answer alongside this external server.
    if (moduleConfig.store_forward.enabled || !router || !airTime || !config.lora.tx_enabled)
        return 7;
    if (!airTime->isTxAllowedChannelUtil(true) || !airTime->isTxAllowedAirUtil() || router->getQueueStatus().free < 2)
        return 6;
    uint8_t payload[233] = {};
    size_t length;
    if (replay) {
        length = usbSfText(payload, command + 60, size - 60, read32(command + 20));
        if (!length) return 1;
    } else {
        length = size - 44;
        meshtastic_StoreAndForward sf = meshtastic_StoreAndForward_init_zero;
        if (!pb_decode_from_bytes(command + 44, length, &meshtastic_StoreAndForward_msg, &sf))
            return 1;
        // Control cannot smuggle a text response or arbitrary application traffic.
        if (sf.rr != meshtastic_StoreAndForward_RequestResponse_ROUTER_HEARTBEAT &&
            sf.rr != meshtastic_StoreAndForward_RequestResponse_ROUTER_PONG &&
            sf.rr != meshtastic_StoreAndForward_RequestResponse_ROUTER_BUSY &&
            sf.rr != meshtastic_StoreAndForward_RequestResponse_ROUTER_HISTORY &&
            sf.rr != meshtastic_StoreAndForward_RequestResponse_ROUTER_STATS)
            return 1;
        if ((target == UINT32_MAX && (sf.rr != meshtastic_StoreAndForward_RequestResponse_ROUTER_HEARTBEAT ||
             sf.which_variant != meshtastic_StoreAndForward_heartbeat_tag || sf.variant.heartbeat.period < 900)) ||
            sf.which_variant == meshtastic_StoreAndForward_text_tag)
            return 1;
        memcpy(payload, command + 44, length);
    }
    meshtastic_MeshPacket *p = router->allocForSending();
    if (!p) return 6;
    p->to = target;
    p->channel = channel;
    p->id = outerId;
    p->decoded.portnum = meshtastic_PortNum_STORE_FORWARD_APP;
    p->decoded.payload.size = length;
    memcpy(p->decoded.payload.bytes, payload, length);
    p->priority = meshtastic_MeshPacket_Priority_BACKGROUND;
    p->want_ack = false;
    p->decoded.want_response = false;
    if (replay) {
        p->from = read32(command + 16);
        p->decoded.reply_id = read32(command + 32);
        p->decoded.emoji = read32(command + 36);
    }
    service->sendToMesh(p);
    // Acceptance by the local service is not a delivery acknowledgement.
    return 0;
}
#endif
