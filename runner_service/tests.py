import base64
import json
import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from runner_service import views
from runner_service.project_config import load_project_catalog
from runner_service.pty_sessions import PtySessionManager


class ProjectConfigTests(SimpleTestCase):
    def test_invalid_catalog_prevents_startup_with_field_name(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"projects":{"demo":{"repoUrl":"https://github.com/example/demo.git",'
                '"branch":"main","runTimeoutMs":true}}}',
                encoding="utf-8",
            )
            with self.assertRaisesMessage(
                ImproperlyConfigured, "projects.demo.runTimeoutMs"
            ):
                load_project_catalog(path)

    def test_missing_config_explains_setup(self):
        with (
            TemporaryDirectory() as directory,
            self.assertRaisesMessage(ImproperlyConfigured, ".config.example.json"),
        ):
            load_project_catalog(Path(directory) / "config.json")


class RunnerTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temp = TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "hello.py").write_text(
            "import os\nprint('hello from Python')\n"
            "assert 'RUNNER_SHARED_TOKEN' not in os.environ\n",
            encoding="utf-8",
        )
        (repo / "Makefile").write_text("all:\n\t@echo built\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Runner Test",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=repo,
            check=True,
        )
        project = {"repoUrl": repo.as_uri(), "branch": "main"}
        cls.enterClassContext(
            override_settings(
                PROJECT_CATALOG={
                    "demo": {**project, "allowBash": True},
                    "restricted": project,
                }
            )
        )
        cls.enterClassContext(override_settings(WORKDIR_BASE=root / "workspaces"))
        cls.enterClassContext(override_settings(RUNNER_SHARED_TOKEN="test-token"))
        cls.enterClassContext(
            patch.dict(os.environ, {"RUNNER_SHARED_TOKEN": "service-secret"})
        )

    def setUp(self):
        self.headers = {
            "X-Runner-Token": "test-token",
            "X-Client-Id": self._testMethodName,
        }
        self.payload = {
            "projectId": "demo",
            "sessionId": "runner_" + self._testMethodName,
        }
        self.addCleanup(
            self.client.delete,
            "/projects/cleanup",
            self.payload,
            content_type="application/json",
            headers=self.headers,
        )

    def post(self, endpoint, **extra):
        return self.client.post(
            "/projects/" + endpoint,
            json.dumps({**self.payload, **extra}),
            content_type="application/json",
            headers=self.headers,
        )

    def test_catalog_and_checkout_build_run_cleanup(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/projects").status_code, 401)
        catalog = self.client.get("/projects", headers=self.headers)
        self.assertEqual(catalog.json(), {"projects": ["demo", "restricted"]})

        prepared = self.post("prepare").json()
        other = self.post("prepare", sessionId="other_session").json()
        self.addCleanup(
            self.client.delete,
            "/projects/cleanup",
            {**self.payload, "sessionId": "other_session"},
            content_type="application/json",
            headers=self.headers,
        )
        self.assertNotEqual(prepared["workdir"], other["workdir"])
        self.assertEqual(self.post("prepare").json()["workdir"], prepared["workdir"])
        self.assertEqual(
            self.post("fs/list", path=[]).json()["items"], prepared["items"]
        )
        self.assertEqual(self.post("fs/read", file="hello.py").json()["kind"], "code")
        self.assertIn("built", self.post("build").json()["output"])
        result = self.post("run", executable="hello.py", runtime="python").json()
        self.assertTrue(result["success"])
        self.assertIn("hello from Python", result["output"])
        response = self.client.delete(
            "/projects/cleanup",
            self.payload,
            content_type="application/json",
            headers=self.headers,
        )
        self.assertEqual(response.json()["deleted"], prepared["workdir"])
        self.assertFalse(Path(prepared["workdir"]).exists())
        self.assertTrue(Path(other["workdir"]).is_dir())

    def test_bash_requires_project_permission_and_token(self):
        self.assertEqual(
            self.post(
                "process/start", projectId="restricted", runtime="bash"
            ).status_code,
            403,
        )
        with override_settings(RUNNER_SHARED_TOKEN=""):
            self.assertEqual(
                self.post("process/start", runtime="bash").status_code, 403
            )

    def test_bash_uses_real_terminal_and_reports_exit(self):
        self.assertEqual(self.post("process/start", runtime="bash").status_code, 200)
        self.assertEqual(self.post("process/resize", rows=35, cols=90).status_code, 200)
        self.assertEqual(
            self.post(
                "process/input", data="stty size </dev/tty\nexit 7\n"
            ).status_code,
            200,
        )
        output = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.post("process/poll").json()
            output.extend(base64.b64decode(result["outputBase64"]))
            if not result["running"]:
                self.assertEqual(result["exitCode"], 7)
                self.assertIn(b"35 90", output)
                break
            time.sleep(0.02)
        else:
            self.fail(f"Bash did not exit. Terminal output: {output!r}")

    def test_pty_expires_without_polling(self):
        manager = PtySessionManager(max_buffer_bytes=1024, max_lifetime_seconds=0.1)
        manager.start(
            "expiry",
            ["sleep", "15"],
            settings.WORKDIR_BASE.parent,
            40,
            100,
            views._execution_environment(),
        )
        self.addCleanup(manager.stop, "expiry")
        session = manager._sessions["expiry"]
        session.process.wait(timeout=5)
        self.assertTrue(manager.poll("expiry")["expired"])

    def test_invalid_json_body_and_unknown_project(self):
        response = self.client.post(
            "/projects/build",
            "[]",
            content_type="application/json",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.post("run", projectId="unknown", executable="a").status_code, 404
        )
