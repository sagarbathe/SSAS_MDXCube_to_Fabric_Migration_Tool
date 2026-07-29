"""
TMDL (Tabular Model Definition Language) generator.

Converts the JSON IR (extractor.amo_client output) plus the feasibility
report (model.feasibility output) into a TMDL model folder that can be
submitted to the Fabric "Create semantic model from definition" API, or
opened directly in Power BI Desktop / Tabular Editor.

Design decisions (documented, not silently assumed):
 - One Lakehouse table per DSV table (dimensions + fact). Table/column names
   are kept identical to the source relational names so the Direct Lake
   partition binding ("entity") lines up 1:1 with the Delta table.
 - Attribute hierarchies from the MD dimension become Tabular "hierarchy"
   objects on the corresponding table.
 - Regular (many-to-one) relationships are derived from the DSV foreign
   keys, matched to the measure group's granularity attribute per dimension.
 - Measures are translated from (aggregate_function, source_column) into a
   best-effort DAX expression:
     Sum              -> SUM('Table'[Column])
     Count             -> COUNTROWS('Table')          (row count semantics)
     Min / Max         -> MIN/MAX('Table'[Column])
     DistinctCount     -> DISTINCTCOUNT('Table'[Column])
   Anything else (semi-additive, etc.) is emitted as a DAX measure with a
   TODO comment - these were already flagged as warnings by the feasibility
   analyzer and require manual authoring.
 - MDX calculated members / KPIs are NOT auto-translated to DAX (MDX and DAX
   are not mechanically equivalent); they are emitted as commented-out
   placeholder measures with the original MDX text preserved, requiring
   manual translation. This is intentional - silently guessing a DAX
   translation of arbitrary MDX would risk producing incorrect numbers.
 - When feasibility recommends "Import" for a cube, the generated tables use
   an M/Power Query partition (import mode) against the same Lakehouse SQL
   analytics endpoint, instead of a directLake partition. The reason(s) are
   embedded as a comment block at the top of model.tmdl.
"""
from __future__ import annotations

import json
import os

AMO_TO_TABULAR_TYPE = {
    "System.Int32": "int64",
    "System.Int16": "int64",
    "System.Int64": "int64",
    "System.Double": "double",
    "System.Single": "double",
    "System.Decimal": "decimal",
    "System.String": "string",
    "System.DateTime": "dateTime",
    "System.Boolean": "boolean",
}


def _tabular_type(amo_type):
    return AMO_TO_TABULAR_TYPE.get(amo_type, "string")


def _dax_measure_expression(measure, fact_table_name):
    agg = measure.get("aggregate_function")
    col = measure.get("source_column")
    table = fact_table_name
    if agg == "Sum":
        return "SUM('" + table + "'[" + col + "])"
    if agg == "Count":
        return "COUNTROWS('" + table + "')"
    if agg == "Min":
        return "MIN('" + table + "'[" + col + "])"
    if agg == "Max":
        return "MAX('" + table + "'[" + col + "])"
    if agg == "DistinctCount":
        return "DISTINCTCOUNT('" + table + "'[" + col + "])"
    return (
        "0 /* TODO: manual DAX translation required for aggregate function '"
        + str(agg) + "' on column [" + str(col) + "] */"
    )


def _quote_name(name):
    if all(ch.isalnum() or ch == "_" for ch in name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _table_columns_ir(ir, table_name):
    for dsv in ir.get("data_source_views", []):
        for t in dsv.get("tables", []):
            if t["name"] == table_name:
                return t["columns"]
    return []


def _dsv_relations(ir):
    rels = []
    for dsv in ir.get("data_source_views", []):
        rels.extend(dsv.get("relations", []))
    return rels


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _gen_table_tmdl(table_name, columns, hierarchies=None, measures_tmdl="", mode="DirectLake", physical_table_name=None):
    """
    table_name: logical Tabular table name shown in the model (kept
        identical to the source relational table name).
    physical_table_name: the actual Lakehouse Delta table name this binds
        to - defaults to table_name, but differs when a --table-prefix is
        used (e.g. logical table "Dim_Date" bound to physical Lakehouse
        table "stg_Dim_Date"). Only the entityName/Import source Item=
        binding uses this - the logical table name (and therefore
        relationships/measures referencing it) is unaffected by a prefix.
    """
    physical_table_name = physical_table_name or table_name
    lines = []
    lines.append("table " + _quote_name(table_name))
    lines.append("")
    for col in columns:
        dtype = _tabular_type(col["data_type"])
        lines.append("\tcolumn " + _quote_name(col["name"]))
        lines.append("\t\tdataType: " + dtype)
        lines.append("\t\tsourceColumn: " + col["name"])
        lines.append("")
    if measures_tmdl:
        lines.append(measures_tmdl)
    if hierarchies:
        for h in hierarchies:
            lines.append("\thierarchy " + _quote_name(h["name"]))
            lines.append("")
            for lvl in h["levels"]:
                lines.append("\t\tlevel " + _quote_name(lvl["name"]))
                lines.append("\t\t\tcolumn: " + _quote_name(lvl["source_attribute_name"]))
                lines.append("")
    if mode == "Import":
        # Import mode: an "m" (Power Query) partition pulling from the shared
        # DatabaseQuery expression (Lakehouse SQL analytics endpoint), instead
        # of a directLake "entity" partition. Requires a scheduled/manual
        # refresh to pick up new Lakehouse data.
        lines.append("\tpartition " + _quote_name(table_name) + " = m")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource =")
        lines.append("\t\t\t\tlet")
        lines.append('\t\t\t\t\tSource = DatabaseQuery,')
        lines.append('\t\t\t\t\tdbo_Table = Source{[Schema="dbo",Item="' + physical_table_name + '"]}[Data]')
        lines.append("\t\t\t\tin")
        lines.append("\t\t\t\t\tdbo_Table")
        lines.append("")
    else:
        lines.append("\tpartition " + _quote_name(table_name) + " = entity")
        lines.append("\t\tmode: directLake")
        lines.append("\t\tsource")
        lines.append("\t\t\tentityName: " + physical_table_name)
        lines.append("\t\t\tschemaName: dbo")
        lines.append("\t\t\texpressionSource: DatabaseQuery")
        lines.append("")
    return "\n".join(lines)


def _gen_measures_block(measures, fact_table_name, column_names=None):
    column_names = column_names or set()
    lines = []
    for m in measures:
        expr = _dax_measure_expression(m, fact_table_name)
        measure_name = m["name"]
        if measure_name in column_names:
            # Tabular disallows a measure and column sharing the same name in
            # the same table (this happened with the MD cube's 'Quantity'
            # measure vs. 'Quantity' column) - disambiguate deterministically.
            measure_name = measure_name + " (Measure)"
        lines.append("\tmeasure " + _quote_name(measure_name) + " = " + expr)
        if m.get("format_string"):
            lines.append("\t\tformatString: " + m["format_string"])
        lines.append("")
    return "\n".join(lines)


def _gen_relationships_tmdl(ir, dim_key_columns):
    """dim_key_columns: dict of table_name -> key column name (attribute key)."""
    rels = _dsv_relations(ir)
    lines = []
    for i, rel in enumerate(rels):
        rel_name = "rel_" + str(i) + "_" + rel["child_table"] + "_" + rel["parent_table"]
        lines.append("relationship " + rel_name)
        lines.append("\tfromColumn: " + rel["child_table"] + "." + rel["child_columns"][0])
        lines.append("\ttoColumn: " + rel["parent_table"] + "." + rel["parent_columns"][0])
        lines.append("\tcrossFilteringBehavior: automatic")
        lines.append("")
    return "\n".join(lines)


def generate_tmdl(ir, feasibility_report, output_dir, lakehouse_sql_endpoint=None, table_prefix=""):
    """
    ir: parsed cube_metadata.json dict
    feasibility_report: parsed feasibility_report.json dict
    output_dir: folder to write the TMDL model definition into
    lakehouse_sql_endpoint: connection string / server name of the target
        Lakehouse SQL analytics endpoint (required for the shared M
        expression that Direct Lake / Import partitions bind to). If not
        supplied, a placeholder is emitted with a TODO comment.
    table_prefix: optional prefix applied to the PHYSICAL Lakehouse Delta
        table name each table's partition binds to (entityName for Direct
        Lake, Item= for Import) - must match the prefix used when writing
        the Delta tables (loader.py --table-prefix). The logical Tabular
        table name shown in the model (and therefore relationships,
        measures, hierarchies) is left unprefixed.
    """
    os.makedirs(output_dir, exist_ok=True)

    cube = ir["cubes"][0]  # NOTE: this tool currently emits one semantic model per cube.
    cube_feasibility = None
    for cf in feasibility_report.get("cubes", []):
        if cf["cube_name"] == cube["name"]:
            cube_feasibility = cf
            break
    mode = cube_feasibility["recommended_mode"] if cube_feasibility else "DirectLake"

    mg = cube["measure_groups"][0]  # NOTE: multi-measure-group cubes need one table graph per group; single-MG here.
    fact_table = mg["fact_table"]

    # attribute id -> (dimension source_table, attribute id -> column name) lookups
    dim_by_id = {d["id"]: d for d in ir["dimensions"]}

    tables_dir = os.path.join(output_dir, "definition", "tables")

    # --- Fact table ---
    fact_columns = _table_columns_ir(ir, fact_table)
    measures_block = _gen_measures_block(mg["measures"], fact_table, column_names={c["name"] for c in fact_columns})
    fact_tmdl = _gen_table_tmdl(
        fact_table, fact_columns, measures_tmdl=measures_block, mode=mode,
        physical_table_name=table_prefix + fact_table,
    )
    _write(os.path.join(tables_dir, fact_table + ".tmdl"), fact_tmdl)

    # --- Dimension tables ---
    for mgd in mg["dimensions"]:
        dim = dim_by_id.get(mgd["dimension_id"])
        if dim is None:
            continue
        dim_table = dim["source_table"]
        dim_columns = _table_columns_ir(ir, dim_table)

        # resolve hierarchy level source_attribute_id -> physical column name
        resolved_hierarchies = []
        attr_by_id = {a["id"]: a for a in dim["attributes"]}
        for hier in dim["hierarchies"]:
            resolved_levels = []
            for lvl in hier["levels"]:
                attr = attr_by_id.get(lvl["source_attribute_id"])
                col_name = None
                if attr and attr.get("name_column") and attr["name_column"].get("column"):
                    col_name = attr["name_column"]["column"]
                elif attr and attr.get("key_columns"):
                    col_name = attr["key_columns"][0]["column"]
                resolved_levels.append({"name": lvl["name"], "source_attribute_name": col_name or lvl["name"]})
            resolved_hierarchies.append({"name": hier["name"], "levels": resolved_levels})

        dim_tmdl = _gen_table_tmdl(
            dim_table, dim_columns, hierarchies=resolved_hierarchies, mode=mode,
            physical_table_name=table_prefix + dim_table,
        )
        _write(os.path.join(tables_dir, dim_table + ".tmdl"), dim_tmdl)

    # --- Relationships ---
    rel_tmdl = _gen_relationships_tmdl(ir, {})
    _write(os.path.join(output_dir, "definition", "relationships.tmdl"), rel_tmdl)

    # --- Calculated members / KPIs: emitted as a plain-text placeholder file ---
    # IMPORTANT: this must NOT live under output_dir/definition/ - Fabric's
    # Dataset workload parses every file in that folder as strict TMDL, and
    # free-form "/* ... */" / "//" comment lines with no top-level TMDL
    # object fail with "TMDL Format Error: Unexpected line type: Other".
    # Writing it as a sibling .md file keeps it as a Phase-1 human-readable
    # artifact without breaking semantic model deployment.
    if cube.get("calculated_members") or cube.get("kpis"):
        placeholder_lines = ["# Manual DAX Translation Required", "", "The following MDX constructs were extracted from the source cube and require manual DAX translation before they can be added to the deployed semantic model:", ""]
        for cm in cube.get("calculated_members", []):
            placeholder_lines.append("- Calculated member: " + cm["name"])
            placeholder_lines.append("  - Original MDX: `" + cm["expression"].replace("\n", " ") + "`")
        for kpi in cube.get("kpis", []):
            placeholder_lines.append("- KPI: " + kpi["name"])
        placeholder_path = os.path.join(os.path.dirname(os.path.normpath(output_dir)), "MANUAL_TRANSLATION_REQUIRED.md")
        _write(placeholder_path, "\n".join(placeholder_lines))

    # --- Shared expression (Lakehouse connection) ---
    endpoint = lakehouse_sql_endpoint or "TODO_SET_LAKEHOUSE_SQL_ENDPOINT"
    expr_tmdl = (
        "expression DatabaseQuery =\n"
        "\t\tlet\n"
        "\t\t\tSource = Sql.Database(\"" + endpoint + "\", \"TODO_SET_LAKEHOUSE_NAME\")\n"
        "\t\tin\n"
        "\t\t\tSource\n"
        "\tlineageTag: databaseQuery-expression\n"
        "\tannotation PBI_ResultType = Table\n"
    )
    _write(os.path.join(output_dir, "definition", "expressions.tmdl"), expr_tmdl)

    # --- model.tmdl (top-level) ---
    # NOTE: recommended mode + reasons are recorded separately in
    # feasibility_report.json (not embedded here) - TMDL's parser rejects
    # freestanding "//" comment lines before the first top-level object.
    model_tmdl = (
        "model Model\n"
        + "\tculture: en-US\n"
        + "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        + "\tsourceQueryCulture: en-US\n"
        + "\tdiscourageImplicitMeasures\n"
    )
    _write(os.path.join(output_dir, "definition", "model.tmdl"), model_tmdl)

    # --- database.tmdl ---
    db_tmdl = (
        "database " + cube["name"].replace(" ", "") + "\n"
        + "\tcompatibilityLevel: 1604\n"
    )
    _write(os.path.join(output_dir, "definition", "database.tmdl"), db_tmdl)

    # --- .platform + definition.pbism (required by Fabric item definition API) ---
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": cube["name"]},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    }
    _write(os.path.join(output_dir, ".platform"), json.dumps(platform, indent=2))
    pbism = {"version": "4.0", "settings": {}}
    _write(os.path.join(output_dir, "definition.pbism"), json.dumps(pbism, indent=2))

    return {"output_dir": output_dir, "mode": mode}


def generate_from_files(metadata_path, feasibility_path, output_dir, lakehouse_sql_endpoint=None, table_prefix=""):
    with open(metadata_path, "r", encoding="utf-8") as f:
        ir = json.load(f)
    with open(feasibility_path, "r", encoding="utf-8") as f:
        feasibility_report = json.load(f)
    return generate_tmdl(ir, feasibility_report, output_dir, lakehouse_sql_endpoint, table_prefix=table_prefix)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate a TMDL semantic model folder from extracted cube metadata")
    parser.add_argument("--metadata", default="output/cube_metadata.json")
    parser.add_argument("--feasibility", default="output/feasibility_report.json")
    parser.add_argument("--output", default="output/SemanticModel")
    parser.add_argument("--lakehouse-endpoint", default=None)
    parser.add_argument(
        "--table-prefix", default="",
        help="Optional prefix applied to the physical Lakehouse table name each table's partition "
             "binds to - must match the prefix used with loader.py --table-prefix.",
    )
    args = parser.parse_args()

    result = generate_from_files(args.metadata, args.feasibility, args.output, args.lakehouse_endpoint, args.table_prefix)
    print("Generated TMDL model at " + result["output_dir"] + " (mode: " + result["mode"] + ")")
