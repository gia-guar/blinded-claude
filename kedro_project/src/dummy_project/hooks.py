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
        data_dir = (Path.cwd() / "data").resolve()

        for name in catalog.list():
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
    def _get_dataset(catalog, name):
        """Best-effort dataset lookup across kedro DataCatalog API variants."""
        getter = getattr(catalog, "_get_dataset", None)
        if getter is not None:
            try:
                return getter(name)
            except Exception:
                pass
        return getattr(catalog, "_datasets", {}).get(name)
