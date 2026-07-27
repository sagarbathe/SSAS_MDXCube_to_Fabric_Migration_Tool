"""
Data mover: extracts rows from the on-prem relational source (the star
schema tables underlying the SSAS cube) and writes them as Delta tables.

Three write targets are supported:
 - local: writes Delta tables to a local folder. Requires only SQL Server
   connectivity, no Fabric connectivity at all. This is the target to use
   on the on-prem side of an air-gapped environment (see "upload" below for
   the second half of that workflow).
 - onelake: writes Delta tables directly to a Fabric Lakehouse's "Tables/"
   area via OneLake's ADLS Gen2-compatible endpoint
   (abfss://onelake.dfs.fabric.microsoft.com/<workspace>/<lakehouse>.Lakehouse/Tables/<table>),
   authenticated via a service principal (ClientSecretCredential). This
   requires no Spark/gateway - the deltalake (delta-rs) library writes the
   Delta transaction log directly. Requires BOTH SQL Server connectivity AND
   Fabric/OneLake connectivity from the same machine/process.
 - upload: reads Delta tables that were already extracted to a local folder
   (via the "local" target above, typically on a separate on-prem machine
   with no Fabric connectivity) and uploads them to OneLake. Requires ONLY
   Fabric/OneLake connectivity - no SQL Server connectivity or AMO/pythonnet
   at all. This is the bridge step for enterprises where the on-prem network
   and the Fabric-connected network are not directly reachable from each
   other: run "local" on-prem, transfer the resulting folder out-of-band
   (e.g. secure file copy, removable media, existing file-transfer gateway),
   then run "upload" from a machine that has Fabric/internet access.

This module deliberately does NOT depend on the AMO/pythonnet extractor - it
only needs the JSON IR (for column type + table name info) and a SQL Server
connection string (for the "local"/"onelake" targets; not needed for
"upload").
"""
from __future__ import annotations

import json
import os

import pandas as pd
import pyodbc

AMO_TO_PANDAS_TYPE = {
    "System.Int32": "Int64",
    "System.Int16": "Int64",
    "System.Int64": "Int64",
    "System.Double": "float64",
    "System.Single": "float64",
    "System.Decimal": "float64",
    "System.String": "string",
    "System.DateTime": "datetime64[ns]",
    "System.Boolean": "boolean",
}


def _sql_connection_string(server, database, driver="ODBC Driver 18 for SQL Server"):
    return (
        "DRIVER={" + driver + "};SERVER=" + server + ";DATABASE=" + database
        + ";Trusted_Connection=yes;TrustServerCertificate=yes;"
    )


def list_source_tables(ir):
    """Returns the set of physical table names referenced by the cube's DSV."""
    tables = []
    for dsv in ir.get("data_source_views", []):
        for t in dsv.get("tables", []):
            tables.append(t["name"])
    return tables


def extract_table_df(sql_server, sql_database, table_name):
    conn_str = _sql_connection_string(sql_server, sql_database)
    conn = pyodbc.connect(conn_str)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM [{table_name}]")
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        df = pd.DataFrame.from_records(list(map(tuple, rows)), columns=columns)
        return df
    finally:
        conn.close()


def write_delta_local(df, output_dir, table_name, mode="overwrite"):
    from deltalake import write_deltalake

    table_path = os.path.join(output_dir, table_name)
    os.makedirs(table_path, exist_ok=True)
    kwargs = {"schema_mode": "overwrite"} if mode == "overwrite" else {}
    write_deltalake(table_path, df, mode=mode, **kwargs)
    return table_path


def write_delta_onelake(df, workspace_id, lakehouse_id, table_name, credential, mode="overwrite"):
    from deltalake import write_deltalake

    table_path = (
        f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/"
        f"{lakehouse_id}/Tables/{table_name}"
    )
    token = credential.get_token("https://storage.azure.com/.default").token
    storage_options = {"bearer_token": token, "use_fabric_endpoint": "true"}
    # schema_mode="overwrite" allows the table's column set to change between
    # runs (e.g. a source column is added/removed) instead of raising
    # SchemaMismatchError against the previously-written Delta schema.
    kwargs = {"schema_mode": "overwrite"} if mode == "overwrite" else {}
    write_deltalake(table_path, df, mode=mode, storage_options=storage_options, **kwargs)
    return table_path


def upload_local_delta_to_onelake(local_table_dir, workspace_id, lakehouse_id, table_name, credential, mode="overwrite"):
    """
    Reads a single Delta table that was already materialized on local disk
    (e.g. by write_delta_local on an on-prem, non-Fabric-connected machine)
    and uploads it to OneLake. No SQL Server connection is used here - this
    is the second half of the air-gapped bridge, and only needs Fabric/
    OneLake network access plus the local Delta folder produced by Phase 1.
    """
    from deltalake import DeltaTable

    dt = DeltaTable(local_table_dir)
    df = dt.to_pandas()
    return write_delta_onelake(df, workspace_id, lakehouse_id, table_name, credential, mode), len(df)


def upload_all_local_tables(local_root, workspace_id, lakehouse_id, credential, mode="overwrite"):
    """
    Uploads every Delta table subfolder found directly under local_root
    (as produced by migrate_all_tables(..., target="local")) to OneLake.
    Returns dict of table_name -> {"rows": n, "path": ...}.
    """
    results = {}
    for entry in sorted(os.listdir(local_root)):
        table_dir = os.path.join(local_root, entry)
        if not os.path.isdir(table_dir):
            continue
        path, rows = upload_local_delta_to_onelake(table_dir, workspace_id, lakehouse_id, entry, credential, mode)
        results[entry] = {"rows": rows, "path": path}
    return results


def migrate_all_tables(ir, sql_server, sql_database, target, **target_kwargs):
    """
    target: "local" or "onelake"
    target_kwargs:
      local  -> output_dir=<path>
      onelake -> workspace_id=..., lakehouse_id=..., credential=<TokenCredential>
    Returns dict of table_name -> {"rows": n, "path": ...}
    """
    results = {}
    for table_name in list_source_tables(ir):
        df = extract_table_df(sql_server, sql_database, table_name)
        if target == "local":
            path = write_delta_local(df, target_kwargs["output_dir"], table_name)
        elif target == "onelake":
            path = write_delta_onelake(
                df,
                target_kwargs["workspace_id"],
                target_kwargs["lakehouse_id"],
                table_name,
                target_kwargs["credential"],
            )
        else:
            raise ValueError(f"Unknown target '{target}'")
        results[table_name] = {"rows": len(df), "path": path}
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract star-schema tables and write them as Delta tables")
    parser.add_argument("--metadata", default="output/cube_metadata.json", help="Not required for --target upload")
    parser.add_argument("--sql-server", default=None, help="Required for --target local/onelake")
    parser.add_argument("--sql-database", default=None, help="Required for --target local/onelake")
    parser.add_argument(
        "--target", choices=["local", "onelake", "upload"], default="local",
        help=(
            "local: on-prem only, no Fabric connectivity needed. "
            "onelake: needs BOTH SQL Server and Fabric connectivity from this machine. "
            "upload: needs ONLY Fabric connectivity; reads Delta tables already produced by "
            "--target local (via --local-dir) and pushes them to OneLake - use this on the "
            "Fabric-connected side of an air-gapped environment."
        ),
    )
    parser.add_argument("--output-dir", default="output/delta", help="Used by --target local")
    parser.add_argument("--local-dir", default=None, help="Required for --target upload: folder produced by --target local")
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--lakehouse-id", default=None)
    args = parser.parse_args()

    if args.target == "local":
        if not args.sql_server or not args.sql_database:
            parser.error("--sql-server and --sql-database are required for --target local")
        with open(args.metadata, "r", encoding="utf-8") as f:
            ir = json.load(f)
        res = migrate_all_tables(ir, args.sql_server, args.sql_database, "local", output_dir=args.output_dir)
    elif args.target == "onelake":
        if not args.sql_server or not args.sql_database:
            parser.error("--sql-server and --sql-database are required for --target onelake")
        with open(args.metadata, "r", encoding="utf-8") as f:
            ir = json.load(f)
        from azure.identity import ClientSecretCredential

        cred = ClientSecretCredential(
            tenant_id=os.environ["FABRIC_TENANT_ID"],
            client_id=os.environ["FABRIC_CLIENT_ID"],
            client_secret=os.environ["FABRIC_CLIENT_SECRET"],
        )
        res = migrate_all_tables(
            ir, args.sql_server, args.sql_database, "onelake",
            workspace_id=args.workspace_id, lakehouse_id=args.lakehouse_id, credential=cred,
        )
    else:  # upload
        if not args.local_dir:
            parser.error("--local-dir is required for --target upload")
        from azure.identity import ClientSecretCredential

        cred = ClientSecretCredential(
            tenant_id=os.environ["FABRIC_TENANT_ID"],
            client_id=os.environ["FABRIC_CLIENT_ID"],
            client_secret=os.environ["FABRIC_CLIENT_SECRET"],
        )
        res = upload_all_local_tables(
            args.local_dir, args.workspace_id, args.lakehouse_id, cred,
        )

    for table_name, info in res.items():
        print(f"{table_name}: {info['rows']} rows -> {info['path']}")
