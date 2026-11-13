"""
本地读取 data/cookie.txt 并登录到已启动的 Web 服务。

Cookie 只在你的机器上读取，不会打印、不会写入日志。

用法：
    1. 新建 data/cookie.txt，把浏览器里的 Cookie 粘进去（一行即可）
    2. 可选：新建 data/pass_token.txt，粘贴 account.xiaomi.com 下的 passToken
    3. python tools/login.py

登录成功后 cookie.txt 会被自动删除（后端已把凭据存到 data/credentials.json）。
如需保留，加参数 --keep。
"""

import sys
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
COOKIE_FILE = ROOT / "data" / "cookie.txt"
PASS_FILE = ROOT / "data" / "pass_token.txt"


def human(n: int) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def main() -> int:
    if not COOKIE_FILE.exists():
        print(f"[!] 找不到 {COOKIE_FILE}")
        print("    请新建该文件，把浏览器里的 Cookie 粘进去后再运行。")
        return 1

    cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not cookie:
        print("[!] cookie.txt 是空的")
        return 1

    pass_token = ""
    if PASS_FILE.exists():
        pass_token = PASS_FILE.read_text(encoding="utf-8").strip()

    print(f"[*] 读取到 Cookie（长度 {len(cookie)}），passToken：{'有' if pass_token else '无'}")

    try:
        resp = requests.post(
            BASE + "/api/login",
            json={"cookie": cookie, "pass_token": pass_token},
            timeout=90,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 请求本地服务失败：{exc}")
        print("    服务是否在运行？  python app.py")
        return 1

    if not resp.ok:
        print(f"[x] 登录失败（HTTP {resp.status_code}）：{data.get('detail')}")
        return 1

    quota = data.get("quota") or {}
    print(f"[✓] 登录成功：用户 {data.get('user')}")
    print(f"    容量：已用 {human(quota.get('used', 0))} / 共 {human(quota.get('total', 0))}")

    if "--keep" not in sys.argv:
        try:
            COOKIE_FILE.unlink()
            PASS_FILE.unlink(missing_ok=True)
            print("[*] 已删除 data/cookie.txt（凭据已保存在 data/credentials.json）")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
