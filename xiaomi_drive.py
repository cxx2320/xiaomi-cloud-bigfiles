"""
小米云盘（i.mi.com）非官方接口客户端。

接口要点（参考社区逆向结果）：
  * 主机：https://i.mi.com
  * 鉴权：Cookie 里的 serviceToken + userId，两者缺一返回 401
  * POST：表单里还必须再带一个 serviceToken 字段
  * serviceToken 约 15 分钟过期，可用 passToken 静默换新
  * 列目录：GET /drive/v2/user/folders/children
  * 容量：  GET /status/alldetail

本模块只做「读」操作，不会修改云盘数据。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://i.mi.com"
ROOT_ID = "0"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class XiaomiAuthError(Exception):
    """未登录 / 令牌失效且无法自动续期。"""


class XiaomiAPIError(Exception):
    """接口返回了非 0 的业务错误码。"""

    def __init__(self, endpoint: str, code: Any, message: str = ""):
        self.endpoint = endpoint
        self.code = code
        self.message = message
        super().__init__(f"{endpoint} 返回错误码 {code}：{message}" if message else f"{endpoint} 返回错误码 {code}")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def strip_start(text: str) -> str:
    """账号站返回的 JSON 带 ``&&&START&&&`` 前缀。"""
    t = (text or "").lstrip()
    if t.startswith("&&&START&&&"):
        return t[len("&&&START&&&"):]
    return t


def is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes")


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """
    解析用户从浏览器复制的 Cookie。

    支持三种常见形态：
      * ``serviceToken=xxx; userId=123; ...``（请求头形态）
      * 多行 ``name=value``
      * 直接粘贴含 ``document.cookie`` 的整段文本
    """
    if not raw:
        return {}
    raw = raw.replace("Cookie:", " ").replace("cookie:", " ")
    # 统一分隔符
    parts: List[str] = []
    for chunk in raw.replace("\n", ";").replace("\r", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)

    cookies: Dict[str, str] = {}
    for item in parts:
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"')
        if not name or not value:
            continue
        # 防止把 "Cookie: xxx" 之类的前缀当成键
        if " " in name:
            name = name.rsplit(" ", 1)[-1]
        cookies[name] = value
    return cookies


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
DATA_DIR = Path(__file__).resolve().parent / "data"
CRED_FILE = DATA_DIR / "credentials.json"


@dataclass
class Credentials:
    user_id: str
    service_token: str = ""
    pass_token: str = ""
    device_id: str = ""

    def ensure_device_id(self) -> None:
        if not self.device_id:
            self.device_id = "wb_" + uuid.uuid4().hex

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def save_credentials(creds: Credentials) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CRED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CRED_FILE)


def load_credentials() -> Optional[Credentials]:
    if not CRED_FILE.exists():
        return None
    try:
        data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("user_id"):
        return None
    return Credentials(
        user_id=str(data.get("user_id") or ""),
        service_token=str(data.get("service_token") or ""),
        pass_token=str(data.get("pass_token") or ""),
        device_id=str(data.get("device_id") or ""),
    )


def delete_credentials() -> None:
    try:
        CRED_FILE.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class XiaomiDrive:
    def __init__(self, creds: Credentials, timeout: float = 40.0):
        self.creds = creds
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA, "Referer": "https://i.mi.com/"})
        self._refresh_lock = threading.Lock()
        self._ka_stop = threading.Event()
        self._ka_thread: Optional[threading.Thread] = None

    # ---------------- 保活 ---------------- #
    def start_keepalive(self, interval: float = 240.0) -> None:
        """serviceToken 约 15 分钟过期；每 4 分钟续一次，保持登录态。"""
        if self._ka_thread and self._ka_thread.is_alive():
            return

        def loop() -> None:
            while not self._ka_stop.wait(interval):
                if not self.renew():
                    self.refresh()

        self._ka_stop.clear()
        self._ka_thread = threading.Thread(target=loop, daemon=True, name="xiaomi-keepalive")
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        self._ka_stop.set()

    # ---------------- 基础请求 ---------------- #
    def _cookie_header(self) -> str:
        parts = [f"serviceToken={self.creds.service_token}", f"userId={self.creds.user_id}"]
        if self.creds.device_id:
            parts.append(f"deviceId={self.creds.device_id}")
        return "; ".join(parts)

    def _absorb_cookies(self, resp: requests.Response) -> None:
        """续期接口会通过 Set-Cookie 下发新的 serviceToken。"""
        changed = False
        for cookie in resp.cookies:
            if cookie.name == "serviceToken" and cookie.value:
                self.creds.service_token = cookie.value
                changed = True
            elif cookie.name == "userId" and cookie.value:
                self.creds.user_id = cookie.value
                changed = True
        if changed:
            try:
                save_credentials(self.creds)
            except OSError:
                pass

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        form: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        _retried: bool = False,
    ) -> Any:
        url = BASE + path
        req_timeout = timeout or self.timeout
        headers = {"Cookie": self._cookie_header(), "Accept": "application/json, text/plain, */*"}
        method = method.upper()

        if method == "POST":
            data = dict(form or {})
            data["serviceToken"] = self.creds.service_token
            resp = self.http.post(url, data=data, headers=headers, timeout=req_timeout)
        else:
            resp = self.http.get(url, params=params, headers=headers, timeout=req_timeout)

        if resp.status_code == 401:
            if not _retried and self.refresh():
                return self.request(method, path, params, form, timeout, _retried=True)
            raise XiaomiAuthError("serviceToken 已失效，且无法用 passToken 自动续期，请重新登录")

        self._absorb_cookies(resp)

        try:
            payload = json.loads(strip_start(resp.text))
        except ValueError:
            raise XiaomiAPIError(path, resp.status_code, f"返回非 JSON（HTTP {resp.status_code}）")

        if not isinstance(payload, dict):
            raise XiaomiAPIError(path, -1, "响应结构异常")

        code = payload.get("code")
        if code is None:
            code = payload.get("R", 0) or 0
        if code not in (0, "0"):
            desc = (
                payload.get("description")
                or payload.get("D")
                or payload.get("S")
                or payload.get("message")
                or ""
            )
            raise XiaomiAPIError(path, code, str(desc))

        return payload["data"] if "data" in payload else payload

    # ---------------- 鉴权 ---------------- #
    def refresh(self) -> bool:
        """用 passToken 换一个新的 serviceToken。"""
        if not (self.creds.pass_token and self.creds.user_id):
            return False
        with self._refresh_lock:
            try:
                r1 = self.http.get(
                    BASE + "/api/user/login",
                    params={"followUp": "https://i.mi.com/", "_locale": "zh_CN"},
                    headers={"User-Agent": UA},
                    timeout=self.timeout,
                )
                j1 = json.loads(strip_start(r1.text))
                login_url = ((j1.get("data") or {}).get("loginUrl") or "").strip()
                if not login_url:
                    return False

                cookies = {"passToken": self.creds.pass_token, "userId": self.creds.user_id}
                if self.creds.device_id:
                    cookies["deviceId"] = self.creds.device_id
                    cookies["pass_ua"] = "web"

                sep = "&" if "?" in login_url else "?"
                r2 = self.http.get(
                    f"{login_url}{sep}_json=true",
                    cookies=cookies,
                    headers={"User-Agent": UA},
                    timeout=self.timeout,
                )
                j2 = json.loads(strip_start(r2.text))
                if j2.get("code") != 0 or j2.get("securityStatus") != 0:
                    return False

                location = (j2.get("location") or "").strip()
                if not location:
                    return False

                r3 = self.http.get(
                    location,
                    allow_redirects=False,
                    headers={"User-Agent": UA},
                    timeout=self.timeout,
                )
                token = r3.cookies.get("serviceToken")
                if not token:
                    return False
                self.creds.service_token = token
                uid = r3.cookies.get("userId")
                if uid:
                    self.creds.user_id = uid
                save_credentials(self.creds)
                return True
            except (requests.RequestException, ValueError):
                return False

    def renew(self) -> bool:
        """保活：约 15 分钟有效期内续一次，新的 serviceToken 通过 Set-Cookie 下发。"""
        try:
            self.request(
                "GET", "/status/lite/setting", params={"type": "AutoRenewal", "inactiveTime": "10"}
            )
            return True
        except (XiaomiAuthError, XiaomiAPIError, requests.RequestException):
            return False

    # ---------------- 业务接口 ---------------- #
    def quota(self) -> Dict[str, Any]:
        """容量总览。detail 为按服务拆分的占用（相册、备份、录音等）。"""
        data = self.request("GET", "/status/alldetail") or {}
        detail: List[Dict[str, Any]] = []
        for key, val in (data.get("usedDetail") or {}).items():
            if isinstance(val, dict):
                size = int(val.get("size") or 0)
                label = val.get("text") or key
            else:
                size = int(val or 0)
                label = key
            if size > 0:
                detail.append({"key": key, "label": str(label), "size": size})
        detail.sort(key=lambda x: x["size"], reverse=True)
        return {
            "total": int(data.get("totalQuota") or 0),
            "used": int(data.get("used") or 0),
            "detail": detail,
        }

    def list_children(
        self,
        parent_id: str,
        page_no: int = 1,
        limit: int = 1000,
        order: str = "SERVICE_TIME",
        reverse: bool = True,
    ) -> Tuple[List[Dict[str, Any]], bool, int]:
        data = self.request(
            "GET",
            "/drive/v2/user/folders/children",
            params={
                "parentId": parent_id,
                "pageNo": page_no,
                "limit": limit,
                "type": "",
                "order": order,
                "reverse": "true" if reverse else "false",
            },
        ) or {}
        records = data.get("records") or []
        return records, is_true(data.get("hasMore")), int(data.get("allCount") or 0)

    def list_all_children(self, parent_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        """读完一个目录的全部条目。"""
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            records, has_more, _ = self.list_children(parent_id, page, limit)
            out.extend(records)
            if not has_more or not records:
                break
            page += 1
            if page > 200:  # 安全阀
                break
        return out

    # ---------------- 相册 ---------------- #
    def gallery_page(self, page_num: int = 0, page_size: int = 1000) -> Dict[str, Any]:
        """
        相册文件分页（pageNum 从 0 开始）。

        该接口较慢且偶发超时，这里做 3 次重试；仍失败则抛出最后一次异常。
        返回 {"galleries": [...], "isLastPage": bool, "indexHash": ...}。
        条目字段：id、fileName、size、type(image/video)、sortTime、dateModified、mimeType、sha1。
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            try:
                data = self.request(
                    "GET",
                    "/gallery/user/galleries",
                    params={"pageNum": page_num, "pageSize": page_size},
                    timeout=90.0,
                )
                return data or {}
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("gallery_page failed")
