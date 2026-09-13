import unittest


class JobShadowImportTests(unittest.TestCase):
    def test_runner_imports_without_package_shadow_collision(self):
        import job_shadow_runner  # noqa: F401


if __name__ == "__main__":
    unittest.main()
