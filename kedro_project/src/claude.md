# Working with the Kedro + MCP Setup

A practical guide for Claude Code sessions on this project.

---

## Architecture

```
Claude Code (this session)          MCP Server (Docker container)
/workspace  ──── source code ───►  runs Kedro pipelines
                                   holds data/ on its own filesystem
            ◄─── MCP tools ──────  exposes results via SSE
```

The Kedro project lives in `/workspace/src/` and you edit it here. The MCP server at `http://mcp_server:8000/sse` picks up your changes and executes the pipeline. The **data directory does not exist in `/workspace`** — it lives inside the container. You cannot browse or edit data files directly.

---

## MCP Tools

Five tools are available. Load them with `ToolSearch` before calling:

```
select:mcp__kedro-runner__run_pipeline,mcp__kedro-runner__get_run_status,
       mcp__kedro-runner__get_run_error,mcp__kedro-runner__list_runs,
       mcp__kedro-runner__get_metrics
```

| Tool | Purpose |
|------|---------|
| `run_pipeline` | Kick off a run (returns a `run_id` immediately) |
| `get_run_status` | Poll for completion; check `nodes_completed` / `nodes_total` |
| `get_run_error` | Get structured error info for a failed run |
| `get_metrics` | Read the latest tracked metrics from `data/09_tracking/` |

Runs are **async** — `run_pipeline` returns before the pipeline finishes. Always poll `get_run_status` until `status` is `"completed"` or `"failed"`.

---

## Debugging Workflow

The MCP error tool is **data-free by design**: it reports the exception type and source location but deliberately omits the exception message and stdout. This protects sensitive data but makes debugging less direct.

Typical loop:

1. `run_pipeline` → get `run_id`
2. `get_run_status` (poll until done)
3. On failure → `get_run_error` → read `error_type` and `project_frames`
4. Open the file + line indicated in `project_frames` and reason from the exception type
5. Fix the code, re-run

Because you can't see the exception message, you reason from:
- **`error_type`** — the Python exception class (`ValueError`, `KeyError`, etc.)
- **`location` / `project_frames`** — the innermost project frame and the call chain leading to it
- **`nodes_seen` / `nodes_completed`** in `get_run_status` — tells you how far the pipeline got before failing

The stack starts from the innermost project frame. Framework frames (Kedro internals, sklearn, pandas) are filtered out, leaving only your project code.

---

## Project Structure

```
/workspace/
├── conf/base/
│   ├── catalog.yml        # Dataset definitions (paths, types)
│   └── parameters.yml     # Pipeline parameters
├── src/dummy_project/
│   ├── pipeline_registry.py   # Registers all pipelines; sets __default__
│   ├── hooks.py               # ConfineWritesToData safety hook
│   └── pipelines/
│       ├── data_science/      # split → train → evaluate
│       └── failing/           # intentional-error pipeline for MCP testing
└── .mcp.json              # Points to http://mcp_server:8000/sse
```

The `failing` pipeline exists to exercise the MCP error path. It is excluded from `__default__` in `pipeline_registry.py`. To trigger it explicitly: `run_pipeline(pipeline="failing")`.

---

## Environment Constraints

- **No `python`, `curl`, `wget`, `nc`** in the Claude Code shell. You cannot execute Python snippets or hit HTTP endpoints directly.
- **No access to the container filesystem.** You cannot read `data/01_raw/`, inspect logs on the MCP server side, or write data files.
- **Changes to source files are picked up immediately** — the MCP server reads from `/workspace/src/` on each run, so there is no rebuild step.
- The only shell tools reliably available are `find`, `grep`, `git`, and file I/O tools (`Read`, `Edit`, `Write`).

---

## Kedro-Specific Notes

- `find_pipelines()` **calls `create_pipeline()` for every discovered pipeline** at startup, before any node runs. A `ValueError` or import error inside `create_pipeline()` will surface as a registration failure, not a node failure — check `project_frames` for which pipeline's `create_pipeline` is implicated.
- Nodes must have at least one input **or** one output. A node with both `inputs=None` and `outputs=None` is rejected by Kedro's DAG validator at construction time.
- Intermediate datasets not listed in `catalog.yml` automatically use `MemoryDataset` — no catalog entry needed for ephemeral values.
- `parameters.yml` values are accessed in nodes via `params:model_options` (prefixed), not by raw key name.

---

## Metrics

After a successful run, `get_metrics` reads the latest file under `data/09_tracking/`. The catalog entry uses `versioned: true`, so each run appends a timestamped copy — `get_metrics` always returns the most recent one regardless of `run_id`.
