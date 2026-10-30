"""End-to-end check of remote execution, outside pytest (its name does not match test_*.py).

Runs the real server (server mode, remote execution) in-process against a mock FIV Job API
that starts each "pod" with `docker run`, and a mock Artifactory. The pod is the Linux stitch
image plus the stitch_wrapper.py under test; the worker is pip-installed from a bare clone of
the current commit of this repo; the stitch tool is a fake that writes Output/FAKE_stitched.bin.

Needs Docker (Linux engine) and the base image. From the repo root:

    python tests/e2e_remote.py [callback|fallback|cancel|all]

Environment overrides:
    IFWI_E2E_WRAPPER     stitch_wrapper.py to test (default: the sibling cloud-dockerfile checkout)
    IFWI_E2E_BASE_IMAGE  base image (default: amr-registry.../fiv-ubuntu24.04-python3.13:v1.1.1)
    IFWI_E2E_DIR         scratch dir (default: %TEMP%/ifwi-e2e)

Scenarios: callback (the wrapper reports back), fallback (no report_base_url: the job ends from
the Job API + result.json), cancel (DELETE the pod; the late callback is not accepted). Results
go to <scratch>/results.json; pod containers are removed at the end.
"""
import http.server
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E = os.environ.get("IFWI_E2E_DIR") or os.path.join(tempfile.gettempdir(), "ifwi-e2e")
WRAPPER = os.environ.get("IFWI_E2E_WRAPPER") or os.path.join(
    os.path.dirname(os.path.dirname(ROOT)),
    "firmware.boot.uefi.iafw.devops.fivportal.cloud-dockerfile", "stitch_wrapper.py")
BASE_IMAGE = os.environ.get("IFWI_E2E_BASE_IMAGE") or \
    "amr-registry.caas.intel.com/fia-cloud/fiv-ubuntu24.04-python3.13:v1.1.1"
ART, JOB, SRV = 18761, 18762, 18763
HOST = "host.docker.internal"
IMAGE = "fiv-ubuntu24.04-python3.13:e2e-wrapper"
BRANCH = "e2e-under-test"
TOKEN = "e2e-token"

os.environ.update({
    "IFWI_MCP_SERVER_MODE": "server", "IFWI_MCP_SERVER_TOKEN": TOKEN,
    "IFWI_MCP_EXEC_MODE": "remote", "IFWI_MCP_EXEC_ENDPOINT": f"http://127.0.0.1:{JOB}/v1/fivportal/job",
    "IFWI_MCP_ARTIFACTORY_REPO": f"http://{HOST}:{ART}/repo/stitch_task",
    "IFWI_MCP_REPORT_BASE_URL": f"http://{HOST}:{SRV}",
    "IFWI_MCP_EXEC_IMAGE": IMAGE, "IFWI_MCP_EXEC_REPO": "file:///repo.git", "IFWI_MCP_EXEC_REF": BRANCH,
    "IFWI_MCP_EXEC_TIMEOUT": "1800", "IFWI_MCP_EXEC_PENDING_TIMEOUT": "600",
    "IFWI_MCP_REFRESH_MIN_INTERVAL": "2", "IFWI_MCP_EXEC_FINALIZE_GRACE": "20",
    "ARTIFACTORY_TOKEN": "e2e-af", "IFWI_MCP_CACHE_DIR": os.path.join(E2E, "cache"),
    "IFWI_MCP_CONFIG": os.path.join(E2E, "config.json"),
    "NO_PROXY": f"127.0.0.1,localhost,{HOST}", "no_proxy": f"127.0.0.1,localhost,{HOST}",
})

STORE = {}
LOG = []
PODS = []


def _force_remove(func, path, _exc):
    os.chmod(path, 0o700)              # git marks its object files read-only on Windows
    func(path)


def prepare():
    """Fresh scratch dir, a pod image with the wrapper under test, and a bare clone of HEAD."""
    if os.path.exists(E2E):
        shutil.rmtree(E2E, onerror=_force_remove)
    os.makedirs(E2E)
    shutil.copy(WRAPPER, os.path.join(E2E, "stitch_wrapper.py"))
    with open(os.path.join(E2E, "Dockerfile"), "w", encoding="ascii") as fh:
        fh.write(f"FROM {BASE_IMAGE}\nCOPY stitch_wrapper.py /app/stitch_wrapper.py\n")
    subprocess.run(["docker", "build", "-q", "-t", IMAGE, E2E], check=True)
    repo = os.path.join(E2E, "repo.git")
    subprocess.run(["git", "clone", "-q", "--bare", ROOT, repo], check=True)
    head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-c", "safe.bareRepository=all", "-C", repo, "branch", "-f", BRANCH, head],
                   check=True)
    log("pod image", IMAGE, "wrapper", WRAPPER, "worker commit", head)


def log(*args, sep=" "):
    line = sep.join(str(a) for a in args)
    LOG.append(line)
    print(line, flush=True)


class Artifactory(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = STORE.get(self.path)
        if body is None:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        body = STORE.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b""))); self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        STORE[self.path] = self.rfile.read(length)
        log("ART PUT", self.path, len(STORE[self.path]))
        self.send_response(201); self.send_header("Content-Length", "0"); self.end_headers()


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


class JobApi(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        job = "fivportal-" + uuid.uuid4().hex[:8]
        argv = payload["command"].split(" ")
        log("JOB POST", job, "os", payload["os"], "deadline", payload["activedeadlineseconds"],
            "cmd", " ".join(a if len(a) < 60 else f"<{len(a)}>" for a in argv))
        no_proxy = f"{HOST},.intel.com,localhost,127.0.0.1"
        # Like the real Linux Job API: the command runs as `sh -c "<command>"`, sh being PID 1.
        run = docker("run", "-d", "--name", job, "-e", f"no_proxy={no_proxy}", "-e", f"NO_PROXY={no_proxy}",
                     "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
                     "-e", "GIT_CONFIG_VALUE_0=*", "-v", f"{os.path.join(E2E, 'repo.git')}:/repo.git:ro",
                     payload["image"], "sh", "-c", payload["command"])
        if run.returncode:
            log("docker run failed", run.stderr)
            return self._json(500, {"Code": -1, "Message": run.stderr[:200]})
        PODS.append(job)
        self._json(200, {"Code": 0, "Message": "CreateSuccess", "Data": {"jobid": job, "status": ""}})

    def do_GET(self):
        job = self.path.rsplit("/", 1)[-1]
        out = docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", job)
        if out.returncode:
            return self._json(200, {"Code": -1, "Message": f'jobs.batch "{job}" not found'})
        state, code = out.stdout.split()
        status = {"created": "pending", "running": "running"}.get(state) or ("succeeded" if code == "0" else "failed")
        self._json(200, {"Code": 0, "Data": {"jobid": job, "status": status}})

    def do_DELETE(self):
        job = self.path.rsplit("/", 1)[-1]
        log("JOB DELETE", job, docker("stop", "-t", "30", job).returncode)
        self.send_response(204); self.end_headers()


def serve(handler, port):
    server = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


FAKE_CLI = """import os, sys, time
time.sleep(float(os.environ.get("FAKE_STITCH_SECONDS", "1")))
os.makedirs("Output", exist_ok=True)
with open(os.path.join("Output", "FAKE_stitched.bin"), "wb") as fh:
    fh.write(b"STITCHED" * 64)
print("Binary created at: Output/FAKE_stitched.bin", sys.argv[1:])
"""


def api(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{SRV}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_terminal(job_id, limit=600):
    started, last = time.time(), None
    while time.time() - started < limit:
        status, body = api("GET", f"/jobs/{job_id}")
        view = body["data"]
        key = (view["state"], view["stage"], (view.get("remote") or {}).get("pod_status"))
        if key != last:
            log(f"  t+{int(time.time() - started)}s", key, "degraded" if (view.get("remote") or {}).get("degraded") else "")
            last = key
        if view["terminal"]:
            return view
        time.sleep(3)
    raise SystemExit(f"job {job_id} not terminal after {limit}s")




def check(name, ok_, detail=""):
    log(("PASS" if ok_ else "FAIL"), name, detail)
    return ok_


def main():
    sys.path.insert(0, ROOT)
    prepare()
    from ifwi_mcp import config, jobs, plan as plan_mod
    from ifwi_mcp.server import mcp
    from ifwi_mcp.http_api import create_app
    import uvicorn

    base = f"http://{HOST}:{ART}/repo/inputs"
    STORE["/repo/inputs/tool.zip"] = zip_bytes({"tool/cli.py": FAKE_CLI,
                                                "tool/config/Config_Stitch_FAKE.ini": "[stitch]\n"})
    STORE["/repo/inputs/ifwi.bin"] = b"\xff" * 4096
    STORE["/repo/inputs/ing.zip"] = zip_bytes({"MMC1/mmc1.bin": b"mmc" * 100})
    serve(Artifactory, ART)
    serve(JobApi, JOB)

    assert config.validate_startup()["ok"], config.validate_startup()
    manager = jobs.get_manager()
    log("recover", manager.recover_on_startup())
    server = uvicorn.Server(uvicorn.Config(create_app(mcp), host="0.0.0.0", port=SRV, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)

    built = plan_mod.build_plan("E2E", "Orange", [{"name": "MMC1", "version": "1.0"}], "Config_Stitch_FAKE",
                                version="2026.01.1.01", ifwi_binary="FAKE",
                                source_urls={"ifwi": f"{base}/ifwi.bin", "stitch_tool": f"{base}/tool.zip",
                                             "ingredients": [f"{base}/ing.zip"]})
    assert built["ok"], built
    plan = built["data"]["plan"]

    scenario = sys.argv[1] if len(sys.argv) > 1 else "all"
    results, passed = {}, True
    try:
        if scenario in ("all", "callback"):
            log("== scenario: callback")
            status, job = manager.submit_plan_object(plan, {"plan_id": plan["plan_id"], "s": 1})
            view = wait_terminal(job["job_id"])
            results["callback"] = view
            data = (view["result"] or {}).get("data") or {}
            passed &= check("callback ends the job",
                            view["state"] == "succeeded" and view["result_source"] == "callback",
                            f"{view['state']} / {view['result_source']}")
            passed &= check("stitched_bin is an Artifactory link",
                            str((data.get("stitched_bin") or {}).get("url", "")).endswith("FAKE_stitched.bin"))
            events = api("GET", f"/jobs/{job['job_id']}?events=true")[1]["data"]["events"]
            log("  events:", *[e["detail"] for e in events], sep="\n    ")
            passed &= check("event log ends with the callback result",
                            events[-1]["detail"].endswith("[callback: ok]"))
        if scenario in ("all", "fallback"):
            log("== scenario: no callback (report_base_url unset) -> Job API + result.json")
            os.environ["IFWI_MCP_REPORT_BASE_URL"] = ""
            status, job = manager.submit_plan_object(plan, {"plan_id": plan["plan_id"], "s": 2})
            view = wait_terminal(job["job_id"])
            results["fallback"] = view
            passed &= check("result.json ends the job",
                            view["state"] == "succeeded" and view["result_source"] == "result_json",
                            f"{view['state']} / {view['result_source']}")
            os.environ["IFWI_MCP_REPORT_BASE_URL"] = f"http://{HOST}:{SRV}"
        if scenario in ("all", "cancel"):
            log("== scenario: cancel a running pod")
            status, job = manager.submit_plan_object(plan, {"plan_id": plan["plan_id"], "s": 3})
            deadline = time.time() + 300
            while time.time() < deadline:
                view = api("GET", f"/jobs/{job['job_id']}")[1]["data"]
                if view["stage"] == "running":
                    break
                time.sleep(2)
            status, body = api("POST", f"/jobs/{job['job_id']}/cancel")
            log("  cancel ->", status, body["data"]["state"], body["data"]["cleanup"])
            time.sleep(40)
            view = api("GET", f"/jobs/{job['job_id']}?events=true")[1]["data"]
            results["cancel"] = view
            passed &= check("cancelled job stays cancelled with its pod deleted",
                            view["state"] == "cancelled" and view["cleanup"] == {"pod_delete": "done"})
            passed &= check("late callback is recorded but not accepted",
                            any(e["detail"].startswith("callback from attempt") for e in view["events"]))
    finally:
        with open(os.path.join(E2E, "results.json"), "w", encoding="utf-8") as fh:
            json.dump({"results": results, "store_keys": sorted(STORE), "log": LOG}, fh, indent=2)
        for pod in PODS:
            docker("rm", "-f", pod)
    log("ALL PASSED" if passed else "SOME CHECKS FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())