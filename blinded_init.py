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
    tracking_ok: bool
    data_dir_exists: bool
    mounts: "list[str]"
    readonly_mounts: "list[str]"
    egress: "list[tuple[str, str, str]]"  # (relpath:line, tag, snippet)


def parse_pyproject(path: Path) -> dict:
    """Pull package_name, project name and dependencies out of pyproject.toml."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception as exc:  # malformed toml — say so plainly
            raise SetupError(f"could not parse {path}: {type(exc).__name__}") from exc
        kedro = data.get("tool", {}).get("kedro", {})
        project = data.get("project", {})
        return {
            "package_name": kedro.get("package_name"),
            "project_name": kedro.get("project_name") or project.get("name"),
            "dependencies": list(project.get("dependencies") or []),
        }

    def _section(name: str) -> str:
        match = re.search(
            rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S
        )
        return match.group(1) if match else ""

    def _key(section: str, key: str):
        match = re.search(rf'^\s*{key}\s*=\s*["\']([^"\']+)["\']', section, re.M)
        return match.group(1) if match else None

    kedro_section = _section("tool.kedro")
    project_section = _section("project")
    deps_match = re.search(r"^\s*dependencies\s*=\s*\[(.*?)\]", project_section, re.M | re.S)
    deps = re.findall(r'["\']([^"\']+)["\']', deps_match.group(1)) if deps_match else []
    return {
        "package_name": _key(kedro_section, "package_name"),
        "project_name": _key(kedro_section, "project_name") or _key(project_section, "name"),
        "dependencies": deps,
    }


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

    if (root / "requirements.txt").is_file():
        deps_kind, deps_detail = "requirements", "requirements.txt"
    elif meta["dependencies"]:
        deps_kind, deps_detail = "pyproject", "pyproject.toml [project.dependencies]"
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
        deps_list=meta["dependencies"],
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
            'RUN pip install --no-cache-dir -r /tmp/requirements.txt "mcp>=1.9.0"'
        )
    if project.deps_kind == "pyproject":
        return (
            "# Project dependencies, extracted from pyproject.toml by blinded_init.py.\n"
            "# Re-extract by hand if you change [project.dependencies].\n"
            f"COPY {harness}/requirements.txt /tmp/requirements.txt\n"
            'RUN pip install --no-cache-dir -r /tmp/requirements.txt "mcp>=1.9.0"'
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
        'RUN pip install --no-cache-dir "mcp>=1.9.0"'
    )


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


def plan(
    project: Project,
    harness: str,
    python_version: str,
    demo: bool,
    mlflow: bool = False,
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
        body = "\n".join(
            ["# Extracted from pyproject.toml [project.dependencies] by blinded_init.py."]
            + project.deps_list
        )
        actions.append(Action("create", out / "requirements.txt", body + "\n"))

    # Hook module: a distinct filename so we never clobber an existing hooks.py.
    hooks_dest = root / "src" / project.package / "blinded_hooks.py"
    hooks_src = (DEMO_PROJECT / "src" / DEMO_PACKAGE / "hooks.py").read_text(encoding="utf-8")
    snippet = HOOK_SNIPPET.format(package=project.package, harness=harness)

    if project.settings_has_confine:
        # The scaffold ships this wired up, and re-runs must not duplicate it.
        actions.append(Action("skip", project.settings_path, note="ConfineWritesToData already wired"))
    else:
        if hooks_dest.exists():
            actions.append(Action("skip", hooks_dest, note="already exists"))
        else:
            actions.append(Action("create", hooks_dest, hooks_src))

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
        actions.append(Action("skip", devcontainer, note="already exists — left alone"))
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
        row("note", "settings.py", "HOOKS already defined — merge by hand")

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
    print("  container has no route to. Existing logging calls keep working; the")
    print("  UI is published to you on 127.0.0.1:5000. It stays unreachable to")
    print("  the agent because log_artifact and log_figure store whole files.")
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


def ladder(root: Path, harness: str, package: str, demo: bool, mlflow: bool) -> None:
    path = root / harness
    compose = "docker compose"
    if mlflow:
        compose += " -f docker-compose.yml -f docker-compose.mlflow.yml"
    print("\nNext — run these yourself (this script does not run docker):")
    print("-" * 60)
    print(f"  cd {path}")
    print(f"  {compose} build mcp_server")
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
        print("\nAnd confirm the agent cannot reach the tracking server:")
        print(f"  {compose} exec dev node -e "
              "\"fetch('http://mlflow:5000').then(r=>console.log('REACHABLE',r.status))"
              ".catch(e=>console.log('blocked:',e.cause?.code||e.message))\"")
        print("  # must print 'blocked: EAI_AGAIN' (name does not resolve from dev)")
        print("  # the UI is yours at http://127.0.0.1:5000")


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
    if uses_mlflow(project) and not use_mlflow:
        warnings.append(
            "MLflow is used here but the overlay was declined, so those calls "
            "will fail. It cannot simply be allowed out either: log_artifact and "
            "log_figure upload whole files and images. Add the overlay or remove "
            "the calls."
        )

    actions = plan(project, harness, args.python, demo, use_mlflow)
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
    ladder(project.root, harness, project.package, demo, use_mlflow)
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
