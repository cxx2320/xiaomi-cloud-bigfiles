"""云盘全盘递归扫描 + 本地缓存 + 统计汇总。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from xiaomi_drive import ROOT_ID, XiaomiAuthError, XiaomiDrive

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_FILE = DATA_DIR / "scan_cache.json"

TYPE_LABELS = {
    "video": "视频",
    "pic": "图片",
    "doc": "文档",
    "audio": "音频",
    "other": "其他",
    "folder": "文件夹",
}

SOURCE_LABELS = {"drive": "云盘", "gallery": "相册"}


def _join(parent_path: str, name: str) -> str:
    return f"{parent_path}/{name}" if parent_path else f"/{name}"


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.dirs_done = 0
        self.files_found = 0
        self.bytes_found = 0
        self.gallery_pages = 0
        self.gallery_found = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.errors: List[str] = []

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "dirs_done": self.dirs_done,
                "files_found": self.files_found,
                "bytes_found": self.bytes_found,
                "gallery_pages": self.gallery_pages,
                "gallery_found": self.gallery_found,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "errors": self.errors[-10:],
            }


def build_summary(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_size = 0
    by_type: Dict[str, Dict[str, int]] = {}
    by_source: Dict[str, Dict[str, int]] = {}
    dir_size: Dict[str, Dict[str, int]] = {}

    for f in files:
        size = int(f.get("size") or 0)
        total_size += size

        ftype = f.get("type") or "other"
        bucket = by_type.setdefault(ftype, {"size": 0, "count": 0})
        bucket["size"] += size
        bucket["count"] += 1

        src = f.get("source") or "drive"
        sbucket = by_source.setdefault(src, {"size": 0, "count": 0})
        sbucket["size"] += size
        sbucket["count"] += 1

        path = f.get("path") or "/"
        parts = [p for p in path.split("/") if p]
        top = parts[0] if len(parts) > 1 else "(根目录)"
        d = dir_size.setdefault(top, {"size": 0, "count": 0})
        d["size"] += size
        d["count"] += 1

    top_files = sorted(files, key=lambda x: int(x.get("size") or 0), reverse=True)[:20]
    top_dirs = sorted(dir_size.items(), key=lambda kv: kv[1]["size"], reverse=True)[:12]

    return {
        "file_count": len(files),
        "total_size": total_size,
        "largest": int(top_files[0]["size"]) if top_files else 0,
        "by_type": [
            {"type": t, "label": TYPE_LABELS.get(t, t), "size": v["size"], "count": v["count"]}
            for t, v in sorted(by_type.items(), key=lambda kv: kv[1]["size"], reverse=True)
        ],
        "sources": [
            {
                "source": s,
                "label": SOURCE_LABELS.get(s, s),
                "size": v["size"],
                "count": v["count"],
            }
            for s, v in sorted(by_source.items(), key=lambda kv: kv[1]["size"], reverse=True)
        ],
        "top_files": top_files,
        "top_dirs": [
            {"name": name, "size": v["size"], "count": v["count"]} for name, v in top_dirs
        ],
    }


class ScanManager:
    """持有扫描结果与缓存，供 Web 层读取。"""

    def __init__(self, workers: int = 8):
        self.workers = max(1, min(workers, 16))
        self.state = ScanState()
        self.files: List[Dict[str, Any]] = []
        self.summary: Dict[str, Any] = build_summary([])
        self.scanned_at: Optional[float] = None
        self.dirs_total = 0
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.load_cache()

    # ---------------- 缓存 ---------------- #
    def load_cache(self) -> None:
        if not CACHE_FILE.exists():
            return
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        files = data.get("files")
        if not isinstance(files, list):
            return
        self.files = files
        self.scanned_at = data.get("scanned_at")
        self.dirs_total = int(data.get("dirs") or 0)
        self.summary = build_summary(self.files)

    def save_cache(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "scanned_at": self.scanned_at,
            "dirs": self.dirs_total,
            "files": self.files,
        }
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)

    # ---------------- 扫描 ---------------- #
    def start(self, drive: XiaomiDrive) -> bool:
        with self._lock:
            if self.state.running:
                return False
            self._thread = threading.Thread(target=self._run, args=(drive,), daemon=True)
            self._thread.start()
            return True

    def _run(self, drive: XiaomiDrive) -> None:
        st = self.state
        with st.lock:
            st.running = True
            st.dirs_done = 0
            st.files_found = 0
            st.bytes_found = 0
            st.gallery_pages = 0
            st.gallery_found = 0
            st.started_at = time.time()
            st.finished_at = None
            st.error = None
            st.errors = []

        files: List[Dict[str, Any]] = []
        pending: List[tuple] = [(ROOT_ID, "")]
        last_renew = time.time()

        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                while pending:
                    batch, pending = pending[: self.workers], pending[self.workers:]
                    futures = [pool.submit(drive.list_all_children, fid) for fid, _ in batch]

                    for (fid, path), fut in zip(batch, futures):
                        try:
                            records = fut.result()
                        except XiaomiAuthError:
                            raise  # 令牌失效：整盘扫描已无意义，直接中止
                        except Exception as exc:  # noqa: BLE001 - 单个目录失败不应中断全盘
                            with st.lock:
                                if len(st.errors) < 20:
                                    st.errors.append(f"{path or '/'}: {exc}")
                            continue

                        batch_size = 0
                        batch_count = 0
                        for rec in records:
                            name = rec.get("name") or ""
                            rtype = rec.get("type") or "other"
                            child_path = _join(path, name)
                            if rtype == "folder":
                                pending.append((str(rec.get("id")), child_path))
                                continue
                            size = int(rec.get("size") or 0)
                            files.append(
                                {
                                    "id": str(rec.get("id") or ""),
                                    "name": name,
                                    "path": child_path,
                                    "dir": path or "/",
                                    "size": size,
                                    "type": rtype,
                                    "label": (rec.get("label") or "").lower(),
                                    "source": "drive",
                                    "createTime": int(rec.get("createTime") or 0),
                                    "modifyTime": int(rec.get("modifyTime") or 0),
                                }
                            )
                            batch_size += size
                            batch_count += 1

                        with st.lock:
                            st.dirs_done += 1
                            st.files_found += batch_count
                            st.bytes_found += batch_size

                    # serviceToken 约 15 分钟过期，扫描中主动续期
                    if time.time() - last_renew > 240:
                        drive.renew()
                        last_renew = time.time()

            self._scan_gallery(drive, files, st)

            files.sort(key=lambda x: x["size"], reverse=True)
            with self._lock:
                self.files = files
                self.summary = build_summary(files)
                self.scanned_at = time.time()
                self.dirs_total = st.dirs_done
            self.save_cache()
        except Exception as exc:  # noqa: BLE001
            with st.lock:
                st.error = str(exc)
        finally:
            with st.lock:
                st.running = False
                st.finished_at = time.time()

    # ---------------- 相册 ---------------- #
    def _scan_gallery(self, drive: XiaomiDrive, files: List[Dict[str, Any]], st: ScanState) -> None:
        """遍历相册（照片 / 视频）。注意：gallery 的 pageNum 从 0 开始。"""
        page = 0
        while page <= 500:  # 安全阀：最多 50 万条
            data = drive.gallery_page(page, page_size=1000)
            items = data.get("galleries") or []
            if not items:
                break

            page_bytes = 0
            added = 0
            for it in items:
                name = it.get("fileName") or it.get("title") or ""
                if not name:
                    continue
                gtype = "video" if str(it.get("type") or "") == "video" else "pic"
                size = int(it.get("size") or 0)
                created = int(it.get("sortTime") or it.get("dateModified") or 0)
                modified = int(it.get("dateModified") or created)
                year = time.strftime("%Y", time.localtime(created / 1000)) if created else "未知"
                dir_path = f"/相册/{year}"
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                thumb = ""
                for key in ("bigThumbnailInfo", "thumbnailInfo"):
                    info = it.get(key)
                    if isinstance(info, dict) and info.get("isUrl") and info.get("data"):
                        thumb = str(info["data"])
                        break
                files.append(
                    {
                        "id": str(it.get("id") or ""),
                        "name": name,
                        "path": f"{dir_path}/{name}",
                        "dir": dir_path,
                        "size": size,
                        "type": gtype,
                        "label": ext,
                        "source": "gallery",
                        "thumb": thumb,
                        "createTime": created,
                        "modifyTime": modified,
                        "sha1": it.get("sha1") or "",
                    }
                )
                page_bytes += size
                added += 1

            with st.lock:
                st.gallery_pages = page
                st.gallery_found += added
                st.files_found += added
                st.bytes_found += page_bytes

            if data.get("isLastPage") or added == 0:
                break
            page += 1
            time.sleep(0.2)
