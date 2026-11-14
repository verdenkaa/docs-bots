"""Document names, access metadata and reserved versions, preserving existing IDs.

Existing revisions require an explicit ingestion_legacy_access_level Alembic
Config attribute, or INGESTION_LEGACY_ACCESS_LEVEL in the migration environment.
Ambiguous names/renames are rejected before DDL; an operator must resolve them.
This migration does not implement the reserve/publish transitions.
"""

import os

from alembic import op
import sqlalchemy as sa


revision = "004_ingestion_documents"
down_revision = "003_model_calls"


def _legacy_plan(rows, access_level):
    if rows and (not isinstance(access_level, str) or not access_level.strip()):
        raise RuntimeError("Existing revisions require explicit ingestion_legacy_access_level")
    documents, names, revisions = {}, {}, []
    for row in rows:
        document_id, name = row["document_id"], row["source_name"]
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("Invalid legacy document name")
        if name in names and names[name] != document_id:
            raise RuntimeError("Duplicate document names require operator resolution")
        if document_id in documents and documents[document_id]["source_name"] != name:
            raise RuntimeError("Legacy document renames require operator resolution")
        names[name] = document_id
        document = documents.setdefault(document_id, dict(
            document_id=document_id, source_name=name, current_revision_id=None,
            current_version=None, _next_version=0, _generation=None,
        ))
        published = row["visible_from"] is not None
        version = base = None
        if published or row["expected_chunk_count"] is not None:
            version = document["_next_version"]
            base = version - 1 if version else None
        if published:
            if document["_generation"] is not None and row["visible_from"] <= document["_generation"]:
                raise RuntimeError("Inconsistent legacy publication order")
            document["_generation"] = row["visible_from"]
            document["_next_version"] += 1
        if row["state"] == "published":
            if not published or document["current_revision_id"] is not None:
                raise RuntimeError("Inconsistent legacy current revision")
            document["current_revision_id"] = row["revision_id"]
            document["current_version"] = version
        revisions.append(dict(revision_id=row["revision_id"], access_level=access_level,
                              document_version=version, base_version=base))
    return ([{key: value for key, value in document.items() if not key.startswith("_")}
             for document in documents.values()], revisions)


def upgrade():
    connection = op.get_bind()
    legacy = sa.table("document_revisions", *[
        sa.column(name, kind) for name, kind in (
            ("revision_id", sa.Uuid), ("document_id", sa.Uuid),
            ("revision_no", sa.Integer), ("source_name", sa.Text),
            ("state", sa.Text), ("visible_from", sa.BigInteger),
            ("expected_chunk_count", sa.Integer),
        )
    ])
    rows = connection.execute(sa.select(legacy).order_by(
        legacy.c.document_id, legacy.c.revision_no)).mappings().all()
    level = op.get_context().config.attributes.get(
        "ingestion_legacy_access_level", os.environ.get("INGESTION_LEGACY_ACCESS_LEVEL")
    )
    documents, revisions = _legacy_plan(rows, level)
    table = op.create_table(
        "documents",
        sa.Column("document_id", sa.Uuid, primary_key=True),
        sa.Column("source_name", sa.Text(collation="C"), nullable=False),
        sa.Column("current_revision_id", sa.Uuid, nullable=True),
        sa.Column("current_version", sa.Integer, nullable=True),
        sa.UniqueConstraint("source_name", name="documents_source_name_key"),
        sa.CheckConstraint("length(btrim(source_name))>0", name="documents_source_name_nonempty"),
        sa.CheckConstraint("current_version IS NULL OR current_version>=0", name="documents_version_nonnegative"),
        sa.CheckConstraint("(current_revision_id IS NULL)=(current_version IS NULL)", name="documents_current_pair"),
    )
    op.add_column("document_revisions", sa.Column("access_level", sa.Text, nullable=True))
    op.add_column("document_revisions", sa.Column("document_version", sa.Integer, nullable=True))
    op.add_column("document_revisions", sa.Column("base_version", sa.Integer, nullable=True))
    if documents:
        connection.execute(sa.insert(table), documents)
    target = sa.table("document_revisions", sa.column("revision_id", sa.Uuid),
                      sa.column("access_level", sa.Text), sa.column("document_version", sa.Integer),
                      sa.column("base_version", sa.Integer))
    for item in revisions:
        connection.execute(sa.update(target).where(target.c.revision_id == item["revision_id"]).values(
            **{key: value for key, value in item.items() if key != "revision_id"}))
    op.alter_column("document_revisions", "access_level", nullable=False)
    op.create_check_constraint("revisions_access_nonempty", "document_revisions",
                               "length(btrim(access_level))>0")
    op.create_check_constraint(
        "revisions_reserved_version", "document_revisions",
        "(document_version IS NULL AND base_version IS NULL) OR "
        "(document_version IS NOT NULL AND document_version>=0 AND "
        "((base_version IS NULL AND document_version=0) OR "
        "(base_version IS NOT NULL AND base_version>=0 AND document_version=base_version+1)))",
    )
    op.create_check_constraint("revisions_counts_require_version", "document_revisions",
                               "expected_chunk_count IS NULL OR document_version IS NOT NULL")
    op.create_index("revisions_published_version", "document_revisions",
                    ["document_id", "document_version"], unique=True,
                    postgresql_where=sa.column("visible_from").is_not(None))
    op.create_foreign_key("revisions_document_fk", "document_revisions", "documents",
                          ["document_id"], ["document_id"])
    op.create_foreign_key("documents_current_revision_fk", "documents", "document_revisions",
                          ["current_revision_id"], ["revision_id"])


def downgrade():
    raise RuntimeError("Removing ingestion metadata requires an operator-managed restore")
