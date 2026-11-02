"""Offline regressions: synthetic pages only; native starts and POSTs are forbidden."""
from concurrent.futures import Future
from contextlib import ExitStack
from pathlib import Path
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from vp_helper import __version__, cli
from vp_helper.browser_login import firefox_profiles
from vp_helper.gym import Gym
from vp_helper.models import Contest, Medals, Reference, Rules, Submission, VPError
from vp_helper.originals import OriginalReplay, archive_path
from vp_helper.parsing import ccpc_reveal_count, directory_name
from vp_helper.qoj import QOJ
from vp_helper.scoring import prize_of, rank_label, revealed_problems, score_submissions, statistics
from vp_helper.storage import read_history, read_json, write_json


def soup(html):
    return BeautifulSoup(html, "html.parser")


def qoj_page(title, ruleset="ICPC"):
    return soup(f'''<h1>{title}</h1><p>Ruleset: {ruleset}</p>
        <p>Penalty: 20 minutes</p><p>Scoreboard will be frozen after 04:00:00</p>
        <table><tr><td>A</td><td><a href="/contest/2565/problem/101">A</a></td></tr>
        <tr><td>B</td><td><a href="/contest/2565/problem/205">B</a></td></tr></table>
        <a href="/download.php?id=example">Statements en</a>''')


def contest(name="QOJ 2565 Winter Practice"):
    return Contest("qoj", "2565", "https://qoj.ac/contest/2565", name, name,
                   {"A": "101", "B": "205"}, Rules(18000))


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict("os.environ", {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_STATE_HOME": str(self.root / "state"), "SUDO_USER": ""}))
        self.stack.enter_context(patch("pathlib.Path.home", return_value=self.root))
        for target in ("socket.create_connection", "socket.socket.connect",
                       "requests.sessions.Session.request", "curl_cffi.requests.Session.request",
                       "vp_helper.http.WebSession.post", "vp_helper.qoj.QOJ.start",
                       "vp_helper.gym.Gym.start"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Forbidden in offline tests: " + target)))


class NamingTests(OfflineTest):
    def test_standard_titles_keep_existing_directories(self):
        for title, expected in (
            ("2024 ICPC Asia Nanjing Regional Contest", "2024 ICPC Nanjing Reg"),
            ("2025 CCPC Zhengzhou Invitational Contest", "2025 CCPC Zhengzhou Inv"),
            ("2025 CCPC Henan Provincial Contest", "2025 CCPC Henan Pro"),
            ("2026 CCPC Changchun Regional Contest", "2026 CCPC Changchun Reg"),
            ("中国大学生程序设计竞赛（2026 CCPC Changchun Regional Contest）", "2026 CCPC Changchun Reg"),
        ):
            with self.subTest(title=title):
                self.assertEqual(directory_name([title], fallback="QOJ 2565"), expected)

    def test_alternate_standard_title_still_takes_precedence(self):
        self.assertEqual(directory_name(["Practice", "2024 ICPC Nanjing Regional"], fallback="QOJ 2565"),
                         "2024 ICPC Nanjing Reg")

    def test_nonstandard_titles_use_platform_and_id(self):
        for title in ("Winter Practice", "2025 ICPC Practice", "训练赛", "Reg/Inv/Pro helper"):
            with self.subTest(title=title):
                name = directory_name([title], fallback="QOJ 2565")
                self.assertTrue(name.startswith("QOJ 2565 "))
                self.assertIsNone(archive_path(contest(name)))
                self.assertIsNone(ccpc_reveal_count(name))
                self.assertNotEqual(name, directory_name([title], fallback="QOJ 4616"))

    def test_fallback_is_one_safe_bounded_path_component(self):
        for title in ("../../bad\\name\x00\n", "...", "", "训练" * 200, '<bad>:"title"|?*'):
            with self.subTest(title=title):
                name = directory_name([title], fallback="QOJ 2565")
                self.assertEqual(Path(name).name, name)
                self.assertNotIn("\\", name)
                self.assertFalse(any(ord(c) < 32 for c in name))
                self.assertLess(len(name.encode("utf8")), 255)
        self.assertEqual(directory_name([], fallback="QOJ 2565"), "QOJ 2565")

    def test_strict_parser_remains_available(self):
        with self.assertRaises(VPError):
            directory_name(["Practice"])


class AdapterTests(OfflineTest):
    def qoj(self, title="Winter Practice", ruleset="ICPC"):
        with patch("vp_helper.qoj.WebSession") as web:
            api = QOJ("https://qoj.ac/contest/2565", {"handle": "example"})
        api.web = web.return_value
        api.web.soup.side_effect = [qoj_page(title, ruleset), soup('''
            <table><tr><td><a href="/contest/2565">Contest</a></td><td>5 hours</td></tr></table>''')]
        api.web.session.headers = {"User-Agent": "offline-test"}
        return api

    def test_qoj_nonstandard_preparation_and_submission_mapping(self):
        api = self.qoj()
        c = api.prepare()
        self.assertEqual(c.directory_name, "QOJ 2565 Winter Practice")
        self.assertEqual(c.rules.duration, 18000)
        self.assertEqual(c.rules.freeze_at, 14400)
        self.assertIsNone(c.rules.problem_reveal_count)
        self.assertEqual(c.pdfs, {"Statements-en.pdf": "https://qoj.ac/download.php?id=example"})
        config = api.submit_configuration(c)
        self.assertEqual(config["problem_ids"], {"A": "101", "B": "205"})
        self.assertEqual(config["base_url"], c.url)
        api.start.assert_not_called()
        api.web.post.assert_not_called()

    def test_qoj_standard_reveal_and_archive_remain_enabled(self):
        c = self.qoj("2026 CCPC Changchun Regional Contest").prepare()
        self.assertEqual(c.name, "2026 CCPC Changchun Reg")
        self.assertEqual(c.rules.problem_reveal_count, 50)
        self.assertEqual(archive_path(c), "data/ccpc/12th/changchun")

    def test_qoj_uses_contest_heading_instead_of_site_logo(self):
        api = self.qoj()
        page = qoj_page("Winter Practice")
        page.h1.wrap(page.new_tag("div", attrs={"class": "uoj-content"}))
        page.insert(0, soup("<h1>QOJ.ac</h1><h1>QOJ</h1>"))
        listing = soup('<tr><td><a href="/contest/2565">Contest</a></td><td>5 hours</td></tr>')
        api.web.soup.side_effect = [page, listing]
        self.assertEqual(api.prepare().name, "QOJ 2565 Winter Practice")

    def test_qoj_embedded_statement_pdf_without_pdf_extension(self):
        api = self.qoj()
        page = qoj_page("Winter Practice")
        page.select_one('a[href*="download.php"]').decompose()
        listing = soup('<tr><td><a href="/contest/2565">Contest</a></td><td>5 hours</td></tr>')
        api.web.soup.side_effect = [page, listing,
            soup('<iframe src="/download.php?type=statement&amp;id=101&amp;contest_id=2565"></iframe>'),
            soup('<iframe src="/download.php?type=statement&amp;id=205&amp;contest_id=2565"></iframe>')]
        c = api.prepare()
        self.assertEqual(c.pdfs, {
            "A.pdf": "https://qoj.ac/download.php?type=statement&id=101&contest_id=2565",
            "B.pdf": "https://qoj.ac/download.php?type=statement&id=205&contest_id=2565"})
        api.web.post.assert_not_called()

    def test_qoj_rejects_non_icpc_before_other_preparation(self):
        for title in ("Winter Practice", "2024 ICPC Nanjing Regional"):
            with self.subTest(title=title):
                api = self.qoj(title, "IOI")
                with self.assertRaisesRegex(VPError, "does not use ICPC scoring"):
                    api.prepare()
                self.assertEqual(api.web.soup.call_count, 1)

    def gym(self, platform="gym", title="Winter Practice", ruleset="ICPC"):
        with patch("vp_helper.gym.WebSession"):
            api = Gym(f"https://codeforces.com/{'contest' if platform == 'cf' else 'gym'}/100001", {})
        api.api = Mock(return_value={"contest": {"type": ruleset, "phase": "FINISHED", "name": title,
                           "durationSeconds": 18000}, "problems": [{"index": "A"}], "rows": []})
        api.web.soup.return_value = soup('<a href="/attachments/statements.pdf">Statements</a>')
        return api

    def test_cf_and_gym_nonstandard_preparation(self):
        for platform in ("cf", "gym"):
            with self.subTest(platform=platform):
                api = self.gym(platform)
                c = api.prepare()
                self.assertEqual(c.name, f"{platform.upper()} 100001 Winter Practice")
                self.assertEqual(c.problems, {"A": "A"})
                self.assertTrue(c.pdfs)
                api.start.assert_not_called()

    def test_gym_standard_name_and_non_icpc_rejection(self):
        self.assertEqual(self.gym(title="2024 ICPC Nanjing Regional").prepare().name, "2024 ICPC Nanjing Reg")
        for ruleset in ("CF", "IOI"):
            with self.subTest(ruleset=ruleset), self.assertRaisesRegex(VPError, "does not use ICPC scoring"):
                self.gym(ruleset=ruleset).prepare()

    def test_unverified_qoj_reference_never_fabricates_results(self):
        api = self.qoj()
        c = api.prepare()
        for elapsed in (600, 18001):
            with self.subTest(elapsed=elapsed):
                reference = api._original_reference("", c, elapsed, True, Mock())
                self.assertFalse(reference.complete)
                self.assertFalse(reference.statistics_complete)
                self.assertEqual(reference.rows, [])
                self.assertIsNone(reference.replay)
                self.assertIsNone(c.medals)
                self.assertIn("unavailable", reference.message)

    def test_unverified_gym_ghost_reference_remains_unknown(self):
        api = self.gym()
        c = api.prepare()
        api._original["rows"] = [{"party": {"participantType": "CONTESTANT", "ghost": True,
                      "teamName": "Example", "members": []}, "problemResults": [{"points": 0}], "penalty": 0}]
        for elapsed in (600, 18001):
            with self.subTest(elapsed=elapsed):
                reference = api.reference(c, elapsed)
                self.assertFalse(reference.complete)
                self.assertFalse(reference.statistics_complete)
                self.assertEqual(reference.rows, [])

    def test_directory_download_and_submit_config_before_native_start_boundary(self):
        api = self.qoj()
        c = api.prepare()
        api.prepare = Mock(return_value=c)
        api.account.update(cookie_source="manual", submit_script=str(self.root / "submit"))
        script = self.root / "submit"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o700)
        # External submit scripts require contiguous IDs; use a synthetic mapping.
        c.problems["B"] = "102"
        api.web.pdf.side_effect = lambda url, target: target.write_bytes(b"%PDF-synthetic-test")
        api.start = Mock(side_effect=VPError("offline native-start boundary"))
        with patch("vp_helper.cli.adapter", return_value=api), patch("vp_helper.cli.load_account", return_value={"download_pdfs": True}), \
             patch("vp_helper.cli.desktop", return_value=self.root / "Desktop"), \
             patch("vp_helper.cli.shutil.which", return_value="tmux"), patch("sys.stdin.isatty", return_value=True):
            with self.assertRaisesRegex(VPError, "offline native-start boundary"):
                cli.prepare_start(c.url)
        directory = self.root / "Desktop" / c.directory_name
        self.assertTrue((directory / "Statements-en.pdf").is_file())
        self.assertEqual(read_json(directory / "contest.json")["problem_ids"], c.problems)
        self.assertEqual((directory / "contest.json").stat().st_mode & 0o777, 0o600)

    def test_bundled_submit_uses_current_release_and_nonconsecutive_ids(self):
        api = self.qoj()
        c = api.prepare()
        selected = cli.state_directory() / "submit"
        selected.parent.mkdir(parents=True)
        wrapper = self.root / "submit-wrapper"
        wrapper.symlink_to(selected)
        release = self.root / "release"
        tool = release / "third_party/qoj/submit"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\nexit 1\n")
        tool.chmod(0o700)
        selected.symlink_to(tool)
        api.account["submit_script"] = str(wrapper)
        directory = self.root / c.directory_name
        directory.mkdir()
        with patch("sys.prefix", str(release / "venv")):
            cli.configure_submit(api, c, directory)
        self.assertEqual(selected.resolve(), tool)
        self.assertEqual(read_json(directory / "contest.json")["problem_ids"], {"A": "101", "B": "205"})


class ReplayTests(OfflineTest):
    def test_verified_replay_medals_freeze_and_reveal(self):
        c = contest("2026 CCPC Changchun Reg")
        c.rules = Rules(600, freeze_at=300, unfreeze_at=600, problem_reveal_count=2)
        c.medals = Medals(1, 0, 0, "synthetic fixture")
        teams = [{"id": "t1", "name": "Example 1", "official": True},
                 {"id": "t2", "name": "Example 2", "official": False}]
        events = [Submission("1", "t1", "A", 60, "CORRECT"),
                  Submission("2", "t2", "A", 120, "CORRECT"),
                  Submission("3", "t1", "B", 360, "CORRECT")]
        rows = [score_submissions(events, list(c.problems), c.rules, 600, team_id=t["id"], name=t["name"])
                for t in teams]
        rows[1].eligible = False
        replay = OriginalReplay(c, teams, events, rows)
        early = replay.at(c, 90)
        self.assertTrue(early.complete)
        self.assertEqual(revealed_problems(c, 90, early), frozenset())
        middle = replay.at(c, 180)
        self.assertEqual(revealed_problems(c, 180, middle), frozenset({"A"}))
        self.assertEqual(statistics(middle.rows, list(c.problems))["accepted"]["A"], 1)
        frozen = replay.at(c, 400)
        self.assertEqual(frozen.rows[0].solved, 1)
        self.assertEqual(revealed_problems(c, 400, frozen), frozenset(c.problems))
        final = replay.at(c, 600, released=True)
        self.assertEqual(final.rows[0].solved, 2)
        self.assertEqual(rank_label(final.rows[0], final.rows), "1")
        self.assertEqual(prize_of(final.rows[0], final.rows, c.medals), "Gold")
        with self.assertRaises(VPError):
            OriginalReplay(c, teams, events[:-1], rows)

    def test_monitor_keeps_personal_score_history_and_unknown_reference(self):
        c = contest()
        events = [Submission("1", "me", "A", 60, "WRONG-ANSWER"),
                  Submission("2", "me", "A", 300, "CORRECT"),
                  Submission("3", "me", "B", 18001, "CORRECT")]
        path = self.root / "vp-example.json"
        write_json(path, {"contest": c.to_dict(), "url": c.url, "start_time": 1000,
                         "handle": "example", "status": "running", "history_path": str(self.root / "history.csv")})
        api = Mock(handle="example", start_time=1000, submissions_complete=True, submission_message="")
        api.submissions.return_value = events
        api.reference.return_value = Reference(message="Original data unavailable")

        class ImmediateExecutor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def submit(self, fn, *args):
                future = Future()
                future.set_result(fn(*args))
                return future

        view = Mock()
        view.input.side_effect = [True, True, False]
        with patch("vp_helper.cli.adapter", return_value=api), \
             patch("vp_helper.cli.ThreadPoolExecutor", return_value=ImmediateExecutor()), \
             patch("vp_helper.cli.TerminalDashboard") as dashboard, \
             patch("vp_helper.cli.time.time", return_value=19002), \
             patch("vp_helper.cli.time.monotonic", side_effect=[200, 201]), patch("vp_helper.cli.time.sleep"):
            dashboard.return_value.__enter__.return_value = view
            cli.monitor(path)
        panel = view.update.call_args.args[0]
        self.assertEqual((panel.me.solved, panel.me.penalty), (1, 25))
        self.assertEqual(panel.rank, "—")
        self.assertEqual(panel.prize, "Unknown")
        self.assertIsNone(panel.stats)
        self.assertEqual(len(panel.submissions), 3)
        self.assertTrue(panel.submissions[0]["practice"])
        saved = read_history(self.root / "history.csv")[0]
        self.assertEqual((saved["solved"], saved["penalty"], saved["rank"], saved["prize"]), ("1", "25", "", "Unknown"))


class FirefoxTests(OfflineTest):
    def profile(self, base, name="example.default-esr", *, absolute=False):
        profile = base / name
        profile.mkdir(parents=True, exist_ok=True)
        (base / "profiles.ini").write_text(
            "[Profile0]\nName=default-esr\nDefault=1\nIsRelative=" + ("0" if absolute else "1")
            + "\nPath=" + (str(profile) if absolute else name) + "\n", encoding="utf8")
        return profile.resolve()

    def test_esr_relative_and_absolute_profiles(self):
        for absolute in (False, True):
            with self.subTest(absolute=absolute):
                profile = self.profile(self.root / ".mozilla/firefox-esr", absolute=absolute)
                self.assertIn(profile, firefox_profiles({}))

    def test_existing_layouts_remain_discoverable(self):
        for base in (self.root / ".mozilla/firefox", self.root / "config/mozilla/firefox",
                     self.root / "snap/firefox/common/.mozilla/firefox",
                     self.root / ".var/app/org.mozilla.firefox/.mozilla/firefox"):
            with self.subTest(base=base):
                self.assertIn(self.profile(base), firefox_profiles({}))

    def test_explicit_profile_has_precedence(self):
        self.profile(self.root / ".mozilla/firefox-esr")
        explicit = self.root / "custom-profile"
        explicit.mkdir()
        self.assertEqual(firefox_profiles({"firefox_profile": str(explicit)}), [explicit])

    def test_default_esr_profile_priority(self):
        base = self.root / ".mozilla/firefox-esr"
        first = self.profile(base, "first")
        second = base / "second"
        second.mkdir()
        with (base / "profiles.ini").open("a") as out:
            out.write("[Profile1]\nPath=second\nIsRelative=1\n[InstallExample]\nDefault=second\n")
        self.assertEqual(firefox_profiles({}), [second, first])

    def test_sudo_discovers_invoking_users_esr_profile(self):
        home = self.root / "ordinary-user"
        profile = self.profile(home / ".mozilla/firefox-esr")
        with patch("os.geteuid", return_value=0), patch.dict("os.environ", {"SUDO_USER": "example"}), \
             patch("pwd.getpwnam", return_value=SimpleNamespace(pw_dir=str(home))):
            self.assertEqual(firefox_profiles({}), [profile])

    def test_absent_profiles_report_diagnostics(self):
        diagnostics = []
        self.assertEqual(firefox_profiles({}, diagnostics=diagnostics), [])
        self.assertTrue(diagnostics)


class VersionTests(OfflineTest):
    def test_version_command_does_not_load_accounts_or_start(self):
        with patch("sys.argv", ["vp", "--version"]), patch("vp_helper.cli.console") as console, \
             patch("vp_helper.cli.start", side_effect=AssertionError("Must not start")), \
             patch("vp_helper.cli.load_account", side_effect=AssertionError("Must not load account")):
            cli.main()
        console.print.assert_called_once_with("VP Helper " + __version__, markup=False)


class InstallerTests(OfflineTest):
    def test_user_installer_preserves_conflicting_entries(self):
        script = Path(__file__).resolve().parents[1] / "install-user.sh"
        for name in ("vp", "submit"):
            with self.subTest(name=name):
                home = self.root / name
                bin_dir = home / ".local/bin"
                bin_dir.mkdir(parents=True)
                target = bin_dir / name
                target.write_text("existing unrelated program\n")
                # Preflight dependencies need no real interpreter or tmux here.
                commands = home / "commands"
                commands.mkdir()
                for command in ("python3", "tmux"):
                    executable = commands / command
                    executable.write_text("#!/bin/sh\nexit 0\n")
                    executable.chmod(0o700)
                result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                    env={**os.environ, "HOME": str(home), "PATH": str(commands) + ":/usr/bin:/bin",
                         "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state")})
                self.assertEqual(result.returncode, 1)
                self.assertIn("Preserved existing", result.stderr)
                self.assertEqual(target.read_text(), "existing unrelated program\n")
                self.assertFalse((home / "data/vp-helper").exists())


if __name__ == "__main__":
    unittest.main()
