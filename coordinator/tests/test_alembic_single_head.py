"""
Alembic migration-graph smoke test.

THE INVARIANT under test: the alembic revision graph has EXACTLY ONE head. Two
unmerged heads (the P0-1 deploy blocker: 0020 -> both 0021_plan_feature_limits
and 0021_recipe_hash_review) crash `alembic upgrade head` /
`scripts/migrate.py` under `set -e` with "Multiple head revisions", crash-looping
every deploy. This test reads the migration scripts statically (NO database
connection, NO `alembic upgrade`) so it runs everywhere — on a laptop with
nothing running, and in CI — and fails the moment a second head is introduced.

It also asserts the graph is fully linkable (every down_revision resolves and the
single head walks back to a single base without a cycle), which catches a dangling
or mistyped down_revision before it reaches a real upgrade.

Runnable with plain `python3 coordinator/tests/test_alembic_single_head.py`.
"""
import os
import sys

try:  # pytest drives this under the suite; script-style runs without it installed.
    import pytest
except ModuleNotFoundError:  # pragma: no cover - script-style fallback
    pytest = None

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# The alembic config lives at coordinator/alembic.ini with script_location = alembic.
ALEMBIC_INI = os.path.join(BACKEND, "alembic.ini")


def _script_directory():
    """Load the alembic ScriptDirectory from the repo config WITHOUT touching a DB.

    ScriptDirectory only parses the migration files on disk; it never connects to
    Postgres, so this is safe to run offline. We point Config at the absolute
    alembic.ini and pin the script_location to the absolute alembic/ dir so the
    test is independent of the current working directory.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if not os.path.exists(ALEMBIC_INI):  # pragma: no cover - config must exist
        if pytest is not None:
            pytest.skip(f"alembic.ini not found at {ALEMBIC_INI}")
        raise RuntimeError(f"alembic.ini not found at {ALEMBIC_INI}")

    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("script_location", os.path.join(BACKEND, "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_exactly_one_head():
    """The single most important assertion: one head (else deploy crash-loops)."""
    sd = _script_directory()
    heads = sd.get_heads()
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads {heads!r}; expected exactly one. "
        "Merge them with `alembic merge` so `migrate.py` / `upgrade head` "
        "doesn't crash on 'Multiple head revisions'."
    )


def test_graph_walks_head_to_base_without_cycle():
    """Walk the whole revision chain from the head; every down_revision must
    resolve, terminate at a base, and not loop."""
    sd = _script_directory()
    (head,) = sd.get_heads()

    seen = set()
    revisions = list(sd.walk_revisions(base="base", head=head))
    for rev in revisions:
        assert rev.revision not in seen, f"cycle / duplicate revision {rev.revision}"
        seen.add(rev.revision)

    # Every script in the directory should be reachable from the single head
    # (no orphaned/forked revision floating off the main line).
    all_revs = {s.revision for s in sd.walk_revisions()}
    assert all_revs == seen, (
        f"unreachable revisions not on the head's line: {all_revs - seen!r}"
    )


def test_bases_single():
    """Exactly one base revision (a forked base would also fork the graph)."""
    sd = _script_directory()
    bases = sd.get_bases()
    assert len(bases) == 1, f"expected one base, got {bases!r}"


def main():
    failures = []
    for fn in (
        test_exactly_one_head,
        test_graph_walks_head_to_base_without_cycle,
        test_bases_single,
    ):
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except BaseException as ex:  # pytest.skip raises; treat as non-fatal here
            print(f"FAIL  {fn.__name__}: {ex}")
            failures.append(f"{fn.__name__}: {ex}")
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nAll alembic single-head smoke checks passed.")


if __name__ == "__main__":
    main()
