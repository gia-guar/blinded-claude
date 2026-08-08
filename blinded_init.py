#!/usr/bin/env python3
"""Set up the blinded-claude harness on a Kedro project.

Wraps an existing project, or scaffolds a new one from the bundled demo. Writes
a ``blinded/`` folder next to the Kedro project containing docker-compose.yml,
both Dockerfiles, the MCP server, and the in-container CLAUDE.md — with every
path already substituted.

The dangerous part of doing this by hand is the dev-side mount list: one wrong
entry silently hands regulated data to the agent and voids the whole model. So
mounts go through :func:`guard_mount`, which is a hard failure, not a warning.

This script never runs docker, pip, or kedro. It writes files and prints the
commands for you to run. It also never reads anything under ``data/`` or
``conf/local/`` — a setup tool for this must not itself become a leak path.

    python blinded_init.py                       # interactive
    python blinded_init.py --existing PATH
    python blinded_init.py --scratch PATH --name my_project

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import keyword
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # 3.9 / 3.10 — regex fallback, no tomli dependency
    tomllib = None  # type: ignore[assignment]

REPO = Path(__file__).resolve().parent
TEMPLATE_DIR = REPO / "template"
DEMO_PROJECT = REPO / "kedro_project"
DEMO_PACKAGE = "dummy_project"

DEFAULT_HARNESS_DIR = "blinded"
DEFAULT_PYTHON = "3.11"
TRACKING_DIR = "data/claude_visible_metrics"

# The upper bound is load-bearing, not tidiness. mcp 2.0 removed
# mcp.server.fastmcp outright and mcp_server/server.py imports FastMCP from it,
# so an unbounded ">=1.9.0" builds a perfectly good image that dies on startup
# with ModuleNotFoundError. Lift this only together with porting server.py to
# the 2.x server API.
MCP_PIN = '"mcp>=1.9.0,<2"'

# Mirrors mcp_server/server.py, which is the source of truth — these are only
# here so the findings table can state the contract. test_blinded_init.py fails
# if they drift apart.
MAX_METRIC_DEPTH = 4
MAX_METRIC_KEYS = 200

# Never mounted into the dev container, and never read by this script.
FORBIDDEN_MOUNTS = ("data", "conf/local")

# Offered as dev-side mounts when present. An explicit allowlist: never derived
# by subtracting forbidden paths from the project root, because a subtraction
# bug exposes data whereas a missing entry merely inconveniences the agent.
MOUNT_CANDIDATES = ("src", "conf/base", "tests", "notebooks")
READONLY_MOUNTS = ("pyproject.toml",)

# The data-side image is always Linux (template/mcp_server.Dockerfile.tmpl is
# FROM python:X-slim), but pyproject.toml is usually written on the developer's
# machine. These distributions publish Windows wheels only, so copying one into
# requirements.txt fails the build outright:
#   ERROR: Could not find a version that satisfies the requirement triton-windows
# They are commented out rather than dropped — a silently missing dependency is
# far worse than a visible one you have to think about. PEP 508 markers are
# honoured instead of this list where present (see classify_deps).
WINDOWS_ONLY = frozenset(
    {
        "triton-windows",
        "pywin32",
        "pypiwin32",
        "pywin32-ctypes",
        "pywinpty",
        "windows-curses",
        "win32-setctime",
        "winshell",
        "wmi",
        "comtypes",
    }
)

# Extras and PEP 735 groups conventionally holding developer tooling. Nothing
# outside [project.dependencies] reaches the data-side image, but flagging these
# would be noise: almost every project has a dev or docs extra and none of it
# runs a pipeline. Groups with any other name do get reported.
DEV_GROUP_NAMES = frozenset(
    {
        "dev",
        "develop",
        "development",
        "test",
        "tests",
        "testing",
        "doc",
        "docs",
        "lint",
        "typing",
        "build",
        "release",
    }
)

# The data side has no route to the internet, so any outbound call in pipeline
# code fails at run time with nothing useful in the (deliberately data-free)
# error report. Finding these up front is the single most useful check here.
EGRESS_PATTERNS = (
    ("mlflow", r"\bmlflow\b"),
    ("boto3/s3", r"\bboto3\b|\bs3fs\b|s3://"),
    ("gcs", r"\bgcsfs\b|gs://"),
    ("azure", r"\bazure[._-]|abfss?://"),
    ("http client", r"\brequests\b|\bhttpx\b|\burllib3\b|\baiohttp\b"),
    ("database", r"\bsqlalchemy\b|\bpsycopg2?\b|\bpymysql\b|\bsnowflake\b"),
    ("url", r"https?://(?!localhost|127\.0\.0\.1|mcp_server)"),
)
EGRESS_SUFFIXES = (".py", ".yml", ".yaml", ".toml", ".cfg", ".ini")
EGRESS_MAX_HITS = 25

HOOK_SNIPPET = """
# --- blinded-claude ---------------------------------------------------------
# Defense-in-depth lint: refuse to run if any catalog dataset would write
# outside data/. The hard control is the read-only container filesystem
# (see {harness}/docker-compose.yml); this just fails fast with a clear message.
from {package}.blinded_hooks import ConfineWritesToData

HOOKS = (ConfineWritesToData(),)
# --- end blinded-claude -----------------------------------------------------
"""

SETTINGS_NEW = '''"""Project settings. See https://docs.kedro.org for the full list of options."""
{snippet}'''

DEMO_NOTES = """
---

## Demo pipelines (delete when you switch to real data)

This project was scaffolded from the blinded-claude demo:

- `data_science` — split → train → evaluate on synthetic data.
- `failing` — raises on purpose, to exercise the `get_run_error` path. It is
  excluded from `__default__`; trigger it with `run_pipeline(pipeline="failing")`.

`seed_data.py` generates the synthetic CSV. Delete it, the `failing` pipeline,
and the demo catalog entries once real data is in place.
"""


class SetupError(Exception):
    """Anything that should stop the run with a readable message."""


class MountError(SetupError):
    """A proposed dev-side mount could expose data."""


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
def is_sensitive(project_root: Path, path: Path) -> bool:
    """True if ``path`` is at or under a forbidden directory.

    Used to keep this script's own reads away from regulated data.
    """
    root = project_root.resolve()
    target = path.resolve()
    for name in FORBIDDEN_MOUNTS:
        forbidden = (root / name).resolve()
        if target == forbidden or forbidden in target.parents:
            return True
    return False


def guard_mount(project_root: Path, rel: str) -> Path:
    """Resolve a dev-side mount, refusing anything that could expose data.

    Rejects the project root itself, anything outside it, the forbidden paths,
    and — importantly — any *ancestor* of a forbidden path: mounting ``conf``
    would drag ``conf/local`` (credentials) along with it, so only ``conf/base``
    is acceptable.
    """
    cleaned = (rel or "").strip().replace("\\", "/").strip("/")
    root = project_root.resolve()
    if not cleaned or cleaned == ".":
        raise MountError(
            "refusing to mount the project root: it contains data/ and conf/local"
        )
    target = (root / cleaned).resolve()
    if target == root:
        raise MountError(
            "refusing to mount the project root: it contains data/ and conf/local"
        )
    if root not in target.parents:
        raise MountError(f"{rel!r} resolves outside the project ({target})")
    for name in FORBIDDEN_MOUNTS:
        forbidden = (root / name).resolve()
        if target == forbidden:
            raise MountError(f"{rel!r} is {name}/ — never mount it into the agent")
        if forbidden in target.parents:
            raise MountError(f"{rel!r} is inside {name}/ — never mount it")
        if target in forbidden.parents:
            raise MountError(
                f"{rel!r} contains {name}/ — mount a narrower path "
                f"(e.g. conf/base instead of conf)"
            )
    return target


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Project:
    root: Path
    package: str
    project_name: str
    settings_path: Path
    settings_has_hooks: bool
    settings_has_confine: bool
    deps_kind: str  # requirements | pyproject | lock | none
    deps_detail: str
    deps_list: "list[str]"
    deps_dropped: "list[tuple[str, str]]"  # (spec, reason) — commented out, Linux image
    dep_groups: "list[str]"  # extras / PEP 735 groups, which are NOT installed
    tracking_ok: bool
    data_dir_exists: bool
    mounts: "list[str]"
    readonly_mounts: "list[str]"
    egress: "list[tuple[str, str, str]]"  # (relpath:line, tag, snippet)


def toml_string_array(section: str, key: str) -> "list[str]":
    """Read a TOML array of strings, tolerating brackets inside the strings.

    A plain ``\\[(.*?)\\]`` stops at the first ``]`` in the text, which for a
    dependency list means it stops inside the first entry that uses extras —
    ``"kedro[jupyter]>=0.19"`` — and silently discards every entry after it.
    That is a wrong answer that looks like a right one, so scan for the real
    closing bracket instead, ignoring anything inside a quoted string.
    """
    opening = re.search(rf"^\s*{re.escape(key)}\s*=\s*\[", section, re.M)
    if not opening:
        return []
    values: "list[str]" = []
    depth = 0
    index = opening.end() - 1  # index of the '['
    while index < len(section):
        char = section[index]
        if char == "#":
            # A trailing comment can hold anything, including a stray apostrophe
            # that would otherwise open a string. Skip to the end of the line.
            newline = section.find("\n", index)
            if newline == -1:
                break
            index = newline + 1
        elif char in "\"'":
            # Consume the whole string, so brackets inside it (extras) are inert.
            end = section.find(char, index + 1)
            if end == -1:
                break  # unterminated string — malformed toml
            values.append(section[index + 1 : end])
            index = end + 1
        else:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return values
            index += 1
    return []  # unterminated array — malformed toml, treat as empty


def parse_pyproject(path: Path) -> dict:
    """Pull package_name, project name and dependencies out of pyproject.toml.

    ``dep_groups`` names any *other* place dependencies are declared (extras,
    PEP 735 groups). Those are not installed into the data-side image, and a
    project that keeps a real runtime dependency there gets an ImportError at
    run time with no internet to fix it, so the caller warns about them.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception as exc:  # malformed toml — say so plainly
            raise SetupError(f"could not parse {path}: {type(exc).__name__}") from exc
        kedro = data.get("tool", {}).get("kedro", {})
        project = data.get("project", {})
        groups = [f"[project.optional-dependencies] {name}"
                  for name in (project.get("optional-dependencies") or {})
                  if name.lower() not in DEV_GROUP_NAMES]
        groups += [f"[dependency-groups] {name}"
                   for name in (data.get("dependency-groups") or {})
                   if name.lower() not in DEV_GROUP_NAMES]
        return {
            "package_name": kedro.get("package_name"),
            "project_name": kedro.get("project_name") or project.get("name"),
            "dependencies": list(project.get("dependencies") or []),
            "dep_groups": groups,
        }

    def _section(name: str) -> str:
        match = re.search(
            rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S
        )
        return match.group(1) if match else ""

    def _key(section: str, key: str):
        match = re.search(rf'^\s*{key}\s*=\s*["\']([^"\']+)["\']', section, re.M)
        return match.group(1) if match else None

    def _group_names(header: str) -> "list[str]":
        body = _section(header)
        return [f"[{header}] {name}"
                for name in re.findall(r"^\s*([A-Za-z0-9._-]+)\s*=\s*\[", body, re.M)
                if name.lower() not in DEV_GROUP_NAMES]

    kedro_section = _section("tool.kedro")
    project_section = _section("project")
    return {
        "package_name": _key(kedro_section, "package_name"),
        "project_name": _key(kedro_section, "project_name") or _key(project_section, "name"),
        "dependencies": toml_string_array(project_section, "dependencies"),
        "dep_groups": (
            _group_names("project.optional-dependencies")
            + _group_names("dependency-groups")
        ),
    }


def requirement_name(spec: str) -> str:
    """PEP 503-normalised distribution name from a PEP 508 requirement string."""
    head = re.split(r"[\[<>=!~;\s@(]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", head).lower()


def classify_deps(deps: "list[str]") -> "tuple[list[str], list[tuple[str, str]]]":
    """Split dependencies into installable and not-on-Linux.

    Returns ``(keep, dropped)`` where dropped is ``[(spec, reason), ...]``.
    An entry carrying its own platform marker is kept verbatim: pip evaluates
    the marker at install time and skips it on Linux by itself, which is a
    better answer than second-guessing what the author wrote.
    """
    keep: "list[str]" = []
    dropped: "list[tuple[str, str]]" = []
    for spec in deps:
        if ";" in spec and re.search(r"sys_platform|platform_system", spec):
            keep.append(spec)  # pip resolves the marker; nothing to decide here
        elif requirement_name(spec) in WINDOWS_ONLY:
            dropped.append((spec, "Windows-only distribution"))
        else:
            keep.append(spec)
    return keep, dropped


def scan_egress(root: Path) -> "list[tuple[str, str, str]]":
    """Look for outbound calls in pipeline code and base config.

    Only ``src/`` and ``conf/base/`` are walked — never data/ or conf/local.
    """
    hits: "list[tuple[str, str, str]]" = []
    for sub in ("src", "conf/base"):
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if len(hits) >= EGRESS_MAX_HITS:
                return hits
            if not path.is_file() or path.suffix.lower() not in EGRESS_SUFFIXES:
                continue
            if is_sensitive(root, path) or "__pycache__" in path.parts:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                for tag, pattern in EGRESS_PATTERNS:
                    if re.search(pattern, line):
                        rel = path.relative_to(root).as_posix()
                        hits.append((f"{rel}:{number}", tag, line.strip()[:80]))
                        break
                if len(hits) >= EGRESS_MAX_HITS:
                    return hits
    return hits


def detect(root: Path, extra_mounts: "list[str]", tracking_dir: str) -> Project:
    root = root.resolve()
    if not root.is_dir():
        raise SetupError(f"not a directory: {root}")

    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise SetupError(
            f"no pyproject.toml in {root} — point --existing at the Kedro "
            f"project root (the directory holding conf/, src/ and data/)"
        )
    meta = parse_pyproject(pyproject)
    package = meta["package_name"]
    if not package:
        raise SetupError(
            f"{pyproject} has no [tool.kedro] package_name — this does not look "
            f"like a Kedro project"
        )
    if not (root / "src" / package).is_dir():
        raise SetupError(f"package directory not found: src/{package}")

    settings_path = root / "src" / package / "settings.py"
    settings_has_hooks = settings_has_confine = False
    if settings_path.is_file():
        settings_text = settings_path.read_text(encoding="utf-8", errors="replace")
        settings_has_hooks = re.search(r"^\s*HOOKS\s*=", settings_text, re.M) is not None
        # Already wired (the scaffold ships it, and re-runs must be idempotent).
        settings_has_confine = "ConfineWritesToData" in settings_text

    catalog = root / "conf" / "base" / "catalog.yml"
    tracking_ok = (
        catalog.is_file()
        and tracking_dir in catalog.read_text(encoding="utf-8", errors="replace")
    )

    deps_list: "list[str]" = []
    if (root / "requirements.txt").is_file():
        deps_kind, deps_detail = "requirements", "requirements.txt"
        # Read only so it can be classified. The generated Dockerfile copies this
        # file into the Linux image verbatim, so a Windows-only pin here breaks
        # the build exactly as one in pyproject.toml would. It belongs to the
        # project, so this path warns rather than rewrites.
        deps_list = [
            line.strip()
            for line in (root / "requirements.txt")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))
        ]
    elif meta["dependencies"]:
        deps_kind, deps_detail = "pyproject", "pyproject.toml [project.dependencies]"
        deps_list = meta["dependencies"]
    else:
        locks = [
            name
            for name in ("uv.lock", "poetry.lock", "Pipfile.lock", "environment.yml")
            if (root / name).is_file()
        ]
        deps_kind = "lock" if locks else "none"
        deps_detail = ", ".join(locks) if locks else "none found"

    mounts: "list[str]" = []
    for candidate in list(MOUNT_CANDIDATES) + list(extra_mounts):
        candidate = candidate.strip("/\\").replace("\\", "/")
        if candidate in mounts:
            continue
        if not (root / candidate).exists():
            if candidate in extra_mounts:
                raise SetupError(f"--mount {candidate!r} does not exist under {root}")
            continue  # optional candidate, simply absent
        guard_mount(root, candidate)  # raises on anything unsafe
        mounts.append(candidate)
    if "src" not in mounts:
        raise SetupError("src/ not found — nothing for the agent to edit")

    readonly = [name for name in READONLY_MOUNTS if (root / name).is_file()]
    for name in readonly:
        guard_mount(root, name)

    return Project(
        root=root,
        package=package,
        project_name=meta["project_name"] or package,
        settings_path=settings_path,
        settings_has_hooks=settings_has_hooks,
        settings_has_confine=settings_has_confine,
        deps_kind=deps_kind,
        deps_detail=deps_detail,
        deps_list=deps_list,
        deps_dropped=classify_deps(deps_list)[1],
        dep_groups=meta["dep_groups"],
        tracking_ok=tracking_ok,
        data_dir_exists=(root / "data").is_dir(),
        mounts=mounts,
        readonly_mounts=readonly,
        egress=scan_egress(root),
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(name: str, tokens: dict) -> str:
    """Fill a {{TOKEN}} template.

    Plain str.replace rather than string.Template: docker-compose.yml contains
    ``${CLAUDE_CODE_OAUTH_TOKEN:-}``, which $-based substitution would mangle.
    """
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{([A-Z_]+)\}\}", text)
    if leftover:
        raise SetupError(f"template {name} has unfilled tokens: {sorted(set(leftover))}")
    return text


def mount_lines(project: Project) -> str:
    lines = [f"      - ../{name}:/workspace/{name}" for name in project.mounts]
    lines += [f"      - ../{name}:/workspace/{name}:ro" for name in project.readonly_mounts]
    return "\n".join(lines)


def workspace_tree(project: Project) -> str:
    entries = list(project.mounts) + list(project.readonly_mounts) + [".mcp.json"]
    notes = {
        "src": "pipeline code (read-write)",
        "conf/base": "catalog.yml + parameters.yml",
        "pyproject.toml": "read-only",
        ".mcp.json": "points at http://mcp_server:8000/sse",
    }
    lines = []
    for index, entry in enumerate(entries):
        branch = "└──" if index == len(entries) - 1 else "├──"
        label = entry + ("/" if "." not in Path(entry).name else "")
        note = notes.get(entry)
        lines.append(f"{branch} {label:<22}{'# ' + note if note else ''}".rstrip())
    return "\n".join(lines)


def deps_install(project: Project, harness: str) -> str:
    if project.deps_kind == "requirements":
        return (
            "# Project dependencies, from the project's own requirements.txt.\n"
            "COPY requirements.txt /tmp/requirements.txt\n"
            f"RUN pip install --no-cache-dir -r /tmp/requirements.txt {MCP_PIN}"
        )
    if project.deps_kind == "pyproject":
        return (
            "# Project dependencies, extracted from pyproject.toml by blinded_init.py.\n"
            "# Read that file before trusting it: entries that cannot install on\n"
            "# Linux are commented out there. Re-extract by hand if you change\n"
            "# [project.dependencies].\n"
            f"COPY {harness}/requirements.txt /tmp/requirements.txt\n"
            f"RUN pip install --no-cache-dir -r /tmp/requirements.txt {MCP_PIN}"
        )
    return (
        "# TODO: no requirements.txt and no [project.dependencies] were found\n"
        f"#       ({project.deps_detail}). blinded_init.py will not guess an\n"
        "#       export command. Produce a requirements.txt, e.g.\n"
        "#         uv export --no-hashes --no-dev > "
        f"{harness}/requirements.txt\n"
        "#         poetry export --without-hashes -f requirements.txt "
        f"-o {harness}/requirements.txt\n"
        "#       then uncomment these two lines:\n"
        f"# COPY {harness}/requirements.txt /tmp/requirements.txt\n"
        "# RUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
        f"RUN pip install --no-cache-dir {MCP_PIN}"
    )


def requirements_body(project: Project, python_version: str) -> str:
    """The generated requirements.txt.

    Deliberately not a verbatim copy of [project.dependencies]: see WINDOWS_ONLY.
    The excluded entries are written back as comments so the file still records
    everything the project asked for.
    """
    keep, dropped = classify_deps(project.deps_list)
    header = ["# Extracted from pyproject.toml [project.dependencies] by blinded_init.py."]
    if dropped:
        header += [
            "#",
            "# NOT a verbatim copy. The commented entries at the bottom cannot install",
            f"# on the data-side image (python:{python_version}-slim, Linux). Nothing",
            "# else was changed. Uncomment one if this call was wrong for your setup.",
        ]
    lines = header + keep
    for spec, reason in dropped:
        lines += ["", f"# {reason} — fails the build on Linux.", f"# {spec}"]
    return "\n".join(lines) + "\n"


# GPU passthrough for the data side, emitted by --gpu. mcp_server is the
# container that executes pipelines, so the reservation belongs there and not on
# dev. It widens nothing: mcp_server stays on the internal-only mcp_bridge with a
# read-only filesystem. CUDA's on-disk caches follow HOME, which the service
# already points at the writable /tmp tmpfs.
GPU_RESERVATION = """    # Added by blinded_init.py --gpu. Needs nvidia-container-toolkit on the
    # host; without it `docker compose up` fails with "could not select device
    # driver". Drop this block to run on CPU.
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]"""


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Action:
    kind: str  # create | append | skip
    path: Path
    content: str = ""
    note: str = ""
    confirm: bool = False  # touches a pre-existing file -> its own confirmation

    def rel_to(self, root: Path) -> str:
        try:
            return self.path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return str(self.path)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Force LF: these files are consumed inside Linux containers.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def compose_file_list(harness: str, mlflow: bool) -> str:
    """The devcontainer's dockerComposeFile array, as rendered JSON."""
    files = [f"../{harness}/docker-compose.yml"]
    if mlflow:
        files.append(f"../{harness}/docker-compose.mlflow.yml")
    if len(files) == 1:
        return f'"{files[0]}"'
    inner = ",\n    ".join(f'"{f}"' for f in files)
    return f"\n    {inner}\n  "


def plan(
    project: Project,
    harness: str,
    python_version: str,
    demo: bool,
    mlflow: bool = False,
    gpu: bool = False,
) -> "list[Action]":
    root = project.root
    out = root / harness
    tokens = {
        "HARNESS_DIR": harness,
        "PROJECT_NAME": re.sub(r"[^A-Za-z0-9_.-]", "_", project.project_name),
        "PACKAGE": project.package,
        "PYTHON_VERSION": python_version,
        "DEV_MOUNTS": mount_lines(project),
        "DEPS_INSTALL": deps_install(project, harness),
        "WORKSPACE_TREE": workspace_tree(project),
        "DEMO_NOTES": DEMO_NOTES if demo else "",
        "GPU_RESERVATION": GPU_RESERVATION if gpu else "",
        # Derived from the same flag that writes the overlay, so the two cannot
        # drift: a generated overlay the devcontainer never loads is worse than
        # no overlay, because the stack looks configured and is not.
        "COMPOSE_FILES": compose_file_list(harness, mlflow),
    }

    actions = [
        Action("create", out / "docker-compose.yml", render("docker-compose.yml.tmpl", tokens)),
        Action("create", out / "mcp_server" / "Dockerfile", render("mcp_server.Dockerfile.tmpl", tokens)),
        Action("create", out / "CLAUDE.md", render("CLAUDE.md.tmpl", tokens)),
    ]
    if mlflow:
        actions.append(
            Action(
                "create",
                out / "docker-compose.mlflow.yml",
                render("docker-compose.mlflow.yml.tmpl", tokens),
            )
        )

    # Verbatim copies — single-sourced from this repo, never duplicated in template/.
    for src, dest in (
        (REPO / "mcp_server" / "server.py", out / "mcp_server" / "server.py"),
        (REPO / "mcp_server" / "pipeline_runner.py", out / "mcp_server" / "pipeline_runner.py"),
        (REPO / "dev_container" / "Dockerfile", out / "dev_container" / "Dockerfile"),
        (REPO / "dev_container" / "claude_mcp_config.json", out / "dev_container" / "claude_mcp_config.json"),
    ):
        actions.append(Action("create", dest, src.read_text(encoding="utf-8")))

    if project.deps_kind == "pyproject":
        actions.append(
            Action(
                "create",
                out / "requirements.txt",
                requirements_body(project, python_version),
            )
        )

    # Hook module: a distinct filename so we never clobber an existing hooks.py.
    hooks_dest = root / "src" / project.package / "blinded_hooks.py"
    hooks_src = (DEMO_PROJECT / "src" / DEMO_PACKAGE / "hooks.py").read_text(encoding="utf-8")
    snippet = HOOK_SNIPPET.format(package=project.package, harness=harness)

    if project.settings_has_confine:
        # The scaffold ships this wired up, and re-runs must not duplicate it.
        actions.append(Action("skip", project.settings_path, note="ConfineWritesToData already wired"))
    else:
        if not hooks_dest.exists():
            actions.append(Action("create", hooks_dest, hooks_src))
        elif hooks_dest.read_text(encoding="utf-8") == hooks_src:
            actions.append(Action("skip", hooks_dest, note="already current"))
        else:
            # Refresh it. Skipping on existence means a project wrapped before a
            # hook fix never receives that fix, not even on --force, because
            # --force only regenerates the harness folder. This file is ours (a
            # distinct filename precisely so we can own it), so overwriting is
            # safe — and a silently stale copy is the worse failure.
            actions.append(
                Action("create", hooks_dest, hooks_src, confirm=True,
                       note="differs from the shipped version — refresh")
            )

        if not project.settings_path.exists():
            actions.append(Action("create", project.settings_path, SETTINGS_NEW.format(snippet=snippet)))
        elif project.settings_has_hooks:
            actions.append(
                Action(
                    "skip",
                    project.settings_path,
                    note="HOOKS already defined — add ConfineWritesToData() to it by hand",
                )
            )
        else:
            current = project.settings_path.read_text(encoding="utf-8")
            actions.append(
                Action("append", project.settings_path, current.rstrip("\n") + "\n" + snippet, confirm=True)
            )

    dockerignore = root / ".dockerignore"
    snippet_text = (TEMPLATE_DIR / "dockerignore.snippet").read_text(encoding="utf-8")
    if not dockerignore.exists():
        actions.append(Action("create", dockerignore, snippet_text.lstrip("\n")))
    elif "blinded-claude" in dockerignore.read_text(encoding="utf-8"):
        actions.append(Action("skip", dockerignore, note="already has the blinded-claude block"))
    else:
        current = dockerignore.read_text(encoding="utf-8")
        actions.append(
            Action("append", dockerignore, current.rstrip("\n") + "\n" + snippet_text, confirm=True)
        )

    devcontainer = root / ".devcontainer" / "devcontainer.json"
    if devcontainer.exists():
        # Left alone because it is commonly hand-customised. But say so loudly
        # when it cannot work: a devcontainer that does not load the overlay
        # produces a stack that looks fine and fails every run in setup.
        note = "already exists — left alone"
        if mlflow and "docker-compose.mlflow.yml" not in devcontainer.read_text(
            encoding="utf-8"
        ):
            note = (
                "already exists — MISSING docker-compose.mlflow.yml in "
                "dockerComposeFile; add it by hand or MLflow runs fail in setup"
            )
        actions.append(Action("skip", devcontainer, note=note))
    else:
        actions.append(Action("create", devcontainer, render("devcontainer.json.tmpl", tokens)))

    return actions


# --------------------------------------------------------------------------- #
# Scratch scaffold
# --------------------------------------------------------------------------- #
SCRATCH_SKIP_DIRS = {".venv", "venv", "__pycache__", ".kedro"}
SCRATCH_SKIP_SUFFIXES = {".pyc", ".pyo", ".csv"}
SCRATCH_RENAME_SUFFIXES = {".py", ".toml", ".yml", ".yaml", ".md", ".json"}


def scaffold(dest: Path, package: str) -> None:
    """Copy the bundled demo project to ``dest`` with the package renamed."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", package) or keyword.iskeyword(package):
        raise SetupError(
            f"{package!r} is not a valid Python package name "
            f"(lowercase letters, digits and underscores; no leading digit; "
            f"not a Python keyword)"
        )
    if dest.exists() and any(dest.iterdir()):
        raise SetupError(f"{dest} exists and is not empty")

    for dirpath, dirnames, filenames in os.walk(DEMO_PROJECT):
        current = Path(dirpath)
        rel_dir = current.relative_to(DEMO_PROJECT)
        # Prune in place, so a .venv or build tree is never descended at all.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SCRATCH_SKIP_DIRS and not name.endswith(".egg-info")
        )
        for name in sorted(filenames):
            src = current / name
            rel = rel_dir / name
            if src.suffix in SCRATCH_SKIP_SUFFIXES:
                continue
            if rel.as_posix().lower() == "src/claude.md":
                continue  # superseded by the generated harness CLAUDE.md
            if rel.parts[0] == "data" and name != ".gitkeep":
                continue  # never carry demo data across

            target = dest / Path(*[part.replace(DEMO_PACKAGE, package) for part in rel.parts])
            if src.suffix in SCRATCH_RENAME_SUFFIXES:
                write_text(target, src.read_text(encoding="utf-8").replace(DEMO_PACKAGE, package))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)

    for sub in ("data/01_raw", TRACKING_DIR, "conf/local"):
        (dest / sub).mkdir(parents=True, exist_ok=True)
        keep = dest / sub / ".gitkeep"
        if not keep.exists():
            write_text(keep, "")


# --------------------------------------------------------------------------- #
# Reporting / CLI
# --------------------------------------------------------------------------- #
def prompt(question: str, default: str = "") -> str:
    try:
        answer = input(question).strip()
    except EOFError:
        raise SetupError("no input available — pass --existing/--scratch and --yes")
    return answer or default


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return prompt(f"{question} [y/N]: ").lower() in {"y", "yes"}


def report(project: Project, harness: str, tracking_dir: str) -> "list[str]":
    """Print the findings table; return the warnings it raised."""
    warnings: "list[str]" = []

    def row(status: str, label: str, text: str) -> None:
        print(f"  {status:<6}{label:<20}{text}")

    print("\nFindings")
    print("-" * 60)
    row("ok", "Kedro package", project.package)
    row("ok", "dev-side mounts", ", ".join(project.mounts + project.readonly_mounts))

    if project.deps_kind in {"lock", "none"}:
        warnings.append(
            f"dependencies: {project.deps_detail}. Fill the TODO in the generated "
            f"Dockerfile. The data side has no run-time internet, so anything not "
            f"baked into the image will not import."
        )
        row("WARN", "dependencies", project.deps_detail)
    else:
        row("ok", "dependencies", project.deps_detail)

    if project.deps_dropped:
        names = ", ".join(requirement_name(spec) for spec, _ in project.deps_dropped)
        if project.deps_kind == "pyproject":
            warnings.append(
                f"{len(project.deps_dropped)} dependency(ies) cannot install on the "
                f"Linux data-side image and are commented out in the generated "
                f"{harness}/requirements.txt: {names}. Confirm nothing under src/ "
                f"imports them, and add the Linux equivalent where something does."
            )
        else:
            warnings.append(
                f"requirements.txt pins {len(project.deps_dropped)} package(s) that "
                f"cannot install on the Linux data-side image: {names}. The generated "
                f"Dockerfile copies that file verbatim, so the build fails until you "
                f"gate them with a marker (; sys_platform == 'win32') or remove them. "
                f"This script does not edit your requirements.txt."
            )
        row("WARN", "platform deps", names)

    if project.dep_groups:
        joined = ", ".join(project.dep_groups)
        installed = ("requirements.txt" if project.deps_kind == "requirements"
                     else "[project.dependencies]")
        warnings.append(
            f"dependencies are also declared in {joined}. Only {installed} is "
            f"installed into the data-side image, so anything the pipelines import "
            f"from those groups raises ImportError at run time — and that side has "
            f"no internet to install it then. Move whatever the pipelines need "
            f"into {installed}."
        )
        row("WARN", "dep groups", joined)

    if project.tracking_ok:
        row("ok", "metrics contract", f"catalog writes to {tracking_dir}")
    else:
        warnings.append(
            f"nothing writes to {tracking_dir}, so get_metrics returns nothing. "
            f"Add a json.JSONDataset (versioned: true) there; the agent can also "
            f"add one, since src and conf/base are writable from the dev side. "
            f"That directory is the only content leaving the secure side, and it "
            f"is filtered: numeric leaves only, nesting to depth "
            f"{MAX_METRIC_DEPTH}, {MAX_METRIC_KEYS} keys per run counted across "
            f"all datasets. Strings, booleans and lists are dropped silently — "
            f"the run still succeeds and the key is simply absent. If you write "
            f"metrics elsewhere, repoint TRACKING_DIR in "
            f"{harness}/mcp_server/server.py."
        )
        row("WARN", "metrics contract", f"nothing writes to {tracking_dir}")

    if project.data_dir_exists:
        row("ok", "data/", "present (mounted read-write, data side only)")
    else:
        warnings.append(
            "data/ does not exist. Docker will create it root-owned on first "
            "`up`. Create it first."
        )
        row("WARN", "data/", "missing")

    if project.settings_has_confine:
        row("ok", "safety hook", "ConfineWritesToData already wired")
    elif project.settings_has_hooks:
        warnings.append(
            f"settings.py already defines HOOKS, so ConfineWritesToData was NOT "
            f"wired in — the catalog lint is inactive until you add it by hand:\n"
            f"    from {project.package}.blinded_hooks import ConfineWritesToData\n"
            f"    HOOKS = (YourExistingHook(), ConfineWritesToData())\n"
            f"  The hook file was still written, so this looks done from the "
            f"filesystem and is not. The read-only container filesystem still "
            f"holds the real boundary either way."
        )
        row("WARN", "settings.py", "HOOKS already defined — lint NOT wired")

    if project.egress:
        warnings.append(
            f"{len(project.egress)} possible outbound call(s) in pipeline code. "
            f"The data side has no internet, so each one moves inside the "
            f"perimeter or comes out. They fail at run time with a deliberately "
            f"uninformative error, so triage them now. Docstring and comment "
            f"matches are common — read the lines, not the count."
        )
        row("WARN", "egress", f"{len(project.egress)} possible outbound call(s):")
        for location, tag, line in project.egress:
            print(f"          {location}  [{tag}]  {line}")
    else:
        row("ok", "egress", "no obvious outbound calls in src/ or conf/base/")
    return warnings


def hardcoded_tracking_uri(project: Project):
    """conf/**/mlflow.yml files pinning server.mlflow_tracking_uri to a fixed host.

    kedro-mlflow reads this key and it WINS over the MLFLOW_TRACKING_URI env var
    the overlay sets, so a project carrying the kedro-mlflow default
    (http://localhost:5000) ignores the overlay entirely. Inside the data-side
    container `localhost` is that container's own loopback, so the experiment
    lookup at context creation retries for minutes and then fails every run
    before a single node executes.

    conf/local is skipped, not overlooked: this script promises never to read it
    (see the module docstring), and that promise outranks a better diagnostic.
    The caller pairs this with an advisory covering the files we will not open.
    """
    hits = []
    conf = project.root / "conf"
    if not conf.is_dir():
        return hits
    for path in sorted(conf.rglob("mlflow.yml")):
        if is_sensitive(project.root, path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("mlflow_tracking_uri:"):
                continue
            value = stripped.split(":", 1)[1].split("#", 1)[0].strip()
            # An oc.env interpolation is the fix, not the problem.
            if value and "oc.env" not in value:
                hits.append((path.relative_to(project.root).as_posix(), value))
    return hits


def uses_mlflow(project: Project) -> bool:
    return any(tag == "mlflow" for _, tag, _ in project.egress)


def ask_mlflow(project: Project, requested: bool, assume_yes: bool) -> bool:
    """Decide whether to write the MLflow overlay.

    Asked after detection so the recommendation can reflect what is actually in
    the project. ``--mlflow`` answers it up front; ``--yes`` declines, because
    adding a service nobody asked for is the wrong default for a batch run.
    """
    if requested:
        return True
    if assume_yes:
        return False

    detected = uses_mlflow(project)
    print("\nExperiment tracking")
    print("-" * 60)
    print("  MLflow found in pipeline code." if detected
          else "  No MLflow found in pipeline code.")
    print("  The overlay runs MLflow inside the perimeter, on a network the dev")
    print("  container has no route to. Existing logging calls keep working. It")
    print("  stays unreachable to the agent because log_artifact and log_figure")
    print("  store whole files. The UI is not published by default — raise it")
    print("  with `--profile ui` while the agent is stopped.")
    print()
    print(f"  [1] add the overlay{'   (recommended)' if detected else ''}")
    print(f"  [2] skip it{'' if detected else '          (recommended)'}")
    return prompt("> ", "1" if detected else "2") == "1"


def show_plan(actions: "list[Action]", root: Path) -> None:
    print("\nPlan")
    print("-" * 60)
    for action in actions:
        suffix = f"   ({action.note})" if action.note else ""
        if action.confirm and not action.note:
            suffix = "   (existing file — confirmed separately)"
        print(f"  {action.kind:<7}{action.rel_to(root)}{suffix}")


def show_diff(action: Action, root: Path) -> None:
    before = action.path.read_text(encoding="utf-8").splitlines(keepends=True)
    after = action.content.splitlines(keepends=True)
    rel = action.rel_to(root)
    diff = difflib.unified_diff(before, after, fromfile=rel, tofile=f"{rel} (proposed)")
    print()
    sys.stdout.writelines(diff)
    print()


def ladder(root: Path, harness: str, package: str, demo: bool, mlflow: bool,
           gpu: bool = False) -> None:
    path = root / harness
    compose = "docker compose"
    if mlflow:
        compose += " -f docker-compose.yml -f docker-compose.mlflow.yml"
    print("\nNext — run these yourself (this script does not run docker):")
    print("-" * 60)
    print(f"  cd {path}")
    print(f"  {compose} build mcp_server")
    if gpu:
        # nvidia-container-toolkit injects nvidia-smi into the container, so this
        # checks the passthrough without assuming torch is installed.
        print(f"  {compose} run --rm mcp_server nvidia-smi -L   # must list a GPU")
    if demo:
        print(f"  {compose} run --rm mcp_server python /app/project/seed_data.py")
    print(f'  {compose} run --rm mcp_server python -c "import {package}; print(\'ok\')"')
    print(f"  {compose} run --rm mcp_server python /app/mcp_server/pipeline_runner.py __default__")
    print(f"  {compose} up -d")
    print(f"  {compose} exec dev claude")
    print("\nThen confirm the boundary actually holds before trusting it with real data:")
    print(f"  {compose} exec dev ls /workspace          # no data/, no conf/local")
    print(f"  {compose} exec mcp_server python -c "
          "\"import socket; socket.create_connection(('1.1.1.1',443),3)\"   # must fail")
    if mlflow:
        print("\nAnd confirm the agent cannot reach the tracking server. Probe every")
        print("route, not just the service name — a published port bypasses the")
        print("network isolation, and a stale hosts entry can make the name-only")
        print("check 'pass' while the API is wide open:")
        print(f"  {compose} exec dev node -e "
              "\"['http://mlflow:5000','http://host.docker.internal:5000',"
              "'http://192.168.65.254:5000'].forEach(u=>fetch(u,{signal:AbortSignal.timeout(5000)})"
              ".then(r=>console.log('REACHABLE',u,r.status))"
              ".catch(e=>console.log('blocked',u,e.cause?.code||e.name)))\"")
        print("  # every line must say 'blocked'. Any REACHABLE is a leak, whatever")
        print("  # the error code on the others.")
        print("\nThe UI is off by default. To look at it, stop the agent first:")
        print(f"  {compose} stop dev")
        print(f"  {compose} --profile ui up -d mlflow_ui   # http://127.0.0.1:5000")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Set up the blinded-claude harness on a Kedro project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--existing", metavar="PATH", help="wrap an existing Kedro project")
    mode.add_argument("--scratch", metavar="PATH", help="scaffold a new project, then wrap it")
    parser.add_argument("--name", help="package name for --scratch (e.g. my_project)")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help=f"data-side Python (default {DEFAULT_PYTHON})")
    parser.add_argument("--harness-dir", default=DEFAULT_HARNESS_DIR, help=f"harness folder name (default {DEFAULT_HARNESS_DIR})")
    parser.add_argument("--mount", action="append", default=[], metavar="DIR",
                        help="extra dev-side mount, relative to the project root (repeatable)")
    parser.add_argument("--tracking-dir", default=TRACKING_DIR,
                        help="where your catalog writes metrics; used for the check only "
                             f"(the server reads {TRACKING_DIR})")
    parser.add_argument("--mlflow", action="store_true",
                        help="write a compose overlay running MLflow inside the perimeter, "
                             "on a network the agent cannot reach. Asked interactively "
                             "otherwise; declined under --yes")
    parser.add_argument("--gpu", action="store_true",
                        help="reserve all host GPUs for the data-side container, which "
                             "is where pipelines actually run. Needs "
                             "nvidia-container-toolkit on the host")
    parser.add_argument("--yes", action="store_true", help="skip all confirmations")
    parser.add_argument("--force", action="store_true", help="overwrite an existing harness folder")
    parser.add_argument("--dry-run", action="store_true", help="report and plan, write nothing")
    return parser


def run(argv: "list[str]") -> int:
    args = build_parser().parse_args(argv)
    harness = args.harness_dir.strip("/\\")
    demo = False

    print("blinded-claude setup")
    print("-" * 60)

    if args.existing:
        root = Path(args.existing).expanduser()
    elif args.scratch:
        root = Path(args.scratch).expanduser()
        demo = True
    else:
        print("Are you starting from scratch?")
        print("  [1] Yes — scaffold a new Kedro project with the harness wired in")
        print("  [2] No  — wrap an existing Kedro project")
        choice = prompt("> ")
        if choice not in {"1", "2"}:
            raise SetupError("choose 1 or 2")
        demo = choice == "1"
        root = Path(prompt("Path for the new project: " if demo else "Path to your Kedro project: ")).expanduser()

    if demo:
        package = args.name or prompt("Package name [my_project]: ", "my_project")
        scaffold(root, package)
        print(f"\nScaffolded {package} at {root.resolve()}")

    project = detect(root, args.mount, args.tracking_dir)
    warnings = report(project, harness, args.tracking_dir)

    out = project.root / harness
    if out.exists() and not args.force:
        raise SetupError(f"{out} already exists — pass --force to overwrite")

    use_mlflow = ask_mlflow(project, args.mlflow, args.yes)
    if use_mlflow:
        fix = (
            "      mlflow_tracking_uri: "
            "${oc.env:MLFLOW_TRACKING_URI,'http://localhost:5000'}\n"
            "    and enable the resolver in settings.py, which Kedro disables by "
            "default:\n"
            "      from omegaconf.resolvers import oc\n"
            "      CONFIG_LOADER_ARGS = {..., \"custom_resolvers\": "
            "{\"oc.env\": oc.env}}"
        )
        for rel, value in hardcoded_tracking_uri(project):
            warnings.append(
                f"{rel} pins server.mlflow_tracking_uri to {value}. Make it "
                f"environment-driven or every run dies in setup — see the note "
                f"below for why.\n" + fix
            )
        warnings.append(
            "kedro-mlflow resolves the experiment at context creation, before "
            "any node runs, and its own server.mlflow_tracking_uri beats the "
            "MLFLOW_TRACKING_URI this overlay sets. So a URI pointing at "
            "localhost (kedro-mlflow's default) is not overridden: inside the "
            "data-side container that is the container's own loopback, and every "
            "run fails in setup after a multi-minute retry, with no node "
            "executed. That key usually lives in conf/local/mlflow.yml, which "
            "this script does not read by design — check it yourself:\n" + fix
        )

    if uses_mlflow(project) and not use_mlflow:
        warnings.append(
            "MLflow is used here but the overlay was declined, so those calls "
            "will fail. It cannot simply be allowed out either: log_artifact and "
            "log_figure upload whole files and images. Add the overlay or remove "
            "the calls."
        )

    actions = plan(project, harness, args.python, demo, use_mlflow, args.gpu)
    show_plan(actions, project.root)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    batch = [a for a in actions if a.kind == "create"]
    if batch and not confirm(f"\nWrite {len(batch)} new file(s)?", args.yes):
        print("Aborted.")
        return 1
    for action in batch:
        write_text(action.path, action.content)

    for action in [a for a in actions if a.kind == "append"]:
        show_diff(action, project.root)
        if confirm(f"Apply this change to {action.rel_to(project.root)}?", args.yes):
            write_text(action.path, action.content)
        else:
            print(f"  skipped {action.rel_to(project.root)} — apply it by hand")

    print(f"\nWrote the harness to {out}")
    if warnings:
        print("\nUnresolved warnings")
        print("-" * 60)
        for warning in warnings:
            print(textwrap.fill(warning, width=76,
                                initial_indent="  ! ", subsequent_indent="    "))
    ladder(project.root, harness, project.package, demo, use_mlflow, args.gpu)
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except SetupError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
