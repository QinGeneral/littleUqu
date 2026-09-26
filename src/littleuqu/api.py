from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import UquError, ca_bundle, config_dir, read_json, write_json


class AuthError(UquError):
    """登录态已过期或被服务端拒绝，批量任务应据此中止而非逐个失败。"""


BASE = "https://fastapi.ukids.cn"
DEFAULT_HEADERS = {
    "format": "JSON",
    "channel": "anp73",
    "ver": "5.0.9",
    "verCode": "509",
    "xfrom": "1",
    "sstp": "nrm",
    "mode": "parents",
    "dtp": "phone",
    "hos": "Android11",
    "User-Agent": "okhttp/3.12.8",
    "chdId": "0",
    "chdAgeDays": "-1",
}
# 只导入 API 需要的设备上下文，不复制 Host、Content-Length 或网络地址。
HEADER_KEYS = set(DEFAULT_HEADERS) | {
    "token",
    "udid",
    "deviceId",
    "imei",
    "udName",
    "udNameShow",
    "ssid",
}


def capture_headers(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    command = text.split("\n\n", 1)[0].replace("\\\n", " ")
    tokens = shlex.split(command)
    headers = {}
    for i, token in enumerate(tokens[:-1]):
        if token in ("-H", "--header"):
            name, sep, value = tokens[i + 1].partition(":")
            if sep and name in HEADER_KEYS and value.strip():
                headers[name] = value.strip()
    return headers


class API:
    def __init__(self):
        self.path = config_dir() / "session.json"
        self.state = read_json(self.path)
        self.session = requests.Session()
        self.session.verify = ca_bundle()
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        device = self.state.setdefault("device_id", uuid.uuid4().hex.upper())
        self.headers = {
            **DEFAULT_HEADERS,
            "deviceId": device,
            "udid": device,
            "imei": device,
            **self.state.get("headers", {}),
        }
        if self.state.get("token"):
            self.headers["token"] = self.state["token"]

    def save(self):
        self.state["headers"] = {k: v for k, v in self.headers.items() if k != "token"}
        write_json(self.path, self.state, private=True)

    def expired(self, skew=300):
        """本地会话是否已超过 token 有效期（含安全余量），无有效期记录时返回 False。"""
        expires, login_at = self.state.get("expires"), self.state.get("login_at")
        if not isinstance(expires, (int, float)) or not isinstance(login_at, (int, float)):
            return False
        return time.time() >= login_at + expires - skew

    def request(self, path: str, params=None, body=None, require_auth=True):
        if not path.startswith("/") or path.startswith("//"):
            raise UquError("API 路径必须是本站相对路径")
        if require_auth and not self.headers.get("token"):
            raise UquError("尚未登录，请运行 littleuqu login 或 auth import-capture")
        if require_auth and self.expired():
            hours = self.state.get("expires", 0) / 3600
            raise AuthError(f"登录已超过 token 有效期（约 {hours:.0f} 小时），请重新运行 littleuqu login")
        headers = {**self.headers, "req-id": uuid.uuid4().hex.upper()}
        if not require_auth:
            headers.pop("token", None)
        try:
            response = self.session.request(
                "POST" if body is not None else "GET",
                BASE + path,
                params=params,
                json=body,
                headers=headers,
                timeout=(15, 60),
            )
            if response.status_code == 401:
                raise AuthError("登录已失效（HTTP 401），请重新运行 littleuqu login 登录")
            if response.status_code == 403:
                raise UquError(
                    "当前账号无权访问该资源（HTTP 403），请检查 VIP 状态；"
                    "若该账号应有权限，可能是登录失效，请重新登录"
                )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise UquError(f"接口请求失败：{path}（{type(exc).__name__}）") from exc
        if not isinstance(data, dict) or data.get("success") is not True:
            # 不原样输出服务端响应，以免包含验证码或 token。
            raise UquError(f"接口返回业务错误：{path}，请检查登录、参数和账户权限")
        return data

    def login(self, mobile: str, code: str):
        data = self.request(
            "/ucapp/mobileLogin", body={"mobile": mobile, "verifyCode": code}, require_auth=False
        )["data"]
        token = data.get("token", {})
        if not token.get("token"):
            raise UquError("登录响应缺少 token")
        self.state.update(
            token=token["token"],
            refresh_token=token.get("refreshToken"),
            expires=token.get("expires"),
            login_at=time.time(),
        )
        self.headers["token"] = token["token"]
        self.save()

    def import_capture(self, path: Path):
        headers = capture_headers(path)
        token_info = {}
        if not headers.get("token"):
            # 支持导入登录响应；json 从 response 标记之后读取。
            text = path.read_text(encoding="utf-8")
            match = re.search(r'\{\s*"success"', text)
            if match:
                data = json.JSONDecoder().raw_decode(text[match.start() :])[0]
                obj = data.get("data", {}).get("token", {})
                if isinstance(obj, dict) and obj.get("token"):
                    headers["token"] = obj["token"]
                    token_info = {
                        "refresh_token": obj.get("refreshToken"),
                        "expires": obj.get("expires"),
                        "login_at": time.time(),
                    }
            if not headers.get("token"):
                raise UquError("抓包中未找到 token，请选择已登录请求或登录响应")
        self.headers.update(headers)
        self.state["token"] = headers["token"]
        # 旧会话的有效期信息不适用于新 token：缺失时宁可不预检，也不能误判过期。
        for key in ("refresh_token", "expires", "login_at"):
            self.state.pop(key, None)
        self.state.update({k: v for k, v in token_info.items() if v is not None})
        self.save()
