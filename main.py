"""根据 FC2 番号查询女优名，并把视频整理到对应的女优目录。

默认流程：
1. 扫描配置的视频目录并建立可恢复的缓存。
2. 启动普通 Chrome，让用户在脚本连接前手动完成登录和 Turnstile。
3. 登录后通过 CDP 接管同一浏览器，逐条查询 FC2CMADB。
4. 将查询结果写入本地映射表，并移动视频到女优同名目录。

缓存、失败记录和持久浏览器 Profile 都保存在脚本目录，但不会提交到 Git。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover - shown to users in the console.
    requests = None

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - shown to users in the console.
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

try:
    from cloakbrowser import launch_persistent_context
except ImportError:  # pragma: no cover - shown to users in the console.
    launch_persistent_context = None


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
CACHE_PATH = SCRIPT_DIR / "cache.json"
FAILED_PATH = SCRIPT_DIR / "failed.json"
ORGANIZED_DIRS_PATH = SCRIPT_DIR / "organized_dirs.json"
LOG_DIR = SCRIPT_DIR / "logs"
LOG_PATH = LOG_DIR / "run.log"
TARGET_DOMAIN = "fc2cmadb.com"
TARGET_BASE_URL = f"https://{TARGET_DOMAIN}"

DEFAULT_VIDEO_EXTENSIONS = [".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts"]
FC2_PATTERN = re.compile(r"(?i)\bfc2(?:\s*[-_ ]?\s*ppv)?\s*[-_ ]?\s*(\d{4,10})\b")
INVALID_WINDOWS_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')
LOOKUP_DELAY_MIN_SECONDS = 5
LOOKUP_DELAY_MAX_SECONDS = 20
ACTRESS_LINK_WAIT_MILLISECONDS = 10_000


@dataclass
class Config:
    """config.json 解析后的强类型运行配置。"""

    video_dir: Path
    cookie: str
    video_extensions: List[str]
    uncategorized_folder: str
    overwrite_existing: bool
    request_timeout: int
    actress_map_csv: Path
    web_lookup_enabled: bool
    lookup_mode: str
    browser_profile_dir: Path
    browser_headless: bool
    browser_wait_seconds: int
    save_browser_results_to_csv: bool
    browser_backend: str = "chrome_cdp"
    chrome_executable: Optional[Path] = None


@dataclass
class FetchResult:
    """一次女优查询的结果；blocked 表示遇到验证或访问限制。"""

    actress: Optional[str]
    error: Optional[str]
    blocked: bool = False


def setup_logging() -> None:
    """同时向控制台和 UTF-8 日志文件输出运行信息。"""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config() -> Config:
    """读取并校验配置，同时把相对路径转换为脚本目录下的绝对路径。"""

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    video_dir = Path(raw.get("video_dir", "")).expanduser()
    if not video_dir:
        raise ValueError("config.json 里的 video_dir 不能为空")
    if not video_dir.exists() or not video_dir.is_dir():
        raise ValueError(f"视频目录不存在或不是文件夹：{video_dir}")

    extensions = raw.get("video_extensions") or DEFAULT_VIDEO_EXTENSIONS
    extensions = [ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions]

    actress_map_csv = Path(raw.get("actress_map_csv", "actress_map.csv")).expanduser()
    if not actress_map_csv.is_absolute():
        actress_map_csv = SCRIPT_DIR / actress_map_csv

    browser_profile_dir = Path(raw.get("browser_profile_dir", "browser_profile")).expanduser()
    if not browser_profile_dir.is_absolute():
        browser_profile_dir = SCRIPT_DIR / browser_profile_dir
    browser_backend = str(raw.get("browser_backend", "chrome_cdp")).strip().lower()
    if browser_backend not in {"chrome_cdp", "cloak"}:
        raise ValueError("browser_backend 只能是 chrome_cdp 或 cloak")
    chrome_executable_raw = str(raw.get("chrome_executable", "")).strip()
    chrome_executable = Path(chrome_executable_raw).expanduser() if chrome_executable_raw else None

    lookup_mode = str(raw.get("lookup_mode", "")).strip().lower()
    if not lookup_mode:
        lookup_mode = "browser"
    if lookup_mode not in {"csv_only", "manual", "browser", "requests", "hybrid"}:
        raise ValueError("lookup_mode 只能是 csv_only、manual、browser、requests 或 hybrid")

    return Config(
        video_dir=video_dir.resolve(),
        cookie=raw.get("cookie", "").strip(),
        video_extensions=extensions,
        uncategorized_folder=raw.get("uncategorized_folder", "未分类").strip() or "未分类",
        overwrite_existing=bool(raw.get("overwrite_existing", True)),
        request_timeout=int(raw.get("request_timeout", 20)),
        actress_map_csv=actress_map_csv.resolve(),
        web_lookup_enabled=lookup_mode in {"requests", "hybrid"},
        lookup_mode=lookup_mode,
        browser_profile_dir=browser_profile_dir.resolve(),
        browser_headless=bool(raw.get("browser_headless", False)),
        browser_wait_seconds=int(raw.get("browser_wait_seconds", 60)),
        save_browser_results_to_csv=bool(raw.get("save_browser_results_to_csv", True)),
        browser_backend=browser_backend,
        chrome_executable=chrome_executable,
    )


def load_json_file(path: Path, default):
    """安全读取运行状态 JSON；损坏时返回默认值，避免整个任务中断。"""

    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.warning("JSON 文件损坏，将使用默认值：%s", path)
        return default


def save_json_file(path: Path, data) -> None:
    """先写临时文件再原子替换，降低程序中断导致 JSON 损坏的概率。"""

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def load_actress_map(path: Path) -> Dict[str, str]:
    """读取番号到女优名的 CSV 映射，并忽略“女優”等历史错误占位值。"""

    if not path.exists():
        logging.warning("女优映射表不存在：%s", path)
        return {}

    actress_map: Dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            lowered = {name.lower().strip(): name for name in reader.fieldnames}
            number_field = lowered.get("number") or lowered.get("fc2") or lowered.get("id") or lowered.get("番号")
            actress_field = lowered.get("actress") or lowered.get("name") or lowered.get("女优") or lowered.get("女優")
            if number_field and actress_field:
                for row in reader:
                    number = normalize_number(row.get(number_field, ""))
                    actress = normalize_actress_name(row.get(actress_field, ""))
                    if number and actress:
                        actress_map[number] = actress
                logging.info("已读取女优映射表：%s 条", len(actress_map))
                return actress_map

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            number = normalize_number(row[0])
            actress = normalize_actress_name(row[1])
            if number and actress and number.lower() not in {"number", "fc2", "id", "番号"}:
                actress_map[number] = actress

    logging.info("已读取女优映射表：%s 条", len(actress_map))
    return actress_map


def append_actress_map(path: Path, number: str, actress: str) -> None:
    """把新查询结果追加到 CSV，供后续运行直接复用。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if needs_header:
            writer.writerow(["number", "actress"])
        writer.writerow([number, actress])
    logging.info("已追加女优映射：FC2-%s -> %s", number, actress)


def normalize_number(value: str) -> Optional[str]:
    """从 CSV 或文本中提取纯数字 FC2 番号。"""

    text = str(value).strip()
    if not text:
        return None
    match = FC2_PATTERN.search(text)
    if match:
        return match.group(1)
    match = re.search(r"\d{4,10}", text)
    return match.group(0) if match else None


def extract_fc2_number(filename: str) -> Optional[str]:
    """从常见的 FC2/FC2-PPV 文件名格式中提取番号。"""

    match = FC2_PATTERN.search(filename)
    return match.group(1) if match else None


def sanitize_folder_name(name: str) -> str:
    """移除 Windows 文件夹名不允许的字符。"""

    cleaned = INVALID_WINDOWS_NAME_CHARS.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "未知女优"


INVALID_ACTRESS_NAMES = {
    "",
    "女优",
    "女優",
    "出演",
    "出演者",
    "actor",
    "actress",
    "未知女优",
}


def normalize_actress_name(name: str) -> Optional[str]:
    """清理女优名，并拒绝字段标题、空值等无效结果。"""

    cleaned = sanitize_folder_name(str(name))
    if cleaned.strip().lower() in INVALID_ACTRESS_NAMES:
        return None
    return cleaned


def wait_before_browser_lookup() -> int:
    """每次联网查询前随机等待 5~20 秒，降低连续访问频率。"""

    seconds = random.randint(LOOKUP_DELAY_MIN_SECONDS, LOOKUP_DELAY_MAX_SECONDS)
    logging.info("下一个视频检索前随机等待 %s 秒", seconds)
    time.sleep(seconds)
    return seconds


def find_chrome_executable(configured: Optional[Path] = None) -> Path:
    """优先采用配置路径，否则查找 Windows 常见 Chrome 安装位置。"""

    candidates = []
    if configured:
        candidates.append(configured)
    for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到 Google Chrome，请安装 Chrome 或在 config.json 设置 chrome_executable"
    )


def reserve_local_port() -> int:
    """向系统申请一个暂时空闲的本地端口，用于 Chrome DevTools Protocol。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def build_chrome_cdp_command(executable: Path, profile: Path, port: int) -> List[str]:
    """生成普通 Chrome 启动命令；此阶段不建立 Playwright/CDP 连接。"""

    return [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        TARGET_BASE_URL,
    ]


def path_is_inside(child: Path, parent: Path) -> bool:
    """判断 child 是否位于 parent 内部，用于避免重复扫描输出目录。"""

    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def should_skip_path(path: Path, video_root: Path, skipped_dirs: Iterable[Path]) -> bool:
    """判断路径是否属于已经整理过或需要跳过的目录。"""

    resolved = path.resolve()
    for skipped in skipped_dirs:
        if path_is_inside(resolved, skipped):
            return True
    return not path_is_inside(resolved, video_root)


def iter_video_files(config: Config, organized_dirs: Iterable[str]) -> Iterable[Path]:
    """递归枚举未整理的视频文件。"""

    skipped_dirs = [config.video_dir / config.uncategorized_folder]
    skipped_dirs.extend(config.video_dir / name for name in organized_dirs if name)

    for path in config.video_dir.rglob("*"):
        if not path.is_file():
            continue
        if should_skip_path(path, config.video_dir, skipped_dirs):
            continue
        if path.suffix.lower() in config.video_extensions:
            yield path


def build_cache(config: Config, existing_cache: Dict[str, dict], organized_dirs: Iterable[str]) -> Dict[str, dict]:
    """合并已有缓存与本次扫描结果，使中断后的任务可以继续运行。"""

    cache = {
        key: item
        for key, item in existing_cache.items()
        if item.get("path") and Path(item["path"]).exists()
    }

    existing_paths = {item["path"] for item in cache.values()}
    for path in iter_video_files(config, organized_dirs):
        number = extract_fc2_number(path.name)
        if not number:
            continue
        path_text = str(path.resolve())
        if path_text in existing_paths:
            continue
        cache_key = f"{number}:{path_text}"
        cache[cache_key] = {
            "number": number,
            "path": path_text,
            "filename": path.name,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        existing_paths.add(path_text)
        logging.info("加入缓存：FC2-%s -> %s", number, path_text)
    return cache


def apply_cookie(session: requests.Session, cookie_text: str) -> None:
    """把配置中的 Cookie 字符串加载到 requests 会话。"""

    if not cookie_text:
        logging.warning("config.json 未填写 cookie，详情页可能无法访问")
        return

    cookie = SimpleCookie()
    cookie.load(cookie_text)
    for key, morsel in cookie.items():
        session.cookies.set(key, morsel.value, domain=TARGET_DOMAIN)


def create_session(config: Config) -> requests.Session:
    """创建带常用浏览器请求头和可选 Cookie 的 HTTP 会话。"""

    if requests is None:
        raise RuntimeError("缺少依赖 requests，请先运行：pip install -r requirements.txt")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7,zh;q=0.6",
        }
    )
    apply_cookie(session, config.cookie)
    return session


def strip_tags(html: str) -> str:
    """移除脚本、样式和 HTML 标签，保留可读文本。"""

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def first_match(html: str, patterns: Iterable[str]) -> Optional[str]:
    """依次尝试正则表达式并返回第一个非空文本结果。"""

    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            text = strip_tags(match.group(1))
            if text:
                return text
    return None


def parse_first_actress(html: str) -> Optional[str]:
    """从静态 HTML 中解析第一个女优链接，作为浏览器 DOM 定位的后备方案。"""

    if re.search(r'<form[^>]+action=["\']https://fc2cmadb\.com/login["\']', html, re.IGNORECASE):
        return None

    patterns = [
        r'<a[^>]+href=["\'](?:https://fc2cmadb\.com)?/actresses/\d+["\'][^>]*>(.*?)</a>',
        r"(?:女优|女優|出演者|出演|Actor|Actress)\s*[：:]?(?:\s*</?[^>]+>)*\s*<a[^>]+href=[\"'](?:https://fc2cmadb\.com)?/actresses/\d+[\"'][^>]*>(.*?)</a>",
        r"(?:女优|女優|出演者|出演|Actor|Actress)\s*[：:]\s*([^<\r\n]+)",
    ]
    actress = first_match(html, patterns)
    if not actress:
        return None

    for separator in ["、", ",", "，", "/", "|", "&"]:
        if separator in actress:
            actress = actress.split(separator)[0]
            break
    return normalize_actress_name(actress)


def is_access_blocked(html: str) -> bool:
    """仅识别明确的 Cloudflare 挑战页，避免把普通 CF 资源误判为验证页。"""

    strong_markers = [
        'id="challenge-running"',
        "id='challenge-running'",
        'id="cf-challenge-running"',
        "id='cf-challenge-running'",
        "cf-browser-verification",
        "checking your browser before accessing",
        "enable javascript and cookies to continue",
        "performing security verification",
    ]
    lowered = html.lower()
    if any(marker in lowered for marker in strong_markers):
        return True

    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not title_match:
        return False
    title = strip_tags(title_match.group(1)).lower()
    return title in {
        "just a moment...",
        "just a moment…",
        "attention required! | cloudflare",
    }


def fetch_actress_name(session: requests.Session, number: str, timeout: int) -> FetchResult:
    """使用普通 HTTP 请求查询女优名，主要供 requests/hybrid 模式使用。"""

    if requests is None:
        return FetchResult(None, "缺少依赖 requests，请先运行：pip install -r requirements.txt", blocked=True)

    url = f"{TARGET_BASE_URL}/articles/{number}"
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        return FetchResult(None, f"网页请求失败：{exc}", blocked=True)

    if is_access_blocked(response.text):
        return FetchResult(None, f"访问受限或 Cookie 已失效，请刷新 {TARGET_DOMAIN} Cookie 后重试", blocked=True)

    actress = parse_first_actress(response.text)
    if actress:
        return FetchResult(actress, None)
    return FetchResult(None, "未解析到女优名，页面可能没有女优字段或结构变化")


def manual_lookup(number: str) -> FetchResult:
    """打开系统默认浏览器，让用户手动输入女优名。"""

    url = f"{TARGET_BASE_URL}/articles/{number}"
    print(f"\n需要手动确认 FC2-{number} 的女优名。")
    print(f"已打开网页：{url}")
    print("在浏览器里查看女优名后，在这里输入；直接回车跳过，输入 q 结束本轮。")
    webbrowser.open(url)
    value = input(f"FC2-{number} 女优名：").strip()
    if value.lower() in {"q", "quit", "exit"}:
        return FetchResult(None, "用户结束本轮手动输入", blocked=True)
    if not value:
        return FetchResult(None, "手动模式未输入女优名，已保留缓存等待重试")
    actress = normalize_actress_name(value)
    if not actress:
        return FetchResult(None, "输入的内容不是有效女优名，已保留缓存等待重试")
    return FetchResult(actress, None)


class BrowserLookup:
    """管理登录、浏览器接管和 FC2CMADB 页面解析。

    chrome_cdp 后端采用两阶段启动：
    - 第一阶段只启动普通 Chrome，用户手动完成登录和 Turnstile。
    - 用户确认登录成功后，第二阶段才通过 CDP 接管浏览器。

    这样可以避免登录验证码在 Playwright/CDP 已连接时出现跨域通信错误。
    """

    def __init__(self, config: Config):
        if config.browser_backend == "cloak" and launch_persistent_context is None:
            raise RuntimeError(
                "缺少依赖 cloakbrowser，请先运行：pip install -r requirements.txt"
            )
        if config.browser_backend == "chrome_cdp" and sync_playwright is None:
            raise RuntimeError(
                "缺少依赖 playwright，请先运行：pip install -r requirements.txt"
            )
        self.config = config
        self._context = None
        self._page = None
        self._playwright = None
        self._browser = None
        self._browser_process = None
        self._cdp_endpoint = None

    def __enter__(self) -> "BrowserLookup":
        return self

    def _ensure_started(self) -> None:
        """确保浏览器已启动并且脚本已经取得页面控制权。"""

        if self._context is not None:
            return
        if self.config.browser_backend == "cloak":
            self._context = launch_persistent_context(
                str(self.config.browser_profile_dir),
                headless=self.config.browser_headless,
                humanize=True,
            )
        else:
            self._launch_system_chrome_process()
            self._connect_system_chrome()
        self._attach_to_active_page()

    def _attach_to_active_page(self) -> None:
        """选择当前标签页，并挂载与 Turnstile 相关的诊断日志。"""

        if self._context is None:
            raise RuntimeError("浏览器上下文尚未建立")
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.on("requestfailed", self._log_turnstile_request_failure)
        self._page.on("console", self._log_turnstile_console_error)

    def _launch_system_chrome_process(self) -> None:
        """启动普通 Chrome 并等待调试端口可用，但暂不连接 Playwright。"""

        if self._browser_process is not None and self._browser_process.poll() is None:
            return
        executable = find_chrome_executable(self.config.chrome_executable)
        port = reserve_local_port()
        command = build_chrome_cdp_command(
            executable,
            self.config.browser_profile_dir,
            port,
        )
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._browser_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        self._cdp_endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"{self._cdp_endpoint}/json/version", timeout=1)
                response.raise_for_status()
                return
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(0.25)
        self._stop_system_chrome()
        raise RuntimeError(f"系统 Chrome 调试端口启动超时：{last_error}")

    def _connect_system_chrome(self) -> None:
        """在用户完成登录后，通过 CDP 连接已启动的普通 Chrome。"""

        if self._context is not None:
            return
        if not self._cdp_endpoint:
            raise RuntimeError("系统 Chrome 调试端口尚未启动")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(self._cdp_endpoint)
        if not self._browser.contexts:
            self._stop_system_chrome()
            raise RuntimeError("系统 Chrome 没有可用浏览器上下文")
        self._context = self._browser.contexts[0]

    def _stop_system_chrome(self) -> None:
        """关闭脚本创建的 CDP、Playwright 和 Chrome 进程。"""

        if self._browser is not None:
            try:
                self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        if self._browser_process is not None:
            if self._browser_process.poll() is None:
                self._browser_process.terminate()
                try:
                    self._browser_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._browser_process.kill()
            self._browser_process = None
        self._cdp_endpoint = None

    @staticmethod
    def _log_turnstile_request_failure(request) -> None:
        url = getattr(request, "url", "")
        if "challenges.cloudflare.com" not in url.lower():
            return
        if "brunhild.challenges.cloudflare.com" in url.lower():
            return
        failure = getattr(request, "failure", None)
        logging.warning("Turnstile 网络请求失败：%s；%s", url, failure or "未知错误")

    @staticmethod
    def _log_turnstile_console_error(message) -> None:
        text = getattr(message, "text", "")
        if not any(word in text.lower() for word in ("turnstile", "cloudflare", "challenge")):
            return
        lowered = text.lower()
        if (
            "private access token challenge" in lowered
            or "preloaded using link preload but not used" in lowered
        ):
            return
        logging.warning("Turnstile 控制台信息：%s", text)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.config.browser_backend == "chrome_cdp":
            self._stop_system_chrome()
        elif self._context is not None:
            self._context.close()
        self._context = None
        self._page = None

    def prepare_login(self) -> None:
        """打开登录入口，并保证 Chrome 模式在登录完成前不连接 CDP。"""

        if self.config.browser_backend == "chrome_cdp":
            if self.config.browser_headless:
                raise RuntimeError("登录准备必须使用有头模式，请将 browser_headless 设为 false")
            self._launch_system_chrome_process()
            print("\n系统 Chrome 已打开 FC2CMADB 首页。")
            print("脚本尚未连接浏览器，请先正常完成登录和 Cloudflare 验证。")
            input("确认已经登录成功后，按回车让脚本接管浏览器...")
            self._connect_system_chrome()
            self._attach_to_active_page()
            return

        self._ensure_started()
        if self._page is None:
            raise RuntimeError("CloakBrowser 页面启动失败")
        if self.config.browser_headless:
            raise RuntimeError("登录准备必须使用有头模式，请将 browser_headless 设为 false")

        self._page.goto(
            TARGET_BASE_URL,
            wait_until="domcontentloaded",
            timeout=self.config.browser_wait_seconds * 1000,
        )
        print("\nCloakBrowser 已打开 FC2CMADB 首页。")
        print("请先在浏览器右上角完成登录，确认登录成功后再回到这里。")
        while True:
            input("登录完成后按回车开始自动抓取...")
            state = self._turnstile_state()
            if state in {"absent", "solved"}:
                break
            if state == "missing":
                print("登录窗口已打开，但 Cloudflare Turnstile 控件没有加载。")
                print("脚本将刷新页面；请重新打开登录窗口并再次输入账号。")
                self._page.reload(
                    wait_until="domcontentloaded",
                    timeout=self.config.browser_wait_seconds * 1000,
                )
                continue
            print("检测到登录窗口中的 Cloudflare Turnstile 仍在验证。")
            print("请不要关闭浏览器；等待验证完成或切换网络后重试。")

    def _turnstile_state(self) -> str:
        """返回 Cloak 后端登录框的 Turnstile 状态。"""

        if self._page is None:
            return "absent"
        try:
            dialog = self._page.locator('div[role="dialog"]')
            widget = self._page.locator(
                'iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
            )
            if widget.count() == 0:
                if dialog.count() and dialog.first.is_visible():
                    return "missing"
                return "absent"

            responses = self._page.locator(
                'input[name="cf-turnstile-response"]'
            )
            for index in range(responses.count()):
                value = responses.nth(index).input_value(timeout=2000).strip()
                if value:
                    return "solved"
            return "pending"
        except PlaywrightError as exc:
            logging.warning("无法读取 Turnstile 状态，将继续等待人工确认：%s", exc)
            return "pending"

    def _turnstile_is_pending(self) -> bool:
        return self._turnstile_state() in {"missing", "pending"}

    def lookup(self, number: str) -> FetchResult:
        """导航到指定番号详情页，并读取页面中的女优名。"""

        try:
            self._ensure_started()
        except Exception as exc:
            return FetchResult(None, f"浏览器启动失败：{exc}", blocked=True)
        if self._page is None:
            return FetchResult(None, "浏览器查询器尚未启动", blocked=True)

        url = f"{TARGET_BASE_URL}/articles/{number}"
        try:
            wait_before_browser_lookup()
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.config.browser_wait_seconds * 1000)
            result = self._read_current_page()
            if result.actress or not result.blocked:
                return result

            if self.config.browser_headless:
                return result

            print("\n检测到 Cloudflare 验证或登录页。")
            print("请在打开的浏览器窗口中手动完成验证/登录，确认页面能看到女优名后回到这里按回车继续。")
            input("处理完成后按回车继续...")
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            return self._read_current_page()
        except PlaywrightTimeoutError as exc:
            return FetchResult(None, f"浏览器等待页面超时：{exc}", blocked=True)
        except PlaywrightError as exc:
            return FetchResult(None, f"浏览器查询失败：{exc}", blocked=True)

    def _read_current_page(self) -> FetchResult:
        """等待动态女优链接挂载后读取；超时再退回静态 HTML 解析。"""

        if self._page is None:
            return FetchResult(None, "浏览器页面不存在", blocked=True)

        # FC2CMADB 使用前端动态渲染。domcontentloaded 时表格可能已经出现，
        # 但女优链接尚未挂载，因此必须等待链接进入 DOM，不能立即 count()。
        locator = self._page.locator(
            'main table a[href*="/actresses/"], '
            'a.link.link-primary.link-hover.font-medium.mr-2[href*="/actresses/"]'
        ).first
        last_error = None
        try:
            locator.wait_for(
                state="attached",
                timeout=ACTRESS_LINK_WAIT_MILLISECONDS,
            )
            actress = normalize_actress_name(locator.inner_text(timeout=5000))
            if actress:
                logging.info("浏览器解析到女优名：%s", actress)
                return FetchResult(actress, None)
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as exc:
            last_error = exc

        html = self._page.content()
        if is_access_blocked(html):
            return FetchResult(None, "访问受限或需要登录/验证，已保留缓存等待重试", blocked=True)
        actress = parse_first_actress(html)
        if actress:
            return FetchResult(actress, None)
        if last_error is not None:
            return FetchResult(None, f"读取女优链接失败：{last_error}")
        return FetchResult(None, "未解析到女优名，已保留缓存等待重试")


def move_one_file(source: Path, target_dir: Path, overwrite_existing: bool) -> Path:
    """创建目标目录并移动单个文件；按配置决定是否覆盖同名文件。"""

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name

    if target.exists():
        if not overwrite_existing:
            raise FileExistsError(f"目标文件已存在：{target}")
        logging.warning("覆盖单个目标文件：%s", target)
        target.unlink()

    shutil.move(str(source), str(target))
    return target


def record_failure(failed: List[dict], item: dict, reason: str, moved_to: Optional[Path] = None) -> None:
    """追加带时间戳的失败记录，便于后续排查和重试。"""

    failed.append(
        {
            "number": item.get("number"),
            "path": item.get("path"),
            "filename": item.get("filename"),
            "reason": reason,
            "moved_to": str(moved_to) if moved_to else None,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
    )
    logging.warning("处理失败：FC2-%s，原因：%s", item.get("number"), reason)


def process_cache(
    config: Config,
    cache: Dict[str, dict],
    failed: List[dict],
    organized_dirs: List[str],
    actress_map: Dict[str, str],
    browser_lookup: Optional[BrowserLookup] = None,
) -> Dict[str, dict]:
    """逐条处理缓存，并返回本轮结束后仍需重试的项目。"""

    session = create_session(config) if config.lookup_mode in {"requests", "hybrid"} else None
    remaining = dict(cache)

    for cache_key, item in list(cache.items()):
        source = Path(item["path"])
        number = item["number"]

        if not source.exists():
            logging.info("源文件已不存在，清除缓存：%s", source)
            remaining.pop(cache_key, None)
            continue

        actress = actress_map.get(number)
        if actress:
            result = FetchResult(actress, None)
        elif config.lookup_mode == "manual":
            result = manual_lookup(number)
            if result.actress and config.save_browser_results_to_csv:
                actress_map[number] = result.actress
                append_actress_map(config.actress_map_csv, number, result.actress)
            elif result.blocked:
                record_failure(failed, item, result.error or "用户结束本轮手动输入，已保留缓存等待重试")
                logging.error("手动模式已结束本轮处理。")
                break
            else:
                record_failure(failed, item, result.error or "手动模式未找到女优名，已保留缓存等待重试")
                continue
        elif config.lookup_mode == "browser" and browser_lookup is not None:
            result = browser_lookup.lookup(number)
            if result.actress and config.save_browser_results_to_csv:
                actress_map[number] = result.actress
                append_actress_map(config.actress_map_csv, number, result.actress)
            elif result.blocked:
                record_failure(failed, item, result.error or "浏览器访问受限，已保留缓存等待重试")
                logging.error("浏览器查询受限，停止本轮处理。请在浏览器中完成验证/登录后重新运行。")
                break
            else:
                record_failure(failed, item, result.error or "浏览器未找到女优名，已保留缓存等待重试")
                continue
        elif config.lookup_mode in {"requests", "hybrid"} and session is not None:
            result = fetch_actress_name(session, number, config.request_timeout)
            if result.blocked and config.lookup_mode == "hybrid" and browser_lookup is not None:
                logging.info("普通请求受限，使用 CloakBrowser 重试：FC2-%s", number)
                result = browser_lookup.lookup(number)
                if result.actress and config.save_browser_results_to_csv:
                    actress_map[number] = result.actress
                    append_actress_map(config.actress_map_csv, number, result.actress)
            if result.blocked:
                record_failure(failed, item, result.error or "访问受限，已保留缓存等待重试")
                if config.lookup_mode == "hybrid":
                    logging.error("HTTP 请求和浏览器查询均受限，停止本轮处理。")
                else:
                    logging.error("检测到访问受限，停止本轮处理。请更新 Cookie 后重新运行。")
                break
        else:
            record_failure(failed, item, "女优映射表没有该番号，已保留缓存等待补充")
            continue

        if result.actress:
            target_dir = config.video_dir / result.actress
            try:
                moved_to = move_one_file(source, target_dir, config.overwrite_existing)
                if result.actress not in organized_dirs:
                    organized_dirs.append(result.actress)
                remaining.pop(cache_key, None)
                logging.info("移动完成：FC2-%s -> %s", number, moved_to)
            except Exception as exc:
                record_failure(failed, item, f"移动到女优文件夹失败：{exc}")
            continue

        uncategorized_dir = config.video_dir / config.uncategorized_folder
        try:
            moved_to = move_one_file(source, uncategorized_dir, config.overwrite_existing)
            remaining.pop(cache_key, None)
            record_failure(failed, item, result.error or "未找到女优名", moved_to)
        except Exception as exc:
            record_failure(failed, item, f"{result.error or '未找到女优名'}；移动到未分类失败：{exc}")

    return remaining


def main() -> int:
    """程序入口：加载状态、扫描文件、执行查询、持久化处理结果。"""

    setup_logging()
    try:
        config = load_config()
        actress_map = load_actress_map(config.actress_map_csv)
        organized_dirs = load_json_file(ORGANIZED_DIRS_PATH, [])
        if not isinstance(organized_dirs, list):
            organized_dirs = []

        cache = load_json_file(CACHE_PATH, {})
        if not isinstance(cache, dict):
            cache = {}

        failed = load_json_file(FAILED_PATH, [])
        if not isinstance(failed, list):
            failed = []

        logging.info("开始扫描视频目录：%s", config.video_dir)
        cache = build_cache(config, cache, organized_dirs)
        save_json_file(CACHE_PATH, cache)

        if not cache:
            logging.info("没有发现待处理的 FC2 视频")
            return 0

        logging.info("开始处理缓存，共 %s 个视频", len(cache))
        if config.lookup_mode in {"browser", "hybrid"}:
            with BrowserLookup(config) as browser_lookup:
                if config.lookup_mode == "browser":
                    browser_lookup.prepare_login()
                remaining = process_cache(config, cache, failed, organized_dirs, actress_map, browser_lookup)
        else:
            remaining = process_cache(config, cache, failed, organized_dirs, actress_map)
        save_json_file(CACHE_PATH, remaining)
        save_json_file(FAILED_PATH, failed)
        save_json_file(ORGANIZED_DIRS_PATH, organized_dirs)
        logging.info("处理结束。剩余缓存：%s，失败记录总数：%s", len(remaining), len(failed))
        return 0
    except Exception as exc:
        logging.exception("脚本执行失败：%s", exc)
        return 1


if __name__ == "__main__":
    exit_code = main()
    if sys.stdin.isatty():
        input("按回车退出...")
    raise SystemExit(exit_code)
