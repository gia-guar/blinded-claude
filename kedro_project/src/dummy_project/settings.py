"""Project settings. See https://docs.kedro.org for the full list of options."""
from dummy_project.hooks import ConfineWritesToData

# Defense-in-depth lint: refuse to run if any catalog dataset would write
# outside data/. The hard control is the read-only container FS (docker-compose).
HOOKS = (ConfineWritesToData(),)
