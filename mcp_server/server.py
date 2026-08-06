"""MCP server that runs Kedro pipelines for the secure side of the bridge.

It lets a connected Claude Code trigger pipeline runs and read back aggregated
results (node status, numeric metrics, structured error locations) WITHOUT
exposing the underlying data. Raw datasets and raw stdout never cross this
boundary — only the curated values returned by the tools below do. This matters
because the dev-side Claude can edit pipeline code freely, so these tool outputs
are the *only* return channel and must not carry data.

Tools: run_pipeline, get_run_status, get_metrics, get_run_error, list_runs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

KEDRO_PROJECT_PATH = Path(
    os.environ.get("KEDRO_PROJECT_PATH", "./kedro_project")
).resolve()
# Named for what it is, not for the Kedro layer convention it replaces: this is
# the one directory whose contents leave the secure side. A path the reader has
# to look up is a path someone eventually writes to by accident.
TRACKING_DIR = KEDRO_PROJECT_PATH / "data" / "claude_visible_metrics"

# The pipeline runs in a SEPARATE subprocess (isolation: a node must never share
# the MCP server's memory). pipeline_runner.py executes the pipeline and, on
# failure, prints a single structured, data-free line prefixed with this
# sentinel. The server trusts only the LAST such line (the runner prints its
# authentic payload after all node output, so node-printed fakes are overridden).
RUNNER_PATH = Path(__file__).resolve().parent / "pipeline_runner.py"
ERROR_SENTINEL = "@@MCP_ERROR_JSON@@"

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP("kedro-runner", host=HOST, port=PORT)

# Kedro logs one "Running node: <name>: ..." line per node and a running
# "Completed N out of M tasks" counter. We parse both to report progress.
_NODE_RE = re.compile(r"Running node:\s*([^:]+):")
_PROGRESS_RE = re.compile(r"Completed (\d+) out of (\d+) tasks")

_runs: "dict[str, Run]" = {}
_runs_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Run:
    """In-memory record of a single pipeline run."""

    def __init__(self, run_id: str, pipeline: str):
        self.run_id = run_id
        self.pipeline = pipeline
        self.status = "starting"  # starting | running | completed | failed
        self.returncode: Optional[int] = None
        self.nodes: list[str] = []
        self.completed = 0
        self.total: Optional[int] = None
        # Captured stdout is kept ONLY for internal progress/error parsing on the
        # secure side. It is never returned by any tool (there is no get_logs).
        self.logs: list[str] = []
        self.error: Optional[dict] = None
        self.started_at = _now()
        self.ended_at: Optional[str] = None
        self.lock = threading.Lock()

    def status_dict(self) -> dict:
        with self.lock:
            return {
                "run_id": self.run_id,
                "pipeline": self.pipeline,
                "status": self.status,
                "returncode": self.returncode,
                "nodes_seen": list(self.nodes),
                "nodes_completed": self.completed,
                "nodes_total": self.total,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            }


def _execute(run: Run) -> None:
    """Run the pipeline runner as a subprocess and stream its stdout."""
    cmd = ["python", str(RUNNER_PATH), run.pipeline]

    with run.lock:
        run.status = "running"
        run.logs.append(f"[mcp_server] $ {' '.join(cmd)} (cwd={KEDRO_PROJECT_PATH})")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(KEDRO_PROJECT_PATH),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            with run.lock:
                run.logs.append(line)
                if ERROR_SENTINEL in line:
                    # Last sentinel wins: the runner prints its authentic,
                    # data-free payload after all node output.
                    try:
                        run.error = json.loads(line.split(ERROR_SENTINEL, 1)[1])
                    except (ValueError, IndexError):
                        pass
                node_match = _NODE_RE.search(line)
                if node_match:
                    node = node_match.group(1).strip()
                    if node not in run.nodes:
                        run.nodes.append(node)
                progress_match = _PROGRESS_RE.search(line)
                if progress_match:
                    run.completed = int(progress_match.group(1))
                    run.total = int(progress_match.group(2))
        proc.wait()
        with run.lock:
            run.returncode = proc.returncode
            run.status = "completed" if proc.returncode == 0 else "failed"
            run.ended_at = _now()
    except Exception as exc:  # noqa: BLE001 — surface any launch failure to caller
        with run.lock:
            run.status = "failed"
            run.logs.append(f"[mcp_server] error: {exc}")
            run.ended_at = _now()


# Bounds on what get_metrics returns. Metric *keys* are code-controlled (the dev
# side edits the pipeline), so flattening nested dicts widens the residual key
# channel noted below. These caps keep it a trickle rather than a stream.
MAX_METRIC_DEPTH = 4
MAX_METRIC_KEYS = 200


def _flatten_numeric(obj, prefix: str = "", depth: int = 0) -> dict:
    """Collect numeric leaves from a (possibly nested) dict as dotted keys.

    Numbers only, bool excluded. Everything else is dropped — including LISTS,
    deliberately: a list of floats is a perfectly good carrier for an entire
    data column, so it must not survive flattening. Do not "fix" that by
    iterating lists here; it would reopen the bulk-exfiltration channel this
    whole server exists to close.
    """
    out: dict = {}
    if depth > MAX_METRIC_DEPTH or not isinstance(obj, dict):
        return out
    for key, value in obj.items():
        name = f"{prefix}{key}"
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[name] = value
        elif isinstance(value, dict):
            out.update(_flatten_numeric(value, f"{name}.", depth + 1))
        # str / list / None fall through: they could carry data.
    return out


def _latest_per_dataset() -> "list[Path]":
    """Newest file for each tracked dataset under TRACKING_DIR.

    Kedro versioned datasets land at
    ``<dataset>.json/<version>/<dataset>.json``, so the first path component
    under TRACKING_DIR identifies the dataset and the newest file within it is
    the current version. An unversioned ``<dataset>.json`` is its own
    single-file group. Taking the newest file overall (the old behaviour) would
    return just one dataset on a project that tracks several.
    """
    groups: "dict[str, list[Path]]" = {}
    for path in TRACKING_DIR.rglob("*.json"):
        if not path.is_file():
            continue
        groups.setdefault(path.relative_to(TRACKING_DIR).parts[0], []).append(path)
    return [max(paths, key=lambda p: p.stat().st_mtime) for paths in groups.values()]


def _load_metrics() -> dict:
    """Merge the latest numeric metrics from every dataset in the visible dir.

    Only NUMERIC values cross the boundary — strings, arrays, and objects are
    dropped, because a pipeline node (editable from the dev side) could
    otherwise smuggle raw data rows out as string "metrics". Nested objects are
    flattened to dotted keys rather than discarded, so a metrics dict shaped
    like ``{"model_a": {"auc": 0.9}}`` reports ``model_a.auc`` instead of
    silently returning nothing.
    """
    if not TRACKING_DIR.exists():
        return {"error": f"tracking directory not found: {TRACKING_DIR}"}

    latest = _latest_per_dataset()
    if not latest:
        return {"error": "no metrics written yet — run the pipeline first"}

    metrics: dict = {}
    sources: list = []
    unreadable: list = []
    prefixed = len(latest) > 1  # namespace keys only when datasets could collide

    for path in sorted(latest, key=lambda p: p.stat().st_mtime):
        dataset = path.relative_to(TRACKING_DIR).parts[0]
        if dataset.endswith(".json"):
            dataset = dataset[: -len(".json")]
        try:
            with path.open() as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # Type only, never the message: a decode error can quote the file.
            unreadable.append({"dataset": dataset, "error_type": type(exc).__name__})
            continue
        if not isinstance(raw, dict):
            unreadable.append({"dataset": dataset, "error_type": "NotAnObject"})
            continue
        metrics.update(_flatten_numeric(raw, f"{dataset}." if prefixed else ""))
        sources.append(
            {
                "dataset": dataset,
                "path": str(path.relative_to(KEDRO_PROJECT_PATH)),
                "modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )

    result: dict = {"metrics": metrics, "sources": sources}
    if len(metrics) > MAX_METRIC_KEYS:
        kept = sorted(metrics)[:MAX_METRIC_KEYS]
        result["truncated"] = len(metrics) - MAX_METRIC_KEYS
        result["metrics"] = {k: metrics[k] for k in kept}
    if unreadable:
        result["unreadable"] = unreadable
    return result


@mcp.tool()
def run_pipeline(pipeline: str = "__default__") -> dict:
    """Trigger a Kedro pipeline run in the background.

    Args:
        pipeline: Pipeline name to run, or "__default__" for the full pipeline.

    Returns:
        A dict with the new ``run_id``; poll it with get_run_status, then read
        get_metrics (on success) or get_run_error (on failure).
    """
    run_id = uuid.uuid4().hex[:8]
    run = Run(run_id, pipeline)
    with _runs_lock:
        _runs[run_id] = run
    threading.Thread(target=_execute, args=(run,), daemon=True).start()
    return {"run_id": run_id, "status": "started", "pipeline": pipeline}


@mcp.tool()
def get_run_status(run_id: str) -> dict:
    """Return a run's status (running/completed/failed) and node progress."""
    run = _runs.get(run_id)
    if run is None:
        return {"error": f"unknown run_id: {run_id}"}
    return run.status_dict()


@mcp.tool()
def get_metrics(run_id: Optional[str] = None) -> dict:
    """Return numeric metrics from data/claude_visible_metrics.

    Merges the newest version of every tracked dataset. Nested objects become
    dotted keys; strings, lists, and booleans are dropped. When more than one
    dataset is tracked, keys are prefixed with the dataset name. At most 200
    keys cross per call, counted after the merge.

    ``run_id`` is accepted for symmetry but ignored: metrics always reflect the
    most recent tracked run on the secure side.
    """
    return _load_metrics()


@mcp.tool()
def get_run_error(run_id: str) -> dict:
    """Return structured, data-free error info for a failed run.

    On failure this reports the exception type and the source location(s) in the
    project where it occurred (module path relative to src/, line number, and
    function). It deliberately OMITS the exception message and raw stdout, both
    of which could contain data. Use it to debug pipeline code without ever
    seeing the data that code ran against.
    """
    run = _runs.get(run_id)
    if run is None:
        return {"error": f"unknown run_id: {run_id}"}
    with run.lock:
        status = run.status
        returncode = run.returncode
        err = run.error
    if status != "failed":
        return {
            "run_id": run_id,
            "status": status,
            "message": "run has not failed; no error to report",
        }
    if err is None:
        return {
            "run_id": run_id,
            "status": status,
            "returncode": returncode,
            "error_type": None,
            "message": (
                "run failed before the pipeline produced a structured error "
                "(e.g. the project failed to bootstrap)"
            ),
        }
    return {"run_id": run_id, "status": status, "returncode": returncode, **err}


@mcp.tool()
def list_runs() -> dict:
    """List all runs triggered during this server session."""
    with _runs_lock:
        return {"runs": [r.status_dict() for r in _runs.values()]}


if __name__ == "__main__":
    print(f"[mcp_server] kedro project: {KEDRO_PROJECT_PATH}")
    print(f"[mcp_server] serving SSE on http://{HOST}:{PORT}/sse")
    mcp.run(transport="sse")
