import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from queue import SimpleQueue
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.invalid"}):
    import oh_bot as bot


class QueueScheduleTests(unittest.TestCase):
    def sync(self, when, *, auto_open=True, auto_close=True, is_open=True,
             questions_seen=True, waiting=False):
        schedule = bot.parse_queue_schedule({
            "QUEUE_AUTO_OPEN": str(auto_open),
            "QUEUE_AUTO_CLOSE": str(auto_close),
        })
        client = Mock()
        with patch.object(bot, "current_queue_schedule", return_value=schedule), \
                patch.object(bot, "datetime") as clock, \
                patch.object(bot, "queue_is_open", is_open), \
                patch.object(bot, "questions_seen", questions_seen), \
                patch.object(bot, "seen_ids", {1} if waiting else set()), \
                patch.object(bot, "last_queue_emit", 0), \
                patch.object(bot, "pending_queue_target", None):
            clock.now.return_value = datetime.fromisoformat(when).replace(tzinfo=bot.EASTERN)
            bot.sync_queue_open(client)
        return client

    def test_auto_close_defaults_to_existing_behavior(self):
        self.assertTrue(bot.parse_queue_schedule({})[1])

    def test_invalid_auto_close_names_the_setting(self):
        with self.assertRaisesRegex(ValueError, "QUEUE_AUTO_CLOSE must be true or false"):
            bot.parse_queue_schedule({"QUEUE_AUTO_CLOSE": "maybe"})

    def test_disabled_auto_close_leaves_queue_open_outside_schedule(self):
        for when in ("2026-11-04T16:59", "2026-11-04T20:00",
                     "2026-11-06T18:00", "2026-11-25T18:00", "2027-06-01T18:00"):
            with self.subTest(when=when):
                self.sync(when, auto_close=False).emit.assert_not_called()

    def test_scheduled_opening_still_works_with_auto_close_disabled(self):
        self.sync("2026-11-04T17:00", auto_close=False, is_open=False).emit.assert_called_once_with(
            "open_queue", namespace="/queue",
        )

    def test_enabled_auto_close_closes_empty_queue(self):
        self.sync("2026-11-04T20:00").emit.assert_called_once_with(
            "close_queue", namespace="/queue",
        )

    def test_auto_close_waits_for_known_open_and_empty_queue(self):
        for state in ({"questions_seen": False}, {"waiting": True}, {"is_open": None}):
            with self.subTest(state=state):
                self.sync("2026-11-04T20:00", **state).emit.assert_not_called()

    def test_reconnect_waits_for_fresh_metadata_before_closing(self):
        schedule = bot.parse_queue_schedule({"QUEUE_AUTO_OPEN": "true"})
        client = Mock()
        with patch.multiple(bot, queue_is_open=True, pending_queue_target=True, last_queue_emit=1,
                            questions_seen=True, seen_ids=set(), notifications=SimpleQueue()), \
                patch.object(bot, "current_queue_schedule", return_value=schedule), \
                patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 4, 20, tzinfo=bot.EASTERN)
            bot.reset_queue_connection()
            self.assertIsNone(bot.pending_queue_target)
            bot.handle_questions({"payload": []})
            bot.sync_queue_open(client)
            client.emit.assert_not_called()
            bot.remember_queue_open({"payload": [{"open": True}]})
            bot.sync_queue_open(client)
            client.emit.assert_called_once_with("close_queue", namespace="/queue")

    def test_question_update_is_visible_before_discord_delivery(self):
        schedule = bot.parse_queue_schedule({"QUEUE_AUTO_OPEN": "true"})
        client = Mock()
        entry = {"id": 1, "state": "on_queue"}
        with patch.multiple(bot, queue_is_open=True, pending_queue_target=None, last_queue_emit=0,
                            questions_seen=True, seen_ids=set(), notifications=SimpleQueue()), \
                patch.object(bot, "current_queue_schedule", return_value=schedule), \
                patch.object(bot, "datetime") as clock, patch.object(bot, "notify_discord") as send:
            clock.now.return_value = datetime(2026, 11, 4, 20, tzinfo=bot.EASTERN)
            bot.handle_questions({"payload": [entry]})
            bot.handle_questions({"payload": [entry]})
            bot.sync_queue_open(client)
            client.emit.assert_not_called()
            send.assert_not_called()
            self.assertEqual(bot.notifications.get_nowait(), (send, entry))
            self.assertTrue(bot.notifications.empty())

    def test_fast_queue_confirmation_enqueues_one_alert(self):
        schedule = bot.parse_queue_schedule({"QUEUE_AUTO_OPEN": "true"})
        client = Mock()
        confirmation = {"payload": [{"open": True}]}
        client.emit.side_effect = lambda *_args, **_kwargs: bot.remember_queue_open(confirmation)
        with patch.multiple(bot, queue_is_open=False, pending_queue_target=None, last_queue_emit=0,
                            notifications=SimpleQueue()), \
                patch.object(bot, "current_queue_schedule", return_value=schedule), \
                patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 4, 18, tzinfo=bot.EASTERN)
            bot.sync_queue_open(client)
            bot.remember_queue_open(confirmation)
            self.assertIsNone(bot.pending_queue_target)
            self.assertEqual(bot.notifications.get_nowait(), (bot.notify_alert, "Queue opened."))
            self.assertTrue(bot.notifications.empty())

    def test_failed_queue_command_can_retry(self):
        schedule = bot.parse_queue_schedule({"QUEUE_AUTO_OPEN": "true"})
        client = Mock()
        client.emit.side_effect = [RuntimeError("disconnected"), None]
        with patch.multiple(bot, queue_is_open=False, pending_queue_target=None, last_queue_emit=0), \
                patch.object(bot, "current_queue_schedule", return_value=schedule), \
                patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 4, 18, tzinfo=bot.EASTERN)
            bot.sync_queue_open(client)
            self.assertIsNone(bot.pending_queue_target)
            bot.sync_queue_open(client)
            self.assertEqual(client.emit.call_count, 2)
            self.assertIs(bot.pending_queue_target, True)

    def test_master_switch_still_disables_all_changes(self):
        for when, is_open in (("2026-11-04T18:00", False), ("2026-11-04T20:00", True)):
            with self.subTest(when=when):
                self.sync(when, auto_open=False, is_open=is_open).emit.assert_not_called()

    def test_auto_close_reloads_and_can_be_reenabled(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(bot, "_schedule_mtime", bot._SCHEDULE_UNSET), \
                patch.object(bot, "_schedule", bot.DEFAULT_QUEUE_SCHEDULE), \
                patch.object(bot, "_schedule_error", None):
            path = Path(folder) / ".env"
            for index, setting in enumerate(("false", "true"), start=1):
                path.write_text(f"QUEUE_AUTO_OPEN=true\nQUEUE_AUTO_CLOSE={setting}\n")
                os.utime(path, (index, index))
                self.assertEqual(bot.current_queue_schedule(path)[1], setting == "true")


if __name__ == "__main__":
    unittest.main()
