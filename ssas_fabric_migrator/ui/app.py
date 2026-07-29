"""
Lightweight web UI for the SSAS -> Fabric migration tool.

Run with (see README Section 11 for full setup, including why this needs
its OWN isolated virtual environment):
    .venv-ui\\Scripts\\python.exe -m streamlit run ssas_fabric_migrator/ui/app.py

This is a thin wrapper around the existing CLI (`cli/orchestrator.py`,
`datamover/loader.py`) and its underlying modules - it does not duplicate
any pipeline logic. It just gives users a form-based way to configure
connections and click through each step instead of typing CLI commands,
with live log output, per-step explanations, and a built-in viewer for the
generated reports.

DEPLOYMENT NOTE:
This app must run on a Windows host with:
  - line-of-sight network access to the on-prem SSAS instance and SQL
    Server (same domain/VPN as required by the CLI today), and
  - an x64 Python interpreter with pythonnet/pandas/pyarrow/deltalake
    installed, configured via the "Python executable" field below (this is
    a SEPARATE interpreter from the one running Streamlit itself - see
    requirements-ui.txt for why they must not share an environment).
"Any device" access is achieved by hosting this single app once on such a
host and letting users reach it via browser (see README Section 11) - the
UI itself does not need to be installed on every analyst's machine.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import streamlit as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
README_PATH = os.path.join(REPO_ROOT, "README.md")
# Used to rewrite README.md's in-file anchor links (e.g. "(#3-prerequisites)") into
# absolute GitHub links when rendering excerpts in the Read Me tab - GitHub auto-generates
# heading anchors so these resolve there, but Streamlit's markdown renderer does not, and
# the Read Me tab only shows excerpts (not the full file) so in-app anchors would be dead.
README_GITHUB_URL = (
    "https://github.com/sagarbathe/SSAS_MDXCube_to_Fabric_Migration_Tool/blob/main/README.md"
)

ENV_FIELDS = [
    ("SSAS_SERVER", "On-prem SSAS server\\instance", False, "LAPTOP-LQVSA8VE\\SSAS"),
    ("SSAS_DATABASE", "On-prem SSAS database (cube) name", False, "RetailCubeDemo"),
    ("SQL_SERVER", "On-prem SQL Server (relational source)", False, "localhost"),
    ("SQL_DATABASE", "On-prem SQL Server database", False, "RetailDW"),
    ("FABRIC_TENANT_ID", "Fabric/Entra ID tenant ID", False, ""),
    ("FABRIC_CLIENT_ID", "Fabric app registration (service principal) client ID", False, ""),
    ("FABRIC_CLIENT_SECRET", "Fabric app registration client secret", True, ""),
    ("FABRIC_WORKSPACE_ID", "Target Fabric workspace ID", False, ""),
]

STEP_LABELS = {
    "extract": "1. Extract cube metadata",
    "analyze": "2. Analyze Direct Lake feasibility",
    "generate": "3. Generate TMDL + notebook scripts",
    "report": "4. Generate MIGRATION_REPORT.md",
    "deploy-lake": "5. Deploy/find Lakehouse",
    "migrate-data": "6. Migrate data (direct)",
    "upload-data": "6. Migrate data (offline transfer - step 2 of 2)",
    "deploy-model": "7. Deploy semantic model",
}

STEP_DESCRIPTIONS = {
    "extract": (
        "Connects to the on-prem SSAS Multidimensional cube via **AMO** (Windows "
        "Integrated Auth - the account running this app needs the SSAS **Server "
        "Administrator** role) and dumps its full metadata (dimensions, measures, "
        "hierarchies, calculated members, KPIs, DSV schema) to `cube_metadata.json`. "
        "No Fabric connectivity needed for this step."
    ),
    "analyze": (
        "Pure offline analysis (no network calls) of the extracted metadata against "
        "known Fabric **Direct Lake** constraints. Recommends Direct Lake or Import "
        "mode per cube, with itemized reasons for any fallback (e.g. parent-child "
        "hierarchies, semi-additive measures, many-to-many relationships)."
    ),
    "generate": (
        "Pure offline generation (no network calls) of a TMDL semantic model folder "
        "(tables, columns, relationships, DAX measures translated from MDX, "
        "hierarchies) plus starter PySpark notebook scripts. MDX calculated members/"
        "KPIs/parent-child are NOT auto-translated - they're flagged for manual DAX "
        "authoring instead of risking a silently wrong translation."
    ),
    "report": (
        "Generates `MIGRATION_REPORT.md`: a plain-language summary of exactly what "
        "was converted automatically vs. what needs manual attention, with suggested "
        "alternatives for everything not converted. Review this before moving to "
        "Phase 2."
    ),
    "deploy-lake": (
        "Creates the target Fabric Lakehouse (or reuses it if the name already "
        "exists) and patches the generated TMDL's `expressions.tmdl` with its real "
        "SQL analytics endpoint, so the semantic model knows where to find its data."
    ),
    "migrate-data": (
        "Extracts the star-schema tables directly from the on-prem SQL Server and "
        "writes them as Delta tables into the Lakehouse via OneLake, in one step. "
        "Requires this machine to reach BOTH the SQL Server AND Fabric/OneLake at "
        "the same time."
    ),
    "upload-data": (
        "Reads Delta tables already produced locally (Step 1 below) and pushes them "
        "into the Lakehouse via OneLake. Requires ONLY Fabric connectivity from this "
        "machine - no SQL Server access needed here."
    ),
    "deploy-model": (
        "Creates (or updates) the Semantic Model item in the target Fabric workspace "
        "from the generated TMDL folder. If a previously-deployed model's storage "
        "mode needs to flip (Direct Lake <-> Import), Fabric doesn't support that "
        "in-place - this step automatically deletes and recreates the item instead."
    ),
}

ALT_DATA_OPTIONS = [
    ("1. Fabric Mirroring for SQL Server (CDC)", "Native, near real-time, zero-ETL - but requires an On-premises Data Gateway (live connectivity) and SQL Server CDC enabled."),
    ("2. Fabric Mirroring for SQL Server 2025 (Change Feed)", "Same idea as #1, log-based instead of CDC tables - requires SQL Server 2025 + Azure Arc enrollment + gateway."),
    ("3. Fabric Open Mirroring", "You push Parquet/CSV + a metadata file to a Fabric landing zone; Fabric auto-converts to Delta with upserts. No gateway required, but you build the extraction/incremental logic yourself."),
    ("4. Fabric Data Factory (Copy Data activity)", "Low-code scheduled pipeline via an On-premises Data Gateway; supports incremental copy via a watermark column."),
    ("5. Fabric Dataflows Gen2", "Power Query-based low-code ingestion; simplest for business-user-maintained transforms on smaller tables; still needs a gateway."),
    ("6. Fabric Notebook (PySpark/JDBC)", "Full custom Spark logic for large tables/complex transforms. This tool already generates a starter script for this path (Step 3's notebooks/*.py)."),
]

PHASE1_STEPS = ["extract", "analyze", "generate", "report"]


def init_state():
    st.session_state.setdefault("env_file", os.path.join("config", ".env"))
    st.session_state.setdefault("output_dir", "output")
    st.session_state.setdefault("lakehouse_mode", "existing")
    st.session_state.setdefault("lakehouse_name", "RetailLakehouse")
    st.session_state.setdefault("available_lakehouses", [])
    st.session_state.setdefault("table_prefix", "")
    st.session_state.setdefault("semantic_model_name", "RetailCubeDemo")
    st.session_state.setdefault("local_export_dir", "output\\delta")
    st.session_state.setdefault("local_delta_dir", "output\\delta")
    st.session_state.setdefault("migration_method", "direct")
    st.session_state.setdefault("python_exe", sys.executable)
    st.session_state.setdefault("env_values", {k: d for k, _, _, d in ENV_FIELDS})


def load_env_file(path: str):
    from ssas_fabric_migrator.cli.orchestrator import load_env

    full_path = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
    env = load_env(full_path)
    for key, _, _, _ in ENV_FIELDS:
        if key in env:
            st.session_state["env_values"][key] = env[key]


def save_env_file(path: str):
    full_path = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        for key, _, _, _ in ENV_FIELDS:
            f.write(f"{key}={st.session_state['env_values'].get(key, '')}\n")


def _stream_subprocess(cmd, log_area):
    log_lines = [f"$ {' '.join(cmd)}", ""]
    log_area.code("\n".join(log_lines), language="text")
    proc = subprocess.Popen(
        cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in proc.stdout:  # type: ignore[union-attr]
        log_lines.append(line.rstrip("\n"))
        log_area.code("\n".join(log_lines), language="text")
    proc.wait()
    return proc.returncode


def run_steps(steps: list[str], log_area):
    """Run the given orchestrator steps as a subprocess, streaming output live."""
    cmd = [
        st.session_state["python_exe"], "-m", "ssas_fabric_migrator.cli.orchestrator",
        "--steps", ",".join(steps),
        "--env-file", st.session_state["env_file"],
        "--output-dir", st.session_state["output_dir"],
        "--lakehouse-name", st.session_state["lakehouse_name"],
        "--semantic-model-name", st.session_state["semantic_model_name"],
        "--table-prefix", st.session_state["table_prefix"],
    ]
    if "upload-data" in steps:
        cmd += ["--local-delta-dir", st.session_state["local_delta_dir"]]

    returncode = _stream_subprocess(cmd, log_area)
    if returncode == 0:
        st.success(f"Steps [{', '.join(steps)}] completed successfully.")
    else:
        st.error(f"Steps [{', '.join(steps)}] failed with exit code {returncode}. See log above.")


def run_local_export(log_area):
    """Runs loader.py --target local directly (this step isn't part of the orchestrator's
    ALL_STEPS - it's an optional Phase 1 export used only by the offline-transfer path)."""
    out = st.session_state["output_dir"]
    metadata_path = os.path.join(out, "cube_metadata.json")
    ev = st.session_state["env_values"]
    cmd = [
        st.session_state["python_exe"], "-m", "ssas_fabric_migrator.datamover.loader",
        "--metadata", metadata_path,
        "--sql-server", ev.get("SQL_SERVER", ""),
        "--sql-database", ev.get("SQL_DATABASE", ""),
        "--target", "local",
        "--output-dir", st.session_state["local_export_dir"],
    ]
    returncode = _stream_subprocess(cmd, log_area)
    if returncode == 0:
        st.success("Local export completed successfully.")
    else:
        st.error(f"Local export failed with exit code {returncode}. See log above.")


def list_lakehouses():
    """Lists existing Lakehouse items in the configured workspace, via a one-off subprocess
    call using the pipeline's Python interpreter (keeps requests/azure-identity out of the
    UI's own isolated venv)."""
    ev = st.session_state["env_values"]
    script = (
        "import json, os\n"
        "from ssas_fabric_migrator.deploy.fabric_client import FabricClient\n"
        "c = FabricClient(os.environ['FABRIC_TENANT_ID'], os.environ['FABRIC_CLIENT_ID'], os.environ['FABRIC_CLIENT_SECRET'])\n"
        "print(json.dumps(c.list_items(os.environ['FABRIC_WORKSPACE_ID'], 'Lakehouse')))\n"
    )
    env = dict(os.environ)
    for key in ("FABRIC_TENANT_ID", "FABRIC_CLIENT_ID", "FABRIC_CLIENT_SECRET", "FABRIC_WORKSPACE_ID"):
        env[key] = ev.get(key, "")
    result = subprocess.run(
        [st.session_state["python_exe"], "-c", script],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Unknown error listing Lakehouses")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _rewrite_readme_anchors(markdown_text: str) -> str:
    """Rewrites in-file anchor links like "(#3-prerequisites)" into absolute links to the
    README on GitHub, where they actually resolve (GitHub auto-generates heading anchors;
    Streamlit's markdown renderer does not, and this tab only shows excerpts of the file)."""
    return re.sub(r"\]\(#([a-z0-9\-]+)\)", rf"]({README_GITHUB_URL}#\1)", markdown_text)


def render_readme_tab():
    st.subheader("What this tool is - quick summary")
    st.markdown(
        "A code-based accelerator that migrates an on-premises **SQL Server Analysis "
        "Services (SSAS) Multidimensional** cube to **Microsoft Fabric**, producing a "
        "Direct Lake (or Import, with documented reasons) Power BI semantic model "
        "backed by Delta tables in a Fabric Lakehouse. It runs in two phases: "
        "**Phase 1 (on-prem)** extracts/analyzes/generates/reports with no Fabric "
        "connectivity, and **Phase 2 (Fabric-connected)** deploys the Lakehouse, "
        "migrates data, and deploys the semantic model with no SSAS connectivity - "
        "so the two halves can be run on entirely separate machines/networks."
    )

    if not os.path.exists(README_PATH):
        st.warning(f"README.md not found at {README_PATH}")
        return

    with open(README_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    sections = {}
    current_title, current_lines = None, []
    for line in content.splitlines():
        if line.startswith("## "):
            if current_title:
                sections[current_title] = "\n".join(current_lines).strip()
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_title:
        sections[current_title] = "\n".join(current_lines).strip()

    purpose_key = next((k for k in sections if k.startswith("1.")), None)
    limitations_key = next((k for k in sections if k.startswith("10.")), None)

    st.divider()
    st.subheader("What this tool DOES (Section 1: Purpose / Objective)")
    if purpose_key:
        st.markdown(_rewrite_readme_anchors(sections[purpose_key]))
    else:
        st.info("Purpose/Objective section not found in README.md.")

    st.divider()
    st.subheader("What this tool CANNOT do / Limitations (Section 10)")
    if limitations_key:
        st.markdown(_rewrite_readme_anchors(sections[limitations_key]))
    else:
        st.info("Limitations section not found in README.md.")

    st.divider()
    st.caption(
        "This is a live summary pulled directly from README.md in the repo root. Links to "
        "other sections (e.g. 'Section 3') open the full README on GitHub, since this tab "
        "only shows excerpts - see the full file there for the step-by-step walkthrough, "
        "prerequisites, architecture diagram, and all other sections."
    )
    st.link_button("Open full README.md on GitHub", README_GITHUB_URL, type="primary")


def render_config_tab():
    st.subheader("Connection configuration")

    is_git_repo = os.path.isdir(os.path.join(REPO_ROOT, ".git"))
    with st.expander(
        "Step 0: Get this tool onto your machine (clone the repo)",
        expanded=not is_git_repo,
    ):
        st.markdown(
            "Everything this app needs - the pipeline code, `config/.env.template`, "
            "and (once you save below) your own `config/.env` file - lives inside "
            "one folder: a local clone/download of this repository. If you haven't "
            "done that on **this machine** yet, do it first:"
        )
        st.code(
            "git clone https://github.com/sagarbathe/SSAS_MDXCube_to_Fabric_Migration_Tool.git\n"
            "cd SSAS_MDXCube_to_Fabric_Migration_Tool",
            language="powershell",
        )
        st.markdown(
            "No `git` installed? Download the ZIP instead from the repo's green "
            "**Code -> Download ZIP** button on GitHub, then extract it anywhere on "
            "this machine. Either way, launch this Streamlit app **from inside that "
            "folder** (see README Section 11) so the paths below resolve correctly - "
            "this app currently sees its own repo folder as:"
        )
        st.code(REPO_ROOT, language="text")
        st.markdown(
            "Then follow [README Sections 3-4](https://github.com/sagarbathe/"
            "SSAS_MDXCube_to_Fabric_Migration_Tool/blob/main/README.md"
            "#3-prerequisites) to install prerequisites/dependencies before using "
            "the steps below."
        )

    st.caption(
        "These values are written to a local `.env` file (git-ignored) and read by "
        "every pipeline step, exactly like the CLI's `--env-file` argument."
    )

    col1, col2 = st.columns([3, 1])
    with col1:
        st.session_state["env_file"] = st.text_input(
            "Env file path (relative to repo root, or absolute)",
            value=st.session_state["env_file"],
        )
    with col2:
        st.write("")
        st.write("")
        if st.button("Load from file", type="primary"):
            try:
                load_env_file(st.session_state["env_file"])
                st.success("Loaded.")
            except Exception as e:
                st.error(f"Could not load: {e}")

    env_path_for_display = st.session_state["env_file"]
    full_env_path = (
        env_path_for_display if os.path.isabs(env_path_for_display)
        else os.path.join(REPO_ROOT, env_path_for_display)
    )
    exists_note = "exists" if os.path.exists(full_env_path) else "does not exist yet - will be created on Save"
    st.info(
        f"This env file lives on disk **relative to wherever you cloned/downloaded this "
        f"repository** on this machine (`{REPO_ROOT}` on this particular machine right "
        f"now) - it will resolve to a different folder automatically on someone else's "
        f"machine or a different clone location:\n\n`{full_env_path}`\n\n"
        f"({exists_note}) - open it directly in a text editor if you'd rather edit it "
        f"outside this UI. It's git-ignored, so its contents (including secrets) are "
        f"never committed to the repo."
    )

    with st.form("env_form"):
        for key, label, is_secret, _default in ENV_FIELDS:
            st.session_state["env_values"][key] = st.text_input(
                label, value=st.session_state["env_values"].get(key, ""),
                type="password" if is_secret else "default", key=f"field_{key}",
            )
        submitted = st.form_submit_button("Save to env file", type="primary")
        if submitted:
            try:
                save_env_file(st.session_state["env_file"])
                st.success(f"Saved to {st.session_state['env_file']}")
            except Exception as e:
                st.error(f"Could not save: {e}")

    st.divider()
    st.subheader("Run settings")
    st.session_state["output_dir"] = st.text_input("Output directory", value=st.session_state["output_dir"])
    st.session_state["semantic_model_name"] = st.text_input(
        "Fabric Semantic Model name", value=st.session_state["semantic_model_name"]
    )
    st.session_state["python_exe"] = st.text_input(
        "Python executable to run pipeline steps with",
        value=st.session_state["python_exe"],
        help=(
            "Use the x64 interpreter with pythonnet/pandas/pyarrow/deltalake installed "
            "(requirements.txt) - NOT the interpreter/venv Streamlit itself is running in. "
            "On Windows ARM64, the default 'python' on PATH usually resolves to an ARM64 "
            "interpreter lacking these - point this at your x64 python.exe instead."
        ),
    )


def render_phase1_tab():
    st.subheader("Phase 1: On-Prem (SSAS + SQL Server, no Fabric connectivity needed)")
    log_area = st.empty()
    for step in PHASE1_STEPS:
        st.markdown(f"**{STEP_LABELS[step]}**")
        st.caption(STEP_DESCRIPTIONS[step])
        if st.button("Run this step", key=f"p1_{step}", type="primary"):
            run_steps([step], log_area)
        st.divider()
    if st.button("Run all of Phase 1", key="p1_all", type="primary"):
        run_steps(PHASE1_STEPS, log_area)


def render_phase2_tab():
    st.subheader("Phase 2: Fabric-connected (no SSAS connectivity needed)")
    log_area = st.empty()

    # --- Step 5: Lakehouse ---
    st.markdown(f"**{STEP_LABELS['deploy-lake']}**")
    st.caption(STEP_DESCRIPTIONS["deploy-lake"])
    st.session_state["lakehouse_mode"] = st.radio(
        "Lakehouse to use",
        options=["existing", "new"],
        format_func=lambda v: "Use an existing Lakehouse" if v == "existing" else "Create a new Lakehouse",
        index=0 if st.session_state["lakehouse_mode"] == "existing" else 1,
        horizontal=True, key="lakehouse_mode_radio",
    )
    if st.session_state["lakehouse_mode"] == "existing":
        col_a, col_b = st.columns([1, 2])
        with col_a:
            if st.button("Refresh list of Lakehouses", type="primary"):
                try:
                    st.session_state["available_lakehouses"] = list_lakehouses()
                    st.success(f"Found {len(st.session_state['available_lakehouses'])} Lakehouse(s).")
                except Exception as e:
                    st.error(f"Could not list Lakehouses: {e}")
        with col_b:
            names = [lh["displayName"] for lh in st.session_state["available_lakehouses"]]
            if names:
                st.session_state["lakehouse_name"] = st.selectbox("Existing Lakehouse", options=names)
            else:
                st.session_state["lakehouse_name"] = st.text_input(
                    "Existing Lakehouse name (click 'Refresh list' above to pick from a dropdown instead)",
                    value=st.session_state["lakehouse_name"],
                )
    else:
        st.session_state["lakehouse_name"] = st.text_input(
            "New Lakehouse name", value=st.session_state["lakehouse_name"]
        )

    st.session_state["table_prefix"] = st.text_input(
        "Delta table name prefix (optional)",
        value=st.session_state["table_prefix"],
        help=(
            "Prepended to each Delta table's name inside the Lakehouse (e.g. 'stg_Dim_Date'). "
            "Useful when sharing one Lakehouse across multiple cube migrations. Leave blank for "
            "no prefix. The semantic model's table names shown in Power BI are unaffected - only "
            "the underlying physical Lakehouse table name changes."
        ),
    )
    if st.button("Deploy / find Lakehouse", key="p2_deploy_lake", type="primary"):
        run_steps(["deploy-lake"], log_area)

    st.divider()

    # --- Step 6: Data migration ---
    st.markdown("**6. Migrate data into the Lakehouse**")
    st.session_state["migration_method"] = st.radio(
        "How do you want to migrate data into the Lakehouse?",
        options=["direct", "offline"],
        format_func=lambda v: (
            "Direct migration - this machine reaches both SQL Server and Fabric"
            if v == "direct" else
            "Offline transfer - export locally, then upload separately (for isolated/air-gapped networks)"
        ),
        index=0 if st.session_state["migration_method"] == "direct" else 1,
        key="migration_method_radio",
    )

    if st.session_state["migration_method"] == "direct":
        st.caption(STEP_DESCRIPTIONS["migrate-data"])
        if st.button("Migrate data now", key="p2_migrate_direct", type="primary"):
            run_steps(["migrate-data"], log_area)
    else:
        st.markdown("**Step 1 of 2: export SQL Server tables to local Delta files**")
        st.caption(
            "Runs on the on-prem side - needs only SQL Server access, no Fabric connectivity. "
            "Produces one local Delta folder per table under the export folder below."
        )
        st.session_state["local_export_dir"] = st.text_input(
            "Local export folder", value=st.session_state["local_export_dir"], key="local_export_dir_input"
        )
        if st.button("Export to local Delta files", key="p2_export_local", type="primary"):
            run_local_export(log_area)

        st.markdown(
            "*(Manually transfer the export folder above from the on-prem machine to this "
            "Fabric-connected machine, using whatever secure file-transfer method your "
            "organization already approves - USB, internal file share, SCP, etc. Nothing is "
            "uploaded directly into OneLake at this point.)*"
        )

        st.markdown("**Step 2 of 2: upload the local Delta files to the Lakehouse**")
        st.caption(STEP_DESCRIPTIONS["upload-data"])
        st.session_state["local_delta_dir"] = st.text_input(
            "Local Delta folder to upload (from Step 1, after transfer)",
            value=st.session_state["local_delta_dir"], key="local_delta_dir_input",
        )
        if st.button("Upload to Lakehouse", key="p2_upload", type="primary"):
            run_steps(["upload-data"], log_area)

    with st.expander("Other ways to migrate data into Fabric (not implemented by this tool)"):
        st.caption(
            "This tool's own approach (direct pyodbc + `deltalake` write, above) is simple and "
            "dependency-light, but is a one-shot batch snapshot with no CDC/incremental support. "
            "For production/enterprise scenarios, Fabric offers these more robust, natively-"
            "supported options instead - see README Section 7 for the full comparison table:"
        )
        for name, desc in ALT_DATA_OPTIONS:
            st.markdown(f"- **{name}** - {desc}")

    st.divider()

    # --- Step 7: Deploy model ---
    st.markdown(f"**{STEP_LABELS['deploy-model']}**")
    st.caption(STEP_DESCRIPTIONS["deploy-model"])
    if st.button("Deploy semantic model", key="p2_deploy_model", type="primary"):
        run_steps(["deploy-model"], log_area)


def render_reports_tab():
    st.subheader("Generated artifacts")
    out = st.session_state["output_dir"]
    full_out = out if os.path.isabs(out) else os.path.join(REPO_ROOT, out)

    report_path = os.path.join(full_out, "MIGRATION_REPORT.md")
    feasibility_path = os.path.join(full_out, "feasibility_report.json")
    manual_path = os.path.join(full_out, "MANUAL_TRANSLATION_REQUIRED.md")

    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            st.markdown(f.read())
    else:
        st.info(f"No report found yet at {report_path}. Run Phase 1 first.")

    with st.expander("feasibility_report.json"):
        if os.path.exists(feasibility_path):
            with open(feasibility_path, "r", encoding="utf-8") as f:
                st.json(json.load(f))
        else:
            st.write("Not generated yet.")

    with st.expander("MANUAL_TRANSLATION_REQUIRED.md"):
        if os.path.exists(manual_path):
            with open(manual_path, "r", encoding="utf-8") as f:
                st.markdown(f.read())
        else:
            st.write("Not generated yet (only produced if the cube has calculated members/KPIs).")


def main():
    st.set_page_config(page_title="SSAS -> Fabric Migration Tool", layout="wide")
    init_state()

    st.title("SSAS Multidimensional -> Microsoft Fabric Migration Tool")
    st.caption(
        "Wraps the existing CLI/orchestrator - see the Read Me tab or README.md for full "
        "step-by-step docs, prerequisites, and limitations."
    )

    with st.sidebar:
        st.header("About this tool")
        st.markdown(
            "- **On-prem steps** (Extract, direct data migration) require this app to run on "
            "a Windows machine with network access to the SSAS instance/SQL Server, "
            "using an account with the SSAS **Server Administrator** role.\n"
            "- **Fabric steps** use the service principal configured in the "
            "Configuration tab (client ID/secret) - it must be a **member/contributor** "
            "of the target workspace.\n"
            "- Import-mode semantic models need a one-time manual credential binding "
            "in the Fabric portal after `deploy-model` - see README Section 10."
        )

    tab_readme, tab_config, tab_phase1, tab_phase2, tab_reports = st.tabs(
        ["Read Me", "Configuration", "Phase 1: On-Prem", "Phase 2: Fabric", "Reports"]
    )
    with tab_readme:
        render_readme_tab()
    with tab_config:
        render_config_tab()
    with tab_phase1:
        render_phase1_tab()
    with tab_phase2:
        render_phase2_tab()
    with tab_reports:
        render_reports_tab()


if __name__ == "__main__":
    main()
