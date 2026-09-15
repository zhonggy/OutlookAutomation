"""Patchright 浏览器封装。

BrowserSession 负责单个浏览器实例的完整生命周期：
    启动 Playwright → 启动 Chromium → 创建 Context → 打开 Page
    → 交给流程使用 → 关闭并归还 Profile

两种启动模式：
- persistent=True  → launch_persistent_context（profile 目录持久化，登录态可复用）
- persistent=False → launch + new_context（每次干净环境）
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from .context import (
    build_args,
    build_context_options,
    build_proxy_option,
    random_viewport,
)
from .profile import ProfileManager, make_fingerprint_seed


class BrowserLaunchError(RuntimeError):
    """浏览器启动失败。"""


class BrowserSession:
    """一次浏览器会话。建议用 with 语句使用，确保资源释放。"""

    def __init__(
        self,
        cfg,
        profile_manager: Optional[ProfileManager] = None,
        proxy_manager=None,
        logger=None,
        account: str = "",
    ):
        self.cfg = cfg
        self.pm = profile_manager
        self.proxy_manager = proxy_manager
        self.log = logger
        self.account = account

        self.playwright = None
        self.browser = None          # Browser 或 BrowserContext(persistent)
        self.context = None          # BrowserContext
        self.page = None
        self.profile = None
        self.proxy_url = ""
        self.timezone = ""
        self.persistent = bool(cfg.get("profile.persistent", True))
        self._closed = False
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------
    def start(self):
        """启动浏览器并返回 Page。"""
        with self._lock:
            if self.page is not None:
                return self.page
            try:
                self._launch()
            except BrowserLaunchError:
                self.close(broken=True)
                raise
            except Exception as exc:
                self.close(broken=True)
                raise BrowserLaunchError(f"启动浏览器失败: {exc}") from exc
            return self.page

    def _launch(self) -> None:
        from patchright.sync_api import sync_playwright

        browser_cfg = self.cfg.section("browser")
        locale = str(browser_cfg.get("locale") or "zh-CN")

        # 1) 代理与地区信息
        #    优先级：Resin 粘性代理（按账号身份） > 普通代理池 > 直连（无伪装）
        info: Dict[str, Any] = {}
        using_proxy = False
        resin_proxy_opt: Optional[Dict[str, str]] = None
        from proxy.resin import get_resin

        resin = get_resin()
        if resin.usable and self.account:
            # Resin 正向代理：认证用户名 Platform.Account，粘性 IP 绑定账号身份
            resin_proxy_opt = resin.forward_proxy_option(self.account)
            if resin_proxy_opt is None:
                raise BrowserLaunchError(
                    f"Resin 已启用但无法为账号 {self.account} 构造代理身份"
                )
            self.proxy_url = f"resin://{resin.platform}.{resin.account_identity(self.account)}"
            self._resin_account_identity = resin.account_identity(self.account)
            using_proxy = True
            # 出口信息走 Resin 反向代理查询（带 X-Resin-Account 头）
            info = resin.lookup_exit_info(self.account)
        elif self.proxy_manager is not None:
            self.proxy_url = self.proxy_manager.pick()
            using_proxy = bool(self.proxy_url)
            if using_proxy:
                info = self.proxy_manager.context_info(self.proxy_url)
        self.timezone = info.get("timezone") or ""
        geolocation = info.get("geolocation")

        # 2) Profile
        fp_enabled = bool(browser_cfg.get("fingerprint_enabled", False))
        seed = make_fingerprint_seed(self.account or self.proxy_url) if fp_enabled else None
        if self.pm is not None:
            self.profile = self.pm.acquire(
                account=self.account, proxy=self.proxy_url, seed=seed
            )
            if fp_enabled and self.profile.fingerprint_seed:
                seed = self.profile.fingerprint_seed

        # 3) 启动参数
        args = build_args(
            locale=locale,
            timezone=self.timezone,
            fingerprint_seed=seed,
            fingerprint_platform=str(browser_cfg.get("fingerprint_platform") or "windows"),
            fingerprint_brand=str(browser_cfg.get("fingerprint_brand") or "Chrome"),
        )
        viewport = random_viewport(
            browser_cfg.get("viewport_widths"), browser_cfg.get("viewport_heights")
        )
        common: Dict[str, Any] = {
            "headless": bool(browser_cfg.get("headless", False)),
            "args": args,
        }
        if resin_proxy_opt is not None:
            common["proxy"] = resin_proxy_opt
        elif not using_proxy:
            proxy_opt = build_proxy_option(self.proxy_url)
            if proxy_opt:
                common["proxy"] = proxy_opt

        executable_path = str(browser_cfg.get("executable_path") or "").strip()
        if executable_path:
            import os
            import sys
            from pathlib import Path as _Path

            # 相对路径统一相对项目根（exe 所在目录）解析，避免受工作目录影响
            cand = _Path(executable_path)
            if not cand.is_absolute():
                from config import PROJECT_ROOT

                cand = PROJECT_ROOT / executable_path
            # 打包版（PyInstaller onedir）：资源在 _internal/ 下，再探测一次
            if not cand.is_file() and getattr(sys, "frozen", False):
                bundled = _Path(__file__).resolve().parent.parent / executable_path
                if bundled.is_file():
                    cand = bundled
            if not cand.is_file():
                raise BrowserLaunchError(
                    f"浏览器可执行文件不存在: {cand}\n"
                    f"请先执行: patchright install chromium，或修改 config.yaml 的 browser.executable_path"
                )
            common["executable_path"] = str(cand)

        ctx_opts = build_context_options(
            locale=locale,
            timezone=self.timezone,
            viewport=viewport,
            geolocation=geolocation,
        )

        self._log_launch(executable_path, seed, fp_enabled)

        # 4) 启动
        self.playwright = sync_playwright().start()
        chromium = self.playwright.chromium

        if self.persistent and self.profile is not None:
            self.browser = chromium.launch_persistent_context(
                self.profile.path, **common, **ctx_opts
            )
            self.context = self.browser
            pages = list(self.context.pages)
            if pages:
                self.page = pages[0]
                for extra in pages[1:]:
                    try:
                        extra.close()
                    except Exception:
                        pass
            else:
                self.page = self.context.new_page()
        else:
            self.browser = chromium.launch(**common)
            self.context = self.browser.new_context(**ctx_opts)
            self.page = self.context.new_page()

        # 5) 超时默认值
        timeout = int(browser_cfg.get("timeout", 60000) or 60000)
        nav_timeout = int(browser_cfg.get("nav_timeout", 45000) or 45000)
        try:
            self.context.set_default_timeout(timeout)
            self.context.set_default_navigation_timeout(nav_timeout)
        except Exception:
            pass

    def _log_launch(self, executable_path: str, seed: Optional[int], fp_enabled: bool) -> None:
        if not self.log:
            return
        kernel = "custom-chromium" if executable_path else "patchright-chromium"
        proxy_desc = self.proxy_url.split("//")[-1] if self.proxy_url else "direct"
        profile_desc = self.profile.profile_id if self.profile else "-"
        self.log.info(
            "browser_launch",
            f"kernel={kernel} fp={fp_enabled} seed={seed} tz={self.timezone} "
            f"proxy={proxy_desc} profile={profile_desc} persistent={self.persistent}",
        )

    def close(self, broken: bool = False) -> None:
        """关闭页面/上下文/浏览器/Playwright，并归还 Profile。异常全部吞掉。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True

            for closer in (
                lambda: self.page.close() if self.page else None,
                lambda: self.context.close()
                if self.context is not None and self.context is not self.browser
                else None,
                lambda: self.browser.close() if self.browser else None,
                lambda: self.playwright.stop() if self.playwright else None,
            ):
                try:
                    closer()
                except Exception:
                    pass

            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None

            if self.pm is not None and self.profile is not None:
                try:
                    self.pm.release(self.profile, broken=broken)
                except Exception:
                    pass
            if self.log:
                self.log.info("browser_close", f"broken={broken}")

    # ---------- 便捷方法 ----------
    def goto(self, url: str, wait_until: str = "domcontentloaded") -> bool:
        if self.page is None:
            self.start()
        try:
            self.page.goto(url, wait_until=wait_until)
            return True
        except Exception as exc:
            if self.log:
                self.log.warn("browser_goto", f"打开 {url} 失败: {exc}")
            return False

    def new_page(self):
        if self.context is None:
            self.start()
        return self.context.new_page()

    def screenshot(self, path: str) -> bool:
        if self.page is None:
            return False
        try:
            self.page.screenshot(path=path)
            return True
        except Exception:
            return False

    def info(self) -> Dict[str, Any]:
        url = ""
        try:
            url = self.page.url if self.page else ""
        except Exception:
            pass
        return {
            "account": self.account,
            "proxy": self.proxy_url,
            "timezone": self.timezone,
            "profile": self.profile.profile_id if self.profile else "",
            "persistent": self.persistent,
            "alive": self.page is not None and not self._closed,
            "url": url,
        }

    # ---------- 上下文管理 ----------
    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(broken=exc_type is not None)
