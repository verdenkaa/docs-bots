"""Read models over persisted facts. No changes to application tables or DTOs."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = "rag_analytics"
READER = "rag_analytics_reader"


def identifier(value):
    if not value or "\x00" in value:
        raise ValueError("Invalid SQL identifier")
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    # Setup explicitly enables standard_conforming_strings before running DDL.
    if "\x00" in value:
        raise ValueError("Invalid SQL value")
    return "'" + value.replace("'", "''") + "'"


def topics():
    values = json.loads((HERE / "topics.json").read_text(encoding="utf-8"))
    names = set()
    for item in values:
        if (not isinstance(item.get("name"), str) or not item["name"].strip()
                or item["name"] in names or not item.get("keywords")
                or any(not isinstance(word, str) or not word.strip() for word in item["keywords"])):
            raise ValueError("Each topic needs a unique name and nonempty keywords")
        names.add(item["name"])
    return values


def views(application_schema="public"):
    source = identifier(application_schema)
    # One topic per question, first matching rule wins. Unmatched questions remain visible.
    rules = "\n".join(
        "WHEN " + " OR ".join(f"strpos(question, {literal(word.casefold())}) > 0" for word in item["keywords"])
        + " THEN " + literal(item["name"]) for item in topics()
    )
    return f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE OR REPLACE VIEW {SCHEMA}.live_requests AS
WITH facts AS (
  SELECT r.request_id, r.received_at, r.channel,
    lower(regexp_replace(btrim(coalesce(nullif(r.prepared_text, ''),
      r.input->>'text', r.input->>'caption', '[вопрос без текста]')), '\\s+', ' ', 'g')) AS question,
    r.result->>'kind' AS result_kind, r.result->>'code' AS result_code,
    coalesce(r.result->>'kind' = 'support' OR c.state IN ('creating','open','closed'), false) AS escalated,
    coalesce(c.reason, r.result->>'reason') AS support_reason,
    c.closed_at,
    EXISTS (SELECT 1 FROM {source}.telegram_updates u
      WHERE u.action->>'kind' = 'offer' AND u.action->>'reason' = 'dissatisfied'
        AND u.action->>'request_id' = r.request_id::text AND u.result->>'kind' = 'handled')
      OR coalesce(c.reason = 'dissatisfied', false) AS dissatisfied,
    CASE WHEN r.channel = 'web' THEN w.completed_at ELSE delivery.reply_at END AS reply_at,
    CASE WHEN r.channel = 'web' THEN w.completed_at IS NOT NULL ELSE coalesce(delivery.delivered, false) END AS answer_available
  FROM {source}.rag_requests r
  LEFT JOIN {source}.support_cases c ON c.request_id = r.request_id
  LEFT JOIN {source}.web_requests w ON w.request_id = r.request_id
  LEFT JOIN LATERAL (
    SELECT min((receipt->>'confirmed_at')::timestamptz) FILTER (
        WHERE receipt->>'variant' = 'normal' AND receipt->>'delivered_text' IS NOT NULL) AS reply_at,
      bool_or(d.state = 'delivered') AND bool_and(receipt->>'variant' = 'normal') AS delivered
    FROM {source}.rag_deliveries d
    LEFT JOIN LATERAL jsonb_array_elements(d.receipts) receipt ON true
    WHERE d.request_id = r.request_id AND d.purpose = 'answer'
  ) delivery ON true
)
SELECT 'live'::text AS data_mode, request_id, received_at, channel, question,
  CASE {rules} ELSE 'Другое / не определено' END AS topic,
  result_kind, result_code, escalated, support_reason, dissatisfied,
  coalesce(result_kind = 'answered' AND answer_available AND reply_at IS NOT NULL
    AND NOT escalated AND NOT dissatisfied, false) AS auto_answer,
  CASE WHEN result_kind = 'answered' AND reply_at >= received_at
    THEN extract(epoch FROM reply_at - received_at) * 1000 END::double precision AS reply_ms,
  CASE WHEN closed_at >= received_at
    THEN extract(epoch FROM closed_at - received_at) * 1000 END::double precision AS resolution_ms
FROM facts;

-- Synthetic rows are a view: no inserts into application tables, queues or logs.
-- Relative to UTC midnight; 28 complete days, stable within a day and on restart.
CREATE OR REPLACE VIEW {SCHEMA}.demo_requests AS
WITH samples(topic, question, base_count, recent_count, failure_percent) AS (VALUES
  ('Командировки', 'как оформить командировку?', 24, 30, 12),
  ('Командировки', 'как получить суточные и оформить авансовый отчет?', 9, 13, 30),
  ('Реестр мебели', 'как добавить мебель в реестр?', 12, 14, 10),
  ('Реестр мебели', 'как списать мебель после инвентаризации?', 4, 19, 67),
  ('БАРС Офис · согласование', 'как изменить маршрут согласования документа?', 6, 22, 72),
  ('БАРС Офис · согласование', 'почему документ завис на согласовании?', 7, 16, 55),
  ('БАРС Офис · доступ', 'как восстановить пароль для входа?', 15, 14, 6),
  ('БАРС Офис · документы', 'как добавить вложение в карточку документа?', 16, 18, 8),
  ('Другое / не определено', 'где посмотреть контакты поддержки?', 3, 4, 50)
), expanded AS (
  SELECT *, row_number() OVER (ORDER BY day_no, topic, question, n)::bigint AS request_id,
    (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
      - day_no * interval '1 day' + (n * 839 % 86400) * interval '1 second' AS received_at,
    (n * 37 + day_no * 11) % 100 AS score
  FROM samples CROSS JOIN generate_series(1, 28) AS day_no
  CROSS JOIN LATERAL generate_series(1, CASE WHEN day_no <= 7 THEN recent_count ELSE base_count END
      + day_no % 4) AS n
), outcomes AS (
  SELECT *, CASE WHEN score < 3 THEN 'notice'
    WHEN score < failure_percent THEN 'support' ELSE 'answered' END AS result_kind,
    score >= failure_percent AND score BETWEEN 90 AND 94 AS manual,
    CASE WHEN score % 3 = 0 THEN 'web' ELSE 'telegram' END AS channel
  FROM expanded
)
SELECT 'demo'::text AS data_mode, request_id, received_at, channel, question, topic,
  result_kind, CASE WHEN result_kind = 'notice' THEN 'technical_error' END::text AS result_code,
  result_kind = 'support' OR manual AS escalated,
  CASE WHEN manual THEN 'dissatisfied' WHEN result_kind = 'support'
    THEN CASE WHEN score % 4 = 0 THEN 'out_of_scope' ELSE 'no_evidence' END END::text AS support_reason,
  manual AS dissatisfied,
  result_kind = 'answered' AND NOT manual AS auto_answer,
  CASE WHEN result_kind = 'answered' THEN (1800 + score * 310)::double precision END AS reply_ms,
  CASE WHEN (result_kind = 'support' OR manual) AND day_no > 1 AND score % 3 <> 0
    THEN (720000 + score * 75000)::double precision END AS resolution_ms
FROM outcomes;

CREATE OR REPLACE VIEW {SCHEMA}.requests AS
  SELECT * FROM {SCHEMA}.live_requests UNION ALL SELECT * FROM {SCHEMA}.demo_requests;
"""
