"""Validate nRF52840 application UF2 blocks without opening a device."""

import struct

from .pipeline import BuilderError


def application_info(raw: bytes, region: dict) -> dict:
    if not raw or len(raw) % 512:
        raise BuilderError("Invalid UF2 file size")
    count = len(raw) // 512
    blocks = {}
    for index in range(count):
        block = raw[index * 512:(index + 1) * 512]
        magic1, magic2, flags, address, size, number, total, family = struct.unpack_from("<8I", block)
        if (magic1 != 0x0A324655 or magic2 != 0x9E5D5157 or
                struct.unpack_from("<I", block, 508)[0] != 0x0AB16F30):
            raise BuilderError("Invalid UF2 magic")
        if flags != 0x2000 or family != region["uf2_family"]:
            raise BuilderError("UF2 must contain nRF52840 flash blocks with the expected family")
        if total != count or number != index or size != 256 or address % 256:
            raise BuilderError("Invalid UF2 block layout")
        if address < region["offset"] or address + size > region["offset"] + region["size"]:
            raise BuilderError("UF2 writes outside the application region")
        if address in blocks:
            raise BuilderError("Overlapping UF2 blocks")
        blocks[address] = block[32:32 + size]
    if min(blocks) != region["offset"]:
        raise BuilderError("UF2 is missing the application start")
    span = max(blocks) + 256 - region["offset"]
    return {"family": region["uf2_family"], "offset": region["offset"],
            "payload_bytes": count * 256, "span_bytes": span, "blocks": count,
            "application_only": True}
