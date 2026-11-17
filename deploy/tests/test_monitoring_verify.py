"""Regression coverage for PostgreSQL startup races in the analytics verifier."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import asyncpg

from deploy.monitoring import verify


class PostgresReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_tcp_sql_after_connection_resets_and_startup_rejection(self):
        ready = AsyncMock()
        connect = AsyncMock(side_effect=[
            ConnectionRefusedError(), ConnectionResetError(),
            asyncpg.CannotConnectNowError("database system is starting up"), ready,
        ])
        with patch.object(asyncpg, "connect", connect), patch.object(verify.asyncio, "sleep", AsyncMock()):
            await verify.wait_postgres(15432)
        self.assertEqual(connect.await_count, 4)
        self.assertTrue(all(call.kwargs["host"] == "127.0.0.1" and call.kwargs["port"] == 15432
                            for call in connect.await_args_list))
        ready.fetchval.assert_awaited_once_with("SELECT 1")
        ready.close.assert_awaited_once()

    async def test_connection_without_a_successful_query_is_not_ready(self):
        initializing, ready = AsyncMock(), AsyncMock()
        initializing.fetchval.side_effect = ConnectionResetError()
        with patch.object(asyncpg, "connect", AsyncMock(side_effect=[initializing, ready])), \
                patch.object(verify.asyncio, "sleep", AsyncMock()):
            await verify.wait_postgres(15432)
        initializing.close.assert_awaited_once()
        ready.fetchval.assert_awaited_once_with("SELECT 1")
        ready.close.assert_awaited_once()

    async def test_deadline_fails_instead_of_continuing_to_fixtures(self):
        clock = Mock()
        clock.monotonic.return_value = 0

        async def advance(seconds):
            clock.monotonic.return_value += seconds

        with patch.object(verify, "time", clock), \
                patch.object(verify.asyncio, "sleep", AsyncMock(side_effect=advance)), \
                patch.object(asyncpg, "connect", AsyncMock(side_effect=ConnectionResetError())):
            with self.assertRaisesRegex(RuntimeError, "published TCP port") as caught:
                await verify.wait_postgres(15432, timeout_s=1)
        self.assertIsInstance(caught.exception.__cause__, ConnectionResetError)

    async def test_credentials_errors_are_not_hidden_by_startup_retries(self):
        connect = AsyncMock(side_effect=asyncpg.InvalidPasswordError("invalid test credentials"))
        with patch.object(asyncpg, "connect", connect):
            with self.assertRaises(asyncpg.InvalidPasswordError):
                await verify.wait_postgres(15432)
        self.assertEqual(connect.await_count, 1)
