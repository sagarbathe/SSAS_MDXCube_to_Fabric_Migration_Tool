"""
End-to-end CLI orchestrator.

This pipeline is split into two phases so it can be run entirely on-prem
first, with the resulting artifacts handed off to a Fabric-connected
environment afterwards (see README.md, "Bridging the Environments", for the
full walkthrough of why/how). Nothing in Phase 1 requires Fabric/internet
connectivity; nothing in Phase 2 requires SSAS/AMO connectivity.

PHASE 1 - On-Prem (SSAS + SQL Server only, no Fabric connectivity needed):
  1. extract   - connect to the SSAS Multidimensional cube via AMO and dump
                 metadata to JSON (requires pythonnet + AMO; run elevated if
                 the AS server's admin ACL requires it).
  2. analyze   - run Direct Lake feasibility analysis (pure JSON, no network).
  3. generate  - produce the TMDL semantic model folder + Fabric notebook
                 Delta-table scripts (pure JSON/text generation, no network).
  4. report    - generate MIGRATION_REPORT.md: what was converted
                 automatically vs. what needs manual attention, with
                 suggested alternatives for everything not converted.

  Phase 1 outputs (hand these off to Phase 2 - see README for the full list):
    output/cube_metadata.json, output/feasibility_report.json,
    output/SemanticModel/ (TMDL folder), output/notebooks/*.py,
    output/MIGRATION_REPORT.md, and optionally a local Delta export produced
    by `python -m ssas_fabric_migrator.datamover.loader --target local`.

PHASE 2 - Fabric-connected (no SSAS/AMO connectivity needed):
  5. deploy-lake   - create/find the target Lakehouse in Fabric and patch
                     the generated TMDL's expressions.tmdl with its real SQL
                     analytics endpoint.
  6. migrate-data  - extract the star-schema tables directly from the
                     on-prem SQL Server (requires this machine to reach BOTH
                     SQL Server AND Fabric/OneLake) and write them as Delta
                     tables into the Lakehouse. Use this only when a single
                     machine/process can reach both networks.
  6b. upload-data  - alternative to migrate-data for air-gapped environments:
                     reads Delta tables already produced on-prem by
                     `loader.py --target local` (transferred out-of-band)
                     and uploads them to OneLake. Requires ONLY Fabric
                     connectivity, no SQL Server connectivity.
  7. deploy-model  - create/update the Semantic Model item in Fabric from
                     the TMDL folder.

Each step can also be run independently via its own module's __main__ (see
extractor/amo_client.py, model/feasibility.py, model/tmdl_generator.py,
report/conversion_report.py, datamover/loader.py, deploy/fabric_client.py).
This orchestrator just chains them with a single config source (.env file).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ALL_STEPS = [
    "extract", "analyze", "generate", "report",
    "deploy-lake", "migrate-data", "upload-data", "deploy-model",
]
PHASE1_STEPS = {"extract", "analyze", "generate", "report"}
PHASE2_STEPS = {"deploy-lake", "migrate-data", "upload-data", "deploy-model"}


def load_env(env_path):
    """Minimal .env parser - avoids adding a python-dotenv dependency."""
    env = dict(os.environ)
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def run_pipeline(env, steps, args):
    from ssas_fabric_migrator.extractor import amo_client
    from ssas_fabric_migrator.model import feasibility, tmdl_generator
    from ssas_fabric_migrator.report import conversion_report
    from ssas_fabric_migrator.datamover import loader as datamover_loader
    from ssas_fabric_migrator.deploy.fabric_client import FabricClient

    out = args.output_dir
    metadata_path = os.path.join(out, "cube_metadata.json")
    feasibility_path = os.path.join(out, "feasibility_report.json")
    report_path = os.path.join(out, "MIGRATION_REPORT.md")
    tmdl_dir = os.path.join(out, "SemanticModel")
    notebooks_dir = os.path.join(out, "notebooks")

    if "extract" in steps:
        print(f"[1/7] Extracting cube metadata from {env['SSAS_SERVER']}/{env['SSAS_DATABASE']} ...")
        amo_client.extract_to_json(env["SSAS_SERVER"], env["SSAS_DATABASE"], metadata_path)

    with open(metadata_path, "r", encoding="utf-8") as f:
        ir = json.load(f)

    if "analyze" in steps:
        print("[2/7] Running Direct Lake feasibility analysis ...")
        feasibility.analyze_file(metadata_path, feasibility_path)

    with open(feasibility_path, "r", encoding="utf-8") as f:
        feasibility_report = json.load(f)

    if "generate" in steps:
        print("[3/7] Generating TMDL semantic model + Fabric notebook scripts ...")
        tmdl_generator.generate_tmdl(ir, feasibility_report, tmdl_dir, table_prefix=args.table_prefix)
        from ssas_fabric_migrator.datamover.notebook_script_generator import generate_all_notebook_scripts

        generate_all_notebook_scripts(ir, notebooks_dir)

    if "report" in steps:
        print("[4/7] Generating conversion report (converted vs. needs-manual-review) ...")
        conversion_report.generate_report(ir, feasibility_report, report_path)
        print(f"    Report written to {report_path} - review it before proceeding to Phase 2.")

    client = None
    if {"deploy-lake", "migrate-data", "upload-data", "deploy-model"} & steps:
        client = FabricClient(env["FABRIC_TENANT_ID"], env["FABRIC_CLIENT_ID"], env["FABRIC_CLIENT_SECRET"])

    lakehouse = None
    if "deploy-lake" in steps:
        print(f"[5/7] Creating/finding Lakehouse '{args.lakehouse_name}' in workspace {env['FABRIC_WORKSPACE_ID']} ...")
        lakehouse = client.create_lakehouse(env["FABRIC_WORKSPACE_ID"], args.lakehouse_name)
        endpoint = client.get_lakehouse_sql_endpoint(env["FABRIC_WORKSPACE_ID"], lakehouse["id"])
        print(f"    Lakehouse id: {lakehouse['id']}")
        print(f"    SQL endpoint: {endpoint}")
        expr_path = os.path.join(tmdl_dir, "definition", "expressions.tmdl")
        if endpoint and os.path.exists(expr_path):
            with open(expr_path, "r", encoding="utf-8") as f:
                content = f.read()
            content = content.replace("TODO_SET_LAKEHOUSE_SQL_ENDPOINT", endpoint)
            content = content.replace("TODO_SET_LAKEHOUSE_NAME", args.lakehouse_name)
            with open(expr_path, "w", encoding="utf-8") as f:
                f.write(content)

    if "migrate-data" in steps:
        print(f"[6/7] Migrating star-schema tables from {env['SQL_SERVER']}/{env['SQL_DATABASE']} to OneLake ...")
        if lakehouse is None:
            lakehouse = client.find_item(env["FABRIC_WORKSPACE_ID"], args.lakehouse_name, "Lakehouse")
        results = datamover_loader.migrate_all_tables(
            ir, env["SQL_SERVER"], env["SQL_DATABASE"], "onelake", table_prefix=args.table_prefix,
            workspace_id=env["FABRIC_WORKSPACE_ID"], lakehouse_id=lakehouse["id"],
            credential=client.credential,
        )
        for table_name, info in results.items():
            print(f"    {table_name}: {info['rows']} rows -> {info['path']}")

    if "upload-data" in steps:
        if not args.local_delta_dir:
            print("--local-delta-dir is required for the upload-data step", file=sys.stderr)
            sys.exit(1)
        print(f"[6/7] Uploading Delta tables from local folder '{args.local_delta_dir}' to OneLake (offline transfer) ...")
        if lakehouse is None:
            lakehouse = client.find_item(env["FABRIC_WORKSPACE_ID"], args.lakehouse_name, "Lakehouse")
        results = datamover_loader.upload_all_local_tables(
            args.local_delta_dir,
            env["FABRIC_WORKSPACE_ID"], lakehouse["id"],
            credential=client.credential, table_prefix=args.table_prefix,
        )
        for table_name, info in results.items():
            print(f"    {table_name}: {info['rows']} rows -> {info['path']}")

    if "deploy-model" in steps:
        print(f"[7/7] Deploying semantic model '{args.semantic_model_name}' ...")
        client.create_semantic_model(env["FABRIC_WORKSPACE_ID"], args.semantic_model_name, tmdl_dir)

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SSAS -> Fabric migration orchestrator")
    parser.add_argument(
        "--steps",
        default="extract,analyze,generate,report,deploy-lake,migrate-data,deploy-model",
        help=(
            "comma-separated subset of: " + ",".join(ALL_STEPS) + ". "
            "Phase 1 (on-prem, no Fabric connectivity): " + ",".join(sorted(PHASE1_STEPS)) + ". "
            "Phase 2 (Fabric-connected, no SSAS connectivity): " + ",".join(sorted(PHASE2_STEPS)) + ". "
            "Use 'migrate-data' when one process can reach both SQL Server and Fabric; use "
            "'upload-data' (with --local-delta-dir) for air-gapped environments instead."
        ),
    )
    parser.add_argument("--env-file", default="config/.env")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--lakehouse-name", default="RetailLakehouse")
    parser.add_argument("--semantic-model-name", default="RetailCubeDemo")
    parser.add_argument(
        "--table-prefix", default="",
        help="Optional prefix applied to the Delta table names created in the Lakehouse (e.g. "
             "'stg_') and threaded through the generated TMDL's physical table bindings to match. "
             "Does not affect source SQL table names or logical Tabular table names in the model.",
    )
    parser.add_argument(
        "--local-delta-dir", default=None,
        help="Folder of locally-exported Delta tables to upload (required for the upload-data step)",
    )
    args = parser.parse_args()

    env = load_env(args.env_file)
    steps = set(args.steps.split(","))

    required = []
    if "extract" in steps:
        required += ["SSAS_SERVER", "SSAS_DATABASE"]
    if steps & {"deploy-lake", "migrate-data", "upload-data", "deploy-model"}:
        required += ["FABRIC_TENANT_ID", "FABRIC_CLIENT_ID", "FABRIC_CLIENT_SECRET", "FABRIC_WORKSPACE_ID"]
    if "migrate-data" in steps:
        required += ["SQL_SERVER", "SQL_DATABASE"]
    missing = [k for k in required if k not in env]
    if missing:
        print(f"Missing required config keys in {args.env_file}: {missing}", file=sys.stderr)
        sys.exit(1)

    run_pipeline(env, steps, args)
