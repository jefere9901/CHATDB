# ChatDB · 数据库智能助手

ChatDB 是一个跑在本机的数据库问答工具。你用中文提问，它结合当前库的表结构给出 SQL 和思路；你确认没问题后点「执行查询」，结果来自真实数据库。

前端是原生 HTML/CSS/JS，没有构建步骤；后端用 FastAPI，专家路由用 LangGraph；会话、知识库、附件和审计都存在本地 SQLite。装好依赖就能跑，不需要 Redis、PostgreSQL 这类额外基础设施。

## 能做什么

- **对话式写 SQL**：结合当前库的表结构，生成、纠错、优化查询，附带口径说明
- **点一下执行**：模型不会自己跑 SQL，回答里的语句由你点击执行，结果表格可以导出 CSV
- **看执行计划**：支持 PostgreSQL、MySQL、SQL Server、SQLite；Oracle 例外，原因见下
- **知识库问答**：上传业务文档后提问，回答会附上检索到的片段和来源
- **结构设计与故障诊断**：读表结构给 DDL 和索引建议；上传日志、执行计划后分析
- **会话管理**：历史对话、搜索、导出 Markdown，所有关键操作写审计

## 快速开始

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

也可以直接运行 `.\start.ps1`。启动后打开 http://127.0.0.1:8000 ，接口文档在 `/docs`。

第一次启动会自动生成一个演示库，位置是 `data/demo.sqlite3`，里面是 `departments` 和 `sales` 两张表、30 天的模拟订单。可以直接问「最近 7 天各部门销售额」试试。想重置数据，删掉这个文件再启动即可。

## 配置

```powershell
Copy-Item .env.example .env
```

常用配置项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 空 | 留空进入本地演示模式，填上则调用真实模型 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 任意 OpenAI 兼容接口地址 |
| `LLM_MODEL` | `deepseek-flash` | 换成你账号实际可用的模型名 |
| `APP_ACCESS_TOKEN` | 空 | 设置后所有业务接口要求 Bearer token |
| `DATABASE_URL` | 空 | 留空用演示库，填了连真实数据库 |
| `DATABASE_TYPE` | 自动推断 | `mysql` / `mariadb` / `postgresql` / `mssql` / `oracle` / `sqlite` |
| `DATABASE_SCHEMAS` | `public` | 多个 Schema 用逗号分隔 |
| `QUERY_TIMEOUT_SECONDS` | `10` | 单条查询超时 |
| `QUERY_ROW_LIMIT` | `200` | 最多返回行数 |
| `APP_DATA_DIR` | `./data` | 会话、知识库、审计的存放目录 |

没有 key 也能正常启动，这时是**本地演示模式**：读表结构、检索知识、执行查询都是真的，只有回答换成内置模板，不具备通用理解能力。填入 key 并重启就切换成真实模型。

密钥只放 `.env`（已加入 `.gitignore`），`.env.example` 保持空值，别往里写真实 key。

## 支持的数据源

| 类型 | 驱动 | 备注 |
| --- | --- | --- |
| MySQL | aiomysql | 本机 MySQL 8 实测通过，支持选库和字段注释 |
| MariaDB | aiomysql | 需显式设置 `DATABASE_TYPE=mariadb` |
| PostgreSQL | asyncpg | 支持多 Schema、只读事务、执行计划 |
| SQL Server | python-tds | 不用装 ODBC，走账号密码认证 |
| Oracle | oracledb Thin | 不用装 Instant Client，面向 12c 及以上 |
| SQLite | 内置 | 填服务器上已有文件的路径，只读打开 |

连接可以在网页「数据库 & 查询」里填表、测试、保存，也可以直接写 `DATABASE_URL`。通过页面保存的连接会立即生效，不用重启。

接入正式库时请用**只读账号**。页面和 SQL Server 连接串里的 `readonly` 只是提示，真正的权限边界在数据库账号上。

## 只读是怎么保证的

查询链路做了几层限制，任一层拦下就不会执行：

1. 用 sqlglot 解析 SQL，只放行单条 `SELECT` / `UNION` / `INTERSECT` / `EXCEPT`
2. 写入、DDL、锁、`SELECT INTO`、会话变量、序列操作直接拒绝
3. 函数走白名单，未知函数不放行
4. 表名必须存在于当前 Schema 的授权表清单里，CTE 不算越权
5. 执行时开只读事务，带超时和行数上限，PostgreSQL 还会限制 `search_path`
6. SQLite 额外挂 authorizer 回调，非读操作一律拒绝

这些是应用层的兜底，替代不了数据库账号权限，两边都要配。

## 目录结构

```text
app/main.py         FastAPI 路由、SSE 流式输出、附件上传
app/agents.py       LangGraph 专家路由与模型调用
app/database.py     只读查询、SQL 校验、Schema 读取
app/connections.py  连接参数校验、密码处理、公开配置
app/store.py        会话 / 文档切片 / 附件 / 审计
static/             前端页面与样式
tests/              回归测试
```

应用自身的数据（会话、知识库、审计）存在 `data/chatdb.sqlite3`，业务库和它是分开的。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check static/app.js
```

测试使用临时数据库，不会动 `data/` 下的数据。想跑真实 MySQL 的集成测试，先设置环境变量：

```powershell
$env:CHATDB_TEST_MYSQL = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:\CHATDB_TEST_MYSQL
```

## 目前的边界

- 只覆盖 SQL 关系型数据库，MongoDB、Redis、Elasticsearch 没有接入
- 应用存储还是 SQLite，向量检索、OCR、监控归因、用户反馈都没做
- 单进程运行，内部锁在进程内存里，不要对同一数据目录起多个 worker
- 访问令牌面向单工作空间，不能当作多租户隔离
- Oracle 的 `EXPLAIN PLAN` 会写计划表，所以页面不提供这个操作，可以上传已有的计划文件给助手分析

更细的操作步骤、各数据库的连接示例和已知坑，都在 [使用说明.md](使用说明.md) 里。
