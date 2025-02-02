import gzip
import json
import logging
import os
from contextlib import contextmanager
from copy import copy
from itertools import islice
from typing import Optional

import requests
from urllib3 import HTTPResponse

import splitgraph.config
from splitgraph.config import get_singleton
from splitgraph.exceptions import get_exception_name
from splitgraph.ingestion.common import generate_column_names
from splitgraph.ingestion.csv.common import (
    CSVOptions,
    dump_options,
    get_s3_params,
    load_options,
    make_csv_reader,
    pad_csv_row,
)
from splitgraph.ingestion.inference import infer_sg_schema

try:
    from multicorn import ANY, ColumnDefinition, ForeignDataWrapper, TableDefinition
except ImportError:
    # Multicorn not installed (OK if we're not on the engine -- tests).
    ForeignDataWrapper = object
    ANY = object()
    TableDefinition = dict
    ColumnDefinition = dict

try:
    from multicorn.utils import log_to_postgres
except ImportError:

    def log_to_postgres(*args, **kwargs):
        print(*args)


_PG_LOGLEVEL = logging.INFO


def _get_table_definition(response, fdw_options, table_name, table_options):
    # Allow overriding introspection options with per-table params (e.g. encoding, delimiter...)
    fdw_options = copy(fdw_options)
    fdw_options.update(table_options)

    csv_options, reader = make_csv_reader(response, CSVOptions.from_fdw_options(fdw_options))
    sample = list(islice(reader, csv_options.schema_inference_rows))

    if not csv_options.header:
        sample = [[""] * len(sample[0])] + sample

    # Ignore empty lines (newlines at the end of file etc)
    sample = [row for row in sample if len(row) > 0]

    sg_schema = infer_sg_schema(sample, None, None)

    # For nonexistent column names: replace with autogenerated ones (can't have empty column names)
    sg_schema = generate_column_names(sg_schema)

    # Merge the autodetected table options with the ones passed to us originally (e.g.
    # S3 object etc)
    new_table_options = copy(table_options)
    new_table_options.update(csv_options.to_table_options())

    # Build Multicorn TableDefinition. ColumnDefinition takes in type OIDs,
    # typmods and other internal PG stuff but other FDWs seem to get by with just
    # the textual type name.
    return TableDefinition(
        table_name=table_name,
        schema=None,
        columns=[ColumnDefinition(column_name=c.name, type_name=c.pg_type) for c in sg_schema],
        options=dump_options(new_table_options),
    )


@contextmanager
def report_errors(table_name: str):
    """Context manager that ignores exceptions and serializes them to JSON using PG's notice
    mechanism instead. The data source is meant to load these to report on partial failures
    (e.g. failed to load one table, but not others)."""
    try:
        yield
    except Exception as e:
        logging.error(
            "Error scanning %s, ignoring: %s: %s",
            table_name,
            get_exception_name(e),
            e,
            exc_info=e,
        )
        log_to_postgres(
            "SPLITGRAPH: "
            + json.dumps(
                {
                    "table_name": table_name,
                    "error": get_exception_name(e),
                    "error_text": str(e),
                }
            )
        )


class CSVForeignDataWrapper(ForeignDataWrapper):
    """Foreign data wrapper for CSV files stored in S3 buckets or HTTP"""

    def __init__(self, fdw_options, fdw_columns):
        # Initialize the logger that will log to the engine's stderr: log timestamp and PID.

        logging.basicConfig(
            format="%(asctime)s [%(process)d] %(levelname)s %(message)s",
            level=get_singleton(splitgraph.config.CONFIG, "SG_LOGLEVEL"),
        )

        # Dict of connection parameters
        self.fdw_options = load_options(fdw_options)

        # The foreign datawrapper columns (name -> ColumnDefinition).
        self.fdw_columns = fdw_columns
        self._num_cols = len(fdw_columns)

        self.csv_options = CSVOptions.from_fdw_options(self.fdw_options)

        # For HTTP: use full URL
        if self.fdw_options.get("url"):
            self.mode = "http"
            self.url = self.fdw_options["url"]
        else:
            self.mode = "s3"
            self.s3_client, self.s3_bucket, self.s3_object_prefix = get_s3_params(self.fdw_options)

            self.s3_object = self.fdw_options["s3_object"]

    def can_sort(self, sortkeys):
        # Currently, can't sort on anything. In the future, we can infer which
        # columns a CSV is sorted on and return something more useful here.
        return []

    def get_rel_size(self, quals, columns):
        return 1000000, len(columns) * 10

    def explain(self, quals, columns, sortkeys=None, verbose=False):
        if self.mode == "http":
            return ["HTTP request", f"URL: {self.url}"]
        else:
            return [
                "S3 request",
                f"Endpoint: {self.s3_client._base_url}",
                f"Bucket: {self.s3_bucket}",
                f"Object ID: {self.s3_object}",
            ]

    def _read_csv(self, csv_reader, csv_options):
        header_skipped = False
        for row_number, row in enumerate(csv_reader):
            if not header_skipped and csv_options.header:
                header_skipped = True
                continue

            # Ignore empty rows too
            if not row:
                continue

            row = pad_csv_row(row, row_number=row_number, num_cols=self._num_cols)

            # CSVs don't really distinguish NULLs and empty strings well. We know
            # that empty strings should be NULLs when coerced into non-strings but we
            # can't easily access type information here. Do a minor hack and treat
            # all empty strings as NULLs.
            row = [r if r != "" else None for r in row]

            yield row

    def execute(self, quals, columns, sortkeys=None):
        """Main Multicorn entry point."""

        if self.mode == "http":
            with requests.get(
                self.url, stream=True, verify=os.environ.get("SSL_CERT_FILE", True)
            ) as response:
                response.raise_for_status()
                stream = response.raw
                if response.headers.get("Content-Encoding") == "gzip":
                    stream = gzip.GzipFile(fileobj=stream)

                csv_options = self.csv_options
                if csv_options.encoding == "" and not csv_options.autodetect_encoding:
                    csv_options = csv_options._replace(encoding=response.encoding)

                csv_options, reader = make_csv_reader(stream, csv_options)
                yield from self._read_csv(reader, csv_options)
        else:
            minio_response: Optional[HTTPResponse] = None
            try:
                minio_response = self.s3_client.get_object(
                    bucket_name=self.s3_bucket, object_name=self.s3_object
                )
                assert minio_response
                csv_options = self.csv_options
                if csv_options.encoding == "" and not csv_options.autodetect_encoding:
                    csv_options = csv_options._replace(autodetect_encoding=True)
                csv_options, reader = make_csv_reader(minio_response, csv_options)
                yield from self._read_csv(reader, csv_options)
            finally:
                if minio_response:
                    minio_response.close()
                    minio_response.release_conn()

    @classmethod
    def import_schema(cls, schema, srv_options, options, restriction_type, restricts):
        # Implement IMPORT FOREIGN SCHEMA to instead scan an S3 bucket for CSV files
        # and infer their CSV schema.

        # 1) if we don't have options["table_options"], do a full scan as normal,
        #    treat LIMIT TO as a list of S3 objects
        # 2) if we do, go through these tables and treat each one as a partial override
        #    of server options
        if "table_options" in options:
            table_options = json.loads(options["table_options"])
        else:
            table_options = None
        srv_options = load_options(srv_options)

        if not table_options:
            # Do a full scan of the file at URL / S3 bucket w. prefix
            if srv_options.get("url"):
                # Infer from HTTP -- singular table with name "data"
                result = cls._introspect_url(srv_options, srv_options["url"])
                if result:
                    return [result]
                return []
            else:
                # Get S3 options
                client, bucket, prefix = get_s3_params(srv_options)

                # Note that we ignore the "schema" here (e.g. IMPORT FOREIGN SCHEMA some_schema)
                # and take all interesting parameters through FDW options.

                # Allow just introspecting one object
                if "s3_object" in srv_options:
                    objects = [srv_options["s3_object"]]
                elif restriction_type == "limit":
                    objects = restricts
                else:
                    objects = [
                        o.object_name
                        for o in client.list_objects(
                            bucket_name=bucket, prefix=prefix or None, recursive=True
                        )
                    ]

                result = []

                for o in objects:
                    if restriction_type == "except" and o in restricts:
                        continue
                    result.append(cls._introspect_s3(client, bucket, o, srv_options))

                return [r for r in result if r]
        else:
            result = []

            # Note we ignore LIMIT/EXCEPT here. There's no point in using them if the user
            # is passing a dict of table options anyway.
            for table_name, this_table_options in table_options.items():
                if "s3_object" in this_table_options:
                    # TODO: we can support overriding S3 params per-table here, but currently
                    #   we don't do it.
                    client, bucket, _ = get_s3_params(srv_options)
                    result.append(
                        cls._introspect_s3(
                            client,
                            bucket,
                            this_table_options["s3_object"],
                            srv_options,
                            table_name,
                            this_table_options,
                        )
                    )
                else:
                    result.append(
                        cls._introspect_url(
                            srv_options, this_table_options["url"], table_name, this_table_options
                        )
                    )
            return [r for r in result if r]

    @classmethod
    def _introspect_s3(
        cls, client, bucket, object_id, srv_options, table_name=None, table_options=None
    ) -> Optional[TableDefinition]:
        response = None
        # Default table name: truncate S3 object key up to the prefix
        table_name = table_name or object_id[len(srv_options.get("s3_object_prefix", "")) :]
        table_options = table_options or {}
        table_options.update({"s3_object": object_id})
        with report_errors(table_name):
            try:
                response = client.get_object(bucket, object_id)
                return _get_table_definition(
                    response,
                    srv_options,
                    table_name,
                    table_options,
                )
            finally:
                if response:
                    response.close()
                    response.release_conn()

    @classmethod
    def _introspect_url(
        cls, srv_options, url, table_name=None, table_options=None
    ) -> Optional[TableDefinition]:
        table_name = table_name or "data"
        table_options = table_options or {}

        with report_errors(table_name):
            with requests.get(
                url, stream=True, verify=os.environ.get("SSL_CERT_FILE", True)
            ) as response:
                response.raise_for_status()
                stream = response.raw
                if response.headers.get("Content-Encoding") == "gzip":
                    stream = gzip.GzipFile(fileobj=stream)
                return _get_table_definition(stream, srv_options, table_name, table_options)
