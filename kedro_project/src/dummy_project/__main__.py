"""Entry point so the project is runnable as `python -m dummy_project`."""
from pathlib import Path

from kedro.framework.cli.utils import find_run_command
from kedro.framework.project import configure_project


def main(*args, **kwargs):
    package_name = Path(__file__).parent.name
    configure_project(package_name)

    interactive = hasattr(__builtins__, "__IPYTHON__")
    kwargs["standalone_mode"] = not interactive

    run = find_run_command(package_name)
    run(*args, **kwargs)


if __name__ == "__main__":
    main()
