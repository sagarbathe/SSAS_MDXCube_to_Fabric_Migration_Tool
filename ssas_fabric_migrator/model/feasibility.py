"""
Direct Lake feasibility analyzer.

Consumes the JSON IR produced by ssas_fabric_migrator.extractor.amo_client and
evaluates the cube against known Microsoft Fabric Direct Lake constraints,
producing a structured report. Any blocking finding causes the recommended
target mode for the affected cube to fall back to Import, with the specific
reason(s) recorded so they can be surfaced to the user and embedded as
comments in the generated TMDL.

Known Direct Lake constraints considered (as of Fabric GA, mid-2025):
 - Physical calculated columns are NOT supported (must be materialized
   upstream in the Lakehouse). MDX calculated members translate to DAX
   calculated measures, which ARE supported, so calculated members alone do
   not block Direct Lake.
 - Parent-child hierarchies (an attribute with Usage=Parent, i.e. a
   self-referencing key column) are detected explicitly from the extracted
   attribute metadata and always flagged as blocking - they require
   PATH()-based calculated columns in Tabular, which are NOT supported in
   Direct Lake unless the path is precomputed in the Lakehouse table
   itself. As a secondary heuristic, any single-level *named* hierarchy is
   also flagged as a warning in case it is a parent-child hierarchy that
   was not modeled as a dedicated Usage=Parent attribute.
 - Semi-additive aggregate functions (AverageOfChildren, ByAccount,
   FirstChild, LastChild, FirstNonEmpty, LastNonEmpty) require a hand
   written CALCULATE/time-intelligence DAX pattern. Not blocking, but
   flagged since they do not map to a plain SUM/COUNT aggregation.
 - ROLAP or writeback partitions imply the source is not a simple queryable
   star-schema table snapshot suitable for a 1:1 Delta table mapping, and
   are flagged as blocking (falls back to Import, needs manual ETL design).
 - Distinct Count measures ARE supported in Direct Lake.
 - Many-to-many measure group dimension relationships require a bridge
   table. Supported in Direct Lake, but flagged for manual review since the
   bridge table must also be materialized as a Delta table.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

SEMI_ADDITIVE_FUNCTIONS = {
    "AverageOfChildren",
    "ByAccount",
    "FirstChild",
    "LastChild",
    "FirstNonEmpty",
    "LastNonEmpty",
}
SIMPLE_AGGREGATIONS = {"Sum", "Count", "Min", "Max", "DistinctCount"}


@dataclass
class Finding:
    severity: str  # "blocking" | "warning" | "info"
    scope: str  # e.g. "cube:Sales Cube", "measure:Sales Amount"
    message: str


@dataclass
class CubeFeasibility:
    cube_name: str
    recommended_mode: str  # "DirectLake" | "Import"
    findings: list = field(default_factory=list)


@dataclass
class FeasibilityReport:
    cubes: list = field(default_factory=list)


def analyze(ir):
    report = FeasibilityReport()

    for cube in ir.get("cubes", []):
        cf = CubeFeasibility(cube_name=cube["name"], recommended_mode="DirectLake")

        for cm in cube.get("calculated_members", []):
            msg = (
                "MDX calculated member found; will be translated to a DAX calculated "
                "measure (supported in Direct Lake). Manual review of the MDX expression "
                "is required to hand-translate to DAX."
            )
            cf.findings.append(Finding("info", "calculated_member:" + cm["name"], msg))

        for kpi in cube.get("kpis", []):
            msg = "KPI found; needs manual DAX translation for Goal/Status/Trend expressions."
            cf.findings.append(Finding("info", "kpi:" + kpi["name"], msg))

        for mg in cube.get("measure_groups", []):
            for part in mg.get("partitions", []):
                mode = part.get("storage_mode")
                scope = "partition:" + mg["name"] + "/" + part["name"]
                if mode not in ("Molap", "InMemory"):
                    cf.recommended_mode = "Import"
                    msg = (
                        "Partition storage mode '" + str(mode) + "' is not a simple MOLAP "
                        "snapshot; cannot guarantee a clean 1:1 Delta table mapping without "
                        "manual ETL design. Falling back to Import."
                    )
                    cf.findings.append(Finding("blocking", scope, msg))
                if part.get("query"):
                    msg = (
                        "Partition is bound by a custom SQL query rather than a plain table "
                        "binding; the Delta table load script must replicate this query's "
                        "logic explicitly."
                    )
                    cf.findings.append(Finding("warning", scope, msg))

            for m in mg.get("measures", []):
                agg = m.get("aggregate_function")
                scope = "measure:" + m["name"]
                if agg in SEMI_ADDITIVE_FUNCTIONS:
                    msg = (
                        "Aggregate function '" + str(agg) + "' is semi-additive and has no "
                        "direct DAX aggregation equivalent; requires a hand-written "
                        "CALCULATE/time-intelligence DAX pattern. Not blocking for Direct Lake."
                    )
                    cf.findings.append(Finding("warning", scope, msg))
                elif agg not in SIMPLE_AGGREGATIONS:
                    msg = "Unrecognized aggregate function '" + str(agg) + "'; manual DAX translation required."
                    cf.findings.append(Finding("warning", scope, msg))

                src_dt = m.get("source_data_type")
                if src_dt and m.get("data_type") != src_dt:
                    msg = (
                        "Measure DataType (" + str(m.get("data_type")) + ") differs from source "
                        "column type (" + str(src_dt) + "); verify the Delta table column type matches."
                    )
                    cf.findings.append(Finding("info", scope, msg))

            for mgd in mg.get("dimensions", []):
                scope = "relationship:" + str(mgd.get("dimension_id"))
                cardinality = mgd.get("cardinality")
                if cardinality not in ("One", None):
                    msg = (
                        "Measure group dimension cardinality '" + str(cardinality) + "' implies "
                        "a many-to-many relationship; requires a bridge Delta table and an "
                        "explicit many-to-many relationship in the semantic model."
                    )
                    cf.findings.append(Finding("warning", scope, msg))
                if not mgd.get("granularity_attribute_id"):
                    cf.recommended_mode = "Import"
                    msg = (
                        "No granularity attribute found for this measure group dimension; "
                        "cannot determine the relationship's 'many' side key column."
                    )
                    cf.findings.append(Finding("blocking", scope, msg))

        for dim in ir.get("dimensions", []):
            for attr in dim.get("attributes", []):
                if attr.get("usage") == "Parent":
                    cf.recommended_mode = "Import"
                    scope = "parent_child_attribute:" + dim["name"] + "/" + attr["name"]
                    msg = (
                        "Attribute '" + attr["name"] + "' has Usage=Parent - this is a genuine "
                        "parent-child hierarchy (self-referencing key column). Direct Lake/DAX "
                        "has no PATH()-based calculated-column equivalent; the hierarchy must be "
                        "precomputed as a materialized path/level structure in the Lakehouse, or "
                        "the model falls back to Import with a manual DAX PATH() pattern. "
                        "Falling back to Import for this cube."
                    )
                    cf.findings.append(Finding("blocking", scope, msg))

            for hier in dim.get("hierarchies", []):
                if len(hier.get("levels", [])) == 1:
                    scope = "hierarchy:" + dim["name"] + "/" + hier["name"]
                    msg = (
                        "Single-level hierarchy detected; if this is a parent-child hierarchy "
                        "(self-referencing key), Direct Lake cannot support it natively. Please "
                        "confirm whether this is parent-child."
                    )
                    cf.findings.append(Finding("warning", scope, msg))

        if cf.recommended_mode == "DirectLake" and not any(f.severity == "blocking" for f in cf.findings):
            msg = "No blocking constructs found. Direct Lake is recommended for this cube."
            cf.findings.append(Finding("info", "cube:" + cube["name"], msg))

        report.cubes.append(cf)

    return report


def analyze_file(input_path, output_path):
    with open(input_path, "r", encoding="utf-8") as f:
        ir = json.load(f)
    report = analyze(ir)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2)
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Analyze Direct Lake feasibility from extracted cube metadata IR")
    parser.add_argument("--input", default="output/cube_metadata.json")
    parser.add_argument("--output", default="output/feasibility_report.json")
    args = parser.parse_args()

    rep = analyze_file(args.input, args.output)
    for cf in rep.cubes:
        print("")
        print("Cube: " + cf.cube_name + " -> Recommended mode: " + cf.recommended_mode)
        for f in cf.findings:
            print("  [" + f.severity.upper() + "] " + f.scope + ": " + f.message)
    print("")
    print("Full report written to " + args.output)
