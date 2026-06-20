"""Run a Kedro pipeline in an isolated subprocess and report failures safely.

server.py launches this as a SEPARATE process (never in-process: a node must not
share the MCP server's memory). On success it just runs the pipeline. On failure
it prints a single JSON line, prefixed with ERROR_SENTINEL, describing ONLY:
  - the exception type name, and
  - the source location(s) inside the project (module path relative to src/,
    line number, function name).
It never prints the exception message or any dataset content, so the error report
that crosses the MCP bridge cannot carry data.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# Must match ERROR_SENTINEL in server.py.
ERROR_SENTINEL = "@@MCP_ERROR_JSON@@"


def _frames(exc: BaseException, src_root: Path) -> list[dict]:
    """Turn a traceback into a list of data-free frame descriptors."""
    frames: list[dict] = []
    for fr in traceback.extract_tb(exc.__traceback__):
        try:
            module = str(Path(fr.filename).resolve().relative_to(src_root))
            in_project = True
        except ValueError:
            module = Path(fr.filename).name  # library/framework frame
            in_project = False
        frames.append(
            {
                "module": module,
                "in_project": in_project,
                "line": fr.lineno,
                "function": fr.name,
            }
        )
    return frames


def main() -> int:
    project_path = Path(os.environ.get("KEDRO_PROJECT_PATH", ".")).resolve()
    src_root = (project_path / "src").resolve()
    pipeline = sys.argv[1] if len(sys.argv) > 1 else "__default__"

    try:
        from kedro.framework.session import KedroSession
        from kedro.framework.startup import bootstrap_project

        bootstrap_project(project_path)
        with KedroSession.create(project_path=project_path) as session:
            if pipeline and pipeline != "__default__":
                session.run(pipeline_name=pipeline)
            else:
                session.run()
    except Exception as exc:  # noqa: BLE001 — report any failure, data-free
        all_frames = _frames(exc, src_root)
        project_frames = [f for f in all_frames if f["in_project"]]
        # Where the user's code failed: deepest project frame, else deepest frame.
        location = (
            project_frames[-1]
            if project_frames
            else (all_frames[-1] if all_frames else None)
        )
        payload = {
            "error_type": type(exc).__name__,
            "location": location,
            "project_frames": project_frames,
        }
        # Printed LAST (after all node output) so the server can trust the final
        # sentinel line. No message, no data — only type + code locations.
        print(ERROR_SENTINEL + json.dumps(payload), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
