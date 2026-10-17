"""Exercise the installer's real backup command in an isolated directory."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

def test_installer_rollback_restores_venv():
    source = (Path(__file__).resolve().parents[1] / "app/install.sh").read_text(encoding="utf-8")
    start = source.index('  tar -czf "$PREUPDATE_BACKUP_PART"')
    end = source.index('  tar -tzf "$PREUPDATE_BACKUP_PART"', start)
    backup_command = source[start:end]
    bash = os.environ.get("FARGOVPN_TEST_BASH") or shutil.which("bash")
    if not bash:
        pytest.skip("bash required; set FARGOVPN_TEST_BASH to a Bash executable")
    
    with tempfile.TemporaryDirectory(prefix="fargovpn-rollback-") as directory:
        root = Path(directory)
        old = root / "app"
        (old / ".venv/bin").mkdir(parents=True)
        (old / ".venv/lib").mkdir()
        (old / ".venv/bin/python").write_text("previous-runtime", encoding="utf-8")
        (old / ".venv/lib/dependency.py").write_text("previous-dependency", encoding="utf-8")
        (old / "config.py").write_text("previous-config", encoding="utf-8")
        (old / "__pycache__").mkdir()
        (old / "__pycache__/ignored.pyc").write_bytes(b"cache")
        # Relative paths also work under Git Bash on Windows.
        script = 'set -Eeuo pipefail\nOLD=app\nPREUPDATE_BACKUP_PART=backup.tar.gz\n' + backup_command
        subprocess.run([bash, "-c", script], cwd=root, check=True)
        shutil.rmtree(old)
        subprocess.run([bash, "-c", 'tar -xzf backup.tar.gz'], cwd=root, check=True)
        assert (old / ".venv/bin/python").read_text() == "previous-runtime"
        assert (old / ".venv/lib/dependency.py").read_text() == "previous-dependency"
        assert (old / "config.py").read_text() == "previous-config"
        assert not (old / "__pycache__/ignored.pyc").exists()


@pytest.mark.parametrize("stuck", [False, True])
def test_quiesce_stops_activation_before_writers_and_fails_closed(tmp_path, stuck):
    bash = os.environ.get("FARGOVPN_TEST_BASH") or shutil.which("bash")
    if not bash:
        pytest.skip("bash required")
    source = (Path(__file__).resolve().parents[1] / "app/install.sh").read_text(encoding="utf-8")
    helper = source[source.index("quiesce_application() {"):source.index("on_error() {")]
    # This fake socket reactivates web when it is stopped in the wrong order.
    script = r'''set -Eeuo pipefail
socket=active
web=active
log=""
run_with_timeout() { shift; "$@"; }
systemctl() {
  case "$1" in
    list-units) echo "vpn-service-broadcast@123.service loaded active running" ;;
    cat) case "$2" in vpn-service-web.socket|vpn-service-web.service|vpn-service-broadcast@123.service) return 0;; *) return 1;; esac ;;
    stop)
      log="$log $2"
      case "$2" in
        vpn-service-web.socket) socket=inactive ;;
        vpn-service-web.service) web=inactive; [[ "$socket" == inactive ]] || web=active ;;
      esac ;;
    show)
      case "$2" in
        vpn-service-web.socket) echo "$socket" ;;
        vpn-service-web.service) echo "$web" ;;
        vpn-service-broadcast@123.service) echo "BROADCAST_STATE" ;;
      esac ;;
  esac
}
''' + helper + r'''
if quiesce_application; then result=0; else result=1; fi
[[ "$result" == EXPECTED_RESULT ]]
[[ "$log" == " vpn-service-web.socket vpn-service-web.service vpn-service-broadcast@123.service" ]]
'''
    script = script.replace("BROADCAST_STATE", "active" if stuck else "inactive").replace("EXPECTED_RESULT", "1" if stuck else "0")
    subprocess.run([bash, "-c", script], cwd=tmp_path, check=True)


def test_self_update_retains_rollback_helper_outside_replaced_source(tmp_path):
    bash = os.environ.get("FARGOVPN_TEST_BASH") or shutil.which("bash")
    if not bash:
        pytest.skip("bash required")
    app = Path(__file__).resolve().parents[1] / "app"
    source = (app / "install.sh").read_text(encoding="utf-8")
    begin = source.index('  PREUPDATE_ROLLBACK_HELPER="$TMP/postgresql_rollback.py"')
    end = source.index('  database_snapshot=', begin)
    local_source = tmp_path / "app"
    local_source.mkdir()
    shutil.copyfile(app / "postgresql_rollback.py", local_source / "postgresql_rollback.py")
    (tmp_path / "safe").mkdir()
    (tmp_path / "snapshot.json").write_text('{"postgresql": false}')
    (tmp_path / "snapshot.json").chmod(0o600)
    script = 'set -Eeuo pipefail\nSRC=app\nTMP=safe\n' + source[begin:end] + '\nrm -rf app\npython3 "$PREUPDATE_ROLLBACK_HELPER" restore snapshot\n'
    subprocess.run([bash, "-c", script], cwd=tmp_path, check=True)
