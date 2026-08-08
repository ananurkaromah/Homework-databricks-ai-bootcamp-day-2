"""
setup_secrets.py
-----------------
One-time helper to store the Lakebase connection URL as a Databricks secret.

The deployed app (via lakebase.py) reads this secret at runtime through
LAKEBASE_SECRET_SCOPE / LAKEBASE_SECRET_KEY - no credentials are ever
committed to source control.

Run once, after provisioning the Lakebase instance and creating a
native-password role, from a terminal with Databricks auth configured
(or from a Databricks notebook):

    python setup_secrets.py

Safe to re-run: creating an already-existing scope is treated as a no-op,
and storing a secret under the same key overwrites the previous value.

Project specifics for the Weather Intelligence app:
    name_role     = weather_app
    lakebase_name = weather_documents      (Lakebase instance name)
    database      = databricks_postgres
"""

import base64
import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import AclPermission

# Must match lakebase.py's LAKEBASE_SECRET_SCOPE / LAKEBASE_SECRET_KEY defaults.
SECRET_SCOPE = "database"
SECRET_KEY = "lakebase-url"

# Lakebase / role specifics for this project.
LAKEBASE_INSTANCE_NAME = "weather_documents"
ROLE_NAME = "weather_app"
DATABASE_NAME = "databricks_postgres"
DEFAULT_PORT = "5432"


def prompt_for_connection_details() -> str:
    """
    Interactively prompt for the Lakebase host and the weather_app role's
    password, then assemble the full native-password connection URL.

    The role is authenticated via a plain Postgres password (NOT
    Databricks OAuth), per the assignment spec, so the URL embeds
    role + password directly.
    """
    print(f"Lakebase instance: {LAKEBASE_INSTANCE_NAME}")
    print(f"Role: {ROLE_NAME}  |  Database: {DATABASE_NAME}\n")

    host = input(
        "Lakebase Postgres host "
        "(e.g. instance-xxxxxxxx.database.cloud.databricks.com): "
    ).strip()
    port = input(f"Port [{DEFAULT_PORT}]: ").strip() or DEFAULT_PORT
    password = getpass.getpass(f"Password for role '{ROLE_NAME}': ")

    if not host:
        raise ValueError("Host is required.")
    if not password:
        raise ValueError("Password is required.")

    return (
        f"postgresql://{ROLE_NAME}:{password}@{host}:{port}/{DATABASE_NAME}"
        "?sslmode=require"
    )


def ensure_scope(client: WorkspaceClient, scope: str) -> None:
    """Create the secret scope if it doesn't already exist (no-op otherwise)."""
    existing_scopes = {s.name for s in client.secrets.list_scopes()}
    if scope in existing_scopes:
        print(f"Secret scope '{scope}' already exists — skipping creation.")
        return
    client.secrets.create_scope(scope=scope)
    print(f"Created secret scope '{scope}'.")


def grant_read_to_users(client: WorkspaceClient, scope: str) -> None:
    """
    Grant READ on the scope to the 'users' principal, matching the
    existing project's ACL pattern, so any user of the deployed app can
    resolve the secret at runtime.
    """
    client.secrets.put_acl(scope=scope, principal="users", permission=AclPermission.READ)
    print(f"Granted READ on scope '{scope}' to principal 'users'.")


def store_secret(client: WorkspaceClient, scope: str, key: str, value: str) -> None:
    """
    Store the connection URL as a secret. Values are stored base64-encoded
    by convention with lakebase.py's _resolve_database_url(), which
    base64-decodes on read.
    """
    encoded = base64.b64encode(value.encode("utf-8")).decode("utf-8")
    client.secrets.put_secret(scope=scope, key=key, string_value=encoded)
    print(f"Stored secret '{scope}/{key}' (overwritten if it already existed).")


def main():
    client = WorkspaceClient()

    ensure_scope(client, SECRET_SCOPE)
    grant_read_to_users(client, SECRET_SCOPE)

    database_url = prompt_for_connection_details()
    store_secret(client, SECRET_SCOPE, SECRET_KEY, database_url)

    print("\nDone. lakebase.py will resolve this secret via:")
    print(f"  LAKEBASE_SECRET_SCOPE = {SECRET_SCOPE!r}")
    print(f"  LAKEBASE_SECRET_KEY   = {SECRET_KEY!r}")
    print(
        "\nFor local dev/testing, you can instead export DATABASE_URL directly "
        "and skip Databricks secrets entirely — lakebase.py checks that first."
    )


if __name__ == "__main__":
    main()