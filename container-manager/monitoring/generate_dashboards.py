"""Build the versioned Grafana JSON. Python stdlib; no infrastructure access."""
import json
import runpy
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
DS = {"type": "loki", "uid": "rag-loki"}
BASE = ('{service=~"${service:regex}",service!=""} | json | __error__="" '
        '| component=~"${component:regex}" | route=~"${route:regex}" '
        '| label_format operation_filter=`{{if eq .operation "step"}}{{.parent_operation}}{{else}}{{.operation}}{{end}}` '
        '| operation_filter=~"${operation:regex}"')


def logs(filters=""):
    return BASE + ' | event="finished"' + (" | " + filters if filters else "")


def count(source, window="$__range", group=""):
    return f'sum{f" by ({group})" if group else ""}(count_over_time({source}[{window}]))'


def unique(source, key="request_id", window="$__range", group=""):
    labels = f"{key},{group}" if group else key
    return f'count{f" by ({group})" if group else ""}(sum by ({labels})(count_over_time({source} | {key}!=""[{window}])))'


def ratio(numerator, denominator):
    # A missing numerator means zero ONLY when the denominator was observed.
    return f'100 * (({numerator}) or (0 * ({denominator}))) / ({denominator})'


def quantile(source, field="duration_ms", q=.95, window="$__interval", group=""):
    return f'quantile_over_time({q}, {source} | {field}!="" | unwrap {field} | __error__=""[{window}]) by ({group})'


def explore(expr):
    state = {"datasource": "rag-loki", "queries": [{"refId": "A", "expr": expr,
             "queryType": "range", "datasource": DS}],
             "range": {"from": "${__from}", "to": "${__to}"}}
    # Keep Grafana variables interpolatable; quote only URL control characters.
    return '/explore?orgId=1&left=' + quote(json.dumps(state, separators=(",", ":")), safe="${}:")


class Dashboard:
    def __init__(self, uid, title, description):
        self.uid, self.title, self.description = uid, title, description
        self.panels, self.y, self.x = [], 0, 0

    def panel(self, title, expressions=None, *, kind="timeseries", width=12, height=8,
              unit="short", description="", source=None, thresholds=None, legends=None):
        if self.x + width > 24:
            self.y += self.line_height
            self.x = 0
        if self.x == 0:
            self.line_height = height
        self.line_height = max(self.line_height, height)
        defaults = {"unit": unit, "noValue": "Нет данных", "decimals": 1 if unit in ("ms", "percent") else 0,
                    "color": {"mode": "palette-classic"},
                    "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "green", "value": None}]}}
        if source:
            defaults["links"] = [{"title": "Открыть соответствующие логи", "url": explore(source), "targetBlank": True}]
        p = {"id": len(self.panels) + 1, "title": title, "type": kind,
             "gridPos": {"x": self.x, "y": self.y, "w": width, "h": height},
             "description": description, "datasource": DS,
             "fieldConfig": {"defaults": defaults, "overrides": []},
             "options": {"tooltip": {"mode": "multi"}, "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]}}}
        if kind in ("stat", "gauge"):
            p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                            "colorMode": "background", "graphMode": "none", "textMode": "auto"}
            defaults["color"] = {"mode": "thresholds"}
        if kind == "gauge":
            defaults.update(min=0, max=100)
            p["options"].update(showThresholdLabels=False, showThresholdMarkers=True)
        if kind == "piechart":
            p["options"] = {"pieType": "donut", "displayLabels": ["name", "percent"],
                            "legend": {"displayMode": "table", "placement": "right", "values": ["value", "percent"]},
                            "reduceOptions": {"calcs": ["lastNotNull"], "values": False}}
        if kind == "table":
            p["options"] = {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Time", "desc": True}]}
            p["transformations"] = [
                {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True, "keepTime": True}},
                {"id": "organize", "options": {
                    "excludeByName": {"steps": True, "schema_version": True, "operation_filter": True},
                    "indexByName": {name: i for i, name in enumerate(("Time", "component", "operation", "step_name", "outcome", "error_code", "request_id", "delivery_id", "job_id", "attempt_no", "duration_ms", "run_id"))},
                }},
            ]
        if kind == "logs":
            p["options"] = {"showTime": True, "showLabels": False, "wrapLogMessage": True,
                            "enableLogDetails": True, "sortOrder": "Descending", "dedupStrategy": "none"}
        instant = kind in ("stat", "gauge", "piechart")
        p["targets"] = [{"refId": chr(65+i), "expr": expr, "datasource": DS,
                         "queryType": "instant" if instant else "range", "editorMode": "code",
                         "legendFormat": legends[i] if legends else "{{outcome}}", "interval": "1m"}
                        for i, expr in enumerate(expressions or [])]
        self.panels.append(p)
        self.x += width
        return p

    def row(self, title):
        if self.x:
            self.y += self.line_height
        self.x = 0
        self.panels.append({"id": len(self.panels)+1, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1

    def note(self, title, text):
        p = self.panel(title, kind="text", width=24, height=4)
        p["options"] = {"mode": "markdown", "content": text}
        p.pop("targets")

    def latency(self, title, source, *, field="duration_ms", group="", description=""):
        self.panel(title, [quantile(source, field, q, group=group) for q in (.5, .95, .99)], unit="ms",
                   source=source, legends=[f'p{int(q*100)} '+("{{"+group+"}}" if group else "") for q in (.5, .95, .99)],
                   description=description or "Перцентили отдельных вызовов, включая повторные попытки. Пропуски означают отсутствие наблюдений.")

    def diagnostic(self, source):
        self.row("Диагностика и корреляция")
        self.panel("Ошибки и проблемные операции", [source], kind="table", width=24, height=9,
                   source=source, description="Исходные JSON-поля. Технические инциденты/попытки, не число уникальных вопросов. Клик по полю открывает эти логи в Explore.")
        trace = (BASE + ' | (request_id!="" and request_id=~"^(${request_id:regex})$") or (delivery_id!="" and delivery_id=~"^(${delivery_id:regex})$") or (job_id!="" and job_id=~"^(${job_id:regex})$")')
        self.panel("Путь request_id / delivery_id / job_id", [trace], kind="logs", width=24, height=10,
                   source=trace, description="Введите хотя бы один ID. Для полного пути сбросьте компонент, операцию и маршрут на All. rag.results содержит delivery_id: найдите его в доставке по request_id и добавьте в фильтр. Пустые ID не выбирают все логи.")

    def save(self):
        variables = []
        for name, values in [
            ("service", "core,worker"),
            ("component", "rag_api,rag_core,exact_cache,model_gateway,ingestion_gateway,ingestion_worker,queue_relay,rabbitmq,runtime"),
            ("operation", "handle_telegram_update,handle_rag_question,handle_rag_result,run_api_maintenance,support_action,support_batch,http,startup,accept_upload,convert_document,handle_ingestion_job,publish_notice,consume,recover_pending_work,question_step,bootstrap,get_answer,store_answer,embed_texts,rerank,classify_injection,generate,recognize_text,describe_image,start_models,close_models"),
            ("route", "rag.questions,rag.results,ingestion.jobs,/telegram/webhook"),
        ]:
            variables.append({"name": name, "label": name, "type": "custom", "query": values,
                              "multi": True, "includeAll": True, "allValue": ".*",
                              "current": {"text": "All", "value": "$__all"}, "options": []})
        for name in ("request_id", "delivery_id", "job_id"):
            variables.append({"name": name, "label": name+" (логи)", "type": "textbox", "query": "",
                              "current": {"text": "", "value": ""}, "options": []})
        document = {"uid": self.uid, "title": self.title, "description": self.description,
                    "schemaVersion": 41, "version": 1, "editable": False, "tags": ["rag", "loki"],
                    "timezone": "browser", "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
                    "timepicker": {"refresh_intervals": ["30s", "1m", "5m"], "time_options": ["5m", "15m", "1h", "6h", "24h", "7d"]},
                    "templating": {"list": variables}, "panels": self.panels,
                    "links": [{"type": "dashboards", "title": "RAG dashboards", "tags": ["rag"], "asDropdown": True, "includeVars": True, "keepTime": True}]}
        target = HERE / "dashboards" / (self.uid + ".json")
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


RED = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
SLO = [{"color": "red", "value": None}, {"color": "orange", "value": 90}, {"color": "green", "value": 95}]
COMMITTED = logs('component="rag_core" | operation="handle_rag_question" | result_status!=""')
INTAKE = logs('component="rag_api" | operation="handle_telegram_update" | outcome="accepted"')
CORE_STEP = logs('component="rag_core" | operation="step" | parent_operation="handle_rag_question" | step_depth=0')
MODEL = logs('component="model_gateway" | operation=~"embed_texts|rerank|classify_injection|generate|recognize_text|describe_image"')
WORKER = logs('component="ingestion_worker" | operation="handle_ingestion_job"')
WORKER_STEP = logs('component="ingestion_worker" | operation="step" | parent_operation="handle_ingestion_job"')
GATEWAY = logs('component="ingestion_gateway" | operation="accept_upload"')
GATEWAY_STEP = logs('component="ingestion_gateway" | operation="step" | parent_operation="accept_upload" | step_depth=0')
RELAY = logs('component="queue_relay" | operation="publish_notice"')
DELIVERY = logs('component="rag_api" | operation="handle_rag_result"')
ERRORS = logs('operation!="step" | error_code!=""')


def overview():
    d = Dashboard("rag-overview", "RAG Overview", "Уникальные принятые вопросы и зафиксированные результаты Core. Цель 20 секунд: приём → коммит Core, без доставки Telegram.")
    d.row("Вопросы и результаты · выбранный диапазон")
    for title, src in [("Принятые вопросы", INTAKE),
                       ("Обработаны без технического итога", COMMITTED+' | result_status=~"answered|support" or (result_status="notice" and result_code!="" and result_code!="technical_error")'),
                       ("Ответы по документации", COMMITTED+' | result_status="answered"'),
                       ("Автоэскалации: намерения Support", COMMITTED+' | result_status="support"'),
                       ("Вопросы: технический итог", COMMITTED+' | result_code="technical_error"')]:
        d.panel(title, [unique(src)], kind="stat", width=4, height=5, source=src,
                thresholds=RED if "технический" in title else None,
                description="Уникальные request_id. Только accepted при приёме либо result_status после подтверждённого коммита Core. Ретраи и stale/not_claimed исключены. Support — сохранённое намерение, не подтверждение создания тикета.")
    d.panel("Доля ответов по документации", [ratio(unique(COMMITTED+' | result_status="answered"'), unique(COMMITTED))], kind="gauge", width=4, height=5, unit="percent", source=COMMITTED,
            description="Answered / все зафиксированные результаты Core за диапазон. Это когорта завершений, не доля от входящих за тот же период.")
    d.row("Время приём → коммит Core · очередь и ретраи включены")
    for q in (.5, .95, .99):
        d.panel(f"p{int(q*100)} обработки вопроса", [quantile(COMMITTED, "processing_elapsed_ms", q, "$__range")], kind="stat", width=6, height=5, unit="ms", source=COMMITTED,
                thresholds=[{"color":"green","value":None},{"color":"red","value":20000}],
                description="processing_elapsed_ms: от persisted received_at до завершения коммита. Межпроцессные UTC часы должны быть синхронизированы; Telegram после коммита не включён.")
    d.panel("В пределах 20 секунд", [ratio(unique(COMMITTED+' | processing_elapsed_ms!="" | processing_elapsed_ms<=20000'), unique(COMMITTED+' | processing_elapsed_ms!=""'))], kind="gauge", width=6, height=5, unit="percent", thresholds=SLO, source=COMMITTED,
            description="Доля зафиксированных результатов с processing_elapsed_ms ≤ 20000. Включает все бизнес-исходы. При отсутствии замеров — Нет данных; целевой порог доли 95% — настройка отображения, не утверждённый SLA.")
    d.row("Динамика · число за интервал графика")
    d.panel("Поступление и завершение вопросов", [unique(INTAKE, window="$__interval"), unique(COMMITTED, window="$__interval")], legends=["Приняты", "Завершены Core"], source=COMMITTED,
            description="Две независимые когорты по времени события. Вопрос может поступить раньше выбранного диапазона и завершиться внутри него.")
    d.panel("Исходы вопросов", [unique(COMMITTED, window="$__interval", group="result_status")], legends=["{{result_status}}"], source=COMMITTED)
    d.latency("Время обработки: p50 / p95 / p99", COMMITTED, field="processing_elapsed_ms")
    d.panel("Распределение зафиксированных исходов", [unique(COMMITTED, group="result_status")], kind="piechart", source=COMMITTED, legends=["{{result_status}}"])
    d.note("Границы показателей", "Активные обработки из started − finished не вычисляются: падения процесса и границы диапазона делают такую оценку недостоверной. Логи не транзакционны с БД: при падении после коммита событие может отсутствовать. «Нет данных» означает отсутствие наблюдений, а не исправность системы.")
    d.diagnostic(ERRORS)
    d.save()


def pipeline():
    d = Dashboard("rag-pipeline", "RAG Pipeline Performance", "Производительность отдельных вызовов и попыток, включая ретраи. Model Gateway — вложенная зависимость.")
    d.row("Конвейер · вызовы за диапазон")
    stages = [("Guardrails", "check_question"), ("Cache", "get_answer"), ("Searcher", "search"), ("Generator", "generate_answer")]
    for title, name in stages:
        src = CORE_STEP+f' | step_name="{name}"'
        d.panel(title+" · вызовы", [count(src)], kind="stat", width=6, height=5, source=src,
                description="Количество вызовов этапа, включая повторные попытки одного вопроса. Не число уникальных вопросов.")
    d.row("Guardrails и Exact Cache")
    guard = CORE_STEP+' | step_name="check_question"'
    cache = CORE_STEP+' | step_name="get_answer"'
    d.latency("Guardrails · p50 / p95 / p99", guard)
    d.panel("Guardrails · допуск, блокировки, ошибки", [count(guard, "$__interval", "outcome")], source=guard,
            description="allowed, blocked, invalid_input, out_of_scope, detector_unavailable и failed — фактические исходы шага.")
    d.latency("Exact Cache · задержка чтения", cache)
    d.panel("Exact Cache · hits / misses / unavailable", [count(cache, "$__interval", "outcome")], source=cache)
    d.panel("Exact Cache · hit rate", [ratio(count(cache+' | outcome="cache_hit"'), count(cache+' | outcome=~"cache_hit|cache_miss"'))], kind="gauge", width=6, height=5, unit="percent", source=cache,
            description="hits / (hits + misses). Unavailable исключён и показан отдельно; сбой кеша не считается обычным промахом.")
    for title, outcome in [("Cache hits", "cache_hit"), ("Cache misses", "cache_miss"), ("Cache unavailable", "unavailable")]:
        d.panel(title, [count(cache+f' | outcome="{outcome}"')], kind="stat", width=6, height=5, source=cache, thresholds=RED if outcome=="unavailable" else None)
    d.row("Поиск и генерация")
    for title, name in stages[2:]:
        src = CORE_STEP+f' | step_name="{name}"'
        d.latency(title+" · p50 / p95 / p99", src)
        d.panel(title+" · исходы вызовов", [count(src, "$__interval", "outcome")], source=src,
                description="Searcher: fragments_found/no_fragments/failed. Generator: answered/insufficient_evidence/failed. Все исходы берутся из реального шага.")
    d.row("Model Gateway · клиентские вызовы (включают ожидание и reuse)")
    d.latency("Ожидание модели · по роли", MODEL, field="wait_ms", group="model_role")
    d.latency("Inference · по роли", MODEL, field="inference_ms", group="model_role",
              description="Наблюдаемый клиентом интервал invoke/stream без валидации и сохранения результата. reused=true не означает отдельную физическую inference. Для вызовов без reuse используйте соседний график.")
    d.latency("Inference · без reused", MODEL+' | reused="false"', field="inference_ms", group="model_role")
    d.latency("Model Gateway · по profile_id", MODEL, group="profile_id")
    d.panel("Вызовы моделей · role / profile / outcome", [count(MODEL, "$__interval", "model_role,profile_id,outcome")], source=MODEL, legends=["{{model_role}} · {{profile_id}} · {{outcome}}"])
    d.panel("Повторное использование модельного результата", [count(MODEL+' | reused="true"', "$__interval", "model_role")], source=MODEL, legends=["{{model_role}}"])
    d.row("Шаги и распределение времени")
    d.latency("Верхние шаги Core · p50 / p95 / p99", CORE_STEP, group="step_name")
    d.panel("Измеренное время по шагам Core", [f'sum by (step_name)(sum_over_time({CORE_STEP} | unwrap duration_ms | __error__=""[$__range]))'], kind="piechart", unit="ms", source=CORE_STEP, legends=["{{step_name}}"],
            description="Сумма длительностей только step_depth=0. Модельные дочерние шаги и родительский handler повторно не складываются. Время вне шагов сюда не входит; это сумма работы попыток, не wall-clock процесса.")
    d.diagnostic(logs('operation="step" | error_code!=""'))
    d.save()


def ingestion():
    d = Dashboard("rag-ingestion", "Ingestion & Document Lifecycle", "Приём Gateway, попытки Worker и подтверждённая публикация разделены.")
    d.row("Загрузки, индексация, публикация · выбранный диапазон")
    entries = [("Принятые загрузки", GATEWAY+' | outcome="accepted"'),
               ("Опубликованные редакции", WORKER+' | outcome="published"'),
               ("Задания: подтверждённый отказ", WORKER+' | outcome="failed"'),
               ("Задания: требуется reconciliation", WORKER+' | outcome="reconciliation_required"')]
    for title, src in entries:
        d.panel(title, [unique(src, "job_id")], kind="stat", width=6, height=5, source=src,
                thresholds=RED if ("отказ" in title or "reconciliation" in title) else None,
                description="Уникальные job_id, наблюдавшиеся с данным исходом за диапазон. Reconciliation — сигнал в логах, не текущая длина списка необработанных заданий. Публикация только после подтверждения publish_revision.")
    d.row("Gateway · подготовка до принятия загрузки")
    d.latency("Приём загрузки · полное время вызова", GATEWAY)
    d.panel("Gateway · принятые и отклонённые загрузки", [count(GATEWAY, "$__interval", "outcome")], source=GATEWAY)
    d.latency("Конвертация документа", GATEWAY_STEP+' | step_name="convert_document"')
    d.latency("Чанкинг и подготовка партии", GATEWAY_STEP+' | step_name=~"prepare_chunks|store_document_batch"', group="step_name")
    d.row("Worker · подготовка и индексирование")
    d.latency("Worker · длительность попытки", WORKER,
              description="Одна попытка handle_ingestion_job. Не полное время от принятия загрузки; может включать обслуживание/очистку после публикации.")
    d.panel("Worker · исходы попыток", [count(WORKER, "$__interval", "outcome")], source=WORKER)
    d.latency("Embeddings · чанки и примеры", WORKER_STEP+' | step_name=~"embed_document|embed_query"', group="step_name")
    d.latency("Подготовка примеров и snapshot", WORKER_STEP+' | step_name=~"generate_topic_questions|store_prepared_revision|load_prepared_revision"', group="step_name")
    d.latency("Qdrant · запись", WORKER_STEP+' | step_name=~"write_revision_points|write_topic_points"', group="step_name")
    d.latency("Qdrant · проверка", WORKER_STEP+' | step_name=~"verify_revision_points|verify_topic_points"', group="step_name")
    d.latency("Публикация · транзакция PostgreSQL", WORKER_STEP+' | step_name="publish_revision"')
    d.panel("Ретраи, reconciliation и неизвестное состояние", [count(WORKER+' | outcome=~"retry_scheduled|reconciliation_required|database_unknown|attempt_expired|attempt_lost"', "$__interval", "outcome")], source=WORKER)
    d.row("Публикации редакций и поколений")
    published = WORKER+' | outcome="published"'
    d.panel("Подтверждённые публикации и переключения поколения", [published], kind="table", width=24, height=9, source=published,
            description="job_id/revision_id, document_id, document_version, corpus_generation. Каждая подтверждённая публикация переключает поколение атомарно. Отдельные ручные переключения без событий здесь не видны.")
    d.note("Полное время загрузка → публикация пока недоступно", "Worker получает JobRef и сохранённые данные без времени регистрации задания. Разность отдельных логов нельзя надёжно вычислить в LogQL при ретраях и потере событий. Минимальное расширение: хранить accepted_at в служебных метаданных задания и писать publication_elapsed_ms после publish_revision. Это требует отдельного согласования изменения внутреннего контракта/хранения; текущие DTO и схема не изменены.")
    d.diagnostic(logs('component=~"ingestion_gateway|ingestion_worker" | operation!="step" | outcome=~"failed|technical_error|reconciliation_required|database_unknown|attempt_expired|unexpected_error|retry_scheduled"'))
    d.save()


def messaging():
    d = Dashboard("rag-messaging", "Messaging & Reliability", "Публикации, потребление, outbox, доставка и восстановление. Счётчики попыток не являются backlog.")
    d.row("Доставка и надёжность · попытки за диапазон")
    stats = [("Relay: опубликовано", RELAY+' | outcome="published"'),
             ("Relay: ошибка попытки", RELAY+' | outcome="failed"'),
             ("Telegram: доставлено", DELIVERY+' | outcome="delivered"'),
             ("Telegram: отказ / неизвестно", DELIVERY+' | outcome=~"failed|unknown|retry_limit|uncertain"')]
    for title, src in stats:
        d.panel(title, [count(src)], kind="stat", width=6, height=5, source=src, thresholds=RED if ("ошибка" in title or "отказ" in title) else None,
                description="Число завершённых попыток с указанным исходом. Публикация и доставка имеют разные единицы учёта; один вопрос может иметь несколько сообщений и попыток.")
    d.row("Маршруты и outbox")
    d.panel("Queue Relay · публикации по маршрутам", [count(RELAY, "$__interval", "route,outcome")], source=RELAY, legends=["{{route}} · {{outcome}}"])
    consume = logs('component="rabbitmq" | operation="consume"')
    d.panel("RabbitMQ · обработанные уведомления", [count(consume, "$__interval", "route,consume_decision")], source=consume, legends=["{{route}} · {{consume_decision}}"])
    d.latency("Outbox · готовность → начало публикации", RELAY, field="outbox_wait_ms", group="route",
              description="publish_started_at − available_at, UTC. Это ожидание после готовности записи, не возраст с момента создания outbox; включает повторные попытки публикации.")
    d.latency("Queue Relay · publish + mark", RELAY, group="route")
    d.panel("Брокер подтвердил, запись published_at не подтверждена", [count(RELAY+' | broker_confirmed="true" | outbox_marked!="true"', "$__interval", "route")], source=RELAY, legends=["{{route}}"],
            description="Подтверждение RabbitMQ отдельно от mark_outbox_published. Возможна повторная отправка; потребители должны быть идемпотентны.")
    d.panel("RabbitMQ · ошибки и повторная доставка", [count(consume+' | consume_decision="retry_broker"', "$__interval", "route"), count(consume+' | broker_redelivered="true"', "$__interval", "route")], source=consume, legends=["retry_broker · {{route}}", "redelivered · {{route}}"])
    d.row("Telegram · доставка результата и отправка частей")
    d.latency("Доставка · время попытки", DELIVERY,
              description="handle_rag_result: claim/план/отправка/фиксация в одной попытке. Очередь до попытки и интервалы между ретраями сюда не входят; это не время от исходного вопроса.")
    telegram = logs('component="rag_api" | operation="step" | step_name=~"sendMessage|sendPhoto"')
    d.latency("Telegram API · время вызова", telegram, group="step_name")
    d.panel("Telegram API · коды ответов", [count(telegram, "$__interval", "step_name,http_status")], source=telegram, legends=["{{step_name}} · {{http_status}}"])
    d.panel("Telegram · rate limit 429", [count(telegram+' | http_status="429" or telegram_error_code="429"', "$__interval", "step_name")], source=telegram, legends=["{{step_name}}"],
            description="HTTP 429 или Telegram error_code 429, не локальная задержка лимитера. Один HTTP вызов считается один раз.")
    d.panel("Доставка · повторы и исходы", [count(DELIVERY, "$__interval", "outcome")], source=DELIVERY)
    recovery = logs('component="runtime" | operation="recover_pending_work"')
    d.panel("Восстановленные уведомления", [f'sum by (route)(sum_over_time({recovery} | unwrap recovered_count | __error__=""[$__interval]))'], source=recovery, legends=["{{route}}"],
            description="Фактическое число восстановленных уведомлений за проход, не размер очереди. При отсутствии восстановлений лог не создаётся.")
    d.row("Технические инциденты")
    d.panel("Ошибки по компонентам", [count(ERRORS, "$__interval", "component")], width=24, source=ERRORS, legends=["{{component}}"],
            description="Технические события с error_code, без проекций step. Один сбой зависимости может наблюдаться несколькими компонентами; это не число уникальных первопричин.")
    d.note("Backlog очередей пока недоступен", "Публикации и consume-события не измеряют ready/unacked: redelivery, in-flight и потеря логов нарушают разность. Минимальный корректный вариант без нового сервиса: включить rabbitmq_management и периодически читать messages_ready/messages_unacknowledged через Management HTTP API; писать gauge-снимки с queue/route и timestamp в JSON без ID. До этого backlog не отображается.")
    d.diagnostic(logs('operation!="step" | error_code!="" or outcome=~"failed|unknown|retry_limit|uncertain|storage_unknown|retry_scheduled"'))
    d.save()


if __name__ == "__main__":
    overview()
    pipeline()
    ingestion()
    messaging()
    # The server adds a PostgreSQL business dashboard through its own provider.
    # The local monitoring-only stack keeps its existing four Loki dashboards.
    runpy.run_path(str(HERE.parents[1] / "deploy/monitoring/generate_dashboard.py"), run_name="__main__")
