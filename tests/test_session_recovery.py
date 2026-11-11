"""Expired long-running monitor sessions and multi-day practice submissions."""
from unittest.mock import Mock, patch

from test_regressions import OfflineTest, contest
from test_qoj_start import response, START
from vp_helper.http import WebSession
from vp_helper.models import VPError
from vp_helper.qoj import QOJ
from vp_helper.scoring import score_submissions, submission_history


URL = "https://qoj.ac/contest/2565/submissions"


class SessionRecoveryTests(OfflineTest):
    def web(self, *, manual=False):
        account = {"handle": "example", "cookie": "session=old"}
        if manual:
            account["cookie_source"] = "manual"
        web = WebSession("https://qoj.ac", account)
        self.addCleanup(web.session.close)
        web.session.close()
        web.session = Mock()
        web._browser_checked = True  # The monitor was already running for hours.
        return web

    def test_blocked_login_recovers_same_account_and_retries_only_get(self):
        for status in (200, 401, 403):
            with self.subTest(status=status):
                web = self.web()
                final = response("<table>Final results</table>", url=URL)
                web.session.get.side_effect = [response("Just a moment...", url="https://qoj.ac/login", status=status), final]

                def refresh(platform, account, **kwargs):
                    self.assertEqual(platform, "qoj")
                    self.assertEqual(account["handle"], "example")
                    account["cookie"] = "session=new"
                    return True

                with patch("vp_helper.browser_login.refresh_browser_account", side_effect=refresh) as login:
                    self.assertIs(web.get(URL, retry=False), final)
                login.assert_called_once_with("qoj", web.account, force=True)
                web.session.cookies.clear.assert_called_once()
                self.assertEqual(web.session.get.call_count, 2)
                web.session.post.assert_not_called()

    def test_rejected_recovery_is_bounded_and_keeps_verification_explanation(self):
        web = self.web()
        blocked = response("<title>Just a moment...</title>", url="https://qoj.ac/login", status=403)
        web.session.get.return_value = blocked
        with patch.object(web, "_refresh_browser", return_value=True) as refresh:
            with self.assertRaisesRegex(VPError, "browser verification"):
                web.get(URL)
        refresh.assert_called_once_with(force=True)
        self.assertEqual(web.session.get.call_count, 2)
        web.session.post.assert_not_called()

    def test_unverified_browser_login_preserves_old_session_and_never_replays_get(self):
        web = self.web()
        web.session.get.return_value = response("Just a moment...", url="https://qoj.ac/login", status=403)
        with patch("vp_helper.browser_login.refresh_browser_account", return_value=False):
            with self.assertRaisesRegex(VPError, "browser verification"):
                web.get(URL)
        self.assertEqual(web.account["cookie"], "session=old")
        web.session.cookies.clear.assert_not_called()
        web.session.get.assert_called_once()

    def test_manual_cookie_is_not_replaced(self):
        web = self.web(manual=True)
        web.session.get.return_value = response("Login required", url="https://qoj.ac/login", status=403)
        with patch("vp_helper.browser_login.browser_cookies") as refresh:
            with self.assertRaisesRegex(VPError, "update the account cookie"):
                web.get(URL)
        refresh.assert_not_called()
        web.session.get.assert_called_once()

    def test_permission_error_and_foreign_login_do_not_refresh_account(self):
        for url in (URL, "https://other.invalid/login"):
            with self.subTest(url=url):
                web = self.web()
                web.session.get.return_value = response("Forbidden", url=url, status=403)
                with patch.object(web, "_refresh_browser") as refresh:
                    with self.assertRaises(VPError):
                        web.get(URL)
                refresh.assert_not_called()
                web.session.get.assert_called_once()

    def test_binary_error_is_not_decoded_as_a_challenge(self):
        web = self.web()
        class BinaryError:
            status_code = 503
            url = URL
            headers = {"Content-Type": "application/pdf"}

            @property
            def text(self):
                raise AssertionError("Binary response decoded")

        with self.assertRaisesRegex(VPError, "HTTP 503"):
            web.check(BinaryError())


def submission_row(sid, when, result, *, absolute=False):
    when = f'<time datetime="{when}">{when}</time>' if absolute else when
    return f'''<tr><td><a href="/submission/{sid}">#{sid}</a></td>
        <td><a href="/contest/2565/problem/101">A</a><sup><a href="/contest/2565">#</a></sup></td>
        <td><span class="uoj-username">example</span></td><td>{result}</td><td>{when}</td></tr>'''


class MultiDaySubmissionTests(OfflineTest):
    def test_recovered_get_syncs_21_27_and_100_hour_results_without_changing_score(self):
        page = ('<table><thead><tr><th>ID</th><th>Problem</th><th>Submitter</th><th>Result</th><th>Time</th></tr></thead>'
                + submission_row("6", "100:01:00", "100")
                + submission_row("5", "28:08:27", "100")
                + submission_row("4", "27:40:21", "Wrong Answer")
                + submission_row("3", "2026-11-03T11:55:05+08:00", "Time Limit Exceeded", absolute=True)
                + submission_row("2", "21:04:52", "100")
                + submission_row("1", "01:00:00", "100") + '</table>')
        web = WebSession("https://qoj.ac", {"handle": "example", "cookie": "session=old"})
        web.session.close()
        web.session = Mock()
        web._browser_checked = True
        web.session.get.side_effect = [response("Just a moment...", url="https://qoj.ac/login", status=403),
                                       response(page, url=URL)]
        with patch("vp_helper.qoj.WebSession", return_value=web):
            api = QOJ("https://qoj.ac/contest/2565", web.account)
        api.start_time = START
        c = contest()
        with patch.object(web, "_refresh_browser", return_value=True), \
             patch.object(api, "_registration", return_value=(START, c.rules.duration)):
            records = api.submissions(c)
        verdicts = {s.id: s.verdict for s in records}
        self.assertEqual(verdicts, {"6": "CORRECT", "5": "CORRECT", "4": "WRONG-ANSWER",
                                   "3": "TIMELIMIT", "2": "CORRECT", "1": "CORRECT"})
        by_id = {s.id: s for s in records}
        self.assertEqual(by_id["3"].time, 27 * 3600 + 55 * 60 + 5)
        self.assertEqual(by_id["6"].time, 100 * 3600 + 60)
        self.assertEqual(score_submissions(records, list(c.problems), c.rules, 101 * 3600).solved, 1)
        history = submission_history(records, list(c.problems), c.rules, 101 * 3600)
        self.assertTrue(all(s["practice"] for s in history[:-1]))
        self.assertFalse(history[-1]["practice"])
        self.assertEqual(api.start_time, START)
        self.assertTrue(api.submissions_complete)
        self.assertEqual(web.session.get.call_count, 2)
        web.session.post.assert_not_called()
