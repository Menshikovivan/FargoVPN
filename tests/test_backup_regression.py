import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "app"


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_backup_imports_message_journal_and_uses_best_effort_wrapper():
    source = read("backup.py")
    assert "import message_journal" in source
    assert "def _record_backup_journal_best_effort(" in source
    assert "Не удалось записать backup-событие в message_journal" in source


def test_successful_telegram_delivery_is_marked_before_journal_call():
    source = read("backup.py")
    result_pos = source.index("result = await bot.send_document")
    sent_pos = source.index("sent = result is not None", result_pos)
    journal_pos = source.index("_record_backup_journal_best_effort(", sent_pos)
    assert sent_pos < journal_pos
    assert "if attempt < 3" in source
    assert "message_journal.record_outgoing_sync(" not in source[source.index("def send_to_telegram_sync"):source.index("def _record_run")].replace("_record_backup_journal_best_effort(", "")


def test_backup_delivery_has_no_duplicate_scheduler_launcher():
    source = read("install.sh")
    assert source.count("/etc/systemd/system/vpn-service-backup.service") >= 1
    assert source.count("/etc/systemd/system/vpn-service-backup.timer") >= 1
    assert "vpn-service-backup.timer" in source
    assert 'service_audit.py" --fix --json' in source
    assert "cron" in read("service_audit.py").lower()


def test_release_files_are_current():
    assert read("VERSION").strip() == "5.1.16"
    repo_readme = Path(ROOT.parent / "README.md").read_text(encoding="utf-8")
    assert repo_readme.startswith("# FargoVPN") and "5.1.16" in repo_readme
    assert not list(ROOT.glob("RELEASE_NOTES_*.md"))
    assert not (ROOT / "INSTALLED_CHANGELOG.md").exists()
    assert not (ROOT / "INSTALLED_CHANGELOG_VERSION").exists()


def test_backup_python_syntax():
    ast.parse(read("backup.py"), filename="backup.py")
