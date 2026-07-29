"""
Minimal Fabric REST API client covering just what this migration tool needs:
 - authenticate as a service principal
 - create/find a Lakehouse in the target workspace
 - resolve the Lakehouse's SQL analytics endpoint (for the TMDL Direct Lake
   / Import expression)
 - create a Semantic Model item from a TMDL definition folder

Reference: https://learn.microsoft.com/rest/api/fabric/
"""
from __future__ import annotations

import base64
import os
import time

import requests
from azure.identity import ClientSecretCredential

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


class FabricClient:
    def __init__(self, tenant_id, client_id, client_secret):
        self.credential = ClientSecretCredential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )

    def _headers(self):
        token = self.credential.get_token(FABRIC_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get(self, path):
        r = requests.get(f"{FABRIC_API_BASE}{path}", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def _post(self, path, json_body=None):
        r = requests.post(f"{FABRIC_API_BASE}{path}", headers=self._headers(), json=json_body)
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(f"POST {path} failed: {r.status_code} {r.text}")
        return r

    def delete_item(self, workspace_id, item_id):
        """
        Deletes a Fabric item outright (e.g. a Semantic Model). Needed
        because updateDefinition cannot change a table's storage mode
        in-place (Fabric error Dataset_Import_FailedToImportDataset /
        "You cannot change the storage mode of partition ... Converting
        existing tables or partitions from Direct Lake to other storage
        modes is not supported"). When a re-run's feasibility
        recommendation flips DirectLake<->Import for a cube that was
        already deployed, delete the existing item first so
        create_semantic_model recreates it from scratch.
        """
        r = requests.delete(f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}", headers=self._headers())
        if r.status_code not in (200, 204):
            raise RuntimeError(f"DELETE /workspaces/{workspace_id}/items/{item_id} failed: {r.status_code} {r.text}")

    def list_items(self, workspace_id, item_type):
        """Returns every item of the given type (e.g. 'Lakehouse') in the workspace - used by the
        UI to let a user pick an existing Lakehouse from a dropdown instead of typing its exact name."""
        items = self._get(f"/workspaces/{workspace_id}/items")
        return [item for item in items.get("value", []) if item.get("type") == item_type]

    def find_item(self, workspace_id, display_name, item_type):
        items = self._get(f"/workspaces/{workspace_id}/items")
        for item in items.get("value", []):
            if item.get("displayName") == display_name and item.get("type") == item_type:
                return item
        return None

    def create_lakehouse(self, workspace_id, name):
        existing = self.find_item(workspace_id, name, "Lakehouse")
        if existing:
            return existing
        r = self._post(f"/workspaces/{workspace_id}/lakehouses", {"displayName": name})
        return self._await_lro_or_body(r, workspace_id, name, "Lakehouse")

    def _await_lro_or_body(self, response, workspace_id, name, item_type):
        if response.status_code == 202:
            # Long-running operation - poll the operation location, then re-fetch the item.
            op_url = response.headers.get("Location")
            for _ in range(60):
                time.sleep(5)
                status = requests.get(op_url, headers=self._headers()).json()
                if status.get("status") == "Succeeded":
                    break
                if status.get("status") == "Failed":
                    raise RuntimeError(f"Fabric operation failed: {status}")
            return self.find_item(workspace_id, name, item_type)
        return response.json()

    def get_lakehouse_sql_endpoint(self, workspace_id, lakehouse_id):
        details = self._get(f"/workspaces/{workspace_id}/lakehouses/{lakehouse_id}")
        props = details.get("properties", {})
        endpoint = props.get("sqlEndpointProperties", {}).get("connectionString")
        return endpoint

    def _encode_definition_parts(self, tmdl_folder):
        parts = []
        for root, _dirs, files in os.walk(tmdl_folder):
            for fname in files:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, tmdl_folder).replace("\\", "/")
                with open(full_path, "rb") as f:
                    payload = base64.b64encode(f.read()).decode("ascii")
                parts.append({"path": rel_path, "payload": payload, "payloadType": "InlineBase64"})
        return parts

    def create_semantic_model(self, workspace_id, name, tmdl_folder):
        existing = self.find_item(workspace_id, name, "SemanticModel")
        parts = self._encode_definition_parts(tmdl_folder)
        body = {"displayName": name, "definition": {"parts": parts}}
        if existing:
            r = self._post(f"/workspaces/{workspace_id}/items/{existing['id']}/updateDefinition", body)
            try:
                return self._await_lro_or_body(r, workspace_id, name, "SemanticModel")
            except RuntimeError as e:
                # Fabric cannot change a table's storage mode (DirectLake <->
                # Import) via updateDefinition on an existing item - see
                # delete_item's docstring. If that's what failed, delete and
                # recreate instead of leaving the item in a broken state.
                if "cannot change the storage mode" in str(e).lower():
                    print(f"    Existing semantic model has a different storage mode; deleting and recreating '{name}' ...")
                    self.delete_item(workspace_id, existing["id"])
                    r = self._post(f"/workspaces/{workspace_id}/semanticModels", body)
                    return self._await_lro_or_body(r, workspace_id, name, "SemanticModel")
                raise
        else:
            r = self._post(f"/workspaces/{workspace_id}/semanticModels", body)
            return self._await_lro_or_body(r, workspace_id, name, "SemanticModel")



if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fabric deployment: create Lakehouse + Semantic Model")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--lakehouse-name", required=True)
    parser.add_argument("--semantic-model-tmdl", required=True)
    parser.add_argument("--semantic-model-name", required=True)
    args = parser.parse_args()

    client = FabricClient(
        tenant_id=os.environ["FABRIC_TENANT_ID"],
        client_id=os.environ["FABRIC_CLIENT_ID"],
        client_secret=os.environ["FABRIC_CLIENT_SECRET"],
    )

    lh = client.create_lakehouse(args.workspace_id, args.lakehouse_name)
    print(f"Lakehouse: {lh}")

    endpoint = client.get_lakehouse_sql_endpoint(args.workspace_id, lh["id"])
    print(f"SQL endpoint: {endpoint}")

    sm = client.create_semantic_model(args.workspace_id, args.semantic_model_name, args.semantic_model_tmdl)
    print(f"Semantic model: {sm}")
