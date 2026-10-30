# tests/test_cli.py
import json

import pytest
import responses

from ifwi_mcp import cli, server
from ifwi_mcp.result import ErrorCode, ok

BASE = "https://fiv.example.com"


@pytest.fixture
def fiv_env(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", BASE)
    monkeypatch.setenv("FIV_TOKEN", "xyz")
    return clean_env


def test_all_expected_commands_registered():
    pairs = {(spec.group, spec.name) for spec in cli.SPECS}
    assert len(cli.SPECS) == 31
    assert (None, "download") in pairs
    assert (None, "list-local-files") in pairs
    assert (None, "extract-archive") in pairs
    assert ("oem", "parse") in pairs
    assert ("fiv", "list-projects") in pairs
    assert ("fiv", "prepare-ifwi") in pairs
    assert ("stitch", "build-plan") in pairs
    assert ("stitch", "execution-config") in pairs
    assert ("cache", "cleanup") in pairs


def test_build_parser_exposes_help_for_every_group():
    parser = cli.build_parser()
    for args in (["--help"], ["fiv", "--help"], ["stitch", "--help"], ["oem", "--help"],
                 ["cache", "--help"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(args)
        assert exc.value.code == 0


def test_missing_required_arg_is_a_usage_error(fiv_env):
    with pytest.raises(SystemExit) as exc:
        cli.main(["fiv", "get-project"])
    assert exc.value.code == 2


def test_download_validation_error_exits_1(fiv_env, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["download", ""])
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_stitch_execution_config_dispatch(fiv_env, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["stitch", "execution-config"])
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["data"]["mode"] == "local"


@responses.activate
def test_fiv_list_projects_dispatch(fiv_env, capsys):
    responses.add(responses.GET, f"{BASE}/app/rest/get_project_info/",
                  json=[{"id": 1, "name": "P", "project_name": "Proj"}], status=200)
    with pytest.raises(SystemExit) as exc:
        cli.main(["fiv", "list-projects"])
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["data"]["projects"][0]["id"] == 1


def test_fiv_prepare_stitch_dispatch_passes_kwargs(fiv_env, monkeypatch, capsys):
    seen = {}

    def fake_prepare_stitch(project, phase, version=None, swimlane=None, search_neighbors=False):
        seen.update(project=project, phase=phase, version=version, swimlane=swimlane,
                    search_neighbors=search_neighbors)
        return ok({"prepared": False, "candidates": []})

    monkeypatch.setattr(server.prepare, "prepare_stitch", fake_prepare_stitch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["fiv", "prepare-stitch", "--project", "P", "--phase", "Orange",
                 "--search-neighbors"])
    assert exc.value.code == 0
    assert seen == {"project": "P", "phase": "Orange", "version": None, "swimlane": None,
                    "search_neighbors": True}
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
