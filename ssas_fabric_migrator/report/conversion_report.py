"""
Conversion report generator.

Consumes the JSON IR produced by extractor.amo_client and the Direct Lake
feasibility report produced by model.feasibility, and writes a single
human-readable Markdown report (MIGRATION_REPORT.md) that answers, for a
given cube, two questions:

  1. What did this tool convert automatically? (tables/Delta mapping,
     dimensions, hierarchies, measures, relationships)
  2. What did it NOT convert, and what should the user do instead?
     - Constructs the extractor DID find but that need manual translation
       (calculated members, KPIs, semi-additive measures, many-to-many
       relationships, custom-SQL/ROLAP partitions, suspected parent-child
       hierarchies) - sourced from the feasibility report's findings.
     - Constructs the extractor has NO visibility into at all (Roles/RLS,
       Actions, Perspectives, Translations, custom rollup/unary operators,
       write-back, MDX SCOPE assignments) - a static catalogue, since these
       must be located by the user in the source SSAS project/database.

This module is intentionally dependency-free (stdlib only) so it can run in
either phase (on-prem, right after extraction/feasibility, or later).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constructs the AMO extractor never reads, regardless of the cube. These are
# not derived from the IR - there is no field for them - so the user must
# check the source cube directly (SSMS Object Explorer / SSDT project /
# Visual Studio cube designer tabs) for each one.
# ---------------------------------------------------------------------------
UNSUPPORTED_CONSTRUCTS = [
    {
        "name": "Dimension/Cube Roles (Row-Level Security)",
        "why": "AMO Role objects (MDX Allowed/Denied Member Set permissions) are not read by the extractor.",
        "alternative": (
            "Recreate as Power BI/Fabric semantic model Roles with DAX row filters "
            "(Model > Manage Roles). Map each MDX allowed/denied member set permission to an "
            "equivalent DAX filter expression on the corresponding table, then assign users/"
            "groups to the role in the Fabric workspace or via the XMLA endpoint."
        ),
    },
    {
        "name": "Actions (Drillthrough, Reporting, Standard)",
        "why": "Cube Action objects are not extracted.",
        "alternative": (
            "Recreate drillthrough behavior using Power BI's built-in drillthrough pages, or "
            "set the 'Detail Rows Expression' property on the relevant measures/table in the "
            "semantic model."
        ),
    },
    {
        "name": "Perspectives",
        "why": "Cube Perspective objects are not extracted.",
        "alternative": (
            "Recreate as Power BI Desktop Perspectives (Modeling tab, via Tabular Editor or "
            "the external tools ecosystem), or publish separate reports/apps scoped to the "
            "relevant tables and measures."
        ),
    },
    {
        "name": "Translations",
        "why": "Caption/Translation objects on the cube, dimensions, attributes and measures are not extracted.",
        "alternative": (
            "Recreate using Tabular Object Model translations (Culture objects) via Tabular "
            "Editor's Translations feature, or the semantic model's XMLA/TMSL Culture objects."
        ),
    },
    {
        "name": "Custom Rollup Formulas / Unary Operators (parent-child dimensions)",
        "why": "AMO CustomRollupColumn / UnaryOperatorColumn bindings on parent-child attributes are not extracted.",
        "alternative": (
            "Precompute the rollup in the Lakehouse (materialize a path/aggregation column via "
            "a notebook or dataflow), or hand-write an equivalent DAX measure using CALCULATE "
            "with a PATH()/PATHITEM()-based filter that reproduces the unary-operator logic."
        ),
    },
    {
        "name": "Write-back Enabled Partitions/Dimensions",
        "why": (
            "Write-enabled measure groups are only flagged indirectly (as a non-MOLAP partition "
            "storage mode in the feasibility report); the extractor does not read the "
            "WriteEnabled property directly."
        ),
        "alternative": (
            "Fabric semantic models do not support write-back natively. Implement a companion "
            "write-back pattern (e.g. a Power App writing to a separate Lakehouse/Warehouse "
            "table, joined back into the model), or keep write-back workloads on the source "
            "system and migrate only the read path."
        ),
    },
    {
        "name": "Calculation Groups / Scoped MDX Assignments (SCOPE statements)",
        "why": (
            "Only Calculated Member and KPI objects are extracted; arbitrary SCOPE(...) ... "
            "END SCOPE assignments in the cube's MDX script are not parsed."
        ),
        "alternative": (
            "Open the cube's MDX Script tab in SSMS/SSDT, identify each SCOPE assignment, and "
            "hand-translate it to an equivalent DAX calculation group or calculated measure in "
            "the semantic model."
        ),
    },
]

# Keyword -> extra alternative-conversion guidance, matched against each
# feasibility finding's scope/message so the report can give more specific
# advice than the raw finding text already contains.
_ALTERNATIVE_BY_KEYWORD = [
    (
        "calculated_member",
        "Open the MDX expression in SSMS/SSDT (Calculations tab) and hand-translate it to an "
        "equivalent DAX measure. Simple arithmetic/aggregation expressions usually translate "
        "directly; MDX functions with no direct DAX equivalent (e.g. set-based navigation "
        "functions) need a rethought DAX pattern.",
    ),
    (
        "kpi",
        "Recreate the KPI's Goal/Status/Trend expressions as DAX measures, then define a KPI "
        "in the semantic model (or in Power BI Desktop) pointing the base measure at the "
        "Goal/Status/Trend measures.",
    ),
    (
        "semi-additive",
        "Write a DAX measure using CALCULATE with time-intelligence functions (e.g. "
        "CLOSINGBALANCEMONTH/OPENINGBALANCEMONTH for LastChild/FirstChild-style semantics, or "
        "AVERAGEX over the relevant grain for AverageOfChildren) to reproduce the semi-additive "
        "aggregation.",
    ),
    (
        "many-to-many",
        "Materialize the bridge table as its own Delta table in the Lakehouse and add an "
        "explicit many-to-many relationship between the fact table and the far-side dimension "
        "via the bridge table in the semantic model.",
    ),
    (
        "parent_child_attribute",
        "This is a genuine Usage=Parent attribute (not just a suspected single-level "
        "hierarchy). Precompute a materialized path (e.g. a delimited ancestor path or a "
        "fixed set of level columns for the known depth) in the Lakehouse table via a "
        "notebook/dataflow, and model it either as a fixed-depth hierarchy or with a DAX "
        "PATH()/PATHITEM() calculated column pattern (Import mode only - Direct Lake does "
        "not support calculated columns).",
    ),
    (
        "single-level hierarchy",
        "If this is confirmed to be a parent-child hierarchy (self-referencing key column), "
        "precompute a materialized path (e.g. a delimited ancestor path or level columns) in "
        "the Lakehouse table and model it as a fixed-depth hierarchy, or use a DAX "
        "PATH()/PATHITEM() calculated column pattern (not supported in Direct Lake - requires "
        "Import mode or Lakehouse-side materialization).",
    ),
    (
        "custom sql query",
        "Reproduce the partition's custom SQL query logic as a Lakehouse notebook/dataflow "
        "transformation (or a Warehouse view) so the resulting Delta table already reflects "
        "the same row-level logic, then point the semantic model's Direct Lake partition at "
        "that materialized table.",
    ),
    (
        "not a simple molap",
        "This measure group's partition is ROLAP or write-enabled; design an explicit ETL/"
        "pipeline step to materialize a queryable snapshot into a Delta table, or keep this "
        "measure group on Import mode with a scheduled refresh replicating the source query.",
    ),
    (
        "no granularity attribute",
        "Fix the measure group dimension relationship in the source cube (assign a granularity "
        "attribute) before re-running the extractor, or manually add the relationship key in "
        "the generated TMDL's relationships.tmdl.",
    ),
]


def _alternative_for(finding):
    text = (finding.get("scope", "") + " " + finding.get("message", "")).lower()
    for keyword, guidance in _ALTERNATIVE_BY_KEYWORD:
        if keyword in text:
            return guidance
    return "Review this finding manually; no automated alternative guidance is available for it."


def _count_ir_objects(ir):
    dims = ir.get("dimensions", [])
    counts = {
        "dimensions": len(dims),
        "hierarchies": sum(len(d.get("hierarchies", [])) for d in dims),
        "attributes": sum(len(d.get("attributes", [])) for d in dims),
        "tables": sum(len(dsv.get("tables", [])) for dsv in ir.get("data_source_views", [])),
        "relationships": sum(len(dsv.get("relations", [])) for dsv in ir.get("data_source_views", [])),
    }
    cubes = ir.get("cubes", [])
    counts["cubes"] = len(cubes)
    counts["measures"] = sum(
        len(mg.get("measures", [])) for c in cubes for mg in c.get("measure_groups", [])
    )
    counts["measure_groups"] = sum(len(c.get("measure_groups", [])) for c in cubes)
    counts["calculated_members"] = sum(len(c.get("calculated_members", [])) for c in cubes)
    counts["kpis"] = sum(len(c.get("kpis", [])) for c in cubes)
    return counts


def _md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        cells = [str(c).replace("\n", " ").replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def generate_report(ir, feasibility_report, output_path):
    counts = _count_ir_objects(ir)
    cube_feas_by_name = {cf["cube_name"]: cf for cf in feasibility_report.get("cubes", [])}

    lines = []
    lines.append("# Migration Conversion Report")
    lines.append("")
    lines.append(f"- **Source server:** {ir.get('server')}")
    lines.append(f"- **Source database:** {ir.get('database')}")
    lines.append(f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("")
    lines.append(
        "This report is generated automatically from the extracted cube metadata and the "
        "Direct Lake feasibility analysis. It is the authoritative summary of what this tool "
        "converted, what it flagged for manual review, and what it never had visibility into "
        "at all. Review every item in sections 3 and 4 before considering the migration complete."
    )
    lines.append("")

    # --- Section 1: summary counts -----------------------------------------
    lines.append("## 1. Summary")
    lines.append("")
    summary_rows = [
        ("Cubes", counts["cubes"]),
        ("Dimensions", counts["dimensions"]),
        ("Dimension attributes", counts["attributes"]),
        ("Hierarchies", counts["hierarchies"]),
        ("Relational tables (DSV)", counts["tables"]),
        ("Table relationships (DSV)", counts["relationships"]),
        ("Measure groups", counts["measure_groups"]),
        ("Measures", counts["measures"]),
        ("Calculated members (found, need manual DAX translation)", counts["calculated_members"]),
        ("KPIs (found, need manual DAX translation)", counts["kpis"]),
    ]
    lines.append(_md_table(["Object", "Count"], summary_rows))
    lines.append("")

    total_findings = 0
    total_blocking = 0
    for cf in feasibility_report.get("cubes", []):
        total_findings += len(cf.get("findings", []))
        total_blocking += sum(1 for f in cf.get("findings", []) if f.get("severity") == "blocking")
    lines.append(
        f"- **Feasibility findings raised:** {total_findings} (of which {total_blocking} are blocking for Direct Lake)"
    )
    lines.append(f"- **Constructs never captured by this tool (see section 4):** {len(UNSUPPORTED_CONSTRUCTS)} categories")
    lines.append("")

    # --- Section 2: converted automatically --------------------------------
    lines.append("## 2. Converted Automatically")
    lines.append("")
    lines.append(
        "These objects were read from the source cube and translated into the generated TMDL "
        "semantic model / Delta table mapping without any manual input."
    )
    lines.append("")
    lines.append("### 2.1 Tables mapped to Delta tables")
    lines.append("")
    table_rows = []
    for dsv in ir.get("data_source_views", []):
        for t in dsv.get("tables", []):
            table_rows.append((t["name"], len(t.get("columns", []))))
    if table_rows:
        lines.append(_md_table(["Source table", "Column count"], table_rows))
    else:
        lines.append("_No tables found in the data source view._")
    lines.append("")

    lines.append("### 2.2 Dimensions & hierarchies")
    lines.append("")
    dim_rows = []
    for d in ir.get("dimensions", []):
        hier_names = ", ".join(h["name"] for h in d.get("hierarchies", [])) or "(none)"
        dim_rows.append((d["name"], len(d.get("attributes", [])), hier_names))
    if dim_rows:
        lines.append(_md_table(["Dimension", "Attribute count", "Hierarchies"], dim_rows))
    else:
        lines.append("_No dimensions found._")
    lines.append("")

    lines.append("### 2.3 Measures")
    lines.append("")
    measure_rows = []
    for cube in ir.get("cubes", []):
        for mg in cube.get("measure_groups", []):
            for m in mg.get("measures", []):
                measure_rows.append((cube["name"], mg["name"], m["name"], m.get("aggregate_function")))
    if measure_rows:
        lines.append(_md_table(["Cube", "Measure group", "Measure", "Aggregation"], measure_rows))
    else:
        lines.append("_No measures found._")
    lines.append("")

    lines.append("### 2.4 Relationships (fact-to-dimension)")
    lines.append("")
    rel_rows = []
    for cube in ir.get("cubes", []):
        for mg in cube.get("measure_groups", []):
            for mgd in mg.get("dimensions", []):
                rel_rows.append((cube["name"], mg["name"], mgd.get("dimension_id"), mgd.get("cardinality") or "One"))
    if rel_rows:
        lines.append(_md_table(["Cube", "Measure group", "Dimension", "Cardinality"], rel_rows))
    else:
        lines.append("_No measure group dimension relationships found._")
    lines.append("")

    # --- Section 3: flagged findings needing manual review ------------------
    lines.append("## 3. Flagged for Manual Review (Found, but Not Fully Automated)")
    lines.append("")
    lines.append(
        "These constructs WERE detected by the extractor/feasibility analyzer but require "
        "manual translation or decisions before the migration can be considered complete."
    )
    lines.append("")
    any_findings = False
    for cube in ir.get("cubes", []):
        cf = cube_feas_by_name.get(cube["name"])
        if not cf:
            continue
        findings = [f for f in cf.get("findings", []) if f.get("severity") in ("blocking", "warning")]
        if not findings:
            continue
        any_findings = True
        lines.append(f"### Cube: {cube['name']} (recommended mode: {cf.get('recommended_mode')})")
        lines.append("")
        rows = []
        for f in findings:
            rows.append((f["severity"].upper(), f["scope"], f["message"], _alternative_for(f)))
        lines.append(_md_table(["Severity", "Item", "Finding", "Suggested alternative"], rows))
        lines.append("")
    if not any_findings:
        lines.append("_No blocking or warning findings were raised for any cube._")
        lines.append("")

    # --- Section 4: never captured -------------------------------------------
    lines.append("## 4. Not Captured by This Tool At All (Manual Source Review Required)")
    lines.append("")
    lines.append(
        "The extractor has no code path that reads these SSAS constructs, so they will be "
        "silently absent from the generated semantic model even if the source cube uses them. "
        "You must check the source cube directly (SSMS Object Explorer, the SSDT project, or "
        "the cube designer's Roles/Actions/Perspectives/Translations/Calculations tabs) to "
        "determine whether each of these applies to your cube."
    )
    lines.append("")
    rows = [(c["name"], c["why"], c["alternative"]) for c in UNSUPPORTED_CONSTRUCTS]
    lines.append(_md_table(["Construct", "Why it's missing", "Suggested alternative"], rows))
    lines.append("")

    # --- Section 5: checklist -------------------------------------------------
    lines.append("## 5. Next Steps Checklist")
    lines.append("")
    lines.append("- [ ] Review every row in Section 3 and hand-translate/implement as needed.")
    lines.append("- [ ] Manually inspect the source cube for every construct in Section 4 and act on any that apply.")
    lines.append("- [ ] Validate row counts and spot-check measure totals in the deployed semantic model against the source cube.")
    lines.append("- [ ] Confirm the recommended mode (Direct Lake vs Import) matches your performance/freshness requirements.")
    lines.append("- [ ] Re-run this report after any manual fixes to confirm no new blocking findings were introduced.")
    lines.append("")

    content = "\n".join(lines)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    return output_path


def generate_from_files(metadata_path, feasibility_path, output_path):
    with open(metadata_path, "r", encoding="utf-8") as f:
        ir = json.load(f)
    with open(feasibility_path, "r", encoding="utf-8") as f:
        feasibility_report = json.load(f)
    return generate_report(ir, feasibility_report, output_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate a Markdown conversion report from extracted cube metadata + feasibility report")
    parser.add_argument("--metadata", default="output/cube_metadata.json")
    parser.add_argument("--feasibility", default="output/feasibility_report.json")
    parser.add_argument("--output", default="output/MIGRATION_REPORT.md")
    args = parser.parse_args()

    path = generate_from_files(args.metadata, args.feasibility, args.output)
    print(f"Conversion report written to {path}")
