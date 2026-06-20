"""Project pipelines registry."""
from __future__ import annotations

from kedro.framework.project import find_pipelines
from kedro.pipeline import Pipeline


def register_pipelines() -> dict[str, Pipeline]:
    """Auto-discover pipelines under dummy_project/pipelines and register them.

    ``__default__`` is the sum of all discovered pipelines EXCEPT ``failing``,
    which deliberately raises. Keeping it out of the default means a normal
    ``run_pipeline()`` still succeeds; trigger the error path explicitly with
    ``run_pipeline(pipeline="failing")``.

    Returns:
        A mapping from pipeline name to ``Pipeline`` object.
    """
    pipelines = find_pipelines()
    default_parts = [p for name, p in pipelines.items() if name != "failing"]
    pipelines["__default__"] = sum(default_parts)
    return pipelines
