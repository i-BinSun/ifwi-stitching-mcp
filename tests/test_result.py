from ifwi_mcp.result import ok, err, ErrorCode


def test_ok_wraps_data():
    assert ok({"x": 1}) == {"ok": True, "data": {"x": 1}}


def test_err_defaults_detail_to_empty_dict():
    result = err(ErrorCode.INVALID_ARGUMENT, "bad param")
    assert result == {
        "ok": False,
        "error_code": "INVALID_ARGUMENT",
        "message": "bad param",
        "detail": {},
    }


def test_err_keeps_detail():
    result = err(ErrorCode.MISSING_TOKEN, "need token", {"which": "fiv"})
    assert result["detail"] == {"which": "fiv"}
    assert result["error_code"] == "MISSING_TOKEN"


def test_error_code_constants_are_stable_strings():
    assert ErrorCode.MULTIPLE_SWIMLANES == "MULTIPLE_SWIMLANES"
    assert ErrorCode.STITCH_RUN_FAILED == "STITCH_RUN_FAILED"
