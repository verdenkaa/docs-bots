"""Generate the server's business dashboard using only the Python standard library."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DS = {"type": "grafana-postgresql-datasource", "uid": "rag-business-postgres"}
# Grafana's sqlstring formatter quotes and escapes every selected value.
FILTER = """data_mode = ${data_mode:sqlstring}
  AND channel IN (${channel:sqlstring})
  AND ('__all' IN (${topic:sqlstring}) OR topic IN (${topic:sqlstring}))"""
BASE = """WITH bounds AS (
  SELECT $__timeFrom()::timestamptz AS start_at, $__timeTo()::timestamptz AS end_at
), facts AS (
  SELECT r.* FROM rag_analytics.requests r CROSS JOIN bounds b
  WHERE r.received_at >= b.start_at AND r.received_at < b.end_at
    AND """ + FILTER + "\n)\n"
COMPARISON = """WITH bounds AS (
  SELECT $__timeFrom()::timestamptz AS start_at, $__timeTo()::timestamptz AS end_at
), facts AS (
  SELECT r.*, CASE WHEN r.received_at >= b.start_at THEN 'current' ELSE 'previous' END AS period
  FROM rag_analytics.requests r CROSS JOIN bounds b
  WHERE r.received_at >= b.start_at - (b.end_at - b.start_at) AND r.received_at < b.end_at
    AND """ + FILTER + "\n)\n"


class Dashboard:
    def __init__(self):
        self.panels = []
        self.x = self.y = self.height = 0

    def panel(self, title, query=None, kind="table", width=12, height=8, unit="short", description=""):
        if self.x + width > 24:
            self.y += self.height
            self.x = self.height = 0
        panel = {
            "id": len(self.panels) + 1, "title": title, "type": kind, "description": description,
            "gridPos": {"x": self.x, "y": self.y, "w": width, "h": height},
            "datasource": DS,
            "fieldConfig": {"defaults": {
                "unit": unit, "noValue": "Нет данных", "decimals": 1 if unit in ("percent", "ms") else 0,
                "color": {"mode": "palette-classic"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]},
            }, "overrides": []},
        }
        if kind == "stat":
            panel["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                                "colorMode": "value", "graphMode": "none", "textMode": "auto"}
        elif kind == "timeseries":
            panel["options"] = {"tooltip": {"mode": "multi", "sort": "desc"},
                                "legend": {"displayMode": "list", "placement": "bottom"}}
            panel["fieldConfig"]["defaults"]["custom"] = {
                "drawStyle": "line", "lineInterpolation": "smooth", "lineWidth": 2,
                "fillOpacity": 12, "spanNulls": False, "showPoints": "never", "axisBorderShow": False,
            }
        elif kind == "barchart":
            panel["options"] = {"orientation": "horizontal", "showValue": "always", "barWidth": .7,
                                "groupWidth": .75, "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}
        else:
            panel["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
        if query:
            panel["targets"] = [{"refId": "A", "datasource": DS, "rawSql": query, "rawQuery": True,
                                 "editorMode": "code", "format": "time_series" if kind == "timeseries" else "table"}]
        self.panels.append(panel)
        self.x += width
        self.height = max(self.height, height)
        return panel

    def note(self, title, content, height=4):
        panel = self.panel(title, kind="text", width=24, height=height)
        panel["options"] = {"mode": "markdown", "content": content}

    def row(self, title):
        if self.x:
            self.y += self.height
        self.x = self.height = 0
        self.panels.append({"id": len(self.panels) + 1, "title": title, "type": "row",
                            "collapsed": False, "panels": [],
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}})
        self.y += 1


def overrides(panel, fields, prop, value):
    for name in fields:
        panel["fieldConfig"]["overrides"].append({
            "matcher": {"id": "byName", "options": name}, "properties": [{"id": prop, "value": value}]})


def document():
    d = Dashboard()
    d.note("Бизнес-аналитика · ${data_mode:text}",
           "**${data_mode:text}.** Показатели по вопросам, **принятым в выбранный период**, и их текущему состоянию. "
           "ДЕМО — синтетические примеры. Автоответ без эскалации — операционная метрика; решение проблемы не подтверждено.", height=3)
    d.row("Эффективность помощника")
    cards = [
        ("Принято вопросов", 'count(*)', "short", "Одна строка на сохранённый request_id; повторы доставки и технические попытки не увеличивают счётчик."),
        ("Автоответы без эскалации", 'count(*) FILTER (WHERE auto_answer)', "short", "Ответ по документации доступен пользователю: нормальная полная доставка Telegram либо коммит web. Исключены эскалации и зафиксированное недовольство."),
        ("Доля успешных автоответов*", '100.0 * count(*) FILTER (WHERE auto_answer) / nullif(count(*) FILTER (WHERE result_kind IS NOT NULL), 0)', "percent", "Автоответы без эскалации / все вопросы с зафиксированным результатом в когорте приёма. Незавершённые исключены из знаменателя. *Операционное определение для демо, не CSAT и не подтверждённое решение."),
        ("Передано оператору", 'count(*) FILTER (WHERE escalated)', "short", "Автоматическое намерение Support либо case в creating/open/closed. Включает ручную передачу после ответа; waiting/awaiting_confirmation и cancelled не считаются передачей."),
        ("Ожидают результата", 'count(*) FILTER (WHERE result_kind IS NULL)', "short", "Принятые вопросы без зафиксированного результата Core. Это текущий срез, не разность строк started и finished."),
        ("Медиана до ответа", 'percentile_cont(0.5) WITHIN GROUP (ORDER BY reply_ms)', "ms", "От приёма до первой нормальной текстовой квитанции Telegram; для web — до доступности ответа. Только answered, отрицательные или отсутствующие замеры исключены; это не время решения."),
    ]
    for (title, expression, unit, description), color in zip(cards, ("blue", "green", "green", "orange", "orange", "blue")):
        panel = d.panel(title, BASE + f'SELECT {expression} AS "{title}" FROM facts', kind="stat", width=8, height=4,
                        unit=unit, description=description)
        panel["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": color}

    d.row("Спрос и качество во времени")
    d.panel("Запросы · текущий и предыдущий период", COMPARISON + """
, aligned AS (
  SELECT *, received_at + CASE WHEN period = 'previous'
    THEN (SELECT end_at - start_at FROM bounds) ELSE interval '0' END AS plot_at FROM facts
)
SELECT $__timeGroupAlias(plot_at, '1d'),
  count(*)::double precision AS value,
  CASE WHEN period = 'current' THEN 'Выбранный период' ELSE 'Предыдущий период' END AS metric
FROM aligned GROUP BY 1, 3 ORDER BY 1, 3
""", kind="timeseries", description="Предыдущий период той же длительности сдвинут на текущую ось времени. UTC-сутки, крайние сутки могут быть неполными. Пустые интервалы не заменяются нулём.")
    d.panel("Автоответы без эскалации · доля по дням", BASE + """
SELECT $__timeGroupAlias(received_at, '1d'),
  100.0 * count(*) FILTER (WHERE auto_answer) / nullif(count(*) FILTER (WHERE result_kind IS NOT NULL), 0)
    AS "Доля автоответов, %"
FROM facts GROUP BY 1 ORDER BY 1
""", kind="timeseries", unit="percent", description="Когорта по UTC-дате приёма. Доля пересчитывается при поздней эскалации; это не неизменяемая история ежедневных снимков.")
    d.panel("Какие темы спрашивают чаще", BASE + """
SELECT topic AS "Тема", count(*) AS "Вопросы" FROM facts GROUP BY topic ORDER BY 2 DESC, 1
""", kind="barchart", description="Каждый вопрос относится ровно к одной теме. Первое совпадение словаря побеждает; нераспознанные вопросы видны отдельно.")
    d.panel("Динамика тем · вопросы по дням", BASE + """
SELECT $__timeGroupAlias(received_at, '1d'), count(*)::double precision AS value, topic AS metric
FROM facts GROUP BY 1, 3 ORDER BY 1, 3
""", kind="timeseries", description="Рост числа вопросов — повод проверить изменения продукта или понятность инструкции, а не автоматическое доказательство дефекта.")

    d.row("Где улучшать документацию и продукт")
    trend = d.panel("Темы · рост спроса и покрытие ответами", COMPARISON + """
SELECT topic AS "Тема",
  count(*) FILTER (WHERE period = 'current') AS "Сейчас",
  count(*) FILTER (WHERE period = 'previous') AS "Ранее",
  count(*) FILTER (WHERE period = 'current') - count(*) FILTER (WHERE period = 'previous') AS "Изменение, шт.",
  round(100.0 * (count(*) FILTER (WHERE period = 'current') - count(*) FILTER (WHERE period = 'previous'))
    / nullif(count(*) FILTER (WHERE period = 'previous'), 0), 1) AS "Рост, %",
  CASE WHEN count(*) FILTER (WHERE period = 'previous') = 0
    THEN 'Нет базы сравнения' ELSE 'Сопоставимо' END AS "Сравнение",
  round(100.0 * count(*) FILTER (WHERE period = 'current' AND auto_answer)
    / nullif(count(*) FILTER (WHERE period = 'current' AND result_kind IS NOT NULL), 0), 1) AS "Автоответы, %",
  count(*) FILTER (WHERE period = 'current' AND support_reason = 'no_evidence' AND escalated) AS "Нет подтверждений",
  count(*) FILTER (WHERE period = 'current' AND dissatisfied) AS "Недовольство"
FROM facts GROUP BY topic ORDER BY "Нет подтверждений" DESC, "Недовольство" DESC, "Сейчас" DESC, topic
""", width=24, height=9,
                    description="Предыдущий смежный диапазон той же длительности, те же фильтры. Нулевая база → Нет базы сравнения, а не бесконечный рост. Нет подтверждений указывает на возможный пробел документации/поиска; недовольство требует разбора ответа и продукта.")
    overrides(trend, ["Рост, %", "Автоответы, %"], "unit", "percent")
    overrides(trend, ["Рост, %", "Автоответы, %"], "decimals", 1)
    for name, width in (("Тема", 220), ("Сейчас", 70), ("Ранее", 70), ("Изменение, шт.", 110),
                        ("Рост, %", 85), ("Сравнение", 150), ("Автоответы, %", 125),
                        ("Нет подтверждений", 160), ("Недовольство", 140)):
        overrides(trend, [name], "custom.width", width)
    priorities = d.panel("Приоритет проверки · частые вопросы без подтверждений", BASE + """
SELECT topic AS "Тема", left(question, 240) AS "Частый вопрос",
  count(*) AS "Повторов",
  count(*) FILTER (WHERE support_reason = 'no_evidence' AND escalated) AS "Нет подтверждений",
  count(*) FILTER (WHERE dissatisfied) AS "Недовольство",
  CASE WHEN count(*) FILTER (WHERE support_reason = 'no_evidence' AND escalated) > 0
    THEN 'Проверить инструкцию и поиск' ELSE 'Разобрать ответ и сценарий продукта' END AS "Следующий шаг"
FROM facts GROUP BY topic, question
HAVING count(*) >= 2 AND count(*) FILTER (WHERE (support_reason = 'no_evidence' AND escalated) OR dissatisfied) > 0
ORDER BY "Нет подтверждений" DESC, "Недовольство" DESC, "Повторов" DESC, topic, question LIMIT 15
""", width=24, height=10,
            description="Приоритет по абсолютному числу проблемных обращений. Группировка точных текстов после lower/trim/сжатия пробелов; смысловые перефразирования не объединяются. Текст обрезается только после группировки. Это гипотезы для проверки, не установленная причина.")
    for name, width in (("Тема", 190), ("Частый вопрос", 370), ("Повторов", 85),
                        ("Нет подтверждений", 160), ("Недовольство", 140), ("Следующий шаг", 240)):
        overrides(priorities, [name], "custom.width", width)
    faq = d.panel("Частые вопросы · кандидаты в FAQ", BASE + """
SELECT topic AS "Тема", left(question, 240) AS "Вопрос", count(*) AS "Повторов",
  round(100.0 * count(*) FILTER (WHERE auto_answer)
    / nullif(count(*) FILTER (WHERE result_kind IS NOT NULL), 0), 1) AS "Автоответы, %"
FROM facts GROUP BY topic, question HAVING count(*) >= 2
ORDER BY "Повторов" DESC, topic, question LIMIT 15
""", width=16, height=9, description="Самые частые точные формулировки. Редкие вопросы скрыты этой таблицей, но включены в KPI и темы. Тексты доступны только вошедшему администратору Grafana.")
    overrides(faq, ["Автоответы, %"], "unit", "percent")
    overrides(faq, ["Автоответы, %"], "decimals", 1)
    for name, width in (("Тема", 180), ("Вопрос", 360), ("Повторов", 85), ("Автоответы, %", 115)):
        overrides(faq, [name], "custom.width", width)
    d.panel("Причины передачи оператору", BASE + """
SELECT CASE support_reason WHEN 'no_evidence' THEN 'Нет подтверждений'
  WHEN 'out_of_scope' THEN 'Вне темы' WHEN 'dissatisfied' THEN 'Недовольство'
  WHEN 'human_request' THEN 'Нужен оператор' ELSE 'Не определено' END AS "Причина",
  count(*) AS "Обращения"
FROM facts WHERE escalated GROUP BY 1 ORDER BY 2 DESC, 1
""", kind="barchart", width=8, height=9, description="Факт/намерение передачи не означает закрытие тикета. Вне темы не объявляется дефектом документации.")
    d.note("Как читать результаты", "**Автоответ:** сохранён `answered`, нормальная полная доставка Telegram "
           "или доступный результат web, нет передачи оператору и зарегистрированного недовольства. "
           "**Знаменатель доли:** все принятые в период вопросы с результатом, включая уведомления и технические ошибки. "
           "Незавершённые видны отдельным KPI. При пустом диапазоне счётчики = 0, доли и медианы = «Нет данных». "
           "Поздние реакции меняют прошлую когорту. **Сигналы:** `no_evidence` → проверить документацию и поиск; "
           "рост/недовольство → проверить продукт и ответ. Причину устанавливает разбор. "
           "ДЕМО содержит 28 полных UTC-дней; его цифры не подтверждают эффективность рабочего решения.", height=4)
    variables = [
        {"name": "data_mode", "label": "Данные", "type": "custom",
         "query": "Рабочие данные : live,ДЕМО (синтетические данные) : demo", "multi": False, "includeAll": False,
         "current": {"text": "Рабочие данные", "value": "live"},
         "options": [{"text": "Рабочие данные", "value": "live", "selected": True},
                     {"text": "ДЕМО (синтетические данные)", "value": "demo", "selected": False}]},
        {"name": "channel", "label": "Канал", "type": "custom", "query": "telegram,web",
         "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"}, "options": []},
        {"name": "topic", "label": "Тема", "type": "query", "datasource": DS, "refresh": 2,
         "query": "SELECT DISTINCT topic AS __text, topic AS __value FROM rag_analytics.requests WHERE data_mode = ${data_mode:sqlstring} ORDER BY 1",
         # Grafana inserts a custom All value verbatim, bypassing sqlstring formatting.
         "multi": True, "includeAll": True, "allValue": "'__all'",
         "current": {"text": "All", "value": "$__all"}, "options": [], "sort": 1},
    ]
    return {"uid": "rag-business", "title": "RAG · Бизнес-аналитика", "schemaVersion": 41, "version": 1,
            "editable": False, "tags": ["rag", "business"], "timezone": "Europe/Moscow",
            "description": "Запросы, темы, автоответы без эскалации и тренды. PostgreSQL; рабочие и синтетические данные разделены.",
            "time": {"from": "now-7d", "to": "now"}, "refresh": "1m",
            "timepicker": {"refresh_intervals": ["1m", "5m", "15m"], "time_options": ["24h", "7d", "14d", "28d"]},
            "templating": {"list": variables}, "panels": d.panels,
            "links": [{"type": "link", "title": "Демо · 7 дней", "url": "/d/rag-business?var-data_mode=demo&var-channel=All&var-topic=All&from=now-7d&to=now"},
                      {"type": "link", "title": "Рабочие данные", "url": "/d/rag-business?var-data_mode=live&var-channel=All&var-topic=All&from=now-7d&to=now"},
                      {"type": "dashboards", "title": "Технические дашборды", "tags": ["loki"], "asDropdown": True, "keepTime": True}]}


def main():
    target = HERE / "dashboards" / "rag-business.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
