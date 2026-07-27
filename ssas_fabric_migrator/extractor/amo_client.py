"""
Generic SSAS Multidimensional metadata extractor.

Uses pythonnet to load the AMO (Microsoft.AnalysisServices) assembly and walk
the object model of a connected server/database, producing a structured,
JSON-serializable intermediate representation (IR) that downstream modules
(direct-lake feasibility analysis, TMDL generation, delta table DDL) consume.

Run with an x64 Python interpreter (see requirements.txt note) - AMO/pythonnet
do not have working wheels on Windows ARM64 at this time.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _find_amo_assembly() -> str:
    """Locate Microsoft.AnalysisServices.dll under the SQL Server install tree."""
    candidates = []
    for base in (r"C:\Program Files\Microsoft SQL Server", r"C:\Program Files (x86)\Microsoft SQL Server"):
        candidates.extend(glob.glob(os.path.join(base, "**", "Microsoft.AnalysisServices.dll"), recursive=True))
    if not candidates:
        raise FileNotFoundError(
            "Could not find Microsoft.AnalysisServices.dll. Install SSMS or the AMO/ADOMD "
            "client libraries, or set AMO_DLL_PATH env var explicitly."
        )
    # Prefer the highest-versioned copy (path sorting is a reasonable proxy).
    candidates.sort()
    return candidates[-1]


def _load_amo():
    import clr  # provided by pythonnet

    dll_path = os.environ.get("AMO_DLL_PATH") or _find_amo_assembly()
    sys.path.append(os.path.dirname(dll_path))
    clr.AddReference(dll_path)
    import Microsoft.AnalysisServices as AMO  # noqa: N813
    return AMO


@dataclass
class KeyColumnIR:
    table: Optional[str]
    column: Optional[str]
    data_type: Optional[str]


@dataclass
class AttributeIR:
    id: str
    name: str
    key_columns: list[KeyColumnIR] = field(default_factory=list)
    name_column: Optional[KeyColumnIR] = None
    is_key_attribute: bool = False
    order_by: Optional[str] = None
    usage: Optional[str] = None


@dataclass
class HierarchyLevelIR:
    name: str
    source_attribute_id: str


@dataclass
class HierarchyIR:
    id: str
    name: str
    levels: list[HierarchyLevelIR] = field(default_factory=list)


@dataclass
class DimensionIR:
    id: str
    name: str
    key_attribute_id: str
    source_table: Optional[str]
    attributes: list[AttributeIR] = field(default_factory=list)
    hierarchies: list[HierarchyIR] = field(default_factory=list)
    is_degenerate: bool = False  # fact/role-playing dims flagged downstream, not here


@dataclass
class MeasureIR:
    id: str
    name: str
    aggregate_function: str
    data_type: str          # Measure.DataType (MeasureDataType enum)
    source_data_type: Optional[str]  # DataItem.DataType (OleDbType enum)
    source_table: Optional[str]
    source_column: Optional[str]
    format_string: Optional[str]


@dataclass
class MeasureGroupDimensionIR:
    cube_dimension_id: str
    dimension_id: str
    granularity_attribute_id: Optional[str]
    cardinality: Optional[str]  # One/Many


@dataclass
class PartitionIR:
    name: str
    source_table: Optional[str]
    query: Optional[str]
    storage_mode: Optional[str]


@dataclass
class MeasureGroupIR:
    name: str
    fact_table: Optional[str]
    measures: list[MeasureIR] = field(default_factory=list)
    dimensions: list[MeasureGroupDimensionIR] = field(default_factory=list)
    partitions: list[PartitionIR] = field(default_factory=list)


@dataclass
class CalculatedMemberIR:
    name: str
    expression: str
    parent_hierarchy: Optional[str]
    format_string: Optional[str]
    visible: bool = True


@dataclass
class KpiIR:
    name: str
    associated_measure_group: Optional[str]
    value_expression: Optional[str]
    goal_expression: Optional[str]
    status_expression: Optional[str]
    trend_expression: Optional[str]


@dataclass
class CubeIR:
    name: str
    measure_groups: list[MeasureGroupIR] = field(default_factory=list)
    calculated_members: list[CalculatedMemberIR] = field(default_factory=list)
    kpis: list[KpiIR] = field(default_factory=list)
    default_measure: Optional[str] = None


@dataclass
class DataSourceIR:
    id: str
    name: str
    connection_string: str
    provider: Optional[str]


@dataclass
class DsvColumnIR:
    name: str
    data_type: str  # .NET type name, e.g. System.Int32


@dataclass
class DsvTableIR:
    name: str
    schema: Optional[str]
    columns: list[DsvColumnIR] = field(default_factory=list)


@dataclass
class DsvRelationIR:
    name: str
    parent_table: str
    parent_columns: list[str]
    child_table: str
    child_columns: list[str]


@dataclass
class DataSourceViewIR:
    id: str
    name: str
    data_source_id: Optional[str]
    tables: list[DsvTableIR] = field(default_factory=list)
    relations: list[DsvRelationIR] = field(default_factory=list)


@dataclass
class CubeModelIR:
    server: str
    database: str
    data_sources: list[DataSourceIR] = field(default_factory=list)
    data_source_views: list[DataSourceViewIR] = field(default_factory=list)
    dimensions: list[DimensionIR] = field(default_factory=list)
    cubes: list[CubeIR] = field(default_factory=list)


def _key_columns(amo_key_col_collection) -> list[KeyColumnIR]:
    out = []
    for kc in amo_key_col_collection:
        table = None
        column = None
        try:
            source = kc.Source
            table = getattr(source, "TableID", None)
            column = getattr(source, "ColumnID", None)
        except Exception:
            pass
        out.append(KeyColumnIR(table=table, column=column, data_type=str(kc.DataType)))
    return out


def extract_database(server_name: str, database_name: str) -> CubeModelIR:
    AMO = _load_amo()

    server = AMO.Server()
    server.Connect(server_name)
    try:
        db = server.Databases.FindByName(database_name)
        if db is None:
            raise ValueError(f"Database '{database_name}' not found on server '{server_name}'")

        ir = CubeModelIR(server=server_name, database=database_name)

        # --- Data sources ---
        for ds in db.DataSources:
            ir.data_sources.append(
                DataSourceIR(
                    id=ds.ID,
                    name=ds.Name,
                    connection_string=ds.ConnectionString,
                    provider=str(getattr(ds, "ManagedProvider", None)),
                )
            )

        # --- Data source views (schema + relations) ---
        for dsv in db.DataSourceViews:
            dsv_ir = DataSourceViewIR(id=dsv.ID, name=dsv.Name, data_source_id=None)
            try:
                dsv_ir.data_source_id = dsv.DataSourceID
            except Exception:
                pass
            schema = dsv.Schema
            for table in schema.Tables:
                t_ir = DsvTableIR(name=table.TableName, schema=None)
                for col in table.Columns:
                    t_ir.columns.append(DsvColumnIR(name=col.ColumnName, data_type=str(col.DataType)))
                dsv_ir.tables.append(t_ir)
            for rel in schema.Relations:
                parent_cols = [c.ColumnName for c in rel.ParentColumns]
                child_cols = [c.ColumnName for c in rel.ChildColumns]
                dsv_ir.relations.append(
                    DsvRelationIR(
                        name=rel.RelationName,
                        parent_table=rel.ParentTable.TableName,
                        parent_columns=parent_cols,
                        child_table=rel.ChildTable.TableName,
                        child_columns=child_cols,
                    )
                )
            ir.data_source_views.append(dsv_ir)

        # --- Dimensions ---
        for dim in db.Dimensions:
            key_attr = dim.KeyAttribute
            dim_ir = DimensionIR(
                id=dim.ID,
                name=dim.Name,
                key_attribute_id=key_attr.ID if key_attr is not None else None,
                source_table=None,
            )
            for attr in dim.Attributes:
                a = attr
                attr_ir = AttributeIR(
                    id=a.ID,
                    name=a.Name,
                    key_columns=_key_columns(a.KeyColumns),
                    is_key_attribute=(key_attr is not None and a.ID == key_attr.ID),
                    usage=str(getattr(a, "Usage", None)) if getattr(a, "Usage", None) is not None else None,
                )
                try:
                    if a.NameColumn is not None and a.NameColumn.Source is not None:
                        attr_ir.name_column = KeyColumnIR(
                            table=getattr(a.NameColumn.Source, "TableID", None),
                            column=getattr(a.NameColumn.Source, "ColumnID", None),
                            data_type=str(a.NameColumn.DataType),
                        )
                except Exception:
                    pass
                dim_ir.attributes.append(attr_ir)
                if attr_ir.is_key_attribute and attr_ir.key_columns:
                    dim_ir.source_table = attr_ir.key_columns[0].table

            for hier in dim.Hierarchies:
                h_ir = HierarchyIR(id=hier.ID, name=hier.Name)
                for lvl in hier.Levels:
                    h_ir.levels.append(
                        HierarchyLevelIR(name=lvl.Name, source_attribute_id=lvl.SourceAttributeID)
                    )
                dim_ir.hierarchies.append(h_ir)

            ir.dimensions.append(dim_ir)

        # --- Cubes ---
        for cube in db.Cubes:
            cube_ir = CubeIR(name=cube.Name)
            try:
                cube_ir.default_measure = cube.DefaultMeasure
            except Exception:
                pass

            for mg in cube.MeasureGroups:
                mg_ir = MeasureGroupIR(name=mg.Name, fact_table=None)

                for m in mg.Measures:
                    src_table = None
                    src_col = None
                    src_dt = None
                    try:
                        src_table = getattr(m.Source.Source, "TableID", None)
                        src_col = getattr(m.Source.Source, "ColumnID", None)
                        src_dt = str(m.Source.DataType)
                    except Exception:
                        pass
                    mg_ir.measures.append(
                        MeasureIR(
                            id=m.ID,
                            name=m.Name,
                            aggregate_function=str(m.AggregateFunction),
                            data_type=str(m.DataType),
                            source_data_type=src_dt,
                            source_table=src_table,
                            source_column=src_col,
                            format_string=getattr(m, "FormatString", None),
                        )
                    )

                for mgd in mg.Dimensions:
                    granularity_attr = None
                    cardinality = None
                    try:
                        cardinality = str(mgd.Cardinality)
                    except Exception:
                        pass
                    try:
                        for a in mgd.Attributes:
                            if str(a.Type) == "Granularity":
                                granularity_attr = a.AttributeID
                    except Exception:
                        pass
                    mg_ir.dimensions.append(
                        MeasureGroupDimensionIR(
                            cube_dimension_id=mgd.CubeDimensionID,
                            dimension_id=mgd.CubeDimensionID,
                            granularity_attribute_id=granularity_attr,
                            cardinality=cardinality,
                        )
                    )

                for part in mg.Partitions:
                    table = None
                    query = None
                    try:
                        table = getattr(part.Source, "TableID", None)
                    except Exception:
                        pass
                    try:
                        query = getattr(part.Source, "QueryDefinition", None)
                    except Exception:
                        pass
                    if table and not mg_ir.fact_table:
                        mg_ir.fact_table = table
                    mg_ir.partitions.append(
                        PartitionIR(name=part.Name, source_table=table, query=query, storage_mode=str(part.StorageMode))
                    )

                cube_ir.measure_groups.append(mg_ir)

            # Calculated members live in the cube's default MDX script (CalculationCollection)
            try:
                for script in cube.MdxScripts:
                    for calc in script.CalculationProperties:
                        pass  # properties only; commands below hold expressions
                for script in cube.MdxScripts:
                    for cmd in script.Commands:
                        text = getattr(cmd, "Text", "") or ""
                        # crude scan for CREATE MEMBER statements; refined per-cube as needed
                        if "CREATE MEMBER" in text.upper():
                            cube_ir.calculated_members.append(
                                CalculatedMemberIR(
                                    name="(see raw MDX script)",
                                    expression=text,
                                    parent_hierarchy=None,
                                    format_string=None,
                                )
                            )
            except Exception:
                pass

            # KPIs
            try:
                for kpi in cube.Kpis:
                    cube_ir.kpis.append(
                        KpiIR(
                            name=kpi.Name,
                            associated_measure_group=getattr(kpi, "AssociatedMeasureGroupID", None),
                            value_expression=getattr(kpi, "Value", None),
                            goal_expression=getattr(kpi, "Goal", None),
                            status_expression=getattr(kpi, "Status", None),
                            trend_expression=getattr(kpi, "Trend", None),
                        )
                    )
            except Exception:
                pass

            ir.cubes.append(cube_ir)

        return ir
    finally:
        server.Disconnect()


def extract_to_json(server_name: str, database_name: str, output_path: str) -> str:
    ir = extract_database(server_name, database_name)
    data = asdict(ir)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract SSAS Multidimensional cube metadata to JSON IR")
    parser.add_argument("--server", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", default="output/cube_metadata.json")
    args = parser.parse_args()

    path = extract_to_json(args.server, args.database, args.output)
    print(f"Wrote metadata IR to {path}")
