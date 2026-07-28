"""
Lightweight web UI for the SSAS -> Fabric migration tool.

Run with:
    streamlit run ssas_fabric_migrator/ui/app.py

This is a thin wrapper around the existing CLI (`cli/orchestrator.py`) and
its underlying modules - it does not duplicate any pipeline logic. It just
gives users a form-based way to configure connections and click through
each step instead of typing CLI commands, with live log output and a
built-in viewer for the generated reports.

DEPLOYMENT NOTE (see README Section 11 for the full write-up):
This app must run on a Windows host with:
  - line-of-sight network access to the on-prem SSAS instance and SQL
    Server (same domain/VPN as required by the CLI today), and
  - an x64 Python interpreter with pythonnet/pandas/pyarrow/deltalake
    installed (Windows ARM64 has no prebuilt wheels for these - see
    requirements.txt).
"Any device" access is achieved by hosting this single app once on such a
host and letting users reach it via browser (see README Section 11) - the
UI itself does not need to be installed on every analyst's machine.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import streamlit as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    "extract": "1. Extract cube metadata (AMO)",
    "analyze": "2. Analyze Direct Lake feasibility",
    "generate": "3. Generate TMDL + notebook scripts",
    "report": "4. Generate MIGRATION_REPORT.md",
    "deploy-lake": "5. Deploy/find Lakehouse",
    "migrate-data": "6. Migrate data (SQL Server -> OneLake, single host)",
    "upload-data": "6b. Upload local Delta export -> OneLake (air-gapped)",
    "deploy-model": "7. Deploy semantic model",
}

PHASE1_STEPS = ["extract", "analyze", "generate", "report"]
PHASE2_STEPS = ["deploy-lake", "migrate-data", "deploy-model"]


def init_state():
    st.session_state.setdefault("env_file", os.path.join("config", ".env"))
    st.session_state.setdefault("output_dir", "output")
    st.session_state.setdefault("lakehouse_name", "RetailLakehouse")
    st.session_state.setdefault("semantic_model_name", "RetailCubeDemo")
    st.session_state.setdefault("local_delta_dir", "")
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


def run_steps(steps: list[str], log_area):
    """Run the given orchestrator steps as a subprocess, streaming output live."""
    env_file = st.session_state["env_file"]
    cmd = [
        st.session_state["python_exe"], "-m", "ssas_fabric_migrator.cli.orchestrator",
        "--steps", ",".join(steps),
        "--env-file", env_file,
        "--output-dir", st.session_state["output_dir"],
        "--lakehouse-name", st.session_state["lakehouse_name"],
        "--semantic-model-name", st.session_state["semantic_model_name"],
    ]
    if "upload-data" in steps and st.session_state["local_delta_dir"]:
        cmd += ["--local-delta-dir", st.session_state["local_delta_dir"]]

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

    if proc.returncode == 0:
        st.success(f"Steps [{', '.join(steps)}] completed successfully.")
    else:
        st.error(f"Steps [{', '.join(steps)}] failed with exit code {proc.returncode}. See log above.")


def render_config_tab():
    st.subheader("Connection configuration")
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
        if st.button("Load from file"):
            try:
                load_env_file(st.session_state["env_file"])
                st.success("Loaded.")
            except Exception as e:
                st.error(f"Could not load: {e}")

    with st.form("env_form"):
        for key, label, is_secret, _default in ENV_FIELDS:
            st.session_state["env_values"][key] = st.text_input(
                label, value=st.session_state["env_values"].get(key, ""),
                type="password" if is_secret else "default", key=f"field_{key}",
            )
        submitted = st.form_submit_button("Save to env file")
        if submitted:
            try:
                save_env_file(st.session_state["env_file"])
                st.success(f"Saved to {st.session_state['env_file']}")
            except Exception as e:
                st.error(f"Could not save: {e}")

    st.divider()
    st.subheader("Run settings")
    st.session_state["output_dir"] = st.text_input("Output directory", value=st.session_state["output_dir"])
    st.session_state["lakehouse_name"] = st.text_input("Fabric Lakehouse name", value=st.session_state["lakehouse_name"])
    st.session_state["semantic_model_name"] = st.text_input(
        "Fabric Semantic Model name", value=st.session_state["semantic_model_name"]
    )
    st.session_state["python_exe"] = st.text_input(
        "Python executable to run pipeline steps with",
        value=st.session_state["python_exe"],
        help=(
            "Use an x64 interpreter with pythonnet/pandas/pyarrow/deltalake installed. "
            "On Windows ARM64, the default 'python' on PATH usually resolves to an ARM64 "
            "interpreter lacking these - point this at your x64 python.exe instead."
        ),
    )


def render_phase_tab(title, steps, key_prefix):
    st.subheader(title)
    log_area = st.empty()
    cols = st.columns(len(steps) + 1)
    for i, step in enumerate(steps):
        if cols[i].button(STEP_LABELS[step], key=f"{key_prefix}_{step}"):
            run_steps([step], log_area)
    if cols[-1].button(f"Run all: {title}", key=f"{key_prefix}_all", type="primary"):
        run_steps(steps, log_area)


def render_upload_data_tab():
    st.subheader("6b. Upload local Delta export (air-gapped bridge)")
    st.caption(
        "Use this instead of 'Migrate data' when the same machine cannot reach both "
        "SQL Server and Fabric. Requires Delta tables already exported locally via "
        "`python -m ssas_fabric_migrator.datamover.loader --target local`."
    )
    st.session_state["local_delta_dir"] = st.text_input(
        "Local Delta export folder", value=st.session_state["local_delta_dir"]
    )
    log_area = st.empty()
    if st.button("Upload to OneLake", key="upload_data_btn"):
        if not st.session_state["local_delta_dir"]:
            st.error("Set the local Delta export folder first.")
        else:
            run_steps(["upload-data"], log_area)


def render_reports_tab():
    st.subheader("Generated artifacts")
    out = st.session_state["output_dir"]
    full_out = out if os.path.isabs(out) else os.path.join(REPO_ROOT, out)

    report_path = os.path.join(full_out, "MIGRATION_REPORT.md")
    feasibility_path = os.path.join(full_out, "feasibility_report.json")
    manual_path = os.path.join(full_out, "SemanticModel", "..", "MANUAL_TRANSLATION_REQUIRED.md")

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
        "Wraps the existing CLI/orchestrator - see README.md for full step-by-step docs, "
        "prerequisites, and limitations."
    )

    with st.sidebar:
        st.header("About this host")
        st.markdown(
            "- **On-prem steps** (Extract, Migrate data) require this app to run on a "
            "Windows machine with network access to the SSAS instance/SQL Server, "
            "using an account with the SSAS **Server Administrator** role.\n"
            "- **Fabric steps** use the service principal configured in the "
            "Configuration tab (client ID/secret) - it must be a **member/contributor** "
            "of the target workspace.\n"
            "- Import-mode semantic models need a one-time manual credential binding "
            "in the Fabric portal after `deploy-model` - see README Section 10."
        )

    tab_config, tab_phase1, tab_phase2, tab_upload, tab_reports = st.tabs(
        ["Configuration", "Phase 1: On-Prem", "Phase 2: Fabric", "Air-gapped upload", "Reports"]
    )
    with tab_config:
        render_config_tab()
    with tab_phase1:
        render_phase_tab("Phase 1: On-Prem (SSAS + SQL Server, no Fabric connectivity needed)", PHASE1_STEPS, "p1")
    with tab_phase2:
        render_phase_tab("Phase 2: Fabric-connected (no SSAS connectivity needed)", PHASE2_STEPS, "p2")
    with tab_upload:
        render_upload_data_tab()
    with tab_reports:
        render_reports_tab()


if __name__ == "__main__":
    main()
