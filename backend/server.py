"""HTTP API, safe artifact serving, and a durable local work queue.

Start with ``python -m backend.server`` from the project root. Only explicitly
configured external engines can perform model inference; the reference engine
is a labelled replay of a known structure, not a generated prediction.
"""
from __future__ import annotations

import copy
import importlib
import json
import logging
import mimetypes
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .store import ConflictError, JobStore
from .worker_pipeline import PipelineScheduler
from .distributed import DistributedController, AuthenticationError
from .storage_service import StorageService
from .proxy_paths import normalize_base_path, strip_request_target, public_urls, index_html

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("protein-studio")
MAX_BODY = 12 * 1024 * 1024
MAX_STRUCTURE = 10 * 1024 * 1024
JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class DataDirectoryLock:
    def __init__(self, directory):
        self.file = (directory / "server.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another server owns this data directory. Choose another PROTEIN_STUDIO_DATA_DIR.") from exc

    def close(self):
        if not self.file.closed:
            if os.name != "nt":
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


def score_query_params(query):
    """Parse the shared sort/min_* result-score parameters of items and export."""
    score_query = {}
    for name in ("sort", "min_ranking", "min_iptm", "min_ptm", "min_plddt"):
        value = query.get(name, [None])[0]
        if value not in (None, ""):
            score_query[name] = value if name == "sort" else float(value)
    return score_query


def public_job(job):
    result = {k: v for k, v in job.items() if not k.startswith("_")}
    result["artifacts"] = [{"name": name, "url": f"/api/jobs/{job['id']}/artifact?name={quote(name, safe='')}"}
                           for name in job.get("_artifacts", {}) if not name.startswith("__")]
    return result


class StudioApplication:
    def __init__(self, data_dir=None, web_dir=None, engine_module=None, workers=1, staged_module=None, base_path=None):
        self.base_path = normalize_base_path(os.environ.get("PROTEIN_STUDIO_BASE_PATH", "") if base_path is None else base_path)
        self.data_dir = Path(data_dir or ROOT / "data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = DataDirectoryLock(self.data_dir)
        self.web_dir = Path(web_dir or ROOT / "web").resolve()
        self.engine = engine_module or importlib.import_module("backend.engine")
        self.store = JobStore(self.data_dir / "jobs.sqlite3")
        self.store.recover_interrupted()
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(exist_ok=True)
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self.workers = []
        self.worker_count = max(1, min(int(workers), 64))
        gpu_workers = max(1, min(int(os.environ.get("PROTEIN_STUDIO_GPU_WORKERS", str(self.worker_count))), 64))
        self.prediction_gate = threading.BoundedSemaphore(gpu_workers)
        self.pipeline = PipelineScheduler(self.data_dir, staged_module=staged_module,
            prep_workers=int(os.environ.get("PROTEIN_STUDIO_CPU_PREP_WORKERS", "2")),
            gpu_workers=gpu_workers,
            validation_workers=int(os.environ.get("PROTEIN_STUDIO_CPU_VALIDATE_WORKERS", "1")),
            ready_cap=int(os.environ.get("PROTEIN_STUDIO_READY_HIGH_WATER", "64")),
            lease_seconds=float(os.environ.get("PROTEIN_STUDIO_PIPELINE_LEASE_SECONDS", "60")),
            prediction_gate=self.prediction_gate)
        self.distributed = DistributedController(self)
        self.storage = StorageService(self.data_dir)
        self._diagnostics = None
        self._diagnostics_at = 0.0
        self._diagnostics_lock = threading.Lock()

    def start(self):
        if self.workers:
            return
        self.pipeline.start()
        self.distributed.start()
        for index in range(self.worker_count):
            thread = threading.Thread(target=self._worker, name=f"prediction-worker-{index}", daemon=True)
            self.workers.append(thread)
            thread.start()

    def close(self):
        self.stop_event.set()
        self.wakeup.set()
        storage_stopped = self.storage.close()
        self.distributed.close()
        pipeline_stopped = self.pipeline.close()
        for worker in self.workers:
            worker.join(timeout=5)
        if storage_stopped and pipeline_stopped and not any(worker.is_alive() for worker in self.workers):
            self.lock.close()
        else:
            LOG.warning("An engine has not stopped yet; retaining the data-directory lock until process exit.")

    def submit(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Submission must be a JSON object.")
        normalized = self.engine.validate_submission(payload)
        if not isinstance(normalized, dict):
            raise RuntimeError("Engine validation returned an invalid submission.")
        self._allow_legacy_submission(normalized)
        job = self.store.create(normalized)
        self.wakeup.set()
        return public_job(job)

    def submit_batch(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Batch must be an object containing a jobs array.")
        submissions = payload["jobs"]
        if not 1 <= len(submissions) <= 200:
            raise ValueError("A batch must contain between 1 and 200 jobs.")
        normalized = []
        for index, submission in enumerate(submissions, 1):
            try:
                if not isinstance(submission, dict):
                    raise ValueError("Submission must be a JSON object.")
                validated = self.engine.validate_submission(submission)
                if not isinstance(validated, dict):
                    raise RuntimeError("Engine validation returned an invalid submission.")
                self._allow_legacy_submission(validated)
                normalized.append(validated)
            except ValueError as exc:
                raise ValueError(f"Batch item {index}: {exc}") from exc
        # No write occurs until every item validates. SQLite rolls back all
        # inserted tasks/events if any insertion fails.
        jobs = self.store.create_many(normalized)
        self.wakeup.set()
        return {"jobs": [public_job(job) for job in jobs], "count": len(jobs)}

    def retry(self, job_id):
        job = self.store.get(job_id)
        if job is None:
            raise KeyError("Task not found.")
        if job["status"] not in {"failed", "cancelled", "completed"}:
            raise ConflictError("Only a finished task can be retried.")
        if job["engine"] == "import":
            raise ConflictError("Imported structures cannot be retried; import the file again instead.")
        normalized = self.engine.validate_submission(copy.deepcopy(job["_submission"]))
        self._allow_legacy_submission(normalized)
        new_job = self.store.create(normalized, parent_id=job_id)
        self.wakeup.set()
        return public_job(new_job)

    @staticmethod
    def _allow_legacy_submission(submission):
        if os.environ.get('PROTEIN_STUDIO_PREDICTION_MODE', 'legacy') == 'workers' and submission.get('engine') != 'reference':
            raise ValueError('Real prediction jobs use campaigns in workers mode; legacy subprocess submission would bypass resident GPU ownership.')

    def delete(self, job_id):
        if not self.store.delete(job_id):
            raise KeyError("Task not found.")
        # Only the server-generated job directory is eligible for cleanup.
        directory = self.jobs_dir / job_id
        if directory.is_symlink():
            directory.unlink()
        elif directory.is_dir():
            shutil.rmtree(directory)

    def _emit(self, job_id, *args, **kwargs):
        if args and isinstance(args[0], dict):
            event = dict(args[0])
            event.update(kwargs)
            stage = event.pop("stage", "running")
            message = event.pop("message", "")
            progress = event.pop("progress", None)
        else:
            event = dict(kwargs)
            stage = args[0] if args else event.pop("stage", "running")
            message = args[1] if len(args) > 1 else event.pop("message", "")
            progress = args[2] if len(args) > 2 else event.pop("progress", None)
        event.pop("time", None)
        self.store.emit(job_id, stage, message, progress, **event)

    def _register_artifacts(self, job_id, result, directory):
        if not isinstance(result, dict):
            raise ValueError("Engine result must be a JSON object.")
        from .result_artifacts import compact_pae, publish_pae_urls
        result = copy.deepcopy(compact_pae(result, directory))
        registry = {}
        structure_name = None

        def register(raw, preferred=None):
            if not raw or not isinstance(raw, (str, Path)):
                return None
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = directory / candidate
            candidate = candidate.resolve()
            try:
                relative = candidate.relative_to(directory.resolve())
            except ValueError as exc:
                raise ValueError("Engine returned an artifact outside the task directory.") from exc
            if not candidate.is_file():
                raise ValueError(f"Engine artifact does not exist: {relative}")
            name = str(relative.as_posix())
            if name in {"", "."}:
                raise ValueError("Invalid artifact name.")
            registry[name] = name
            return name

        for key in ("structurePath", "structure_path", "structureFile"):
            if result.get(key):
                structure_name = register(result[key])
                break
        raw_artifacts = result.get("artifacts", [])
        if isinstance(raw_artifacts, dict):
            raw_artifacts = [{"name": k, "path": v} for k, v in raw_artifacts.items()]
        for artifact in raw_artifacts if isinstance(raw_artifacts, list) else []:
            if isinstance(artifact, str):
                register(artifact)
            elif isinstance(artifact, dict):
                register(artifact.get("path") or artifact.get("file") or artifact.get("name"))
        for model in result.get("rankedModels", []):
            if isinstance(model, dict):
                path = model.get("structurePath") or model.get("structureFile") or model.get("path")
                if path:
                    registered_name = register(path)
                    model["structureUrl"] = f"/api/jobs/{job_id}/artifact?name={quote(registered_name, safe='')}"
                    model["structureFile"] = registered_name
        if structure_name is None:
            raise ValueError("Engine produced no structure artifact.")
        result["structureUrl"] = f"/api/jobs/{job_id}/structure"
        result["structureFile"] = structure_name
        result["artifacts"] = [{"name": name, "size": (directory / relative).stat().st_size,
                                "url": f"/api/jobs/{job_id}/artifact?name={quote(name, safe='')}"}
                               for name, relative in sorted(registry.items())]
        registry["__structure__"] = structure_name
        result = publish_pae_urls(result, directory, f"/api/jobs/{job_id}")
        return result, registry

    def _worker(self):
        while not self.stop_event.is_set():
            self.wakeup.clear()
            try:
                job = self.store.claim(allowed_engines=('reference',) if os.environ.get('PROTEIN_STUDIO_PREDICTION_MODE', 'legacy') == 'workers' else None)
            except Exception:
                LOG.exception("Unable to claim next task")
                self.stop_event.wait(0.5)
                continue
            if job is None:
                self.wakeup.wait(0.5)
                continue
            job_id = job["id"]
            directory = self.jobs_dir / job_id
            try:
                directory.mkdir(exist_ok=True)
                acquired = False
                try:
                    while not self.stop_event.is_set() and not self.store.cancelled(job_id):
                        if self.prediction_gate.acquire(timeout=.2):
                            acquired = True
                            break
                    if not acquired:
                        self.store.finish(job_id, "cancelled")
                        continue
                    result = self.engine.run_prediction(
                        job, directory, lambda *a, **kw: self._emit(job_id, *a, **kw),
                        lambda: self.stop_event.is_set() or self.store.cancelled(job_id))
                finally:
                    if acquired:
                        self.prediction_gate.release()
                if self.stop_event.is_set() or self.store.cancelled(job_id):
                    self.store.finish(job_id, "cancelled")
                    continue
                result, artifacts = self._register_artifacts(job_id, result, directory)
                self.store.finish(job_id, "completed", result=result, artifacts=artifacts)
            except Exception as exc:
                cancelled = (self.stop_event.is_set() or self.store.cancelled(job_id)
                             or type(exc).__name__ == "PredictionCancelled")
                if cancelled:
                    self.store.finish(job_id, "cancelled")
                else:
                    LOG.exception("Prediction failed: %s", job_id)
                    registry = {}
                    paths = list(directory.glob("engine-seed-*.log"))
                    paths += [directory / "execution.json", directory / "input" / "protein.fasta",
                              directory / "input" / "input.json", directory / "input" / "submission.json"]
                    for path in paths:
                        resolved = path.resolve()
                        if not path.is_symlink() and resolved.is_relative_to(directory.resolve()) and resolved.is_file():
                            relative = resolved.relative_to(directory.resolve()).as_posix()
                            registry[relative] = relative
                    self.store.finish(job_id, "failed", error=str(exc)[:2000] or type(exc).__name__, artifacts=registry)

    def artifact(self, job_id, name):
        job = self.store.get(job_id)
        if job is None:
            raise KeyError("Task not found.")
        relative = job.get("_artifacts", {}).get(name)
        if relative is None:
            raise KeyError("Artifact not found.")
        directory = (self.jobs_dir / job_id).resolve()
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise KeyError("Artifact not available.")
        return path

    def archive(self, job_id):
        """Bundle one completed job's registered artifacts into a zip snapshot.

        Following the campaign export pattern, the bundle is written to a
        temporary file inside the data directory and only streamed afterwards,
        so a slow client never holds the store or engine open.
        """
        job = self.store.get(job_id)
        if job is None:
            raise KeyError("Task not found.")
        if job.get("status") != "completed":
            raise ConflictError("Archive is only available for completed jobs.")
        registry = job.get("_artifacts") or {}
        names = sorted(name for name in registry if name != "__structure__")
        if not names:
            raise ConflictError("Job has no result artifacts to archive.")
        handle = tempfile.TemporaryFile(mode="w+b", dir=self.data_dir)
        try:
            with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as bundle:
                for name in names:
                    bundle.write(self.artifact(job_id, name), name)
            size = handle.seek(0, os.SEEK_END)
            handle.seek(0)
            return handle, size
        except BaseException:
            handle.close()
            raise

    def diagnostics(self):
        with self._diagnostics_lock:
            if self._diagnostics is None or time.monotonic() - self._diagnostics_at > 30:
                gpus, gpu_error = [], None
                executable = shutil.which("nvidia-smi")
                if executable:
                    try:
                        proc = subprocess.run([executable, "--query-gpu=index,name,memory.total",
                                               "--format=csv,noheader,nounits"],
                                              capture_output=True, text=True, timeout=3, check=False)
                        if proc.returncode == 0:
                            for line in proc.stdout.splitlines():
                                parts = [part.strip() for part in line.split(",")]
                                if len(parts) >= 3:
                                    gpus.append({"index": parts[0], "name": ",".join(parts[1:-1]),
                                                 "memoryMiB": float(parts[-1])})
                        else:
                            gpu_error = "nvidia-smi did not return available CUDA devices."
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        gpu_error = str(exc)[:300]
                else:
                    gpu_error = "nvidia-smi is not installed; no NVIDIA GPU was detected by this service."
                self._diagnostics = {"gpu_available": bool(gpus), "gpuAvailable": bool(gpus),
                                     "gpus": gpus, "gpuDiagnostic": gpu_error,
                                     "backend": {"python": platform.python_version(), "platform": platform.system(),
                                                 "storage": "SQLite", "workers": self.worker_count}}
                self._diagnostics_at = time.monotonic()
            result = copy.deepcopy(self._diagnostics)
        result["pipeline"] = self.pipeline.config()
        result["engines"] = self.engine.list_engines()
        return result

    def database_profiles(self):
        """Report campaign configuration readiness for each trusted profile.

        Filesystem preflight is still separate from native-runtime execution or
        GPU health. Profile commands may be usable without a global default.
        """
        from .databases import list_profiles
        profiles = list_profiles()
        defaults = self.pipeline.config()['capabilities']
        module = self.pipeline._module()
        for profile in profiles:
            capabilities = (module.staged_capabilities(profile)
                            if profile.get('prepareCommand') else defaults)
            capability = next((item for item in capabilities if item.get('engine') == profile['engine']), None)
            profile['campaignCapability'] = {
                'available': bool(profile['ready'] and capability and capability.get('staged')
                                  and capability.get('available')),
                'diagnostics': capability.get('diagnostics', []) if capability else ['No staged campaign adapter.'],
            }
        return profiles

    def create_campaign(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Campaign must be a JSON object.")
        name = payload.get("name", "Prediction campaign")
        engine = payload.get("engine", "reference")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError("Campaign name must contain 1–200 characters.")
        settings = payload.get("settings", {})
        allowed_settings = {"name", "seeds", "numSamples", "numRecycles", "example", "priority", "preset"}
        if not isinstance(settings, dict) or set(settings) - allowed_settings:
            raise ValueError("Unsupported campaign settings; use model sampling and input settings only.")
        status = payload.get("status", "active")
        if status not in {"active", "paused"}:
            raise ValueError("Campaign status must be active or paused.")
        placement = payload.get('placement', 'hybrid')
        if placement not in {'local', 'aliyun', 'hybrid'}:
            raise ValueError('Campaign placement must be local, aliyun, or hybrid.')
        execution = payload.get("executionProfile", {})
        if not isinstance(execution, dict) or set(execution) - {"id", "version"} or any(not isinstance(v, str) for v in execution.values()):
            raise ValueError("executionProfile accepts only id/version metadata; commands belong in administrator configuration.")
        capabilities = self.pipeline.config()["capabilities"]
        capability = next((entry for entry in capabilities if entry.get("engine") == engine), None)
        if capability is None or not capability.get("staged"):
            raise ValueError("This engine has no staged campaign adapter; use a supported staged engine or the legacy job endpoint.")
        profile = payload.get("databaseProfile", "reference" if engine == "reference" else None)
        if isinstance(profile, dict):
            if set(profile) != {"id"}:
                raise ValueError("databaseProfile must select a registered profile ID, not supply database paths.")
            profile = profile.get("id")
        if not isinstance(profile, str) or not profile:
            raise ValueError("Select a registered database profile ID.")
        from .databases import get_profile
        try:
            database = get_profile(profile)
        except KeyError as exc:
            raise ValueError(f"Unknown database profile: {profile}") from exc
        if database.get("engine") not in {engine, "any", "all"}:
            raise ValueError("Database profile is not compatible with the selected engine.")
        if not database.get("ready"):
            diagnostics = database.get("diagnostics", [])
            raise ValueError("Database profile is not ready: " + "; ".join(
                d.get("message", str(d)) if isinstance(d, dict) else str(d) for d in diagnostics))
        if database.get('prepareCommand'):
            capabilities = self.pipeline._module().staged_capabilities(database)
            capability = next((entry for entry in capabilities if entry.get('engine') == engine), None)
        if capability is None or not capability.get('available'):
            raise ValueError('Staged engine is unavailable: ' + '; '.join(
                capability.get('diagnostics', []) if capability else ['No compatible staged adapter.']))
        return self.pipeline.store.create({"name": name.strip(), "engine": engine, "settings": settings,
            "databaseProfile": database, "executionProfile": execution, "status": status, "placement": placement})

    def append_campaign_items(self, campaign_id, payload):
        campaign = self.pipeline.store.get(campaign_id)
        if campaign is None:
            raise KeyError("Campaign not found.")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not 1 <= len(payload["items"]) <= 200:
            raise ValueError("items must contain between 1 and 200 targets per request.")
        normalized = []
        allowed = {"targetId", "name", "chains", "sequence", "settings", "seeds", "numSamples",
                   "numRecycles", "example", "priority", "preset"}
        for index, raw in enumerate(payload["items"], 1):
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise ValueError(f"Item {index} contains unsupported input fields.")
            target_id = raw.get("targetId")
            if not isinstance(target_id, str) or not 1 <= len(target_id) <= 200 or any(ord(c) < 32 for c in target_id):
                raise ValueError(f"Item {index} needs a printable targetId of 1–200 characters.")
            settings = raw.get("settings", {})
            if not isinstance(settings, dict) or set(settings) - {"seeds", "numSamples", "numRecycles", "example", "priority", "preset"}:
                raise ValueError(f"Item {index} has unsupported settings.")
            submission = dict(campaign["settings"])
            submission.update(settings)
            submission.update({key: value for key, value in raw.items() if key not in {"targetId", "settings"}})
            submission.update(engine=campaign["engine"], name=raw.get("name") or target_id[:120])
            try:
                job = self.engine.validate_submission(submission)
            except ValueError as exc:
                raise ValueError(f"Item {index} ({target_id}): {exc}") from exc
            normalized.append({"targetId": target_id, "submission": job})
        response = self.pipeline.store.append(campaign_id, normalized)
        self.pipeline.wakeup.set()
        return response

    def import_structure(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Import must be a JSON object.")
        text = payload.get("structureText")
        fmt = payload.get("format", "pdb")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("structureText must contain a PDB or mmCIF structure.")
        if len(text.encode("utf-8")) > MAX_STRUCTURE:
            raise OverflowError("Structure text exceeds the 10 MiB limit.")
        if fmt not in {"pdb", "mmcif", "cif"}:
            raise ValueError("format must be pdb or mmcif.")
        if "\x00" in text:
            raise ValueError("Structure text contains a NUL byte.")
        source = payload.get("source", "unknown")
        if source not in {"unknown", "experimental", "alphafold", "alphafold2", "alphafold3", "colabfold", "afdb"}:
            raise ValueError("Unsupported structure source.")
        name = payload.get("name") or "Imported structure"
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError("name must be text of at most 200 characters.")
        confidence = payload.get("confidence")
        if confidence is not None and not isinstance(confidence, (dict, list)):
            raise ValueError("confidence must be a JSON object or array.")
        from .parsers import parse_structure, parse_confidence, interface_contacts
        job = self.store.create({"name": name, "engine": "import", "chains": [],
                                 "source": source, "priority": "normal"}, initial_status="running")
        directory = self.jobs_dir / job["id"]
        directory.mkdir(exist_ok=True)
        path = directory / ("imported.pdb" if fmt == "pdb" else "imported.cif")
        try:
            path.write_text(text, encoding="utf-8")
            result = parse_structure(path, source=source)
            if not result.get("residues"):
                raise ValueError("No protein residues could be parsed from the structure.")
            artifacts = [{"path": str(path)}]
            if confidence is not None:
                confidence_path = directory / "confidence.json"
                confidence_path.write_text(json.dumps(confidence, allow_nan=False), encoding="utf-8")
                parsed_confidence = parse_confidence(confidence_path)
                vector = parsed_confidence.pop("plddt", None)
                if vector is not None:
                    if len(vector) == len(result["residues"]):
                        result["plddt"] = vector
                        result["meanPlddt"] = sum(vector) / len(vector)
                        for residue, value in zip(result["residues"], vector):
                            residue["plddt"] = value
                        result["confidenceDefinition"] = "User-provided per-residue pLDDT from confidence JSON"
                    else:
                        parsed_confidence.setdefault("warnings", []).append(
                            "Confidence pLDDT length does not match parsed residues; the vector was not assigned to residues.")
                pae = parsed_confidence.get("pae")
                if pae is not None and len(pae) != len(result["residues"]):
                    # PAE axes pair with residues by index (ChimeraX "alphafold pae"
                    # associates a matrix with a model and refuses on dimension
                    # mismatch); an unaligned matrix would silently mislabel residues.
                    parsed_confidence.pop("pae")
                    parsed_confidence.pop("interfacePae", None)
                    parsed_confidence.pop("ipSae", None)
                    parsed_confidence.setdefault("warnings", []).append(
                        f"Imported PAE is {len(pae)}x{len(pae)} but the structure parsed "
                        f"{len(result['residues'])} residues; the matrix was not attached because its "
                        "axes would not align with residues. Raw confidence JSON remains downloadable.")
                result.update(parsed_confidence)
                matrix = parsed_confidence.get("pae")
                if matrix and len(matrix) == len(result["residues"]) and len(
                        {residue["chain"] for residue in result["residues"]}) >= 2:
                    # Same AFDB pair convention as the engine path; the guard
                    # above already proved the rows align with residues.
                    summary = interface_contacts(matrix, result["residues"], lines=True)
                    if summary["contacts"]:
                        result["interfaceContacts"] = summary["contacts"]
                    if summary.get("scores"):
                        result["interfaceScores"] = summary["scores"]
                    if summary.get("lines"):
                        result["interfaceLines"] = summary["lines"]
                    if summary.get("linesTruncated"):
                        result.setdefault("warnings", []).append(
                            "Interface contact lines were truncated at 2048 pairs; "
                            "the PAE matrix and structure remain downloadable.")
                    if summary["truncated"]:
                        result.setdefault("warnings", []).append(
                            "Interface contact residue ranges were truncated at 512 runs per side; "
                            "the PAE matrix and structure remain downloadable.")
                artifacts.append({"path": str(confidence_path)})
            result.update(structurePath=str(path), structureFile=path.name,
                          artifacts=artifacts,
                          provenance={"mode": "import", "source": source, "isPrediction": False,
                                      "note": "User-imported structure; this application did not predict it.",
                                      "confidenceProvided": confidence is not None},
                          timings={})
            result, registered = self._register_artifacts(job["id"], result, directory)
            self.store.finish(job["id"], "completed", result=result, artifacts=registered)
            return public_job(self.store.get(job["id"]))
        except Exception as exc:
            self.store.finish(job["id"], "failed", error=str(exc)[:2000])
            raise ValueError(f"Structure import failed: {exc}") from exc


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        if app.distributed.enabled and address[0] not in {'localhost', '127.0.0.1', '::1'} and os.environ.get('PROTEIN_STUDIO_ALLOW_REMOTE_BIND') != '1':
            raise ValueError('Remote worker binding requires PROTEIN_STUDIO_ALLOW_REMOTE_BIND=1 and a protected TLS deployment.')
        super().__init__(address, StudioHandler)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "ProteinStudio/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    @property
    def app(self):
        return self.server.app

    def _pae(self, result, root, query_string):
        """Serve only bounded preview or exact-window data, never a full N² array."""
        query = parse_qs(query_string, keep_blank_values=True)
        allowed = {'maxSize', 'rowStart', 'colStart', 'rows', 'cols'}
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise ValueError('Unsupported or repeated PAE query fields.')

        def number(name, minimum, maximum):
            raw = query[name][0]
            if not re.fullmatch('[0-9]+', raw):
                raise ValueError(name + ' must be an integer.')
            value = int(raw)
            if not minimum <= value <= maximum:
                raise ValueError(name + f' must be in {minimum}..{maximum}.')
            return value

        tile = {'rowStart', 'colStart', 'rows', 'cols'}
        if tile & set(query):
            if set(query) != tile:
                raise ValueError('An exact PAE window requires rowStart, colStart, rows and cols only.')
            arguments = dict(row_start=number('rowStart', 0, 1000000),
                             col_start=number('colStart', 0, 1000000),
                             rows=number('rows', 1, 128), cols=number('cols', 1, 128))
        else:
            arguments = dict(max_size=number('maxSize', 1, 256) if 'maxSize' in query else 256)
        descriptor = (result or {}).get('paeArtifact')
        if not isinstance(descriptor, dict) or descriptor.get('status') != 'available':
            raise KeyError('This result has no indexed PAE artifact.')
        from .result_artifacts import read_pae
        self._json(200, read_pae(descriptor, root, **arguments))

    def _local_request(self):
        if self.app.distributed.enabled and self.client_address[0] not in {'127.0.0.1', '::1'}:
            return False
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            hostname, port = parsed.hostname, parsed.port
        except ValueError:
            return False
        allowed = {"localhost", "127.0.0.1", "::1"}
        allowed.update(h.strip().lower() for h in os.environ.get("PROTEIN_STUDIO_ALLOWED_HOSTS", "").split(",") if h.strip())
        if hostname not in allowed or parsed.username or parsed.password or parsed.path or not host:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                source = urlsplit(origin)
                expected_port = port or 80
                if source.scheme != "http" or source.hostname != hostname or (source.port or 80) != expected_port:
                    return False
            except ValueError:
                return False
        # Same-origin application: no wildcard CORS headers are ever emitted.
        return True

    def _headers(self, status, content_type, length, **headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()

    def _json(self, status, payload):
        body = json.dumps(public_urls(payload, self.app.base_path), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self, required=True):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ValueError("Chunked request bodies are not supported.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            self.close_connection = True
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise OverflowError("Request body exceeds the 12 MiB limit.")
        if length == 0:
            if required:
                raise ValueError("A JSON request body is required.")
            return {}
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.close_connection = True
            raise TypeError("Content-Type must be application/json.")
        self.connection.settimeout(15)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Incomplete request body.")
        try:
            payload = json.loads(data, parse_constant=lambda v: (_ for _ in ()).throw(ValueError("Non-finite JSON values are not supported.")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _file(self, path, download=False):
        if not download and path == self.app.web_dir / "index.html":
            body = index_html(path.read_text(encoding="utf-8"), self.app.base_path).encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in {".pdb", ".cif", ".mmcif"}:
            content_type = "text/plain; charset=utf-8"
        headers = {}
        if download:
            headers["Content_Disposition"] = "attachment; filename*=UTF-8''" + quote(path.name)
        with path.open("rb") as handle:
            self._headers(200, content_type, os.fstat(handle.fileno()).st_size, **headers)
            if self.command != "HEAD":
                shutil.copyfileobj(handle, self.wfile, length=64 * 1024)

    def _campaign_export(self, campaign_id, query):
        """Snapshot metadata in bounded pages, then stream one downloadable file.

        The database read snapshot ends before sending to a slow client. Large
        coordinates, PAE, sequences and private filesystem paths are not exported.
        The export accepts the item listing's score sort/threshold parameters so
        a screening workflow downloads exactly the selected subset; legacy rows
        are scored by the paged backfill before the snapshot is taken.
        """
        from .campaigns import STAGES, TERMINAL, parse_score_query
        stage = query.get('stage', [None])[0]
        if stage and stage not in STAGES:
            raise ValueError('Unknown pipeline stage.')
        order, thresholds = parse_score_query(**score_query_params(query))
        store = self.app.pipeline.store
        store.backfill_scores(campaign_id)
        with tempfile.TemporaryFile(mode='w+b', dir=self.app.data_dir) as handle:
            with store.connection() as conn:
                conn.execute('BEGIN')
                campaign = conn.execute('SELECT status FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
                if campaign is None:
                    raise KeyError('Campaign not found.')
                conditions, params = ['campaign_id=?'], [campaign_id]
                if stage:
                    if campaign['status'] == 'cancelled' and stage == 'cancelled':
                        conditions.append("stage NOT IN ('completed','failed')")
                    elif campaign['status'] == 'cancelled' and stage not in TERMINAL:
                        conditions.append('0')
                    else:
                        conditions.append('stage=?')
                        params.append(stage)
                for column, value in thresholds:
                    # Unscored rows never pass a score threshold.
                    conditions.append(column + '>=?')
                    params.append(value)
                cursor = conn.execute('''SELECT id,campaign_id,target_id,input_hash,sequence_length,stage,attempt,error,failed_stage,
                    cache_hit,preparation_reuse,preparation_activity,prepare_not_before,prepare_defer_count,
                    created_at,updated_at,prep_seconds,predict_seconds,validate_seconds,result_path,
                    score_ranking,score_iptm,score_ptm,score_plddt,ranking_metric,
                    json_extract(input_json,'$.name') AS name FROM campaign_items WHERE ''' +
                    ' AND '.join(conditions) + ' ORDER BY ' + order, params)
                while rows := cursor.fetchmany(200):
                    for row in rows:
                        item = store._public_item(row, campaign['status'] == 'cancelled')
                        item.pop('resultPath', None)
                        base = f"/api/campaigns/{campaign_id}/items/{item['id']}"
                        item.update(resultDetailUrl=base, structureUrl=base+'/structure' if item['hasResult'] else None)
                        handle.write((json.dumps(public_urls(item, self.app.base_path), ensure_ascii=False, allow_nan=False)+'\n').encode('utf-8'))
            size = handle.tell()
            handle.seek(0)
            self._headers(200, 'application/x-ndjson; charset=utf-8', size,
                          Content_Disposition=f'attachment; filename="campaign-{campaign_id}-{stage or "all"}.jsonl"',
                          X_Export_Scope='consistent-metadata-snapshot')
            if self.command != 'HEAD':
                shutil.copyfileobj(handle, self.wfile, length=64*1024)

    def _dispatch(self):
        original = urlsplit(self.path)
        internal = strip_request_target(self.path, self.app.base_path)
        if self.app.base_path and original.path == self.app.base_path and self.command in {"GET", "HEAD"}:
            location = self.app.base_path + "/" + ("?" + original.query if original.query else "")
            self._headers(308, "text/plain", 0, Location=location)
            return
        self.path = internal
        if urlsplit(self.path).path.startswith('/worker/'):
            self.app.distributed.handle(self)
            return
        if not self._local_request():
            self.close_connection = True
            self._json(403, {"error": "Only requests from the configured local same-origin application are allowed."})
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if "\x00" in path:
            raise ValueError("Invalid request path.")
        parts = [part for part in path.split("/") if part]
        method = "GET" if self.command == "HEAD" else self.command
        if method == "OPTIONS":
            self._headers(204, "text/plain", 0, Allow="GET, HEAD, POST, DELETE, OPTIONS")
            return
        if path == "/sources.json" and method == "GET":
            source_file = ROOT / "sources.json"
            if not source_file.is_file():
                raise KeyError("Source registry not available.")
            self._file(source_file)
            return
        if path == "/api/health" and method == "GET":
            self._json(200, {"status": "ok", "service": "protein-studio", "version": "1.0"})
            return
        if path == "/api/system" and method == "GET":
            self._json(200, self.app.diagnostics())
            return
        if path == "/api/pipeline/config" and method == "GET":
            self._json(200, self.app.pipeline.config())
            return
        if path == "/api/cluster" and method == "GET":
            self._json(200, self.app.distributed.cluster())
            return
        if path == '/api/storage' and method == 'GET':
            self._json(200, self.app.storage.snapshot())
            return
        if len(parts) >= 3 and parts[:2] == ['api', 'storage']:
            if len(parts) == 3 and parts[2] in {'scan', 'plan', 'apply'} and method == 'POST':
                self._json(202, self.app.storage.start(parts[2], self._read_json()))
                return
            if len(parts) == 3 and parts[2] == 'cancel' and method == 'POST':
                body = self._read_json()
                if not isinstance(body, dict) or set(body) != {'jobId'}:
                    raise ValueError('Storage cancellation requires jobId only.')
                self._json(200, self.app.storage.cancel(body['jobId']))
                return
            if method == 'GET' and (parts[2] == 'entries' or (len(parts) == 4 and parts[2] == 'plans')):
                query = parse_qs(parsed.query)
                limit = max(1, min(200, int(query.get('limit', ['50'])[0])))
                offset = max(0, int(query.get('offset', ['0'])[0]))
                if parts[2] == 'entries' and len(parts) == 3:
                    self._json(200, self.app.storage.manager.entries(query.get('scanId', [''])[0], limit, offset,
                                                                    query.get('kind', [None])[0]))
                    return
                if parts[2] == 'plans' and JOB_ID.fullmatch(parts[3]):
                    self._json(200, self.app.storage.manager.plan_details(parts[3], limit, offset))
                    return
            raise KeyError('Storage endpoint not found.')
        if path == "/api/databases" and method == "GET":
            self._json(200, self.app.database_profiles())
            return
        if path == "/api/campaigns":
            if method == "GET":
                query = parse_qs(parsed.query)
                limit = max(1, min(200, int(query.get("limit", ["50"])[0])))
                offset = max(0, int(query.get("offset", ["0"])[0]))
                self._json(200, self.app.pipeline.store.list(limit, offset))
                return
            if method == "POST":
                self._json(201, self.app.create_campaign(self._read_json()))
                return
        if len(parts) >= 3 and parts[:2] == ["api", "campaigns"]:
            campaign_id = parts[2]
            if not JOB_ID.fullmatch(campaign_id):
                raise KeyError("Campaign not found.")
            if len(parts) == 3 and method == "GET":
                campaign = self.app.pipeline.store.get(campaign_id)
                if campaign is None:
                    raise KeyError("Campaign not found.")
                self._json(200, campaign)
                return
            if len(parts) == 4 and parts[3] in {"pause", "resume", "cancel", "seal"} and method == "POST":
                self._read_json(required=False)
                self._json(200, self.app.pipeline.store.control(campaign_id, parts[3]))
                self.app.pipeline.wakeup.set()
                return
            if len(parts) == 4 and parts[3] == "retry-failed" and method == "POST":
                self._read_json(required=False)
                self._json(200, self.app.pipeline.store.retry_failed(campaign_id))
                self.app.pipeline.wakeup.set()
                return
            if len(parts) == 4 and parts[3] == 'export' and method == 'GET':
                self._campaign_export(campaign_id, parse_qs(parsed.query))
                return
            if len(parts) == 4 and parts[3] == "items":
                if method == "POST":
                    self._json(201, self.app.append_campaign_items(campaign_id, self._read_json()))
                    return
                if method == "GET":
                    query = parse_qs(parsed.query)
                    limit = max(1, min(200, int(query.get("limit", ["25"])[0])))
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    score_query = score_query_params(query)
                    self._json(200, self.app.pipeline.store.items(campaign_id, limit, offset,
                                query.get("stage", [None])[0], **score_query))
                    return
            if len(parts) in {5, 6} and parts[3] == "items" and method == "GET":
                item_id = parts[4]
                if not JOB_ID.fullmatch(item_id):
                    raise KeyError("Campaign item not found.")
                if len(parts) == 5:
                    self._json(200, self.app.pipeline.detail(campaign_id, item_id))
                    return
                if parts[5] == 'pae':
                    item = self.app.pipeline.detail(campaign_id, item_id)
                    root = self.app.pipeline.items_root / campaign_id / item_id
                    self._pae(item.get('result'), root, parsed.query)
                    return
                if parts[5] in {"structure", "artifact"}:
                    name = "__structure__" if parts[5] == "structure" else parse_qs(parsed.query).get("name", [""])[0]
                    self._file(self.app.pipeline.artifact(campaign_id, item_id, name), download=True)
                    return
        if path == "/api/jobs":
            if method == "GET":
                query = parse_qs(parsed.query)
                try:
                    limit = max(1, min(int(query.get("limit", ["200"])[0]), 1000))
                except ValueError:
                    raise ValueError("limit must be an integer")
                summaries = []
                for job in self.app.store.list(limit):
                    summary = public_job(job)
                    if isinstance(summary.get("result"), dict):
                        summary["result"] = {key: value for key, value in summary["result"].items()
                                             if key in {"sequenceLength", "meanPlddt", "ptm", "iptm", "structureFormat",
                                                        "structureUrl", "provenance", "timings", "warnings"}}
                    summaries.append(summary)
                self._json(200, summaries)
                return
            if method == "POST":
                self._json(201, self.app.submit(self._read_json()))
                return
        if path == "/api/jobs/batch" and method == "POST":
            self._json(201, self.app.submit_batch(self._read_json()))
            return
        if path == "/api/import" and method == "POST":
            self._json(201, self.app.import_structure(self._read_json()))
            return
        if len(parts) == 3 and parts[:2] == ["api", "examples"] and method == "GET":
            if parts[2] not in {"ubiquitin", "crambin", "ubiquitin-afdb"}:
                raise KeyError("Example not found.")
            self._json(200, self.app.engine.load_example(parts[2]))
            return
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            job_id = parts[2]
            if not JOB_ID.fullmatch(job_id):
                raise KeyError("Task not found.")
            if len(parts) == 3:
                if method == "GET":
                    job = self.app.store.get(job_id)
                    if job is None:
                        raise KeyError("Task not found.")
                    self._json(200, public_job(job))
                    return
                if method == "DELETE":
                    self._read_json(required=False)
                    self.app.delete(job_id)
                    self._json(200, {"deleted": True, "id": job_id})
                    return
            if len(parts) == 4:
                action = parts[3]
                if method == 'GET' and action == 'pae':
                    job = self.app.store.get(job_id)
                    if job is None:
                        raise KeyError('Task not found.')
                    self._pae(job.get('result'), self.app.jobs_dir / job_id, parsed.query)
                    return
                if method == "POST" and action == "cancel":
                    self._read_json(required=False)
                    job = self.app.store.cancel(job_id)
                    if job is None:
                        raise KeyError("Task not found.")
                    self._json(200, public_job(job))
                    return
                if method == "POST" and action == "retry":
                    self._read_json(required=False)
                    self._json(201, self.app.retry(job_id))
                    return
                if method == "GET" and action in {"structure", "artifact"}:
                    name = "__structure__" if action == "structure" else parse_qs(parsed.query).get("name", [""])[0]
                    self._file(self.app.artifact(job_id, name), download=True)
                    return
                if method == "GET" and action == "archive":
                    handle, size = self.app.archive(job_id)
                    with handle:
                        self._headers(200, "application/zip", size,
                                      Content_Disposition=f'attachment; filename="job-{job_id}.zip"',
                                      X_Export_Scope="consistent-artifact-snapshot")
                        if self.command != "HEAD":
                            shutil.copyfileobj(handle, self.wfile, length=64 * 1024)
                    return
        if path.startswith("/api/"):
            raise KeyError("API route not found.")
        if method != "GET":
            self._json(405, {"error": "Method not allowed."})
            return
        requested = (self.app.web_dir / (path.lstrip("/") or "index.html")).resolve()
        if not requested.is_relative_to(self.app.web_dir) or not requested.is_file():
            raise KeyError("File not found.")
        self._file(requested)

    def _handle(self):
        try:
            self._dispatch()
        except AuthenticationError as exc:
            self.close_connection = True
            self._json(401, {"error": str(exc)})
        except ConflictError as exc:
            self._json(409, {"error": str(exc)})
        except KeyError as exc:
            self._json(404, {"error": str(exc.args[0])})
        except OverflowError as exc:
            self._json(413, {"error": str(exc)})
        except TypeError as exc:
            self._json(415, {"error": str(exc)})
        except (ValueError, UnicodeError) as exc:
            self._json(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (socket.timeout, TimeoutError):
            self.close_connection = True
        except Exception:
            LOG.exception("Unhandled HTTP request error")
            self._json(500, {"error": "Internal server error; see the local server log."})

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("PROTEIN_STUDIO_HOST", "127.0.0.1")
    port = int(os.environ.get("PROTEIN_STUDIO_PORT", "8765"))
    app = StudioApplication(data_dir=os.environ.get("PROTEIN_STUDIO_DATA_DIR"),
                            workers=int(os.environ.get("PROTEIN_STUDIO_WORKERS", "1")))
    try:
        server = StudioHTTPServer((host, port), app)
    except BaseException:
        app.close()
        raise
    app.start()
    def request_shutdown(signum, frame):
        app.stop_event.set()
        app.wakeup.set()
        # shutdown() waits for serve_forever, so run it outside the signal/main thread.
        threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()
    signal.signal(signal.SIGTERM, request_shutdown)
    LOG.info("Protein Studio: http://%s:%s", host, server.server_port)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        LOG.warning("Binding a non-loopback interface: use network isolation; this local app has no user authentication.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
