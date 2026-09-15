"""OutlookAutomation 命令行入口。

用法：
    python main.py serve                    启动本地管理面板（默认 127.0.0.1:8000）
    python main.py import                   从 accounts.txt 导入账号
    python main.py run --limit 10           批量执行登录任务（跑完即退出）
    python main.py run --account a@b.com    只跑单个账号
    python main.py stats                    查看统计
    python main.py accounts --status NEW    列出账号
    python main.py tasks --limit 20         列出最近任务
    python main.py export                   导出结果 CSV
    python main.py clean --profiles         清理临时浏览器环境
    python main.py doctor                   环境自检
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from account import describe, get_account_manager
from browser import get_profile_manager
from config import load_config
from database import get_db
from logger import get_logger, setup_from_config


def _bootstrap():
    cfg = load_config()
    cfg.ensure_dirs()
    setup_from_config(cfg)
    log = get_logger(name="main", flow="MAIN")
    db = get_db(cfg.path_of("database.path", "data/app.db"))
    return cfg, log, db


# ---------- 子命令 ----------
def cmd_serve(args) -> int:
    cfg, log, _db = _bootstrap()
    from api import run_server

    if args.port:
        cfg.set("api.port", args.port)
    if args.host:
        cfg.set("api.host", args.host)
    if args.no_auth:
        cfg.set("api.auth_enabled", False)
        log.warn("serve", "已通过 --no-auth 关闭接口认证，同机任意进程可下发任务")
    if getattr(args, "open", False):
        import threading
        import webbrowser

        def _open_later():
            port = int(cfg.get("api.port", 8000))
            webbrowser.open(f"http://127.0.0.1:{port}")

        threading.Timer(1.2, _open_later).start()
    run_server(cfg)
    return 0


def cmd_import(args) -> int:
    cfg, log, db = _bootstrap()
    am = get_account_manager(cfg, db, logger=log)
    result = am.import_file(args.file)
    if not result.get("exists"):
        print(f"账号文件不存在: {result['file']}")
        print(f"请创建该文件，每行一条：账号{cfg.get('system.account_separator', '----')}密码")
        return 1
    print(f"导入完成：{result['imported']} 条，跳过 {result['skipped']} 行")
    print(f"当前账号统计：{am.stats()['by_status']}")
    return 0


def cmd_run(args) -> int:
    cfg, log, db = _bootstrap()
    from task import get_task_manager

    tm = get_task_manager(cfg, logger=log)
    am = tm.am

    if am.stats()["total"] == 0:
        result = am.import_file()
        if not result.get("imported"):
            print("没有可执行的账号。先执行 python main.py import")
            return 1

    workers = args.workers or int(cfg.get("system.max_workers", 3))
    tm.start(workers=workers, restore=not args.no_restore)

    if args.account:
        tasks = [tm.submit(args.account, task_type=args.type)]
    else:
        tasks = tm.submit_batch(task_type=args.type, limit=args.limit)

    if not tasks:
        print("没有待处理账号（可用 python main.py accounts --status OK 查看已完成）")
        tm.stop()
        return 0

    print(f"已派发 {len(tasks)} 个任务，Worker={workers}。Ctrl+C 可中断（任务会保留断点）")
    try:
        while True:
            idle = tm.wait_idle(timeout=5)
            snap = tm.stats()
            print(
                f"\r队列 {snap['queue']['size']} | 处理 {snap['processed']} | "
                f"成功 {snap['succeeded']} | 失败 {snap['failed']} | "
                f"浏览器 {snap['browsers']}",
                end="",
                flush=True,
            )
            if idle:
                break
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止...")
    finally:
        print()
        tm.stop()

    snap = tm.stats()
    print(f"完成：成功 {snap['succeeded']}，失败 {snap['failed']}")
    print(f"验证码统计：{snap['captcha']}")
    return 0


def cmd_work(args) -> int:
    """独立执行进程：由面板以子进程方式拉起（OutlookRegister 模式）。

    面板只负责下单与展示；本进程持有浏览器与 Worker 线程，
    面板关闭/卡顿不影响执行。停止方式：CTRL_BREAK（优雅）或 terminate（强杀）。
    """
    cfg, log, db = _bootstrap()
    from task import get_task_manager

    tm = get_task_manager(cfg, logger=log)
    am = tm.am
    if am.stats()["total"] == 0:
        result = am.import_file()
        if not result.get("imported"):
            print("没有可执行的账号，先导入账号再启动")
            return 1

    workers = args.workers or int(cfg.get("system.max_workers", 3))
    tm.start(workers=workers, restore=not args.no_restore)

    pid_file = cfg.resolve("data/worker.pid")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    print(f"[WORKER] 已启动 {workers} 个 Worker，PID={os.getpid()}，等待任务...")

    try:
        # 常驻：周期性从数据库补拉新任务（面板随时下单都能被接住）+ 统计落盘
        tick = 0
        stats_file = cfg.resolve("data/worker_stats.json")
        while True:
            time.sleep(2)
            tick += 1
            try:
                tm.queue.restore()
            except Exception:
                pass
            if tick % 3 == 0:
                try:
                    snap = tm.stats()
                    stats_file.write_text(json.dumps(snap), encoding="utf-8")
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[WORKER] 收到停止信号，正在收尾...")
    finally:
        tm.stop()
        try:
            pid_file.unlink()
        except OSError:
            pass
        snap = tm.stats()
        print(f"[WORKER] 已退出。累计成功 {snap['succeeded']}，失败 {snap['failed']}")
    return 0


def cmd_stats(args) -> int:
    cfg, log, db = _bootstrap()
    from flow import stats_snapshot
    from proxy import get_proxy_manager

    am = get_account_manager(cfg, db, logger=log)
    stats = am.stats()
    print("=== 账号 ===")
    print(f"总数: {stats['total']}")
    for status, count in sorted(stats["by_status"].items()):
        print(f"  {describe(status):<8} {count}")
    print("\n=== 任务 ===")
    for status, count in sorted(db.count_tasks().items()):
        print(f"  {status:<12} {count}")
    print("\n=== 验证码 ===")
    print(f"  {stats_snapshot()}")
    print("\n=== 代理 ===")
    snap = get_proxy_manager(cfg.section("proxy")).snapshot()
    print(f"  模式: {'直连' if snap['direct'] else snap['type'] + '://' + snap['host']}")
    print(f"  端口数: {snap['ports']}")
    print("\n=== 环境 ===")
    pm = get_profile_manager(cfg, db)
    ps = pm.snapshot()
    print(f"  Profile 数: {ps['count']}  占用 {ps['total_mb']} MB  目录 {ps['root']}")
    return 0


def cmd_accounts(args) -> int:
    cfg, log, db = _bootstrap()
    am = get_account_manager(cfg, db, logger=log)
    items = am.list(status=args.status, limit=args.limit)
    if not items:
        print("无匹配账号")
        return 0
    print(f"{'ID':<5} {'账号':<34} {'状态':<14} 执行/失败  备注")
    for a in items:
        print(
            f"{a.id:<5} {a.account:<34} {describe(a.status):<14} "
            f"{a.run_count}/{a.fail_count}      {a.note[:40]}"
        )
    return 0


def cmd_tasks(args) -> int:
    _cfg, _log, db = _bootstrap()
    items = db.list_tasks(status=args.status, limit=args.limit)
    if not items:
        print("无任务记录")
        return 0
    print(f"{'ID':<5} {'类型':<8} {'账号':<30} {'状态':<11} {'阶段':<16} 耗时")
    for t in items:
        dur = f"{t.duration():.1f}s" if t.duration() else "-"
        print(f"{t.id:<5} {t.type:<8} {t.account:<30} {t.status:<11} {t.stage:<16} {dur}")
        if t.error:
            print(f"      └─ {t.error[:100]}")
    return 0


def cmd_export(args) -> int:
    cfg, log, db = _bootstrap()
    am = get_account_manager(cfg, db, logger=log)
    target = args.output or cfg.resolve("data/accounts_export.csv")
    path = am.export_csv(target)
    print(f"已导出: {path}")
    return 0


def cmd_clean(args) -> int:
    cfg, log, db = _bootstrap()
    pm = get_profile_manager(cfg, db, logger=log)
    if args.all_profiles:
        print("警告：这会删除全部浏览器环境（含所有登录态 Cookie），不可恢复。")
        if input("确认清空？输入 yes 继续: ").strip().lower() != "yes":
            print("已取消")
            return 1
        print(f"已清空 {pm.clear_all()} 个环境")
        return 0
    if args.profiles:
        print(f"已清理临时环境 {pm.clear_temporary()} 个")
    if args.tasks:
        print(f"已清理任务记录 {db.clear_tasks()} 条")
    if not (args.profiles or args.tasks):
        print("请指定 --profiles / --tasks / --all-profiles")
    return 0


def cmd_doctor(args) -> int:
    cfg, _log, db = _bootstrap()
    ok = True
    print(f"Python: {sys.version.split()[0]}")
    print(f"项目根目录: {cfg.root}")
    print(f"配置文件: {cfg.source_path or '(使用内置默认值)'}")

    for module in ("patchright", "yaml", "fastapi", "uvicorn"):
        try:
            __import__(module)
            print(f"[OK]   依赖 {module}")
        except ImportError:
            print(f"[FAIL] 依赖 {module} 未安装")
            ok = False
    try:
        import apscheduler  # noqa: F401

        print("[OK]   依赖 apscheduler")
    except ImportError:
        print("[WARN] apscheduler 未安装（定时任务不可用）")

    try:
        from patchright.sync_api import sync_playwright

        with sync_playwright() as p:
            print(f"[OK]   Chromium: {p.chromium.executable_path}")
    except Exception as exc:
        print(f"[FAIL] Chromium 不可用: {exc}")
        print("       执行: patchright install chromium")
        ok = False

    print(f"[OK]   数据库: {db.path}")
    accounts_file = cfg.path_of("system.accounts_file", "accounts.txt")
    print(f"{'[OK]  ' if accounts_file.is_file() else '[WARN]'} 账号文件: {accounts_file}")
    print(f"[OK]   日志目录: {cfg.path_of('logger.dir')}")
    print(f"[OK]   环境目录: {cfg.path_of('profile.root')}")
    if bool(cfg.get("api.auth_enabled", True)):
        print("[OK]   API Token 认证已开启")
    else:
        print("[WARN] API Token 认证已关闭，同机任意进程可下发任务")
    return 0 if ok else 1


# ---------- 参数 ----------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="OutlookAutomation 本地自动化框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="启动本地管理面板")
    p.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，不建议修改）")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-auth", action="store_true", help="关闭 Token 认证（不推荐）")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器（打包版双击默认开启）")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("import", help="导入账号文件")
    p.add_argument("--file", default=None, help="账号文件路径，默认 config.system.accounts_file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("run", help="执行登录任务")
    p.add_argument("--limit", type=int, default=10, help="批量任务数量")
    p.add_argument("--account", default=None, help="只跑指定账号")
    p.add_argument("--type", default="login", help="流程类型")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--no-restore", action="store_true", help="不恢复上次未完成任务")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("work", help="独立执行进程（供面板子进程调用，也可手动运行）")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--no-restore", action="store_true", help="不恢复未完成任务")
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("stats", help="查看统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("accounts", help="列出账号")
    p.add_argument("--status", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("tasks", help="列出任务")
    p.add_argument("--status", default=None)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("export", help="导出账号结果 CSV")
    p.add_argument("--output", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("clean", help="清理数据")
    p.add_argument("--profiles", action="store_true", help="清理临时浏览器环境")
    p.add_argument("--all-profiles", action="store_true", help="清空全部环境（高危）")
    p.add_argument("--tasks", action="store_true", help="清空任务记录")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[list] = None) -> int:
    # 打包版双击 exe（无任何命令行参数）→ 直接启动管理面板并自动打开浏览器，避免“闪退”
    no_args = (len(sys.argv) <= 1) if argv is None else (len(argv) == 0)
    if no_args and getattr(sys, "frozen", False):
        parser = build_parser()
        args = parser.parse_args(["serve", "--open"])
        try:
            return args.func(args)
        except KeyboardInterrupt:
            print("\n已中断")
            return 130

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
