#!/usr/bin/env python3
"""Restore a persistent pre-update snapshot."""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import tarfile
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
import update_manager

UNITS=["vpn-service-bot.service","vpn-service-web.service","vpn-service-backup.timer","vpn-service-reminders.timer"]


def write(job_id,state,progress,phase,message,**extra):
    update_manager.write_status(state,job_id=job_id,progress=progress,phase=phase,message=message,**extra)


def top_root(archive):
    return update_manager._preupdate_archive_root(Path(archive))


def validate_members(archive, root):
    with tarfile.open(archive,"r:gz") as tar:
        for m in tar.getmembers():
            name=m.name.replace("\\","/")
            in_venv = "/.venv/" in f"/{name.strip('/')}" or name.rstrip("/").endswith("/.venv")
            if (m.issym() or m.islnk()) and in_venv:
                continue
            if m.issym() or m.islnk() or m.isdev() or not (m.isfile() or m.isdir()):
                raise RuntimeError("Архив содержит ссылки или специальные файлы")
            allowed = (not root) or name == root or name.startswith(root + "/") or root.startswith(name + "/")
            if not name or name.startswith("/") or ".." in Path(name).parts or not allowed:
                raise RuntimeError("Архив содержит небезопасный путь")


def restore_systemd(sidecar):
    if not sidecar.is_file(): return
    allowed={u for u in UNITS}|{"vpn-service-update@.service","vpn-service-broadcast@.service","fargovpn-bot.service","fargovpn-web.service","fargovpn-backup.service","fargovpn-backup.timer","fargovpn-reminders.service","fargovpn-reminders.timer"}
    with tarfile.open(sidecar,"r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or "/" in m.name or m.name not in allowed:
                raise RuntimeError("Архив systemd содержит недопустимый файл")
            src=tar.extractfile(m)
            if src:
                target=Path("/etc/systemd/system")/m.name
                target.write_bytes(src.read()); os.chmod(target,m.mode&0o777)


def restart_services():
    subprocess.run(["systemctl","daemon-reload"],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for unit in UNITS:
        subprocess.run(["systemctl","enable",unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(["systemctl","restart",unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def run(job_id,backup):
    target=update_manager.APP_DIR.resolve()
    previous=update_manager._preupdate_version(backup) or "предыдущей версии"
    old=None
    try:
        root = top_root(backup)
        validate_members(backup,root)
        # A pre-update archive is not a release package: its only required
        # property is that it contains the previous installation tree.
        write(job_id,"rolling-back",5,"prepare",f"Подготавливается откат к версии {previous}",version=previous)
        with tempfile.TemporaryDirectory(prefix="vpn_rollback_") as temp:
            old=Path(temp)/"current"
            write(job_id,"rolling-back",20,"stop-services","Останавливаются службы")
            for u in UNITS: subprocess.run(["systemctl","stop",u],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            shutil.move(str(target),str(old))
            try:
                write(job_id,"rolling-back",45,"restore-files","Восстанавливаются файлы предыдущей версии")
                with tarfile.open(backup,"r:gz") as tar:
                    prefix = root.rstrip("/") + "/" if root else ""
                    for member in tar.getmembers():
                        name = member.name.replace("\\", "/").strip("/")
                        if root and not (name == root or name.startswith(prefix)):
                            continue
                        rel = name[len(root):].lstrip("/") if root else name
                        if rel == ".venv" or rel.startswith(".venv/"):
                            continue
                        # Compatibility with older full-tree snapshots that still
                        # carry one extra ``app/`` wrapper after the detected root.
                        if rel.startswith("app/") and rel != "app/main.py" and rel != "app/config.py":
                            rel = rel[4:]
                        if not rel:
                            continue
                        member.name = rel
                        tar.extract(member, target, filter="data")
                # A virtual environment is runtime state, not application
                # source. Reuse the environment that was healthy immediately
                # before rollback; dependency reconciliation remains handled
                # by the normal installer on the next update.
                current_venv = old / ".venv"
                if current_venv.exists() and not (target / ".venv").exists():
                    shutil.move(str(current_venv), str(target / ".venv"))
                if not (target.is_dir() and (target / "main.py").is_file() and (target / "config.py").is_file()):
                    # Historical snapshots created by install.sh contain the APP_DIR
                    # itself, therefore main.py/config.py are directly under target.
                    raise RuntimeError("После распаковки архив не восстановил каталог приложения")
                restore_systemd(update_manager._preupdate_systemd_path(backup))
                write(job_id,"rolling-back",82,"restart","Перезапускаются службы")
                restart_services()
                if subprocess.run(["systemctl","is-active","--quiet","vpn-service-bot.service"],check=False).returncode!=0:
                    raise RuntimeError("После отката бот не запустился")
                write(job_id,"completed",100,"complete",f"Откат завершён: версия {previous}",version=update_manager.current_version(),finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                return 0
            except Exception:
                if target.exists(): shutil.rmtree(target,ignore_errors=True)
                shutil.move(str(old),str(target)); restart_services(); raise
    except Exception as error:
        update_manager.write_status("failed",job_id=job_id,progress=max(1,int(update_manager.read_status().get("progress") or 1)),phase="failed",message="Откат не завершён",error=str(error),finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        traceback.print_exc(); return 1


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--job-id",required=True); ap.add_argument("--backup",required=True); a=ap.parse_args()
    return run(a.job_id,update_manager.validate_preupdate_backup(a.backup))

if __name__=="__main__": raise SystemExit(main())
