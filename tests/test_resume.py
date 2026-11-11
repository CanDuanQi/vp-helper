"""Resume saved panels without native starts, network access or file rewrites."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_regressions import OfflineTest, contest
from vp_helper import cli
from vp_helper.models import VPError
from vp_helper.resume_context import resume_state
from vp_helper.storage import config_directory, read_json, state_directory, write_json


class ResumeTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch("sys.stdin.isatty", return_value=True))
        self.stack.enter_context(patch("vp_helper.cli.shutil.which", return_value="tmux"))
        for name in ("start", "prepare_start", "load_account", "adapter", "configure_submit", "bind_submit_context"):
            self.stack.enter_context(patch("vp_helper.cli." + name, side_effect=AssertionError("Forbidden during resume: " + name)))
        self.exists = self.stack.enter_context(patch("vp_helper.cli.tmux_exists", return_value=True))
        self.attach = self.stack.enter_context(patch("vp_helper.cli.attach"))
        self.launch = self.stack.enter_context(patch("vp_helper.cli.launch"))
        self.run = self.stack.enter_context(patch("vp_helper.cli.subprocess.run", return_value=SimpleNamespace(stdout="0\n")))

    def saved(self, *, started=1000, platform="qoj", current=True):
        c = contest()
        if platform != "qoj":
            c.platform = platform
            c.url = "https://codeforces.com/" + ("gym" if platform == "gym" else "contest") + "/2565"
        folder = self.root / f"{platform}-{started}"
        folder.mkdir()
        path = state_directory() / f"vp-{platform}-2565-{started}.json"
        state = {"contest": c.to_dict(), "url": c.url, "start_time": started, "handle": "example",
                 "directory": str(folder), "tmux_session": path.stem, "status": "complete",
                 "submissions": [{"id": "1", "team": "me", "problem": "A", "time": 27*3600, "verdict": "CORRECT"}],
                 "history_path": str(self.root / "history.csv")}
        write_json(path, state)
        write_json(folder / "contest.json", {"handle": "example", "base_url": c.url,
                   "vp_context": {"state_path": str(path), "url": c.url, "handle": "example", "start_time": started}})
        write_json(config_directory() / "config.json", {platform: {"handle": "example", "submit_script": "/synthetic/custom-submit"}})
        if current:
            write_json(state_directory() / "current.json", {"state_path": str(path)})
        self.stack.enter_context(patch("pathlib.Path.cwd", return_value=folder))
        return path, state, folder

    def test_existing_completed_session_attaches_without_rewriting_files(self):
        path, state, folder = self.saved()
        (folder / "A.cpp").write_text("// keep my code\n")
        (folder / "A.pdf").write_bytes(b"keep my PDF")
        files = [path, folder / "contest.json", folder / "A.cpp", folder / "A.pdf", state_directory() / "current.json"]
        before = {p: p.read_bytes() for p in files}
        cli.resume()
        self.attach.assert_called_once_with(state["tmux_session"])
        self.launch.assert_not_called()
        self.assertEqual({p: p.read_bytes() for p in files}, before)

    def test_completed_session_after_reboot_rebuilds_only_the_saved_panel(self):
        path, state, _ = self.saved()
        self.exists.return_value = False
        cli.resume()
        self.launch.assert_called_once_with(path, state)
        self.attach.assert_not_called()
        self.assertEqual(read_json(path)["start_time"], 1000)

    def test_dead_monitor_respawns_only_the_upper_pane(self):
        path, state, _ = self.saved()
        self.run.return_value.stdout = "1\n"
        cli.resume()
        commands = [call.args[0] for call in self.run.call_args_list]
        respawn = [c for c in commands if c[1] == "respawn-pane"]
        self.assertEqual(len(respawn), 1)
        self.assertIn(state["tmux_session"] + ":0.0", respawn[0])
        self.assertIn("_monitor", respawn[0][-1])
        self.assertIn(str(path), respawn[0][-1])
        self.assertFalse(any(c[1] in ("new-session", "split-window", "kill-session") for c in commands))
        self.attach.assert_called_once()

    def test_folder_beats_another_current_vp_without_changing_current_pointer(self):
        old_path, old_state, old_folder = self.saved()
        new_path, _, _ = self.saved(started=2000)
        with patch("pathlib.Path.cwd", return_value=old_folder):
            cli.resume()
        self.attach.assert_called_once_with(old_state["tmux_session"])
        self.assertEqual(read_json(state_directory() / "current.json")["state_path"], str(new_path))
        self.assertEqual(read_json(old_path), old_state)

    def test_subdirectory_finds_the_contest_binding(self):
        path, state, folder = self.saved()
        source = folder / "src"
        source.mkdir()
        with patch("pathlib.Path.cwd", return_value=source):
            self.assertEqual(resume_state(), (path, state))

    def test_outside_contest_uses_last_prepared_vp(self):
        path, state, _ = self.saved()
        with patch("pathlib.Path.cwd", return_value=self.root):
            self.assertEqual(resume_state(), (path, state))

    def test_unbound_folder_does_not_fall_back_to_another_contest(self):
        _, _, folder = self.saved()
        write_json(folder / "contest.json", {"handle": "example"})
        with self.assertRaisesRegex(VPError, "no saved VP context"):
            cli.resume()
        self.attach.assert_not_called()
        self.launch.assert_not_called()

    def test_missing_or_external_state_never_starts_a_vp(self):
        path, state, folder = self.saved()
        config = read_json(folder / "contest.json")
        for target in (str(path.with_name("missing.json")), str(self.root / "outside.json")):
            with self.subTest(target=target):
                config["vp_context"]["state_path"] = target
                write_json(folder / "contest.json", config)
                with self.assertRaises(VPError):
                    cli.resume()
        self.attach.assert_not_called()
        self.launch.assert_not_called()

    def test_binding_mismatch_and_unrelated_tmux_session_are_rejected(self):
        path, state, folder = self.saved()
        original = read_json(folder / "contest.json")
        for field, value in (("start_time", 2000), ("handle", "other"), ("url", "https://qoj.ac/contest/999")):
            with self.subTest(field=field):
                config = {**original, "vp_context": {**original["vp_context"], field: value}}
                write_json(folder / "contest.json", config)
                with self.assertRaisesRegex(VPError, "does not match"):
                    cli.resume()
        write_json(folder / "contest.json", original)
        write_json(path, {**state, "tmux_session": "unrelated-user-session"})
        with self.assertRaisesRegex(VPError, "invalid"):
            cli.resume()
        self.attach.assert_not_called()
        self.launch.assert_not_called()

    def test_changed_account_is_rejected_before_attaching(self):
        self.saved()
        write_json(config_directory() / "config.json", {"qoj": {"handle": "other"}})
        with self.assertRaisesRegex(VPError, "account changed"):
            cli.resume()
        self.attach.assert_not_called()

    def test_cf_can_use_shared_gym_account_but_qoj_never_uses_it(self):
        self.saved(platform="cf")
        write_json(config_directory() / "config.json", {"gym": {"handle": "example", "submit_script": "/synthetic/custom-submit"}})
        cli.resume()
        self.attach.assert_called_once()
        self.saved(platform="qoj")
        write_json(config_directory() / "config.json", {"gym": {"handle": "example"}})
        with self.assertRaisesRegex(VPError, "account changed"):
            cli.resume()
        self.assertEqual(self.attach.call_count, 1)

    def test_qoj_cf_and_gym_all_resume_without_preparing(self):
        for platform in ("qoj", "cf", "gym"):
            with self.subTest(platform=platform):
                _, state, _ = self.saved(platform=platform)
                cli.resume()
                self.attach.assert_called_with(state["tmux_session"])
        self.assertEqual(self.attach.call_count, 3)

    def test_managed_submit_switches_platform_without_rewriting_contest_config(self):
        _, _, folder = self.saved()
        release = self.root / "release"
        tool = release / "third_party/qoj/submit"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o700)
        selected = state_directory() / "submit"
        selected.symlink_to(self.root / "old-cf-submit")
        wrapper = self.root / "submit"
        wrapper.symlink_to(selected)
        write_json(config_directory() / "config.json", {"qoj": {"handle": "example"}})
        before = (folder / "contest.json").read_bytes()
        with patch("vp_helper.cli.shutil.which", side_effect=lambda name: str(wrapper) if name == "submit" else "tmux"), \
             patch("vp_helper.cli.sys.prefix", str(release / "venv")):
            cli.resume()
        self.assertEqual(selected.resolve(), tool)
        self.assertEqual((folder / "contest.json").read_bytes(), before)

    def test_command_routes_to_resume_and_noninteractive_calls_fail_safely(self):
        self.saved()
        with patch("sys.argv", ["vp", "resume"]):
            cli.main()
        self.attach.assert_called_once()
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(VPError, "interactive"):
                cli.resume()

    def test_no_record_reports_error_without_launching_or_preparing(self):
        with patch("pathlib.Path.cwd", return_value=self.root):
            with self.assertRaisesRegex(VPError, "No saved VP"):
                cli.resume()
        self.attach.assert_not_called()
        self.launch.assert_not_called()
