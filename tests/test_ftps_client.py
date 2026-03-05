import unittest

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


if __name__ == "__main__":
    unittest.main()
