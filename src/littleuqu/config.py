from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
from pathlib import Path

from platformdirs import user_config_path


class UquError(Exception):
    """可以安全显示给用户的错误。"""


def ca_bundle() -> str | bool:
    """返回显式配置或 Python 运行时的系统 CA bundle，始终保持 TLS 校验。"""
    configured = (
        os.environ.get("LITTLEUQU_CA_BUNDLE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
    )
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise UquError(f"CA bundle 不存在：{path}")
        return str(path)
    system_cafile = ssl.get_default_verify_paths().cafile
    return system_cafile if system_cafile and Path(system_cafile).is_file() else True


def config_dir() -> Path:
    return Path(os.environ.get("LITTLEUQU_CONFIG_DIR", user_config_path("littleuqu")))


def read_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise UquError(f"无法读取 JSON：{path}") from exc


def write_json(path: Path, value, private: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    return value[:100] or "未命名"


def scrub(value):
    if isinstance(value, dict):
        return {
            k: (
                "<redacted>"
                if k.lower()
                in {
                    "token",
                    "refreshtoken",
                    "mobile",
                    "verifycode",
                    "playauth",
                    "playurl",
                    "sign",
                }
                else scrub(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and ("auth_key=" in value or "token=" in value.lower()):
        return value.split("?", 1)[0] + "?<redacted>"
    return value
