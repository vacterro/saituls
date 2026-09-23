"""Discriminating regressions for the native history acceptance oracle."""
import unittest
from live_native_queue import inspect_history


class HistoryTests(unittest.TestCase):
    def run_history(self, events):
        admissions = [{'id': f'msg_{n}', 'sessionID': 'ses_test'} for n in range(1, 6)]
        rows = []
        for seq, (kind, n, finish) in enumerate(events, 1):
            rows.append({'durable': {'seq': seq}, 'type': 'session.next.' + kind,
                         'data': {'sessionID': 'ses_test', 'messageID': f'msg_{n}',
                                  'assistantMessageID': f'assistant_{n}', 'finish': finish}})
        return inspect_history({'data': rows}, admissions)

    def test_prompted_is_not_busy(self):
        self.assertEqual(self.run_history([('prompted', 1, None)]), ([], None))

    def test_started_is_busy(self):
        self.assertEqual(self.run_history([('prompted', 1, None), ('step.started', 1, None)]),
                         ([], 'msg_1'))

    def test_real_gap_race_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'before previous'):
            self.run_history([('prompted', 1, None), ('prompted', 2, None), ('step.started', 1, None)])

    def test_five_admissions_or_promotions_are_not_pass(self):
        self.assertEqual(self.run_history([]), ([], None))
        with self.assertRaises(RuntimeError):
            self.run_history([('prompted', n, None) for n in range(1, 6)])

    def test_successful_fifo(self):
        events = [(kind, n, finish) for n in range(1, 6) for kind, finish in
                  [('prompted', None), ('step.started', None), ('step.ended', 'stop')]]
        self.assertEqual(self.run_history(events), ([f'msg_{n}' for n in range(1, 6)], None))

    def test_provider_failure_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'not happy-path'):
            self.run_history([('prompted', 1, None), ('step.started', 1, None), ('step.failed', 1, None)])

    def test_tool_step_is_not_terminal(self):
        with self.assertRaisesRegex(RuntimeError, 'before previous'):
            self.run_history([('prompted', 1, None), ('step.started', 1, None),
                              ('step.ended', 1, 'tool-calls'), ('prompted', 2, None)])

    def test_duplicate_and_concurrent_steps_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'concurrent'):
            self.run_history([('prompted', 1, None), ('step.started', 1, None), ('step.started', 2, None)])


if __name__ == '__main__':
    unittest.main()
