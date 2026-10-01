#pragma once
#include <cstdlib>
#include <cstdint>

// rweather/Crypto HOST_BUILD expects POSIX endian helpers.
#define __LITTLE_ENDIAN 1234
#define __BYTE_ORDER __LITTLE_ENDIAN
#define htole16(x) (x)
#define htole32(x) (x)
#define htole64(x) (x)
#define le16toh(x) (x)
#define le32toh(x) (x)
#define le64toh(x) (x)
#define htobe16(x) _byteswap_ushort(x)
#define htobe32(x) _byteswap_ulong(x)
#define htobe64(x) _byteswap_uint64(x)
#define be16toh(x) htobe16(x)
#define be32toh(x) htobe32(x)
#define be64toh(x) htobe64(x)
