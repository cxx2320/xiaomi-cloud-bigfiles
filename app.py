"""小米云盘大文件分析 —— Web 应用入口。

启动： python app.py   然后打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scanner import SOURCE_LABELS, TYPE_LABELS, ScanManager, build_summary
from xiaomi_drive import (
    Credentials,
    XiaomiAPIError,
    XiaomiAuthError,
    XiaomiDrive,
    delete_credentials,
    load_credentials,
    parse_cookie_string,
    save_credentials,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="小米云盘大文件分析", docs_url="/api/docs")


class AppState:
    def __init__(self) -> None:
        self.drive: Optional[XiaomiDrive] = None
        self.creds: Optional[Credentials] = None
        self.scanner = ScanManager(workers=8)
        creds = load_credentials()
        if creds:
            self.attach(creds)

    def attach(self, creds: Credentials) -> None:
        """切换登录凭据，并启动后台保活线程。"""
        if self.drive:
            self.drive.stop_keepalive()
        self.creds = creds
        self.drive = XiaomiDrive(creds)
        self.drive.start_keepalive()

    def detach(self) -> None:
        if self.drive:
            self.drive.stop_keepalive()
        self.drive = None
        self.creds = None


state = AppState()


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    cookie: str = ""
    pass_token: str = ""


class ScanBody(BaseModel):
    workers: int = 8


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- #
# 登录 / 状态
# --------------------------------------------------------------------------- #
def _mask(uid: str) -> str:
    if not uid:
        return ""
    if len(uid) <= 4:
        return uid[0] + "***"
    return uid[:2] + "***" + uid[-2:]


def _quota() -> Optional[Dict[str, int]]:
    if not state.drive:
        return None
    try:
        return state.drive.quota()
    except Exception:  # noqa: BLE001 - 状态查询失败不应影响页面加载
        return None


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    quota = _quota()
    scan = state.scanner.state.snapshot()
    return {
        "logged_in": bool(state.drive),
        "token_ok": quota is not None,
        "user": _mask(state.creds.user_id) if state.creds else "",
        "has_pass_token": bool(state.creds and state.creds.pass_token),
        "quota": quota,
        "cache": {
            "count": len(state.scanner.files),
            "size": int(state.scanner.summary.get("total_size") or 0),
            "dirs": state.scanner.dirs_total,
            "scanned_at": state.scanner.scanned_at,
        },
        "scan": scan,
    }


@app.post("/api/login")
def api_login(body: LoginBody) -> Dict[str, Any]:
    cookies = parse_cookie_string(body.cookie)

    service_token = cookies.get("serviceToken") or cookies.get("servicetoken") or ""
    user_id = cookies.get("userId") or cookies.get("userid") or ""
    pass_token = body.pass_token.strip() or cookies.get("passToken") or ""

    if not service_token or not user_id:
        raise HTTPException(
            status_code=400,
            detail="Cookie 里缺少 serviceToken 或 userId，请在 i.mi.com 的开发者工具中复制完整 Cookie",
        )

    creds = Credentials(user_id=str(user_id), service_token=service_token, pass_token=pass_token)
    creds.device_id = (state.creds.device_id if state.creds else "") or ""
    creds.ensure_device_id()

    drive = XiaomiDrive(creds)
    try:
        quota = drive.quota()
    except XiaomiAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except XiaomiAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 网络异常等
        raise HTTPException(status_code=502, detail=f"连接小米云失败：{exc}") from exc

    state.attach(creds)
    save_credentials(creds)

    return {"ok": True, "user": _mask(creds.user_id), "quota": quota}


@app.post("/api/logout")
def api_logout() -> Dict[str, Any]:
    state.detach()
    delete_credentials()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
@app.post("/api/scan")
def api_scan(body: Optional[ScanBody] = None) -> Dict[str, Any]:
    if not state.drive:
        raise HTTPException(status_code=401, detail="尚未登录")
    if state.scanner.state.snapshot()["running"]:
        raise HTTPException(status_code=409, detail="扫描已在进行中")

    if body is not None and body.workers:
        state.scanner.workers = max(1, min(int(body.workers), 16))

    started = state.scanner.start(state.drive)
    if not started:
        raise HTTPException(status_code=409, detail="扫描已在进行中")
    return {"ok": True}


@app.get("/api/scan")
def api_scan_progress() -> Dict[str, Any]:
    return state.scanner.state.snapshot()


# --------------------------------------------------------------------------- #
# 文件列表 / 统计
# --------------------------------------------------------------------------- #
SORT_KEYS = {
    "size": "size",
    "created": "createTime",
    "modified": "modifyTime",
    "name": "name",
}


@app.get("/api/files")
def api_files(
    sort: str = Query("size"),
    order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    min_size: int = Query(0, ge=0),
    ftype: str = Query("", alias="type"),
    source: str = Query(""),
    q: str = Query(""),
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = state.scanner.files

    filtered = files
    if min_size > 0 or ftype or q or source:
        needle = q.strip().lower()
        filtered = [
            f
            for f in files
            if int(f.get("size") or 0) >= min_size
            and (not ftype or f.get("type") == ftype)
            and (not source or (f.get("source") or "drive") == source)
            and (not needle or needle in (f.get("name") or "").lower() or needle in (f.get("path") or "").lower())
        ]

    key = SORT_KEYS.get(sort, "size")
    reverse = order.lower() != "asc"
    if key == "name":
        filtered = sorted(filtered, key=lambda f: (f.get("name") or "").lower(), reverse=reverse)
    else:
        filtered = sorted(filtered, key=lambda f: int(f.get(key) or 0), reverse=reverse)

    total = len(filtered)
    page = filtered[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "size_sum": sum(int(f.get("size") or 0) for f in filtered),
        "items": page,
        "type_labels": TYPE_LABELS,
    }


@app.get("/api/summary")
def api_summary() -> Dict[str, Any]:
    return {
        "summary": state.scanner.summary,
        "scanned_at": state.scanner.scanned_at,
        "type_labels": TYPE_LABELS,
    }


@app.get("/api/summary/rebuild")
def api_summary_rebuild() -> Dict[str, Any]:
    state.scanner.summary = build_summary(state.scanner.files)
    return state.scanner.summary


@app.get("/api/export")
def api_export() -> Response:
    files = sorted(state.scanner.files, key=lambda f: int(f.get("size") or 0), reverse=True)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["名称", "来源", "路径", "大小(字节)", "大小", "类型", "创建时间", "修改时间"])
    for f in files:
        writer.writerow(
            [
                f.get("name", ""),
                SOURCE_LABELS.get(f.get("source") or "drive", ""),
                f.get("path", ""),
                int(f.get("size") or 0),
                _human_size(int(f.get("size") or 0)),
                TYPE_LABELS.get(f.get("type") or "", f.get("type") or ""),
                _fmt_time(f.get("createTime")),
                _fmt_time(f.get("modifyTime")),
            ]
        )

    data = "\ufeff" + buf.getvalue()  # BOM 让 Excel 正确识别 UTF-8
    filename = time.strftime("xiaomi_drive_%Y%m%d_%H%M.csv")
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _fmt_time(ms: Any) -> str:
    try:
        ts = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    if ts > 10_000_000_000:  # 毫秒
        ts //= 1000
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
