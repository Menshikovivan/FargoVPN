#!/usr/bin/env python3
from __future__ import annotations
import ast, os, re, stat, subprocess, sys, tempfile, time
from pathlib import Path

"""Compatibility bridge for a pre-existing external Nginx/L4 deployment.

This module does not install, enable, start or otherwise provision Nginx.
It only maintains the dedicated FargoVPN location in the already-installed
external Nginx virtual host so FargoVPN is published under its own URI.
"""

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

def _nginx_dump() -> str:
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
        return ""
    return result.stdout or ""

def _server_blocks(text: str):
    # \s also consumes the blank line BEFORE `server {`. If that newline is
    # included in the match, insertion after the first newline ends up at
    # http scope, where nginx rejects a `location` directive.
    for match in re.finditer(r"(?m)^[ \t]*server[ \t]*\{", text):
        start = match.start()
        depth = 0
        end = None
        index = start
        while index < len(text):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
            index += 1
        if end is not None:
            yield start, end, text[start:end]

def _server_names(server: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"(?m)^\s*server_name\s+([^;]+);", server):
        names.extend(re.findall(r"[A-Za-z0-9_.-]+", match.group(1).lower()))
    return [name for name in names if name != "_"]

def _https_server(server: str) -> bool:
    return bool(re.search(r"(?m)^\s*listen\s+[^;]*\bssl(?:\s|;)", server)) or bool(
        re.search(r"(?m)^\s*listen\s+127\.0\.0\.1:9443(?:\s|;)", server)
    )

def _server_score(server: str) -> int:
    if not _https_server(server):
        return -1
    if re.search(r"(?m)^\s*ssl_reject_handshake\s+on\s*;", server):
        return -1
    domain = str(cfg("WEB_DOMAIN", "") or cfg("WEB_TLS_SERVER_NAME", "") or "").strip().lower()
    if domain.startswith("https://"):
        from urllib.parse import urlsplit
        domain = urlsplit(domain).hostname or ""
    domain = domain.split(":", 1)[0].strip()
    names = _server_names(server)
    # The L4 router's primary HTTPS host can listen ONLY on the Unix socket.
    # Its local 9443 fallback can be a separate virtual host.
    unix_https = bool(re.search(r"(?m)^\s*listen\s+unix:[^;]*\bssl(?:\s|;)", server))
    score = (100 if domain and domain in names else 0)
    score += 200 if unix_https else 0
    # The router's primary masked site carries the xHTTP location; the CDN
    # and DNS hosts can use the same Unix listener and even share panel paths.
    score += 300 if re.search(r"(?m)^\s*location\s+[^\n]*Stream-One-Path", server) else 0
    score += 5 if names else 0
    return score

def public_domain_from_nginx_text(text: str, p: str | None = None) -> str:
    """Find the hostname of the HTTPS virtual host serving the FargoVPN URI.

    The external L4 project terminates stream traffic into an HTTPS server that
    may listen on a Unix socket plus 127.0.0.1:9443 rather than TCP:443. We
    therefore identify the server by the managed location first, then fall back
    to an HTTPS/9443 server with a real `server_name`.
    """
    p = p or prefix()
    if not p:
        return ""
    exact = re.compile(rf"(?m)^\s*location\s+\^~\s+{re.escape(p)}/\s*\{{")
    candidates: list[tuple[int, str]] = []
    for _start, _end, server in _server_blocks(text):
        names = _server_names(server)
        if not names:
            continue
        listens_https = bool(re.search(r"(?m)^\s*listen\s+[^;]*(?:ssl|proxy_protocol)", server))
        listens_9443 = bool(re.search(r"(?m)^\s*listen\s+127\.0\.0\.1:9443(?:\s|;)", server))
        has_route = bool(exact.search(server))
        score = (1000 if has_route else 0) + (100 if listens_9443 else 0) + (50 if listens_https else 0)
        if score:
            candidates.append((score, names[0]))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]

def _set_config_value(name: str, value) -> None:
    from config_store import config_write_lock
    with config_write_lock(CONFIG):
        _set_config_value_locked(name, value)


def _set_config_value_locked(name: str, value) -> None:
    """Update one simple literal setting in the installed config atomically."""
    if not CONFIG.is_file():
        return
    try:
        source = CONFIG.read_text(encoding="utf-8")
    except OSError:
        return
    replacement = f"{name} = {value!r}"
    pattern = re.compile(rf"(?m)^{re.escape(name)}\s*=.*$")
    updated = pattern.sub(replacement, source, count=1)
    if updated == source and not pattern.search(source):
        updated = source.rstrip() + "\n" + replacement + "\n"
    if updated == source:
        return
    temp = CONFIG.with_name("." + CONFIG.name + ".nginx.tmp")
    try:
        temp.write_text(updated, encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, CONFIG)
    finally:
        temp.unlink(missing_ok=True)

def public_url() -> str:
    p = prefix()
    if not p:
        return ""
    domain = str(cfg("WEB_DOMAIN", "") or cfg("WEB_TLS_SERVER_NAME", "") or "").strip()
    if domain.startswith("https://"):
        from urllib.parse import urlsplit
        domain = urlsplit(domain).hostname or ""
    domain = domain.split(":", 1)[0].strip()
    if not domain:
        domain = public_domain_from_nginx_text(_nginx_dump(), p)
    if not domain:
        return ""
    return f"https://{domain}{p}/"

def block(p, socket_path):
    # Include multipart overhead and preserve large update/restore uploads.
    upload_mb = max(100, int(cfg("CHAT_MEDIA_MAX_MB", 100)),
                    int(cfg("BROADCAST_MEDIA_MAX_MB", 45)),
                    int(cfg("IDENTITY_IMPORT_MAX_MB", 512)),
                    int(cfg("UPDATE_MAX_ARCHIVE_MB", 1024))) + 2
    return """{A}
    location = {p} {{ return 301 {p}/; }}
    location ^~ {p}/ {{
        client_max_body_size {upload_mb}m;
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
{B}""".format(A=A,B=B,p=p,socket_path=socket_path,upload_mb=upload_mb)

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

    def choose(source):
        ranked = [(score, start, end, server) for start, end, server in _server_blocks(source)
                  if (score := _server_score(server)) >= 0]
        return max(ranked, key=lambda item: item[0]) if ranked else None

    selected = choose(text)
    if selected is None:
        return None
    _, start, end, server = selected
    # Remove any FargoVPN location for this exact prefix from every server block
    # before inserting the canonical one into the selected HTTPS host.
    def _is_any_fargovpn_location(block_text: str) -> bool:
        return bool(re.search(rf"(?m)^\s*location\s+(?:=|\^~)\s+{re.escape(p)}(?:/|\s|\{{)", block_text))
    cleaned_text = _strip_location_blocks(text, _is_any_fargovpn_location)
    if cleaned_text != text:
        text = cleaned_text
        selected = choose(text)
        if selected is None:
            return None
        _, start, end, server = selected
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
    dump = _nginx_dump()
    if not dump:
        return []
    result_files: list[Path] = []
    seen: set[Path] = set()
    marker_re = re.compile(r"^# configuration file (.+?):$")
    for line in dump.splitlines():
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
    loaded = _nginx_loaded_conf_files()
    for path in loaded:
        seen.add(path)
        result.append(path)
    # When nginx -T provides its file list, an unreferenced file in conf.d
    # cannot affect the running server. Never edit one as a fallback.
    if loaded:
        return result
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
    return any(_server_score(server) >= 0 for _, _, server in _server_blocks(text))

def _location_exists_in_text(text: str, prefix_path: str, socket_path: str) -> bool:
    """Detect the FargoVPN managed route in an actual nginx config dump/file.

    nginx -T may normalize whitespace and the route can live in a generated or
    symlinked include. Do not rely on one exact byte sequence; verify the
    dedicated location and its proxy target structurally.
    """
    if not text or not prefix_path:
        return False
    location_re = re.compile(
        rf"(?m)^\s*location\s+\^~\s+{re.escape(prefix_path)}/\s*\{{"
    )
    for match in location_re.finditer(text):
        start = match.start()
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
                    if re.search(rf"(?m)^\s*proxy_pass\s+http://unix:{re.escape(socket_path)}:/\s*;", body):
                        return True
                    break
    return False


def _location_exists_in_nginx() -> bool:
    p = prefix()
    sock = str(cfg("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock") or "/run/vpn-service/fargovpn.sock")
    if not p:
        return False
    dump = _nginx_dump()
    # The dump is the only proof that nginx actually loaded this route.
    # Looking at source files after a stale dump can approve an inactive route.
    return _location_exists_in_text(dump, p, sock)

def _persist_public_identity() -> str:
    p = prefix()
    if not p:
        return ""
    domain = public_domain_from_nginx_text(_nginx_dump(), p)
    if not domain:
        return public_url()
    _set_config_value("WEB_DOMAIN", domain)
    _set_config_value("WEB_TLS_SERVER_NAME", domain)
    url = f"https://{domain}{p}/"
    _set_config_value("BOT_PANEL_URL", url)
    _set_config_value("PUBLIC_PANEL_URL", url)
    return url


def ensure_once():
    if not bool(cfg("WEB_REVERSE_PROXY", False)):
        print("FargoVPN nginx guard: WEB_REVERSE_PROXY выключен", file=sys.stderr)
        return False
    p = prefix()
    if not p:
        print("FargoVPN nginx guard: WEB_PUBLIC_PREFIX отсутствует или имеет неверный формат", file=sys.stderr)
        return False
    socket_path = str(cfg("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock") or "/run/vpn-service/fargovpn.sock")
    candidates = []
    for path in _nginx_loaded_conf_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _file_contains_target_server(text):
            score = max(_server_score(server) for _, _, server in _server_blocks(text))
            candidates.append((score, path))
    target = max(candidates, key=lambda item: (item[0], item[1] == MAIN.resolve()))[1] if candidates else None
    if target is None:
        print("FargoVPN nginx guard: в nginx -T не найден загруженный HTTPS server (ssl/Unix socket/9443)", file=sys.stderr)
        # A pre-existing correct route is fine even when its source file is not
        # one of the common include directories (for example generated config).
        ok = _location_exists_in_nginx()
        if ok:
            _persist_public_identity()
        return ok
    text = target.read_text(encoding="utf-8", errors="replace")
    new = _insert_into_named_https_server(text, block(p, socket_path))
    if new is None:
        print(f"FargoVPN nginx guard: HTTPS server в {target} не выбран для вставки", file=sys.stderr)
        ok = _location_exists_in_nginx()
        if ok:
            _persist_public_identity()
        return ok
    if new == text and _location_exists_in_nginx():
        return True
    backup = target.with_name(target.name + ".fargovpn-backup")
    temp = target.with_name("." + target.name + ".fargovpn.tmp")
    mode = stat.S_IMODE(target.stat().st_mode)
    temp.write_text(new, encoding="utf-8")
    os.chmod(temp, mode)
    os.chown(temp, target.stat().st_uid, target.stat().st_gid)
    changed = False
    try:
        # Install first, then validate the actual loaded configuration. If the
        # active configuration becomes invalid, restore the exact prior file.
        os.replace(target, backup)
        os.replace(temp, target)
        changed = True
        test = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=20)
        if test.returncode != 0:
            print(f"FargoVPN nginx guard: nginx -t после изменения {target}: {(test.stderr or test.stdout).strip()}", file=sys.stderr)
            return False
        reload_result = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=20)
        if reload_result.returncode != 0:
            print(f"FargoVPN nginx guard: reload nginx: {(reload_result.stderr or reload_result.stdout).strip()}", file=sys.stderr)
            return False
        ok = _location_exists_in_nginx()
        if ok:
            _persist_public_identity()
            backup.unlink(missing_ok=True)
        else:
            print(f"FargoVPN nginx guard: маршрут {p} отсутствует в nginx -T после изменения {target}", file=sys.stderr)
        return ok
    except Exception as exc:
        print(f"FargoVPN nginx guard: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    finally:
        if changed and backup.exists():
            os.replace(backup, target)
            subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=20)
        temp.unlink(missing_ok=True)

if __name__=="__main__":
    args=set(os.sys.argv[1:])
    if "--check" in args:
        raise SystemExit(0 if _location_exists_in_nginx() else 1)
    if "--print-url" in args:
        url = _persist_public_identity() or public_url()
        if url:
            print(url)
            raise SystemExit(0)
        raise SystemExit(1)
    once="--once" in args
    if once:
        try:
            raise SystemExit(0 if ensure_once() else 1)
        except Exception as error:
            print(f"FargoVPN nginx guard error: {type(error).__name__}: {error}", flush=True)
            raise SystemExit(1)
    while True:
        try:
            ensure_once()
        except Exception as error:
            print(f"FargoVPN nginx guard error: {type(error).__name__}: {error}", flush=True)
        time.sleep(max(60, int(cfg("NGINX_GUARD_INTERVAL_SECONDS", 300))))
