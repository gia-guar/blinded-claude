"""Project hooks.

ConfineWritesToData is a *defense-in-depth lint*, not a security boundary: it
fails fast at catalog-creation time if any catalog dataset is configured to
write outside the project's ``data/`` directory. It only sees catalog-mediated
saves — it cannot stop a node from calling ``open()`` / ``to_parquet()``
directly. The real enforcement is the read-only container filesystem in
docker-compose.yml (data/ is the only writable path).
"""
from __future__ import annotations

from pathlib import Path

from kedro.framework.hooks import hook_impl


class ConfineWritesToData:
    @hook_impl
    def after_catalog_created(self, catalog) -> None:
        data_dir = (self._project_root() / "data").resolve()

        for name in self._dataset_names(catalog):
            dataset = self._get_dataset(catalog, name)
            if dataset is None:
                continue

            raw_filepath = getattr(dataset, "_filepath", None)
            if raw_filepath is None:
                continue  # MemoryDataset and other non-file datasets

            filepath = Path(str(raw_filepath)).resolve()
            if filepath != data_dir and data_dir not in filepath.parents:
                raise RuntimeError(
                    f"Dataset '{name}' is configured to write outside data/: "
                    f"{filepath}. Move its filepath under data/."
                )

    @staticmethod
    def _project_root() -> Path:
        """Project root, independent of the process working directory.

        Not ``Path.cwd()``: the harness runs pipelines from ``pipeline_runner.py``
        with the project bind-mounted at a path that is not the working
        directory, so cwd pointed the lint at a directory containing no datasets
        and every catalog entry looked like a violation. Kedro resolves relative
        catalog filepaths against the project root, so that is the only correct
        thing to compare against.
        """
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "pyproject.toml").is_file():
                return parent
        return here.parents[2]  # src/<package>/blinded_hooks.py

    @staticmethod
    def _dataset_names(catalog):
        """Dataset names across kedro DataCatalog API variants.

        Kedro 1.0 made DataCatalog Mapping-like and dropped ``list()`` in favour
        of ``keys()``. Calling the wrong one is not a soft failure: the hook
        raises AttributeError inside ``after_catalog_created``, so every run dies
        before a node executes and the lint never inspects anything.
        """
        for attr in ("keys", "list"):
            method = getattr(catalog, attr, None)
            if method is not None:
                try:
                    return list(method())
                except Exception:
                    continue
        return list(getattr(catalog, "_datasets", {}))

    @staticmethod
    def _get_dataset(catalog, name):
        """Best-effort dataset lookup across kedro DataCatalog API variants."""
        for attr in ("_get_dataset", "get"):
            getter = getattr(catalog, attr, None)
            if getter is not None:
                try:
                    return getter(name)
                except Exception:
                    continue
        return getattr(catalog, "_datasets", {}).get(name)
