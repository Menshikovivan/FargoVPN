#!/usr/bin/env python3
"""Detect and optionally disable duplicate VPN bot/web/backup/reminder launchers."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CANONICAL_UNITS = {
    "vpn-service-bot.service",
    "vpn-service-web.service",
    "vpn-service-backup.service",
    "vpn-service-backup.timer",
    "vpn-service-reminders.service",
    "vpn-service-reminders.timer",
}
SCRIPT_MARKERS = ("main.py", "webapp.py", "panel_runtime.py", "backup.py", "trigger_reminders.py")
KNOWN_LEGACY_UNITS = {
    "fargovpn-bot.service",
    "fargovpn-web.service",
    "fargovpn-backup.service",
    "fargovpn-backup.timer",
    "fargovpn-reminders.service",
    "fargovpn-reminders.timer",
    "vpn-bot.service",
    "vpn-bot-web.service",
    "vpn-bot-backup.service",
    "vpn-bot-backup.timer",
    "vpn-bot-reminders.service",
    "vpn-bot-reminders.timer",
    "vpn_bot.service",
    "vpn_bot_web.service",
    "vpn_bot_backup.service",
    "vpn_bot_backup.timer",
    "vpn_bot_reminders.service",
    "vpn_bot_reminders.timer",
}
APP_DIR = str(Path(__file__).resolve().parent).lower()
PATH_MARKERS = (
    APP_DIR,
    "/root/vpn_bot",
    "fargovpn",
)

def run(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as error:
        return subprocess.CompletedProcess(args, 1, "", str(error))


def systemd_available() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def _unit_property(unit: str, prop: str) -> str:
    result = run(["systemctl", "show", unit, f"--property={prop}", "--value"])
    return (result.stdout or "").strip()


def relevant_units() -> list[dict[str, Any]]:
    if not systemd_available():
        return []
    result = run(["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
    units: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if not name.endswith((".service", ".timer")):
            continue
        fragment = _unit_property(name, "FragmentPath")
        exec_start = _unit_property(name, "ExecStart")
        workdir = _unit_property(name, "WorkingDirectory")
        try:
            fragment_text = Path(fragment).read_text(errors="replace") if fragment else ""
        except OSError:
            fragment_text = ""
        haystack = f"{name} {fragment} {exec_start} {workdir} {fragment_text}".lower()
        name_lower = name.lower()
        project_command = any(marker in haystack for marker in SCRIPT_MARKERS) and any(
            marker in haystack for marker in PATH_MARKERS
        )
        known_legacy_name = name_lower in KNOWN_LEGACY_UNITS
        relevant = name in CANONICAL_UNITS or project_command or known_legacy_name
        if not relevant:
            continue
        active = (run(["systemctl", "is-active", name]).stdout or "").strip()
        enabled = (run(["systemctl", "is-enabled", name]).stdout or "").strip()
        units.append(
            {
                "name": name,
                "canonical": name in CANONICAL_UNITS,
                "active": active,
                "enabled": enabled,
                "fragment": fragment,
                "exec_start": exec_start,
                "working_directory": workdir,
            }
        )
    return units


def relevant_processes() -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return processes
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        lowered = raw.lower()
        if (
            not raw
            or not any(marker in lowered for marker in SCRIPT_MARKERS)
            or not any(marker in lowered for marker in PATH_MARKERS)
        ):
            continue
        processes.append({"pid": int(entry.name), "command": raw})
    return sorted(processes, key=lambda item: item["pid"])


def cron_references() -> list[dict[str, Any]]:
    files = [Path("/etc/crontab")]
    files.extend(sorted(Path("/etc/cron.d").glob("*")) if Path("/etc/cron.d").exists() else [])
    files.extend([Path("/var/spool/cron/crontabs/root"), Path("/var/spool/cron/root")])
    refs: list[dict[str, Any]] = []
    for path in files:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lowered = line.lower()
            project_path = any(marker in lowered for marker in PATH_MARKERS)
            reminder_ref = "trigger_reminders.py" in lowered and project_path
            backup_ref = "backup.py" in lowered and project_path
            if reminder_ref or backup_ref:
                refs.append({"path": str(path), "line": number, "text": line})
    return refs


def audit() -> dict[str, Any]:
    units = relevant_units()
    processes = relevant_processes()
    cron = cron_references()
    duplicates = [unit for unit in units if not unit["canonical"]]

    process_groups: dict[str, int] = {marker: 0 for marker in SCRIPT_MARKERS}
    for process in processes:
        for marker in SCRIPT_MARKERS:
            if marker in process["command"]:
                process_groups[marker] += 1
    duplicate_processes = {name: count for name, count in process_groups.items() if count > 1}

    return {
        "systemd_available": systemd_available(),
        "units": units,
        "duplicate_units": duplicates,
        "processes": processes,
        "duplicate_processes": duplicate_processes,
        "cron_references": cron,
        "healthy": not duplicates and not duplicate_processes and not cron,
    }


def _remove_cron_references(refs: list[dict[str, Any]]) -> list[str]:
    changed: list[str] = []
    by_path: dict[str, set[int]] = {}
    for ref in refs:
        by_path.setdefault(ref["path"], set()).add(int(ref["line"]))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for path_text, line_numbers in by_path.items():
        path = Path(path_text)
        try:
            original = path.read_text(errors="replace")
            backup = path.with_name(path.name + f".before-vpn-service-{stamp}")
            shutil.copy2(path, backup)
            kept = [line for index, line in enumerate(original.splitlines(), 1) if index not in line_numbers]
            path.write_text("\n".join(kept) + ("\n" if original.endswith("\n") else ""))
            changed.append(str(path))
        except OSError:
            continue
    return changed


def fix() -> dict[str, Any]:
    before = audit()
    actions: list[str] = []
    if systemd_available():
        for unit in before["duplicate_units"]:
            name = unit["name"]
            run(["systemctl", "disable", "--now", name], timeout=30)
            fragment = Path(unit.get("fragment") or "")
            if fragment.is_file() and str(fragment).startswith("/etc/systemd/system/"):
                try:
                    fragment.unlink()
                    actions.append(f"removed {fragment}")
                except OSError:
                    pass
            actions.append(f"disabled {name}")
        if before["duplicate_units"]:
            run(["systemctl", "daemon-reload"], timeout=30)
    for path in _remove_cron_references(before["cron_references"]):
        actions.append(f"cleaned cron {path}")
    after = audit()
    return {"before": before, "actions": actions, "after": after}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = fix() if args.fix else audit()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json or args.fix else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
