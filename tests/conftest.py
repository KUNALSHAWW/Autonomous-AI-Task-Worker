import os
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = int(os.environ.get("TEST_SANDBOX_PORT", "8199"))
BASE = f"http://127.0.0.1:{PORT}"
os.environ["SANDBOX_URL"] = BASE


@pytest.fixture(scope="session")
def sandbox():
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "SANDBOX_DB": os.path.join(tmp, "sandbox.db")}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", str(PORT),
                             "--log-level", "warning"], cwd=ROOT, env=env)
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    yield BASE
    proc.terminate()


def reset(base, seed=5, chaos=None):
    return httpx.post(f"{base}/acme/admin/reset", json={"seed": seed, "chaos": chaos or {}}, timeout=30).json()["truth"]


def state(base):
    return httpx.get(f"{base}/acme/admin/state", timeout=30).json()
