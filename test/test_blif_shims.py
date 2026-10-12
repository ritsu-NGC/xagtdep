import itertools
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "doc/date2027/example_dag.blif"


def evaluate_blif(path, inputs):
    signals = {}
    outputs = []
    pending = None

    def flush():
        if pending is None:
            return
        fanins, output, rows = pending
        bits = "".join(str(int(signals[name])) for name in fanins)
        signals[output] = any(
            value == "1" and all(a == "-" or a == b for a, b in zip(cube, bits))
            for cube, value in rows
        )

    for line in path.read_text().splitlines():
        tokens = line.split()
        if not tokens or tokens[0].startswith("#"):
            continue
        if tokens[0].startswith("."):
            flush()
            pending = None
            if tokens[0] == ".inputs":
                signals.update(zip(tokens[1:], inputs))
            elif tokens[0] == ".outputs":
                outputs.extend(tokens[1:])
            elif tokens[0] == ".names":
                pending = (tokens[1:-1], tokens[-1], [])
        elif pending is not None:
            pending[2].append(
                (tokens[0], tokens[1]) if len(tokens) == 2 else ("", tokens[0])
            )
    flush()
    return tuple(signals[name] for name in outputs)


class BlifShimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.counter = ROOT / "build/blif_to_tcount"
        cls.optimizer = ROOT / "build/blif_optimize"
        if not cls.counter.is_file() or not cls.optimizer.is_file():
            raise unittest.SkipTest("Build blif_to_tcount and blif_optimize first")

    def run_json(self, binary, *args, exit_code=0):
        result = subprocess.run(
            [str(binary), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        return json.loads(result.stdout)

    def test_counter_matches_pre_refactor_fixture_counts(self):
        self.assertEqual(
            self.run_json(self.counter, "--strategy", "structural", FIXTURE),
            {
                "ok": True,
                "t_count": 42,
                "cnot_count": 46,
                "h_count": 20,
                "total": 120,
                "qubits": 12,
                "and_pos": 1,
                "strategy": "structural",
            },
        )

    def test_all_passes_preserve_fixture_function(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("const_fanin", "cut_rewriting", "resub"):
                with self.subTest(name=name):
                    output = pathlib.Path(directory) / f"{name}.blif"
                    summary = self.run_json(
                        self.optimizer, FIXTURE, output, f"--pass={name}"
                    )
                    self.assertEqual(
                        set(summary),
                        {
                            "ok", "pass", "nodes_before", "and_before",
                            "nodes_after", "and_after",
                        },
                    )
                    self.assertTrue(summary["ok"])
                    self.assertEqual(summary["pass"], name)
                    self.assertEqual(summary["nodes_before"], 9)
                    self.assertEqual(summary["and_before"], 7)
                    self.assertLessEqual(summary["nodes_after"], 9)
                    self.assertLessEqual(summary["and_after"], 7)
                    for inputs in itertools.product((False, True), repeat=4):
                        self.assertEqual(
                            evaluate_blif(output, inputs),
                            evaluate_blif(FIXTURE, inputs),
                        )

    def test_default_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = self.run_json(
                self.optimizer, FIXTURE, pathlib.Path(directory) / "output.blif"
            )
            self.assertEqual(summary["pass"], "const_fanin")

    def test_buffer_and_not_match_pre_refactor_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "unary.blif"
            source.write_text(
                ".model unary\n.inputs a b\n.outputs o\n"
                ".names a buf\n1 1\n.names b inv\n0 1\n"
                ".names buf inv o\n11 1\n.end\n"
            )
            self.assertEqual(
                self.run_json(self.counter, "--strategy", "structural", source),
                {
                    "ok": True,
                    "t_count": 6,
                    "cnot_count": 6,
                    "h_count": 2,
                    "total": 14,
                    "qubits": 3,
                    "and_pos": 1,
                    "strategy": "structural",
                },
            )

    def test_bad_args_and_missing_input(self):
        self.assertFalse(self.run_json(self.optimizer, exit_code=2)["ok"])
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / 'missing"file.blif'
            output = pathlib.Path(directory) / "output.blif"
            summary = self.run_json(self.optimizer, missing, output, exit_code=1)
            self.assertFalse(summary["ok"])
            self.assertIn(str(missing), summary["error"])
            self.assertFalse(output.exists())
            summary = self.run_json(
                self.optimizer, FIXTURE, output, "--pass=unknown", exit_code=1
            )
            self.assertFalse(summary["ok"])
            self.assertFalse(output.exists())

    def test_both_shims_reject_unsupported_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "unsupported.blif"
            source.write_text(
                ".model unsupported\n.inputs a b\n.outputs o\n"
                ".names a b o\n00 1\n.end\n"
            )
            counter = self.run_json(self.counter, source, exit_code=1)
            optimizer = self.run_json(
                self.optimizer, source, pathlib.Path(directory) / "out.blif",
                exit_code=1,
            )
            self.assertEqual(counter, optimizer)
            self.assertIn("unsupported 2-input cover", counter["error"])

    def test_output_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            for output in (
                pathlib.Path(directory),
                pathlib.Path(directory) / "missing" / "out.blif",
            ):
                with self.subTest(output=output):
                    summary = self.run_json(
                        self.optimizer, FIXTURE, output, exit_code=1
                    )
                    self.assertFalse(summary["ok"])
                    self.assertTrue(summary["error"])


if __name__ == "__main__":
    unittest.main()
