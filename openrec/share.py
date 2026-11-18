"""局域网分享：只在本机开启 HTTP 文件服务，供手机/同网设备扫码观看或下载。

不连接互联网，不上传任何数据。
"""
import html
import os
import re
import secrets
import socket
import string
import threading
import time
import urllib.parse
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIME = {
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
}


def lan_ip() -> str:
    """本机局域网 IPv4：先让系统按默认路由选网卡，再退回主机名解析。"""
    def _local(ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            # 分享只应绑定可用的局域网地址：排除公网、回环、链路本地与未指定地址
            return (not addr.is_global and not addr.is_loopback
                    and not addr.is_link_local and not addr.is_unspecified)
        except ValueError:
            return False

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # UDP connect 不发送任何数据包，仅用于让系统选出默认出口网卡
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            if _local(ip):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _local(ip):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}TB"


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenRecShare/1.0"
    timeout = 20          # 慢连接最多占用线程 20 秒

    def log_message(self, *_):
        pass

    @property
    def root(self):
        return self.server.owner.root

    @property
    def token(self):
        return self.server.owner.token

    def _files(self):
        try:
            items = []
            for name in sorted(os.listdir(self.root), reverse=True):
                if name.startswith("."):
                    continue
                p = os.path.join(self.root, name)
                if os.path.isfile(p) and os.path.splitext(name)[1].lower() in MIME:
                    items.append((name, os.path.getsize(p)))
            return items
        except Exception:
            return []

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            if not self._authorized(parsed):
                self._serve_login(locked=self.server.owner.is_locked(
                    self.client_address[0]))
                return
            self._serve_index()
            return
        if parsed.path.startswith("/f/"):
            if not self._authorized(parsed):
                self.send_error(429 if self.server.owner.is_locked(
                    self.client_address[0]) else 403, "Access denied")
                return
            name = urllib.parse.unquote(parsed.path[3:])
            self._serve_file(name)
            return
        self.send_error(404)

    def _authorized(self, parsed) -> bool:
        """校验口令；任何解析/比较异常都按失败计数，避免绕过锁定。"""
        ip = self.client_address[0]
        try:
            given = urllib.parse.parse_qs(parsed.query, errors="replace").get("k", [""])[0]
        except Exception:
            given = "?"
        return self.server.owner.check_token(ip, given)

    def _serve_login(self, locked: bool = False):
        """未带口令或口令错误时只返回口令输入页，不泄露任何文件名。"""
        page = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenRec 本地录制</title>
<style>
body{background:#0b2545;color:#e6f1ff;font-family:system-ui,"Microsoft YaHei";margin:0;padding:24px}
h1{font-size:18px;font-weight:600;margin:0 0 16px}
form{display:flex;gap:8px;max-width:360px}
input{flex:1;background:#0e3059;border:1px solid #1f4c7c;border-radius:8px;color:#e6f1ff;
padding:10px 12px;font-size:15px}
button{background:#2c5fa8;border:none;border-radius:8px;color:#fff;padding:10px 16px;font-size:15px}
p{color:#8fb0da;font-size:12px;margin-top:20px}
</style></head><body>
<h1>OpenRec · 本地录制文件</h1>
<form method="get" action="/"><input name="k" placeholder="访问口令" autocomplete="off" autofocus><button>进入</button></form>
<p>请输入电脑端“局域网分享”弹窗中显示的 8 位口令。</p>
<p>__LOCKED__</p>
</body></html>"""
        if locked:
            page = page.replace("__LOCKED__", "口令错误次数过多，请 1 分钟后再试。")
        else:
            page = page.replace("<p>__LOCKED__</p>", "")
        data = page.encode("utf-8")
        self.send_response(429 if locked else 403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_index(self):
        rows = []
        for name, size in self._files():
            href = f"/f/{urllib.parse.quote(name)}?k={self.token}"
            rows.append(
                f'<li><a href="{href}">{html.escape(name)}</a>'
                f'<span>{_fmt_size(size)}</span></li>')
        page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenRec 本地录制</title>
<style>
body{{background:#0b2545;color:#e6f1ff;font-family:system-ui,"Microsoft YaHei";margin:0;padding:24px}}
h1{{font-size:18px;font-weight:600;margin:0 0 16px}}
ul{{list-style:none;padding:0;margin:0}}
li{{display:flex;justify-content:space-between;gap:12px;padding:14px;border-bottom:1px solid #143a66}}
a{{color:#6da4dd;text-decoration:none;word-break:break-all}}
span{{color:#8fb0da;white-space:nowrap}}
p{{color:#6d8fbe;font-size:12px;margin-top:20px}}
</style></head><body>
<h1>OpenRec · 本地录制文件</h1>
<ul>{''.join(rows) or '<li>暂无文件</li>'}</ul>
<p>仅局域网可访问，关闭分享即失效。</p>
</body></html>"""
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, name: str):
        if name != os.path.basename(name) or ":" in name:
            self.send_error(400)
            return
        if name.startswith("."):
            # 隐藏文件不展示也不允许直接下载，与列表页保持一致
            self.send_error(404)
            return
        if os.path.splitext(name)[1].lower() not in MIME:
            self.send_error(404)   # 与列表页保持一致：只提供音视频/图片
            return
        root = os.path.realpath(self.root)
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root:
            self.send_error(400)
            return
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        ctype = MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                first, last = m.group(1), m.group(2)
                try:
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    elif last:
                        # bytes=-N 表示最后 N 字节
                        suffix = int(last)
                        if suffix <= 0:
                            self.send_error(416, "Range Not Satisfiable")
                            return
                        start = max(0, size - suffix)
                        end = size - 1
                    else:
                        self.send_error(416, "Range Not Satisfiable")
                        return
                except (ValueError, OverflowError):
                    self.send_error(416, "Range Not Satisfiable")
                    return
                if start < 0 or start >= size or start > end:
                    self.send_error(416, "Range Not Satisfiable")
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'inline; filename="{urllib.parse.quote(name)}"')
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass


class ShareServer:
    MAX_FAILS = 5              # 连续错误次数
    LOCK_SECONDS = 60          # 触发后的临时锁定时长
    FAIL_MEMORY_SECONDS = 600  # 失败计数保留时长，超时自动遗忘

    def __init__(self, root: str):
        self.root = root
        self.token = "".join(secrets.choice(string.ascii_uppercase + string.digits)
                             for _ in range(8))
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self._thread: threading.Thread | None = None
        self._fail_lock = threading.Lock()
        self._fails: dict[str, int] = {}
        self._fail_ts: dict[str, float] = {}
        self._locked: dict[str, float] = {}

    # ------------------------------------------------ 口令失败计数
    def is_locked(self, ip: str) -> bool:
        with self._fail_lock:
            return self._locked.get(ip, 0.0) > time.monotonic()

    def check_token(self, ip: str, given: str) -> bool:
        """恒定时间比较口令；先编码为 UTF-8 字节，非 ASCII 不会抛异常。"""
        if self.is_locked(ip):
            return False
        try:
            ok = bool(given) and secrets.compare_digest(
                given.encode("utf-8"), self.token.encode("utf-8"))
        except Exception:
            ok = False
        if ok:
            self.clear_failures(ip)
        elif given:
            self.note_failure(ip)
        return ok

    def note_failure(self, ip: str):
        with self._fail_lock:
            now = time.monotonic()
            # 清理过期锁定与长期未再失败的计数，避免字典无限增长
            for key in [k for k, v in self._locked.items() if v <= now]:
                self._locked.pop(key, None)
            for key in [k for k, v in self._fail_ts.items()
                        if now - v > self.FAIL_MEMORY_SECONDS]:
                self._fail_ts.pop(key, None)
                self._fails.pop(key, None)
            count = self._fails.get(ip, 0) + 1
            if count >= self.MAX_FAILS:
                self._locked[ip] = now + self.LOCK_SECONDS
                self._fails.pop(ip, None)
                self._fail_ts.pop(ip, None)
            else:
                self._fails[ip] = count
                self._fail_ts[ip] = now

    def clear_failures(self, ip: str):
        with self._fail_lock:
            self._fails.pop(ip, None)
            self._fail_ts.pop(ip, None)
            self._locked.pop(ip, None)

    def start(self, port: int) -> str:
        self.stop()
        # 每次开启都换新口令：关闭再开时旧 URL 不应恢复可用
        self.token = "".join(secrets.choice(string.ascii_uppercase + string.digits)
                             for _ in range(8))
        with self._fail_lock:
            self._fails.clear()
            self._fail_ts.clear()
            self._locked.clear()
        host = lan_ip()
        last = None
        for candidate in range(port, min(port + 12, 65536)):
            try:
                httpd = ThreadingHTTPServer((host, candidate), _Handler)
            except OSError as exc:
                last = exc
                continue
            httpd.owner = self
            httpd.daemon_threads = True
            self.httpd = httpd
            self.port = candidate
            self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            self._thread.start()
            return self.url()
        raise RuntimeError(f"端口被占用：{last}")

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
            self.port = 0

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def url(self) -> str:
        return f"http://{lan_ip()}:{self.port}/?k={self.token}" if self.port else ""
