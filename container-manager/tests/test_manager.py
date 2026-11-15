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


COMPOSE_COMMAND = (
    ['docker', 'compose'] if shutil.which('docker') else
    ['docker-compose'] if shutil.which('docker-compose') else
    ['podman', 'compose'] if shutil.which('podman') else None
)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-manager-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / ".env"
        self.manager_root = self.root / "container-manager"
        self.manager_root.mkdir()
        root_patch = patch.object(manager, "ROOT", self.manager_root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        config_patch = patch.object(manager, "ENV_FILE", self.path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        env_patch = patch.dict(os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    @patch.object(manager, "run", return_value="")
    def test_new_stand_generates_s3_secret_once(self, run):
        manager.initialize_env(["docker"], announce=False)
        original = self.path.read_bytes()
        settings = dict(line.split("=", 1) for line in original.decode().splitlines() if "=" in line)
        self.assertGreaterEqual(len(settings["S3_SECRET_KEY"]), 32)
        self.assertNotEqual(settings["S3_SECRET_KEY"], settings["PGPASSWORD"])
        manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), original)
        run.assert_called_once()

    @patch.object(manager, "run", return_value="")
    def test_upgrade_preserves_existing_bytes_and_secrets(self, run):
        original = b"# Existing stand\r\nPGPASSWORD=keep-this\r\nTELEGRAM_BOT_TOKEN=keep-too"
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
        self.path.write_text("PGPASSWORD=keep\n", encoding="utf-8")
        original = self.path.read_bytes()
        os.environ["S3_SECRET_KEY"] = "configured-in-shell"
        manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), original)
        run.assert_not_called()

    @patch.object(manager, "run", return_value="rag-hackathon_s3_data\n")
    def test_missing_secret_with_s3_data_requires_restore(self, run):
        self.path.write_text("PGPASSWORD=keep\n", encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(manager.ManagerError, "Restore S3_SECRET_KEY"):
            manager.initialize_env(["docker"], announce=False)
        self.assertEqual(self.path.read_bytes(), original)

    @patch.object(manager, "run", return_value="rag-hackathon_postgres_data\n")
    def test_missing_env_with_existing_volumes_is_not_regenerated(self, run):
        with self.assertRaisesRegex(manager.ManagerError, "Restore the original .env"):
            manager.initialize_env(["docker"], announce=False)
        self.assertFalse(self.path.exists())

    @patch.object(manager, "run")
    def test_legacy_manager_env_is_not_silently_replaced(self, run):
        (self.manager_root / '.env').write_text('POSTGRES_PASSWORD=old-secret\n', encoding='utf-8')
        with self.assertRaisesRegex(manager.ManagerError, 'Legacy'):
            manager.initialize_env(['docker'], announce=False)
        self.assertFalse(self.path.exists())
        run.assert_not_called()

    def test_start_and_build_use_same_root_file_and_project(self):
        self.path.write_text('RAG_COMPOSE_PROJECT=custom-project\n', encoding='utf-8')
        backend = manager.Manager(['docker'], local=True)
        for command in (backend.prefix, backend.scaffold_prefix):
            self.assertEqual(command[command.index('--env-file') + 1], str(self.path.resolve()))
            self.assertEqual(command[command.index('-p') + 1], 'custom-project')
        self.assertIn(str(self.manager_root / 'compose.scaffold.yaml'), backend.scaffold_prefix)

    def test_explicit_file_is_used_for_interpolation_and_service_env_file(self):
        alternate = self.root / 'selected.env'
        alternate.write_text('RAG_COMPOSE_PROJECT=selected-project\n', encoding='utf-8')
        os.environ['RAG_ENV_FILE'] = str(alternate)
        backend = manager.Manager(['docker'], local=False)
        self.assertEqual(backend.project, 'selected-project')
        with patch.object(manager.subprocess, 'run') as process:
            process.return_value.returncode = 0
            manager.run(['docker', 'compose', 'config'])
        self.assertEqual(process.call_args.kwargs['env']['RAG_ENV_FILE'], str(alternate.resolve()))


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
                    # сервисы моделей поднимаются до приложения: core проверяет роли моделей при старте
                    self.assertEqual(start.call_args_list[1].args[0], manager.GROUPS["models"])
                    self.assertEqual(start.call_args_list[2].args[0], manager.APP)

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


@unittest.skipUnless(COMPOSE_COMMAND, "A Compose provider is required to validate Compose")
class ComposeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-compose-test-")
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / "test.env"
        self.env_file.write_text("RAG_CORE_MAX_ATTEMPTS=7\nINGESTION_CHUNK_CHARACTERS=1234\n", encoding="utf-8")
        self.env = dict(os.environ, RAG_ENV_FILE=str(self.env_file), PGPASSWORD="test-postgres", RABBITMQ_PASSWORD="test-rabbit",
                        GRAFANA_USER="custom-admin", PGUSER="custom-pg", PGDATABASE="custom-db",
                        RABBITMQ_USER="custom-rabbit", RABBITMQ_PUBLISHED_PORT="15672",
                        GRAFANA_PASSWORD="test-grafana", S3_ACCESS_KEY="test-access",
                        S3_SECRET_KEY="test-secret", S3_PORT="19000", S3_CONSOLE_PORT="19001",
                        CORE_IMAGE="", WORKER_IMAGE="", DETECTOR_IMAGE="", DETECTOR_PORT="")

    def config(self, local=False, scaffold=False):
        command = [*COMPOSE_COMMAND, "--env-file", str(self.env_file), "-f", str(MANAGER_DIR / "compose.yaml")]
        if local:
            command += ["-f", str(MANAGER_DIR / "compose.local.yaml")]
        if scaffold:
            command += ["-f", str(MANAGER_DIR / "compose.scaffold.yaml")]
        command += ["--profile", "*", "config", "--format", "json"]
        return subprocess.run(command, env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_server_and_local_configuration(self):
        for local in (False, True):
            with self.subTest(local=local):
                result = self.config(local)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = json.loads(result.stdout)
                self.assertEqual(set(config["services"]), set(manager.SERVICES))
                postgres = config['services']['postgres']['environment']
                self.assertEqual(postgres['POSTGRES_USER'], 'custom-pg')
                self.assertEqual(postgres['POSTGRES_DB'], 'custom-db')
                self.assertEqual(postgres['POSTGRES_PASSWORD'], 'test-postgres')
                self.assertEqual(config['services']['rabbitmq']['environment']['RABBITMQ_DEFAULT_USER'], 'custom-rabbit')
                self.assertEqual(config['services']['grafana']['environment']['GF_SECURITY_ADMIN_USER'], 'custom-admin')
                if local:
                    self.assertEqual(config['services']['rabbitmq']['ports'][0]['published'], '15672')
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
                detector = config["services"]["detector"]
                self.assertEqual(detector["image"], "localhost/rag-detector:scaffold")
                self.assertIn("healthcheck", detector)
                if local:
                    self.assertEqual({(p["host_ip"], p["published"], p["target"]) for p in detector["ports"]},
                                     {("127.0.0.1", "8090", 8080)})
                else:
                    self.assertFalse(detector.get("ports"))
                for role in manager.APP:
                    app = config["services"][role]
                    self.assertEqual(app['environment']['RAG_CORE_MAX_ATTEMPTS'], '7')
                    self.assertEqual(app['environment']['INGESTION_CHUNK_CHARACTERS'], '1234')
                    self.assertEqual(app['environment']['PGUSER'], postgres['POSTGRES_USER'])
                    self.assertEqual(app['environment']['PGPASSWORD'], postgres['POSTGRES_PASSWORD'])
                    self.assertEqual(app["depends_on"]["s3"]["condition"], "service_healthy")
                    self.assertEqual(app["environment"]["S3_ENDPOINT_URL"], "http://s3:9000")
                    self.assertEqual(app["environment"]["S3_ACCESS_KEY"], s3["environment"]["RUSTFS_ACCESS_KEY"])
                    self.assertEqual(app["environment"]["S3_SECRET_KEY"], s3["environment"]["RUSTFS_SECRET_KEY"])

    def test_empty_s3_secret_is_rejected(self):
        self.env["S3_SECRET_KEY"] = ""
        result = self.config()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("S3_SECRET_KEY", result.stderr)

    def test_build_uses_selected_images_and_does_not_embed_env_as_build_args(self):
        self.env.update(CORE_IMAGE='localhost/config-test-core:local',
                        WORKER_IMAGE='localhost/config-test-worker:local')
        result = self.config(scaffold=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        self.assertEqual(Path(services['detector']['build']['context']),
                         MANAGER_DIR.parent / 'app' / 'injection_detector')
        for role in manager.APP:
            service = services[role]
            self.assertEqual(service['image'], self.env[role.upper() + '_IMAGE'])
            self.assertEqual(Path(service['build']['context']), MANAGER_DIR.parent / 'app')
            self.assertEqual(service['build']['target'], role)
            self.assertFalse(service['build'].get('args'))
            self.assertFalse(service['build'].get('secrets'))
            self.assertEqual(service['environment']['RAG_CORE_MAX_ATTEMPTS'], '7')


if __name__ == "__main__":
    unittest.main()
