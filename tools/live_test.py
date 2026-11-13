"""
真实账号端到端测试：扫描全盘 → 校验排序 / 过滤 / 统计 / 导出。

前提：已通过 tools/login.py（或页面）登录，且服务在 http://127.0.0.1:8000 运行。
用法：python tools/live_test.py [--max-wait 秒]
"""

import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
S = requests.Session()


def human(n) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} TB"


def ts(v) -> str:
    if not v:
        return "-"
    n = int(v)
    if n < 10_000_000_000:  # 秒级时间戳
        n *= 1000
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(n / 1000))


def get(path, **params):
    r = S.get(BASE + path, params=params, timeout=180)
    r.raise_for_status()
    return r.json()


def check(name, ok, detail=""):
    print(f"  {'[✓]' if ok else '[x]'} {name}{('  ' + detail) if detail else ''}")
    return ok


def main() -> int:
    max_wait = 1800
    if "--max-wait" in sys.argv:
        max_wait = int(sys.argv[sys.argv.index("--max-wait") + 1])

    print("=" * 68)
    print("1) 检查登录状态")
    st = get("/api/status")
    if not st.get("logged_in"):
        print("  [x] 未登录，请先运行：python tools/login.py")
        return 1
    q = st.get("quota") or {}
    print(f"  用户：{st.get('user')}  容量：{human(q.get('used'))} / {human(q.get('total'))}")
    print(f"  缓存：{st['cache']['count']} 个文件（扫描时间 {ts(st['cache']['scanned_at'])}）")

    print("\n2) 启动全盘扫描")
    try:
        r = S.post(BASE + "/api/scan", json={"workers": 8}, timeout=30)
        print(f"  HTTP {r.status_code} {r.json()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [x] 启动失败：{exc}")
        return 1

    print("\n3) 等待扫描完成（每 5 秒刷新，最多 {} 秒）".format(max_wait))
    start = time.time()
    last = 0.0
    while True:
        p = get("/api/scan")
        now = time.time()
        if now - last > 5 or not p.get("running"):
            last = now
            print(
                f"  目录 {p.get('dirs_done'):>6} ｜ 文件 {p.get('files_found'):>7} ｜ "
                f"累计 {human(p.get('bytes_found')):>10} ｜ 用时 {now - start:.0f}s"
            )
        if not p.get("running"):
            break
        if now - start > max_wait:
            print("  [!] 超时，扫描仍在后台继续")
            return 2
        time.sleep(1)

    if p.get("error"):
        print(f"  [x] 扫描出错：{p['error']}")
        return 1
    if p.get("errors"):
        print("  [!] 部分目录失败：")
        for e in p["errors"]:
            print("      " + e)

    print("\n4) 汇总统计")
    sm = get("/api/summary")
    s = sm["summary"]
    print(f"  文件总数 {s['file_count']:,}　总占用 {human(s['total_size'])}　最大单文件 {human(s['largest'])}")
    print("  占用最多的目录：")
    for d in s["top_dirs"][:5]:
        print(f"    {d['name']:<22} {human(d['size']):>10}  {d['count']} 个")
    print("  按类型：")
    for t in s["by_type"]:
        print(f"    {t['label']:<6} {human(t['size']):>10}  {t['count']} 个")

    ok = True
    print("\n5) 排序 / 过滤校验")
    top = get("/api/files", sort="size", order="desc", limit=10)
    ok &= check("按大小降序", all(
        top["items"][i]["size"] >= top["items"][i + 1]["size"] for i in range(len(top["items"]) - 1)
    ), f"共 {top['total']:,} 个文件")
    if top["items"]:
        print(f"      最大：{top['items'][0]['name']}  {human(top['items'][0]['size'])}")

    asc = get("/api/files", sort="size", order="asc", limit=5)
    ok &= check("按大小升序", all(
        asc["items"][i]["size"] <= asc["items"][i + 1]["size"] for i in range(len(asc["items"]) - 1)
    ))

    newest = get("/api/files", sort="created", order="desc", limit=5)
    ok &= check("按创建时间降序", all(
        newest["items"][i]["createTime"] >= newest["items"][i + 1]["createTime"]
        for i in range(len(newest["items"]) - 1)
    ))
    for it in newest["items"][:3]:
        print(f"      {ts(it['createTime'])}  {human(it['size']):>9}  {it['name']}")

    oldest = get("/api/files", sort="created", order="asc", limit=3)
    ok &= check("按创建时间升序", all(
        oldest["items"][i]["createTime"] <= oldest["items"][i + 1]["createTime"]
        for i in range(len(oldest["items"]) - 1)
    ), f"最早：{ts(oldest['items'][0]['createTime'])}" if oldest["items"] else "")

    page2 = get("/api/files", sort="size", order="desc", limit=5, offset=5)
    ok &= check("分页偏移", page2["offset"] == 5 and len(page2["items"]) <= 5)

    big = get("/api/files", min_size=1024 ** 3, limit=20)
    ok &= check(
        "最小大小过滤（≥1GB）",
        all(i["size"] >= 1024 ** 3 for i in big["items"]),
        f"命中 {big['total']:,} 个",
    )

    vids = get("/api/files", **{"type": "video", "limit": 1})
    ok &= check("类型过滤（视频）", True, f"命中 {vids['total']:,} 个")

    if top["items"]:
        kw = top["items"][0]["name"][:6]
        found = get("/api/files", q=kw, limit=5)
        ok &= check("关键字搜索", found["total"] >= 1, f"'{kw}' 命中 {found['total']:,} 个")

    print("\n6) 导出 CSV")
    r = S.get(BASE + "/api/export", timeout=300)
    lines = r.content.decode("utf-8-sig").splitlines()
    ok &= check(
        "CSV 导出",
        r.ok and lines and lines[0].startswith("名称"),
        f"{len(r.content):,} 字节 / {max(0, len(lines) - 1):,} 行数据",
    )
    if lines:
        print(f"      表头：{lines[0]}")

    print("\n" + "=" * 68)
    print("测试结果：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
