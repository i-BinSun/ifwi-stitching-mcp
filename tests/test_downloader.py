import responses
from ifwi_mcp import downloader
from ifwi_mcp.result import ErrorCode


def test_rejects_empty(clean_env):
    assert downloader.download("")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_rejects_traversal_dest_name(clean_env):
    r = downloader.download("https://a/b.bin", dest_name="../evil")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert r["detail"]["param"] == "dest_name"


def test_local_missing_path(clean_env):
    assert downloader.download("/no/such/file.bin")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_local_copy(clean_env, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    r = downloader.download(str(src), category="ifwi")
    assert r["ok"] is True
    assert r["data"]["source"] == "copy"
    assert r["data"]["bytes"] == 5
    assert (clean_env / "cache" / "ifwi" / "src.bin").read_bytes() == b"hello"


def test_url_missing_token(clean_env, monkeypatch):
    monkeypatch.delenv("ARTIFACTORY_TOKEN", raising=False)
    r = downloader.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.MISSING_TOKEN


@responses.activate
def test_url_download_success(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", body=b"binary", status=200)
    r = downloader.download("https://artifactory/x.bin", category="ingredients")
    assert r["ok"] is True
    assert r["data"]["source"] == "download"
    assert (clean_env / "cache" / "ingredients" / "x.bin").read_bytes() == b"binary"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_url_403_is_auth_failed(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", status=403)
    r = downloader.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.AUTH_FAILED
    assert r["detail"]["http_status"] == 403


@responses.activate
def test_url_500_is_download_failed(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", status=500)
    r = downloader.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.DOWNLOAD_FAILED


def test_list_local_files(clean_env, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"xy")
    downloader.download(str(src), category="ifwi")
    listing = downloader.list_local_files()
    assert listing["ok"] is True
    names = [f["path"].split("/")[-1] for f in listing["data"]["files"]]
    assert "a.bin" in names


def test_local_copy_is_skipped_on_second_call(clean_env, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    first = downloader.download(str(src), category="ifwi")
    assert first["data"]["source"] == "copy"
    src.unlink()  # prove the second call doesn't re-read the source at all
    second = downloader.download(str(src), category="ifwi")
    assert second["ok"] is True
    assert second["data"]["source"] == "cache"
    assert second["data"]["local_path"] == first["data"]["local_path"]


@responses.activate
def test_url_download_is_skipped_on_second_call(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/y.bin", body=b"binary", status=200)
    first = downloader.download("https://artifactory/y.bin", category="ingredients")
    assert first["data"]["source"] == "download"
    second = downloader.download("https://artifactory/y.bin", category="ingredients")
    assert second["data"]["source"] == "cache"
    assert len(responses.calls) == 1  # no second HTTP request
