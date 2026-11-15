"""Verify dashboards against disposable Loki/Grafana, never the application's data.

Run with Python 3.12+ and Docker: python container-manager/monitoring/verify.py
No Compose project, .env file or persistent volume is used. Test events are generated
only in the private Loki instance. These fixtures are not production measurements.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from http.client import HTTPException
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parent


def docker(*args):
    result = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"docker {' '.join(map(str, args))}: {result.stderr.strip()}")
    return (result.stdout + (result.stderr if args and args[0] == "logs" else "")).strip()


def http(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as exc:
        raise RuntimeError(f"{path}: HTTP {exc.code}: {exc.read().decode()}") from exc


def wait_ready(base, path):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urlopen(base + path, timeout=2) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException):
            time.sleep(0.5)
    raise RuntimeError(f"Service did not become ready: {base}{path}")


def panels(dashboard):
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panels(panel)


def expand(expression, overrides=None):
    values = dict(service=".*", component=".*", operation=".*", route=".*", request_id="",
                  delivery_id="", job_id="",
                  __range="5m", __interval="1m", __auto="1m", __range_s="300")
    values.update(overrides or {})

    def replace(match):
        name = match[1] or match[2]
        if name not in values:
            raise AssertionError(f"Unknown Grafana variable: {name}")
        return values[name]

    return re.sub(r"\$\{(\w+)(?::\w+)?\}|\$(\w+)", replace, expression)


def query(loki, expression, timestamp):
    # Loki's instant endpoint intentionally refuses log queries.
    metric = not expression.lstrip().startswith("{")
    params = {"query": expression}
    if metric:
        params["time"] = str(timestamp)
        endpoint = "/loki/api/v1/query"
    else:
        params.update(start=str(timestamp - 300_000_000_000), end=str(timestamp), limit="1000")
        endpoint = "/loki/api/v1/query_range"
    result = http(loki, endpoint + "?" + urlencode(params))
    assert result["status"] == "success", result
    return result["data"]["result"]


def assert_scalar(loki, expression, timestamp, expected):
    result = query(loki, expression, timestamp)
    assert len(result) == 1, (expression, result)
    actual = float(result[0]["value"][1])
    assert math.isclose(actual, expected, rel_tol=1e-6), (expression, expected, actual)


def check_links(dashboard):
    """Check URL encoding and that Explore links point to the existing datasource."""
    for panel in panels(dashboard):
        links = list(panel.get("links", []))
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") == "links":
                    links += prop["value"]
        links += panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])
        for link in links:
            url = unquote(link.get("url", ""))
            if "/explore" in url:
                assert "rag-loki" in url, link
                assert "query" in url or "expr" in url, link


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--hold", action="store_true", help="Keep private Grafana open until its temporary marker file is removed, for visual inspection")
    arguments.add_argument("--app-events", action="store_true", help="Also run the real Core handler with the existing mocked-infrastructure test fixture (requires app dependencies)")
    options = arguments.parse_args()
    dashboards = [json.loads(path.read_text(encoding="utf-8"))
                  for path in sorted((ROOT / "dashboards").glob("*.json"))]
    assert len(dashboards) == 4, "Expected all four dashboard JSON files"
    expected_uids = {dashboard["uid"] for dashboard in dashboards}
    assert len(expected_uids) == 4, "Duplicate dashboard UIDs"
    for dashboard in dashboards:
        check_links(dashboard)
    prefix = "rag-dashboard-test-" + uuid4().hex[:12]
    names = {"loki": prefix + "-loki", "grafana": prefix + "-grafana"}
    created = []
    network_created = False
    with tempfile.TemporaryDirectory(prefix="rag-dashboard-test-") as temporary:
        config = Path(temporary) / "loki.yaml"
        config.write_text((ROOT / "loki.yaml").read_text(encoding="utf-8").replace("/loki", "/tmp/loki"), encoding="utf-8")
        try:
            docker("network", "create", prefix)
            network_created = True
            docker("run", "-d", "--name", names["loki"], "--network", prefix,
                   "--network-alias", "loki", "--user", "0", "-p", "127.0.0.1::3100",
                   "--mount", f"type=bind,src={config},dst=/etc/loki/rag.yaml,readonly",
                   "grafana/loki:3.5.8", "-config.file=/etc/loki/rag.yaml")
            created.append(names["loki"])
            loki = "http://" + docker("port", names["loki"], "3100/tcp").splitlines()[0]
            wait_ready(loki, "/ready")
            docker("run", "-d", "--name", names["grafana"], "--network", prefix,
                   "-p", "127.0.0.1::3000", "-e", "GF_AUTH_ANONYMOUS_ENABLED=true",
                   "-e", "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer", "-e", "GF_USERS_ALLOW_SIGN_UP=false",
                   "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
                   "--mount", f"type=bind,src={ROOT / 'datasources.yaml'},dst=/etc/grafana/provisioning/datasources/loki.yaml,readonly",
                   "--mount", f"type=bind,src={ROOT / 'dashboards.yaml'},dst=/etc/grafana/provisioning/dashboards/rag.yaml,readonly",
                   "--mount", f"type=bind,src={ROOT / 'dashboards'},dst=/var/lib/grafana/dashboards/rag,readonly",
                   "grafana/grafana:12.1.1")
            created.append(names["grafana"])
            grafana = "http://" + docker("port", names["grafana"], "3000/tcp").splitlines()[0]
            wait_ready(grafana, "/api/health")
            first = check_provisioning(grafana, expected_uids)
            health = http(grafana, "/api/datasources/uid/rag-loki/health")
            assert health["status"] == "OK", health
            # Restart the same private container/database to catch duplicate provisioning.
            docker("restart", names["grafana"])
            # Docker Desktop may reassign a randomly published port on restart.
            grafana = "http://" + docker("port", names["grafana"], "3000/tcp").splitlines()[0]
            wait_ready(grafana, "/api/health")
            assert check_provisioning(grafana, expected_uids) == first
            print("PASS: four dashboards and rag-loki provisioned; restart preserves IDs", flush=True)
            now = time.time_ns()
            expressions = [(dashboard["title"], panel["title"], target.get("refId"), expand(target["expr"]))
                           for dashboard in dashboards for panel in panels(dashboard)
                           for target in panel.get("targets", []) if target.get("expr")]
            for title, panel, ref, expression in expressions:
                try:
                    result = query(loki, expression, now)
                    assert not result, f"No-data query fabricated values: {result}"
                except Exception as exc:
                    raise AssertionError(f"{title} / {panel} / {ref}: {expression}") from exc
            print(f"PASS: {len(expressions)} LogQL expressions parse; empty Loki stays empty", flush=True)
            replay(loki, dashboards, now)
            if options.app_events:
                replay_application(loki, now)
            for title, panel, ref, expression in expressions:
                try:
                    query(loki, expression, now)
                except Exception as exc:
                    raise AssertionError(f"{title} / {panel} / {ref}: {expression}") from exc
            print(f"PASS: {len(expressions)} expressions execute against replay events", flush=True)
            if options.hold:
                marker = Path(temporary) / "hold"
                marker.touch()
                print(f"Private Grafana: {grafana}/d/rag-overview (synthetic test data); fixture_now_ms={now // 1_000_000}.\nRemove marker to clean up test containers: {marker}", flush=True)
                while marker.exists():
                    time.sleep(0.5)
        except BaseException:
            for name in created:
                print(f"Diagnostic log ({name}):\n" + docker("logs", "--tail", "30", name))
            raise
        finally:
            for name in reversed(created):
                docker("rm", "-f", name)
            if network_created:
                docker("network", "rm", prefix)


def check_provisioning(grafana, expected):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        found = http(grafana, "/api/search?type=dash-db")
        if {entry["uid"] for entry in found} == expected:
            break
        time.sleep(0.3)
    assert {entry["uid"] for entry in found} == expected, found
    records = {}
    for uid in expected:
        record = http(grafana, "/api/dashboards/uid/" + uid)
        assert record["meta"]["provisioned"], record["meta"]
        records[uid] = record["dashboard"]["id"]
    datasource = http(grafana, "/api/datasources/uid/rag-loki")
    assert datasource["type"] == "loki" and datasource["url"] == "http://loki:3100", datasource
    return records


def replay(loki, dashboards, now):
    """Exercise actual dashboard expressions with clearly synthetic private data."""
    records = []
    source = load_module("monitoring_event_fixture", ROOT.parents[1] / "app/rag/runtime/events.py")

    def event(component, operation, kind="finished", **fields):
        output = StringIO()
        with redirect_stdout(output):
            source.emit_event(component, operation, kind, str(uuid4()), **fields)
        records.extend(json.loads(line) for line in output.getvalue().splitlines())

    for request, status, elapsed in [(101, "answered", 1000), (102, "support", 10000),
                                     (103, "notice", 30000)]:
        event("rag_api", "handle_telegram_update", outcome="accepted", request_id=request)
        event("rag_core", "handle_rag_question", "started", request_id=request,
              route="rag.questions")
        event("rag_core", "handle_rag_question", request_id=request, route="rag.questions",
              outcome="completed", result_status=status,
              result_code="technical_error" if status == "notice" else "",
              duration_ms=elapsed / 2, processing_elapsed_ms=elapsed, attempt_no=2)
    # An accepted log line delivered twice must not turn three questions into four.
    event("rag_api", "handle_telegram_update", outcome="accepted", request_id=101)
    # A retry and stale claim have no committed result_status; started is not finished.
    event("rag_core", "handle_rag_question", request_id=101, route="rag.questions",
          outcome="retry_scheduled", duration_ms=45000, attempt_no=1)
    event("rag_core", "handle_rag_question", request_id=101, route="rag.questions",
          outcome="stale_attempt", duration_ms=60000, attempt_no=3)
    event("rag_core", "question_step", request_id=101, error_code="dependency_timeout",
          retryable=True)
    event("queue_relay", "publish_notice", route="rag.questions", request_id=101,
          message_id=str(uuid4()), outcome="published", broker_confirmed=True,
          outbox_marked=True, outbox_wait_ms=200, duration_ms=5)
    event("queue_relay", "publish_notice", route="rag.results", delivery_id=501,
          message_id=str(uuid4()), outcome="failed", broker_confirmed=False,
          outbox_marked=False, outbox_wait_ms=300, duration_ms=10, error_code="broker_timeout")
    jobs = ["00000000-0000-4000-8000-00000000020" + str(number) for number in (1, 2, 3)]
    event("queue_relay", "publish_notice", route="ingestion.jobs", job_id=jobs[0],
          message_id=str(uuid4()), outcome="published", broker_confirmed=True,
          outbox_marked=True, outbox_wait_ms=400, duration_ms=5)
    event("rag_api", "handle_rag_result", route="rag.results", request_id=101, delivery_id=501,
          outcome="delivered", duration_ms=600, attempt_no=1)
    event("rag_core", "handle_rag_question", request_id=104, outcome="not_claimed", steps=[
        dict(name="check_question", outcome="allowed", duration_ms=100, steps=[
            dict(name="embed_texts", outcome="completed", duration_ms=80, wait_ms=10,
                 inference_ms=70, model_role="embedding", profile_id="fixture-embedding", reused=False)]),
        dict(name="get_answer", outcome="cache_hit", duration_ms=10),
        dict(name="get_answer", outcome="cache_miss", duration_ms=20),
        dict(name="get_answer", outcome="unavailable", duration_ms=30),
        dict(name="generate_answer", outcome="answered", duration_ms=500, steps=[
            dict(name="generate", outcome="completed", duration_ms=400, wait_ms=50,
                 inference_ms=350, model_role="generation", profile_id="fixture-generation", reused=False)]),
    ])
    event("model_gateway", "generate", model_role="generation", profile_id="fixture-generation",
          outcome="completed", wait_ms=50, inference_ms=350, duration_ms=400, reused=False)
    event("model_gateway", "embed_texts", model_role="embedding", profile_id="fixture-embedding",
          outcome="completed", wait_ms=10, inference_ms=70, duration_ms=80, reused=False)
    event("ingestion_gateway", "accept_upload", job_id=jobs[0], outcome="accepted", duration_ms=1000)
    event("ingestion_worker", "handle_ingestion_job", job_id=jobs[0], outcome="published",
          revision_id=str(uuid4()), corpus_generation=8, duration_ms=2000)
    event("ingestion_worker", "handle_ingestion_job", job_id=jobs[1], outcome="failed", duration_ms=2000)
    event("ingestion_worker", "handle_ingestion_job", job_id=jobs[2],
          outcome="reconciliation_required", duration_ms=2000)
    event("rag_api", "handle_rag_result", request_id=102, outcome="retry_scheduled", steps=[
        dict(name="sendMessage", outcome="failed", http_status=429,
             telegram_error_code=429, duration_ms=100, retry_after_seconds=2)])
    streams = []
    for service in ("core", "worker"):
        values = [[str(now - 30_000_000_000 + index * 1_000_000), json.dumps(record)]
                  for index, record in enumerate(records)
                  if (record["component"].startswith("ingestion_") or record.get("route") == "ingestion.jobs") == (service == "worker")]
        streams.append({"stream": {"service": service}, "values": values})
    http(loki, "/loki/api/v1/push", {"streams": streams})
    # A push acknowledgement precedes query visibility in Loki's distributed query path.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if query(loki, '{service="core"} | json | request_id="103"', now):
            break
        time.sleep(0.2)
    else:
        raise AssertionError("Replay events did not become queryable")

    terminal = ('{service="core"} | json | __error__="" | component="rag_core" '
                '| operation="handle_rag_question" | event="finished" | result_status!=""')
    accepted = ('{service="core"} | json | __error__="" | component="rag_api" '
                '| operation="handle_telegram_update" | event="finished" | outcome="accepted"')
    unique = lambda stream: f'count(sum by (request_id) (count_over_time({stream}[5m])))'
    assert_scalar(loki, unique(accepted), now, 3)
    assert_scalar(loki, unique(terminal), now, 3)
    for quantile, expected in [(0.5, 10000), (0.95, 28000), (0.99, 29600)]:
        assert_scalar(loki, f'quantile_over_time({quantile}, {terminal} | unwrap processing_elapsed_ms '
                      '| __error__="" [5m]) by ()', now, expected)
    assert_scalar(loki, f'100 * ({unique(terminal + " | processing_elapsed_ms <= 20000")}) / '
                  f'({unique(terminal)})', now, 200 / 3)
    # Exact request lookup must find the question in all three relevant components.
    correlated = query(loki, '{service="core"} | json | request_id="101"', now)
    found = {json.loads(line)["component"] for stream in correlated for _, line in stream["values"]}
    assert {"rag_api", "rag_core", "queue_relay"} <= found, found
    expected_panels = {
        "rag-overview": {2: 3, 3: 2, 4: 1, 5: 1, 6: 1, 7: 100 / 3,
                         9: 10000, 10: 28000, 11: 29600, 12: 200 / 3},
        "rag-pipeline": {2: 1, 11: 50, 12: 1, 13: 1, 14: 1},
        "rag-ingestion": {2: 1, 3: 1, 4: 1, 5: 1},
        "rag-messaging": {2: 2, 3: 1, 4: 1, 17: 1},
    }
    for dashboard in dashboards:
        for panel in panels(dashboard):
            expected = expected_panels.get(dashboard["uid"], {}).get(panel["id"])
            if expected is not None:
                assert_scalar(loki, expand(panel["targets"][0]["expr"]), now, expected)
            if dashboard["uid"] == "rag-pipeline" and panel["id"] == 29:
                expression = expand(panel["targets"][0]["expr"], {"operation": "handle_rag_question"})
                results = query(loki, expression, now)
                assert sum(float(row["value"][1]) for row in results) == 660, results
                assert {row["metric"]["step_name"] for row in results} == {
                    "check_question", "get_answer", "generate_answer"}, results
            if dashboard["uid"] == "rag-pipeline" and panel["id"] == 11:
                # Exercise the absent numerator branch of the exact dashboard formula.
                expression = expand(panel["targets"][0]["expr"]).replace('outcome="cache_hit"', 'outcome="never_observed"')
                assert_scalar(loki, expression, now, 0)
                assert not query(loki, expand(panel["targets"][0]["expr"], {"service": "missing-service"}), now)
            if dashboard["uid"] == "rag-pipeline" and panel["id"] == 21:
                results = query(loki, expand(panel["targets"][0]["expr"]), now)
                role_wait = {row["metric"]["model_role"]: float(row["value"][1]) for row in results}
                assert role_wait == {"embedding": 10, "generation": 50}, role_wait
            if dashboard["uid"] == "rag-pipeline" and panel["id"] == 22:
                results = query(loki, expand(panel["targets"][0]["expr"]), now)
                role_inference = {row["metric"]["model_role"]: float(row["value"][1]) for row in results}
                assert role_inference == {"embedding": 70, "generation": 350}, role_inference
            if dashboard["uid"] == "rag-ingestion" and panel["id"] == 2:
                assert_scalar(loki, expand(panel["targets"][0]["expr"], {"service": "worker", "component": "ingestion_gateway", "operation": "accept_upload"}), now, 1)
                assert not query(loki, expand(panel["targets"][0]["expr"], {"service": "core"}), now)
            if dashboard["uid"] == "rag-messaging" and panel["id"] == 2:
                assert_scalar(loki, expand(panel["targets"][0]["expr"], {"route": "rag.questions"}), now, 1)
                assert_scalar(loki, expand(panel["targets"][0]["expr"], {"route": "ingestion.jobs", "service": "worker"}), now, 1)
            if panel["title"].startswith("Путь request_id"):
                expression = expand(panel["targets"][0]["expr"], {"request_id": "101"})
                result = query(loki, expression, now)
                lines = [json.loads(line) for stream in result for _, line in stream["values"]]
                assert lines and all(str(line.get("request_id")) == "101" for line in lines), lines
                expression = expand(panel["targets"][0]["expr"], {"request_id": "101", "delivery_id": "501"})
                result = query(loki, expression, now)
                lines = [json.loads(line) for stream in result for _, line in stream["values"]]
                assert any(line.get("component") == "queue_relay" and line.get("route") == "rag.results" for line in lines), lines
    print("PASS: actual dashboard targets: unique counts, retry exclusion, percentiles, percentages, cache, hierarchy, worker, relay, Telegram 429, ID filter", flush=True)


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def replay_application(loki, now):
    """Capture a real handler invocation; infrastructure is mocked by its owner tests."""
    sys.path.insert(0, str(ROOT.parents[1] / "app"))
    tests = load_module("monitoring_core_fixture", ROOT.parents[1] / "app/tests/test_rag_core.py")
    case = tests.CoreTests("test_answer_commit_before_cache")
    output = StringIO()
    try:
        case.setUp()
        with redirect_stdout(output):
            asyncio.run(case.test_answer_commit_before_cache())
    finally:
        case.doCleanups()
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    committed = [record for record in records if record.get("result_status") == "answered"]
    assert len(committed) == 1 and committed[0]["processing_elapsed_ms"] >= 0, records
    assert any(record.get("operation") == "step" for record in records), records
    forbidden = {"text", "question", "answer_text", "normalized_question", "prompt"}
    assert all(not forbidden.intersection(record) for record in records), records
    values = [[str(now - 10_000_000_000 + index * 1_000_000), json.dumps(record)]
              for index, record in enumerate(records)]
    http(loki, "/loki/api/v1/push", {"streams": [{"stream": {"service": "core"}, "values": values}]})
    result = query(loki, '{service="core"} | json | request_id="1"', now)
    assert result, "Real Core events did not reach Loki"
    print(f"PASS: {len(records)} real Core events captured through existing test fixture; dependencies mocked", flush=True)


if __name__ == "__main__":
    main()
