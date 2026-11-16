import asyncio
import csv
import io
import json
import re
import secrets
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agents import AGENTS, generate, graph
from .config import ROOT, settings
from .database import QueryRejected, database, in_thread
from .connections import ConnectionRequest, catalog
from .store import store

active_sessions = set()
database_switching = False
security = HTTPBearer(auto_error=False)


async def authorize(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if settings.access_token:
        if not credentials or not secrets.compare_digest(credentials.credentials, settings.access_token):
            raise HTTPException(401, "访问令牌无效。")


@asynccontextmanager
async def lifespan(app):
    store.init()
    await database.startup()
    yield
    await database.close()


app = FastAPI(title="ChatDB Agent", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith("/api/database/connection"):
        # Pydantic 默认错误可能带上请求原文（包括密码）。
        return JSONResponse(status_code=422, content={"detail": "连接参数格式不正确，请检查端口和字段长度。"})
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": exc.errors()}))


@app.get("/api/database/connection", dependencies=[Depends(authorize)])
async def connection_settings():
    return {"types": catalog(), "connection": database.connection_config(), "demo": database.is_demo}


@app.post("/api/database/connection/test", dependencies=[Depends(authorize)])
async def test_connection(body: ConnectionRequest):
    try:
        return await database.connect(body)
    except (ValueError, QueryRejected):
        raise HTTPException(400, "连接参数无效，请检查类型、主机、账号、Schema 和文件路径；更换主机或账号时需重填密码。")
    except Exception:
        raise HTTPException(503, "连接测试失败，请检查服务、端口、账号密码及读取表结构的权限。")


@app.post("/api/database/connection", dependencies=[Depends(authorize)])
async def save_connection(body: ConnectionRequest):
    global database_switching
    if active_sessions or database_switching:
        raise HTTPException(409, "请先停止正在生成的回答，再切换连接。")
    database_switching = True
    try:
        result = await database.connect(body, persist=True)
        store.log(None, "database_connection_saved", {"type": database.kind,
                                                     "database": database.connection_config()["database"]})
        return result
    except (ValueError, QueryRejected):
        raise HTTPException(400, "连接参数无效，请检查类型、主机、账号、Schema 和文件路径；更换主机或账号时需重填密码。")
    except Exception:
        raise HTTPException(503, "连接或保存失败，请检查服务、账号权限和 .env 文件。")
    finally:
        database_switching = False


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(ROOT / "static" / "index.html"))


@app.get("/api/status", dependencies=[Depends(authorize)])
async def status():
    return {"name": "ChatDB", "mode": "live" if settings.api_key else "demo",
            "model": settings.model if settings.api_key else "本地演示",
            "database": database.label,
            "demo_database": database.is_demo,
            "selected_database": database.mysql_database if database.dialect == "mysql" else None,
            "dialect": database.dialect, "agents": AGENTS,
            "authenticated": bool(settings.access_token), "row_limit": settings.row_limit}


def require_session(session_id):
    if not store.session(session_id):
        raise HTTPException(404, "会话不存在。")


@app.get("/api/sessions", dependencies=[Depends(authorize)])
async def list_sessions():
    return store.sessions()


@app.post("/api/sessions", dependencies=[Depends(authorize)], status_code=201)
async def create_session():
    return store.create_session()


@app.get("/api/sessions/{session_id}", dependencies=[Depends(authorize)])
async def session_detail(session_id: str):
    require_session(session_id)
    return {"session": store.session(session_id), "messages": store.messages(session_id)}


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(authorize)])
async def delete_session(session_id: str):
    require_session(session_id)
    if session_id in active_sessions:
        raise HTTPException(409, "请先停止正在生成的回答。")
    store.delete_session(session_id)
    return {"ok": True}


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    agent: str = "auto"
    attachment_ids: List[str] = Field(default_factory=list, max_length=3)


def sse(event, data):
    return "event: {}\ndata: {}\n\n".format(event, json.dumps(data, ensure_ascii=False, default=str))


@app.post("/api/sessions/{session_id}/chat", dependencies=[Depends(authorize)])
async def chat(session_id: str, body: ChatRequest):
    require_session(session_id)
    if database_switching:
        raise HTTPException(409, "数据库连接正在切换，请稍后重试。")
    if not body.content.strip():
        raise HTTPException(422, "请输入问题。")
    if body.agent not in AGENTS:
        raise HTTPException(422, "不支持的专家类型。")
    if session_id in active_sessions:
        raise HTTPException(409, "当前会话已有回答正在生成。")
    attachments = []
    for attachment_id in body.attachment_ids:
        attachment = store.attachment(attachment_id)
        if not attachment:
            raise HTTPException(404, "附件不存在，请重新上传。")
        attachments.append(attachment)
    active_sessions.add(session_id)
    try:
        user_id = store.add_message(session_id, "user", body.content.strip(),
                                   {"attachments": [{"id": a["id"], "name": a["name"]} for a in attachments]})
        store.log(session_id, "question", {"message_id": user_id, "content": body.content, "agent": body.agent,
                                          "attachments": body.attachment_ids})
        messages = store.messages(session_id)
        assistant_id = store.add_message(session_id, "assistant", "", {"status": "streaming"})
    except BaseException:
        active_sessions.discard(session_id)
        raise

    async def events():
        answer = ""
        meta = {"status": "streaming", "agent": body.agent, "traces": [], "references": []}
        queue = asyncio.Queue()

        async def produce():
            try:
                state = await graph.ainvoke({"query": body.content, "requested_agent": body.agent})
                meta.update({"agent": state["agent"], "traces": state["traces"],
                             "references": state["references"], "mode": "live" if settings.api_key else "demo"})
                await queue.put(("context", meta.copy()))
                async for token in generate(state, messages, attachments):
                    await queue.put(("delta", {"text": token}))
                await queue.put(("complete", {}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不把数据库连接信息、密钥或堆栈返回浏览器。
                detail = str(exc) if isinstance(exc, RuntimeError) else "处理失败，请检查模型或数据库配置。"
                await queue.put(("failure", {"message": detail}))

        async def bounded_produce():
            try:
                await asyncio.wait_for(produce(), timeout=180)
            except asyncio.TimeoutError:
                await queue.put(("failure", {"message": "生成超过 180 秒，已停止，请缩短问题后重试。"}))

        task = asyncio.create_task(bounded_produce())
        try:
            yield sse("start", {"user_id": user_id, "message_id": assistant_id})
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=8)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event == "delta":
                    answer += payload["text"]
                elif event == "complete":
                    meta["status"] = "complete"
                    store.update_message(assistant_id, answer, meta)
                    store.log(session_id, "answer", {"message_id": assistant_id, "content": answer,
                                                    "sql": re.findall(r"```sql\s*\n(.*?)```", answer, re.S | re.I),
                                                    "metadata": meta})
                    yield sse("done", {"message_id": assistant_id, "metadata": meta})
                    break
                elif event == "failure":
                    meta["status"] = "error"
                    meta["error"] = payload["message"]
                    answer += "\n\n" + payload["message"]
                    yield sse("error", payload)
                    break
                yield sse(event, payload)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if meta["status"] == "streaming":
                meta["status"] = "interrupted"
            store.update_message(assistant_id, answer, meta)
            if meta["status"] != "complete":
                store.log(session_id, "answer_" + meta["status"],
                          {"message_id": assistant_id, "content": answer, "metadata": meta})
            active_sessions.discard(session_id)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/database/schema", dependencies=[Depends(authorize)])
async def get_schema():
    try:
        return {"dialect": database.dialect, "database": database.connection_config()["database"],
                "demo": database.is_demo, "explain_supported": database.kind != "oracle",
                "tables": await database.schema(),
                "databases": await database.databases() if database.dialect == "mysql" else []}
    except Exception:
        raise HTTPException(503, "数据库连接失败，请检查只读账号和连接配置。")


class DatabaseSelection(BaseModel):
    database: str = Field(min_length=1, max_length=128)


@app.post("/api/database/select", dependencies=[Depends(authorize)])
async def select_database(body: DatabaseSelection):
    global database_switching
    if active_sessions or database_switching:
        raise HTTPException(409, "请先停止正在生成的回答，再选择数据库。")
    database_switching = True
    try:
        await database.select_mysql_database(body.database, persist=True)
        store.log(None, "database_selected", {"database": body.database})
        return {"ok": True, "database": body.database}
    except QueryRejected as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(503, "数据库选择或配置保存失败，请检查账号权限和 .env 文件。")
    finally:
        database_switching = False


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    session_id: Optional[str] = None
    explain: bool = False


@app.post("/api/database/query", dependencies=[Depends(authorize)])
async def query(body: QueryRequest):
    if body.session_id:
        require_session(body.session_id)
    try:
        store.log(body.session_id, "query_requested", {"sql": body.sql, "explain": body.explain})
        result = jsonable_encoder(await database.execute(body.sql, body.explain))
        store.log(body.session_id, "explain" if body.explain else "query",
                  {"sql": body.sql, "status": "success", "result": result})
        return result
    except QueryRejected as exc:
        store.log(body.session_id, "query_blocked", {"sql": body.sql, "reason": str(exc)})
        raise HTTPException(400, str(exc))
    except Exception:
        store.log(body.session_id, "query_failed", {"sql": body.sql, "reason": "查询失败或超时"})
        raise HTTPException(400, "查询失败或超时，请检查 SQL 字段、权限和数据库连接。")


ALLOWED_EXTENSIONS = {".txt", ".md", ".log", ".csv", ".json", ".sql", ".pdf", ".docx", ".html"}


def extract_text(content, suffix):
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) > 150:
            raise ValueError("PDF 最多支持 150 页。")
        value = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif suffix == ".docx":
        from docx import Document
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(item.file_size for item in entries) > 50 * 1024 * 1024:
                raise ValueError("DOCX 解压内容过大，请拆分文档。")
        document = Document(io.BytesIO(content))
        value = "\n".join(p.text for p in document.paragraphs)
        value += "\n" + "\n".join(" | ".join(c.text for c in row.cells)
                                  for table in document.tables for row in table.rows)
    else:
        try:
            value = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            value = content.decode("gb18030")
        if suffix == ".html":
            from html.parser import HTMLParser
            class TextParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                    self.hidden = 0
                def handle_starttag(self, tag, attrs):
                    if tag in {"script", "style"}:
                        self.hidden += 1
                def handle_endtag(self, tag):
                    if tag in {"script", "style"}:
                        self.hidden = max(0, self.hidden - 1)
                def handle_data(self, data):
                    if not self.hidden:
                        self.parts.append(data)
            parser = TextParser()
            parser.feed(value)
            value = "\n".join(parser.parts)
    if not value.strip():
        raise ValueError("没有提取到正文；扫描 PDF 需要先转换为文本。")
    if len(value) > 200000:
        raise ValueError("解析后正文超过 200,000 字符，请拆分文档。")
    return value.strip()


async def parse_upload(file):
    name = Path((file.filename or "document").replace("\\", "/")).name[:160]
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "支持 TXT、MD、LOG、CSV、JSON、SQL、PDF、DOCX 和 HTML。")
    content = bytearray()
    try:
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(413, "文件不能超过 10MB。")
        value = await in_thread(extract_text, bytes(content), suffix)
        return name, value
    except HTTPException:
        raise
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ValueError, UnicodeDecodeError)) else "文件解析失败，请检查文件格式。"
        raise HTTPException(400, detail)
    finally:
        await file.close()


@app.post("/api/attachments", dependencies=[Depends(authorize)], status_code=201)
async def upload_attachment(file: UploadFile = File(...)):
    name, content = await parse_upload(file)
    attachment_id = store.add_attachment(name, content)
    store.log(None, "attachment_uploaded", {"id": attachment_id, "name": name, "characters": len(content)})
    return {"id": attachment_id, "name": name, "characters": len(content)}


@app.get("/api/knowledge", dependencies=[Depends(authorize)])
async def list_knowledge():
    return store.documents()


@app.post("/api/knowledge", dependencies=[Depends(authorize)], status_code=201)
async def upload_knowledge(file: UploadFile = File(...)):
    name, content = await parse_upload(file)
    document_id = store.add_document(name, name, content)
    store.log(None, "knowledge_uploaded", {"id": document_id, "name": name})
    return {"id": document_id, "name": name}


@app.delete("/api/knowledge/{document_id}", dependencies=[Depends(authorize)])
async def delete_knowledge(document_id: str):
    if not store.delete_document(document_id):
        raise HTTPException(404, "文档不存在。")
    store.log(None, "knowledge_deleted", {"id": document_id})
    return {"ok": True}


@app.get("/api/audit", dependencies=[Depends(authorize)])
async def audit():
    return store.audit()


@app.get("/api/sessions/{session_id}/export", dependencies=[Depends(authorize)])
async def export_session(session_id: str):
    require_session(session_id)
    content = "# " + store.session(session_id)["title"] + "\n\n"
    for message in store.messages(session_id):
        content += "## " + ("用户" if message["role"] == "user" else "ChatDB") + "\n\n" + message["content"] + "\n\n"
    return Response(content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="chatdb-conversation.md"'})
