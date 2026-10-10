"""Integration: the two-tool workflow over a real Jupyter wire.

local request -> remote Jupyter kernel -> locally available typed JSON plan.

Everything else in the suite proves the two halves separately: the capture end on
real pyspark clients (test_capture_plans_live.py, in-process, no Jupyter at all)
and the screening end with no pyspark (test_json_plans.py). This file joins them
across the actual transport the deployment uses:

1.  a real `jupyter_server` subprocess, with a real `ipykernel` kernel whose
    interpreter has pyspark (the execution point, simulated on localhost);
2.  the harness side -- this test -- creates the kernel session over the REST API,
    executes the bootstrap and capture cells, and reads the plan projection out of
    the cell's stdout, exactly as an MCP tool would;
3.  a *second interpreter* (the fast venv, which has no pyspark) screens the
    projection and its verdict is asserted -- the claim "the local machine needs
    no pyspark" is executed, not assumed.

Simulated on localhost: the same REST/WS API a JupyterHub fronts; what is NOT
simulated is Hub auth/session semantics on top. A cluster is never needed: the
kernel builds its plan against a dead endpoint (the capture stub answers the
Config prefetch locally), so this runs anywhere the wheel does.

Skipped without jupyter_server/ipykernel, like every differential extra. In CI the
differential job does not install them yet -- locally (or once CI does), the matrix
covers this per engine.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("pyspark", reason="the kernel interpreter needs pyspark")
pytest.importorskip("jupyter_server", reason="the simulated Jupyter needs a server")
pytest.importorskip("ipykernel", reason="the simulated Jupyter needs a kernel")
pytest.importorskip("websocket", reason="the harness side speaks the kernel WS protocol")
pytest.importorskip("requests", reason="the harness side speaks REST")

import requests  # noqa: E402

from sparkscreen import Verdict  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

#: The interpreter that plays "the local machine". The repo's fast venv is the
#: one environment guaranteed to have sparkscreen WITHOUT pyspark; when it is
#: absent, the local screening runs in-process instead (the wire is still
#: exercised, but the no-pyspark claim is then covered by test_json_plans.py
#: rather than by this test).
LOCAL_PYTHON = ROOT / ".venv" / "bin" / "python"

TOKEN = "sparkscreen-e2e"
BOOT_TIMEOUT = 60.0
CELL_TIMEOUT = 120.0

CAPTURE_OVERWRITE = (
    "import json\n"
    "from sparkscreen.connect import capture_plans\n"
    "out = capture_plans(\n"
    "    \"spark.range(1).write.mode('overwrite').saveAsTable('prod.users')\",\n"
    "    session=spark, globs=globals(),\n"
    ")\n"
    "print('PLAN_JSON:' + json.dumps(out))\n"
)

CAPTURE_SQL = (
    "import json\n"
    "from sparkscreen.connect import capture_plans\n"
    "out = capture_plans(\"spark.sql('DROP TABLE prod.t')\",\n"
    "    session=spark, globs=globals())\n"
    "print('PLAN_JSON:' + json.dumps(out))\n"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Jupyter:
    """A started server plus a live kernel session, spoken to over REST + WS."""

    def __init__(self, tmp: Path, kernel_py: str) -> None:
        self.root = tmp
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.headers = {"Authorization": f"token {TOKEN}"}

        # The kernelspec points at the engine interpreter; the server's env (which
        # the kernel inherits) puts sparkscreen's src on the path, so the kernel
        # can import it without any installation step.
        spec_dir = tmp / "kernels" / "sparkscreen-e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "kernel.json").write_text(json.dumps({
            "argv": [kernel_py, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
            "display_name": "sparkscreen e2e",
            "language": "python",
        }))

        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp),
               "JUPYTER_PATH": str(tmp),
               "PYTHONPATH": str(ROOT / "src")}
        self.proc = subprocess.Popen(
            [kernel_py, "-m", "jupyter_server",
             "--port", str(self.port), "--ip", "127.0.0.1", "--no-browser",
             "--ServerApp.token", TOKEN,
             "--ServerApp.root_dir", str(tmp)],
            env=env, cwd=str(tmp),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.base}/api", headers=self.headers, timeout=2)
                if r.status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            time.sleep(0.4)
        else:
            self.dump_log("server did not come up")
            raise RuntimeError("jupyter server did not start")

        r = requests.post(f"{self.base}/api/sessions", headers=self.headers,
                          json={"name": "e2e", "type": "notebook",
                                "path": "e2e.ipynb",
                                "kernel": {"name": "sparkscreen-e2e"}})
        r.raise_for_status()
        self.kernel_id = r.json()["kernel"]["id"]

        from websocket import create_connection

        self.ws = create_connection(
            f"ws://127.0.0.1:{self.port}/api/kernels/{self.kernel_id}/channels"
            f"?token={TOKEN}",
            header=[f"Authorization: token {TOKEN}"],
            timeout=CELL_TIMEOUT,
        )

    def run_cell(self, code: str) -> str:
        """Execute one cell; return its collected stdout. A cell error fails hard."""
        msg_id = uuid.uuid4().hex
        self.ws.send(json.dumps({
            "header": {"msg_id": msg_id, "username": "harness",
                       "session": uuid.uuid4().hex, "msg_type": "execute_request",
                       "version": "5.3"},
            "parent_header": {}, "metadata": {}, "buffers": [],
            "content": {"code": code, "silent": False, "store_history": False,
                        "user_expressions": {}, "allow_stdin": False,
                        "stop_on_error": True},
        }))
        out: list[str] = []
        while True:
            frame = json.loads(self.ws.recv())
            if frame.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            mtype = frame["header"]["msg_type"]
            if mtype == "stream" and frame["content"]["name"] == "stdout":
                out.append(frame["content"]["text"])
            elif mtype == "error":
                raise AssertionError(
                    "kernel cell raised:\n" + "\n".join(frame["content"]["traceback"])
                )
            elif mtype == "status" and frame["content"]["execution_state"] == "idle":
                return "".join(out)

    def extract(self, stdout: str, marker: str) -> str:
        for line in stdout.splitlines():
            if line.startswith(marker):
                return line[len(marker):]
        self.dump_log(f"marker {marker!r} not in cell output")
        raise AssertionError(f"kernel stdout carried no {marker!r} line")

    def dump_log(self, why: str) -> None:
        try:
            self.proc.terminate()
            out = self.proc.communicate(timeout=10)[0].decode(errors="replace")
        except Exception:  # noqa: BLE001
            out = "<server log unavailable>"
        print(f"--- jupyter server log ({why}) ---\n{out[-4000:]}")

    def close(self) -> None:
        try:
            requests.delete(
                f"{self.base}/api/sessions/{self.kernel_id}", headers=self.headers,
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass
        self.ws.close()
        self.proc.terminate()
        try:
            self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="module")
def jupyter(tmp_path_factory):
    server = Jupyter(tmp_path_factory.mktemp("jupyter-e2e"), __import__("sys").executable)
    yield server
    server.close()


@pytest.fixture(scope="module")
def wired(jupyter):
    """The kernel bootstrapped into the two-tool shape: sparkscreen + a session."""
    out = jupyter.run_cell(
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT / 'src'}')\n"
        "import sparkscreen\n"
        "print('BOOT_OK', sparkscreen.__version__)\n"
    )
    assert "BOOT_OK" in out
    out = jupyter.run_cell(
        "from pyspark.sql.connect.session import SparkSession as ConnectSession\n"
        "spark = ConnectSession('sc://localhost:1')\n"
        "print('SESSION_OK', type(spark.client).__name__)\n"
    )
    assert "SESSION_OK" in out
    return jupyter


def _screen_locally(cap: dict, tmp: Path) -> str:
    """Run the local screening in the pyspark-free interpreter; return the verdict.

    Fails hard when the local interpreter could import pyspark -- the local side
    of this topology is supposed to need none, and a run where it accidentally
    does proves nothing.
    """
    plan_file = tmp / "capture.json"
    plan_file.write_text(json.dumps(cap))
    probe = (
        "import json, sys\n"
        "from sparkscreen.plans import JsonPlan, screen_plan\n"
        "cap = json.load(open(sys.argv[1]))\n"
        "report = screen_plan(JsonPlan(cap['plan']),\n"
        "                     engine_version=cap['pyspark_version'])\n"
        "print('HAS_PYSARK', 'pyspark' in sys.modules)\n"
        "print('VERDICT', report.verdict.value)\n"
    )
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run([str(LOCAL_PYTHON), "-c", probe, str(plan_file)],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "HAS_PYSARK False" in proc.stdout, (
        f"the local machine imported pyspark; the no-pyspark claim was not tested\n"
        f"{proc.stdout}"
    )
    return [ln for ln in proc.stdout.splitlines() if ln.startswith("VERDICT")][0]


def test_local_request_yields_a_locally_screened_plan(wired, tmp_path):
    """The full arc: cell in, projection out, verdict back with no pyspark local."""
    out = wired.run_cell(CAPTURE_OVERWRITE)
    cap = json.loads(wired.extract(out, "PLAN_JSON:"))["captures"][0]
    assert cap["pyspark_version"]
    assert list(cap["plan"]["command"]) == ["write_operation"]

    assert _screen_locally(cap, tmp_path) == f"VERDICT {Verdict.DENY.value}"


def test_sql_inside_the_plan_reaches_the_local_grammar(wired, tmp_path):
    out = wired.run_cell(CAPTURE_SQL)
    cap = json.loads(wired.extract(out, "PLAN_JSON:"))["captures"][0]

    assert _screen_locally(cap, tmp_path) == f"VERDICT {Verdict.DENY.value}"
