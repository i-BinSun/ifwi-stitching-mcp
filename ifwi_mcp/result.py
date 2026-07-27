"""Unified result shape shared by every module and MCP tool."""
from typing import Optional


class ErrorCode:
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    MISSING_TOKEN = "MISSING_TOKEN"
    AUTH_FAILED = "AUTH_FAILED"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    MULTIPLE_SWIMLANES = "MULTIPLE_SWIMLANES"
    INGREDIENT_NOT_FOUND = "INGREDIENT_NOT_FOUND"
    STITCH_TOOL_NOT_FOUND = "STITCH_TOOL_NOT_FOUND"
    OEM_REGION_NOT_FOUND = "OEM_REGION_NOT_FOUND"
    OEM_PARSE_FAILED = "OEM_PARSE_FAILED"
    OEM_MATCH_AMBIGUOUS = "OEM_MATCH_AMBIGUOUS"
    OEM_MATCH_NONE = "OEM_MATCH_NONE"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    EXTRACT_FAILED = "EXTRACT_FAILED"
    VENV_SETUP_FAILED = "VENV_SETUP_FAILED"
    STITCH_RUN_FAILED = "STITCH_RUN_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def err(error_code: str, message: str, detail: Optional[dict] = None) -> dict:
    return {"ok": False, "error_code": error_code, "message": message, "detail": detail or {}}
