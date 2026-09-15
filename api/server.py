"""本地管理 API（FastAPI）。

安全说明：
- 默认仅监听 127.0.0.1，不要改成 0.0.0.0（该接口能启动浏览器、读写账号数据）
- 默认开启 Token 认证：首次启动生成 data/api_token，请求需带 X-API-Token
  面板页面会提示输入 Token 并存到 localStorage
- 若关闭认证（api.auth_enabled=false），同机任意进程都能下发任务，请自行评估风险
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from account import describe
from browser import get_profile_manager
from config import load_config
from database import get_db
from flow import CheckpointManager, list_flows, stats_snapshot as captcha_stats
from logger import get_buffer, get_logger, setup_from_config
from proxy import get_proxy_manager
from task import get_task_manager

STATIC_DIR = Path(__file__).parent / "static"


# ---------- 请求模型 ----------
class AccountIn(BaseModel):
    account: str = Field(..., min_length=1)
    password: str = ""
    note: str = ""


class AccountsText(BaseModel):
    text: str = ""


class TaskIn(BaseModel):
    account: str = ""
    type: str = "login"
    priority: int = 0
    password: str = ""
    max_attempt: Optional[int] = None


class BatchIn(BaseModel):
    accounts: Optional[List[str]] = None
    type: str = "login"
    limit: int = 20
    priority: int = 0


class WorkersIn(BaseModel):
    workers: Optional[int] = None
    restore: bool = True


class ConfigIn(BaseModel):
    data: Dict[str, Any]


# ---------- 独立执行进程管理（OutlookRegister 模式） ----------
def _pid_alive(pid: int) -> bool:
    """检查进程是否存活（不等待、不发信号）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


class WorkerProcessManager:
    """管理独立执行进程（main.py work）的生命周期。面板进程本身不开浏览器。"""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.requested_workers = 0

    def _pid_file(self) -> Path:
        return self.cfg.resolve("data/worker.pid")

    def external_alive(self) -> tuple:
        """检测是否有面板之外启动的执行进程（如手动 python main.py work）。"""
        pf = self._pid_file()
        if pf.is_file():
            try:
                pid = int(pf.read_text(encoding="utf-8").strip() or 0)
            except ValueError:
                pid = 0
            if pid and _pid_alive(pid):
                return True, pid
        return False, 0

    def alive(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        # 面板重启后重新接管外部进程的状态显示
        ext, _pid = self.external_alive()
        return ext

    def start(self, workers: Optional[int] = None) -> Dict[str, Any]:
        n = int(workers or self.cfg.get("system.max_workers", 3))
        n = max(1, min(n, 16))
        if self.alive():
            self.requested_workers = n
            return {"ok": True, "already": True, "workers": n}

        # 清理残留的过期 pid 文件
        ext, ext_pid = self.external_alive()
        if not ext and self._pid_file().is_file():
            try:
                self._pid_file().unlink()
            except OSError:
                pass

        log_dir = self.cfg.path_of("logger.dir", "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        out_f = open(log_dir / "worker.out", "ab")
        err_f = open(log_dir / "worker.err", "ab")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        if getattr(sys, "frozen", False):
            # PyInstaller 打包后：直接用 exe 自身拉起 work（main.py 不存在）
            worker_cmd = [sys.executable, "work", "--workers", str(n)]
        else:
            worker_cmd = [sys.executable, str(self.cfg.root / "main.py"), "work", "--workers", str(n)]
        self.proc = subprocess.Popen(
            worker_cmd,
            cwd=str(self.cfg.root),
            stdout=out_f,
            stderr=err_f,
            creationflags=creationflags,
        )
        self.started_at = time.time()
        self.requested_workers = n
        self.log.ok("worker_proc", f"已拉起执行进程 PID={self.proc.pid} workers={n}")
        return {"ok": True, "pid": self.proc.pid, "workers": n}

    def stop(self, timeout: float = 20.0) -> Dict[str, Any]:
        stopped = []
        # ① 我们拉起的子进程：CTRL_BREAK 优雅停止，超时强杀
        if self.proc is not None and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.terminate()
            except Exception as exc:
                self.log.warn("worker_proc", f"发送停止信号失败: {exc}")
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.log.warn("worker_proc", "优雅停止超时，已强制结束")
            stopped.append(self.proc.pid)
        self.proc = None

        # ② 外部启动的执行进程（面板重启后接管场景）：按 pid 文件强杀
        ext, ext_pid = self.external_alive()
        if ext:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(ext_pid)],
                                   capture_output=True, timeout=10)
                else:
                    os.kill(ext_pid, signal.SIGTERM)
                stopped.append(ext_pid)
            except Exception as exc:
                self.log.warn("worker_proc", f"停止外部执行进程 {ext_pid} 失败: {exc}")

        if stopped:
            self.log.info("worker_proc", f"执行进程已停止: {stopped}")
        return {"ok": True, "stopped": stopped}


# ---------- Token ----------
def _resolve_token(cfg) -> str:
    """读取或生成 API Token。文件优先于配置，便于轮换。"""
    configured = str(cfg.get("api.token") or "").strip()
    if configured:
        return configured
    token_file = cfg.resolve("data/api_token")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if token_file.is_file():
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    token_file.write_text(token, encoding="utf-8")
    return token


def create_app(cfg=None) -> FastAPI:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    setup_from_config(cfg)
    log = get_logger(name="api", flow="API")

    db = get_db(cfg.path_of("database.path", "data/app.db"))
    tm = get_task_manager(cfg, logger=log)
    pm = get_profile_manager(cfg, db, logger=log)
    proxy_mgr = get_proxy_manager(cfg.section("proxy"), logger=log)
    wpm = WorkerProcessManager(cfg, log)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        # 面板关闭时不停止执行进程（独立性是设计目标）；只停定时任务
        tm.stop_scheduler()

    auth_enabled = bool(cfg.get("api.auth_enabled", True))
    token = _resolve_token(cfg) if auth_enabled else ""

    app = FastAPI(title="OutlookAutomation", version="1.3.0", docs_url="/docs", lifespan=_lifespan)

    def require_token(x_api_token: Optional[str] = Header(default=None)) -> None:
        if not auth_enabled:
            return
        if not x_api_token or not secrets.compare_digest(x_api_token, token):
            raise HTTPException(status_code=401, detail="无效或缺失 X-API-Token")

    guard = [Depends(require_token)]

    # ---------- 页面 ----------
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return FileResponse(str(page))
        return JSONResponse({"message": "面板文件缺失，请访问 /docs 使用 API"})

    @app.get("/api/meta")
    def meta():
        """无需认证：供面板判断是否需要 Token。"""
        return {
            "name": "OutlookAutomation",
            "version": "1.3.0",
            "auth_enabled": auth_enabled,
            "host": cfg.get("api.host"),
            "port": cfg.get("api.port"),
        }

    # ---------- 状态 ----------
    @app.get("/api/stats", dependencies=guard)
    def stats():
        alive = wpm.alive()
        counts = db.count_tasks()
        completed = counts.get("COMPLETED", 0)
        failed = counts.get("FAILED", 0)

        # 子进程内存中的实时统计（验证码通过率等）从落盘文件读取
        live: Dict[str, Any] = {}
        stats_file = cfg.resolve("data/worker_stats.json")
        if alive and stats_file.is_file():
            try:
                live = json.loads(stats_file.read_text(encoding="utf-8"))
            except Exception:
                live = {}

        workers_view = []
        if alive:
            pid = wpm.proc.pid if wpm.proc is not None else "external"
            workers_view.append(
                {
                    "index": 1,
                    "name": f"exec-pid{pid}",
                    "alive": True,
                    "processed": live.get("processed", completed + failed),
                    "succeeded": live.get("succeeded", completed),
                    "failed": live.get("failed", failed),
                    "current_task": None,
                    "current_account": "",
                    "uptime": round(time.time() - (wpm.started_at or time.time()), 1),
                }
            )

        return {
            "ts": time.time(),
            "task": {
                "running": alive,
                "mode": "subprocess",
                "uptime": round(time.time() - wpm.started_at, 1) if alive and wpm.started_at else 0,
                "workers": workers_view,
                "worker_count": len(workers_view),
                "queue": tm.queue.snapshot(),
                "tasks": counts,
                "accounts": tm.am.stats(),
                "browsers": live.get("browsers", 0),
                "captcha": live.get("captcha", captcha_stats()),
                "processed": live.get("processed", completed + failed),
                "succeeded": live.get("succeeded", completed),
                "failed": live.get("failed", failed),
                "flows": sorted(list_flows()),
                "scheduler": tm.scheduler_jobs(),
            },
            "db": db.stats(),
            "captcha": live.get("captcha", captcha_stats()),
            "proxy": proxy_mgr.snapshot(),
            "profiles": {"count": len(pm.list_dirs())},
        }

    # ---------- 账号 ----------
    @app.get("/api/accounts", dependencies=guard)
    def list_accounts(
        status: Optional[str] = None, limit: int = Query(200, le=2000), offset: int = 0
    ):
        items = tm.am.list(status=status, limit=limit, offset=offset)
        return {
            "total": sum(db.count_accounts().values()),
            "items": [
                {**a.to_dict(), "status_label": describe(a.status)} for a in items
            ],
            "stats": tm.am.stats(),
        }

    @app.post("/api/accounts", dependencies=guard)
    def add_account(body: AccountIn):
        account_id = tm.am.add(body.account, body.password, body.note)
        return {"ok": True, "id": account_id}

    @app.post("/api/accounts/import", dependencies=guard)
    def import_accounts(body: Optional[AccountsText] = None):
        """带 text 从文本导入；不带则读取 config.system.accounts_file。"""
        if body and body.text.strip():
            return tm.am.import_text(body.text)
        return tm.am.import_file()

    @app.delete("/api/accounts/{account}", dependencies=guard)
    def delete_account(account: str):
        tm.am.remove(account)
        return {"ok": True}

    @app.post("/api/accounts/batch_delete", dependencies=guard)
    def delete_batch_accounts(body: Dict[str, Any]):
        """批量删除账号（面板多选删除用）。"""
        accounts = body.get("accounts", [])
        if not isinstance(accounts, list) or not accounts:
            raise HTTPException(status_code=400, detail="accounts 必须是账号列表")
        for acc in accounts:
            tm.am.remove(str(acc))
        return {"ok": True, "deleted": len(accounts)}

    @app.post("/api/accounts/batch_reset", dependencies=guard)
    def reset_batch_accounts(body: Dict[str, Any]):
        """批量重置账号状态为 NEW（面板多选重置用）。"""
        accounts = body.get("accounts", [])
        if not isinstance(accounts, list) or not accounts:
            raise HTTPException(status_code=400, detail="accounts 必须是账号列表")
        for acc in accounts:
            tm.am.reset_status(str(acc))
        return {"ok": True, "reset": len(accounts)}

    @app.post("/api/accounts/{account}/reset", dependencies=guard)
    def reset_account(account: str):
        tm.am.reset_status(account)
        return {"ok": True}

    @app.get("/api/accounts/export", dependencies=guard)
    def export_accounts():
        path = tm.am.export_csv(cfg.resolve("data/accounts_export.csv"))
        return FileResponse(str(path), filename=path.name)

    # ---------- 任务 ----------
    @app.get("/api/tasks", dependencies=guard)
    def list_tasks(
        status: Optional[str] = None,
        type: Optional[str] = None,
        limit: int = Query(100, le=1000),
        offset: int = 0,
    ):
        items = tm.db.list_tasks(status=status, task_type=type, limit=limit, offset=offset)
        return {"counts": db.count_tasks(), "items": [t.to_dict() for t in items]}

    @app.post("/api/tasks", dependencies=guard)
    def create_task(body: TaskIn):
        if not body.account:
            raise HTTPException(status_code=400, detail="account 不能为空")
        task = tm.submit(
            account=body.account,
            task_type=body.type,
            priority=body.priority,
            password=body.password,
            max_attempt=body.max_attempt,
        )
        return {"ok": True, "task": task.to_dict()}

    @app.post("/api/tasks/batch", dependencies=guard)
    def create_batch(body: BatchIn):
        tasks = tm.submit_batch(
            accounts=body.accounts, task_type=body.type, limit=body.limit, priority=body.priority
        )
        return {"ok": True, "count": len(tasks), "tasks": [t.to_dict() for t in tasks]}

    @app.get("/api/tasks/{task_id}", dependencies=guard)
    def get_task(task_id: int):
        task = db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        ckpt = CheckpointManager(db, task_id=task_id, account=task.account)
        return {
            "task": task.to_dict(),
            "checkpoints": ckpt.timeline(),
            "events": db.list_events(task_id=task_id, limit=200),
        }

    @app.post("/api/tasks/{task_id}/cancel", dependencies=guard)
    def cancel_task(task_id: int):
        ok = tm.cancel(task_id)
        if not ok:
            raise HTTPException(status_code=400, detail="任务不可取消（不存在或已结束）")
        return {"ok": True}

    @app.delete("/api/tasks/{task_id}", dependencies=guard)
    def delete_task(task_id: int):
        db.delete_task(task_id)
        return {"ok": True}

    @app.post("/api/tasks/clear", dependencies=guard)
    def clear_tasks(statuses: Optional[List[str]] = None):
        removed = db.clear_tasks(statuses)
        return {"ok": True, "removed": removed}

    # ---------- Worker（独立执行进程） ----------
    @app.post("/api/workers/start", dependencies=guard)
    def start_workers(body: Optional[WorkersIn] = None):
        body = body or WorkersIn()
        result = wpm.start(body.workers)
        tm.start_scheduler()
        return result

    @app.post("/api/workers/stop", dependencies=guard)
    def stop_workers():
        return wpm.stop()

    @app.post("/api/workers/restart", dependencies=guard)
    def restart_workers(body: Optional[WorkersIn] = None):
        wpm.stop()
        return wpm.start((body.workers if body else None))

    @app.get("/api/queue", dependencies=guard)
    def queue_info():
        return tm.queue.snapshot()

    @app.post("/api/queue/clear", dependencies=guard)
    def queue_clear():
        return {"ok": True, "cancelled": tm.clear_queue()}

    # ---------- 浏览器 / Profile ----------
    @app.get("/api/browsers", dependencies=guard)
    def list_browsers():
        # 浏览器在独立执行进程内，面板进程看不到实例明细
        return {
            "active": "-",
            "items": [],
            "note": "浏览器运行在独立执行进程中，此处不显示实例明细",
        }

    @app.post("/api/browsers/close_all", dependencies=guard)
    def close_browsers():
        # 跨进程无法直接关闭；停止执行进程即可全部收尾
        return {"ok": False, "note": "请使用「停止」结束执行进程来关闭全部浏览器"}

    @app.get("/api/profiles", dependencies=guard)
    def list_profiles():
        return pm.snapshot()

    @app.delete("/api/profiles/{profile_id}", dependencies=guard)
    def delete_profile(profile_id: str):
        return {"ok": pm.delete(profile_id)}

    @app.post("/api/profiles/clear_temp", dependencies=guard)
    def clear_temp_profiles():
        return {"ok": True, "removed": pm.clear_temporary()}

    @app.post("/api/profiles/prune", dependencies=guard)
    def prune_profiles(days: float = 30.0):
        return {"ok": True, "removed": pm.prune_older_than(days)}

    # ---------- 代理 ----------
    @app.get("/api/proxy", dependencies=guard)
    def proxy_info():
        from proxy import get_resin

        data = proxy_mgr.snapshot()
        data["resin"] = get_resin().snapshot()
        return data

    @app.post("/api/proxy/reset", dependencies=guard)
    def proxy_reset():
        proxy_mgr.reset()
        return {"ok": True}

    @app.get("/api/proxy/pick", dependencies=guard)
    def proxy_pick():
        url = proxy_mgr.pick()
        return {"proxy": url or "direct", "info": proxy_mgr.context_info(url)}

    # ---------- Resin 粘性代理 ----------
    @app.get("/api/resin", dependencies=guard)
    def resin_info():
        from proxy import get_resin

        r = get_resin()
        return {
            "config": {
                "enabled": r.enabled,
                "url": r.url,
                "platform": r.platform,
                "identity_mode": r.identity_mode,
            },
            "snapshot": r.snapshot(),
        }

    @app.put("/api/resin", dependencies=guard)
    def resin_save(body: ConfigIn):
        """保存 Resin 配置（只接受 resin 段），立即生效（重置单例）。"""
        from proxy import reset_resin

        data = (body.data or {}).get("resin")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="data.resin 必须是对象")
        allowed = {k: data[k] for k in ("enabled", "url", "platform", "identity_mode") if k in data}
        if "url" in allowed:
            allowed["url"] = str(allowed["url"]).strip().rstrip("/")
        cfg.update({"resin": allowed}, save=True)
        reset_resin()
        from proxy import get_resin

        r = get_resin()
        log.info("resin_save", f"Resin 配置已保存 enabled={r.enabled}")
        return {"ok": True, "snapshot": r.snapshot()}

    @app.post("/api/resin/test", dependencies=guard)
    def resin_test():
        """连通性 + 粘性测试：临时 Account 连续两次查出口 IP。"""
        from proxy import get_resin, reset_resin

        reset_resin()  # 用最新配置
        r = get_resin()
        return r.test_connection()

    # ---------- 日志 / 配置 ----------
    @app.get("/api/logs", dependencies=guard)
    def logs(after: int = 0, limit: int = Query(200, le=2000), level: Optional[str] = None):
        items = get_buffer(after_seq=after, limit=limit, level=level)
        return {"cursor": items[-1]["seq"] if items else after, "items": items}

    @app.get("/api/events", dependencies=guard)
    def events(task_id: Optional[int] = None, limit: int = Query(200, le=2000)):
        return {"items": db.list_events(task_id=task_id, limit=limit)}

    @app.get("/api/config", dependencies=guard)
    def get_config():
        data: Dict[str, Any] = cfg.as_dict()
        if "api" in data:
            data["api"] = {**data["api"], "token": "***"}
        return data

    @app.put("/api/config", dependencies=guard)
    def update_config(body: ConfigIn):
        """网页改配置：深合并保存为合法 YAML。token 不可经此接口修改。"""
        data = body.data or {}
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="data 必须是对象")
        if "resin" in data and isinstance(data["resin"], dict) and data["resin"].get("url"):
            # 去掉尾部斜杠，保持与 Resin 解析约定一致
            data["resin"]["url"] = str(data["resin"]["url"]).strip().rstrip("/")
        if isinstance(data.get("api"), dict):
            token_in = data["api"].pop("token", None)
            if token_in not in (None, "", "***"):
                raise HTTPException(status_code=400, detail="api.token 请直接编辑 data/api_token 文件修改")
            if not data["api"]:
                data.pop("api")
        path = cfg.update(data, save=True)
        # 让进程内单例感知变更（代理池/执行子进程下次启动时读取新文件）
        log.info("config_update", f"配置已更新并保存: {path}")
        out: Dict[str, Any] = cfg.as_dict()
        if "api" in out:
            out["api"] = {**out["api"], "token": "***"}
        return {"ok": True, "path": str(path), "config": out}

    @app.get("/api/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    app.state.token = token
    app.state.cfg = cfg
    app.state.task_manager = tm
    return app


def run_server(cfg=None) -> None:
    """阻塞式启动面板服务。"""
    import uvicorn

    cfg = cfg or load_config()
    app = create_app(cfg)
    host = str(cfg.get("api.host", "127.0.0.1"))
    port = int(cfg.get("api.port", 8000))
    if bool(cfg.get("api.auth_enabled", True)):
        print(f"[API] Token: {app.state.token}")
        print(f"[API] Token 文件: {cfg.resolve('data/api_token')}")
    else:
        print("[API] 警告：认证已关闭，同机任意进程均可调用本接口")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[API] 警告：监听地址为 {host}，该接口可被局域网访问，请确保已开启认证")
    print(f"[API] 面板地址: http://{host}:{port}")
    # 关闭访问日志：每请求写盘在 Windows 上增加明显延迟
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


app = None  # 供 `uvicorn api.server:app` 使用时延迟创建


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
