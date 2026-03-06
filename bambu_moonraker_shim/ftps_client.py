import ftplib
import re
import socket
import ssl
import threading
import time
from typing import Any, Dict, List, Optional

from bambu_moonraker_shim.config import Config


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """
    FTPS client subclass that supports implicit TLS (port 990) and an
    A1/A1 Mini data channel workaround where the data socket must stay plain.
    """

    def __init__(
        self,
        host: str = "",
        user: str = "",
        passwd: str = "",
        acct: str = "",
        keyfile=None,
        certfile=None,
        timeout: int = 60,
        context=None,
        skip_data_tls: bool = False,
    ):
        self._skip_data_tls = bool(skip_data_tls)
        super().__init__(
            host=host,
            user=user,
            passwd=passwd,
            acct=acct,
            keyfile=keyfile,
            certfile=certfile,
            timeout=timeout,
            context=context,
        )

    def connect(self, host: str = "", port: int = 0, timeout: int = -999):
        if host:
            self.host = host
        self.port = port if port > 0 else 990
        if timeout != -999:
            self.timeout = timeout

        for result in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            self.af = result[0]
            break

        plain_sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self.context.wrap_socket(plain_sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        # Explicitly call FTP.ntransfercmd to avoid FTP_TLS wrapping logic.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p and not self._skip_data_tls:
            conn = self.context.wrap_socket(conn, server_hostname=self.host)
        return conn, size


class BambuFTPSClient:
    def __init__(self):
        self.host = Config.BAMBU_HOST
        self.port = Config.BAMBU_FTPS_PORT
        self.user = Config.BAMBU_FTPS_USER
        self.password = Config.BAMBU_FTPS_PASS
        self.model = str(Config.BAMBU_MODEL or "").upper()

        self._retry_delays_seconds = [2, 4, 8]
        self._chunk_size_bytes = 64 * 1024
        self._operation_lock = threading.Lock()

        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

        self.ftp: Optional[ImplicitFTP_TLS] = None

    def _is_a1_series(self) -> bool:
        return "A1" in self.model

    def _build_remote_path(self, remote_name: str) -> str:
        if remote_name.startswith("/"):
            return remote_name
        return f"{Config.BAMBU_FTPS_UPLOADS_DIR.rstrip('/')}/{remote_name.lstrip('/')}"

    def _reset_connection(self):
        if self.ftp:
            try:
                self.ftp.close()
            except Exception:
                pass
        self.ftp = None

    def _with_retry(self, operation_name: str, operation):
        last_error: Optional[Exception] = None
        for attempt in range(len(self._retry_delays_seconds) + 1):
            try:
                # The underlying FTP client/connection is not thread-safe.
                # Serialize FTPS operations so async callers using to_thread
                # don't corrupt shared socket/session state.
                with self._operation_lock:
                    return operation()
            except Exception as exc:
                last_error = exc
                self._reset_connection()
                if attempt >= len(self._retry_delays_seconds):
                    break
                delay = self._retry_delays_seconds[attempt]
                print(
                    f"FTPS {operation_name} failed (attempt {attempt + 1}), "
                    f"retrying in {delay}s: {exc}"
                )
                time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"FTPS {operation_name} failed without an exception")

    def connect(self):
        """Establishes the FTPS connection."""
        if self.ftp:
            try:
                self.ftp.voidcmd("NOOP")
                return
            except Exception:
                self._reset_connection()

        print(f"Connecting to FTPS {self.host}:{self.port}...")
        try:
            self.ftp = ImplicitFTP_TLS(
                context=self.context,
                skip_data_tls=self._is_a1_series(),
            )
            self.ftp.connect(host=self.host, port=self.port, timeout=10)
            self.ftp.login(user=self.user, passwd=self.password)
            self.ftp.set_pasv(True)
            if self._is_a1_series():
                try:
                    self.ftp.prot_p()
                except Exception as exc:
                    print(f"A1 FTPS prot_p failed, using prot_c fallback: {exc}")
                    self.ftp.prot_c()
            else:
                self.ftp.prot_p()
            print("FTPS Connected!")
        except Exception as exc:
            print(f"FTPS Connection Error: {exc}")
            self._reset_connection()
            raise

    def _list_files_once(self, path: str = "/") -> List[Dict[str, Any]]:
        self.connect()
        files: List[Dict[str, Any]] = []

        known_dirs = {
            "logger",
            "recorder",
            "image",
            "ipcam",
            "timelapse",
            "cache",
            "language",
            "model",
            "corelogger",
            "verify_job",
            ".Spotlight-V100",
            ".fseventsd",
        }

        try:
            for name, facts in self.ftp.mlsd(path):
                if name in {".", ".."}:
                    continue
                is_dir = facts.get("type") == "dir"
                size = int(facts.get("size", 0))
                modified = time.time()
                modified_raw = facts.get("modify")
                if modified_raw:
                    try:
                        modified = time.mktime(time.strptime(modified_raw, "%Y%m%d%H%M%S"))
                    except Exception:
                        pass
                files.append(
                    {
                        "name": name,
                        "is_dir": is_dir,
                        "size": size,
                        "modified": modified,
                    }
                )
        except ftplib.error_perm:
            print("MLSD failed, falling back to NLST with SIZE lookups")
            names = self.ftp.nlst(path)
            for name in names:
                if name in {".", ".."}:
                    continue
                full_path = f"/{name}" if path == "/" else f"{path.rstrip('/')}/{name}"
                is_dir = name in known_dirs
                size = 0
                if not is_dir:
                    try:
                        value = self.ftp.size(full_path)
                        size = int(value or 0)
                    except Exception:
                        if "." not in name:
                            is_dir = True
                files.append(
                    {
                        "name": name,
                        "is_dir": is_dir,
                        "size": size,
                        "modified": time.time(),
                    }
                )

        return files

    def list_files(self, path: str = "/") -> List[Dict[str, Any]]:
        return self._with_retry("list_files", lambda: self._list_files_once(path))

    def _upload_file_once(self, local_path: str, remote_filename: str):
        self.connect()
        target_path = self._build_remote_path(remote_filename)
        print(f"Uploading {local_path} to {target_path}...")

        with open(local_path, "rb") as fp:
            conn = self.ftp.transfercmd(f"STOR {target_path}")
            try:
                while True:
                    chunk = fp.read(self._chunk_size_bytes)
                    if not chunk:
                        break
                    conn.sendall(chunk)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            self.ftp.voidresp()

        print("Upload complete")

    def upload_file(self, local_path: str, remote_filename: str):
        self._with_retry(
            "upload_file",
            lambda: self._upload_file_once(local_path, remote_filename),
        )

    def _download_file_once(self, remote_path: str) -> bytes:
        self.connect()
        target = self._build_remote_path(remote_path)
        print(f"Downloading {target}...")
        data = bytearray()
        try:
            self.ftp.retrbinary(f"RETR {target}", data.extend)
        except ftplib.error_perm as exc:
            if "550" in str(exc):
                raise FileNotFoundError(target) from exc
            raise
        return bytes(data)

    def download_file(self, remote_path: str) -> bytes:
        return self._with_retry("download_file", lambda: self._download_file_once(remote_path))

    def _delete_file_once(self, remote_filename: str):
        self.connect()
        target_path = self._build_remote_path(remote_filename)
        print(f"Deleting {target_path}...")
        self.ftp.delete(target_path)

    def delete_file(self, remote_filename: str):
        self._with_retry("delete_file", lambda: self._delete_file_once(remote_filename))

    @staticmethod
    def _extract_named_number(payload: str, label: str) -> Optional[int]:
        patterns = [
            rf"{label}\s*[:=]\s*([0-9]+)",
            rf"{label}\s+([0-9]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, payload, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    pass
        return None

    def _get_storage_info_once(self) -> Dict[str, int]:
        self.connect()
        total = None
        used = None
        free = None

        for command in ("SITE STORAGE", "SITE DF", "STATFS", "STAT /", "STAT"):
            try:
                response = self.ftp.sendcmd(command)
            except Exception:
                continue

            if total is None:
                total = self._extract_named_number(response, "total")
            if used is None:
                used = self._extract_named_number(response, "used")
            if free is None:
                free = self._extract_named_number(response, "free")

            if total is not None and used is not None and free is not None:
                break

        if used is None:
            try:
                root_listing = self._list_files_once("/")
                used = sum(item.get("size", 0) for item in root_listing if not item.get("is_dir"))
            except Exception:
                used = 0

        if total is None and free is not None:
            total = used + free
        if free is None and total is not None:
            free = max(total - used, 0)
        if total is None:
            # Assume 32GB default for Bambu SD cards if not reported
            total = max(32 * 1024 * 1024 * 1024, used)
        if free is None:
            free = max(total - used, 0)

        return {"total": int(total), "used": int(used), "free": int(free)}

    def get_storage_info(self) -> Dict[str, int]:
        return self._with_retry("get_storage_info", self._get_storage_info_once)


ftps_client = BambuFTPSClient()
