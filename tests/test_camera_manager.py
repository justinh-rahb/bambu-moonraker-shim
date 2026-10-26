import unittest
from unittest.mock import patch

from bambu_moonraker_shim.camera_manager import (
    P1CameraManager,
    build_auth_packet,
    is_jpeg_frame,
)


class CameraAuthPacketTests(unittest.TestCase):
    def test_build_auth_packet_uses_expected_layout(self):
        packet = build_auth_packet("12345678")

        self.assertEqual(len(packet), 80)
        self.assertEqual(packet[:16], bytes.fromhex("40000000003000000000000000000000"))
        self.assertEqual(packet[16:20], b"bblp")
        self.assertEqual(packet[48:56], b"12345678")

    def test_build_auth_packet_rejects_overlong_access_code(self):
        with self.assertRaises(ValueError):
            build_auth_packet("x" * 33)

    def test_is_jpeg_frame_checks_markers(self):
        self.assertTrue(is_jpeg_frame(b"\xff\xd8\xff\xe0abc\xff\xd9"))
        self.assertFalse(is_jpeg_frame(b"not-a-jpeg"))


class CameraManagerConfigTests(unittest.TestCase):
    def test_builtin_webcam_uses_expected_urls(self):
        with patch("bambu_moonraker_shim.camera_manager.Config.BAMBU_HOST", "192.168.1.5"), patch(
            "bambu_moonraker_shim.camera_manager.Config.BAMBU_ACCESS_CODE", "87654321"
        ), patch("bambu_moonraker_shim.camera_manager.Config.BAMBU_CAMERA_ENABLED", "true"):
            manager = P1CameraManager()

        webcam = manager.get_builtin_webcam()
        self.assertIsNotNone(webcam)
        self.assertEqual(webcam["stream_url"], "/webcam?action=stream")
        self.assertEqual(webcam["snapshot_url"], "/webcam?action=snapshot")
        self.assertEqual(webcam["uid"], P1CameraManager.BUILTIN_UID)


class MoonrakerWebcamTests(unittest.TestCase):
    def test_current_webcams_prepends_builtin_and_filters_duplicate_uid(self):
        from bambu_moonraker_shim import moonraker_api

        builtin = {"uid": "bambu-p1-camera", "name": "Bambu Camera"}
        stored = [
            {"uid": "bambu-p1-camera", "name": "Duplicate"},
            {"uid": "user-camera", "name": "USB Cam"},
        ]

        with patch.object(moonraker_api.camera_manager, "get_builtin_webcam", return_value=builtin), patch.object(
            moonraker_api.database_manager, "get_item", return_value=stored
        ):
            webcams = moonraker_api._current_webcams()

        self.assertEqual(webcams, [{**builtin, "name": "Duplicate"}, {"uid": "user-camera", "name": "USB Cam"}])

    def test_mjpeg_chunk_has_boundary_and_headers(self):
        from bambu_moonraker_shim import moonraker_api

        frame = b"\xff\xd8\xff\xe0abc\xff\xd9"
        chunk = moonraker_api._mjpeg_chunk(frame)

        self.assertTrue(chunk.startswith(b"--frame\r\n"))
        self.assertIn(b"Content-Type: image/jpeg\r\n", chunk)
        self.assertIn(b"Content-Length: 9\r\n\r\n", chunk)
        self.assertTrue(chunk.endswith(frame + b"\r\n"))


if __name__ == "__main__":
    unittest.main()
