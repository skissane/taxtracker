import tempfile
import zipfile
from pathlib import Path

import click
from click.testing import CliRunner
from django.test import TestCase

from lifetracker.cli.filter_zip import filter_zip, filter_zip_command


class FilterZipTests(TestCase):
    def test_filters_matching_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("keep.txt", b"keep")
                zf.writestr("reject_me.txt", b"reject")
            output_zip = Path(tmp) / "out.zip"

            filter_zip(str(input_zip), str(output_zip), "reject")

            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(zf.namelist(), ["keep.txt"])
                self.assertEqual(zf.read("keep.txt"), b"keep")

    def test_missing_input_file_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(click.ClickException) as ctx:
                filter_zip(str(Path(tmp) / "missing.zip"), str(Path(tmp) / "out.zip"), "x")
            self.assertIn("missing.zip' was not found", str(ctx.exception))

    def test_missing_output_directory_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("keep.txt", b"keep")
            output_zip = Path(tmp) / "missing" / "out.zip"

            with self.assertRaises(click.ClickException) as ctx:
                filter_zip(str(input_zip), str(output_zip), "x")
            self.assertIn("out.zip' was not found", str(ctx.exception))

    def test_bad_zip_file_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_zip = Path(tmp) / "bad.zip"
            bad_zip.write_bytes(b"not a zip")
            with self.assertRaises(click.ClickException) as ctx:
                filter_zip(str(bad_zip), str(Path(tmp) / "out.zip"), "x")
            self.assertIn("is not a valid ZIP file", str(ctx.exception))

    def test_same_input_and_output_path_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "same.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("keep.txt", b"keep")

            with self.assertRaises(click.ClickException) as ctx:
                filter_zip(str(path), str(path), "x")

            self.assertIn("are the same file", str(ctx.exception))

    def test_hard_linked_input_and_output_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("keep.txt", b"keep")
            link = Path(tmp) / "out.zip"
            link.hardlink_to(path)

            with self.assertRaises(click.ClickException):
                filter_zip(str(path), str(link), "x")

            with zipfile.ZipFile(path) as zf:
                self.assertEqual(zf.namelist(), ["keep.txt"])

    def test_cli_end_to_end(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("keep.txt", b"keep")
                zf.writestr("reject_me.txt", b"reject")
            output_zip = Path(tmp) / "out.zip"

            result = runner.invoke(filter_zip_command, [str(input_zip), str(output_zip), "reject"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("kept: 1", result.output)
            self.assertIn("skipped: 1", result.output)
