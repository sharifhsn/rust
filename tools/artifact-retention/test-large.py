#!/usr/bin/env python3
"""Exercise runtime output parsing with actual short child processes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from large import Experiment, WORKLOADS


class RuntimeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        args = argparse.Namespace(out=self.root/"receipts", sources=self.root/"sources",
                                  cargo=Path(sys.executable), toolchain=Path(sys.executable).parent)
        self.experiment = Experiment(args)
        self.cfg = dict(name="default", features=None, flags="", opt="0", assertions="true")

    def measure(self, output):
        code = "import sys,time; time.sleep(0.05); sys.stdout.write(sys.argv[1])"
        with patch.object(self.experiment, "checkpoint"), patch.object(self.experiment, "status"):
            return self.experiment.measure("nushell", self.root, "control", self.cfg,
                                           "runtime-oracle", [sys.executable, "-c", code, output],
                                           census=False, runtime=True)

    def test_nushell_numeric_output_has_a_complete_receipt(self):
        row = self.measure("30\n")
        self.assertEqual(row["returncode"], 0)
        self.assertEqual(row["artifacts"], 0)
        self.assertEqual(row["fresh"], 0)
        self.assertEqual(row["binary_sha256"], hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest())
        receipts = json.loads((self.experiment.args.out/"results.json").read_text())
        self.assertEqual(receipts, [row])
        self.assertTrue((self.experiment.args.out/"commands"/row["log"]/"rss-samples.json").exists())

    def test_non_artifact_json_values_are_program_output(self):
        for output in ['[30]', 'null', '"30"', '{"value":30}']:
            with self.subTest(output=output), patch.dict(WORKLOADS["nushell"], oracle=output):
                row = self.measure(output)
                self.assertEqual(row["artifacts"], 0)

    def test_cargo_objects_are_still_counted(self):
        output = '{"reason":"compiler-artifact","fresh":true}\n30'
        with patch.dict(WORKLOADS["nushell"], oracle=output):
            row = self.measure(output)
        self.assertEqual(row["artifacts"], 1)
        self.assertEqual(row["fresh"], 1)

    def test_wrong_runtime_answer_still_fails_and_keeps_receipt(self):
        with self.assertRaises(AssertionError):
            self.measure("31\n")
        row = self.experiment.rows[0]
        self.assertEqual((self.experiment.args.out/"commands"/row["log"]/"stdout").read_text(), "31\n")
        self.assertEqual(json.loads((self.experiment.args.out/"results.json").read_text()), [row])


if __name__ == "__main__":
    unittest.main()
