import json
from pathlib import Path
import struct

import pytest

from meshmemo_builder.build import app_partition, read_prepared
from meshmemo_builder.pipeline import BuilderError, resolve_plan, sha256


def test_partition_budget_comes_from_generated_binary(tmp_path):
    table = tmp_path / "partitions.bin"
    table.write_bytes(struct.pack("<HBBII16sI", 0x50AA, 1, 2, 0x9000, 0x5000, b"nvs", 0)
                      + struct.pack("<HBBII16sI", 0x50AA, 0, 0x10, 0x10000, 0x330000, b"app0", 0)
                      + b"\xff" * 32)
    assert app_partition(table) == {"name": "app0", "offset": 0x10000, "size": 0x330000}


@pytest.mark.parametrize("raw", [b"", b"\xff" * 32, b"truncated"])
def test_invalid_partition_table_is_rejected(tmp_path, raw):
    table = tmp_path / "partitions.bin"
    table.write_bytes(raw)
    with pytest.raises(BuilderError):
        app_partition(table)


def test_modified_source_cannot_be_built_as_original_plan(tmp_path):
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    source = firmware / "test.cpp"
    source.write_bytes(b"original")
    state = {"status": "prepared", "plan": resolve_plan(), "source_sha256": {"test.cpp": sha256(source)}}
    state["plan"]["hardware"]["status"]["prepare"] = "older-advisory-status"
    state["plan"]["hardware"]["artifact"] = "legacy-unused-name.bin"
    (tmp_path / "prepare.json").write_text(json.dumps(state), encoding="utf-8")
    assert read_prepared(tmp_path) == state
    source.write_bytes(b"changed")
    with pytest.raises(BuilderError, match="source changed"):
        read_prepared(tmp_path)
