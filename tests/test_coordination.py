from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from fulcrum.coordination import ProcessLock
from tests import local_python


class CoordinationTests(unittest.TestCase):
    def test_lock_descriptor_survives_parent_exit_until_child_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "state.lock"
            ready_path = root / "child.ready"
            command = [
                sys.executable,
                "-B",
                "-c",
                (
                    "import os,subprocess,sys;"
                    "from pathlib import Path;"
                    "from fulcrum.coordination import ProcessLock,inherited_lock_fds;"
                    f"lock=ProcessLock(Path({str(lock_path)!r}));lock.__enter__();"
                    "subprocess.Popen([sys.executable,'-c',"
                    f"\"from pathlib import Path;import time;Path({str(ready_path)!r}).write_text('ready');time.sleep(0.4)\""
                    "],pass_fds=inherited_lock_fds());os._exit(0)"
                ),
            ]
            with local_python(command):
                parent = subprocess.Popen(command, cwd=Path(__file__).parents[1])
            parent.wait(timeout=2)
            deadline = time.monotonic() + 1
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_path.exists())
            with self.assertRaises(Exception):
                with ProcessLock(lock_path, blocking=False):
                    pass
            time.sleep(0.5)
            with ProcessLock(lock_path, blocking=False):
                pass


if __name__ == "__main__":
    unittest.main()
