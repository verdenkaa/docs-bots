"""Initial Runtime state, delivery, access, support and transactional outbox."""
from alembic import op

revision = "001_runtime"
down_revision = None


def upgrade():
    statements = """
CREATE TABLE employees (
 user_id bigint PRIMARY KEY CHECK(user_id>0), enabled boolean NOT NULL,
 role text NOT NULL CHECK(role IN ('employee','operator','admin'))
);
CREATE TABLE rag_requests (
 request_id bigserial PRIMARY KEY, user_id bigint NOT NULL, chat_id bigint NOT NULL,
 update_id bigint NOT NULL UNIQUE, input jsonb NOT NULL, received_at timestamptz NOT NULL,
 prepared_text text, state text NOT NULL DEFAULT 'queued'
 CHECK(state IN ('queued','processing','awaiting_delivery','awaiting_support','finished')),
 pinned_generation bigint CHECK(pinned_generation>=0), result jsonb,
 attempt_no integer NOT NULL DEFAULT 0 CHECK(attempt_no>=0), resume_at timestamptz
);
CREATE UNIQUE INDEX one_active_request ON rag_requests(user_id) WHERE state!='finished';
CREATE INDEX requests_recovery ON rag_requests(state,resume_at);
CREATE INDEX requests_generation ON rag_requests(pinned_generation) WHERE pinned_generation IS NOT NULL;
CREATE INDEX requests_received ON rag_requests(received_at);
CREATE TABLE telegram_updates (
 update_id bigint PRIMARY KEY CHECK(update_id>=0), user_id bigint, chat_id bigint,
 received_at timestamptz NOT NULL, action jsonb, result jsonb NOT NULL
);
CREATE TABLE support_cases (
 case_id uuid PRIMARY KEY, request_id bigint NOT NULL UNIQUE REFERENCES rag_requests,
 user_id bigint NOT NULL, state text NOT NULL CHECK(state IN
 ('waiting','awaiting_confirmation','creating','open','closed','cancelled')),
 reason text NOT NULL CHECK(reason IN ('dissatisfied','human_request','out_of_scope','no_evidence')),
 check_at timestamptz, ticket_url text, created_at timestamptz, closed_at timestamptz,
 history jsonb NOT NULL DEFAULT '[]',
 CHECK ((state IN ('open','closed')) = (ticket_url IS NOT NULL AND created_at IS NOT NULL)),
 CHECK ((state='closed') = (closed_at IS NOT NULL))
);
CREATE INDEX support_due ON support_cases(state,check_at);
CREATE TABLE rag_deliveries (
 delivery_id bigserial PRIMARY KEY, user_id bigint NOT NULL, chat_id bigint NOT NULL,
 request_id bigint REFERENCES rag_requests, case_id uuid REFERENCES support_cases,
 purpose text NOT NULL CHECK(purpose IN ('answer','notice','status','busy','denied','first_question',
 'support_offer','support_confirmation','ticket_link')),
 content jsonb, terminal_for_request boolean NOT NULL DEFAULT false, dedupe_key text UNIQUE,
 parts jsonb, receipts jsonb NOT NULL DEFAULT '[]', next_part integer NOT NULL DEFAULT 0 CHECK(next_part>=0),
 state text NOT NULL DEFAULT 'waiting' CHECK(state IN ('waiting','sending','delivered','failed','unknown','cancelled')),
 attempt_no integer NOT NULL DEFAULT 0 CHECK(attempt_no>=0), resume_at timestamptz,
 delivery_uncertain boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX one_terminal_delivery ON rag_deliveries(request_id) WHERE terminal_for_request;
CREATE UNIQUE INDEX one_wait_status ON rag_deliveries(request_id) WHERE purpose='status';
CREATE INDEX delivery_order ON rag_deliveries(chat_id,delivery_id) WHERE state IN ('waiting','sending');
CREATE INDEX delivery_recovery ON rag_deliveries(state,resume_at);
CREATE TABLE support_messages (
 message_id bigserial PRIMARY KEY, case_id uuid NOT NULL REFERENCES support_cases,
 author_id bigint NOT NULL, author_role text NOT NULL CHECK(author_role IN ('employee','operator','admin')),
 client_message_id uuid NOT NULL, text text NOT NULL CHECK(length(btrim(text))>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(case_id,author_id,client_message_id)
);
CREATE TABLE document_revisions (
 revision_id uuid PRIMARY KEY, document_id uuid NOT NULL, revision_no integer NOT NULL CHECK(revision_no>0),
 source_name text NOT NULL CHECK(length(btrim(source_name))>0), image_ids jsonb NOT NULL DEFAULT '[]',
 staging_ref text, state text NOT NULL DEFAULT 'queued' CHECK(state IN
 ('queued','indexing','published','retired','failed','deleting','cleaned')),
 attempt_no integer NOT NULL DEFAULT 0 CHECK(attempt_no>=0), resume_at timestamptz,
 qdrant_write_pending boolean NOT NULL DEFAULT false,
 expected_chunk_count integer CHECK(expected_chunk_count>0),
 expected_topic_count integer CHECK(expected_topic_count>0), visible_from bigint, visible_until bigint,
 UNIQUE(document_id,revision_no),
 CHECK((expected_chunk_count IS NULL)=(expected_topic_count IS NULL)),
 CHECK(visible_until IS NULL OR visible_until>visible_from)
);
CREATE UNIQUE INDEX one_published_revision ON document_revisions(document_id) WHERE state='published';
CREATE INDEX revisions_recovery ON document_revisions(state,resume_at);
CREATE INDEX revisions_visibility ON document_revisions(visible_from,visible_until);
CREATE TABLE corpus_state (
 singleton_id smallint PRIMARY KEY CHECK(singleton_id=1), generation bigint NOT NULL CHECK(generation>=0),
 embedding_profile text, embedding_dimensions integer
);
INSERT INTO corpus_state(singleton_id,generation) VALUES(1,0);
CREATE TABLE outbox (
 message_id uuid PRIMARY KEY, route text NOT NULL, request_id bigint REFERENCES rag_requests,
 delivery_id bigint REFERENCES rag_deliveries, revision_id uuid REFERENCES document_revisions,
 available_at timestamptz NOT NULL, published_at timestamptz,
 CHECK((route='rag.questions' AND request_id IS NOT NULL AND delivery_id IS NULL AND revision_id IS NULL)
 OR (route='rag.results' AND request_id IS NULL AND delivery_id IS NOT NULL AND revision_id IS NULL)
 OR (route='ingestion.jobs' AND request_id IS NULL AND delivery_id IS NULL AND revision_id IS NOT NULL))
);
CREATE INDEX outbox_ready ON outbox(available_at) WHERE published_at IS NULL;
CREATE INDEX outbox_questions ON outbox(request_id,route,published_at);
CREATE INDEX outbox_deliveries ON outbox(delivery_id,route,published_at);
CREATE INDEX outbox_revisions ON outbox(revision_id,route,published_at)
"""
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for name in ("outbox", "corpus_state", "document_revisions", "support_messages", "rag_deliveries",
                 "support_cases", "telegram_updates", "rag_requests", "employees"):
        op.execute(f"DROP TABLE {name}")
