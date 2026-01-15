import ftplib
import ssl
import socket
import time
from typing import List, Dict, Optional, Any
from bambu_moonraker_shim.config import Config

class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """
    FTPS client subclass that supports Implicit TLS (required for Bambu printers on port 990).
    Standard ftplib.FTP_TLS defaults to Explicit TLS (AUTH TLS).
    """
    def __init__(self, host='', user='', passwd='', acct='',
                 keyfile=None, certfile=None, timeout=60, context=None):
        super().__init__(host=host, user=user, passwd=passwd, acct=acct,
                         keyfile=keyfile, certfile=certfile, timeout=timeout, context=context)

    def connect(self, host='', port=0, timeout=-999):
        """
        Connect to host with implicit TLS.
        Arguments:
        - host: hostname to connect to
        - port: port to connect to (defaults to 990 for implicit SSL)
        - timeout: connection timeout in seconds
        """
        if host != '':
            self.host = host
        if port > 0:
            self.port = port
        else:
            self.port = 990  # Default for implicit FTPS
        if timeout != -999:
            self.timeout = timeout
        
        # Get address info to determine address family (IPv4 vs IPv6)
        # This sets self.af which ftplib expects
        for res in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            self.af = res[0]
            break
        
        # Create a plain socket first
        plain_sock = socket.create_connection((self.host, self.port), self.timeout)
        
        # Wrap it with SSL immediately for implicit mode
        self.sock = self.context.wrap_socket(
            plain_sock,
            server_hostname=self.host
        )
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

class BambuFTPSClient:
    def __init__(self):
        self.host = Config.BAMBU_HOST
        self.port = Config.BAMBU_FTPS_PORT
        self.user = Config.BAMBU_FTPS_USER
        self.password = Config.BAMBU_FTPS_PASS
        
        # SSL Context
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE
        
        self.ftp: Optional[ImplicitFTP_TLS] = None

    def connect(self):
        """Establishes the FTPS connection."""
        if self.ftp:
            try:
                self.ftp.voidcmd("NOOP")
                return # Already connected
            except:
                self.ftp = None # Reconnect
        
        print(f"Connecting to FTPS {self.host}:{self.port}...")
        try:
            self.ftp = ImplicitFTP_TLS(context=self.context)
            self.ftp.connect(host=self.host, port=self.port, timeout=10)
            self.ftp.login(user=self.user, passwd=self.password)
            self.ftp.prot_p() # Secure data connection
            print("FTPS Connected!")
        except Exception as e:
            print(f"FTPS Connection Error: {e}")
            self.ftp = None
            raise

    def list_files(self, path: str = "/") -> List[Dict[str, Any]]:
        """
        Lists files in a directory. 
        Returns parsed list of dicts: {name, size, modified, is_dir}
        """
        self.connect()
        files = []
        
        # Known directories on Bambu printers to filter out
        known_dirs = {
            "logger", "recorder", "image", "ipcam", "timelapse", "cache",
            "language", "model", "corelogger", "verify_job", ".Spotlight-V100",
            ".fseventsd"
        }
        
        try:
            # Try MLSD first (structured listing)
            for name, facts in self.ftp.mlsd(path):
                if name in [".", ".."]:
                    continue
                    
                is_dir = facts.get("type") == "dir"
                size = int(facts.get("size", 0))
                modified_raw = facts.get("modify")
                
                modified = time.time()
                if modified_raw:
                    try:
                        struct_time = time.strptime(modified_raw, "%Y%m%d%H%M%S")
                        modified = time.mktime(struct_time)
                    except:
                        pass

                files.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": size,
                    "modified": modified
                })
                
        except ftplib.error_perm:
            # Fallback to NLST (Bambu doesn't support MLSD)
            print("MLSD failed, falling back to NLST with SIZE lookups")
            try:
                names = self.ftp.nlst(path)
                for name in names:
                    # Skip bare directory markers
                    if name in [".", ".."]:
                        continue
                    
                    # Determine path for SIZE command
                    if path == "/":
                        full_path = f"/{name}"
                    else:
                        full_path = f"{path.rstrip('/')}/{name}"
                    
                    # Check if it's a known directory
                    is_dir = name in known_dirs
                    
                    # Try to get file size (will fail for directories)
                    size = 0
                    if not is_dir:
                        try:
                            size = self.ftp.size(full_path)
                            if size is None:
                                size = 0
                        except:
                            # SIZE failed, might be a directory or unsupported
                            # If it has no extension, likely a directory
                            if "." not in name:
                                is_dir = True
                    
                    files.append({
                        "name": name,
                        "is_dir": is_dir,
                        "size": size,
                        "modified": time.time()  # NLST doesn't give timestamps
                    })
            except Exception as e:
                print(f"NLST Error: {e}")
                
        return files

    def upload_file(self, local_path: str, remote_filename: str):
        """Uploads a local file to the printer."""
        self.connect()
        target_path = f"{Config.BAMBU_FTPS_UPLOADS_DIR.rstrip('/')}/{remote_filename}".replace("//", "/")
        print(f"Uploading {local_path} to {target_path}...")
        
        try:
            self._ensure_remote_dirs(target_path)
            with open(local_path, "rb") as fp:
                # Use storbinary to upload
                self.ftp.storbinary(f"STOR {target_path}", fp)
            print("Upload complete")
            
        except socket.timeout:
            # Bambu FTPS quirk: sometimes times out at end of upload even when successful
            # This is the known "quirky close" behavior mentioned in research
            print("Upload completed (timeout at close, likely successful)")
            # Force reconnect next time to avoid stale connection
            self.ftp = None
            
        except Exception as e:
            print(f"Upload Error: {e}")
            # Force reconnect on any error
            self.ftp = None
            raise

    def _ensure_remote_dirs(self, full_remote_path: str):
        parts = full_remote_path.strip("/").split("/")
        if len(parts) <= 1:
            return

        parent_parts = parts[:-1]
        current = ""
        for part in parent_parts:
            current += f"/{part}"
            try:
                self.ftp.mkd(current)
            except Exception:
                pass

    def delete_file(self, remote_filename: str):
        """Deletes a file on the printer."""
        self.connect()
        target_path = f"{Config.BAMBU_FTPS_UPLOADS_DIR.rstrip('/')}/{remote_filename}"
        print(f"Deleting {target_path}...")
        try:
            self.ftp.delete(target_path)
        except Exception as e:
            print(f"Delete Error: {e}")
            raise
            
ftps_client = BambuFTPSClient()
