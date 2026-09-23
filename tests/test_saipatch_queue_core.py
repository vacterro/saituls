"""
test_saipatch_queue_core.py - Unit test suite for queue_core.py.
Verifies all SQLite queue operations in isolation using a temporary database.
"""
import os
import sys
import json
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Scripts", "saipatch")))
import queue_core

SCHEMA_SQL = """
CREATE TABLE `session` (
  `id` text PRIMARY KEY,
  `directory` text NOT NULL,
  `title` text NOT NULL,
  `time_updated` integer NOT NULL
);

CREATE TABLE `session_input` (
  `id` text PRIMARY KEY,
  `session_id` text NOT NULL REFERENCES `session`(`id`) ON DELETE CASCADE,
  `prompt` text NOT NULL,
  `delivery` text NOT NULL,
  `admitted_seq` integer NOT NULL,
  `promoted_seq` integer,
  `time_created` integer NOT NULL
);
CREATE UNIQUE INDEX `session_input_session_admitted_seq_idx` ON `session_input` (`session_id`, `admitted_seq`);
CREATE INDEX `session_input_session_pending_delivery_seq_idx` ON `session_input` (`session_id`, `promoted_seq`, `delivery`, `admitted_seq`);

CREATE TABLE `event` (
  `id` text PRIMARY KEY,
  `aggregate_id` text NOT NULL,
  `seq` integer NOT NULL,
  `type` text NOT NULL,
  `data` text
);
"""

class TestQueueCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA_SQL)
        
        # Seed test sessions
        conn.execute("INSERT INTO session VALUES ('ses_test_1', 'V:/code/proj1', 'Test Project 1', 1000)")
        conn.execute("INSERT INTO session VALUES ('ses_test_2', 'V:/code/proj2', 'Test Project 2', 2000)")
        
        # Seed test items in ses_test_2
        # Item 1: already promoted
        conn.execute("INSERT INTO session_input VALUES ('msg_1', 'ses_test_2', '{\"text\":\"first prompt\"}', 'queue', 1, 2, 1000)")
        # Items 2, 3, 4: pending in queue
        conn.execute("INSERT INTO session_input VALUES ('msg_2', 'ses_test_2', '{\"text\":\"second prompt\"}', 'queue', 3, NULL, 1100)")
        conn.execute("INSERT INTO session_input VALUES ('msg_3', 'ses_test_2', '{\"text\":\"third prompt\"}', 'queue', 5, NULL, 1200)")
        conn.execute("INSERT INTO session_input VALUES ('msg_4', 'ses_test_2', '{\"text\":\"fourth prompt\"}', 'queue', 8, NULL, 1300)")
        
        # Seed events for ses_test_2
        conn.execute("INSERT INTO event VALUES ('evt_1', 'ses_test_2', 1, 'session.created.1', '{}')")
        conn.execute("INSERT INTO event VALUES ('evt_2', 'ses_test_2', 2, 'session.next.prompted.1', '{}')")
        conn.execute("INSERT INTO event VALUES ('evt_3', 'ses_test_2', 3, 'session.next.step.ended.2', '{\"finish\":\"tool-calls\"}')")
        
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_list_recent_sessions(self):
        sessions = queue_core.list_recent_sessions(db_path=self.db_path)
        self.assertEqual(len(sessions), 2)
        # ses_test_2 has time_updated 2000 > 1000
        self.assertEqual(sessions[0]["id"], "ses_test_2")
        self.assertEqual(sessions[0]["project_name"], "proj2")

    def test_get_session_status(self):
        status = queue_core.get_session_status("ses_test_2", db_path=self.db_path, check_live_process=False)
        self.assertEqual(status["state"], "RUNNING")
        self.assertEqual(status["active_turn"]["id"], "msg_1")
        self.assertEqual(status["active_turn"]["text"], "first prompt")

    def test_get_sent_history(self):
        history = queue_core.get_sent_history("ses_test_2", db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], "msg_1")
        self.assertEqual(history[0]["promoted_seq"], 2)

    def test_delete_session(self):
        ok = queue_core.delete_session("ses_test_1", db_path=self.db_path)
        self.assertTrue(ok)
        sessions = queue_core.list_recent_sessions(db_path=self.db_path)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], "ses_test_2")

    def test_get_pending_queue(self):
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0]["id"], "msg_2")
        self.assertEqual(queue[0]["text"], "second prompt")
        self.assertEqual(queue[1]["id"], "msg_3")
        self.assertEqual(queue[2]["id"], "msg_4")

    def test_swap_items_preserves_unique_constraint(self):
        # Swap msg_2 (seq 3) and msg_3 (seq 5)
        ok = queue_core.swap_items("msg_2", "msg_3", db_path=self.db_path)
        self.assertTrue(ok)
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual(queue[0]["id"], "msg_3")
        self.assertEqual(queue[0]["admitted_seq"], 3)
        self.assertEqual(queue[1]["id"], "msg_2")
        self.assertEqual(queue[1]["admitted_seq"], 5)

    def test_move_up_and_down(self):
        # Move msg_4 up (from index 2 to index 1)
        ok = queue_core.move_item_in_queue("ses_test_2", "msg_4", "up", db_path=self.db_path)
        self.assertTrue(ok)
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual([q["id"] for q in queue], ["msg_2", "msg_4", "msg_3"])
        
        # Move msg_2 down (from index 0 to index 1)
        ok = queue_core.move_item_in_queue("ses_test_2", "msg_2", "down", db_path=self.db_path)
        self.assertTrue(ok)
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual([q["id"] for q in queue], ["msg_4", "msg_2", "msg_3"])

    def test_edit_queued_prompt(self):
        ok = queue_core.edit_queued_prompt("msg_3", "edited prompt content", db_path=self.db_path)
        self.assertTrue(ok)
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        msg_3 = next(q for q in queue if q["id"] == "msg_3")
        self.assertEqual(msg_3["text"], "edited prompt content")

    def test_delete_queued_item(self):
        ok = queue_core.delete_queued_item("msg_3", db_path=self.db_path)
        self.assertTrue(ok)
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual(len(queue), 2)
        self.assertNotIn("msg_3", [q["id"] for q in queue])
        
        # Cannot delete promoted item msg_1
        ok_promoted = queue_core.delete_queued_item("msg_1", db_path=self.db_path)
        self.assertFalse(ok_promoted)

    def test_clear_queue(self):
        count = queue_core.clear_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual(count, 3)
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual(len(queue), 0)
        
        # Promoted item msg_1 was NOT deleted
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT count(*) FROM session_input WHERE id = 'msg_1'")
        self.assertEqual(c.fetchone()[0], 1)
        conn.close()

    def test_add_queued_prompt(self):
        new_id = queue_core.add_queued_prompt("ses_test_2", "new fifth prompt", db_path=self.db_path)
        self.assertIsNotNone(new_id)
        self.assertTrue(new_id.startswith("msg_"))
        
        queue = queue_core.get_pending_queue("ses_test_2", db_path=self.db_path)
        self.assertEqual(len(queue), 4)
        self.assertEqual(queue[-1]["id"], new_id)
        self.assertEqual(queue[-1]["text"], "new fifth prompt")
        self.assertGreater(queue[-1]["admitted_seq"], 8)

if __name__ == "__main__":
    unittest.main()
