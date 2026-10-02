"""The migration history must have exactly one head.

Two branches that each add a migration on the same parent both pass their own
tests and merge without a git conflict, since they add different files. The
history then has two heads, `alembic upgrade head` refuses to run, and the
container restarts forever. This fails the second of the two pull requests
before it merges.

It reads the migration files only. It does not need a database.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parent.parent


def _script() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_migrations_have_a_single_head():
    heads = _script().get_heads()
    assert len(heads) == 1, (
        f"the migration history has {len(heads)} heads: {sorted(heads)}. "
        "Point the newer migration's down_revision at the other head, or add a "
        "merge revision with `alembic merge heads`."
    )
