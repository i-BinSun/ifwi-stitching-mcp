"""Zero-dependency decoder for the IFWI OEM information region.

Copied/adapted from the stitch tool (do NOT import from it):
  - OakStreamAPIfwi/.../Output/defs/structure.py : IFWIEntry.from_binary,
    struct "<16s 48s 128s 32s 32s" (product/ifwi_version/flavor/hash/padding).
  - OakStreamAPIfwi/.../Output/diagnostics/binary_hash.py : parse_oem_info,
    OEM_INFO_START_ADDR = 0xF00, region length 256.
When the upstream OEM structure changes, re-sync this module by hand.
Stdlib only — no network, no subprocess.
"""
import re
import struct

from .result import ok, err, ErrorCode

OEM_INFO_START = 0xF00
OEM_INFO_LEN = 256
_STRUCT_FMT = "<16s 48s 128s 32s 32s"
_DATE_RE = re.compile(r"^[0-9]{8}$")
_TIME_RE = re.compile(r"^[0-9]{6}$")


def _decode_str(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("utf-8", errors="ignore")


def _classify_flavor(ifwi_version: str, flavor_value: str) -> str:
    version_parts = ifwi_version.split(".")
    if len(version_parts) not in (4, 5):
        return "invalid"
    flavor_parts = flavor_value.split(".")
    if len(flavor_parts) >= 3 and _TIME_RE.match(flavor_parts[-1]) and _DATE_RE.match(flavor_parts[-2]):
        return "customized"
    return "official"


def decode_entry(oem_bytes: bytes) -> dict:
    if len(oem_bytes) != OEM_INFO_LEN:
        raise ValueError(f"OEM entry must be {OEM_INFO_LEN} bytes, got {len(oem_bytes)}")
    product_b, version_b, flavor_b, hash_b, _padding = struct.unpack(_STRUCT_FMT, oem_bytes)
    product = _decode_str(product_b)
    ifwi_version = _decode_str(version_b)
    flavor_value = _decode_str(flavor_b)
    return {
        "product": product,
        "ifwi_version": ifwi_version,
        "flavor_value": flavor_value,
        "flavor_type": _classify_flavor(ifwi_version, flavor_value),
        "hash": hash_b.hex(),
    }


def parse_ifwi_oem(local_ifwi_path: str) -> dict:
    try:
        with open(local_ifwi_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size < OEM_INFO_START + OEM_INFO_LEN:
                return err(ErrorCode.OEM_REGION_NOT_FOUND,
                           "file too small to contain the OEM region",
                           {"file": local_ifwi_path, "reason": f"size {size} < {OEM_INFO_START + OEM_INFO_LEN}"})
            fh.seek(OEM_INFO_START)
            oem_bytes = fh.read(OEM_INFO_LEN)
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        return err(ErrorCode.INVALID_ARGUMENT, "cannot read IFWI file",
                   {"param": "local_ifwi_path", "expected": f"readable file ({exc})"})
    try:
        entry = decode_entry(oem_bytes)
    except Exception as exc:  # noqa: BLE001 - map any decode failure
        return err(ErrorCode.OEM_PARSE_FAILED, "failed to decode OEM entry",
                   {"raw_snippet": oem_bytes[:64].hex(), "reason": str(exc)})
    if not entry["product"]:
        return err(ErrorCode.OEM_PARSE_FAILED, "OEM product field is empty",
                   {"raw_snippet": oem_bytes[:64].hex()})
    return ok(entry)
