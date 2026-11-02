"""Optional statement downloads, with native starts and terminal launch mocked."""
from unittest.mock import Mock, patch

import test_regressions as fixtures
from test_regressions import OfflineTest, qoj_page, soup
from vp_helper import cli
from vp_helper.models import Contest, Rules, VPError
from vp_helper.qoj import QOJ
from vp_helper.storage import read_json, write_json


class PreferenceTests(OfflineTest):
    def test_unset_preference_defaults_to_no_and_only_y_enables_download(self):
        for answer in ("", "n", "N", "yes", "anything", "y", "Y", " y "):
            with self.subTest(answer=answer), patch("builtins.input", return_value=answer) as prompt:
                config = {}
                self.assertEqual(cli.pdf_download_enabled(config), answer.strip().lower() == "y")
                prompt.assert_called_once_with("Download problem PDFs? (y/N): ")
                self.assertEqual(config, {})

    def test_explicit_boolean_never_prompts(self):
        for value in (False, True):
            with self.subTest(value=value), patch("builtins.input", side_effect=AssertionError("Must not prompt")):
                self.assertIs(cli.pdf_download_enabled({"download_pdfs": value}), value)

    def test_invalid_preference_is_not_interpreted_as_truthy(self):
        for value in (None, 0, 1, "false", "true", []):
            with self.subTest(value=value), patch("builtins.input", side_effect=AssertionError("Must not prompt")):
                with self.assertRaisesRegex(VPError, "download_pdfs must be true or false"):
                    cli.pdf_download_enabled({"download_pdfs": value})

    def test_end_of_input_skips_download(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(cli.pdf_download_enabled({}))


class PreparationTests(OfflineTest):
    def api(self, platform):
        path = "contest" if platform in ("qoj", "cf") else "gym"
        origin = "qoj.ac" if platform == "qoj" else "codeforces.com"
        url = f"https://{origin}/{path}/2565"
        c = Contest(platform, "2565", url, "Example contest", "Example contest",
                    {"A": "101", "B": "102"}, Rules(18000),
                    {"A.pdf": url + "/statements/A.pdf", "B.pdf": url + "/statements/B.pdf"})
        script = self.root / "submit"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o700)
        api = Mock(handle="example", account={"cookie": "fake", "submit_script": str(script),
                                             "submit_config_format": "contest.json"})
        api.web.session.headers = {"User-Agent": "offline-test"}
        api.prepare.return_value = c
        api.submit_configuration.return_value = {"name": c.name, "base_url": c.url, "handle": "example",
            "problems": "AB", "offset": 101, "problem_ids": c.problems, "cookie": "fake", "user_agent": "offline-test"}
        api.start.return_value = 1000
        api.web.pdf.side_effect = lambda url, target: target.write_bytes(b"%PDF-synthetic-test")
        return api, c

    def test_shared_flow_respects_download_choices_and_still_launches_monitor(self):
        cases = [({}, "", False), ({}, "n", False), ({}, "y", True),
                 ({"download_pdfs": False}, "y", False), ({"download_pdfs": True}, "n", True)]
        for platform in ("qoj", "cf", "gym"):
            for index, (config, answer, expected) in enumerate(cases):
                with self.subTest(platform=platform, config=config, answer=answer):
                    api, c = self.api(platform)
                    root = self.root / platform / str(index)
                    directory = root / "Desktop" / c.directory_name
                    directory.mkdir(parents=True)
                    source = directory / "A.cpp"
                    source.write_text("// existing code\n")
                    old_pdf = directory / "Saved-notes.pdf"
                    old_pdf.write_bytes(b"old PDF notes")
                    (directory / "A.pdf").write_bytes(b"existing A.pdf")
                    with patch("vp_helper.cli.load_account", return_value=config), \
                         patch("vp_helper.cli.adapter", return_value=api), \
                         patch("vp_helper.cli.state_directory", return_value=root / "state"), \
                         patch("vp_helper.cli.desktop", return_value=root / "Desktop"), \
                         patch("vp_helper.cli.shutil.which", return_value="tmux"), \
                         patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=answer) as prompt, \
                         patch("vp_helper.cli.tmux_exists", return_value=False), \
                         patch("vp_helper.cli.launch") as launch, patch("vp_helper.cli.console"):
                        cli.start(c.url)
                    api.prepare.assert_called_once_with(download_pdfs=expected)
                    api.start.assert_called_once_with(c)
                    launch.assert_called_once()
                    state_path, state = launch.call_args.args
                    self.assertEqual(state["start_time"], 1000)
                    self.assertEqual(read_json(state_path)["start_time"], 1000)
                    self.assertEqual(read_json(directory / "contest.json")["vp_context"]["start_time"], 1000)
                    self.assertEqual(source.read_text(), "// existing code\n")
                    self.assertEqual(old_pdf.read_bytes(), b"old PDF notes")
                    self.assertEqual((directory / "A.pdf").read_bytes(), b"existing A.pdf")
                    if expected:
                        self.assertEqual(api.web.pdf.call_count, 1)
                        self.assertEqual((directory / "B.pdf").read_bytes(), b"%PDF-synthetic-test")
                    else:
                        api.web.pdf.assert_not_called()
                        self.assertEqual((directory / "A.pdf").read_bytes(), b"existing A.pdf")
                        self.assertFalse((directory / "B.pdf").exists())
                    if "download_pdfs" in config:
                        prompt.assert_not_called()
                    else:
                        prompt.assert_called_once_with("Download problem PDFs? (y/N): ")

    def test_existing_vp_never_prompts_or_downloads_even_after_reboot(self):
        for session_exists, running in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(session_exists=session_exists, running=running):
                api, c = self.api("qoj")
                root = self.root / f"resume-{session_exists}-{running}"
                directory = root / "Desktop" / c.directory_name
                directory.mkdir(parents=True)
                (directory / "A.pdf").write_bytes(b"existing PDF")
                state_path = root / "state/vp-qoj-2565-1000.json"
                state = {"contest": c.to_dict(), "url": c.url, "start_time": 1000, "handle": "example",
                         "directory": str(directory), "tmux_session": "vp-qoj-2565-1000",
                         "status": "running" if running else "complete", "submissions": []}
                write_json(state_path, state)
                write_json(root / "state/current.json", {"state_path": str(state_path)})
                config = {"download_pdfs": True} if session_exists or running else {}
                with patch("vp_helper.cli.load_account", return_value=config), \
                     patch("vp_helper.cli.adapter", return_value=api), \
                     patch("vp_helper.cli.state_directory", return_value=root / "state"), \
                     patch("vp_helper.cli.desktop", return_value=root / "Desktop"), \
                     patch("vp_helper.cli.shutil.which", return_value="tmux"), \
                     patch("sys.stdin.isatty", return_value=True), patch("vp_helper.cli.time.time", return_value=1001 if running else 20000), \
                     patch("vp_helper.cli.tmux_exists", return_value=session_exists), \
                     patch("builtins.input", side_effect=AssertionError("Must not prompt on resume")), patch("vp_helper.cli.console"):
                    path, saved = cli.prepare_start(c.url)
                self.assertEqual(path, state_path)
                self.assertEqual(saved["start_time"], 1000)
                self.assertEqual((directory / "A.pdf").read_bytes(), b"existing PDF")
                api.web.pdf.assert_not_called()
                if session_exists or running:
                    api.prepare.assert_not_called()
                    api.start.assert_not_called()
                    self.assertEqual(read_json(state_path), state)
                else:
                    # The expired/no-tmux path still checks the remote record,
                    # but it does not repeat the PDF question for this VP.
                    api.prepare.assert_called_once_with(download_pdfs=False)


class AdapterDownloadTests(OfflineTest):
    def test_qoj_can_prepare_without_pdf_links_or_problem_page_requests(self):
        with patch("vp_helper.qoj.WebSession"):
            api = QOJ("https://qoj.ac/contest/2565", {})
        page = qoj_page("Practice")
        page.select_one('a[href*="download.php"]').decompose()
        api.web.soup.side_effect = [page, soup('<tr><td><a href="/contest/2565">Contest</a></td><td>5 hours</td></tr>')]
        c = api.prepare(download_pdfs=False)
        self.assertEqual(c.pdfs, {})
        self.assertEqual(c.problems, {"A": "101", "B": "205"})
        self.assertEqual(api.web.soup.call_count, 2)
        api.web.pdf.assert_not_called()
        api.start.assert_not_called()

    def test_cf_and_gym_skip_attachment_and_problem_requests(self):
        for platform in ("cf", "gym"):
            with self.subTest(platform=platform):
                # Reuse the existing synthetic ICPC standings fixture.
                api = fixtures.AdapterTests.gym(self, platform)
                api.web.soup.return_value = soup("<h1>Practice</h1><p>No PDF attachments</p>")
                c = api.prepare(download_pdfs=False)
                self.assertEqual(c.pdfs, {})
                self.assertEqual(c.rules.duration, 18000)
                api.web.soup.assert_called_once_with(api.url, params={"locale": "en"})
                api.start.assert_not_called()

    def test_skipping_download_does_not_bypass_icpc_checks(self):
        with patch("vp_helper.qoj.WebSession"):
            api = QOJ("https://qoj.ac/contest/2565", {})
        api.web.soup.return_value = qoj_page("Practice", "IOI")
        with self.assertRaisesRegex(VPError, "does not use ICPC scoring"):
            api.prepare(download_pdfs=False)
        for platform in ("cf", "gym"):
            with self.subTest(platform=platform), self.assertRaisesRegex(VPError, "does not use ICPC scoring"):
                fixtures.AdapterTests.gym(self, platform, ruleset="CF").prepare(download_pdfs=False)
