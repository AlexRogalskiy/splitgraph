import logging
import traceback
from collections import Callable
from functools import wraps

from psycopg2.sql import SQL, Identifier

from splitgraph.core.types import TableSchema, Changeset
from splitgraph.engine import validate_type
from splitgraph.engine.postgres.engine import get_change_key, PostgresEngine


def log_exception(f):
    """Emit exceptions with full traceback instead of just the error text"""

    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception:
            logging.error(traceback.format_exc())
            raise

    return wrapped


def rollback_at_end(func: Callable) -> Callable:
    @wraps(func)
    def wrapped(self, *args, **kwargs):
        repository = self.image.repository
        try:
            return func(self, *args, **kwargs)
        finally:
            repository.rollback_engines()

    return wrapped


def _migrate_schema(engine, table_schema, table_name, table_schema_spec, new_schema_spec):
    """Migrate the schema of a table to match the schema_spec"""

    old_cols = {c.name: c.pg_type for c in table_schema_spec}
    new_cols = {c.name: c.pg_type for c in new_schema_spec}
    for c in old_cols:
        if c not in new_cols:
            engine.run_sql(
                SQL("ALTER TABLE {}.{} DROP COLUMN {}").format(
                    Identifier(table_schema),
                    Identifier(table_name),
                    Identifier(c),
                )
            )
    for c in new_cols:
        if c not in old_cols:
            engine.run_sql(
                SQL("ALTER TABLE {}.{} ADD COLUMN {} %s" % validate_type(new_cols[c])).format(
                    Identifier(table_schema),
                    Identifier(table_name),
                    Identifier(c),
                )
            )
        elif new_cols[c] != old_cols[c]:
            engine.run_sql(
                SQL(
                    "ALTER TABLE {}.{} ALTER COLUMN {} TYPE %s" % validate_type(new_cols[c])
                ).format(
                    Identifier(table_schema),
                    Identifier(table_name),
                    Identifier(c),
                )
            )


def _make_changeset(
    engine: PostgresEngine,
    schema: str,
    table: str,
    schema_spec: TableSchema,
    upsert_condition: str = "TRUE",
) -> Changeset:
    """Build a fake changeset from the temporary table and the existing table to pass
    to the object manager (store as a Splitgraph diff)."""

    # PK -> (upserted / deleted, old row, new row)
    # We don't find out the old row here. This is because it requires a JOIN on the current
    # Splitgraph table, so if we're adding e.g. 100k rows to a 1M row table, it's going to cause big
    # performance issues. Instead, we pretend that all rows
    # have been inserted (apart from the ones that have been deleted by having the magic
    # _sdc_deleted_at column).

    # We also don't care about finding out the new row here, as the storage routine queries
    # the table directly to get those values.

    # The tradeoff is that now, when querying the table, we need to include not only fragments
    # whose index matches the query, but also all fragments that might overwrite those fragments
    # (through PK range overlap). Since we don't record old row values in this changeset's index,
    # we can no longer find if a fragment deletes some row by inspecting the index -- we need to
    # use PK ranges to find out overlapping fragments.

    change_key = [c for c, _ in get_change_key(schema_spec)]
    # Query:
    # SELECT (col_1, col_2, ...) AS pk,
    #        (custom upsert condition),
    #        {} AS old_row
    # FROM new_table n
    query = (
        SQL("SELECT ")
        + SQL(",").join(SQL("n.") + Identifier(c) for c in change_key)
        + SQL(",")
        + SQL(upsert_condition + " AS upserted FROM {}.{} n").format(
            Identifier(schema), Identifier(table)
        )
    ).as_string(engine.connection)
    result = engine.run_sql(query)
    return {tuple(row[:-1]): (row[-1], {}, {}) for row in result}
