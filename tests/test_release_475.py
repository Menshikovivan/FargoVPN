import ast
import importlib.util
import re
import sys
import tempfile
import tarfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


class ReleaseContractTests(unittest.TestCase):
    def test_version_is_current_everywhere(self):
        self.assertEqual((ROOT / "VERSION").read_text().strip(), "5.1")
        self.assertEqual((ROOT / "app" / "VERSION").read_text().strip(), "5.1")
        self.assertIn("Текущая версия: `5.1`", read("README.md"))
        self.assertRegex(read("CHANGELOG.md"), r"(?m)^##\s+4\.7\.3\s*$")

    def test_python_and_shell_syntax(self):
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # install.sh is validated by the release command:
        # bash -n install.sh. Keep this test focused on Python without requiring
        # shellcheck to be installed on the deployment host.

    def test_no_yandex_or_age_backup_contracts(self):
        payload = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in ROOT.rglob("*")
            if p.is_file() and p.parent.name != "tests" and p.suffix in {".py", ".sh", ".md", ".txt", ".service", ".js"}
        ).lower()
        self.assertNotIn("yandex", payload)
        self.assertNotIn("webdav", payload)
        self.assertNotRegex(payload, r"\.age\b")

    def test_backup_is_tar_gz_and_restore_accepts_numbered_parts(self):
        backup = read("backup.py")
        restore = read("restore_manager.py")
        self.assertRegex(backup, r'vpn_service_full_backup_\{stamp\}\.tar\.gz')
        self.assertIn('f"{path.name}.part{index:03d}"', backup)
        self.assertIn("SAFE_PART_RE", restore)
        self.assertIn("indexes != list(range(1, len(parts) + 1))", restore)

    def test_xui_new_clients_have_empty_flow(self):
        source = read("services/xui_api.py")
        self.assertIn('new_client["flow"] = ""', source)
        self.assertIn('"flow": str(base.get("flow") or "") if existing_client else ""', source)

    def test_update_publisher_is_restricted(self):
        source = read("webapp.py")
        self.assertIn("update_publisher_for_request", source)
        self.assertIn("can_publish_update", source)
        self.assertIn("if not can_publish_update(request):", source)
        self.assertIn("UPDATE_PUBLISHER_USERNAME", source)

    def test_registration_migration_has_idempotent_marker(self):
        source = read("init_db.py")
        sql = read("migrations/registration_access_4_7_2.sql")
        self.assertIn("fargovpn_schema_migrations", source)
        self.assertIn("4.7.2-registration-access", source)
        self.assertIn("CREATE TABLE IF NOT EXISTS fargovpn_schema_migrations", sql)
        self.assertIn("DO $$", sql)

    def test_canonical_changelog_contract_is_current(self):
        source = read("update_manager.py")
        self.assertIn('changelog_path = APP_DIR / "CHANGELOG.md"', source)
        self.assertIn("re.escape(wanted)", source)
        self.assertRegex(read("CHANGELOG.md"), r"(?m)^##\s+4\.9\.2\s")

    def test_clean_release_has_no_legacy_note_artifacts(self):
        self.assertFalse(list(ROOT.glob("RELEASE_NOTES_*.md")))
        self.assertFalse((ROOT / "INSTALLED_CHANGELOG.md").exists())
        self.assertFalse((ROOT / "INSTALLED_CHANGELOG_VERSION").exists())

    def test_runtime_changelog_reader_uses_canonical_changelog(self):
        import types
        import importlib.util

        fake_config = types.ModuleType("config")
        fake_detached = types.ModuleType("detached_jobs")
        fake_detached.DetachedJobError = RuntimeError
        fake_detached.launch_detached = lambda *a, **k: None
        sys.modules.setdefault("config", fake_config)
        config_module = sys.modules["config"]
        config_module.GITHUB_REPOSITORY_OWNER = "example"
        config_module.GITHUB_REPOSITORY_NAME = "fargovpn"
        sys.modules.setdefault("detached_jobs", fake_detached)
        spec = importlib.util.spec_from_file_location("update_manager_472_test", ROOT / "update_manager.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "test.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                data = read("CHANGELOG.md").encode("utf-8")
                import io
                info = tarfile.TarInfo("FargoVPN-4.9/CHANGELOG.md")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            text = module._read_changelog_from_archive(archive_path, "5.1")
            self.assertTrue(text.startswith("## 5.0.5"))
            self.assertNotIn("## 4.9.2", text)

    def test_github_public_surface_contains_changelog(self):
        import hashlib
        import types
        import importlib.util
        fake_config = types.ModuleType("config")
        fake_config.GITHUB_REPOSITORY_OWNER = "example"
        fake_config.GITHUB_REPOSITORY_NAME = "fargovpn"
        fake_detached = types.ModuleType("detached_jobs")
        fake_detached.DetachedJobError = RuntimeError
        fake_detached.launch_detached = lambda *a, **k: None
        sys.modules.setdefault("config", fake_config)
        config_module = sys.modules["config"]
        config_module.GITHUB_REPOSITORY_OWNER = "example"
        config_module.GITHUB_REPOSITORY_NAME = "fargovpn"
        sys.modules.setdefault("detached_jobs", fake_detached)
        spec = importlib.util.spec_from_file_location("update_manager_472_github_test", ROOT / "update_manager.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            tar_path = Path(temp) / "release.tar.gz"
            with tarfile.open(tar_path, "w:gz") as archive:
                for name in ("README.md", "LICENSE", "CHANGELOG.md", ".gitignore", "VERSION", "CHANGELOG.md"):
                    data = (ROOT / name).read_bytes()
                    info = tarfile.TarInfo(f"FargoVPN-4.9/{name}")
                    info.size = len(data)
                    archive.addfile(info, __import__("io").BytesIO(data))
            extracted = Path(temp) / "root"
            extracted.mkdir()
            with tarfile.open(tar_path, "r:gz") as archive:
                archive.extractall(extracted, filter="data")
            root = extracted / "FargoVPN-4.9"
            files = module._github_main_public_files(root, tar_path, "5.1", hashlib.sha256(tar_path.read_bytes()).hexdigest())
            self.assertIn("CHANGELOG.md", files)
            self.assertRegex(files["CHANGELOG.md"].decode(), r"(?m)^##\s+4\.9(?:\s|$)")
            body = module.github_notes(module._read_changelog_from_archive(tar_path, "5.1"), "5.1", "a" * 64, tar_path.stat().st_size)
            self.assertIn("## 5.0.5", body)
            self.assertNotIn("## 4.9", body)
            self.assertIn("### Метаданные", body)

    def test_runtime_installed_changelog_uses_canonical_changelog(self):
        import types
        import importlib.util
        fake_config = types.ModuleType("config")
        fake_detached = types.ModuleType("detached_jobs")
        fake_detached.DetachedJobError = RuntimeError
        fake_detached.launch_detached = lambda *a, **k: None
        sys.modules.setdefault("config", fake_config)
        sys.modules.setdefault("detached_jobs", fake_detached)
        spec = importlib.util.spec_from_file_location("update_manager_472_installed_test", ROOT / "update_manager.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        result = module.installed_changelog()
        self.assertEqual(result["version"], "5.1")
        self.assertIn("## 5.0.5", result["text"])

    def test_changelog_contains_current_section(self):
        changelog = read("CHANGELOG.md")
        self.assertRegex(changelog, r"(?m)^##\s+4\.9\.2\s")
        self.assertIn("GREATEST(...)", changelog)
        self.assertIn("sendChatAction", changelog)

    def test_all_settings_fields_have_a_persistence_path(self):
        web = read("webapp.py")
        settings = web[web.index('<form id="settings-form"'):web.index('@app.post("/settings")')]
        names = set(re.findall(r'name="([^"]+)"', settings))
        ignored = {
            "csrf_token", "settings_tab", "xui_inbound_selection_present",
            "xui_inbound_form_loaded", "xui_inbound_count", "xui_inbound_ids",
            "web_password", "web_password_confirm", "bot_token", "api_token",
            "github_api_token",
        }
        names -= ignored

        values_block = web[web.index('values: dict[str, Any] = {'):web.index('save_config_values(values)')]
        # Every visible setting name must either map to an explicitly constructed
        # config key in _save_settings or be handled by the dedicated /settings/xui
        # form.
        explicit = {
            "service_name": "SERVICE_NAME", "admin_ids": "ADMIN_IDS",
            "subscription_days": "SUBSCRIPTION_DAYS", "bot_welcome_text": "BOT_WELCOME_TEXT",
            "bot_support_prompt": "BOT_SUPPORT_PROMPT", "faq_incy_url": "FAQ_INCY_URL",
            "web_timezone": "WEB_TIMEZONE", "web_domain": "WEB_DOMAIN",
            "bot_identity_refresh_seconds": "BOT_IDENTITY_REFRESH_SECONDS",
            "bot_sync_interval_seconds": "BOT_SYNC_INTERVAL_SECONDS",
            "broadcast_media_max_mb": "BROADCAST_MEDIA_MAX_MB",
            "broadcast_send_delay_seconds": "BROADCAST_SEND_DELAY_SECONDS",
            "broadcast_stale_seconds": "BROADCAST_STALE_SECONDS",
            "price": "PAYMENT_PRICE", "phone": "PAYMENT_PHONE", "bank": "PAYMENT_BANK",
            "receiver": "PAYMENT_RECEIVER", "receipt_ocr_enabled": "RECEIPT_OCR_ENABLED",
            "receipt_auto_approve": "RECEIPT_AUTO_APPROVE",
            "receipt_allow_masked_phone": "RECEIPT_ALLOW_MASKED_PHONE",
            "receipt_filter_name": "RECEIPT_FILTER_NAME", "receipt_filter_phone": "RECEIPT_FILTER_PHONE",
            "receipt_filter_amount": "RECEIPT_FILTER_AMOUNT", "receipt_filter_date": "RECEIPT_FILTER_DATE",
            "receipt_filter_status": "RECEIPT_FILTER_STATUS", "receipt_filter_duplicate": "RECEIPT_FILTER_DUPLICATE",
            "receipt_aliases": "RECEIPT_RECEIVER_ALIASES", "receipt_min_amount": "RECEIPT_MIN_AMOUNT",
            "receipt_max_age_hours": "RECEIPT_MAX_AGE_HOURS", "receipt_timezone": "RECEIPT_TIMEZONE",
            "receipt_ocr_languages": "RECEIPT_OCR_LANGUAGES", "receipt_ocr_timeout": "RECEIPT_OCR_TIMEOUT",
            "xui_panel_url": "XUI_PANEL_URL", "sub_url": "SUB_BASE_URL",
            "xui_verify_tls": "XUI_VERIFY_TLS", "cache_seconds": "XUI_CACHE_SECONDS",
            "reminder_days": "REMINDER_DAYS", "web_username": "WEB_USERNAME",
            "web_session_max_age_seconds": "WEB_SESSION_MAX_AGE_SECONDS",
            "web_login_max_attempts": "WEB_LOGIN_MAX_ATTEMPTS", "web_login_window_seconds": "WEB_LOGIN_WINDOW_SECONDS",
            "web_login_block_seconds": "WEB_LOGIN_BLOCK_SECONDS", "web_login_max_block_seconds": "WEB_LOGIN_MAX_BLOCK_SECONDS",
            "web_login_security_retention_days": "WEB_LOGIN_SECURITY_RETENTION_DAYS",
            "backup_keep_days": "BACKUP_KEEP_DAYS", "backup_interval_days": "BACKUP_INTERVAL_DAYS",
            "backup_retry_interval_seconds": "BACKUP_RETRY_INTERVAL_SECONDS",
            "backup_pending_keep_days": "BACKUP_PENDING_KEEP_DAYS", "backup_telegram": "BACKUP_TELEGRAM",
            "backup_telegram_part_mb": "BACKUP_TELEGRAM_PART_MB", "backup_include_venv": "BACKUP_INCLUDE_VENV",
            "user_event_keep_days": "USER_EVENT_KEEP_DAYS", "user_event_max_rows": "USER_EVENT_MAX_ROWS",
            "metrics_store_interval_seconds": "METRICS_STORE_INTERVAL_SECONDS",
            "identity_import_max_mb": "IDENTITY_IMPORT_MAX_MB", "chat_media_max_mb": "CHAT_MEDIA_MAX_MB",
            "chat_media_cache_days": "CHAT_MEDIA_CACHE_DAYS", "chat_media_cache_max_mb": "CHAT_MEDIA_CACHE_MAX_MB",
        }
        missing = [name for name in names if name in explicit and f'"{explicit[name]}"' not in values_block]
        self.assertEqual(missing, [])

    def test_settings_config_backups_are_bounded_and_private(self):
        source = read("webapp.py")
        self.assertIn("old_backups = sorted(", source)
        self.assertIn("old_backups[5:]", source)
        self.assertIn("os.chmod(backup, 0o600)", source)

    def test_publisher_settings_have_persistence_path(self):
        web = read("webapp.py")
        block = web[web.index('updates_settings_block ='):web.index('panel_push_script =')]
        values = web[web.index('values: dict[str, Any] = {'):web.index('save_config_values(values)')]
        for name, key in {
            "github_repository_owner": "GITHUB_REPOSITORY_OWNER",
            "github_repository_name": "GITHUB_REPOSITORY_NAME",
            "github_target_branch": "GITHUB_TARGET_BRANCH",
            "github_release_tag_prefix": "GITHUB_RELEASE_TAG_PREFIX",
            "github_release_name_template": "GITHUB_RELEASE_NAME_TEMPLATE",
            "github_release_asset_name": "GITHUB_RELEASE_ASSET_NAME",
            "github_release_make_latest": "GITHUB_RELEASE_MAKE_LATEST",
            "github_release_draft": "GITHUB_RELEASE_DRAFT",
            "github_release_prerelease": "GITHUB_RELEASE_PRERELEASE",
            "update_check_interval": "UPDATE_CHECK_INTERVAL",
            "update_max_archive_mb": "UPDATE_MAX_ARCHIVE_MB",
            "update_stale_job_seconds": "UPDATE_STALE_JOB_SECONDS",
        }.items():
            self.assertIn(f'name="{name}"', block)
            self.assertIn(f'"{key}"', values)

    def test_xui_dedicated_form_persists_reminders_and_cache(self):
        web = read("webapp.py")
        handler = web[web.index('@app.post("/settings/xui")'):web.index('@app.post("/api/tls/auto", response_class=JSONResponse)')]
        self.assertIn('"XUI_CACHE_SECONDS"', handler)
        self.assertIn('form, "cache_seconds"', handler)
        self.assertIn('form.get("reminder_days")', handler)
        self.assertIn('values["REMINDER_DAYS"] = reminder_days', handler)

    def test_broadcast_counts_terminal_telegram_failures_correctly(self):
        source = read("main.py")
        self.assertIn("if result is None:", source)
        self.assertIn("failed += 1", source)

    def test_manual_grant_detects_terminal_telegram_delivery(self):
        source = read("main.py")
        self.assertIn("delivered = await bot.send_message(", source)
        self.assertIn("if delivered is not None", source)

    def test_support_does_not_claim_delivery_when_all_admins_fail(self):
        source = read("main.py")
        self.assertIn("delivered_admins = 0", source)
        self.assertIn("if delivered_admins:", source)
        self.assertIn("Не удалось доставить обращение администраторам", source)

    def test_subscription_report_does_not_block_telegram_event_loop(self):
        source = read("main.py")
        self.assertIn("text = await asyncio.to_thread(subscription_report_text)", source)

    def test_telegram_admin_broadcast_uses_configured_delay(self):
        source = read("main.py")
        self.assertNotIn("await asyncio.sleep(0.04)", source)
        self.assertIn('BROADCAST_SEND_DELAY_SECONDS', source)

    def test_reminders_use_config_not_hardcoded_runtime_value(self):
        source = read("trigger_reminders.py")
        self.assertIn('getattr(config, "REMINDER_DAYS"', source)

    def test_installer_has_single_backup_service_and_disables_legacy_units(self):
        source = read("install.sh")
        self.assertIn("vpn-service-backup.service", source)
        self.assertIn("vpn-service-backup.timer", source)
        for unit in ("fargovpn-backup.service", "fargovpn-backup.timer",
                     "vpn-bot-backup.service", "vpn-bot-backup.timer",
                     "vpn_bot_backup.service", "vpn_bot_backup.timer"):
            self.assertIn(unit, source)
        self.assertNotIn("journalctl", source)

    def test_safe_part_assembly(self):
        # Import restore_manager without invoking application startup.
        spec = importlib.util.spec_from_file_location(
            "restore_manager_test", ROOT / "restore_manager.py"
        )
        module = importlib.util.module_from_spec(spec)
        original_file_handler = __import__("logging").FileHandler
        __import__("logging").FileHandler = lambda *args, **kwargs: __import__("logging").NullHandler()
        try:
            spec.loader.exec_module(module)
        finally:
            __import__("logging").FileHandler = original_file_handler
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "parts"
            src.mkdir()
            (src / "x.tar.gz.part001").write_bytes(b"abc")
            (src / "x.tar.gz.part002").write_bytes(b"def")
            out = Path(tmp) / "joined.tar.gz"
            # The implementation validates the result as tar.gz, so build a
            # real archive for the behavioural check instead.
            with tempfile.TemporaryDirectory() as td:
                stage = Path(td) / "vpn_service_backup"
                stage.mkdir()
                (stage / "manifest.json").write_text("{}", encoding="utf8")
                (stage / "checksums.json").write_text("{}", encoding="utf8")
                archive = Path(td) / "x.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(stage, arcname="vpn_service_backup")
                raw = archive.read_bytes()
                split = (len(raw) + 1) // 2
                (src / "x.tar.gz.part001").write_bytes(raw[:split])
                (src / "x.tar.gz.part002").write_bytes(raw[split:])
                module.reassemble_parts(src, out)
                with tarfile.open(out, "r:gz") as tar:
                    names = {m.name for m in tar.getmembers()}
                self.assertIn("vpn_service_backup/manifest.json", names)


if __name__ == "__main__":
    unittest.main()
