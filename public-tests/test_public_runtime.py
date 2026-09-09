import tempfile
import unittest
from pathlib import Path

from agent_control_plane.store import Store


class PublicRuntimeTests(unittest.TestCase):
    def test_public_store_lifecycle_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.add_task("public", "public contract")
            self.assertEqual(store.task("public")["status"], "READY")
            store.cancel_task("public", "public test")
            self.assertEqual(store.task("public")["status"], "FAILED")
            store.close()


if __name__ == "__main__": unittest.main()
