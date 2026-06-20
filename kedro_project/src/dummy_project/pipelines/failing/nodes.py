"""Node(s) for the deliberately failing pipeline.

This exists only to test what the MCP bridge returns when a Kedro run blows up:
the run should end with status "failed" and a non-zero returncode, and
get_run_error should report the exception type and this file's location (never
the message or any data). It does NOT touch the data catalog, so the failure
path can be exercised without involving any real dataset.
"""
from __future__ import annotations


def boom():
    """Always raise, to simulate a node failing mid-pipeline."""
    raise RuntimeError(
        "Intentional failure from the 'failing' pipeline — "
        "this is expected, it exists to test the MCP error path."
    )
