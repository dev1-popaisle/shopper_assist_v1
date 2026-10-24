import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "product_crawler", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


class CliTests(unittest.TestCase):
    def test_no_file_exits_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            proc = _run([], cwd)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("URL file is required", proc.stderr)
            self.assertEqual(list(cwd.iterdir()), [])

    def test_extra_args_create_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            proc = _run(["urls.txt", "extra"], cwd)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(list(cwd.iterdir()), [])

    def test_missing_file_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            proc = _run(["urls.txt"], cwd)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("URL file not found", proc.stderr)
            self.assertEqual(list(cwd.iterdir()), [])

    def test_non_http_url_in_file_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "urls.txt").write_text("ftp://example.com/products/shoe\n", encoding="utf-8")
            proc = _run(["urls.txt"], cwd)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("absolute http or https URL", proc.stderr)
            self.assertEqual([path.name for path in cwd.iterdir()], ["urls.txt"])
            self.assertEqual(
                (cwd / "urls.txt").read_text(encoding="utf-8"),
                "ftp://example.com/products/shoe\n",
            )


if __name__ == "__main__":
    unittest.main()
