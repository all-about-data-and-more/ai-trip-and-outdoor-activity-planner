"""
One-time setup: store the Lakebase connection URL as a Databricks secret.

This is the ONLY secret the Trip Planner needs — every third-party data source
(Open-Meteo geocoding / weather / air quality, and Wikimedia) is key-less for
non-commercial use, so there's nothing else to configure.

Run locally (with the Databricks CLI configured) or from a notebook. Never
commit the resulting secret value anywhere.

    python setup_secrets.py
"""

import base64
import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

w = WorkspaceClient()

SCOPE = "database"
KEY = "lakebase-url"

# Create the scope if it doesn't exist yet (ignore "already exists").
try:
    w.secrets.create_scope(scope=SCOPE)
except Exception as exc:  # noqa: BLE001 - scope may already exist; that's fine
    print(f"(scope {SCOPE!r} already exists or could not be created: {exc})")

# lakebase.py base64-decodes this secret, so store the URL base64-encoded —
# same encoding scheme the rest of the bootcamp apps use.
raw_url = getpass.getpass("Paste your Lakebase URL (postgresql://...): ").strip()
w.secrets.put_secret(
    scope=SCOPE,
    key=KEY,
    string_value=base64.b64encode(raw_url.encode("utf-8")).decode("utf-8"),
)
print(f"Stored {SCOPE}/{KEY}.")

# Let workspace users (the app's service principal) read it.
w.secrets.put_acl(
    scope=SCOPE,
    principal="users",
    permission=workspace.AclPermission.READ,
)
print("Granted READ on the secret scope to 'users'. Done.")
