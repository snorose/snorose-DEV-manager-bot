import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "src" / "app"
sys.path.insert(0, str(APP_ROOT))


class RuntimeConfigTest(unittest.TestCase):
    def test_mangum_lifespan_is_disabled_for_wsgi_adapter(self):
        import main

        self.assertEqual(main.handler.lifespan, "off")

    def test_python_runtime_dependencies_are_pinned(self):
        requirements = PROJECT_ROOT / "src" / "requirements.txt"

        unpinned = [
            line
            for line in requirements.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#") and "==" not in line
        ]

        self.assertEqual(unpinned, [])


if __name__ == "__main__":
    unittest.main()
