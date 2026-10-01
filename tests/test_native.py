"""Protocol checks against the prepared C++ bridge, without a radio or server SDK."""

import os
from pathlib import Path
import struct
import subprocess

import pytest


def header(operation, token, session=123):
    return struct.pack("<4sBBHII", b"USF1", operation, 0, 0, session, token)


@pytest.fixture
def native():
    binary = os.environ.get("MESHMEMO_TEST_HARNESS")
    if not binary:
        pytest.skip("Set MESHMEMO_TEST_HARNESS to a compiled bridge_harness")
    assert Path(binary).is_file()

    def request(*commands):
        data = "\n".join(x.hex() if isinstance(x, bytes) else x for x in commands) + "\n"
        return subprocess.run([binary, "--replay"], input=data, text=True, capture_output=True,
                              check=True, timeout=10).stdout.splitlines()
    return request


def test_native_capabilities(native):
    response = bytes.fromhex(native(header(1, 1))[0])
    assert len(response) == 112
    assert response[:6] == b"USF2\x81\x00"
    assert struct.unpack_from("<HBBI", response, 16) == (192, 1, 4, 12)
    assert response[28:68] == b"54e0d8d0ab2ff56b3a9ce967e53f79e49af560fb"
    assert struct.unpack_from("<IHHHH", response, 68) == (31, 256, 100, 208, 1)
    assert any(response[80:112])


def test_native_exact_retry_sends_once_and_conflict_is_rejected(native):
    encrypted = header(5, 2) + struct.pack("<IIII", 111, 222, 333, 23) + bytes(16) + b"ciphertext"
    altered = encrypted[:-1] + b"!"
    result = native(header(1, 1), encrypted, encrypted, altered, "count")
    assert result[1] == result[2]
    assert bytes.fromhex(result[1])[5] == 0
    assert bytes.fromhex(result[3])[5] == 5
    assert result[4] == "1"


def test_native_invalid_broadcast_recipient_never_reaches_sender(native):
    invalid = header(5, 2) + struct.pack("<IIII", 111, 0xFFFFFFFF, 333, 23) + bytes(16) + b"ciphertext"
    result = native(header(1, 1), invalid, "count")
    assert bytes.fromhex(result[1])[5] == 1
    assert result[2] == "0"
