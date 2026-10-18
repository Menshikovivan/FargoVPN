"""Run isolated real installer config handling without system mutations."""
import subprocess
import tempfile
from pathlib import Path

installer = Path(__file__).resolve().parents[1] / "app/install.sh"
source = installer.read_text()
helpers = source[source.index("config_is_valid() {"):source.index("find_old() {")]
start = source.index('  if config_is_valid "$OLD/config.py"; then')
block = source[start:source.index("  # The canonical application database", start)]
for name, content, expected in [
    ("missing", None, False),
    ("valid", "SERVICE_NAME = 'test'\n", True),
    ("recoverable", "SERVICE_NAME = 'test'\nbroken (\n", True),
    ("unrecoverable", "broken (\n", False),
]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old = root / "old"; old.mkdir()
        temp = root / "temp"; temp.mkdir()
        if content is not None:
            (old / "config.py").write_text(content)
        result = subprocess.run(["bash", "-c", 'set -Eeuo pipefail\nOLD=$1; TMP=$2; SRC=$3\n' + helpers + block,
                                 "test", str(old), str(temp), str(installer.parent)], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr)
        assert (temp / "config.py").exists() == expected, name
        assert (temp / "corrupt_config.py").exists() == (name in {"recoverable", "unrecoverable"}), name
        if name == "valid":
            assert (temp / "config.py").read_text() == content
        if expected:
            compile((temp / "config.py").read_text(), "config.py", "exec")
        print(name + ": PASS")

helper = installer.parent / "scripts/setup_postgresql.sh"
assert helper.is_file(), "PostgreSQL setup helper must be in the installed app tree"
assert helper.stat().st_mode & 0o111, "PostgreSQL setup helper must be executable"
assert helper.read_bytes() == (installer.parent.parent / "scripts/setup_postgresql.sh").read_bytes()
subprocess.run(["bash", "-n", str(helper)], check=True)
assert source.index('if [[ ! -f "$SRC/scripts/setup_postgresql.sh" ]]') < source.index('summary_running stop-services')
print("PostgreSQL helper packaging/preflight: PASS")
