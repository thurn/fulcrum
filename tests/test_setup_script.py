from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests import local_launcher_stub

ROOT = Path(__file__).resolve().parents[1]


class SetupScriptTests(unittest.TestCase):
    def test_json_mode_keeps_provisioning_output_off_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            checkout = home / "fulcrum"
            scripts = checkout / "scripts"
            scripts.mkdir(parents=True)
            setup = scripts / "setup"
            shutil.copy2(ROOT / "scripts" / "setup", setup)
            (checkout / "requirements-dev.lock").touch()

            environment_bin = checkout / ".venv" / "bin"
            environment_bin.mkdir(parents=True)
            python = environment_bin / "python"
            python.write_text("#!/bin/sh\necho provisioning-output\n", encoding="utf-8")
            python.chmod(0o755)
            fulcrum = environment_bin / "fulcrum"
            fulcrum.write_text(
                "#!/bin/sh\nprintf '%s\\n' '{\"ok\":true}'\n", encoding="utf-8"
            )
            fulcrum.chmod(0o755)

            prerequisites = home / "bin"
            prerequisites.mkdir()
            for name in ("python3.12", "git", "bd", "dolt", "tg"):
                executable = prerequisites / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)

            with local_launcher_stub(setup):
                result = subprocess.run(
                    [str(setup), "--json"],
                    env={
                        **os.environ,
                        "HOME": str(home),
                        "PATH": os.pathsep.join(
                            (str(prerequisites), os.environ.get("PATH", ""))
                        ),
                    },
                    text=True,
                    capture_output=True,
                    timeout=10,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"ok": True})
            self.assertEqual(result.stderr.count("provisioning-output"), 2)


if __name__ == "__main__":
    unittest.main()
