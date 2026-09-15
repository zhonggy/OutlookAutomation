"""YAML 配置加载器。

约定：
- 配置文件默认 config/config.yaml
- 所有相对路径统一相对项目根目录解析（PROJECT_ROOT）
- 支持点号路径访问：cfg.get("browser.headless", False)
- 支持环境变量覆盖：OA_BROWSER__HEADLESS=true
"""

from __future__ import annotations

import copy
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：以 exe 所在目录为项目根（data/logs/profiles 都写在 exe 旁边）
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 打包后 config/config.yaml 在 exe 旁边（spec 里 datas 输出到 exe 同级的 config/）
# 若不存在（比如用户删了配置），回退到内置默认值（_internal/config/config.yaml）
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
_BUNDLED_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
if not DEFAULT_CONFIG_PATH.is_file() and _BUNDLED_CONFIG_PATH.is_file():
    DEFAULT_CONFIG_PATH = _BUNDLED_CONFIG_PATH

ENV_PREFIX = "OA_"

_DEFAULTS: Dict[str, Any] = {
    "browser": {
        "executable_path": "",
        "headless": False,
        "timeout": 60000,
        "nav_timeout": 45000,
        "locale": "zh-CN",
        "fingerprint_enabled": False,
        "fingerprint_platform": "windows",
        "fingerprint_brand": "Chrome",
        "viewport_widths": [1366, 1440, 1536, 1680, 1920],
        "viewport_heights": [768, 864, 900, 1050, 1080],
    },
    "profile": {
        "root": "profiles",
        "persistent": True,
        "reuse": True,
        "cleanup_on_exit": False,
    },
    "proxy": {
        "enabled": False,
        "mode": "single",
        "type": "http",
        "host": "127.0.0.1",
        "single_port": 7890,
        "port_start": 24000,
        "port_end": 24064,
        "max_per_proxy": 20,
        "ip_info_lookup": True,
        "ip_info_timeout": 8,
    },
    "resin": {
        "enabled": False,
        "url": "",
        "platform": "Default",
        "identity_mode": "email_prefix",
    },
    "flow": {
        "login_url": "https://login.live.com/",
        "max_captcha_retries": 3,
        "captcha_strategy": 0,
        "captcha_screenshot": True,
        "wait_verify_timeout": 300,
        "checkpoint_enabled": True,
    },
    "system": {
        "max_workers": 3,
        "task_retry": 1,
        "accounts_file": "accounts.txt",
        "account_separator": "----",
    },
    "database": {"path": "data/app.db"},
    "logger": {
        "dir": "logs",
        "level": "INFO",
        "console": True,
        "file": True,
        "max_bytes": 10485760,
        "backup_count": 5,
    },
    "api": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8000,
        "auth_enabled": True,
        "token": "",
    },
    "scheduler": {"enabled": False, "jobs": []},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：override 的同名 key 覆盖 base，dict 递归，其他类型直接替换。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(text: str) -> Any:
    """把环境变量字符串转成 bool / int / float / str。"""
    low = text.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """OA_BROWSER__HEADLESS=true → data["browser"]["headless"] = True"""
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = env_key[len(ENV_PREFIX):].lower().split("__")
        if not path or not path[0]:
            continue
        cursor = data
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = _coerce(env_val)
    return data


class Config:
    """配置对象。点号访问 + 路径解析。"""

    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None):
        self._data = data
        self._path = path

    # ---------- 读取 ----------
    def get(self, dotted: str, default: Any = None) -> Any:
        cursor: Any = self._data
        for part in dotted.split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                return default
        return cursor

    def section(self, name: str) -> Dict[str, Any]:
        value = self._data.get(name, {})
        return value if isinstance(value, dict) else {}

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cursor = self._data
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def update(self, partial: Dict[str, Any], save: bool = True) -> Path:
        """深合并更新配置（partial 只需包含要改的键），默认持久化到源文件。

        程序生成 YAML，避免手改语法错误。返回写入路径。
        """
        self._data = _deep_merge(self._data, partial)
        if save:
            return self.save()
        return self._path or DEFAULT_CONFIG_PATH

    # ---------- 路径 ----------
    @property
    def root(self) -> Path:
        return PROJECT_ROOT

    @property
    def source_path(self) -> Optional[Path]:
        return self._path

    def path_of(self, dotted: str, default: str = "") -> Path:
        """读取配置中的路径值并解析为绝对路径。"""
        raw = str(self.get(dotted, default) or default)
        return self.resolve(raw)

    def resolve(self, raw: str) -> Path:
        """相对路径基于项目根目录展开；绝对路径原样返回。"""
        p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def ensure_dirs(self) -> None:
        """创建运行所需目录。"""
        for dotted in ("logger.dir", "profile.root"):
            self.path_of(dotted).mkdir(parents=True, exist_ok=True)
        self.path_of("database.path").parent.mkdir(parents=True, exist_ok=True)

    # ---------- 持久化 ----------
    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else (self._path or DEFAULT_CONFIG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self._data, fh, allow_unicode=True, sort_keys=False)
        return target

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Config path={self._path} sections={list(self._data)}>"


_lock = threading.Lock()
_cached: Optional[Config] = None


def load_config(path: Optional[str | Path] = None, use_cache: bool = True) -> Config:
    """加载配置。默认带进程级缓存，reload 时传 use_cache=False。"""
    global _cached
    if use_cache and path is None and _cached is not None:
        return _cached

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
                if isinstance(loaded, dict):
                    raw = loaded
        except yaml.YAMLError as exc:
            # 配置文件语法错误：回退默认值并大声告警，让面板至少能启动、
            # 用户可通过网页配置页重写合法配置
            print(f"=" * 60)
            print(f"[配置错误] {cfg_path} 解析失败，已回退内置默认配置！")
            print(f"  原因: {exc}")
            print(f"  可在网页面板「配置」页重新设置并保存，或执行:")
            print(f"  git checkout -- config/config.yaml")
            print(f"=" * 60)

    merged = _apply_env_overrides(_deep_merge(_DEFAULTS, raw))
    # 源文件不存在或解析失败（raw 为空）时置 None：表示当前生效的是默认配置
    cfg = Config(merged, cfg_path if raw else None)

    if use_cache and path is None:
        with _lock:
            _cached = cfg
    return cfg


def reload_config(path: Optional[str | Path] = None) -> Config:
    global _cached
    with _lock:
        _cached = None
    return load_config(path, use_cache=True)
