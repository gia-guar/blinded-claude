"""Assemble the deliberately failing pipeline: a single node that raises."""
from kedro.pipeline import Pipeline, node, pipeline

from .nodes import boom


def create_pipeline(**kwargs) -> Pipeline:
    return pipeline(
        [
            node(
                func=boom,
                inputs=None,
                outputs="_boom_dummy",
                name="boom_node",
            ),
        ]
    )
