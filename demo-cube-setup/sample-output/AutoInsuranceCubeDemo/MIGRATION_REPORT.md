# Migration Conversion Report

- **Source server:** LAPTOP-LQVSA8VE\SSAS
- **Source database:** AutoInsuranceCubeDemo
- **Generated:** 2026-07-27 20:31:16 UTC

This report is generated automatically from the extracted cube metadata and the Direct Lake feasibility analysis. It is the authoritative summary of what this tool converted, what it flagged for manual review, and what it never had visibility into at all. Review every item in sections 3 and 4 before considering the migration complete.

## 1. Summary

| Object | Count |
| --- | --- |
| Cubes | 1 |
| Dimensions | 5 |
| Dimension attributes | 29 |
| Hierarchies | 3 |
| Relational tables (DSV) | 6 |
| Table relationships (DSV) | 7 |
| Measure groups | 1 |
| Measures | 5 |
| Calculated members (found, need manual DAX translation) | 2 |
| KPIs (found, need manual DAX translation) | 1 |

- **Feasibility findings raised:** 4 (of which 1 are blocking for Direct Lake)
- **Constructs never captured by this tool (see section 4):** 7 categories

## 2. Converted Automatically

These objects were read from the source cube and translated into the generated TMDL semantic model / Delta table mapping without any manual input.

### 2.1 Tables mapped to Delta tables

| Source table | Column count |
| --- | --- |
| Dim_Date | 10 |
| Dim_Geography | 7 |
| Dim_ClaimType | 3 |
| Dim_Policyholder | 8 |
| Dim_Policy | 11 |
| Fact_Claims | 14 |

### 2.2 Dimensions & hierarchies

| Dimension | Attribute count | Hierarchies |
| --- | --- | --- |
| Dim Date | 6 | Calendar |
| Dim Geography | 6 | Geography |
| Dim Claim Type | 2 | Claim Category Detail |
| Dim Policyholder | 6 | (none) |
| Dim Policy | 9 | (none) |

### 2.3 Measures

| Cube | Measure group | Measure | Aggregation |
| --- | --- | --- | --- |
| Auto Insurance Cube | Fact Claims | Incurred Amount | Sum |
| Auto Insurance Cube | Fact Claims | Paid Amount | Sum |
| Auto Insurance Cube | Fact Claims | Reserve Amount | Sum |
| Auto Insurance Cube | Fact Claims | Deductible Amount | Sum |
| Auto Insurance Cube | Fact Claims | Claim Count | Sum |

### 2.4 Relationships (fact-to-dimension)

| Cube | Measure group | Dimension | Cardinality |
| --- | --- | --- | --- |
| Auto Insurance Cube | Fact Claims | Dim Date | One |
| Auto Insurance Cube | Fact Claims | Dim Geography | One |
| Auto Insurance Cube | Fact Claims | Dim Claim Type | One |
| Auto Insurance Cube | Fact Claims | Dim Policyholder | One |
| Auto Insurance Cube | Fact Claims | Dim Policy | One |

## 3. Flagged for Manual Review (Found, but Not Fully Automated)

These constructs WERE detected by the extractor/feasibility analyzer but require manual translation or decisions before the migration can be considered complete.

### Cube: Auto Insurance Cube (recommended mode: Import)

| Severity | Item | Finding | Suggested alternative |
| --- | --- | --- | --- |
| BLOCKING | parent_child_attribute:Dim Date/Date Rollup | Attribute 'Date Rollup' has Usage=Parent - this is a genuine parent-child hierarchy (self-referencing key column). Direct Lake/DAX has no PATH()-based calculated-column equivalent; the hierarchy must be precomputed as a materialized path/level structure in the Lakehouse, or the model falls back to Import with a manual DAX PATH() pattern. Falling back to Import for this cube. | This is a genuine Usage=Parent attribute (not just a suspected single-level hierarchy). Precompute a materialized path (e.g. a delimited ancestor path or a fixed set of level columns for the known depth) in the Lakehouse table via a notebook/dataflow, and model it either as a fixed-depth hierarchy or with a DAX PATH()/PATHITEM() calculated column pattern (Import mode only - Direct Lake does not support calculated columns). |

## 4. Not Captured by This Tool At All (Manual Source Review Required)

The extractor has no code path that reads these SSAS constructs, so they will be silently absent from the generated semantic model even if the source cube uses them. You must check the source cube directly (SSMS Object Explorer, the SSDT project, or the cube designer's Roles/Actions/Perspectives/Translations/Calculations tabs) to determine whether each of these applies to your cube.

| Construct | Why it's missing | Suggested alternative |
| --- | --- | --- |
| Dimension/Cube Roles (Row-Level Security) | AMO Role objects (MDX Allowed/Denied Member Set permissions) are not read by the extractor. | Recreate as Power BI/Fabric semantic model Roles with DAX row filters (Model > Manage Roles). Map each MDX allowed/denied member set permission to an equivalent DAX filter expression on the corresponding table, then assign users/groups to the role in the Fabric workspace or via the XMLA endpoint. |
| Actions (Drillthrough, Reporting, Standard) | Cube Action objects are not extracted. | Recreate drillthrough behavior using Power BI's built-in drillthrough pages, or set the 'Detail Rows Expression' property on the relevant measures/table in the semantic model. |
| Perspectives | Cube Perspective objects are not extracted. | Recreate as Power BI Desktop Perspectives (Modeling tab, via Tabular Editor or the external tools ecosystem), or publish separate reports/apps scoped to the relevant tables and measures. |
| Translations | Caption/Translation objects on the cube, dimensions, attributes and measures are not extracted. | Recreate using Tabular Object Model translations (Culture objects) via Tabular Editor's Translations feature, or the semantic model's XMLA/TMSL Culture objects. |
| Custom Rollup Formulas / Unary Operators (parent-child dimensions) | AMO CustomRollupColumn / UnaryOperatorColumn bindings on parent-child attributes are not extracted. | Precompute the rollup in the Lakehouse (materialize a path/aggregation column via a notebook or dataflow), or hand-write an equivalent DAX measure using CALCULATE with a PATH()/PATHITEM()-based filter that reproduces the unary-operator logic. |
| Write-back Enabled Partitions/Dimensions | Write-enabled measure groups are only flagged indirectly (as a non-MOLAP partition storage mode in the feasibility report); the extractor does not read the WriteEnabled property directly. | Fabric semantic models do not support write-back natively. Implement a companion write-back pattern (e.g. a Power App writing to a separate Lakehouse/Warehouse table, joined back into the model), or keep write-back workloads on the source system and migrate only the read path. |
| Calculation Groups / Scoped MDX Assignments (SCOPE statements) | Only Calculated Member and KPI objects are extracted; arbitrary SCOPE(...) ... END SCOPE assignments in the cube's MDX script are not parsed. | Open the cube's MDX Script tab in SSMS/SSDT, identify each SCOPE assignment, and hand-translate it to an equivalent DAX calculation group or calculated measure in the semantic model. |

## 5. Next Steps Checklist

- [ ] Review every row in Section 3 and hand-translate/implement as needed.
- [ ] Manually inspect the source cube for every construct in Section 4 and act on any that apply.
- [ ] Validate row counts and spot-check measure totals in the deployed semantic model against the source cube.
- [ ] Confirm the recommended mode (Direct Lake vs Import) matches your performance/freshness requirements.
- [ ] Re-run this report after any manual fixes to confirm no new blocking findings were introduced.
