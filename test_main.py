import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock, patch

import main


class MainHelpersTest(unittest.TestCase):
    def test_extract_fc2_number_supports_common_formats(self):
        cases = {
            "FC2-1234567.mp4": "1234567",
            "fc2 ppv 7654321.mkv": "7654321",
            "abc-fc2_246810-test.mp4": "246810",
            "FC2PPV_13579.avi": "13579",
            "no-number.mp4": None,
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(main.extract_fc2_number(filename), expected)

    def test_sanitize_folder_name_removes_windows_invalid_chars(self):
        self.assertEqual(main.sanitize_folder_name(' A/B:C*D?E"F<G>H| '), "A_B_C_D_E_F_G_H_")

    def test_normalize_actress_name_rejects_field_labels(self):
        for value in ["女优", "女優", "出演者", "Actor", "Actress", "", "未知女优"]:
            with self.subTest(value=value):
                self.assertIsNone(main.normalize_actress_name(value))

    def test_parse_first_actress_from_actress_link(self):
        html = '<a href="/actresses/123">女优A</a><a href="/actresses/456">女优B</a>'
        self.assertEqual(main.parse_first_actress(html), "女优A")

    def test_parse_first_actress_from_absolute_fc2cmadb_link(self):
        html = '<a href="https://fc2cmadb.com/actresses/123">女优A</a>'
        self.assertEqual(main.parse_first_actress(html), "女优A")

    def test_parse_first_actress_from_fc2cmadb_detail_row(self):
        html = """
        <tr>
          <th>女優：</th>
          <td>
            <a class="link link-primary link-hover font-medium mr-2"
               href="https://fc2cmadb.com/actresses/4225">えりか</a>
          </td>
        </tr>
        """
        self.assertEqual(main.parse_first_actress(html), "えりか")

    def test_parse_first_actress_from_label_text(self):
        html = '<div>ID: 4566405</div><div>女优：小春</div><div>马赛克：截图</div>'
        self.assertEqual(main.parse_first_actress(html), "小春")

    def test_parse_first_actress_from_japanese_label_link(self):
        html = '<span>女優：</span><a href="/actresses/123">小春</a>'
        self.assertEqual(main.parse_first_actress(html), "小春")

    def test_parse_first_actress_ignores_search_option(self):
        html = '''
        <select>
          <option value="writer">販売者</option>
        </select>
        <div>
          女優:
          <span class="text-white ml-2">
            <a class="mr-2 font-medium text-blue-600" href="/actresses/10009">小春</a>
          </span>
        </div>
        '''
        self.assertEqual(main.parse_first_actress(html), "小春")

    def test_parse_first_actress_returns_none_for_login_page(self):
        html = '<form method="POST" action="https://fc2cmadb.com/login"></form>'
        self.assertIsNone(main.parse_first_actress(html))

    def test_login_link_alone_is_not_treated_as_access_blocked(self):
        html = '<a href="https://fc2cmadb.com/login">登录</a>'
        self.assertFalse(main.is_access_blocked(html))

    def test_parse_first_actress_does_not_return_field_label(self):
        html = "<table><tr><th>女優：</th><td></td></tr></table>"
        self.assertIsNone(main.parse_first_actress(html))

    def test_load_actress_map_skips_invalid_field_label(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "actress_map.csv"
            path.write_text(
                "number,actress\n1234567,女優\n7654321,えりか\n",
                encoding="utf-8-sig",
            )

            result = main.load_actress_map(path)

            self.assertEqual(result, {"7654321": "えりか"})

    def test_move_one_file_creates_named_target_directory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "FC2-1234567.mp4"
            source.write_bytes(b"video")
            target_dir = root / "小春"

            moved_to = main.move_one_file(source, target_dir, overwrite_existing=False)

            self.assertEqual(moved_to, target_dir / source.name)
            self.assertTrue(target_dir.is_dir())
            self.assertTrue(moved_to.is_file())
            self.assertFalse(source.exists())

    @patch("main.create_session")
    @patch("main.fetch_actress_name")
    @patch("main.move_one_file")
    def test_process_cache_removes_successful_item(self, move_one_file, fetch_actress_name, create_session):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=True,
            lookup_mode="requests",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []
        create_session.return_value = Mock()
        fetch_actress_name.return_value = main.FetchResult("女优A", None)
        move_one_file.return_value = source.parent / "女优A" / source.name

        remaining = main.process_cache(config, cache, failed, organized_dirs, {})

        self.assertEqual(remaining, {})
        self.assertEqual(failed, [])
        self.assertEqual(organized_dirs, ["女优A"])

    @patch("main.create_session")
    @patch("main.fetch_actress_name")
    @patch("main.move_one_file")
    def test_process_cache_keeps_item_when_access_blocked(self, move_one_file, fetch_actress_name, create_session):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=True,
            lookup_mode="requests",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []
        create_session.return_value = Mock()
        fetch_actress_name.return_value = main.FetchResult(
            None,
            "访问受限或 Cookie 已失效，请刷新 fc2cmadb.com Cookie 后重试",
            blocked=True,
        )

        remaining = main.process_cache(config, cache, failed, organized_dirs, {})

        self.assertEqual(remaining, cache)
        self.assertEqual(len(failed), 1)
        move_one_file.assert_not_called()

    @patch("main.create_session")
    @patch("main.fetch_actress_name")
    @patch("main.move_one_file")
    def test_hybrid_uses_requests_without_starting_browser(
        self, move_one_file, fetch_actress_name, create_session
    ):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=True,
            lookup_mode="hybrid",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=True,
            browser_wait_seconds=1,
            save_browser_results_to_csv=False,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        browser_lookup = Mock()
        fetch_actress_name.return_value = main.FetchResult("女优A", None)
        move_one_file.return_value = source.parent / "女优A" / source.name

        remaining = main.process_cache(config, cache, [], [], {}, browser_lookup)

        self.assertEqual(remaining, {})
        browser_lookup.lookup.assert_not_called()

    @patch("main.create_session")
    @patch("main.fetch_actress_name")
    @patch("main.move_one_file")
    def test_hybrid_falls_back_to_browser_when_requests_is_blocked(
        self, move_one_file, fetch_actress_name, create_session
    ):
        source = Path(__file__).resolve()
        with TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "actress_map.csv"
            config = main.Config(
                video_dir=source.parent,
                cookie="",
                video_extensions=[".mp4"],
                uncategorized_folder="未分类",
                overwrite_existing=True,
                request_timeout=1,
                actress_map_csv=map_path,
                web_lookup_enabled=True,
                lookup_mode="hybrid",
                browser_profile_dir=Path(tmp) / "browser_profile",
                browser_headless=True,
                browser_wait_seconds=1,
                save_browser_results_to_csv=True,
            )
            cache = {
                "1234567:path": {
                    "number": "1234567",
                    "path": str(source),
                    "filename": source.name,
                }
            }
            failed = []
            organized_dirs = []
            browser_lookup = Mock()
            fetch_actress_name.return_value = main.FetchResult(
                None, "HTTP 403", blocked=True
            )
            browser_lookup.lookup.return_value = main.FetchResult("小春", None)
            move_one_file.return_value = source.parent / "小春" / source.name

            remaining = main.process_cache(
                config, cache, failed, organized_dirs, {}, browser_lookup
            )

            self.assertEqual(remaining, {})
            self.assertEqual(failed, [])
            browser_lookup.lookup.assert_called_once_with("1234567")
            self.assertIn("1234567,小春", map_path.read_text(encoding="utf-8-sig"))

    @patch("main.create_session")
    @patch("main.fetch_actress_name")
    @patch("main.move_one_file")
    def test_hybrid_keeps_item_when_browser_fallback_is_blocked(
        self, move_one_file, fetch_actress_name, create_session
    ):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=True,
            lookup_mode="hybrid",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=True,
            browser_wait_seconds=1,
            save_browser_results_to_csv=False,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        browser_lookup = Mock()
        fetch_actress_name.return_value = main.FetchResult(
            None, "HTTP 403", blocked=True
        )
        browser_lookup.lookup.return_value = main.FetchResult(
            None, "浏览器仍被拦截", blocked=True
        )

        remaining = main.process_cache(
            config, cache, failed, [], {}, browser_lookup
        )

        self.assertEqual(remaining, cache)
        self.assertEqual(len(failed), 1)
        move_one_file.assert_not_called()

    @patch("main.create_session")
    @patch("main.fetch_actress_name")
    @patch("main.move_one_file")
    def test_process_cache_uses_actress_map_without_web_lookup(self, move_one_file, fetch_actress_name, create_session):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=False,
            lookup_mode="csv_only",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []
        move_one_file.return_value = source.parent / "小春" / source.name

        remaining = main.process_cache(config, cache, failed, organized_dirs, {"1234567": "小春"})

        self.assertEqual(remaining, {})
        self.assertEqual(failed, [])
        self.assertEqual(organized_dirs, ["小春"])
        create_session.assert_not_called()
        fetch_actress_name.assert_not_called()

    @patch("main.create_session")
    @patch("main.move_one_file")
    def test_process_cache_keeps_item_when_map_missing_and_web_disabled(self, move_one_file, create_session):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=False,
            lookup_mode="csv_only",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []

        remaining = main.process_cache(config, cache, failed, organized_dirs, {})

        self.assertEqual(remaining, cache)
        self.assertEqual(len(failed), 1)
        create_session.assert_not_called()
        move_one_file.assert_not_called()

    @patch("main.create_session")
    @patch("main.webbrowser.open")
    @patch("builtins.input", return_value="小春")
    @patch("main.move_one_file")
    def test_process_cache_uses_manual_lookup_and_appends_map(
        self,
        move_one_file,
        input_mock,
        webbrowser_open,
        create_session,
    ):
        source = Path(__file__).resolve()
        with TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "actress_map.csv"
            config = main.Config(
                video_dir=source.parent,
                cookie="",
                video_extensions=[".mp4"],
                uncategorized_folder="未分类",
                overwrite_existing=True,
                request_timeout=1,
                actress_map_csv=map_path,
                web_lookup_enabled=False,
                lookup_mode="manual",
                browser_profile_dir=Path(tmp) / "browser_profile",
                browser_headless=False,
                browser_wait_seconds=1,
                save_browser_results_to_csv=True,
            )
            cache = {
                "1234567:path": {
                    "number": "1234567",
                    "path": str(source),
                    "filename": source.name,
                }
            }
            failed = []
            organized_dirs = []
            move_one_file.return_value = source.parent / "小春" / source.name

            remaining = main.process_cache(config, cache, failed, organized_dirs, {})

            self.assertEqual(remaining, {})
            self.assertEqual(failed, [])
            self.assertEqual(organized_dirs, ["小春"])
            self.assertIn("1234567,小春", map_path.read_text(encoding="utf-8-sig"))
            webbrowser_open.assert_called_once_with("https://fc2cmadb.com/articles/1234567")
            create_session.assert_not_called()

    @patch("main.create_session")
    @patch("main.webbrowser.open")
    @patch("builtins.input", return_value="q")
    @patch("main.move_one_file")
    def test_process_cache_stops_when_manual_lookup_quits(
        self,
        move_one_file,
        input_mock,
        webbrowser_open,
        create_session,
    ):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=False,
            lookup_mode="manual",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []

        remaining = main.process_cache(config, cache, failed, organized_dirs, {})

        self.assertEqual(remaining, cache)
        self.assertEqual(len(failed), 1)
        webbrowser_open.assert_called_once_with("https://fc2cmadb.com/articles/1234567")
        create_session.assert_not_called()
        move_one_file.assert_not_called()

    @patch("main.create_session")
    @patch("main.move_one_file")
    def test_process_cache_uses_browser_lookup_and_appends_map(self, move_one_file, create_session):
        source = Path(__file__).resolve()
        with TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "actress_map.csv"
            config = main.Config(
                video_dir=source.parent,
                cookie="",
                video_extensions=[".mp4"],
                uncategorized_folder="未分类",
                overwrite_existing=True,
                request_timeout=1,
                actress_map_csv=map_path,
                web_lookup_enabled=False,
                lookup_mode="browser",
                browser_profile_dir=Path(tmp) / "browser_profile",
                browser_headless=False,
                browser_wait_seconds=1,
                save_browser_results_to_csv=True,
            )
            cache = {
                "1234567:path": {
                    "number": "1234567",
                    "path": str(source),
                    "filename": source.name,
                }
            }
            failed = []
            organized_dirs = []
            browser_lookup = Mock()
            browser_lookup.lookup.return_value = main.FetchResult("小春", None)
            move_one_file.return_value = source.parent / "小春" / source.name

            remaining = main.process_cache(config, cache, failed, organized_dirs, {}, browser_lookup)

            self.assertEqual(remaining, {})
            self.assertEqual(failed, [])
            self.assertEqual(organized_dirs, ["小春"])
            self.assertIn("1234567,小春", map_path.read_text(encoding="utf-8-sig"))
            create_session.assert_not_called()

    @patch("main.create_session")
    @patch("main.move_one_file")
    def test_process_cache_keeps_item_when_browser_blocked(self, move_one_file, create_session):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=False,
            lookup_mode="browser",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []
        browser_lookup = Mock()
        browser_lookup.lookup.return_value = main.FetchResult(
            None,
            "访问受限或需要登录/验证，已保留缓存等待重试",
            blocked=True,
        )

        remaining = main.process_cache(config, cache, failed, organized_dirs, {}, browser_lookup)

        self.assertEqual(remaining, cache)
        self.assertEqual(len(failed), 1)
        create_session.assert_not_called()
        move_one_file.assert_not_called()

    @patch("main.create_session")
    @patch("main.move_one_file")
    def test_process_cache_keeps_item_when_browser_finds_no_actress(self, move_one_file, create_session):
        source = Path(__file__).resolve()
        config = main.Config(
            video_dir=source.parent,
            cookie="",
            video_extensions=[".mp4"],
            uncategorized_folder="未分类",
            overwrite_existing=True,
            request_timeout=1,
            actress_map_csv=source.parent / "actress_map.csv",
            web_lookup_enabled=False,
            lookup_mode="browser",
            browser_profile_dir=source.parent / "browser_profile",
            browser_headless=False,
            browser_wait_seconds=1,
            save_browser_results_to_csv=True,
        )
        cache = {
            "1234567:path": {
                "number": "1234567",
                "path": str(source),
                "filename": source.name,
            }
        }
        failed = []
        organized_dirs = []
        browser_lookup = Mock()
        browser_lookup.lookup.return_value = main.FetchResult(None, "未解析到女优名")

        remaining = main.process_cache(config, cache, failed, organized_dirs, {}, browser_lookup)

        self.assertEqual(remaining, cache)
        self.assertEqual(len(failed), 1)
        create_session.assert_not_called()
        move_one_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
