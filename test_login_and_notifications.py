import os
import tempfile
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.invalid"}):
    import oh_bot
import refresh_cookie


class AutomaticLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.marker = os.path.join(self.temp.name, "attempted")
        self.page = Mock(url="https://login.cmu.edu/idp/profile/login")
        self.password = Mock()
        self.password.count.return_value = 1
        self.form = Mock()
        self.form.count.return_value = 1
        self.form.get_attribute.return_value = "/idp/login"
        self.username = Mock()
        self.username.count.return_value = 1
        self.page.locator.return_value.first = self.password
        self.password.locator.return_value = self.form
        self.form.locator.return_value.first = self.username
        self.keyring = Mock()
        self.keyring.get_password.return_value = "example password"

    def attempt(self):
        with patch.dict(os.environ, {"ANDREW_ID": "example"}), \
                patch.dict("sys.modules", {"keyring": self.keyring}), \
                patch.object(refresh_cookie, "LOGIN_ATTEMPT_FILE", self.marker):
            return refresh_cookie.try_saved_credentials(self.page)

    def test_submits_once_and_stops_until_reset(self):
        self.assertTrue(self.attempt())
        self.assertFalse(self.attempt())
        self.username.fill.assert_called_once_with("example")
        self.password.fill.assert_called_once_with("example password")
        self.password.press.assert_called_once_with("Enter")
        self.assertEqual(os.stat(self.marker).st_mode & 0o777, 0o600)

    def test_rejects_other_host_and_form_action(self):
        self.page.url = "https://login.cmu.edu.evil.example/"
        self.assertFalse(self.attempt())
        self.page.url = "https://login.cmu.edu/"
        self.form.get_attribute.return_value = "https://evil.example/submit"
        self.assertFalse(self.attempt())
        self.password.fill.assert_not_called()

    def test_opens_ohq_login_link_when_enabled(self):
        self.page.url = "https://ohq.eberly.cmu.edu/"
        link = self.page.locator.return_value.first
        link.is_visible.return_value = True
        with patch.dict(os.environ, {"ANDREW_ID": "example"}), \
                patch.object(refresh_cookie, "LOGIN_ATTEMPT_FILE", self.marker):
            self.assertTrue(refresh_cookie.open_cmu_login(self.page))
        self.page.locator.assert_called_once_with('a[href="/api/login/cmushib"]')
        link.click.assert_called_once()

    def test_does_not_follow_link_for_another_host_or_after_attempt(self):
        self.page.url = "https://example.invalid/"
        with patch.dict(os.environ, {"ANDREW_ID": "example"}), \
                patch.object(refresh_cookie, "LOGIN_ATTEMPT_FILE", self.marker):
            self.assertFalse(refresh_cookie.open_cmu_login(self.page))
            self.page.url = "https://ohq.eberly.cmu.edu/"
            open(self.marker, "w").close()
            self.assertFalse(refresh_cookie.open_cmu_login(self.page))
        self.page.locator.assert_not_called()

    def test_session_check_uses_ohq_api(self):
        self.page.url = "https://ohq.eberly.cmu.edu/"
        self.page.context.cookies.return_value = [{"name": "session_id", "value": "example"}]
        with patch.object(refresh_cookie.requests, "get", return_value=Mock(status_code=200)) as get:
            self.assertEqual(refresh_cookie.live_session_header(self.page), "session_id=example")
        self.page.context.cookies.assert_called_once_with("https://ohq.eberly.cmu.edu/api/user")
        get.assert_called_once_with(
            "https://ohq.eberly.cmu.edu/api/user",
            headers={"Cookie": "session_id=example"}, allow_redirects=False, timeout=5,
        )

    def test_rejects_session_redirect(self):
        self.page.url = refresh_cookie.OHQ_URL
        self.page.context.cookies.return_value = [{"name": "session_id", "value": "expired"}]
        with patch.object(refresh_cookie.requests, "get", return_value=Mock(status_code=302)):
            self.assertIsNone(refresh_cookie.live_session_header(self.page))


class NotificationTests(unittest.TestCase):
    def test_retry_and_mention_suppression(self):
        responses = [Mock(status_code=500), Mock(status_code=204)]
        entry = {"first_name": "A", "last_name": "B", "topic": "@everyone", "location": "Room"}
        with patch.object(oh_bot.requests, "post", side_effect=responses) as post, \
                patch.object(oh_bot.time, "sleep"):
            oh_bot.notify_discord(entry)
        self.assertEqual(post.call_count, 2)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["content"], "**A B** joined the queue — *@everyone*")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(post.call_args.kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
