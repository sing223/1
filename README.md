# FC2 视频女优分类脚本

根据视频文件名中的 FC2 番号查询 [FC2CMADB](https://fc2cmadb.com/)，读取作品的女优名，并把视频移动到对应的女优同名文件夹。

脚本支持断点续跑、女优映射缓存、失败记录、随机访问延迟，以及登录完成后再接管 Chrome 的两阶段浏览器流程。

## 主要功能

- 识别 `FC2-1234567`、`FC2-PPV-1234567`、`FC2PPV_1234567` 等常见文件名。
- 自动扫描指定目录及其子目录。
- 从 FC2CMADB 作品详情页提取第一个女优名。
- 自动创建女优同名文件夹并移动视频。
- 将成功结果写入 `actress_map.csv`，后续运行无需再次查询。
- 使用 `cache.json` 保存待处理任务，程序中断后可以继续。
- 使用 `failed.json` 和 `logs/run.log` 保存失败原因。
- 每次浏览器查询前随机等待 5～20 秒。
- 过滤 `女優`、`女优`、`Actor`、`Actress` 等错误占位值。

## 工作流程

默认使用 `chrome_cdp` 后端：

1. 扫描视频目录并建立缓存。
2. 启动一个独立的普通 Google Chrome。
3. 此时脚本尚未连接浏览器，请在 Chrome 中手动完成年龄确认、Cloudflare 验证和登录。
4. 登录成功后回到终端按回车。
5. 脚本通过本地 CDP 端口接管同一个 Chrome 会话。
6. 逐条打开作品详情页，等待动态女优链接加载。
7. 创建女优文件夹、移动视频并更新映射表。

登录前不连接 Playwright/CDP，是为了避免 Turnstile 出现 `Error 300031` 或跨域 `postMessage` 错误。

## 环境要求

- Windows 10/11
- Python 3.9 或更高版本
- Google Chrome
- 能够访问 FC2CMADB 的网络

## 安装

```powershell
git clone https://github.com/sing223/1.git
cd 1
git switch codex/cloakbrowser-hybrid
python -m pip install -r requirements.txt
```

默认的 `chrome_cdp` 后端使用系统 Chrome。项目仍保留 CloakBrowser 后端作为可选方案。

## 配置

复制示例配置：

```powershell
Copy-Item config.example.json config.json
```

编辑 `config.json`：

```json
{
  "video_dir": "D:\\Videos\\FC2",
  "cookie": "",
  "video_extensions": [".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts"],
  "uncategorized_folder": "未分类",
  "overwrite_existing": true,
  "request_timeout": 20,
  "actress_map_csv": "actress_map.csv",
  "lookup_mode": "browser",
  "browser_backend": "chrome_cdp",
  "chrome_executable": "",
  "browser_profile_dir": "chrome_cdp_profile",
  "browser_headless": false,
  "browser_wait_seconds": 60,
  "save_browser_results_to_csv": true
}
```

### 配置项说明

| 配置项 | 说明 |
| --- | --- |
| `video_dir` | 待整理视频的根目录，支持本地路径和 UNC 网络路径 |
| `cookie` | requests 模式使用的 Cookie；浏览器模式通常留空 |
| `video_extensions` | 需要扫描的视频扩展名 |
| `uncategorized_folder` | 无法分类时使用的目录名 |
| `overwrite_existing` | 目标存在同名文件时是否覆盖 |
| `request_timeout` | 普通 HTTP 请求超时秒数 |
| `actress_map_csv` | 番号与女优名映射表 |
| `lookup_mode` | 查询模式，默认 `browser` |
| `browser_backend` | `chrome_cdp` 或 `cloak` |
| `chrome_executable` | Chrome 可执行文件路径；留空时自动查找 |
| `browser_profile_dir` | 独立浏览器 Profile 目录 |
| `browser_headless` | 是否无头运行；需要登录时必须为 `false` |
| `browser_wait_seconds` | 页面导航超时秒数 |
| `save_browser_results_to_csv` | 是否把浏览器查询结果写入映射表 |

### 查询模式

| 模式 | 行为 |
| --- | --- |
| `browser` | 使用浏览器查询，推荐 |
| `requests` | 仅使用普通 HTTP 请求 |
| `hybrid` | 先 requests，受限后回退浏览器 |
| `manual` | 打开网页后手动输入女优名 |
| `csv_only` | 仅使用现有映射表，不访问网站 |

### 浏览器后端

`chrome_cdp`：

- 默认选项。
- 先使用普通 Chrome 完成登录，之后脚本才连接。
- 登录状态保存在 `chrome_cdp_profile`。

`cloak`：

- 使用 CloakBrowser 定制 Chromium。
- 某些版本可能与 Cloudflare Turnstile 存在 iframe 通信兼容问题。
- 可以在配置中设置：

```json
"browser_backend": "cloak"
```

## 运行

双击：

```text
run.bat
```

或在 PowerShell 中运行：

```powershell
python main.py
```

首次运行浏览器模式时：

1. 等待 Chrome 自动打开。
2. 在右上角进入登录窗口。
3. 手动完成验证并登录。
4. 确认网页已经处于登录状态。
5. 回终端按回车。

不要在登录完成前按回车，否则脚本会过早接管浏览器。

## 输出目录示例

```text
视频根目录/
├── アキナ/
│   └── FC2-936916.mp4
├── ゆみ/
│   └── FC2-2733769.mp4
└── 未分类/
```

目标女优文件夹不存在时会自动创建。

## 运行状态文件

以下文件只保存在本地，不会提交到 Git：

| 文件或目录 | 用途 |
| --- | --- |
| `config.json` | 本机配置 |
| `actress_map.csv` | 成功查询的番号映射 |
| `cache.json` | 尚未完成的任务 |
| `failed.json` | 历史失败记录 |
| `organized_dirs.json` | 已创建的分类目录 |
| `logs/run.log` | 完整运行日志 |
| `chrome_cdp_profile/` | Chrome 登录状态和缓存 |

## 测试

```powershell
python -m unittest -v
python -m py_compile main.py test_main.py
```

## 常见问题

### Turnstile 一直转圈或登录按钮为灰色

- 确认使用的是 `chrome_cdp` 后端。
- 关闭之前由脚本启动的 Chrome，再重新运行。
- 必须在终端按回车之前完成登录。
- 不要同时启动多个脚本实例，它们不能共用同一个 Profile。

### 页面能看到女优名，但脚本提示未解析到

脚本会等待动态女优链接最多 10 秒。请确认使用的是最新代码，并重新启动脚本。女优链接应类似：

```html
<a href="https://fc2cmadb.com/actresses/7407">アキナ</a>
```

### 出现 `女優`、`https_` 等错误文件夹

这是旧版本错误解析产生的结果。新版本会过滤字段标题，但不会自动移动已经整理的文件，以免误操作。请手动检查并整理这些旧目录。

### 提示浏览器 Profile 已被占用

关闭所有由本脚本启动的 Chrome 窗口后重试。不要让两个脚本实例同时使用同一个 `browser_profile_dir`。

### 网络目录不存在

如果 `video_dir` 是类似 `\\nas\视频\hpes` 的 UNC 路径，请先确认：

- NAS 已连接；
- 当前 Windows 用户有访问权限；
- 路径拼写正确。

## 注意事项

- 脚本不会自动处理验证码，验证和登录必须由用户手动完成。
- 请遵守目标网站的使用条款和当地法律。
- 不要把 `config.json`、Cookie、浏览器 Profile 或私人映射表上传到公共仓库。
- 大量连续访问可能触发网站限制，请保留随机延迟。

