import asyncio
import os
import ssl
import unittest
from unittest.mock import patch

from app.control.account.backends import sql as sql_backend
from app.platform.config.backends.sql import SqlConfigBackend


class _DummyEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class SqlEngineFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        sql_backend._ENGINE_CACHE.clear()
        sql_backend._ENGINE_KEYS_BY_ID.clear()

    def tearDown(self) -> None:
        sql_backend._ENGINE_CACHE.clear()
        sql_backend._ENGINE_KEYS_BY_ID.clear()

    def test_create_pgsql_engine_normalizes_url_and_extracts_ssl(self) -> None:
        sentinel = object()
        with patch.dict(
            os.environ,
            {
                "ACCOUNT_SQL_POOL_SIZE": "2",
                "ACCOUNT_SQL_MAX_OVERFLOW": "1",
                "ACCOUNT_SQL_POOL_TIMEOUT": "15",
                "ACCOUNT_SQL_POOL_RECYCLE": "600",
            },
            clear=False,
        ):
            # Clear serverless env vars to get non-serverless defaults
            with patch.dict(os.environ, {"VERCEL": "", "AWS_LAMBDA_FUNCTION_NAME": "", "FUNCTIONS_WORKER_RUNTIME": ""}, clear=False):
                with patch.object(sql_backend, "create_async_engine", return_value=sentinel) as create_engine:
                    engine = sql_backend.create_pgsql_engine(
                        "postgres://user:pass@example.com:5432/defaultdb?sslmode=require&application_name=grok2api"
                    )

        self.assertIs(engine, sentinel)
        create_engine.assert_called_once()
        args, kwargs = create_engine.call_args
        self.assertEqual(
            args[0],
            "postgresql+asyncpg://user:pass@example.com:5432/defaultdb?application_name=grok2api",
        )
        self.assertEqual(kwargs["pool_size"], 2)
        self.assertEqual(kwargs["max_overflow"], 1)
        self.assertEqual(kwargs["pool_timeout"], 15)
        self.assertEqual(kwargs["pool_recycle"], 600)
        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertTrue(kwargs["pool_use_lifo"])
        # SSL connect_args should be present
        self.assertIn("connect_args", kwargs)
        self.assertIsInstance(kwargs["connect_args"]["ssl"], ssl.SSLContext)

    def test_create_pgsql_engine_ssl_disabled_omits_connect_args(self) -> None:
        sentinel = object()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sql_backend, "create_async_engine", return_value=sentinel) as create_engine:
                engine = sql_backend.create_pgsql_engine(
                    "postgresql://user:pass@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=disable"
                )

        self.assertIs(engine, sentinel)
        create_engine.assert_called_once()
        _, kwargs = create_engine.call_args
        # sslmode=disable results in no connect_args
        self.assertNotIn("connect_args", kwargs)

    def test_create_pgsql_engine_prefix_is_normalized(self) -> None:
        """postgres:// → postgresql+asyncpg:// prefix normalisation."""
        sentinel = object()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sql_backend, "create_async_engine", return_value=sentinel) as create_engine:
                engine = sql_backend.create_pgsql_engine(
                    "postgres://user:pass@example.com:5432/testdb"
                )

        self.assertIs(engine, sentinel)
        create_engine.assert_called_once()
        args, _ = create_engine.call_args
        self.assertEqual(
            args[0],
            "postgresql+asyncpg://user:pass@example.com:5432/testdb",
        )

    def test_create_pgsql_engine_reuses_shared_engine_for_same_url(self) -> None:
        """When SSL is disabled (connect_args=None), identical URLs share one engine."""
        sentinel = object()
        with patch.object(sql_backend, "create_async_engine", return_value=sentinel) as create_engine:
            engine_a = sql_backend.create_pgsql_engine(
                "postgresql://user:pass@example.com:5432/defaultdb?sslmode=disable"
            )
            engine_b = sql_backend.create_pgsql_engine(
                "postgresql://user:pass@example.com:5432/defaultdb?sslmode=disable"
            )

        self.assertIs(engine_a, engine_b)
        create_engine.assert_called_once()

    def test_repository_close_disposes_and_evicts_cached_engine(self) -> None:
        engine = _DummyEngine()
        with patch.object(sql_backend, "create_async_engine", return_value=engine):
            shared = sql_backend.create_pgsql_engine(
                "postgresql://user:pass@example.com:5432/defaultdb?sslmode=require"
            )

        repo = sql_backend.SqlAccountRepository(shared, dialect="postgresql", dispose_engine=True)
        asyncio.run(repo.close())

        self.assertEqual(engine.dispose_calls, 1)
        self.assertEqual(sql_backend._ENGINE_CACHE, {})
        self.assertEqual(sql_backend._ENGINE_KEYS_BY_ID, {})

    def test_sql_config_backend_can_skip_disposing_shared_engine(self) -> None:
        engine = _DummyEngine()
        backend = SqlConfigBackend(engine, dialect="postgresql", dispose_engine=False)

        asyncio.run(backend.close())

        self.assertEqual(engine.dispose_calls, 0)

    def test_create_mysql_engine_strips_ssl_mode_from_url(self) -> None:
        sentinel = object()
        with patch.object(sql_backend, "create_async_engine", return_value=sentinel) as create_engine:
            engine = sql_backend.create_mysql_engine(
                "mysql://user:pass@example.com:3306/defaultdb?ssl-mode=REQUIRED&charset=utf8mb4"
            )

        self.assertIs(engine, sentinel)
        create_engine.assert_called_once()
        args, kwargs = create_engine.call_args
        self.assertEqual(
            args[0],
            "mysql+aiomysql://user:pass@example.com:3306/defaultdb?charset=utf8mb4",
        )
        self.assertIn("connect_args", kwargs)
        self.assertIsInstance(kwargs["connect_args"]["ssl"], ssl.SSLContext)


if __name__ == "__main__":
    unittest.main()
