import unittest
from unittest.mock import Mock, patch
from bambu_moonraker_shim.ftps_client import DownloadLimitError

from bambu_moonraker_shim.ftps_client import BambuFTPSClient


class FtpsClientTests(unittest.TestCase):
    def test_build_remote_path(self):
        client = BambuFTPSClient()
        self.assertTrue(client._build_remote_path("file.gcode").endswith("/file.gcode"))
        self.assertEqual(client._build_remote_path("/absolute/file.gcode"), "/absolute/file.gcode")

    def test_a1_series_detection(self):
        client = BambuFTPSClient()
        client.model = "A1 MINI"
        self.assertTrue(client._is_a1_series())
        client.model = "X1C"
        self.assertFalse(client._is_a1_series())

    def test_metadata_download_limit_is_not_retried(self):
        client = BambuFTPSClient()
        client.ftp = Mock()
        client.connect = Mock()
        client.ftp.retrbinary.side_effect = lambda command, callback: callback(b'12345')
        with patch.object(client, '_reset_connection') as reset, self.assertRaises(DownloadLimitError):
            client.download_file('file.3mf', max_size=4)
        client.ftp.retrbinary.assert_called_once()
        reset.assert_called_once()

    def test_listing_modification_time_is_utc_or_unknown(self):
        client = BambuFTPSClient()
        client.ftp = Mock()
        client.connect = Mock()
        client.ftp.mlsd.return_value = [('dated.gcode', {'size': '4', 'modify': '20260101000000', 'type': 'file'}), ('unknown.gcode', {'size': '2', 'type': 'file'})]
        entries = client._list_files_once('/')
        self.assertEqual(entries[0]['modified'], 1767225600)
        self.assertEqual(entries[1]['modified'], 0)


if __name__ == "__main__":
    unittest.main()
