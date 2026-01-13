import contextlib
import os
import posixpath
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from ftplib import FTP_TLS, error_perm
from typing import List, Optional, Iterable

from config import Config


@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    size: Optional[int]
    modified: Optional[int]


class ImplicitFTP_TLS(FTP_TLS):
    def connect(self, host: str = "", port: int = 0, timeout: Optional[float] = None):
        if timeout is None:
            timeout = self.timeout
        self.sock = socket.create_connection((host, port), timeout)
        if self.context is None:
            self.context = ssl.create_default_context()
        server_hostname = host if self.context.check_hostname else None
        self.sock = self.context.wrap_socket(self.sock, server_hostname=server_hostname)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class BambuFtpsClient:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        base_dir: str,
        verify_cert: bool,
        timeout: float,
        passive: bool,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.base_dir = base_dir
        self.verify_cert = verify_cert
        self.timeout = timeout
        self.passive = passive

    def _ssl_context(self) -> ssl.SSLContext:
        if self.verify_cert:
            return ssl.create_default_context()
        context = ssl._create_unverified_context()
        context.check_hostname = False
        return context

    @contextlib.contextmanager
    def session(self) -> Iterable[FTP_TLS]:
        context = self._ssl_context()
        ftp = ImplicitFTP_TLS(context=context, timeout=self.timeout)
        try:
            ftp.connect(self.host, self.port)
            ftp.login(self.user, self.password)
            ftp.prot_p()
            ftp.set_pasv(self.passive)
            yield ftp
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    def _to_epoch(self, mdtm: str) -> Optional[int]:
        if not mdtm:
            return None
        try:
            timestamp = datetime.strptime(mdtm, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
            return int(timestamp.timestamp())
        except ValueError:
            return None

    def _parse_mdtm_response(self, response: str) -> Optional[int]:
        if not response:
            return None
        parts = response.split()
        if len(parts) < 2:
            return None
        return self._to_epoch(parts[-1])

    def _ensure_base(self, path: str) -> str:
        if not path:
            path = "."
        if path.startswith("/"):
            return path
        return posixpath.join(self.base_dir, path)

    def list_dir(self, path: str) -> List[RemoteEntry]:
        target = self._ensure_base(path)
        entries: List[RemoteEntry] = []
        with self.session() as ftp:
            try:
                for name, facts in ftp.mlsd(target):
                    if name in (".", ".."):
                        continue
                    entry_type = facts.get("type", "file")
                    is_dir = entry_type == "dir"
                    size = None
                    if not is_dir:
                        try:
                            size = int(facts.get("size")) if facts.get("size") else None
                        except ValueError:
                            size = None
                    modified = self._to_epoch(facts.get("modify", ""))
                    entries.append(
                        RemoteEntry(
                            name=name,
                            is_dir=is_dir,
                            size=size,
                            modified=modified,
                        )
                    )
                return entries
            except error_perm:
                pass

            names = ftp.nlst(target)
            for name in names:
                basename = posixpath.basename(name.rstrip("/"))
                if basename in (".", ".."):
                    continue
                remote_path = name
                size = None
                modified = None
                is_dir = False
                try:
                    size = ftp.size(remote_path)
                except error_perm:
                    is_dir = True
                try:
                    mdtm_response = ftp.sendcmd(f"MDTM {remote_path}")
                    modified = self._parse_mdtm_response(mdtm_response)
                except error_perm:
                    pass
                entries.append(
                    RemoteEntry(
                        name=basename,
                        is_dir=is_dir,
                        size=size,
                        modified=modified,
                    )
                )
        return entries

    def upload(self, fileobj, remote_path: str) -> None:
        target = self._ensure_base(remote_path)
        with self.session() as ftp:
            fileobj.seek(0, os.SEEK_SET)
            ftp.storbinary(f"STOR {target}", fileobj, blocksize=1024 * 1024)

    def delete(self, remote_path: str) -> None:
        target = self._ensure_base(remote_path)
        with self.session() as ftp:
            ftp.delete(target)

    def stat(self, remote_path: str) -> RemoteEntry:
        target = self._ensure_base(remote_path)
        with self.session() as ftp:
            basename = posixpath.basename(target.rstrip("/"))
            size = None
            modified = None
            try:
                size = ftp.size(target)
            except error_perm:
                size = None
            try:
                mdtm_response = ftp.sendcmd(f"MDTM {target}")
                modified = self._parse_mdtm_response(mdtm_response)
            except error_perm:
                modified = None
            return RemoteEntry(
                name=basename,
                is_dir=False,
                size=size,
                modified=modified,
            )


ftps_client = BambuFtpsClient(
    host=Config.BAMBU_HOST,
    port=Config.FTPS_PORT,
    user=Config.FTPS_USER,
    password=Config.FTPS_PASSWORD,
    base_dir=Config.FTPS_BASE_DIR,
    verify_cert=Config.FTPS_VERIFY_CERT,
    timeout=Config.FTPS_TIMEOUT,
    passive=True,
)
