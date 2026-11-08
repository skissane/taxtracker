import contextlib
import io
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scripts.validate_uv_lock_os_arch import PlatformClassification, _classify_platform, _split_platform
from scripts.validate_uv_lock_os_arch import main as os_arch_main


class ValidateUvLockOsArchTests(SimpleTestCase):
    def test_split_platform(self) -> None:
        cases = [
            ("any", ("any", None)),
            ("win32", ("win", "x86")),
            ("win_amd64", ("win", "amd64")),
            ("linux_armv7l", ("linux", "armv7l")),
            ("manylinux1_x86_64", ("manylinux", "x86_64")),
            ("manylinux2014_ppc64le", ("manylinux", "ppc64le")),
            ("manylinux_2_17_s390x", ("manylinux", "s390x")),
            ("musllinux_1_2_aarch64", ("musllinux", "aarch64")),
            ("macosx_10_15_x86_64", ("macosx", "x86_64")),
            ("macosx_11_0_arm64", ("macosx", "arm64")),
            ("ios_13_0_arm64_iphoneos", ("ios", "arm64_iphoneos")),
            ("android_21_arm64_v8a", ("android", "arm64_v8a")),
            ("pyemscripten_2026_0_wasm32", ("pyemscripten", "wasm32")),
        ]
        for platform, expected in cases:
            with self.subTest(platform):
                self.assertEqual(_split_platform(platform), expected)

    def test_split_platform_rejects_unrecognised(self) -> None:
        with self.assertRaisesRegex(ValueError, "unrecognised"):
            _split_platform("2_17_x86_64")

    def test_classify_platform(self) -> None:
        supported = PlatformClassification.SUPPORTED
        tolerated = PlatformClassification.TOLERATED
        unsupported = PlatformClassification.UNSUPPORTED
        cases = [
            (("any", None), supported),
            (("manylinux", "x86_64"), supported),
            (("manylinux", "aarch64"), supported),
            (("musllinux", "x86_64"), supported),
            (("musllinux", "aarch64"), supported),
            (("macosx", "x86_64"), supported),
            (("macosx", "arm64"), supported),
            (("macosx", "universal2"), supported),
            (("linux", "x86_64"), supported),
            (("macosx", "intel"), supported),
            (("macosx", "i386"), supported),
            (("ios", "arm64_iphoneos"), tolerated),
            (("ios", "arm64_iphonesimulator"), tolerated),
            (("ios", "x86_64_iphonesimulator"), tolerated),
            (("pyemscripten", "wasm32"), tolerated),
            (("manylinux", "s390x"), unsupported),
            (("musllinux", "armv7l"), unsupported),
            (("linux", "riscv64"), unsupported),
            (("win", "amd64"), unsupported),
            (("android", "arm64_v8a"), unsupported),
        ]
        for os_arch, expected in cases:
            with self.subTest(os_arch):
                self.assertEqual(_classify_platform(os_arch), expected)

    def _run(self, *filenames: str) -> tuple[int, str]:
        """Run main() in a temp dir whose uv.lock has one package with a wheel
        for each of the given filenames; return its exit code and stdout."""
        return self._run_wheels(*(f'url = "https://example.com/{f}"' for f in filenames))

    def _run_wheels(self, *wheels: str) -> tuple[int, str]:
        """Like _run, but each wheel is given as the body of its TOML inline
        table, e.g. `path = "dist/pkg-1.0-py3-none-any.whl"`."""
        lock = (
            '[[package]]\nname = "pkg"\nversion = "1.0"\nwheels = [\n'
            + "".join(f"  {{ {w} }},\n" for w in wheels)
            + "]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "uv.lock").write_text(lock, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.chdir(path), contextlib.redirect_stdout(stdout):
                code = os_arch_main()
        return code, stdout.getvalue()

    def test_allowed_platforms_pass(self) -> None:
        code, stdout = self._run(
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-cp314-cp314-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            "pkg-1.0-cp314-cp314-macosx_11_0_arm64.whl",
        )
        self.assertEqual(code, 0)
        self.assertIn("only has wheels for allowed platforms", stdout)

    def test_disallowed_platforms_fail(self) -> None:
        code, stdout = self._run(
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-cp314-cp314-win_amd64.whl",
            "pkg-1.0-cp314-cp314-manylinux_2_17_s390x.manylinux2014_s390x.whl",
            "pkg-1.0-cp314-cp314-weird.whl",
        )
        self.assertEqual(code, 1)
        self.assertIn("pkg-1.0-cp314-cp314-win_amd64.whl (win_amd64)", stdout)
        self.assertIn("(manylinux_2_17_s390x)", stdout)
        self.assertIn("(manylinux2014_s390x)", stdout)
        self.assertIn("(weird)", stdout)
        self.assertNotIn("any.whl", stdout)

    def test_local_wheels_checked(self) -> None:
        code, stdout = self._run_wheels(
            'path = "dist/pkg-1.0-py3-none-any.whl"',
            'path = "dist/pkg-1.0-cp314-cp314-win_amd64.whl"',
            'filename = "pkg-1.0-cp314-cp314-manylinux_2_17_s390x.whl"',
        )
        self.assertEqual(code, 1)
        self.assertIn("pkg-1.0-cp314-cp314-win_amd64.whl (win_amd64)", stdout)
        self.assertIn("(manylinux_2_17_s390x)", stdout)
        self.assertNotIn("any.whl", stdout)

    def test_wheel_without_location_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no url, path or filename"):
            self._run_wheels('hash = "sha256:00"')
