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

"""Warehouse SQL attribution, validation and CLI regressions."""

import pytest

from _cube_gate import assert_cube_compiles, assert_ossie_is_valid, cube_gate
from _util import parse
from ossie_cube import (
    ConversionError,
    convert_cube_to_ossie,
    convert_cube_view_to_ossie,
    convert_ossie_to_cube,
)
from ossie_cube._common import dump_yaml
from ossie_cube.cli import main


def _files(sql="amount", dimension_sql="id"):
    return {
        "model.yml": dump_yaml(
            {
                "cubes": [
                    {
                        "name": "orders",
                        "sql_table": "db.public.orders",
                        "dimensions": [
                            {
                                "name": "id",
                                "type": "number",
                                "sql": dimension_sql,
                                "primary_key": True,
                            }
                        ],
                        "measures": [{"name": "value", "type": "max", "sql": sql}],
                    }
                ],
                "views": [
                    {
                        "name": "sales",
                        "cubes": [{"join_path": "orders", "includes": ["id", "value"]}],
                    }
                ],
            }
        )
    }


def _import(files, project, **kwargs):
    if project:
        return convert_cube_view_to_ossie(files, "sales", **kwargs)[0]
    return convert_cube_to_ossie(files, **kwargs)[0]


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    ("dialect", "sql", "dimension", "expected"),
    [
        ("ANSI_SQL", "amount + 1", "id", "MAX(orders.amount + 1)"),
        (
            "BIGQUERY",
            "SAFE_DIVIDE(`gross value`, quantity)",
            "{CUBE}.`order-id`",
            "MAX(SAFE_DIVIDE(orders.`gross value`, orders.quantity))",
        ),
        (
            "DATABRICKS",
            "DATEDIFF(day, started_at, ended_at)",
            "{CUBE}.`order-id`",
            "MAX(DATEDIFF(day, orders.started_at, orders.ended_at))",
        ),
        (
            "SNOWFLAKE",
            "DATEDIFF(day, started_at, ended_at)",
            '{CUBE}."order-id"',
            "MAX(DATEDIFF(day, orders.started_at, orders.ended_at))",
        ),
    ],
)
def test_native_sql_is_qualified_tagged_and_accepted_by_core_validator(
    project, dialect, sql, dimension, expected
):
    out = _import(_files(sql, dimension), project, source_dialect=dialect)
    assert_ossie_is_valid(out, dialect)
    model = parse(out)
    metric = model["metrics"][0]["expression"]["dialects"][0]
    assert metric == {"dialect": dialect, "expression": expected}
    assert (
        model["datasets"][0]["fields"][0]["expression"]["dialects"][0]["dialect"]
        == dialect
    )
    assert "orders.day" not in metric["expression"]


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("dialect", ["ANSI_SQL", "BIGQUERY", "DATABRICKS", "SNOWFLAKE"])
def test_dialect_survives_export_and_reimport(project, dialect):
    out = _import(_files(), project, source_dialect=dialect)
    cube, _ = convert_ossie_to_cube(out)
    restored, _ = convert_cube_to_ossie(cube, source_dialect=dialect)
    assert_ossie_is_valid(restored, "reimport")
    assert (
        parse(restored)["metrics"][0]["expression"]
        == parse(out)["metrics"][0]["expression"]
    )


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    "dialect", ["MDX", "DAX", "HOLISTICS_AQL", "mysql", "", 1, [], {}]
)
def test_only_spec_warehouse_dialects_are_accepted(project, dialect):
    with pytest.raises(ConversionError, match="Unsupported source dialect"):
        _import(_files(), project, source_dialect=dialect)


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    "sql", ["SUM(", "id; DROP TABLE orders", "", "-- comment only", ";;"]
)
def test_explicit_import_refuses_invalid_field_sql(project, sql):
    with pytest.raises(ConversionError):
        _import(_files(dimension_sql=sql), project, source_dialect="BIGQUERY")


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    "sql", ["SUM(", "amount; DROP TABLE orders", "", "-- comment only"]
)
def test_explicit_import_refuses_invalid_measure_sql(project, sql):
    with pytest.raises(ConversionError):
        _import(_files(sql=sql), project, source_dialect="BIGQUERY")


def test_omitting_dialect_retains_legacy_behavior_but_explicit_ansi_validates():
    files = _files(dimension_sql="{CUBE}.`order-id`")
    out = _import(files, False)
    assert (
        parse(out)["datasets"][0]["fields"][0]["expression"]["dialects"][0]["dialect"]
        == "ANSI_SQL"
    )
    with pytest.raises(ConversionError, match="orders.id.*ANSI_SQL"):
        _import(files, False, source_dialect="ANSI_SQL")


def test_restored_ossie_expression_keeps_its_original_dialects():
    model = {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "db.public.orders",
                "fields": [
                    {
                        "name": "id",
                        "datatype": "Integer",
                        "expression": {
                            "dialects": [
                                {"dialect": "OSSIE_SQL_2026", "expression": "id"},
                                {"dialect": "SNOWFLAKE", "expression": "id"},
                            ]
                        },
                    }
                ],
            }
        ],
    }
    files, _ = convert_ossie_to_cube(dump_yaml(model), dialect="SNOWFLAKE")
    out = _import(files, False, source_dialect="SNOWFLAKE")
    assert (
        parse(out)["datasets"][0]["fields"][0]["expression"]
        == model["datasets"][0]["fields"][0]["expression"]
    )
    assert_ossie_is_valid(out)


@pytest.mark.parametrize("project", [False, True])
def test_cli_writes_valid_output(tmp_path, project):
    source = tmp_path / "cube.yml"
    source.write_text(_files("`amount value`", "{CUBE}.`order-id`")["model.yml"])
    dest = tmp_path / "ossie.yml"
    args = [
        "import",
        "-i",
        str(source),
        "-o",
        str(dest),
        "--source-dialect",
        "BIGQUERY",
    ]
    if project:
        args += ["--project", "--view", "sales"]
    assert main(args) == 0
    assert_ossie_is_valid(dest.read_text())
    assert (
        parse(dest.read_text())["metrics"][0]["expression"]["dialects"][0]["dialect"]
        == "BIGQUERY"
    )


def test_cli_validation_failure_preserves_existing_output(tmp_path, capsys):
    source = tmp_path / "cube.yml"
    source.write_text(_files(dimension_sql="SUM(")["model.yml"])
    dest = tmp_path / "ossie.yml"
    dest.write_text("existing output")
    assert (
        main(
            [
                "import",
                "-i",
                str(source),
                "-o",
                str(dest),
                "--source-dialect",
                "BIGQUERY",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "orders.id" in captured.err and "BIGQUERY" in captured.err
    assert not captured.out
    assert dest.read_text() == "existing output"


def test_cli_rejects_a_non_warehouse_dialect(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["import", "-i", str(tmp_path), "--source-dialect", "DAX"])
    assert exc.value.code == 2


def test_case_geo_and_count_use_the_selected_dialect():
    files = _files()
    model = parse(files["model.yml"])
    cube = model["cubes"][0]
    cube["dimensions"] += [
        {
            "name": "category",
            "type": "string",
            "case": {
                "when": [{"sql": "{CUBE}.`gross value` > 0", "label": "positive"}],
                "else": {"label": "other"},
            },
        },
        {
            "name": "location",
            "type": "geo",
            "latitude": {"sql": "{CUBE}.`lat value`"},
            "longitude": {"sql": "{CUBE}.`lon value`"},
        },
    ]
    cube["measures"].append({"name": "count", "type": "count"})
    out = _import({"model.yml": dump_yaml(model)}, False, source_dialect="BIGQUERY")
    assert_ossie_is_valid(out)
    doc = parse(out)
    fields = {field["name"]: field for field in doc["datasets"][0]["fields"]}
    assert {"category", "location_latitude", "location_longitude"} <= fields.keys()
    for item in [*fields.values(), *doc["metrics"]]:
        assert item["expression"]["dialects"][0]["dialect"] == "BIGQUERY"


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(("kind", "unsafe"), [("max", False), ("sum", True)])
def test_fanout_checks_read_native_quoted_columns(project, kind, unsafe):
    model = parse(_files()["model.yml"])
    orders = model["cubes"][0]
    orders["joins"] = [
        {
            "name": "users",
            "relationship": "many_to_one",
            "sql": "{CUBE}.user_id = {users}.id",
        }
    ]
    model["cubes"].append(
        {
            "name": "users",
            "sql_table": "db.public.users",
            "dimensions": [
                {"name": "id", "type": "number", "sql": "id", "primary_key": True}
            ],
            "measures": [{"name": "credit", "type": kind, "sql": "`credit score`"}],
        }
    )
    model["views"][0]["cubes"].append(
        {"join_path": "orders.users", "includes": ["credit"]}
    )
    files = {"model.yml": dump_yaml(model)}
    if unsafe:
        with pytest.raises(ConversionError, match="FANOUT_UNSAFE_METRIC"):
            _import(files, project, source_dialect="BIGQUERY", strict_fanout=True)
    else:
        out = _import(files, project, source_dialect="BIGQUERY", strict_fanout=True)
        assert_ossie_is_valid(out)
        assert "MAX(users.`credit score`)" in out


def test_projection_does_not_validate_unpublished_invalid_sql():
    model = parse(_files()["model.yml"])
    model["cubes"][0]["dimensions"].append(
        {"name": "private", "sql": "SUM(", "type": "number"}
    )
    out = _import({"model.yml": dump_yaml(model)}, True, source_dialect="BIGQUERY")
    assert_ossie_is_valid(out)
    assert "private" not in out


@cube_gate
@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("dialect", ["BIGQUERY", "DATABRICKS", "SNOWFLAKE"])
def test_native_import_exports_to_a_model_cube_compiles(project, dialect):
    sql = '"amount value"' if dialect == "SNOWFLAKE" else "`amount value`"
    out = _import(_files(sql), project, source_dialect=dialect)
    files, _ = convert_ossie_to_cube(out)
    assert_cube_compiles(files, f"{dialect} import (project={project})")


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "DATE_DIFF(ended_at, started_at, DAY)",
            "MAX(DATE_DIFF(orders.ended_at, orders.started_at, DAY))",
        ),
        (
            "ARRAY_LENGTH([amount, tax])",
            "MAX(ARRAY_LENGTH([orders.amount, orders.tax]))",
        ),
    ],
)
def test_bigquery_grammar_controls_units_and_array_literals(project, sql, expected):
    out = _import(_files(sql), project, source_dialect="BIGQUERY")
    assert_ossie_is_valid(out)
    assert (
        parse(out)["metrics"][0]["expression"]["dialects"][0]["expression"] == expected
    )


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("amount # trailing note", "MAX(orders.amount)"),
        ("amount /* closed */", "MAX(orders.amount)"),
        (
            "ARRAY_LENGTH([amount /* note */, tax])",
            "MAX(ARRAY_LENGTH([orders.amount              , orders.tax]))",
        ),
        ("`orders.amount`", "MAX(orders.`amount`)"),
        ("`payload.amount`", "MAX(orders.`payload`.`amount`)"),
        ("R'''literal # text'''", "MAX(R'''literal # text''')"),
    ],
)
def test_bigquery_comments_literals_and_quoted_paths(project, sql, expected):
    out = _import(_files(sql), project, source_dialect="BIGQUERY")
    assert_ossie_is_valid(out)
    actual = parse(out)["metrics"][0]["expression"]["dialects"][0]["expression"]
    assert " ".join(actual.split()) == " ".join(expected.split())


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("dialect", ["ANSI_SQL", "BIGQUERY", "DATABRICKS", "SNOWFLAKE"])
def test_unterminated_source_comment_cannot_be_normalized_away(project, dialect):
    with pytest.raises(ConversionError):
        _import(_files("amount /* unclosed"), project, source_dialect=dialect)


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("sql", ["DROP TABLE orders", "SET x = 1", "SELECT 1"])
def test_sql_commands_are_not_scalar_field_expressions(project, sql):
    with pytest.raises(ConversionError, match="orders.id.*BIGQUERY"):
        _import(_files(dimension_sql=sql), project, source_dialect="BIGQUERY")


@pytest.mark.parametrize("project", [False, True])
def test_struct_metric_is_attributed_to_its_dataset_for_fanout(project):
    model = parse(_files()["model.yml"])
    model["cubes"][0]["joins"] = [
        {
            "name": "users",
            "relationship": "many_to_one",
            "sql": "{CUBE}.user_id = {users}.id",
        }
    ]
    model["cubes"].append(
        {
            "name": "users",
            "sql_table": "db.public.users",
            "dimensions": [
                {"name": "id", "sql": "id", "type": "number", "primary_key": True}
            ],
            "measures": [{"name": "credit", "type": "sum", "sql": "payload.balance"}],
        }
    )
    model["views"][0]["cubes"].append(
        {"join_path": "orders.users", "includes": ["credit"]}
    )
    with pytest.raises(ConversionError, match="FANOUT_UNSAFE_METRIC"):
        _import(
            {"m.yml": dump_yaml(model)},
            project,
            source_dialect="BIGQUERY",
            strict_fanout=True,
        )


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize(
    ("dialect", "cast_type"),
    [
        ("BIGQUERY", "STRING"),
        ("DATABRICKS", "STRING"),
        ("SNOWFLAKE", "VARCHAR"),
        ("ANSI_SQL", "VARCHAR"),
    ],
)
def test_composite_count_uses_a_warehouse_string_type(project, dialect, cast_type):
    model = parse(_files()["model.yml"])
    model["cubes"][0]["dimensions"].append(
        {"name": "tenant", "type": "string", "sql": "tenant", "primary_key": True}
    )
    model["cubes"][0]["measures"] = [{"name": "value", "type": "count"}]
    out = _import({"m.yml": dump_yaml(model)}, project, source_dialect=dialect)
    assert_ossie_is_valid(out)
    sql = parse(out)["metrics"][0]["expression"]["dialects"][0]["expression"]
    assert sql == (
        f"COUNT(DISTINCT CONCAT(CAST(orders.id AS {cast_type}), "
        f"CAST(orders.tenant AS {cast_type})))"
    )
    files, _ = convert_ossie_to_cube(out)
    restored = _import(files, False, source_dialect=dialect)
    assert (
        parse(restored)["metrics"][0]["expression"]
        == parse(out)["metrics"][0]["expression"]
    )
    assert_ossie_is_valid(restored)
    restored_default, _ = convert_cube_to_ossie(files)
    assert (
        parse(restored_default)["metrics"][0]["expression"]
        == parse(out)["metrics"][0]["expression"]
    )


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("kind", ["dimension", "measure"])
@pytest.mark.parametrize("alias", ["CUBE", "TABLE", "orders"])
def test_bigquery_fully_quoted_masked_alias(project, kind, alias):
    sql = f"`{{{alias}}}.payload.amount`"
    files = _files(sql=sql) if kind == "measure" else _files(dimension_sql=sql)
    out = _import(files, project, source_dialect="BIGQUERY")
    assert_ossie_is_valid(out)
    model = parse(out)
    item = (
        model["metrics"][0] if kind == "measure" else model["datasets"][0]["fields"][0]
    )
    expected = (
        "MAX(orders.`payload`.`amount`)"
        if kind == "measure"
        else "`payload`.`amount`"
        if project
        else "`payload.amount`"
    )
    assert item["expression"]["dialects"][0]["expression"] == expected


def _cross_dataset_composite_count(dialect):
    def field(name):
        return {
            "name": name,
            "datatype": "Integer",
            "expression": {"dialects": [{"dialect": dialect, "expression": name}]},
        }

    expression = (
        "SUM(orders.amount) / COUNT(DISTINCT CONCAT("
        "CAST(users.id AS STRING), CAST(users.tenant AS STRING)))"
    )
    return {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "db.public.orders",
                "primary_key": ["id"],
                "fields": [
                    field("id"),
                    field("amount"),
                    field("user_id"),
                    field("user_tenant"),
                ],
            },
            {
                "name": "users",
                "source": "db.public.users",
                "primary_key": ["id", "tenant"],
                "fields": [field("id"), field("tenant")],
            },
        ],
        "relationships": [
            {
                "name": "orders_users",
                "from": "orders",
                "to": "users",
                "from_columns": ["user_id", "user_tenant"],
                "to_columns": ["id", "tenant"],
            }
        ],
        "metrics": [
            {
                "name": "ratio",
                "expression": {
                    "dialects": [{"dialect": dialect, "expression": expression}]
                },
            }
        ],
    }


@pytest.mark.parametrize("dialect", ["BIGQUERY", "DATABRICKS"])
def test_hidden_parts_keep_their_dialect_on_default_reimport(dialect):
    model = _cross_dataset_composite_count(dialect)
    files, _ = convert_ossie_to_cube(dump_yaml(model))
    users = parse(files["model/cubes/users.yml"])["cubes"][0]
    part = users["measures"][0]
    assert part["type"] == "count"
    restored, _ = convert_cube_to_ossie(files)
    assert_ossie_is_valid(restored)
    assert (
        parse(restored)["metrics"][0]["expression"] == model["metrics"][0]["expression"]
    )
    assert part["meta"]["ossie"]["dialect"] == dialect


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("dialect", ["BIGQUERY", "DATABRICKS"])
def test_reprojected_count_uses_its_parked_dialect(project, dialect):
    model = parse(_files()["model.yml"])
    model["cubes"][0]["dimensions"].append(
        {"name": "tenant", "type": "string", "sql": "tenant", "primary_key": True}
    )
    model["cubes"][0]["measures"] = [{"name": "value", "type": "count"}]
    original = _import({"m.yml": dump_yaml(model)}, project, source_dialect=dialect)
    files, _ = convert_ossie_to_cube(original)
    restored, _, _ = convert_cube_view_to_ossie(files, "sales")
    assert_ossie_is_valid(restored)
    assert (
        parse(restored)["metrics"][0]["expression"]
        == parse(original)["metrics"][0]["expression"]
    )
