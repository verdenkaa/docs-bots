"""S3 startup configuration and safe migration of existing stand credentials."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MANAGER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MANAGER_DIR))
import manager


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-manager-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / ".env"
        root_patch = patch.object(manager, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = patch.dict(os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    @patch.object(manager, "run", return_value="")
    def test_new_stand_generates_s3_secret_once(self, run):
        manager.initialize_env(["docker"], announce=False)
        original = self.path.read_bytes()
        settings = dict(line.split("=", 1) for line in original.decode().splitlines() if "=" in line)
        self.assertGreaterEqual(len(settings["S3_SECRET_KEY"]), 32)
        self.assertNotEqual(settings["S3_SECRET_KEY"], settings["POSTGRES_PASSWORD"])
        manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), original)
        run.assert_called_once()

    @patch.object(manager, "run", return_value="")
    def test_upgrade_preserves_existing_bytes_and_secrets(self, run):
        original = b"# Existing stand\r\nPOSTGRES_PASSWORD=keep-this\r\nTELEGRAM_BOT_TOKEN=keep-too"
        self.path.write_bytes(original)
        manager.initialize_env(["docker"], announce=False)
        upgraded = self.path.read_bytes()
        self.assertTrue(upgraded.startswith(original))
        self.assertIn(b"\nS3_SECRET_KEY=", upgraded)
        self.assertIn("label=com.docker.compose.volume=s3_data", run.call_args.args[0])
        manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), upgraded)
        run.assert_called_once()

    @patch.object(manager, "run")
    def test_explicit_secret_including_empty_is_not_replaced(self, run):
        for value in ("existing-secret", "", "'quoted secret'"):
            with self.subTest(value=value):
                original = f"\ufeff# settings\n export S3_SECRET_KEY = {value}\n".encode("utf-8")
                self.path.write_bytes(original)
                manager.initialize_env(["docker"], announce=False)
                self.assertEqual(self.path.read_bytes(), original)
        run.assert_not_called()

    @patch.object(manager, "run")
    def test_shell_secret_is_respected(self, run):
        self.path.write_text("POSTGRES_PASSWORD=keep\n", encoding="utf-8")
        original = self.path.read_bytes()
        os.environ["S3_SECRET_KEY"] = "configured-in-shell"
        manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), original)
        run.assert_not_called()

    @patch.object(manager, "run", return_value="rag-hackathon_s3_data\n")
    def test_missing_secret_with_s3_data_requires_restore(self, run):
        self.path.write_text("POSTGRES_PASSWORD=keep\n", encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(manager.ManagerError, "Restore S3_SECRET_KEY"):
            manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), original)

    @patch.object(manager, "run", return_value="rag-hackathon_postgres_data\n")
    def test_missing_env_with_existing_volumes_is_not_regenerated(self, run):
        with self.assertRaisesRegex(manager.ManagerError, "Restore the original .env"):
            manager.initialize_env(["docker"], announce=False)
        self.assertFalse(self.path.exists())


class StartupTests(unittest.TestCase):
    def test_s3_is_started_in_every_scenario_before_application(self):
        for mode in ("infra", "app", "full"):
            with self.subTest(mode=mode):
                backend = manager.Manager(["docker"], local=False)
                with patch.object(backend, "validate"), patch.object(backend, "start") as start, \
                        patch.object(backend, "stop"), patch.object(backend, "status"), \
                        patch.object(backend, "check_monitoring"):
                    backend.scenario(mode)
                self.assertIn("s3", start.call_args_list[0].args[0])
                if mode != "infra":
                    self.assertEqual(start.call_args_list[1].args[0], manager.APP)

    def test_s3_requires_health_and_reports_api_and_console(self):
        container = manager.Container("test", "running", "starting", 0, "now", 0)
        self.assertFalse(container.ready("s3"))
        container.health = "healthy"
        self.assertTrue(container.ready("s3"))
        container.ports = {"9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19000"}],
                           "9001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19001"}]}
        self.assertEqual(container.endpoints("s3"), [
            ("s3:9000", "http://127.0.0.1:19000/"),
            ("s3:9001", "http://127.0.0.1:19001/"),
        ])


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is required to validate Compose")
class ComposeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-compose-test-")
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / "test.env"
        self.env_file.write_text("", encoding="utf-8")
        self.env = dict(os.environ, POSTGRES_PASSWORD="test-postgres", RABBITMQ_PASSWORD="test-rabbit",
                        GRAFANA_PASSWORD="test-grafana", S3_ACCESS_KEY="test-access",
                        S3_SECRET_KEY="test-secret", S3_PORT="19000", S3_CONSOLE_PORT="19001",
                        CORE_IMAGE="", WORKER_IMAGE="")

    def config(self, local=False):
        command = ["docker", "compose", "--env-file", str(self.env_file), "-f", str(MANAGER_DIR / "compose.yaml")]
        if local:
            command += ["-f", str(MANAGER_DIR / "compose.local.yaml")]
        command += ["--profile", "*", "config", "--format", "json"]
        return subprocess.run(command, env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_server_and_local_configuration(self):
        for local in (False, True):
            with self.subTest(local=local):
                result = self.config(local)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = json.loads(result.stdout)
                self.assertEqual(set(config["services"]), set(manager.SERVICES))
                s3 = config["services"]["s3"]
                self.assertEqual(s3["image"], "docker.io/rustfs/rustfs:1.0.5")
                self.assertEqual(s3["volumes"][0]["source"], "s3_data")
                self.assertEqual(s3["volumes"][0]["target"], "/data")
                self.assertEqual(s3["environment"]["RUSTFS_SECRET_KEY"], "test-secret")
                if local:
                    self.assertEqual({(p["host_ip"], p["published"], p["target"]) for p in s3["ports"]}, {
                        ("127.0.0.1", "19000", 9000), ("127.0.0.1", "19001", 9001),
                    })
                else:
                    self.assertFalse(s3.get("ports"))
                for role in manager.APP:
                    app = config["services"][role]
                    self.assertEqual(app["depends_on"]["s3"]["condition"], "service_healthy")
                    self.assertEqual(app["environment"]["S3_ENDPOINT_URL"], "http://s3:9000")
                    self.assertEqual(app["environment"]["S3_ACCESS_KEY"], s3["environment"]["RUSTFS_ACCESS_KEY"])
                    self.assertEqual(app["environment"]["S3_SECRET_KEY"], s3["environment"]["RUSTFS_SECRET_KEY"])

    def test_empty_s3_secret_is_rejected(self):
        self.env["S3_SECRET_KEY"] = ""
        result = self.config()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("S3_SECRET_KEY", result.stderr)


if __name__ == "__main__":
    unittest.main()
