from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import sys
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
except ImportError:  # pragma: no cover - shown to users in the console.
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError

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

DEFAULT_VIDEO_EXTENSIONS = [".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts"]
FC2_PATTERN = re.compile(r"(?i)\bfc2(?:\s*[-_ ]?\s*ppv)?\s*[-_ ]?\s*(\d{4,10})\b")
INVALID_WINDOWS_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


@dataclass
class Config:
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


@dataclass
class FetchResult:
    actress: Optional[str]
    error: Optional[str]
    blocked: bool = False


def setup_logging() -> None:
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

    lookup_mode = str(raw.get("lookup_mode", "")).strip().lower()
    if not lookup_mode:
        lookup_mode = "requests" if bool(raw.get("web_lookup_enabled", False)) else "manual"
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
    )


def load_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.warning("JSON 文件损坏，将使用默认值：%s", path)
        return default


def save_json_file(path: Path, data) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def load_actress_map(path: Path) -> Dict[str, str]:
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
                    actress = sanitize_folder_name(row.get(actress_field, ""))
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
            actress = sanitize_folder_name(row[1])
            if number and actress and number.lower() not in {"number", "fc2", "id", "番号"}:
                actress_map[number] = actress

    logging.info("已读取女优映射表：%s 条", len(actress_map))
    return actress_map


def append_actress_map(path: Path, number: str, actress: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if needs_header:
            writer.writerow(["number", "actress"])
        writer.writerow([number, actress])
    logging.info("已追加女优映射：FC2-%s -> %s", number, actress)


def normalize_number(value: str) -> Optional[str]:
    text = str(value).strip()
    if not text:
        return None
    match = FC2_PATTERN.search(text)
    if match:
        return match.group(1)
    match = re.search(r"\d{4,10}", text)
    return match.group(0) if match else None


def extract_fc2_number(filename: str) -> Optional[str]:
    match = FC2_PATTERN.search(filename)
    return match.group(1) if match else None


def sanitize_folder_name(name: str) -> str:
    cleaned = INVALID_WINDOWS_NAME_CHARS.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "未知女优"


def path_is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def should_skip_path(path: Path, video_root: Path, skipped_dirs: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for skipped in skipped_dirs:
        if path_is_inside(resolved, skipped):
            return True
    return not path_is_inside(resolved, video_root)


def iter_video_files(config: Config, organized_dirs: Iterable[str]) -> Iterable[Path]:
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
    if not cookie_text:
        logging.warning("config.json 未填写 cookie，详情页可能无法访问")
        return

    cookie = SimpleCookie()
    cookie.load(cookie_text)
    for key, morsel in cookie.items():
        session.cookies.set(key, morsel.value, domain="fc2ppvdb.com")


def create_session(config: Config) -> requests.Session:
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
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def first_match(html: str, patterns: Iterable[str]) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            text = strip_tags(match.group(1))
            if text:
                return text
    return None


def parse_first_actress(html: str) -> Optional[str]:
    if re.search(r'<form[^>]+action=["\']https://fc2ppvdb\.com/login["\']', html, re.IGNORECASE):
        return None

    patterns = [
        r'<a[^>]+href=["\'](?:https://fc2ppvdb\.com)?/actresses/\d+["\'][^>]*>(.*?)</a>',
        r"(?:女优|女優|出演者|出演|Actor|Actress)\s*[：:]?(?:\s*</?[^>]+>)*\s*<a[^>]+href=[\"'](?:https://fc2ppvdb\.com)?/actresses/\d+[\"'][^>]*>(.*?)</a>",
        r"(?:女优|女優|出演者|出演|Actor|Actress)\s*[：:]\s*([^<\r\n]+)",
    ]
    actress = first_match(html, patterns)
    if not actress:
        return None

    for separator in ["、", ",", "，", "/", "|", "&"]:
        if separator in actress:
            actress = actress.split(separator)[0]
            break
    return sanitize_folder_name(actress)


def is_access_blocked(html: str) -> bool:
    markers = [
        "cf-browser-verification",
        "cf-challenge",
        "cf-turnstile",
        "cloudflare",
        "Just a moment",
        "Checking your browser",
        "https://fc2ppvdb.com/login",
    ]
    lowered = html.lower()
    return any(marker.lower() in lowered for marker in markers)


def fetch_actress_name(session: requests.Session, number: str, timeout: int) -> FetchResult:
    if requests is None:
        return FetchResult(None, "缺少依赖 requests，请先运行：pip install -r requirements.txt", blocked=True)

    url = f"https://fc2ppvdb.com/articles/{number}"
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        return FetchResult(None, f"网页请求失败：{exc}", blocked=True)

    if is_access_blocked(response.text):
        return FetchResult(None, "访问受限或 Cookie 已失效，请刷新 fc2ppvdb Cookie 后重试", blocked=True)

    actress = parse_first_actress(response.text)
    if actress:
        return FetchResult(actress, None)
    return FetchResult(None, "未解析到女优名，页面可能没有女优字段或结构变化")


def manual_lookup(number: str) -> FetchResult:
    url = f"https://fc2ppvdb.com/articles/{number}"
    print(f"\n需要手动确认 FC2-{number} 的女优名。")
    print(f"已打开网页：{url}")
    print("在浏览器里查看女优名后，在这里输入；直接回车跳过，输入 q 结束本轮。")
    webbrowser.open(url)
    value = input(f"FC2-{number} 女优名：").strip()
    if value.lower() in {"q", "quit", "exit"}:
        return FetchResult(None, "用户结束本轮手动输入", blocked=True)
    if not value:
        return FetchResult(None, "手动模式未输入女优名，已保留缓存等待重试")
    return FetchResult(sanitize_folder_name(value), None)


class BrowserLookup:
    def __init__(self, config: Config):
        if launch_persistent_context is None:
            raise RuntimeError(
                "缺少依赖 cloakbrowser，请先运行：pip install -r requirements.txt"
            )
        self.config = config
        self._context = None
        self._page = None

    def __enter__(self) -> "BrowserLookup":
        return self

    def _ensure_started(self) -> None:
        if self._context is not None:
            return
        self._context = launch_persistent_context(
            str(self.config.browser_profile_dir),
            headless=self.config.browser_headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()

    def lookup(self, number: str) -> FetchResult:
        try:
            self._ensure_started()
        except Exception as exc:
            return FetchResult(None, f"CloakBrowser 启动失败：{exc}", blocked=True)
        if self._page is None:
            return FetchResult(None, "浏览器查询器尚未启动", blocked=True)

        url = f"https://fc2ppvdb.com/articles/{number}"
        try:
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
        if self._page is None:
            return FetchResult(None, "浏览器页面不存在", blocked=True)

        locator = self._page.locator(
            'a[href^="/actresses/"], a[href*="fc2ppvdb.com/actresses/"]'
        ).first
        try:
            locator.wait_for(timeout=5000)
            actress = sanitize_folder_name(locator.inner_text(timeout=5000))
            if actress:
                return FetchResult(actress, None)
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as exc:
            return FetchResult(None, f"读取女优链接失败：{exc}")

        html = self._page.content()
        if is_access_blocked(html):
            return FetchResult(None, "访问受限或需要登录/验证，已保留缓存等待重试", blocked=True)
        actress = parse_first_actress(html)
        if actress:
            return FetchResult(actress, None)
        return FetchResult(None, "未解析到女优名，已保留缓存等待重试")


def move_one_file(source: Path, target_dir: Path, overwrite_existing: bool) -> Path:
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
