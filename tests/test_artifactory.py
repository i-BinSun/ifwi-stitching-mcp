import responses
from ifwi_mcp import artifactory
from ifwi_mcp.result import ErrorCode


def test_rejects_empty(clean_env):
    assert artifactory.download("")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_rejects_traversal_dest_name(clean_env):
    r = artifactory.download("https://a/b.bin", dest_name="../evil")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert r["detail"]["param"] == "dest_name"


def test_local_missing_path(clean_env):
    assert artifactory.download("/no/such/file.bin")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_local_copy(clean_env, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    r = artifactory.download(str(src), category="ifwi")
    assert r["ok"] is True
    assert r["data"]["source"] == "copy"
    assert r["data"]["bytes"] == 5
    assert (clean_env / "cache" / "ifwi" / "src.bin").read_bytes() == b"hello"


def test_url_missing_token(clean_env, monkeypatch):
    monkeypatch.delenv("ARTIFACTORY_TOKEN", raising=False)
    r = artifactory.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.MISSING_TOKEN


@responses.activate
def test_url_download_success(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", body=b"binary", status=200)
    r = artifactory.download("https://artifactory/x.bin", category="ingredients")
    assert r["ok"] is True
    assert r["data"]["source"] == "download"
    assert (clean_env / "cache" / "ingredients" / "x.bin").read_bytes() == b"binary"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_truncated_download_is_rejected(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", body=b"short", status=200,
                  headers={"Content-Length": "4096"})
    r = artifactory.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.DOWNLOAD_FAILED
    # nothing half-written may survive to be served as cache
    assert not (clean_env / "cache" / "ifwi" / "x.bin").exists()


class _ShortBody:
    """A response that hands back fewer bytes than it advertises, without raising."""
    status_code = 200
    headers = {"Content-Length": "4096"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=0):
        yield b"short"


def test_short_body_against_content_length_is_rejected(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    monkeypatch.setattr(artifactory.requests, "get", lambda *a, **k: _ShortBody())
    r = artifactory.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.DOWNLOAD_FAILED
    assert r["detail"]["expected_bytes"] == 4096
    assert r["detail"]["received_bytes"] == 5
    assert not (clean_env / "cache" / "ifwi" / "x.bin").exists()


@responses.activate
def test_url_403_is_auth_failed(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", status=403)
    r = artifactory.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.AUTH_FAILED
    assert r["detail"]["http_status"] == 403


@responses.activate
def test_url_500_is_download_failed(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", status=500)
    r = artifactory.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.DOWNLOAD_FAILED


def test_list_local_files(clean_env, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"xy")
    artifactory.download(str(src), category="ifwi")
    listing = artifactory.list_local_files()
    assert listing["ok"] is True
    names = [f["path"].split("/")[-1] for f in listing["data"]["files"]]
    assert "a.bin" in names


def test_local_copy_is_skipped_on_second_call(clean_env, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    first = artifactory.download(str(src), category="ifwi")
    assert first["data"]["source"] == "copy"
    src.unlink()  # prove the second call doesn't re-read the source at all
    second = artifactory.download(str(src), category="ifwi")
    assert second["ok"] is True
    assert second["data"]["source"] == "cache"
    assert second["data"]["local_path"] == first["data"]["local_path"]


@responses.activate
def test_url_download_is_skipped_on_second_call(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/y.bin", body=b"binary", status=200)
    first = artifactory.download("https://artifactory/y.bin", category="ingredients")
    assert first["data"]["source"] == "download"
    second = artifactory.download("https://artifactory/y.bin", category="ingredients")
    assert second["data"]["source"] == "cache"
    assert len(responses.calls) == 1  # no second HTTP request


@responses.activate
def test_upload_file_success(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("IFWI_ARTIFACTORY_TOKEN", "tok")
    src = tmp_path / "out.bin"
    src.write_bytes(b"output data")

    responses.add(responses.PUT, "https://artifactory/repo/run/out.bin", status=201)
    res = artifactory.upload_file(str(src), "https://artifactory/repo/run/out.bin")
    assert res["ok"] is True
    assert res["data"]["url"] == "https://artifactory/repo/run/out.bin"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"


def test_upload_file_missing_local(clean_env):
    res = artifactory.upload_file("/nonexistent/file.bin", "https://artifactory/repo/out.bin")
    assert res["error_code"] == ErrorCode.INVALID_ARGUMENT


@responses.activate
def test_read_range_partial_content(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/i.bin", body=b"OEM!", status=206)
    res = artifactory.read_range("https://artifactory/i.bin", 0xF00, 4)
    assert res["data"]["content"] == b"OEM!"
    assert responses.calls[0].request.headers["Range"] == "bytes=3840-3843"


@responses.activate
def test_read_range_without_server_support_slices_full_body(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/i.bin", body=b"0123456789", status=200)
    res = artifactory.read_range("https://artifactory/i.bin", 3, 4)
    assert res["data"]["content"] == b"3456"


@responses.activate
def test_read_range_short_file_and_auth(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/short.bin", status=416)
    responses.add(responses.GET, "https://artifactory/denied.bin", status=401)
    assert artifactory.read_range("https://artifactory/short.bin", 0xF00, 256)["data"]["content"] == b""
    assert artifactory.read_range("https://artifactory/denied.bin", 0xF00, 256)["error_code"] == ErrorCode.AUTH_FAILED
