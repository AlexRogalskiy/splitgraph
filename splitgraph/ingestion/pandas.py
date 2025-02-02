"""Routines that ingest/export CSV files to/from Splitgraph images using Pandas"""

import csv
from io import StringIO
from typing import TYPE_CHECKING, Optional, Union

import pandas as pd
from pandas.core.frame import DataFrame
from pandas.core.series import Series
from pandas.io.sql import get_schema
from psycopg2.sql import SQL, Identifier
from sqlalchemy import create_engine
from sqlalchemy.engine.base import Engine

from splitgraph.core.image import Image
from splitgraph.core.repository import Repository
from splitgraph.ingestion.common import IngestionAdapter
from splitgraph.ingestion.csv import copy_csv_buffer

if TYPE_CHECKING:
    from splitgraph.engine.postgres.engine import PostgresEngine, PsycopgEngine


def _get_sqlalchemy_engine(engine: "PostgresEngine") -> Engine:
    server, port, username, password, dbname = (
        engine.conn_params["SG_ENGINE_HOST"],
        engine.conn_params["SG_ENGINE_PORT"],
        engine.conn_params["SG_ENGINE_USER"],
        engine.conn_params["SG_ENGINE_PWD"],
        engine.conn_params["SG_ENGINE_DB_NAME"],
    )
    return create_engine("postgresql://%s:%s@%s:%s/%s" % (username, password, server, port, dbname))


class PandasIngestionAdapter(IngestionAdapter):
    @staticmethod
    def create_ingestion_table(data, engine, schema: str, table: str, **kwargs):
        engine.delete_table(schema, table)
        # Use sqlalchemy's engine to convert types and create a DDL statement for the table.

        # If there's an unnamed index (created by Pandas), we don't add PKs to the table.
        if data.index.names == [None]:
            ddl = get_schema(data, name=table, con=_get_sqlalchemy_engine(engine))
        else:
            ddl = get_schema(
                data.reset_index(),
                name=table,
                keys=data.index.names,
                con=_get_sqlalchemy_engine(engine),
            )
        engine.run_sql_in(schema, ddl)

    @staticmethod
    def data_to_new_table(
        data, engine: "PsycopgEngine", schema: str, table: str, no_header: bool = True, **kwargs
    ):
        df_to_table_fast(engine, data, schema, table)

    @staticmethod
    def query_to_data(engine, query: str, schema: Optional[str] = None, **kwargs):
        # Pandas' `read_sql_table/query` because they has type inference via SQLAlchemy
        # (from the datatypes in the query that postgres gives back).
        if schema:
            query = (
                SQL("SET search_path TO {},public;")
                .format(Identifier(schema))
                .as_string(engine.connection)
                + query
            )

        return pd.read_sql_query(sql=query, con=_get_sqlalchemy_engine(engine), **kwargs)


def df_to_table_fast(
    engine: "PsycopgEngine", df: Union[Series, DataFrame], target_schema: str, target_table: str
):
    # Instead of using Pandas' to_sql, dump the dataframe to csv and then load it on the other
    # end using Psycopg's copy_to.
    # Don't write the index column if it's unnamed (generated by Pandas)
    csv_str = df.to_csv(
        header=False, index=df.index.names != [None], escapechar="\\", quoting=csv.QUOTE_ALL
    )
    # Dirty hack
    csv_str = csv_str.replace('""', "")
    buffer = StringIO()
    buffer.write(csv_str)
    buffer.seek(0)
    copy_csv_buffer(buffer, engine, target_schema, target_table, no_header=True)


_pandas_adapter = PandasIngestionAdapter()


def sql_to_df(
    sql: str,
    image: Optional[Union[Image, str]] = None,
    repository: Optional[Repository] = None,
    use_lq: bool = False,
    **kwargs
) -> DataFrame:
    """
    Executes an SQL query against a Splitgraph image, returning the result.

    Extra `**kwargs` are passed to Pandas' `read_sql_query`.

    :param sql: SQL query to execute.
    :param image: Image object, image hash/tag (`str`) or None (use the currently checked out image).
    :param repository: Repository the image belongs to. Must be set if `image` is a hash/tag or None.
    :param use_lq: Whether to use layered querying or check out the image if it's not checked out.
    :return: A Pandas dataframe.
    """
    return _pandas_adapter.to_data(sql, image, repository, use_lq, **kwargs)


def df_to_table(
    df: Union[Series, DataFrame],
    repository: Repository,
    table: str,
    if_exists: str = "patch",
    schema_check: bool = True,
) -> None:
    """Writes a Pandas DataFrame to a checked-out Splitgraph table. Doesn't create a new image.

    :param df: Pandas DataFrame to insert.
    :param repository: Splitgraph Repository object. Must be checked out.
    :param table: Table name.
    :param if_exists: Behaviour if the table already exists: 'patch' means that primary keys that already exist in the
    table will be updated and ones that don't will be inserted. 'replace' means that the table will be dropped and
    recreated.
    :param schema_check: If False, skips checking that the dataframe is compatible with the target schema.
    """
    _pandas_adapter.to_table(df, repository, table, if_exists, schema_check)
