from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class AppSmokeTests(unittest.TestCase):
    """Drive the real Streamlit script offline (no API key, so the rules fallback runs)."""

    def setUp(self):
        self._old = os.environ.get("GROQ_API_KEY")
        os.environ["GROQ_API_KEY"] = ""

    def tearDown(self):
        if self._old is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = self._old

    def _app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()

    def test_renders_without_exception(self):
        at = self._app()
        self.assertFalse(at.exception)

    def test_single_run_shows_recommendation(self):
        at = self._app()
        at.sidebar.selectbox[0].select("REQ-1005").run()
        at.sidebar.radio[1].set_value("single").run()
        at.button[0].click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("budget exception" in x.value.lower() for x in list(at.warning) + list(at.info)))

    def test_compare_mode_runs_both_architectures(self):
        at = self._app()
        at.sidebar.selectbox[0].select("REQ-1006").run()
        at.sidebar.radio[1].set_value("compare").run()
        at.button[0].click().run()
        self.assertFalse(at.exception)
        agree = [x.value for x in at.success] + [x.value for x in at.warning]
        self.assertTrue(any("architectures" in v.lower() for v in agree))


if __name__ == "__main__":
    unittest.main()
