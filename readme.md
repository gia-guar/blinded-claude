# blinded-claude

**Let Claude Code develop and run data pipelines without ever seeing the data.**

`blinded-claude` is a two-container Docker starter. One container runs Claude
Code with nothing but **editable source code**; the other holds the **regulated
data** and runs the pipelines. Claude reaches the data side *only* through a
handful of MCP tools that return aggregated metrics and structured error
locations — never raw rows. The result: an AI agent that can author, run, and
debug a [Kedro](https://kedro.org) pipeline while staying blind to the dataset it
runs against.

---

## Why this exists

If you work with sensitive or regulated data, you can't just point an LLM agent
at it. But you still want the agent's help building the pipeline. The trick is to
**separate the place code is written from the place data lives**, and to make the
only channel between them a small, auditable set of return values.

![img](blinded-claude-visual-explainer.svg)

```
   dev container (Claude Code)            mcp_server container (the data side)
 ┌───────────────────────────┐         ┌───────────────────────────────────┐
 │ • Claude Code             │  MCP/SSE │ • runs Kedro pipelines            │
 │ • editable src/ + conf/   │ ───────► │ • holds data/ (regulated)         │
 │ • NO data, NO route to    │ ◄─────── │ • read-only filesystem            │
 │   the data side's data     │ results │ • NO internet                     │
 └───────────────────────────┘         └───────────────────────────────────┘
        external net                          internal-only bridge net
     (Anthropic API only)                  (no route to the internet)
```

**The invariant that makes it sound:** once Claude can edit pipeline code, the
data's only escape route is *whatever the MCP tools return*. So:

- `data/` and `conf/local/` (credentials) are **never** mounted into the dev
  container — only `conf/base/` is.
- `src/` and `conf/` are **read-only** on the data side (kernel-enforced
  read-only filesystem; `data/` is the single writable path).
- The data-side container has **no internet** (internal-only Docker network).
- The MCP tools return **numbers and code locations only** — no dataset rows, no
  raw stdout, no exception messages.

No shared writable filesystem + no network egress + curated tool outputs ⇒ the
loop is closed.

---

## What the agent can and can't see

| MCP tool | Returns | Carries data? |
|----------|---------|---------------|
| `run_pipeline` | a `run_id` | no |
| `get_run_status` | status + node progress counts | no |
| `get_metrics` | **numeric** metrics only (strings/lists dropped, nested objects flattened to dotted keys) | no |
| `get_run_error` | exception **type** + source **location** (file/line/function) | no |
| `list_runs` | all runs this session | no |

`get_run_error` deliberately
omits the exception *message* and reports only the exception class and where in
your code it occurred, so Claude can debug a `KeyError` at `nodes.py:42` without
seeing the offending value. This is to prevent unintentional leaks from third pary
dependencies.

> **Residual channels (by design).** A few low-bandwidth signals remain: return
> code, node count/names, run timing, metric *keys*, and the exception class
> name. These leak a few bytes per run, not bulk data. If your threat model needs
> them closed, add a metric-key allowlist and normalize statuses/timings — see
> `mcp_server/server.py`.

---

## Repository layout

```
.
├── blinded_init.py             # setup wizard — points the harness at any Kedro project
├── template/                   # {{TOKEN}} templates the wizard fills in
├── tests/                      # unit tests for the wizard + the metrics filter
├── docker-compose.yml          # the two-container boundary, for the bundled demo
├── .devcontainer/
│   └── devcontainer.json       # VS Code "Reopen in Container" → dev service
├── dev_container/
│   ├── Dockerfile              # node:20-slim + Claude Code (no python, no data)
│   └── claude_mcp_config.json  # points Claude at http://mcp_server:8000/sse
├── mcp_server/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py               # MCP server: the 5 tools above
│   └── pipeline_runner.py      # runs Kedro in an isolated subprocess, data-free errors
└── kedro_project/
    ├── conf/base/              # catalog.yml + parameters.yml (editable on both sides)
    ├── conf/local/             # credentials (gitignored; data side only, never on the agent side)
    ├── data/                   # lives on the data side only (gitignored, never committed)
    ├── seed_data.py            # generates synthetic demo data
    └── src/dummy_project/      # pipelines: data_science (+ failing, for error tests)
```

---

## Prerequisites

- **Docker Desktop** (with Compose v2). On Windows, the WSL2 backend is fine.
- A **Claude subscription** (for OAuth login) or an Anthropic API setup token.
- *Optional:* **VS Code** + the **Dev Containers** extension, if you want the
  "Reopen in Container" workflow instead of the terminal.

---

## Quick start — the setup wizard

`blinded_init.py` points the harness at a Kedro project of your own. It asks
whether you're starting from scratch or wrapping something that already exists,
inspects the target, and writes a `blinded/` folder with every path substituted.
Stdlib only, no dependencies:

```bash
python blinded_init.py
```

```
Are you starting from scratch?
  [1] Yes — scaffold a new Kedro project with the harness wired in
  [2] No  — wrap an existing Kedro project
> 2
Path to your Kedro project: /work/my-pipeline

...findings...

Experiment tracking
------------------------------------------------------------
  MLflow found in pipeline code.
  ...
  [1] add the overlay   (recommended)
  [2] skip it
> 1
```

The tracking question is asked after inspection, so it knows whether your code
actually uses MLflow and recommends accordingly. `--mlflow` answers it up front;
`--yes` declines it.

Or non-interactively:

```bash
python blinded_init.py --existing /work/my-pipeline
python blinded_init.py --scratch  /work/new-thing --name new_thing
python blinded_init.py --existing /work/my-pipeline --dry-run   # report only
```

It reports what it found before writing anything — dependency source, whether
your catalog writes to `data/claude_visible_metrics/` (the only return channel),
and any outbound calls in pipeline code, which **will** fail on the data side
because it has no internet. Files under `blinded/` are written after one
confirmation; edits to files you already have (`settings.py`, `.dockerignore`)
are shown as a diff and confirmed one at a time. Nothing under `data/` or
`conf/local/` is ever read, mounted into the agent, or written.

The wizard never runs `docker`, `pip` or `kedro` — it prints the commands for
you to run.

> The path you give it is the **Kedro project root** (the directory holding
> `conf/`, `src/` and `data/`), which may sit inside a larger repo. That
> directory is what the generated compose file anchors to.

Useful flags: `--mount DIR` (extra dev-side mount, repeatable), `--python 3.12`
(data-side image), `--mlflow`, `--harness-dir NAME`, `--yes`, `--force`,
`--dry-run`.

---

## Quick start — the bundled demo

To see the boundary work end to end before pointing it at anything real:

```bash
git clone <your-fork-url> blinded-claude
cd blinded-claude

# 1. Build both images.
docker compose build

# 2. Seed synthetic demo data on the data side (no local Python needed).
#    Writes kedro_project/data/01_raw/dummy_data.csv.
docker compose run --rm mcp_server python /app/kedro_project/seed_data.py

# 3. Start the stack.
docker compose up -d

# 4. Log Claude in (OAuth, one time — saved in the claude_config volume).
docker compose exec dev claude
#    ...follow the browser + paste-code prompt, then ask it to run the pipeline.
```

Then, inside that `claude` session, try:

> Run the default pipeline, wait for it to finish, and show me the metrics.

Claude will call `run_pipeline` → poll `get_run_status` → read `get_metrics`.
To exercise the error path:

> Run the `failing` pipeline and tell me where it broke.

Tear everything down with `docker compose down` (add `-v` to also drop the saved
Claude login).

### Alternative: VS Code "Reopen in Container"

Open the folder in VS Code → **Reopen in Container**. This attaches VS Code to
the `dev` service (which starts `mcp_server` via `depends_on`). Open a terminal
and run `claude`. The MCP tools are reachable immediately.

### Alternative auth: inject a token instead of interactive login

Run `claude setup-token` on your host (where a browser is available), then:

```bash
# bash
export CLAUDE_CODE_OAUTH_TOKEN=...    # the token you just generated
docker compose up -d
```
```powershell
# PowerShell
$env:CLAUDE_CODE_OAUTH_TOKEN = "..."
docker compose up -d
```

---

## Using your own data and pipeline

Run `blinded_init.py` against your project (see above) rather than moving your
project in here. Whichever route you take, the same four things have to be true:

1. **Your dataset lives in `data/01_raw/`** (gitignored — it stays local and
   never reaches the dev container or git).
2. **The catalog defines your outputs**, and the only ones that leave the secure
   side are **aggregated metrics** (`json.JSONDataset`, `versioned: true`, under
   `data/claude_visible_metrics/`). The name is deliberate: everything written
   there is visible to the agent, and nothing else is. Without at least one such
   entry the agent gets nothing back. It can add one itself — `src/` and
   `conf/base/` are writable from the dev side — which is expected, not a hole:
   the filtering below is enforced server-side, so it applies to whatever the
   agent writes just as it applies to what you write.
3. **Dependencies are baked into the data-side image at build time.** That
   container has no internet at run time, so nothing can be installed then. The
   wizard wires this up from your `requirements.txt` or
   `[project.dependencies]`; with only a lock file it leaves a `TODO` in the
   generated Dockerfile rather than guessing an export command.
4. **Execution happens on the data side, through the MCP tools.** The dev
   container has no Python and no data, by design.

**Credentials** (DB passwords, API keys for your data sources) go in
`conf/local/credentials.yml`. That path is gitignored **and** mounted only into
the data-side `mcp_server` container — never into the dev container — so
pipelines authenticate at run time while the agent stays blind to the secrets.

The dev container's mount list is an **allowlist**: only the directories named
in it are visible to the agent. `blinded_init.py` refuses to generate a mount
that is, contains, or sits inside `data/` or `conf/local/` — including an
ancestor like `conf/`, which would drag the credentials along with it.

Anything a node writes must stay under `data/` (enforced two ways: the
`ConfineWritesToData` catalog lint, and the read-only container filesystem).

---

## External logging (MLflow, experiment trackers, etc.)

**Out of the box this won't work, by design.** The data-side container is on an
**internal-only network with no route to the internet**, so pipeline code cannot
reach an external `https://mlflow.example.com` That "no egress" property is the
load-bearing part of the security model: since the agent authors the pipeline code,
any outbound channel is a potential exfiltration channel. MLflow in particular is a
*full* data sink, not a metrics-only one (`log_artifact`/`log_dict`/`log_text` upload arbitrary
content, and even tags/params take arbitrary strings), so "just allow MLflow"
would reopen the leak.

**Recommended: run the tracker inside the perimeter.** `blinded_init.py --mlflow`
generates `blinded/docker-compose.mlflow.yml`, a compose overlay that does this:

```bash
docker compose -f docker-compose.yml -f docker-compose.mlflow.yml up -d
```

Your existing `mlflow.log_metrics` / `log_artifact` / `log_figure` calls keep
working unchanged. The topology is what makes it safe:

```
dev ──── mcp_bridge ──── mcp_server ──── tracking ──── mlflow ──── viewer
    └─── external ──► Anthropic API                                  │
                                                          127.0.0.1:5000 (you)
```

The dev container is **not attached to `tracking`**, so the agent has no route
to the artifact store it is filling. Docker's embedded DNS only resolves
container names within a shared network (`mlflow` does not resolve from dev),
and its inter-bridge isolation rules drop forwarding between networks (the
subnet is unreachable by raw IP too). The MLflow UI is published on loopback for
you, on a network mlflow alone is attached to.

Note that this is enforced by topology, **not** by the dev container lacking an
HTTP client — it runs `node:20-slim`, and Node has a global `fetch`. Any
reasoning of the form "the agent has no curl, so it can't make requests" is
wrong. Keep MLflow's backend and artifact stores local, as the overlay
configures them: pointing either at a remote URI would give pipeline code a
relay out.

---

## How it works (under the hood)

- `run_pipeline` launches `pipeline_runner.py` in an **isolated subprocess** — a
  node never shares the MCP server's memory, so it can't reach in and tamper with
  the tools.
- On failure, the runner walks the traceback with Python's `traceback` module and
  emits a single sentinel-prefixed JSON line containing only the exception class
  and project-relative code locations. The server trusts only the **last**
  sentinel line, so a node can't spoof an earlier one.
- `get_metrics` reads the newest version of every dataset under
  `data/claude_visible_metrics/` and keeps only numeric values (booleans
  excluded), so rows can't ride out as string "metrics". Nested objects are
  flattened to dotted keys (`model_a.auc`); **lists are dropped entirely**,
  because a list of floats is a perfectly good carrier for a whole data column.
  Nesting is capped at depth 4 and the merged result at 200 keys — a global cap,
  so adding tracked datasets does not buy more budget. Rejected values are
  dropped silently; the key simply never appears.

For the in-container agent's day-to-day workflow, see
**[`kedro_project/src/CLAUDE.md`](kedro_project/src/CLAUDE.md)**. `blinded_init.py`
generates a project-specific version of that guide at `blinded/CLAUDE.md` and
mounts it at `/workspace/CLAUDE.md`, so it loads at session start.

---

## Tests

```bash
python -m unittest discover tests
```

Stdlib `unittest`, no dependencies — `mcp` is stubbed, so the suite runs without
the data-side image. It covers the metric filter (nested dicts, lists, bools,
depth caps), the mount guard, `pyproject.toml` parsing on both the `tomllib` and
regex paths, and a self-test that wraps this repo's own demo project.

---

## Troubleshooting

- **`docker version` segfaults / Dev Containers fails to start under WSL.**
  WSL's `~/.docker/config.json` may point `currentContext` at a Windows named
  pipe. Fix with `docker context use default`, or add
  `export DOCKER_HOST=unix:///var/run/docker.sock` to your WSL `~/.bashrc`.
- **`claude` says it isn't authenticated.** Re-run `docker compose exec dev
  claude` to log in, or set `CLAUDE_CODE_OAUTH_TOKEN` before `docker compose up`.
  The login persists in the `claude_config` volume across rebuilds.
- **`get_metrics` says no metrics yet.** Run a pipeline first; metrics appear
  under `data/claude_visible_metrics/` only after a successful run. If a run
  succeeded but a key is missing, the value was filtered — it was a string,
  boolean, or list.

---

## Security note

This is a **starter**, not a certified control. It demonstrates a sound
architecture (no data on the agent side, no egress on the data side, curated tool
outputs) and closes the high-bandwidth leaks. Review it against your own threat
model, especially the residual channels noted above, before trusting it with
real regulated data.
