"""Connection switching, credential privacy, and dialect boundaries."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from sqlalchemy.engine import make_url

from app.connections import ConnectionRequest, connection_url, public_connection, DATABASE_TYPES
from app.config import settings
from app.database import Database, QueryRejected, limited_sql, validate_sql


class ConnectionConfigTests(unittest.TestCase):
    def test_special_password_and_ipv6_roundtrip(self):
        password = "p@ss:/?# 中文'\""
        body = ConnectionRequest(type="mysql", host="::1", username="reader", password=password, database="业务库")
        url = connection_url(body)
        parsed = make_url(url.render_as_string(hide_password=False))
        self.assertEqual(parsed.password, password)
        self.assertEqual(parsed.host, "::1")
        public = public_connection(url, "mysql", "")
        self.assertNotIn(password, str(public))
        self.assertNotIn("password", public)
        self.assertTrue(public["has_password"])

    def test_reuse_only_on_same_server_account_and_type(self):
        url = connection_url(ConnectionRequest(type="mysql", host="localhost", username="reader", password="secret"))
        same = ConnectionRequest(type="mysql", host="localhost", username="reader", reuse_password=True, database="another")
        self.assertEqual(connection_url(same, url, "mysql").password, "secret")
        for field, value in [("host", "other"), ("username", "admin"), ("port", 3307), ("type", "mariadb")]:
            body = same.model_copy(update={field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                connection_url(body, url, "mysql")

    def test_oracle_service_and_sid(self):
        for mode in ["service_name", "sid"]:
            url = connection_url(ConnectionRequest(type="oracle", host="localhost", username="reader",
                                                   database="FREEPDB1", oracle_mode=mode))
            self.assertEqual(url.query.get("service_name") if mode == "service_name" else url.database, "FREEPDB1")

    def test_all_drivers_load_and_accept_url(self):
        for kind in DATABASE_TYPES:
            if kind == "sqlite":
                continue
            with self.subTest(kind=kind):
                body = ConnectionRequest(type=kind, host="localhost", username="reader", database="business")
                db = Database()
                db._configure(connection_url(body), kind, "")
                args, kwargs = db.engine.dialect.create_connect_args(db.url)
                self.assertIsNotNone(args)
                self.assertIsInstance(kwargs, dict)
                self.assertEqual(db.dialect, DATABASE_TYPES[kind]["dialect"])

    def test_sqlserver_and_oracle_limits_and_guards(self):
        self.assertIn("TOP", limited_sql("SELECT 1 AS n", "tsql"))
        self.assertIn("FETCH FIRST", limited_sql("SELECT 1 AS n", "oracle"))
        self.assertIn("FROM DUAL", limited_sql("SELECT 1 AS n", "oracle"))
        validate_sql("SELECT 1 FROM DUAL", "oracle", set(), "READER")
        for sql in ["SELECT * FROM orders WITH (UPDLOCK)", "SELECT * FROM orders OPTION (MAXDOP 1)",
                    "SELECT NEXT VALUE FOR seq FROM orders"]:
            with self.subTest(sql=sql), self.assertRaises(QueryRejected):
                validate_sql(sql, "tsql", {("dbo", "orders")}, "dbo")
        for dialect, schema in [("tsql", "dbo"), ("oracle", "READER")]:
            table = "ORDERS" if dialect == "oracle" else "orders"
            self.assertIn(schema, validate_sql("SELECT COUNT(*) FROM orders", dialect, {(schema, table)}, schema))
            for sql in ["DELETE FROM orders", "SELECT * INTO copy FROM orders",
                        "SELECT * FROM private.orders", "SELECT sequence.NEXTVAL FROM orders"]:
                with self.subTest(dialect=dialect, sql=sql), self.assertRaises(QueryRejected):
                    validate_sql(sql, dialect, {(schema, table)}, schema)
        with self.assertRaises(QueryRejected):
            validate_sql('SELECT * FROM "orders"', "oracle", {("READER", "ORDERS")}, "READER")

    def test_env_update_preserves_model_and_is_atomic(self):
        from app.config import save_database_url
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            env = path / ".env"
            env.write_text("LLM_API_KEY=keep-this\nDATABASE_URL=old\nDATABASE_SCHEMAS=public\n", encoding="utf-8")
            with patch("app.config.ROOT", path):
                save_database_url("sqlite:///example.db", "sqlite", "main")
            content = env.read_text(encoding="utf-8")
            self.assertIn("LLM_API_KEY=keep-this", content)
            self.assertIn("DATABASE_TYPE=sqlite", content)
            self.assertEqual(content.count("DATABASE_URL="), 1)
            self.assertEqual(len(list(path.iterdir())), 1)


class ConnectionSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = settings.database_url, settings.database_type, settings.schemas
        self.db = Database()
        self.db.demo_path = Path(self.tmp.name) / "demo.sqlite3"
        with patch.object(settings, "database_url", ""):
            await self.db.startup()

    async def asyncTearDown(self):
        await self.db.close()
        settings.database_url, settings.database_type, settings.schemas = self.original
        self.tmp.cleanup()

    async def test_sqlite_test_then_save_and_readonly_query(self):
        body = ConnectionRequest(type="sqlite", database=str(self.db.demo_path))
        original = self.db.connection_config()
        result = await self.db.connect(body)
        self.assertEqual(result["table_count"], 2)
        self.assertEqual(self.db.connection_config(), original)
        with patch("app.database.save_database_url") as save:
            await self.db.connect(body, persist=True)
            save.assert_called_once()
        self.assertFalse(self.db.is_demo)
        result = await self.db.execute("SELECT COUNT(*) FROM sales")
        self.assertEqual(result["rows"], [[360]])
        with self.assertRaises(QueryRejected):
            await self.db.execute("DELETE FROM sales")

    async def test_failure_does_not_replace_connection_or_save(self):
        body = ConnectionRequest(type="mysql", host="localhost", username="reader", password="hidden")
        before = self.db.connection_config()
        with patch.object(Database, "_probe", new=AsyncMock(side_effect=RuntimeError("bad connection"))), \
                patch("app.database.save_database_url") as save:
            with self.assertRaises(RuntimeError):
                await self.db.connect(body, persist=True)
            save.assert_not_called()
        self.assertEqual(self.db.connection_config(), before)
        self.assertEqual((await self.db.execute("SELECT 1"))["rows"], [[1]])

    async def test_persistence_failure_keeps_previous_database(self):
        body = ConnectionRequest(type="sqlite", database=str(self.db.demo_path))
        with patch("app.database.save_database_url", side_effect=OSError("permission denied")):
            with self.assertRaises(OSError):
                await self.db.connect(body, persist=True)
        self.assertTrue(self.db.is_demo)

    async def test_oracle_plan_refused_without_database_access(self):
        self.db.kind = "oracle"
        with self.assertRaises(QueryRejected):
            self.db._sync_execute("SELECT 1 FROM DUAL", True)
