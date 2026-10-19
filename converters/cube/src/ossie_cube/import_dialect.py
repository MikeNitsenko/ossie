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
    DIALECT_ANSI,
    DIALECT_OSSIE_SQL,
    SQL_DIALECT_READERS,
    ConversionError,
)

# Portable Ossie SQL uses the generic ANSI reader when restored from metadata.
# Keep it separate from the warehouse dialects accepted as source_dialect.
_EXPRESSION_DIALECT_READERS = {
    **SQL_DIALECT_READERS,
    DIALECT_OSSIE_SQL: SQL_DIALECT_READERS[DIALECT_ANSI],
}


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
    resolved_dialects = {}
    for scope, item in items:
        for entry in item.get("expression", {}).get("dialects", []):
            dialect = entry["dialect"]
            if dialect not in _EXPRESSION_DIALECT_READERS:
                continue
            sql = entry["expression"]
            reader = _EXPRESSION_DIALECT_READERS[dialect]
            try:
                if reader not in resolved_dialects:
                    resolved_dialects[reader] = sqlglot.Dialect.get_or_raise(reader)
                # Condition is SQLGlot's scalar grammar entry point, including
                # arithmetic, literals, functions and bare column references.
                # Parse once in that context: a statement parser can misread a
                # column named `update`, while wrapping in SELECT could accept
                # aliases or FROM clauses that are not scalar expressions.
                # Keep the result list so multiple expressions cannot hide in
                # the Block that parse_one returns for multiple statements.
                statements = resolved_dialects[reader].parse_into(
                    sqlglot.exp.Condition, sql
                )
                # Empty statement slots also matter: filtering them out would
                # accept malformed fragments such as `; amount` or `+ ; amount`.
                if len(statements) != 1 or statements[0] is None:
                    raise ConversionError(
                        f"{scope} ({dialect}): expected a single SQL expression"
                    )
            except (sqlglot.errors.SqlglotError, RecursionError) as exc:
                raise ConversionError(
                    f"{scope} ({dialect}): invalid SQL expression: "
                    f"{str(exc).splitlines()[0]}"
                ) from None
