"""Tests for the setup wizard and the metrics boundary.

Stdlib unittest only — no pytest, and no dependency on `mcp` being installed
(server.py is imported with a stub, see _load_server).

    python -m unittest discover tests
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import blinded_init  # noqa: E402


def _load_server():
    """Import mcp_server/server.py with a stubbed FastMCP.

    The real `mcp` package is a runtime dependency of the data-side image, not
    of this test suite, so stub just enough of it to let the module import.
    """
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            return lambda fn: fn

        def run(self, *args, **kwargs):
            pass

    fastmcp.FastMCP = _FastMCP
    server_pkg = types.ModuleType("mcp.server")
    server_pkg.fastmcp = fastmcp
    root = types.ModuleType("mcp")
    root.server = server_pkg
    sys.modules.setdefault("mcp", root)
    sys.modules.setdefault("mcp.server", server_pkg)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp)

    spec = importlib.util.spec_from_file_location(
        "blinded_server", REPO / "mcp_server" / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server()


def make_project(root: Path, package: str = "demo_pkg", *, settings: str = "") -> Path:
    """Minimal Kedro-shaped project on disk."""
    (root / "src" / package).mkdir(parents=True)
    (root / "conf" / "base").mkdir(parents=True)
    (root / "conf" / "local").mkdir(parents=True)
    (root / "data" / "01_raw").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "{package}"
            dependencies = ["kedro>=0.19.9,<0.20.0", "pandas>=2.0.0"]

            [tool.kedro]
            package_name = "{package}"
            project_name = "{package}"
            """
        ).strip(),
        encoding="utf-8",
    )
    (root / "conf" / "base" / "catalog.yml").write_text(
        "metrics:\n  type: json.JSONDataset\n"
        "  filepath: data/claude_visible_metrics/metrics.json\n  versioned: true\n",
        encoding="utf-8",
    )
    if settings:
        (root / "src" / package / "settings.py").write_text(settings, encoding="utf-8")
    return root


class ContractConstantsTests(unittest.TestCase):
    """server.py enforces the metric contract; the wizard quotes it in its
    findings table and CLAUDE.md documents it. If they drift, the wizard states
    something false about what will actually cross the boundary."""

    def setUp(self):
        # A fresh module: LatestPerDatasetTests reassigns TRACKING_DIR on the
        # shared one, so reading it here would be test-order dependent.
        self.pristine = _load_server()

    def test_caps_match_the_server(self):
        self.assertEqual(blinded_init.MAX_METRIC_DEPTH, self.pristine.MAX_METRIC_DEPTH)
        self.assertEqual(blinded_init.MAX_METRIC_KEYS, self.pristine.MAX_METRIC_KEYS)

    def test_default_tracking_dir_matches_the_server(self):
        # The wizard tells the user where to write; the server decides where to
        # read. Disagreement means get_metrics is silently always empty.
        self.assertEqual(
            blinded_init.TRACKING_DIR,
            "/".join(self.pristine.TRACKING_DIR.parts[-2:]),
        )

    def test_agent_guide_names_the_same_directory(self):
        guide = blinded_init.render("CLAUDE.md.tmpl", {"WORKSPACE_TREE": "", "DEMO_NOTES": ""})
        self.assertIn(blinded_init.TRACKING_DIR, guide)


class FlattenNumericTests(unittest.TestCase):
    """The metric filter is the return channel — it gets the most scrutiny."""

    def test_keeps_top_level_numbers(self):
        self.assertEqual(
            server._flatten_numeric({"auc": 0.9, "n": 3}), {"auc": 0.9, "n": 3}
        )

    def test_flattens_nested_dicts_to_dotted_keys(self):
        got = server._flatten_numeric({"model_a": {"auc": 0.9}, "model_b": {"auc": 0.8}})
        self.assertEqual(got, {"model_a.auc": 0.9, "model_b.auc": 0.8})

    def test_drops_strings_and_none(self):
        got = server._flatten_numeric({"auc": 0.9, "note": "row-1,row-2", "x": None})
        self.assertEqual(got, {"auc": 0.9})

    def test_drops_lists_entirely(self):
        # A list of floats is a whole data column. It must never cross.
        got = server._flatten_numeric({"preds": [0.1, 0.2, 0.3], "auc": 0.9})
        self.assertEqual(got, {"auc": 0.9})

    def test_drops_lists_nested_inside_dicts(self):
        got = server._flatten_numeric({"m": {"preds": [1.0, 2.0], "auc": 0.5}})
        self.assertEqual(got, {"m.auc": 0.5})

    def test_drops_bools(self):
        self.assertEqual(server._flatten_numeric({"ok": True, "auc": 0.1}), {"auc": 0.1})

    def test_stops_at_depth_cap(self):
        deep = value = {}
        for _ in range(server.MAX_METRIC_DEPTH + 3):
            child = {}
            value["nest"] = child
            value = child
        value["auc"] = 0.9
        self.assertEqual(server._flatten_numeric(deep), {})

    def test_non_dict_input_yields_nothing(self):
        self.assertEqual(server._flatten_numeric([1, 2, 3]), {})
        self.assertEqual(server._flatten_numeric("0.9"), {})


class LatestPerDatasetTests(unittest.TestCase):
    def test_newest_version_per_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracking = Path(tmp) / "data" / "claude_visible_metrics"
            # Kedro versioned layout: <dataset>.json/<version>/<dataset>.json
            for dataset, versions in (("a.json", ("v1", "v2")), ("b.json", ("v1",))):
                for index, version in enumerate(versions):
                    path = tracking / dataset / version / dataset
                    path.parent.mkdir(parents=True)
                    path.write_text("{}", encoding="utf-8")
                    stamp = 1_700_000_000 + index * 100
                    os.utime(path, (stamp, stamp))

            server.TRACKING_DIR = tracking
            got = sorted(p.parent.name for p in server._latest_per_dataset())
            self.assertEqual(got, ["v1", "v2"])  # newest of a/, only one of b/

    def test_merges_all_datasets_with_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracking = root / "data" / "claude_visible_metrics"
            for dataset, payload in (("a.json", '{"auc": 0.9}'), ("b.json", '{"auc": 0.5}')):
                path = tracking / dataset / "v1" / dataset
                path.parent.mkdir(parents=True)
                path.write_text(payload, encoding="utf-8")

            server.TRACKING_DIR = tracking
            server.KEDRO_PROJECT_PATH = root
            result = server._load_metrics()
            self.assertEqual(result["metrics"], {"a.auc": 0.9, "b.auc": 0.5})
            self.assertEqual(len(result["sources"]), 2)

    def test_single_dataset_is_not_prefixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracking = root / "data" / "claude_visible_metrics"
            path = tracking / "metrics.json" / "v1" / "metrics.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"auc": 0.9}', encoding="utf-8")

            server.TRACKING_DIR = tracking
            server.KEDRO_PROJECT_PATH = root
            self.assertEqual(server._load_metrics()["metrics"], {"auc": 0.9})

    def test_unreadable_file_reports_type_not_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracking = root / "data" / "claude_visible_metrics"
            path = tracking / "bad.json" / "v1" / "bad.json"
            path.parent.mkdir(parents=True)
            # A decode error can quote the document; only the type may escape.
            path.write_text('{"secret_row": "PATIENT-12345", ', encoding="utf-8")

            server.TRACKING_DIR = tracking
            server.KEDRO_PROJECT_PATH = root
            result = server._load_metrics()
            self.assertEqual(result["unreadable"], [{"dataset": "bad", "error_type": "JSONDecodeError"}])
            self.assertNotIn("PATIENT-12345", repr(result))


class MountGuardTests(unittest.TestCase):
    """One wrong mount voids the model, so these are hard failures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_allows_code_directories(self):
        for name in ("src", "conf/base"):
            self.assertTrue(blinded_init.guard_mount(self.root, name).exists())

    def test_rejects_data(self):
        for name in ("data", "data/01_raw", "./data", "data/"):
            with self.assertRaises(blinded_init.MountError):
                blinded_init.guard_mount(self.root, name)

    def test_rejects_credentials(self):
        with self.assertRaises(blinded_init.MountError):
            blinded_init.guard_mount(self.root, "conf/local")

    def test_rejects_ancestor_of_forbidden_path(self):
        # conf/ would drag conf/local (credentials) along with it.
        with self.assertRaises(blinded_init.MountError):
            blinded_init.guard_mount(self.root, "conf")

    def test_rejects_project_root(self):
        for name in (".", "", "./", "/"):
            with self.assertRaises(blinded_init.MountError):
                blinded_init.guard_mount(self.root, name)

    def test_rejects_paths_outside_the_project(self):
        for name in ("..", "../elsewhere", "src/../.."):
            with self.assertRaises(blinded_init.MountError):
                blinded_init.guard_mount(self.root, name)


class IsSensitiveTests(unittest.TestCase):
    def test_flags_data_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            self.assertTrue(blinded_init.is_sensitive(root, root / "data" / "01_raw" / "x.csv"))
            self.assertTrue(blinded_init.is_sensitive(root, root / "conf" / "local" / "credentials.yml"))
            self.assertFalse(blinded_init.is_sensitive(root, root / "conf" / "base" / "catalog.yml"))
            self.assertFalse(blinded_init.is_sensitive(root, root / "src" / "demo_pkg"))


class ParsePyprojectTests(unittest.TestCase):
    def _write(self, tmp: str) -> Path:
        path = Path(tmp) / "pyproject.toml"
        path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "fallback_name"
                dependencies = [
                    "kedro>=0.19.9,<0.20.0",
                    "pandas>=2.0.0",
                ]

                [tool.kedro]
                package_name = "my_pkg"
                project_name = "My Project"
                """
            ).strip(),
            encoding="utf-8",
        )
        return path

    def test_tomllib_path(self):
        if blinded_init.tomllib is None:
            self.skipTest("tomllib unavailable on this interpreter")
        with tempfile.TemporaryDirectory() as tmp:
            got = blinded_init.parse_pyproject(self._write(tmp))
            self.assertEqual(got["package_name"], "my_pkg")
            self.assertEqual(got["project_name"], "My Project")
            self.assertEqual(len(got["dependencies"]), 2)

    def test_regex_fallback_matches_tomllib(self):
        original = blinded_init.tomllib
        blinded_init.tomllib = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                got = blinded_init.parse_pyproject(self._write(tmp))
                self.assertEqual(got["package_name"], "my_pkg")
                self.assertEqual(got["project_name"], "My Project")
                self.assertEqual(
                    got["dependencies"], ["kedro>=0.19.9,<0.20.0", "pandas>=2.0.0"]
                )
        finally:
            blinded_init.tomllib = original


class ExtrasTruncationTests(unittest.TestCase):
    """Regression: a dependency using extras must not truncate the list.

    ``dependencies = ["kedro[jupyter]", "umap-learn"]`` used to stop at the ']'
    inside the first entry, silently dropping everything after it — a wrong
    answer that looked like a right one, and only on the 3.9/3.10 fallback path.
    """

    SOURCE = textwrap.dedent(
        """
        [project]
        name = "fallback_name"
        dependencies = [
            "kedro[jupyter,docs]>=0.19.9,<0.20.0",
            "pandas[performance]>=2.0.0",   # don't drop me
            "umap-learn>=0.5.12",
        ]

        [project.optional-dependencies]
        dev = ["pytest"]
        gpu = ["cupy-cuda12x"]

        [tool.kedro]
        package_name = "my_pkg"
        project_name = "My Project"
        """
    ).strip()

    EXPECTED = [
        "kedro[jupyter,docs]>=0.19.9,<0.20.0",
        "pandas[performance]>=2.0.0",
        "umap-learn>=0.5.12",
    ]

    def _parse(self, use_regex: bool) -> dict:
        original = blinded_init.tomllib
        if use_regex:
            blinded_init.tomllib = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "pyproject.toml"
                path.write_text(self.SOURCE, encoding="utf-8")
                return blinded_init.parse_pyproject(path)
        finally:
            blinded_init.tomllib = original

    def test_regex_fallback_keeps_entries_after_extras(self):
        self.assertEqual(self._parse(use_regex=True)["dependencies"], self.EXPECTED)

    def test_both_parsers_agree(self):
        if blinded_init.tomllib is None:
            self.skipTest("tomllib unavailable on this interpreter")
        self.assertEqual(
            self._parse(use_regex=False)["dependencies"],
            self._parse(use_regex=True)["dependencies"],
        )

    def test_non_dev_groups_reported_and_dev_groups_ignored(self):
        for use_regex in (True, False):
            if not use_regex and blinded_init.tomllib is None:
                continue
            groups = self._parse(use_regex)["dep_groups"]
            self.assertEqual(len(groups), 1, groups)
            self.assertIn("gpu", groups[0])

    def test_unterminated_array_is_empty_not_a_hang(self):
        self.assertEqual(
            blinded_init.toml_string_array('dependencies = [\n  "kedro",\n', "dependencies"),
            [],
        )


class ClassifyDepsTests(unittest.TestCase):
    def test_windows_only_wheel_is_separated_out(self):
        keep, dropped = blinded_init.classify_deps(
            ["torch>=2.6.0", "triton-windows>=3.7.0.post26", "umap-learn>=0.5.12"]
        )
        self.assertEqual(keep, ["torch>=2.6.0", "umap-learn>=0.5.12"])
        self.assertEqual([spec for spec, _ in dropped], ["triton-windows>=3.7.0.post26"])

    def test_existing_platform_marker_is_left_alone(self):
        # pip evaluates the marker itself, which beats second-guessing the author.
        spec = 'pywin32>=306 ; sys_platform == "win32"'
        keep, dropped = blinded_init.classify_deps([spec])
        self.assertEqual(keep, [spec])
        self.assertEqual(dropped, [])

    def test_name_normalisation(self):
        self.assertEqual(blinded_init.requirement_name("Triton_Windows>=3.7"), "triton-windows")
        self.assertEqual(blinded_init.requirement_name("kedro[jupyter]>=0.19"), "kedro")

    def test_requirements_body_comments_out_and_explains(self):
        project = types.SimpleNamespace(
            deps_list=["torch>=2.6.0", "triton-windows>=3.7.0.post26"]
        )
        body = blinded_init.requirements_body(project, "3.11")
        self.assertIn("torch>=2.6.0", body)
        self.assertIn("# triton-windows>=3.7.0.post26", body)
        self.assertIn("NOT a verbatim copy", body)
        # The live (uncommented) lines must not include the Windows-only wheel.
        live = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(live, ["torch>=2.6.0"])


class GpuReservationTests(unittest.TestCase):
    def _compose(self, gpu: bool) -> str:
        project = blinded_init.detect(
            blinded_init.DEMO_PROJECT, [], blinded_init.TRACKING_DIR
        )
        actions = blinded_init.plan(project, "blinded", "3.11", demo=False, gpu=gpu)
        compose = next(a for a in actions if a.path.name == "docker-compose.yml")
        return compose.content

    def test_absent_by_default(self):
        self.assertNotIn("nvidia", self._compose(gpu=False))

    def test_reserved_on_mcp_server_only_when_asked(self):
        content = self._compose(gpu=True)
        self.assertIn("driver: nvidia", content)
        self.assertIn("capabilities: [gpu]", content)
        # It must land in mcp_server (which runs the pipelines), not in dev.
        mcp_block, _, dev_block = content.partition("\n  dev:")
        self.assertIn("driver: nvidia", mcp_block)
        self.assertNotIn("driver: nvidia", dev_block)

    def test_gpu_does_not_widen_the_network_boundary(self):
        # A GPU reservation must not become a route out: mcp_server stays on the
        # internal-only bridge and keeps its read-only filesystem.
        mcp_block = self._compose(gpu=True).partition("\n  dev:")[0]
        self.assertIn("read_only: true", mcp_block)
        self.assertIn("mcp_bridge", mcp_block)
        self.assertNotIn("external", mcp_block.partition("    networks:")[2])


class RenderTests(unittest.TestCase):
    def test_unfilled_token_is_an_error(self):
        with self.assertRaises(blinded_init.SetupError):
            blinded_init.render("docker-compose.yml.tmpl", {"HARNESS_DIR": "blinded"})

    def test_dollar_syntax_survives_substitution(self):
        # string.Template would mangle ${CLAUDE_CODE_OAUTH_TOKEN:-}; str.replace must not.
        text = self._compose()
        self.assertIn("${CLAUDE_CODE_OAUTH_TOKEN:-}", text)
        self.assertIn("- ../src:/workspace/src", text)

    def test_dev_service_mounts_nothing_sensitive(self):
        """The generated compose file is the artifact that has to be right."""
        dev_block = self._compose().split("\n  dev:\n", 1)[1].split("\nnetworks:", 1)[0]
        sources = re.findall(r"^\s+- (\S+?):/workspace", dev_block, re.M)
        self.assertIn("../src", sources)
        for source in sources:
            self.assertNotIn("/data", source)
            self.assertNotIn("conf/local", source)

    def _compose(self) -> str:
        project = self._project()
        return blinded_init.render(
            "docker-compose.yml.tmpl",
            {
                "HARNESS_DIR": "blinded",
                "PROJECT_NAME": "demo",
                "PACKAGE": "demo_pkg",
                "PYTHON_VERSION": "3.11",
                "DEV_MOUNTS": blinded_init.mount_lines(project),
                "DEPS_INSTALL": "",
                "WORKSPACE_TREE": "",
                "DEMO_NOTES": "",
                "GPU_RESERVATION": "",
            },
        )

    def _project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = make_project(Path(tmp.name))
        return blinded_init.detect(root, [], blinded_init.TRACKING_DIR)


class MlflowOverlayTests(unittest.TestCase):
    """MLflow holds artifacts derived from the data, so the agent must have no
    route to it. That is enforced purely by which networks each service joins."""

    def setUp(self):
        self.text = blinded_init.render(
            "docker-compose.mlflow.yml.tmpl", {"PROJECT_NAME": "demo"}
        )

    def test_overlay_never_declares_a_dev_service(self):
        # Compose merges overlays into the base file, and network lists merge
        # additively. A `dev` block here — for any reason — could hand the agent
        # a route to the artifact store.
        self.assertNotIn("\n  dev:", self.text)

    def test_mlflow_and_mcp_server_share_the_tracking_network(self):
        mlflow_block = self.text.split("\n  mlflow:", 1)[1].split("\n  mcp_server:", 1)[0]
        self.assertIn("- tracking", mlflow_block)

        mcp_block = self.text.split("\n  mcp_server:", 1)[1].split("\nnetworks:", 1)[0]
        self.assertIn("- tracking", mcp_block)
        self.assertIn("- mcp_bridge", mcp_block)  # must keep its MCP door open

    def test_tracking_network_has_no_internet(self):
        networks = self.text.split("\nnetworks:", 1)[1]
        tracking = networks.split("tracking:", 1)[1].split("viewer:", 1)[0]
        self.assertIn("internal: true", tracking)

    def test_always_on_mlflow_publishes_no_port(self):
        """A published port routes around every network rule above.

        On Docker Desktop, publishing happens inside the Linux VM and any
        container can reach that VM's gateway, so `127.0.0.1:5000:5000` on the
        always-on service leaves the API readable from dev at
        host.docker.internal:5000. Asserting the string appears *somewhere* is
        not enough — it has to be absent from the service that is always up.
        """
        mlflow_block = self.text.split("\n  mlflow:", 1)[1].split("\n  mlflow_ui:", 1)[0]
        self.assertNotIn("ports:", mlflow_block)
        self.assertNotIn("- viewer", mlflow_block)

    def test_ui_publisher_is_opt_in_and_loopback_only(self):
        ui_block = self.text.split("\n  mlflow_ui:", 1)[1].split("\n  mcp_server:", 1)[0]
        self.assertIn("profiles: [ui]", ui_block)
        self.assertIn('"127.0.0.1:5000:5000"', ui_block)

    def test_overlay_is_written_only_when_asked(self):
        project = blinded_init.detect(blinded_init.DEMO_PROJECT, [], blinded_init.TRACKING_DIR)
        names = lambda acts: {a.path.name for a in acts}
        self.assertNotIn(
            "docker-compose.mlflow.yml",
            names(blinded_init.plan(project, "blinded", "3.11", demo=False)),
        )
        self.assertIn(
            "docker-compose.mlflow.yml",
            names(blinded_init.plan(project, "blinded", "3.11", demo=False, mlflow=True)),
        )


class DetectTests(unittest.TestCase):
    def test_detects_bundled_demo_project(self):
        """Self-test: the repo wraps its own demo."""
        project = blinded_init.detect(blinded_init.DEMO_PROJECT, [], blinded_init.TRACKING_DIR)
        self.assertEqual(project.package, "dummy_project")
        self.assertIn("src", project.mounts)
        self.assertIn("conf/base", project.mounts)
        self.assertNotIn("data", project.mounts)
        self.assertNotIn("conf/local", project.mounts)
        self.assertTrue(project.tracking_ok)
        self.assertTrue(project.settings_has_confine)
        self.assertEqual(project.deps_kind, "pyproject")

    def test_plan_for_demo_never_touches_data(self):
        project = blinded_init.detect(blinded_init.DEMO_PROJECT, [], blinded_init.TRACKING_DIR)
        actions = blinded_init.plan(project, "blinded", "3.11", demo=False)
        for action in actions:
            self.assertFalse(
                blinded_init.is_sensitive(project.root, action.path),
                f"{action.kind} targets a sensitive path: {action.path}",
            )
        # Already wired, so the hook edits are skipped rather than duplicated.
        settings = [a for a in actions if a.path.name == "settings.py"]
        self.assertEqual([a.kind for a in settings], ["skip"])

    def test_rejects_non_kedro_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(blinded_init.SetupError):
                blinded_init.detect(Path(tmp), [], blinded_init.TRACKING_DIR)

    def test_rejects_unsafe_extra_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            with self.assertRaises(blinded_init.MountError):
                blinded_init.detect(root, ["data"], blinded_init.TRACKING_DIR)

    def test_missing_extra_mount_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            with self.assertRaises(blinded_init.SetupError):
                blinded_init.detect(root, ["nope"], blinded_init.TRACKING_DIR)

    def test_warns_when_nothing_writes_to_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            (root / "conf" / "base" / "catalog.yml").write_text(
                "raw:\n  type: pandas.CSVDataset\n  filepath: data/01_raw/x.csv\n",
                encoding="utf-8",
            )
            project = blinded_init.detect(root, [], blinded_init.TRACKING_DIR)
            self.assertFalse(project.tracking_ok)


class EgressScanTests(unittest.TestCase):
    def test_flags_outbound_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            (root / "src" / "demo_pkg" / "nodes.py").write_text(
                "import mlflow\nimport boto3\nurl = 'https://tracker.example.com'\n",
                encoding="utf-8",
            )
            tags = {tag for _, tag, _ in blinded_init.scan_egress(root)}
            self.assertIn("mlflow", tags)
            self.assertIn("boto3/s3", tags)

    def test_ignores_comments_and_local_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            (root / "src" / "demo_pkg" / "nodes.py").write_text(
                "# import mlflow\nurl = 'http://mcp_server:8000/sse'\n",
                encoding="utf-8",
            )
            self.assertEqual(blinded_init.scan_egress(root), [])

    def test_never_reads_data_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_project(Path(tmp))
            (root / "conf" / "local" / "credentials.yml").write_text(
                "password: https://secret.example.com\n", encoding="utf-8"
            )
            (root / "data" / "01_raw" / "rows.yml").write_text(
                "url: https://leak.example.com\n", encoding="utf-8"
            )
            for location, _, line in blinded_init.scan_egress(root):
                self.assertNotIn("secret.example.com", line)
                self.assertNotIn("leak.example.com", line)
                self.assertFalse(location.startswith("data/"))
                self.assertFalse(location.startswith("conf/local"))


class ScaffoldTests(unittest.TestCase):
    def test_renames_package_and_carries_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "fresh"
            blinded_init.scaffold(dest, "my_project")

            self.assertTrue((dest / "src" / "my_project" / "settings.py").is_file())
            self.assertFalse((dest / "src" / "dummy_project").exists())
            for path in dest.rglob("*.py"):
                self.assertNotIn(
                    "dummy_project", path.read_text(encoding="utf-8"), f"stale name in {path}"
                )
            self.assertEqual(list((dest / "data" / "01_raw").glob("*.csv")), [])
            # The generated harness CLAUDE.md supersedes the demo's copy.
            self.assertFalse((dest / "src" / "CLAUDE.md").exists())

    def test_rejects_bad_package_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            for name in ("My-Project", "1project", "my project", "", "import"):
                with self.assertRaises(blinded_init.SetupError):
                    blinded_init.scaffold(dest, name)
            self.assertFalse(dest.exists(), "rejected name must not leave a partial tree")

    def test_scaffolded_project_passes_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "fresh"
            blinded_init.scaffold(dest, "my_project")
            project = blinded_init.detect(dest, [], blinded_init.TRACKING_DIR)
            self.assertEqual(project.package, "my_project")
            self.assertTrue(project.tracking_ok)
            self.assertTrue(project.settings_has_confine)


if __name__ == "__main__":
    unittest.main()
