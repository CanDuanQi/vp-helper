"""Run the real start/parser logic against synthetic QOJ pages, never the network."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from vp_helper import cli, http
from vp_helper.http import WebSession
from vp_helper.models import Contest, Rules, VPError
from vp_helper.parsing import timestamp
from vp_helper.qoj import QOJ
from vp_helper.storage import read_json


URL = "https://qoj.ac/contest/2565"
AT = "2026-11-02T08:00:00+08:00"
START = timestamp(AT)
NAV = '<ul class="nav-pills"><li><a href="/user/profile/example">example</a></li></ul>'
DASHBOARD = NAV + '''<form id="form-regvir" method="post" action="?locale=en">
    <input type="hidden" name="_token" value="dashboard-token">
    <button type="submit" name="submit-regvir" value="regvir">Start!</button></form>'''
FINAL = NAV + '''<h1>Virtual Contest Rules</h1>
    <form id="form-register_vp" method="post" action="/contest/2565/virtual/register?locale=en">
    <input type="hidden" name="_token" value="final-token">
    <button type="submit" name="submit-register_vp" value="register_vp">Register!</button></form>'''


def soup(html):
    return BeautifulSoup(html, "html.parser")


def response(html="", *, url=URL, status=200, headers=None):
    return SimpleNamespace(content=html.encode(), text=html, url=url, status_code=status, headers=headers or {})


def registration(*, virtual=(), timed=(), pages=""):
    def user_cell(handle):
        return f'<td><span class="uoj-username">{handle}</span></td>'

    five = "".join('<tr><td>1</td>' + user_cell(handle) + '<td>1500</td><td>'
                   + f'<time datetime="{at}">server time</time></td><td>{minutes}</td></tr>'
                   for handle, at, minutes in timed)
    three = "".join('<tr><td>1</td>' + user_cell(handle) + '<td>'
                    + f'<time datetime="{at}">server time</time></td></tr>' for handle, at in virtual)
    return soup(f'''<table><thead><tr><th>#</th><th>Username</th><th>Rating</th>
        <th>Start Time</th><th>Duration</th></tr></thead><tbody>{five}</tbody></table>
        <table><thead><tr><th>#</th><th>Username</th><th>Start Time</th></tr></thead>
        <tbody>{three}</tbody></table>{pages}''')


EMPTY = lambda: registration()
STARTED = lambda: registration(virtual=[("example", AT)])


class OfflineStartTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict("os.environ", {"XDG_STATE_HOME": str(self.root)}))
        stack.enter_context(patch("pathlib.Path.home", return_value=self.root))
        # Even an accidental real transport call must fail before doing I/O.
        for target in ("socket.create_connection", "socket.socket.connect",
                       "requests.sessions.Session.request", "curl_cffi.requests.Session.request"):
            stack.enter_context(patch(target, side_effect=AssertionError("Network forbidden: " + target)))
        self.sleep = stack.enter_context(patch("vp_helper.qoj.time.sleep"))
        stack.enter_context(patch("vp_helper.qoj.time.time", return_value=START + 60))

    def api(self, pages):
        with patch("vp_helper.qoj.WebSession"):
            api = QOJ(URL, {"handle": "example", "cookie": "fake-cookie"})
        pages = iter(pages)

        def read(url, **kwargs):
            if url == URL:
                return soup(DASHBOARD)
            self.assertEqual(url, URL + "/registrants")
            self.assertEqual(kwargs["params"]["locale"], "en")
            return next(pages)

        api.web.soup.side_effect = read
        api.web.get.return_value = response(FINAL, url=URL + "/virtual/register?locale=en")
        api.web.post.return_value = response(NAV + "<p>Contest is running</p>")
        return api

    def contest(self):
        return Contest("qoj", "2565", URL, "QOJ 2565 Practice", "QOJ 2565 Practice",
                       {"A": "101"}, Rules(18000))


class StartTests(OfflineStartTest):
    def test_single_cli_command_prepares_starts_and_launches_panel(self):
        api = self.api([EMPTY(), STARTED()])
        c = self.contest()
        c.pdfs = {"A.pdf": "https://qoj.ac/download.php?type=statement&id=101"}
        api.prepare = Mock(return_value=c)
        api.web.session.headers = {"User-Agent": "offline-test"}
        script = self.root / "submit"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o700)
        api.account["submit_script"] = str(script)
        api.web.pdf.side_effect = lambda url, target: target.write_bytes(b"%PDF-synthetic-test")
        with patch("vp_helper.cli.adapter", return_value=api), patch("vp_helper.cli.load_account", return_value={}), \
             patch("vp_helper.cli.desktop", return_value=self.root / "Desktop"), \
             patch("vp_helper.cli.shutil.which", return_value="tmux"), patch("sys.stdin.isatty", return_value=True), \
             patch("vp_helper.cli.tmux_exists", return_value=False), patch("vp_helper.cli.launch") as launch, \
             patch("vp_helper.cli.console"):
            cli.start(URL)
        path, state = launch.call_args.args
        self.assertEqual(state["start_time"], START)
        self.assertEqual(read_json(path)["start_time"], START)
        self.assertTrue((Path(state["directory"]) / "A.pdf").exists())
        self.assertEqual(read_json(Path(state["directory"]) / "contest.json")["vp_context"]["start_time"], START)
        self.assertEqual(api.web.post.call_count, 1)

    def test_auto_start_posts_final_form_once_and_uses_native_time(self):
        api = self.api([EMPTY(), STARTED()])
        c = self.contest()
        self.assertEqual(api.start(c), START)
        self.assertEqual(api.start_time, START)
        api.web.get.assert_called_once_with(URL + "/virtual/register", params={"locale": "en"})
        api.web.post.assert_called_once_with(URL + "/virtual/register?locale=en",
            {"_token": "final-token", "submit-register_vp": "register_vp"},
            referer=URL + "/virtual/register?locale=en")
        self.assertFalse(api._start_receipt().exists())

    def test_existing_running_vp_resumes_without_any_post(self):
        api = self.api([registration(virtual=[("example", AT)], timed=[("example", AT, 120)])])
        c = self.contest()
        self.assertEqual(api.start(c), START)
        self.assertEqual(c.rules.duration, 7200)
        api.web.post.assert_not_called()
        api.web.get.assert_not_called()

    def test_delayed_registry_is_only_polled_with_get(self):
        api = self.api([EMPTY(), EMPTY(), EMPTY(), EMPTY(), STARTED()])
        self.assertEqual(api.start(self.contest()), START)
        self.assertEqual(api.web.post.call_count, 1)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 3, 5])

    def test_post_timeout_but_server_started_is_reconciled(self):
        api = self.api([EMPTY(), EMPTY(), STARTED()])
        api.web.post.side_effect = VPError("Response timed out")
        self.assertEqual(api.start(self.contest()), START)
        self.assertEqual(api.web.post.call_count, 1)
        self.assertFalse(api._start_receipt().exists())

    def test_finished_old_record_is_not_confirmation_of_new_start(self):
        old_at = "2026-11-01T08:00:00+08:00"
        old = lambda: registration(virtual=[("example", old_at)])
        api = self.api([old(), old(), STARTED()])
        self.assertEqual(api.start(self.contest()), START)
        self.assertEqual(api.web.post.call_count, 1)
        self.assertEqual(self.sleep.call_count, 1)

    def test_already_finished_vp_without_start_form_is_restored(self):
        old_at = "2026-11-01T08:00:00+08:00"
        api = self.api([registration(virtual=[("example", old_at)])])
        original = api.web.soup.side_effect
        api.web.soup.side_effect = lambda url, **kw: soup(NAV) if url == URL else original(url, **kw)
        self.assertEqual(api.start(self.contest()), timestamp(old_at))
        api.web.post.assert_not_called()

    def test_explicit_rejection_is_reported_and_token_is_redacted(self):
        api = self.api([EMPTY()])
        api.web.post.return_value = response('<title>QOJ</title><div class="uoj-content">'
            '<p>This page has expired. final-token fake-cookie</p></div>')
        with self.assertRaisesRegex(VPError, "QOJ rejected the VP registration") as caught:
            api.start(self.contest())
        self.assertNotIn("final-token", str(caught.exception))
        self.assertNotIn("fake-cookie", str(caught.exception))
        self.assertEqual(api.web.post.call_count, 1)
        self.assertFalse(api._start_receipt().exists())

    def test_form_field_and_json_rejections_are_recognized(self):
        for content in ('<form><div class="has-error"><p class="help-block">Registration denied</p></div></form>',
                        '{"success": false, "message": "Registration denied"}'):
            with self.subTest(content=content):
                api = self.api([EMPTY()])
                api.web.post.return_value = response(content)
                with self.assertRaisesRegex(VPError, "Registration denied"):
                    api.start(self.contest())
                self.assertEqual(api.web.post.call_count, 1)
                self.assertFalse(api._start_receipt().exists())

    def test_success_notice_is_not_misread_as_rejection(self):
        api = self.api([EMPTY(), STARTED()])
        api.web.post.return_value = response('<title>Message - QOJ</title>'
            '<main>Registration accepted. Your participation is starting.</main>')
        self.assertEqual(api.start(self.contest()), START)
        self.assertEqual(api.web.post.call_count, 1)

    def test_unknown_result_persists_and_next_invocation_never_posts(self):
        api = self.api([EMPTY()] + [EMPTY() for _ in range(6)])
        with self.assertRaisesRegex(VPError, "no second start POST"):
            api.start(self.contest())
        self.assertEqual(api.web.post.call_count, 1)
        pending = read_json(api._start_receipt())
        self.assertEqual(pending, {"url": URL, "handle": "example", "previous_start": None})
        self.assertEqual(api._start_receipt().stat().st_mode & 0o777, 0o600)
        self.assertEqual(sum(call.args[0] for call in self.sleep.call_args_list), 30)
        retry = self.api([EMPTY() for _ in range(6)])
        with self.assertRaisesRegex(VPError, "no second start POST"):
            retry.start(self.contest())
        retry.web.post.assert_not_called()
        retry.web.get.assert_not_called()
        # The same command can recover once QOJ exposes the native record.
        recovered = self.api([STARTED()])
        self.assertEqual(recovered.start(self.contest()), START)
        recovered.web.post.assert_not_called()
        self.assertFalse(recovered._start_receipt().exists())

    def test_crash_after_sending_does_not_allow_a_second_post(self):
        api = self.api([EMPTY()])
        api.web.post.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            api.start(self.contest())
        self.assertTrue(api._start_receipt().exists())
        retry = self.api([STARTED()])
        self.assertEqual(retry.start(self.contest()), START)
        retry.web.post.assert_not_called()

    def test_timed_out_request_with_no_record_remains_pending(self):
        api = self.api([EMPTY()] + [EMPTY() for _ in range(6)])
        api.web.post.side_effect = VPError("Response timed out")
        with self.assertRaisesRegex(VPError, "Response timed out"):
            api.start(self.contest())
        self.assertEqual(api.web.post.call_count, 1)
        self.assertTrue(api._start_receipt().exists())

    def test_post_success_with_unparseable_registry_keeps_diagnostic_and_guard(self):
        malformed = soup("<table><tr><th>Changed columns</th></tr></table>")
        api = self.api([EMPTY()] + [malformed for _ in range(6)])
        with self.assertRaisesRegex(VPError, "Registration query failed.*columns"):
            api.start(self.contest())
        self.assertEqual(api.web.post.call_count, 1)
        self.assertTrue(api._start_receipt().exists())

    def test_parser_failure_before_post_does_not_mean_unregistered(self):
        api = self.api([soup("<p>Registry failed to load</p>")])
        with self.assertRaisesRegex(VPError, "columns could not be identified"):
            api.start(self.contest())
        api.web.post.assert_not_called()

    def test_account_mismatch_never_posts(self):
        api = self.api([])
        api.handle = "different-account"
        with self.assertRaisesRegex(VPError, "does not belong"):
            api.start(self.contest())
        api.web.post.assert_not_called()

    def test_final_form_destination_and_csrf_are_checked_before_post(self):
        for html in (FINAL.replace("final-token", ""),
                     FINAL.replace('/contest/2565/virtual/register?locale=en', 'https://other.invalid/start'),
                     FINAL.replace('form-register_vp', 'changed-form')):
            with self.subTest(html=html):
                api = self.api([EMPTY()])
                api.web.get.return_value = response(html, url=URL + "/virtual/register")
                with self.assertRaises(VPError):
                    api.start(self.contest())
                api.web.post.assert_not_called()
                self.assertFalse(api._start_receipt().exists())

    def test_legacy_direct_start_remains_supported(self):
        api = self.api([EMPTY(), STARTED()])
        api.web.get.side_effect = VPError("qoj.ac returned HTTP 404")
        self.assertEqual(api.start(self.contest()), START)
        self.assertEqual(api.web.post.call_count, 1)
        self.assertEqual(api.web.post.call_args.args[1]["submit-regvir"], "regvir")


class RegistrationTests(OfflineStartTest):
    def test_join_virtual_identity_and_duration_in_both_tables(self):
        page = registration(virtual=[("example", AT)], timed=[("example", AT, 120)])
        api = self.api([])
        self.assertEqual(api._registration(first=page, duration=18000), (START, 7200))

    def test_official_only_account_is_not_assumed_virtual(self):
        page = registration(timed=[("example", AT, 300)])
        api = self.api([])
        self.assertIsNone(api._registration(first=page, duration=18000))

    def test_join_records_across_pages_and_select_latest(self):
        page1 = registration(virtual=[("example", AT)], pages='<a href="?page=2">2</a>')
        page2 = registration(timed=[("example", AT, 120)])
        api = self.api([page2])
        self.assertEqual(api._registration(first=page1, duration=18000), (START, 7200))
        self.assertEqual(api.web.soup.call_args.kwargs["params"]["page"], 2)

    def test_latest_registration_is_selected_across_all_pages(self):
        old = "2026-11-01T08:00:00+08:00"
        page1 = registration(virtual=[("example", old)], pages='<a href="?page=2">2</a>')
        api = self.api([STARTED()])
        self.assertEqual(api._registration(first=page1, duration=18000), (START, 18000))

    def test_datetime_attribute_plain_text_and_timezone(self):
        api = self.api([])
        for value in (AT, "2026-11-02T00:00:00Z"):
            with self.subTest(value=value):
                page = registration(virtual=[("example", value)])
                self.assertEqual(api._registration(first=page, duration=18000), (START, 18000))
        page = STARTED()
        page.time.replace_with("2026-11-02 08:00:00")
        self.assertEqual(api._registration(first=page, duration=18000), (START, 18000))

    def test_malformed_native_time_is_an_error_not_absence(self):
        api = self.api([])
        with self.assertRaisesRegex(VPError, "Invalid platform timestamp"):
            api._registration(first=registration(virtual=[("example", "changed format")]), duration=18000)

    def test_changed_column_order_uses_headers(self):
        page = soup(f'''<table><thead><tr><th>Start Time</th><th>Username</th><th>#</th></tr></thead>
            <tr><td><time datetime="{AT}"></time></td><td><a href="/user/profile/example">nickname</a></td>
            <td>1</td></tr></table>''')
        self.assertEqual(self.api([])._registration(first=page, duration=18000), (START, 18000))

    def test_changed_virtual_table_is_not_hidden_by_valid_five_column_table(self):
        page = registration(timed=[("example", AT, 300)])
        headers = page.select("table")[1].select("th")
        headers[2].string = "Unexpected clock format"
        with self.assertRaisesRegex(VPError, "start-time column"):
            self.api([])._registration(first=page, duration=18000)


class TransportTests(OfflineStartTest):
    def web(self):
        web = WebSession("https://qoj.ac", {}, auto_refresh=False)
        web.session.close()
        web.session = Mock()
        return web

    def test_303_follows_with_get_and_exactly_one_post(self):
        web = self.web()
        web.session.post.return_value = response(status=303, headers={"Location": "/contest/2565"})
        final = response("<p>Running</p>")
        with patch.object(web, "get", return_value=final) as get:
            self.assertIs(web.post(URL, {"_token": "fake"}, referer=URL), final)
        get.assert_called_once_with(URL)
        web.session.post.assert_called_once_with(URL, data={"_token": "fake"},
            headers={"Referer": URL}, timeout=(10, 40), allow_redirects=False)

    def test_307_and_308_never_replay_post(self):
        for status in (307, 308):
            with self.subTest(status=status):
                web = self.web()
                web.session.post.return_value = response(status=status, headers={"Location": "/contest/2565"})
                with self.assertRaisesRegex(VPError, "not followed"):
                    web.post(URL, {"_token": "fake"})
                self.assertEqual(web.session.post.call_count, 1)
                web.session.get.assert_not_called()

    def test_cross_origin_redirect_is_not_followed(self):
        web = self.web()
        web.session.post.return_value = response(status=302, headers={"Location": "https://other.invalid/"})
        with self.assertRaisesRegex(VPError, "unexpected form redirect"):
            web.post(URL, {"_token": "fake"})
        self.assertEqual(web.session.post.call_count, 1)
        web.session.get.assert_not_called()

    def test_transport_timeout_does_not_retry_post(self):
        web = self.web()
        web.session.post.side_effect = http._RequestException("Response lost")
        with self.assertRaisesRegex(VPError, "did not confirm"):
            web.post(URL, {"_token": "fake"})
        self.assertEqual(web.session.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
