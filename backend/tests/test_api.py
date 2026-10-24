"""Local HTTP/persistence integration tests, without a GPU or downloads."""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from backend.server import StudioApplication, StudioHTTPServer
from backend.store import JobStore

PDB = "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 20.00           C  \nEND\n"


class FakeEngine:
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.block = self.fail = False
        self.outside = None

    def validate_submission(self, payload):
        chains = payload.get("chains")
        if not isinstance(chains, list) or not chains or not chains[0].get("sequence"):
            raise ValueError("At least one amino-acid sequence is required.")
        return {"name": payload.get("name", "Test"), "engine": "reference", "chains": chains,
                "priority": payload.get("priority", "normal"), "seeds": [1], "numSamples": 1}

    def list_engines(self):
        return [{"id": "reference", "available": True, "isPrediction": False}]

    def load_example(self, name):
        return {"id": name, "name": name, "chains": [{"id": "A", "sequence": "A"}]}

    def run_prediction(self, job, work_dir, emit, cancelled):
        self.started.set()
        emit("loading", "Loading test fixture", 20)
        while self.block and not self.release.wait(0.01):
            if cancelled():
                raise RuntimeError("Cancelled by test")
        if self.fail:
            raise RuntimeError("Intentional adapter failure")
        path = work_dir / "model.pdb"
        path.write_text(PDB)
        emit({"stage": "parsing", "message": "Parsing test fixture", "progress": 80})
        return {"structurePath": str(self.outside or path), "structureFormat": "pdb",
                "residues": [], "provenance": {"mode": "test-fixture"}}


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>Test</title>")
        self.engine = FakeEngine()
        self.app = StudioApplication(self.root / "data", self.web, self.engine)
        self.httpd = StudioHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.app.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.engine.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.thread.join(1)
        self.temp.cleanup()

    def request(self, path, method="GET", payload=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req_headers = {"Content-Type": "application/json"} if data is not None else {}
        req_headers.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=req_headers)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        body = response.read()
        if "application/json" in response.headers.get("Content-Type", ""):
            body = json.loads(body)
        return response.status, body

    def submit(self, **extra):
        payload = {"name": "Test", "engine": "reference", "chains": [{"id": "A", "sequence": "A"}]}
        payload.update(extra)
        status, body = self.request("/api/jobs", "POST", payload)
        self.assertEqual(status, 201, body)
        return body

    def wait_terminal(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, job = self.request("/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail("Task did not reach a terminal state")

    def test_health_static_and_example(self):
        self.assertEqual(self.request("/api/health")[1]["status"], "ok")
        self.assertIn(b"<title>Test", self.request("/")[1])
        self.assertEqual(self.request("/api/examples/crambin")[1]["id"], "crambin")
        self.assertEqual(self.request("/api/examples/../private")[0], 404)

    def test_complete_events_and_download(self):
        job = self.wait_terminal(self.submit()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"], 100)
        self.assertGreaterEqual(len(job["events"]), 4)
        self.assertNotIn("_submission", job)
        self.assertNotIn("_artifacts", job)
        self.assertIsInstance(self.request("/api/jobs")[1], list)
        self.assertEqual(self.request(f"/api/jobs/{job['id']}/structure"), (200, PDB.encode()))
        self.assertEqual(self.request(job["result"]["artifacts"][0]["url"])[1], PDB.encode())

    def test_archive_route_bundles_registered_artifacts(self):
        import io
        import zipfile
        job = self.wait_terminal(self.submit(name="归档任务")["id"])
        with urllib.request.urlopen(f"{self.base}/api/jobs/{job['id']}/archive", timeout=3) as response:
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            self.assertIn("attachment", response.headers["Content-Disposition"])
            bundle = zipfile.ZipFile(io.BytesIO(response.read()))
        names = bundle.namelist()
        self.assertIn(job["result"]["structureFile"], names)
        self.assertEqual(bundle.read(job["result"]["structureFile"]).decode(), PDB)
        # The internal structure marker is routing metadata, not a file on disk.
        self.assertNotIn("__structure__", names)

    def test_archive_requires_completed_artifacts(self):
        self.engine.block = True
        running = self.submit(name="还在跑")
        self.assertEqual(self.request(f"/api/jobs/{running['id']}/archive")[0], 409)
        self.engine.release.set()
        self.wait_terminal(running["id"])
        self.assertEqual(self.request("/api/jobs/nonexistent/archive")[0], 404)

    def test_validation_and_same_origin(self):
        self.assertEqual(self.request("/api/jobs", "POST", {"chains": []})[0], 400)
        self.assertEqual(self.request("/api/jobs", "POST", raw=b"{")[0], 400)
        self.assertEqual(self.request("/api/jobs", "POST", raw=b"[]")[0], 400)
        self.assertEqual(self.request("/api/jobs", "POST", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/health", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/health", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/health", headers={"Origin": self.base})[0], 200)

    def test_cancel_queued_and_running(self):
        self.engine.block = True
        first = self.submit()
        self.assertTrue(self.engine.started.wait(1))
        second = self.submit()
        self.assertEqual(self.request(f"/api/jobs/{second['id']}/cancel", "POST")[0], 200)
        self.assertEqual(self.wait_terminal(second["id"])["status"], "cancelled")
        self.assertEqual(self.request(f"/api/jobs/{first['id']}", "DELETE")[0], 409)
        self.assertEqual(self.request(f"/api/jobs/{first['id']}/cancel", "POST")[0], 200)
        self.assertEqual(self.wait_terminal(first["id"])["status"], "cancelled")
        self.assertEqual(self.request(f"/api/jobs/{first['id']}", "DELETE")[0], 200)
        self.assertEqual(self.request(f"/api/jobs/{first['id']}")[0], 404)

    def test_failure_retry_new_attempt(self):
        self.engine.fail = True
        first = self.submit()
        failed = self.wait_terminal(first["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Intentional", failed["error"])
        self.engine.fail = False
        status, second = self.request(f"/api/jobs/{first['id']}/retry", "POST")
        self.assertEqual(status, 201)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["retryOf"], first["id"])
        self.assertEqual(self.wait_terminal(second["id"])["status"], "completed")
        self.assertEqual(self.request(f"/api/jobs/{first['id']}")[1]["status"], "failed")

    def test_artifacts_cannot_escape(self):
        job = self.wait_terminal(self.submit()["id"])
        self.assertEqual(self.request(f"/api/jobs/{job['id']}/artifact?name=..%2Fjobs.sqlite3")[0], 404)
        self.assertEqual(self.request("/%2e%2e/data/jobs.sqlite3")[0], 404)
        self.engine.outside = self.web / "index.html"
        failed = self.wait_terminal(self.submit()["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("outside", failed["error"])

    def test_data_directory_single_owner(self):
        with self.assertRaisesRegex(RuntimeError, "Another server"):
            StudioApplication(self.root / "data", self.web, self.engine)

    def test_import_preserves_unknown_bfactor(self):
        status, job = self.request("/api/import", "POST", {
            "name": "Imported ALA", "format": "pdb", "structureText": PDB})
        self.assertEqual(status, 201, job)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["engine"], "import")
        self.assertIsNone(job["result"]["plddt"])
        self.assertFalse(job["result"]["provenance"]["isPrediction"])
        self.assertEqual(self.request(f"/api/jobs/{job['id']}/structure")[1], PDB.encode())
        self.assertEqual(self.request(f"/api/jobs/{job['id']}/retry", "POST")[0], 409)
        self.assertEqual(self.request("/api/import", "POST", {"format": "pdb", "structureText": "invalid"})[0], 400)

    def test_real_reference_engine_uses_bundled_published_structure(self):
        from backend import engine
        self.app.engine = engine
        example = self.request("/api/examples/ubiquitin-afdb")[1]
        status, task = self.request("/api/jobs", "POST", {
            "name": "Actual bundled AFDB replay", "engine": "reference", "example": "ubiquitin-afdb",
            "chains": example["chains"], "seeds": [1], "numSamples": 1})
        self.assertEqual(status, 201, task)
        job = self.wait_terminal(task["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["result"]["provenance"]["mode"], "reference")
        self.assertFalse(job["result"]["provenance"]["newPrediction"])
        self.assertGreater(len(job["result"]["residues"]), 0)
        self.assertIsNone(job["result"]["pae"])
        descriptor = job["result"]["paeArtifact"]
        self.assertEqual(descriptor['rows'], 229)
        code, preview = self.request(descriptor['tileUrl']+'?maxSize=8')
        self.assertEqual(code, 200, preview)
        self.assertEqual(preview['shape'], [8, 8])
        self.assertEqual(preview['sourceShape'], [229, 229])
        self.assertNotIn("pae", self.request("/api/jobs")[1][0]["result"])
        model_url = job["result"]["rankedModels"][0]["structureUrl"]
        self.assertTrue(model_url.startswith("/api/jobs/"))
        self.assertEqual(self.request(model_url)[0], 200)
        self.assertIn(b"ATOM", self.request(f"/api/jobs/{job['id']}/structure")[1])

    def test_batch_submission_is_accepted_in_input_order(self):
        jobs = [{"name": f"Batch {index}", "engine": "reference",
                 "chains": [{"id": "A", "sequence": "A"}], "priority": "high"}
                for index in range(3)]
        status, batch = self.request("/api/jobs/batch", "POST", {"jobs": jobs})
        self.assertEqual(status, 201, batch)
        self.assertEqual(batch["count"], 3)
        self.assertEqual([job["name"] for job in batch["jobs"]], [job["name"] for job in jobs])
        self.assertEqual(len({job["id"] for job in batch["jobs"]}), 3)
        for job in batch["jobs"]:
            self.assertNotIn("_submission", job)
            self.assertEqual(self.wait_terminal(job["id"])["status"], "completed")

    def test_invalid_batch_does_not_write_any_task(self):
        valid = {"name": "Valid", "chains": [{"id": "A", "sequence": "A"}]}
        status, response = self.request("/api/jobs/batch", "POST", {"jobs": [valid, {"chains": []}]})
        self.assertEqual(status, 400, response)
        self.assertIn("Batch item 2", response["error"])
        self.assertEqual(self.request("/api/jobs")[1], [])
        self.assertEqual(self.request("/api/jobs/batch", "POST", {"jobs": [valid] * 201})[0], 400)
        self.assertEqual(self.request("/api/jobs/batch", "POST", {"jobs": []})[0], 400)
        self.assertEqual(self.request("/api/jobs")[1], [])

    def test_import_confidence_alignment_and_mismatch(self):
        status, job = self.request("/api/import", "POST", {
            "format": "pdb", "structureText": PDB, "confidence": {"plddt": [91.0]}})
        self.assertEqual(status, 201, job)
        self.assertEqual(job["result"]["residues"][0]["plddt"], 91.0)
        self.assertEqual(job["result"]["meanPlddt"], 91.0)
        self.assertEqual(job["chains"][0]["sequence"], "A")
        status, mismatch = self.request("/api/import", "POST", {
            "format": "pdb", "structureText": PDB, "confidence": {"plddt": [91.0, 80.0]}})
        self.assertEqual(status, 201, mismatch)
        self.assertIsNone(mismatch["result"]["plddt"])
        self.assertTrue(mismatch["result"]["warnings"])


class StorageTests(unittest.TestCase):
    def test_restart_preserves_queue_recovers_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            original = JobStore(path)
            first = original.create({"name": "first", "chains": []})
            second = original.create({"name": "second", "chains": []})
            claimed = original.claim()
            self.assertIn(claimed["id"], {first["id"], second["id"]})
            reopened = JobStore(path)
            self.assertEqual(reopened.recover_interrupted(), 1)
            self.assertEqual(reopened.get(claimed["id"])["status"], "failed")
            self.assertIsNotNone(reopened.claim())

    def test_cancellation_wins_result_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "db")
            job = store.create({"name": "cancel race"})
            store.claim()
            store.cancel(job["id"])
            store.finish(job["id"], "completed", result={"wrong": "result"})
            terminal = store.get(job["id"])
            self.assertEqual(terminal["status"], "cancelled")
            self.assertIsNone(terminal["result"])

    def test_list_projects_out_large_arrays_but_detail_preserves_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "db")
            job = store.create({"name": "Large result"})
            store.claim()
            arrays = {"pae": [[0.0, 2.0], [3.0, 0.0]],
                      "residues": [{"aa": "A"}, {"aa": "G"}],
                      "plddt": [90.0, 80.0], "rankedModels": [{"rank": 1}]}
            store.finish(job["id"], "completed", result={"sequenceLength": 2, **arrays})
            summary = store.list()[0]["result"]
            self.assertEqual(summary["sequenceLength"], 2)
            for key, value in arrays.items():
                self.assertNotIn(key, summary)
                self.assertEqual(store.get(job["id"])["result"][key], value)

    def test_high_priority_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary) / "db")
            store.create({"name": "normal", "priority": "normal"})
            high = store.create({"name": "high", "priority": "high"})
            self.assertEqual(store.claim()["id"], high["id"])


class ServerLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process-group shutdown test")
    def test_sigterm_cancels_external_process_and_persists_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "sleep_engine.py"
            script.write_text("import os,sys,time\nfrom pathlib import Path\np=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True)\n(p/'child.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
            environment = dict(os.environ)
            for key in ("PROTEIN_STUDIO_GPU_GROUPS_JSON", "PROTEIN_STUDIO_GPU_DEVICES"):
                environment.pop(key, None)
            environment.update(PROTEIN_STUDIO_HOST="127.0.0.1", PROTEIN_STUDIO_PORT="0",
                               PROTEIN_STUDIO_DATA_DIR=str(root / "data"), PROTEIN_STUDIO_WORKERS="1",
                               PROTEIN_STUDIO_ALPHAFOLD2_COMMAND=json.dumps([sys.executable, str(script), "{output_dir}"]))
            log_path = root / "server.log"
            child_pid = None
            with log_path.open("wb") as log:
                process = subprocess.Popen([sys.executable, "-m", "backend.server"],
                                           cwd=Path(__file__).resolve().parents[2], env=environment,
                                           stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 5
                    port = None
                    while time.monotonic() < deadline:
                        match = re.search(r"Protein Studio: http://127\.0\.0\.1:(\d+)", log_path.read_text())
                        if match:
                            port = int(match.group(1))
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(port, log_path.read_text())
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/jobs", method="POST",
                              data=json.dumps({"name": "Shutdown test", "engine": "alphafold2",
                                               "chains": [{"id": "A", "sequence": "A"}], "numSamples": 1}).encode(),
                              headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=3) as response:
                        task = json.load(response)
                    pid_path = root / "data/jobs" / task["id"] / "output/seed-1/child.pid"
                    deadline = time.monotonic() + 3
                    while not pid_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(pid_path.exists(), log_path.read_text())
                    child_pid = int(pid_path.read_text())
                    process.terminate()
                    self.assertEqual(process.wait(timeout=8), 0, log_path.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                    store = JobStore(root / "data/jobs.sqlite3")
                    self.assertEqual(store.get(task["id"])["status"], "cancelled")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                    if child_pid:
                        try:
                            os.killpg(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
