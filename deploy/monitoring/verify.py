"""Verify actual SQL and provisioning in disposable PostgreSQL/Grafana containers.

Run from repository root with app dependencies:
  app/.venv/Scripts/python.exe -m deploy.monitoring.verify --engine podman
No .env, application services, permanent volumes, models or real user data are used.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4

from .generate_dashboard import document
from .schema import READER
from .setup import provision

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "isolated-only-$analytics'password"


def http(base, path, body=None):
    request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if body is not None:
            raise RuntimeError(error.read().decode()) from error
        raise


def wait_http(base, path):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            return http(base, path)
        except (OSError, ValueError):
            time.sleep(.5)
    raise RuntimeError("Isolated service not ready: " + path)


async def fixtures(port):
    import asyncpg
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import URL, text
    from rag.runtime.postgresql._models import _Base

    await wait_postgres(port)
    url = URL.create("postgresql+asyncpg", username="test", password="isolated-only",
                     host="127.0.0.1", port=port, database="test")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA fixture_app"))
            await connection.execute(text("SET LOCAL search_path TO fixture_app"))
            await connection.run_sync(_Base.metadata.create_all)
    finally:
        await engine.dispose()
    conn = await asyncpg.connect(host="127.0.0.1", port=port, user="test", password="isolated-only", database="test")
    try:
        await conn.execute("SET search_path TO fixture_app")
        now = datetime.now(timezone.utc) - timedelta(hours=2)
        for n in range(1, 12):
            channel = "web" if n == 2 else "telegram"
            kind = "support" if n in (6, 7) else "notice" if n == 8 else None if n == 9 else "answered"
            result = {"kind": kind}
            if n == 8:
                result["code"] = "technical_error"
            if n in (6, 7):
                result["reason"] = "no_evidence" if n == 6 else "out_of_scope"
            await conn.execute("""INSERT INTO rag_requests(request_id,user_id,channel,chat_id,update_id,input,received_at,state,result)
                VALUES($1,$1,$2,$3,$3,$4::jsonb,$5,'finished',$6::jsonb)""", n, channel,
                None if channel == "web" else n,
                json.dumps({"kind": "text", "text": "  Как оформить   КОМАНДИРОВКУ? "}, ensure_ascii=False),
                now - (timedelta(days=8) if n == 11 else timedelta()),
                json.dumps(result) if kind else None)
            if n == 2:
                await conn.execute("""INSERT INTO web_requests(request_id,user_id,idempotency_key,payload_hash,completed_at)
                    VALUES(2,2,$1,'hash',$2)""", str(uuid4()), now + timedelta(seconds=3))
            elif kind == "answered":
                receipt = {"variant": "length_fallback" if n == 3 else "normal",
                           "delivered_text": "Fixture answer", "confirmed_at": (now + timedelta(seconds=5)).isoformat()}
                await conn.execute("""INSERT INTO rag_deliveries(user_id,chat_id,request_id,purpose,state,receipts)
                    VALUES($1,$1,$1,'answer',$2,$3::jsonb)""", n, "sending" if n == 4 else "delivered", json.dumps([receipt]))
            if n in (5, 6, 7):
                state = "creating" if n == 6 else "open"
                reason = "dissatisfied" if n == 5 else "no_evidence" if n == 6 else "out_of_scope"
                await conn.execute("""INSERT INTO support_cases(case_id,request_id,user_id,state,reason,ticket_url,created_at)
                    VALUES($1,$2,$2,$3,$4,$5,$6)""", uuid4(), n, state, reason,
                    None if state == "creating" else "https://fixture.invalid/ticket", None if state == "creating" else now)
            if n == 10:
                await conn.execute("""INSERT INTO telegram_updates(update_id,received_at,action,result)
                    VALUES(100,$1,'{"kind":"offer","reason":"dissatisfied","request_id":10}', '{"kind":"handled"}')""", now)
        # Repeated setup must neither duplicate data nor reset application state.
        await provision(conn, "fixture_app", PASSWORD)
        await provision(conn, "fixture_app", PASSWORD)
        rows = await conn.fetch("SELECT * FROM rag_analytics.requests WHERE data_mode='live' ORDER BY request_id")
        assert len(rows) == 11
        assert [r["request_id"] for r in rows if r["auto_answer"]] == [1, 2, 11]
        assert [r["request_id"] for r in rows if r["escalated"]] == [5, 6, 7]
        assert all(r["topic"] == "Командировки" for r in rows)
        assert rows[1]["reply_ms"] == 3000
        assert rows[2]["auto_answer"] is False  # fallback is not the documented answer
        assert rows[3]["auto_answer"] is False  # partial Telegram delivery
        assert rows[9]["dissatisfied"] is True  # reaction without an opened case
        counts = await conn.fetchrow("""SELECT count(*) AS total,
            count(*) FILTER (WHERE result_kind IS NOT NULL) AS completed,
            count(*) FILTER (WHERE auto_answer) AS auto
            FROM rag_analytics.requests WHERE data_mode='live' AND received_at >= now()-interval '7 days'""")
        assert tuple(counts.values()) == (10, 9, 2), counts
        assert await conn.fetchval("SELECT count(*) FROM rag_analytics.requests WHERE data_mode='demo'") > 2000
    finally:
        await conn.close()
    reader = await asyncpg.connect(host="127.0.0.1", port=port, user=READER, password=PASSWORD, database="test")
    try:
        assert await reader.fetchval("SELECT count(*) FROM rag_analytics.requests") > 2000
        assert not await reader.fetchval("SELECT has_table_privilege(current_user, 'rag_analytics.requests', 'UPDATE,INSERT,DELETE')")
        for sql in ("SELECT * FROM fixture_app.rag_requests", "DELETE FROM rag_analytics.requests",
                    "CREATE TABLE rag_analytics.forbidden(value int)"):
            try:
                await reader.execute(sql)
            except (asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError,
                    asyncpg.ObjectNotInPrerequisiteStateError):
                pass
            else:
                raise AssertionError("Reader unexpectedly permitted: " + sql)
    finally:
        await reader.close()
    print("PASS: real schema, custom schema name, delivery/fallback, web, late escalation, deduplication, reader permissions", flush=True)


async def wait_postgres(port, *, timeout_s=60):
    """Wait for SQL through the published TCP endpoint used by the fixtures.

    The image's temporary init server accepts Unix-socket connections before
    the final TCP server starts. A socket-only pg_isready probe is insufficient.
    """
    import asyncpg

    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        connection = None
        try:
            connection = await asyncpg.connect(
                host="127.0.0.1", port=port, user="test", password="isolated-only", database="test",
                timeout=min(2, max(.01, deadline - time.monotonic())), command_timeout=2,
            )
            await connection.fetchval("SELECT 1")
            return
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as error:
            last_error = error
        finally:
            if connection is not None:
                await connection.close(timeout=2)
        await asyncio.sleep(.25)
    raise RuntimeError("Isolated PostgreSQL did not become ready on its published TCP port") from last_error


def expand(sql, mode, channel="'telegram','web'", topic=None):
    if topic is None:
        # Custom All values bypass Grafana formatters; test the stored value verbatim.
        topic = document()["templating"]["list"][2]["allValue"]
    return (sql.replace("${data_mode:sqlstring}", "'" + mode + "'")
            .replace("${channel:sqlstring}", channel).replace("${topic:sqlstring}", topic))


def check_queries(grafana):
    now = datetime.now(timezone.utc)
    total = 0
    for mode, days, offset, channel, topic in (
        ("live", 7, 0, "'telegram','web'", None),
        ("demo", 7, 0, "'telegram','web'", None),
        ("demo", 7, 0, "'web'", "'Командировки'"),
        ("live", 7, 60, "'telegram','web'", None),
    ):
        values_by_title = {}
        for panel in document()["panels"]:
            for target in panel.get("targets", []):
                query = {**target, "rawSql": expand(target["rawSql"], mode, channel, topic),
                         "intervalMs": 86400000, "maxDataPoints": 1000}
                response = http(grafana, "/api/ds/query", {"from": str(int((now-timedelta(days=days+offset)).timestamp()*1000)),
                                "to": str(int((now-timedelta(days=offset)).timestamp()*1000)), "queries": [query]})
                result = response["results"]["A"]
                assert not result.get("error"), (panel["title"], result)
                assert result.get("status", 200) == 200, (panel["title"], result)
                values_by_title[panel["title"]] = result.get("frames", [])
                total += 1
        if mode == "live":
            def value(title):
                return values_by_title[title][0]["data"]["values"][0][0]
            if offset == 0:
                assert value("Принято вопросов") == 10
                assert value("Автоответы без эскалации") == 2
                assert abs(value("Доля успешных автоответов*") - 100*2/9) < 1e-6
                assert value("Передано оператору") == 3
                assert value("Ожидают результата") == 1
            else:
                assert value("Принято вопросов") == 0
                assert value("Доля успешных автоответов*") is None
                assert value("Медиана до ответа") is None
        variable_query = document()["templating"]["list"][2]["query"]
        http(grafana, "/api/ds/query", {"from": "now-7d", "to": "now", "queries": [{
            "refId": "A", "datasource": {"uid": "rag-business-postgres", "type": "grafana-postgresql-datasource"},
            "rawSql": expand(variable_query, mode), "format": "table", "rawQuery": True}]})
    print(f"PASS: {total} actual Grafana SQL targets; live/demo, channel/topic filters, previous periods and empty ranges", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="docker", choices=("docker", "podman"))
    parser.add_argument("--hold", action="store_true", help="Keep isolated Grafana until Ctrl+C for visual inspection")
    options = parser.parse_args()
    prefix = "rag-business-test-" + uuid4().hex[:10]
    created = []
    network_created = False

    def engine(*args):
        result = subprocess.run([options.engine, *map(str, args)], capture_output=True, text=True, encoding="utf-8", timeout=120)
        if result.returncode:
            raise RuntimeError("Isolated container command failed: " + result.stderr.strip())
        return (result.stdout + (result.stderr if args and args[0] == "logs" else "")).strip()

    def port(name, number):
        return int(engine("port", name, str(number) + "/tcp").splitlines()[0].rsplit(":", 1)[1])

    try:
        engine("network", "create", prefix)
        network_created = True
        pg = prefix + "-postgres"
        engine("run", "-d", "--name", pg, "--network", prefix, "--network-alias", "postgres",
               "-p", "127.0.0.1::5432", "-e", "POSTGRES_USER=test", "-e", "POSTGRES_DB=test",
               "-e", "POSTGRES_PASSWORD=isolated-only", "postgres:17-alpine")
        created.append(pg)
        asyncio.run(fixtures(port(pg, 5432)))
        name = prefix + "-grafana"
        args = ["run", "-d", "--name", name, "--network", prefix, "-p", "127.0.0.1::3000",
                "-e", "GF_AUTH_ANONYMOUS_ENABLED=true", "-e", "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer",
                "-e", "GF_ANALYTICS_REPORTING_ENABLED=false", "-e", "GF_USERS_ALLOW_SIGN_UP=false",
                "-e", "ANALYTICS_DATABASE=test", "-e", "ANALYTICS_PASSWORD=" + PASSWORD]
        for source, target in (("datasource.yaml", "/etc/grafana/provisioning/datasources/business.yaml"),
                               ("dashboards.yaml", "/etc/grafana/provisioning/dashboards/business.yaml"),
                               ("dashboards", "/var/lib/grafana/dashboards/business")):
            args += ["--mount", f"type=bind,src={ROOT / 'deploy' / 'monitoring' / source},dst={target},readonly"]
        engine(*args, "grafana/grafana:12.1.1")
        created.append(name)
        grafana = f"http://127.0.0.1:{port(name, 3000)}"
        wait_http(grafana, "/api/health")
        record = wait_http(grafana, "/api/dashboards/uid/rag-business")
        first_id = record["dashboard"]["id"]
        health = http(grafana, "/api/datasources/uid/rag-business-postgres/health")
        assert health["status"] == "OK", health
        check_queries(grafana)
        engine("restart", name)
        grafana = f"http://127.0.0.1:{port(name, 3000)}"
        record = wait_http(grafana, "/api/dashboards/uid/rag-business")
        assert record["dashboard"]["id"] == first_id
        print("PASS: provisioning and restart preserve dashboard ID", flush=True)
        if options.hold:
            print("VISUAL_QA_URL=" + grafana + "/d/rag-business?var-data_mode=demo&from=now-7d&to=now", flush=True)
            while True:
                time.sleep(1)
    except Exception:
        for name in created:
            print(f"Isolated container logs: {name}", flush=True)
            print(engine("logs", "--tail", "45", name), flush=True)
        raise
    finally:
        for name in reversed(created):
            engine("rm", "-f", name)
        if network_created:
            engine("network", "rm", prefix)


if __name__ == "__main__":
    main()
