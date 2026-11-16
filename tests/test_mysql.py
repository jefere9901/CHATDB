"""可选真实 MySQL 集成验证：只读元数据、合成查询及执行计划，不改动业务数据。"""
import os
import unittest

from app.config import settings
from app.database import Database, QueryRejected


@unittest.skipUnless(os.getenv("CHATDB_TEST_MYSQL") == "1", "设置 CHATDB_TEST_MYSQL=1 后验证本地 MySQL")
class MySQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not settings.database_url.startswith("mysql"):
            self.skipTest("未配置 MySQL DATABASE_URL")
        self.db = Database()
        await self.db.startup()
        available = await self.db.databases()
        if not self.db.mysql_database:
            if not available:
                await self.db.close()
                self.skipTest("没有可用业务数据库")
            # 仅为测试临时选库，不写入用户 .env。
            await self.db.select_mysql_database(available[0])

    async def asyncTearDown(self):
        await self.db.close()

    async def test_real_schema_query_and_explain(self):
        schema = await self.db.schema()
        self.assertTrue(all(table["schema"] == self.db.mysql_database for table in schema))
        result = await self.db.execute("SELECT 1 AS connection_ok")
        self.assertEqual(result["rows"], [[1]])
        plan = await self.db.execute("SELECT 1 AS connection_ok", explain=True)
        self.assertTrue(plan["explain"])
        self.assertTrue(plan["rows"])

    async def test_synthetic_result_limit(self):
        result = await self.db.execute("""
            WITH RECURSIVE numbers AS (
                SELECT 1 AS n UNION ALL
                SELECT n + 1 FROM numbers WHERE n < 300
            ) SELECT n FROM numbers
        """)
        self.assertEqual(len(result["rows"]), min(settings.row_limit, 300))
        self.assertEqual(result["truncated"], settings.row_limit < 300)

    async def test_writes_and_system_schema_are_blocked(self):
        for sql in ["DELETE FROM forbidden_table", "SELECT * FROM mysql.user", "SELECT SLEEP(20)"]:
            with self.subTest(sql=sql):
                with self.assertRaises(QueryRejected):
                    await self.db.execute(sql)
