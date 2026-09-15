#!/usr/bin/env python3
from __future__ import annotations
import os, re, subprocess, tempfile, time
from pathlib import Path

CONFIG=Path(__file__).resolve().parent/"config.py"
MAIN=Path("/etc/nginx/conf.d/01-main.conf")
A="# BEGIN FARGOVPN MANAGED LOCATION"
B="# END FARGOVPN MANAGED LOCATION"

def cfg(name, default=None):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("vpn_service_installed_config", CONFIG)
        if spec is None or spec.loader is None:
            return default
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, name, default)
    except Exception:
        return default

def prefix():
    p=str(cfg("WEB_PUBLIC_PREFIX", "") or "").strip()
    if not re.fullmatch(r"/[A-Za-z0-9_-]{8,96}", p):
        return ""
    return p

def block(p, socket_path):
    return """{A}
    location = {p} {{ return 301 {p}/; }}
    location ^~ {p}/ {{
        limit_req zone=panel burst=60 nodelay;
        proxy_pass http://unix:{socket_path}:/;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix {p};
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
        proxy_buffering off;
        proxy_request_buffering off;
        # FargoVPN generates canonical public redirects and a prefix-scoped session cookie itself.
        # Do not rewrite them again here or /prefix/ becomes /prefix/prefix/.
        # Keep nginx transparent to Location and Set-Cookie Path.
        proxy_intercept_errors off;
    }}
{B}""".format(A=A,B=B,p=p,socket_path=socket_path)

def _strip_location_blocks(text: str, predicate) -> str:
    lines = text.splitlines(True)
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"\s*location\b", line):
            depth = line.count("{") - line.count("}")
            block_lines = [line]
            j = i + 1
            while j < len(lines) and depth > 0:
                block_lines.append(lines[j])
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            block_text = "".join(block_lines)
            if depth == 0 and predicate(block_text):
                i = j
                continue
        out.append(line)
        i += 1
    return "".join(out)

def _insert_into_named_https_server(text: str, newblock: str) -> str | None:
    """Insert our location into the first suitable HTTPS/default web server.

    Never hard-code a customer's hostname: discover the server block from the
    configured WEB_DOMAIN when available, otherwise use a non-default server
    block that actually listens on 443/9443. This keeps the package portable.
    """
    configured_domain = str(cfg("WEB_DOMAIN", "") or cfg("WEB_TLS_SERVER_NAME", "") or "").strip()
    if configured_domain.startswith("https://"):
        from urllib.parse import urlsplit
        configured_domain = urlsplit(configured_domain).hostname or ""
    configured_domain = configured_domain.split(":", 1)[0].strip().lower()
    p = prefix()
    if not p:
        return None

    candidates=[]
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        start=match.start()
        depth=0
        end=None
        i=start
        while i < len(text):
            if text[i]=='{': depth += 1
            elif text[i]=='}':
                depth -= 1
                if depth==0:
                    end=i+1
                    break
            i += 1
        if end is None:
            continue
        server=text[start:end]
        listen443=bool(re.search(r"(?m)^\s*listen\s+(?:[^;]*:)?(?:443|9443)(?:\s|;)", server))
        if not listen443:
            # proxy-protocol setups often expose 127.0.0.1:9443 internally;
            # also accept any server that explicitly owns the configured name.
            listen443=bool(re.search(r"(?m)^\s*listen\s+127\.0\.0\.1:9443", server))
        names=[]
        for m in re.finditer(r"(?m)^\s*server_name\s+([^;]+);", server):
            names.extend(re.findall(r"[A-Za-z0-9_.-]+", m.group(1).lower()))
        score=0
        if configured_domain and configured_domain in names:
            score += 100
        if listen443:
            score += 20
        if not names or "_" in names:
            score -= 5
        candidates.append((score,start,end,server))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, start, end, server = candidates[0]
    # Remove any FargoVPN location for this exact prefix from every server block
    # before inserting the canonical one into the selected HTTPS host.
    def _is_any_fargovpn_location(block_text: str) -> bool:
        return bool(re.search(rf"(?m)^\s*location\s+(?:=|\^~)\s+{re.escape(p)}(?:/|\s|\{{)", block_text))
    cleaned_text = _strip_location_blocks(text, _is_any_fargovpn_location)
    if cleaned_text != text:
        text = cleaned_text
        candidates = []
        for match in re.finditer(r"(?m)^\s*server\s*\{", text):
            start2 = match.start(); depth2 = 0; end2 = None; i2 = start2
            while i2 < len(text):
                if text[i2] == '{': depth2 += 1
                elif text[i2] == '}':
                    depth2 -= 1
                    if depth2 == 0:
                        end2 = i2 + 1; break
                i2 += 1
            if end2 is None: continue
            srv = text[start2:end2]
            listen = bool(re.search(r"(?m)^\s*listen\s+(?:[^;]*:)?(?:443|9443)(?:\s|;)", srv)) or bool(re.search(r"(?m)^\s*listen\s+127\.0\.0\.1:9443", srv))
            names=[]
            for mm in re.finditer(r"(?m)^\s*server_name\s+([^;]+);", srv):
                names.extend(re.findall(r"[A-Za-z0-9_.-]+", mm.group(1).lower()))
            score2=(100 if configured_domain and configured_domain in names else 0)+(20 if listen else 0)- (5 if not names or '_' in names else 0)
            candidates.append((score2,start2,end2,srv))
        if not candidates:
            return None
        candidates.sort(key=lambda x:x[0], reverse=True)
        _, start, end, server = candidates[0]
    # Remove any previously generated FargoVPN admin location, regardless of
    # whether an older release used TCP:8088 or the current Unix socket.
    # Remove every previously generated marker block, regardless of release
    # suffix, backend type, or formatting differences between old versions.
    server = re.sub(
        r"\n?\s*# BEGIN FARGOVPN(?:-[^\n]*)? MANAGED LOCATION.*?# END FARGOVPN(?:-[^\n]*)? MANAGED LOCATION\s*",
        "\n",
        server,
        flags=re.S,
    )
    def _is_legacy_fargovpn_location(block_text: str) -> bool:
        if not re.search(r"(?m)^\s*location\s+(?:=|\^~)\s+[^\s{]*fargovpn-admin[^\s{]*", block_text):
            return False
        return bool(
            re.search(r"(?m)^\s*proxy_pass\s+", block_text)
            or re.search(r"(?m)^\s*proxy_redirect\s+", block_text)
            or re.search(r"(?m)^\s*proxy_cookie_path\s+", block_text)
            or re.search(r"(?m)^\s*proxy_set_header\s+X-Forwarded-Prefix\s+", block_text)
            or re.search(r"(?m)^\s*proxy_set_header\s+Host\s+", block_text)
        )
    server = _strip_location_blocks(server, _is_legacy_fargovpn_location)
    first_nl = server.find('\n')
    if first_nl < 0:
        return None
    server = server[:first_nl+1] + newblock + "\n" + server[first_nl+1:]
    return text[:start] + server + text[end:]

def _nginx_loaded_conf_files() -> list[Path]:
    """Return configuration files actually parsed by the running nginx binary.

    `nginx -T` prints explicit `# configuration file ...:` markers for every
    included file. This is more reliable than guessing under /etc/nginx when a
    distribution, Mask installer, or generated configuration uses another
    include path or a symlinked/generated file.
    """
    try:
        result = subprocess.run(
            ["nginx", "-T"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except Exception:
        return []
    result_files: list[Path] = []
    seen: set[Path] = set()
    marker_re = re.compile(r"^# configuration file (.+?):$")
    for line in (result.stdout or "").splitlines():
        m = marker_re.match(line.strip())
        if not m:
            continue
        raw = m.group(1).strip()
        path = Path(raw)
        try:
            real = path.resolve()
        except OSError:
            continue
        if real in seen or not real.is_file():
            continue
        seen.add(real)
        result_files.append(real)
    return result_files


def _candidate_conf_files() -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    # First use the files nginx actually loaded. Keep the filesystem scan as a
    # fallback for installations where nginx -T cannot be executed yet.
    for path in _nginx_loaded_conf_files():
        seen.add(path)
        result.append(path)
    roots = [Path("/etc/nginx/conf.d"), Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/streams-enabled"), Path("/etc/nginx")]
    for root in roots:
        if not root.exists():
            continue
        try:
            paths = sorted(root.rglob("*.conf"))
        except OSError:
            continue
        for path in paths:
            try:
                real = path.resolve()
            except OSError:
                continue
            if real in seen or not real.is_file():
                continue
            seen.add(real)
            result.append(real)
    return result


def _file_contains_target_server(text: str) -> bool:
    configured_domain = str(cfg("WEB_DOMAIN", "") or cfg("WEB_TLS_SERVER_NAME", "") or "").strip().lower()
    if configured_domain.startswith("https://"):
        from urllib.parse import urlsplit
        configured_domain = urlsplit(configured_domain).hostname or ""
    configured_domain = configured_domain.split(":", 1)[0].strip().lower()
    p = prefix()
    if not p:
        return False
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        start = match.start(); depth = 0; end = None; i = start
        while i < len(text):
            if text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1; break
            i += 1
        if end is None:
            continue
        server = text[start:end]
        listen = bool(re.search(r"(?m)^\s*listen\s+(?:[^;]*:)?(?:443|9443)(?:\s|;)", server)) or bool(re.search(r"(?m)^\s*listen\s+127\.0\.0\.1:9443", server))
        names = []
        for m in re.finditer(r"(?m)^\s*server_name\s+([^;]+);", server):
            names.extend(re.findall(r"[A-Za-z0-9_.-]+", m.group(1).lower()))
        if listen and (not configured_domain or configured_domain in names):
            return True
    return False

def _location_exists_in_nginx() -> bool:
    p = prefix()
    sock = str(cfg("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock") or "/run/vpn-service/fargovpn.sock")
    if not p:
        return False
    try:
        result = subprocess.run(["nginx", "-T"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20, check=False)
    except Exception:
        return False
    text = result.stdout or ""
    needle = f"location ^~ {p}/"
    start = text.find(needle)
    if start < 0:
        return False
    tail = text[start:]
    depth = 0
    opened = False
    for index, char in enumerate(tail[:20000]):
        if char == "{":
            depth += 1
            opened = True
        elif char == "}" and opened:
            depth -= 1
            if depth == 0:
                body = tail[:index + 1]
                return f"proxy_pass http://unix:{sock}:/" in body
    return False


def ensure_once():
    if not bool(cfg("WEB_REVERSE_PROXY", False)):
        return False
    p = prefix()
    if not p:
        return False
    socket_path = str(cfg("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock") or "/run/vpn-service/fargovpn.sock")
    candidates = []
    for path in _candidate_conf_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _file_contains_target_server(text):
            candidates.append(path)
    target = next((x for x in candidates if x == MAIN.resolve()), None) or (candidates[0] if candidates else None)
    if target is None:
        # A pre-existing correct route is fine even when its source file is not
        # one of the common include directories (for example generated config).
        return _location_exists_in_nginx()
    text = target.read_text(encoding="utf-8", errors="replace")
    new = _insert_into_named_https_server(text, block(p, socket_path))
    if new is None:
        return _location_exists_in_nginx()
    if new == text and _location_exists_in_nginx():
        return True
    backup = target.with_name(target.name + ".fargovpn-backup")
    temp = target.with_name("." + target.name + ".fargovpn.tmp")
    temp.write_text(new, encoding="utf-8")
    os.chmod(temp, 0o644)
    try:
        # Install first, then validate the actual loaded configuration. If the
        # active configuration becomes invalid, restore the exact prior file.
        os.replace(target, backup)
        os.replace(temp, target)
        test = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=20)
        if test.returncode != 0:
            os.replace(target, temp)
            os.replace(backup, target)
            return False
        reload_result = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=20)
        if reload_result.returncode != 0:
            os.replace(target, temp)
            os.replace(backup, target)
            return False
        # Only after successful reload is the backup removed.
        backup.unlink(missing_ok=True)
        return _location_exists_in_nginx()
    except Exception:
        try:
            if backup.exists():
                target.unlink(missing_ok=True)
                os.replace(backup, target)
        except Exception:
            pass
        return False
    finally:
        temp.unlink(missing_ok=True)

if __name__=="__main__":
    args=set(os.sys.argv[1:])
    if "--check" in args:
        raise SystemExit(0 if _location_exists_in_nginx() else 1)
    once="--once" in args
    while True:
        try: ensure_once()
        except Exception: pass
        if once: break
        time.sleep(2)
