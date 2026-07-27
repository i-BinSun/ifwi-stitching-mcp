import struct
from pathlib import Path
import pytest
from ifwi_mcp import oem_parser
from ifwi_mcp.result import ErrorCode

STRUCT_FMT = "<16s 48s 128s 32s 32s"


def make_oem_bytes(product="OKSDCRB1", version=".2026.28.3.01",
                   flavor="_1P0_NonIPClean_Trace_DebugSigned",
                   hash_hex="90" * 32):
    return struct.pack(
        STRUCT_FMT,
        product.encode().ljust(16, b"\x00"),
        version.encode().ljust(48, b"\x00"),
        flavor.encode().ljust(128, b"\x00"),
        bytes.fromhex(hash_hex).ljust(32, b"\x00"),
        b"\x00" * 32,
    )


def make_ifwi_file(path: Path, oem_bytes: bytes):
    data = bytearray(b"\xff" * (oem_parser.OEM_INFO_START + oem_parser.OEM_INFO_LEN))
    data[oem_parser.OEM_INFO_START:oem_parser.OEM_INFO_START + 256] = oem_bytes
    path.write_bytes(data)
    return path


def test_decode_entry_official():
    entry = oem_parser.decode_entry(make_oem_bytes())
    assert entry["product"] == "OKSDCRB1"
    assert entry["ifwi_version"] == ".2026.28.3.01"
    assert entry["flavor_value"] == "_1P0_NonIPClean_Trace_DebugSigned"
    assert entry["flavor_type"] == "official"
    assert entry["hash"] == "90" * 32


def test_decode_entry_customized():
    entry = oem_parser.decode_entry(make_oem_bytes(flavor="yyao7.20250507.130527"))
    assert entry["flavor_type"] == "customized"


def test_decode_entry_invalid_version():
    entry = oem_parser.decode_entry(make_oem_bytes(version="2026.28"))
    assert entry["flavor_type"] == "invalid"


def test_decode_entry_wrong_length_raises():
    with pytest.raises(ValueError):
        oem_parser.decode_entry(b"\x00" * 100)


def test_parse_ifwi_oem_success(tmp_path):
    f = make_ifwi_file(tmp_path / "ifwi.bin", make_oem_bytes())
    result = oem_parser.parse_ifwi_oem(str(f))
    assert result["ok"] is True
    assert result["data"]["product"] == "OKSDCRB1"


def test_parse_ifwi_oem_missing_file(tmp_path):
    result = oem_parser.parse_ifwi_oem(str(tmp_path / "nope.bin"))
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_parse_ifwi_oem_too_small(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(b"\x00" * 100)
    result = oem_parser.parse_ifwi_oem(str(small))
    assert result["error_code"] == ErrorCode.OEM_REGION_NOT_FOUND


def test_parse_ifwi_oem_empty_product_is_parse_failed(tmp_path):
    f = make_ifwi_file(tmp_path / "ifwi.bin", make_oem_bytes(product=""))
    result = oem_parser.parse_ifwi_oem(str(f))
    assert result["error_code"] == ErrorCode.OEM_PARSE_FAILED
    assert "raw_snippet" in result["detail"]
