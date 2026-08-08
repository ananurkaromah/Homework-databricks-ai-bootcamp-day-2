"""
setup_secrets.py
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
"""
import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

# Must match LAKEBASE_SECRET_SCOPE / LAKEBASE_SECRET_KEY in app.yaml and lakebase.py.
SCOPE = "database"
KEY = "lakebase-url"

URL_PROMPT = (
    "Paste your Lakebase connection URL "
    "(postgresql://<role>:<password>@<host>.database.cloud.databricks.com:5432/"
    "databricks_postgres?sslmode=require): "
)


def ensure_scope(w: WorkspaceClient, scope: str) -> None:
    """Create the secret scope if it doesn't already exist."""
    try:
        w.secrets.create_scope(scope=scope)
        print(f"Created secret scope '{scope}'.")
    except Exception as e:
        print(f"Scope '{scope}' already exists (or could not be created): {e}")


def store_connection_url(w: WorkspaceClient, scope: str, key: str) -> None:
    """
    Prompt for the Lakebase connection URL and store it as a secret.

    Stored as plain text via string_value — do NOT base64-encode this value
    yourself. The Databricks Secrets API always base64-encodes `value` on
    every get_secret() response regardless of how it was written; lakebase.py
    reverses that single encoding layer on read. Encoding here too would
    double-encode the value and break the deployed app's connection.
    """
    url = getpass.getpass(URL_PROMPT)
    w.secrets.put_secret(scope=scope, key=key, string_value=url)
    print(f"Stored secret '{scope}/{key}'.")


def grant_read_access(w: WorkspaceClient, scope: str) -> None:
    """Let the app's runtime identity (and other workspace users) read the secret."""
    w.secrets.put_acl(
        scope=scope, principal="users", permission=workspace.AclPermission.READ
    )
    print("Granted READ to 'users'.")


def main() -> None:
    w = WorkspaceClient()
    ensure_scope(w, SCOPE)
    store_connection_url(w, SCOPE, KEY)
    grant_read_access(w, SCOPE)
    print("Done.")


if __name__ == "__main__":
    main()