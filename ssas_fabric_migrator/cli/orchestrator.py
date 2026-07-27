"""
End-to-end CLI orchestrator.

Pipeline:
  1. extract      - connect to the SSAS Multidimensional cube via AMO and
                     dump metadata to JSON (requires pythonnet + AMO; run
                     elevated if the AS server's admin ACL requires it).
  2. analyze       - run Direct Lake feasibility analysis.
  3. generate      - produce the TMDL semantic model folder + Fabric
                     notebook Delta-table scripts.
  4. deploy-lake   - create/find the target Lakehouse in Fabric and patch
                     the generated TMDL's expressions.tmdl with its real
                     SQL analytics endpoint.
  5. migrate-data  - extract the star-schema tables from the on-prem SQL
                     Server and write them as Delta tables directly into the
                     Lakehouse via OneLake (no Spark/gateway required).
  6. deploy-model  - create/update the Semantic Model item in Fabric from
                     the TMDL folder.

Each step can also be run independently via its own module's __main__ (see
extractor/amo_client.py, model/feasibility.py, model/tmdl_generator.py,
datamover/loader.py, deploy/fabric_client.py). This orchestrator just chains
them with a single config source (.env file).
"""
from __future__ import annotations

import argparse
import json
import os
import sys


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
    from ssas_fabric_migrator.datamover import loader as datamover_loader
    from ssas_fabric_migrator.deploy.fabric_client import FabricClient

    out = args.output_dir
    metadata_path = os.path.join(out, "cube_metadata.json")
    feasibility_path = os.path.join(out, "feasibility_report.json")
    tmdl_dir = os.path.join(out, "SemanticModel")
    notebooks_dir = os.path.join(out, "notebooks")

    if "extract" in steps:
        print(f"[1/6] Extracting cube metadata from {env['SSAS_SERVER']}/{env['SSAS_DATABASE']} ...")
        amo_client.extract_to_json(env["SSAS_SERVER"], env["SSAS_DATABASE"], metadata_path)

    with open(metadata_path, "r", encoding="utf-8") as f:
        ir = json.load(f)

    if "analyze" in steps:
        print("[2/6] Running Direct Lake feasibility analysis ...")
        feasibility.analyze_file(metadata_path, feasibility_path)

    with open(feasibility_path, "r", encoding="utf-8") as f:
        feasibility_report = json.load(f)

    if "generate" in steps:
        print("[3/6] Generating TMDL semantic model + Fabric notebook scripts ...")
        tmdl_generator.generate_tmdl(ir, feasibility_report, tmdl_dir)
        from ssas_fabric_migrator.datamover.notebook_script_generator import generate_all_notebook_scripts

        generate_all_notebook_scripts(ir, notebooks_dir)

    client = None
    if {"deploy-lake", "migrate-data", "deploy-model"} & steps:
        client = FabricClient(env["FABRIC_TENANT_ID"], env["FABRIC_CLIENT_ID"], env["FABRIC_CLIENT_SECRET"])

    lakehouse = None
    if "deploy-lake" in steps:
        print(f"[4/6] Creating/finding Lakehouse '{args.lakehouse_name}' in workspace {env['FABRIC_WORKSPACE_ID']} ...")
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
        print(f"[5/6] Migrating star-schema tables from {env['SQL_SERVER']}/{env['SQL_DATABASE']} to OneLake ...")
        if lakehouse is None:
            lakehouse = client.find_item(env["FABRIC_WORKSPACE_ID"], args.lakehouse_name, "Lakehouse")
        results = datamover_loader.migrate_all_tables(
            ir, env["SQL_SERVER"], env["SQL_DATABASE"], "onelake",
            workspace_id=env["FABRIC_WORKSPACE_ID"], lakehouse_id=lakehouse["id"],
            credential=client.credential,
        )
        for table_name, info in results.items():
            print(f"    {table_name}: {info['rows']} rows -> {info['path']}")

    if "deploy-model" in steps:
        print(f"[6/6] Deploying semantic model '{args.semantic_model_name}' ...")
        client.create_semantic_model(env["FABRIC_WORKSPACE_ID"], args.semantic_model_name, tmdl_dir)

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SSAS -> Fabric migration orchestrator")
    parser.add_argument(
        "--steps",
        default="extract,analyze,generate,deploy-lake,migrate-data,deploy-model",
        help="comma-separated subset of: extract,analyze,generate,deploy-lake,migrate-data,deploy-model",
    )
    parser.add_argument("--env-file", default="config/.env")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--lakehouse-name", default="RetailLakehouse")
    parser.add_argument("--semantic-model-name", default="RetailCubeDemo")
    args = parser.parse_args()

    env = load_env(args.env_file)
    required = ["SSAS_SERVER", "SSAS_DATABASE"]
    missing = [k for k in required if k not in env]
    if missing:
        print(f"Missing required config keys in {args.env_file}: {missing}", file=sys.stderr)
        sys.exit(1)

    run_pipeline(env, set(args.steps.split(",")), args)
