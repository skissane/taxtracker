import tempfile
import zipfile
from email.message import EmailMessage
from pathlib import Path

import click
from click.testing import CliRunner
from django.test import TestCase

from lifetracker.cli.zip_eml_to_txt import convert_eml_zip_to_txt, zip_eml_to_txt_command
from tests.cli.test_zip_eml_to_pdf import _build_plain_eml


class ZipEmlToTxtTests(TestCase):
    def test_extracts_plain_text_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("plain.eml", _build_plain_eml())
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            count = convert_eml_zip_to_txt(str(input_zip), str(output_zip))

            self.assertEqual(count, 1)
            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(zf.namelist(), ["plain.txt"])
                content = zf.read("plain.txt").decode()
                self.assertIn("Subject: Plain Email", content)
                self.assertIn("Plain body text", content)

    def test_skips_eml_without_plain_text_body(self) -> None:
        html_only = EmailMessage()
        html_only["Subject"] = "HTML Only"
        html_only["From"] = "sender@example.com"
        html_only["To"] = "recipient@example.com"
        html_only["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
        html_only.set_content("<p>no plain part</p>", subtype="html")

        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("html_only.eml", bytes(html_only))
            output_zip = Path(tmp) / "out.zip"

            count = convert_eml_zip_to_txt(str(input_zip), str(output_zip))

            self.assertEqual(count, 0)
            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(zf.namelist(), [])

    def test_no_eml_members_raises_click_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            with self.assertRaises(click.ClickException):
                convert_eml_zip_to_txt(str(input_zip), str(output_zip))

            self.assertFalse(output_zip.exists())

    def test_cli_end_to_end(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("plain.eml", _build_plain_eml())
            output_zip = Path(tmp) / "out.zip"

            result = runner.invoke(zip_eml_to_txt_command, [str(input_zip), str(output_zip)])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Batch complete!", result.output)

    def test_cli_exits_non_zero_when_no_eml(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            result = runner.invoke(zip_eml_to_txt_command, [str(input_zip), str(output_zip)])

            self.assertEqual(result.exit_code, 1)
            self.assertIn("No .eml files found in the input ZIP.", result.output)
