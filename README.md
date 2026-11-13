# 小米云盘 · 大文件分析

一个本地运行的 Python Web 应用：扫描小米云**盘**与云**相册**（i.mi.com）的全部文件，按**文件大小**或**新增/修改日期**排序，快速找出最占空间的视频、照片和压缩包。

> 使用社区逆向的非官方接口，仅供个人查看自己账号的存储占用。只做读取，不会修改或删除任何云盘内容。

## 功能

- 登录后展示云盘总容量 / 已用空间
- 一键递归扫描：**云盘全目录 + 云相册**（照片与视频），带实时进度
- 按 大小 / 创建时间 / 修改时间 / 名称 排序，升降序切换
- 预览：图片与视频可点「预览」弹窗查看缩略图（相册）；`Esc` 关闭
- 过滤：最小大小（10MB / 100MB / 500MB / 1GB / 5GB）、文件类型、来源（云盘 / 相册）、关键字
- 汇总：占用最多的目录 Top 12、按类型统计、最大单文件
- 扫描结果缓存到 `data/scan_cache.json`，重启后无需重新扫描
- 导出 CSV（Excel 可直接打开）
- 后台每 4 分钟自动续期 `serviceToken`，不会因 15 分钟过期而中断

> 预览用的是相册返回的**带签名缩略图直链**，有效期由小米控制。若某天预览提示「签名已过期」，重新点一次「开始扫描全盘」即可刷新。

## 快速开始

```powershell
cd e:\python_mi_yun
python -m pip install -r requirements.txt
python app.py
```

浏览器打开 <http://127.0.0.1:8000>。

## 获取 Cookie

1. 浏览器打开 <https://i.mi.com> 并登录小米账号
2. `F12` → Console → 输入 `document.cookie` 回车
3. 复制整段字符串（需含 `serviceToken` 和 `userId`）
4. 粘贴到页面输入框，点击「连接云盘」

**关于 passToken（建议填）**：`serviceToken` 只有约 15 分钟有效期。全盘扫描耗时较长时，建议额外提供 `account.xiaomi.com` 域名下的 `passToken`，程序会在令牌过期时自动换新，扫描不会中断。

> 浏览器 DevTools → Application → Cookies → `account.xiaomi.com` 中查找 `passToken`。

## 目录结构

```
app.py             FastAPI 应用与 HTTP 接口
xiaomi_drive.py    小米云盘客户端（鉴权、续期、列目录、容量）
scanner.py         全盘递归扫描、缓存、统计汇总
static/            前端页面（原生 HTML/CSS/JS，无构建步骤）
data/              运行时生成：credentials.json（Cookie）、scan_cache.json（扫描缓存）
```

## 接口一览

| 操作 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 列目录 | GET | `/drive/v2/user/folders/children` | `parentId`、`pageNo`、`limit`、`order`、`reverse` |
| 相册列表 | GET | `/gallery/user/galleries` | `pageNum`、`pageSize`，返回 `galleries[]`、`isLastPage` |
| 容量 | GET | `/status/alldetail` | `totalQuota`、`used`、`usedDetail`（按服务拆分） |
| 续期 | GET | `/status/lite/setting` | 通过 Set-Cookie 下发新 `serviceToken` |
| 文件详情 | GET | `/drive/v2/user/files/<id>` | 含 `storage.downloadUrl` |

小米随时可能调整接口，如遇 401 或错误码，重新登录一次通常即可。

## 注意事项

- 扫描大账号（上万文件）可能耗时数分钟，期间请保持页面打开
- `data/credentials.json` 内含登录凭据，权限为 600，请勿分享该文件
- 空的 `serviceToken` 或错误的 `userId` 会直接返回 401
