# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Dialect selection and SQL validation for explicit warehouse imports."""

import sqlglot

from ._common import (
    DIALECT_OSSIE_SQL,
    SQL_DIALECT_READERS,
    ConversionError,
)


def validate_source_dialect(source_dialect):
    """Accept only warehouse dialects registered by the Ossie specification."""
    if source_dialect is not None and (
        not isinstance(source_dialect, str) or source_dialect not in SQL_DIALECT_READERS
    ):
        raise ConversionError(
            f"Unsupported source dialect {source_dialect!r}; choose one of "
            f"{', '.join(sorted(SQL_DIALECT_READERS))}"
        )


def validate_imported_expressions(model):
    """Check emitted SQL in its declared dialect, including restored expressions.

    Kept independent of the checkout's validation script so the installed converter
    behaves identically. Non-SQL dialects restored from meta.ossie stay opaque, as
    they do in the core validator. Nothing is transpiled or executed.
    """
    items = [
        (f"field '{ds['name']}.{field['name']}'", field)
        for ds in model.get("datasets", [])
        for field in ds.get("fields", [])
    ]
    items.extend(
        (f"metric '{metric['name']}'", metric) for metric in model.get("metrics", [])
    )
    for scope, item in items:
        for entry in item.get("expression", {}).get("dialects", []):
            dialect = entry["dialect"]
            if dialect not in SQL_DIALECT_READERS and dialect != DIALECT_OSSIE_SQL:
                continue
            sql = entry["expression"]
            reader = SQL_DIALECT_READERS.get(dialect)
            try:
                try:
                    statements = sqlglot.parse(sql, read=reader)
                except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
                    # Match the core validator's retry for bare column expressions.
                    statements = sqlglot.parse(f"SELECT {sql}", read=reader)
                count = sum(statement is not None for statement in statements)
                if count != 1:
                    raise ConversionError(
                        f"{scope} ({dialect}): expected a single SQL expression, "
                        f"found {count} statements"
                    )
                # A single DDL/DML command is valid SQL, but cannot serve as a
                # field or metric expression. Use the scalar-expression entry
                # point after checking statement count (parse_one can return a
                # Block for several statements).
                sqlglot.parse_one(sql, read=reader, into=sqlglot.exp.Condition)
            except (sqlglot.errors.SqlglotError, RecursionError) as exc:
                raise ConversionError(
                    f"{scope} ({dialect}): invalid SQL expression: "
                    f"{str(exc).splitlines()[0]}"
                ) from None
