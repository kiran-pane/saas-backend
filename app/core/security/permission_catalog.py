"""Single source of truth for the platform's permission catalog.

`alembic/versions/0001_initial_schema.py` inserts this same list once as
part of the baseline schema migration (deliberately as literal SQL —
migrations should never import application code, so they can't break
when the code changes). `app/seeds/permissions.py` uses THIS module to
idempotently sync the catalog afterwards, which is how you add a new
permission going forward: add a tuple here, run the seed script, done —
no new migration needed for what is reference data, not a schema change.

Format: (code, resource, action, description)
"""

PERMISSION_CATALOG: list[tuple[str, str, str, str]] = [
    ("rbac.manage", "rbac", "manage", "Create/edit roles, permissions, and role assignments"),
    ("users.read", "users", "read", "View users in the tenant"),
    ("users.write", "users", "write", "Create/edit users in the tenant"),
    ("documents.read", "documents", "read", "View documents"),
    ("documents.write", "documents", "write", "Upload/ingest documents"),
    ("billing.invoice.read", "billing.invoice", "read", "View invoices"),
    ("billing.invoice.delete", "billing.invoice", "delete", "Delete invoices"),
]

# Permissions automatically granted to every tenant's seeded "Member" role.
# Intentionally read-only / low-privilege — anything else must be granted
# explicitly by an Owner/admin via the RBAC console.
DEFAULT_MEMBER_PERMISSION_CODES: set[str] = {"users.read", "documents.read", "billing.invoice.read"}
