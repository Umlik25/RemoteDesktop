import unittest

from core import video_encoder


class VideoEncoderTests(unittest.TestCase):
    def test_4k_bitrate_scales_with_fps_and_fits_network_budget(self):
        at_60 = video_encoder.target_bitrate_mbps(3840, 2160, 60, 70, 700)
        at_120 = video_encoder.target_bitrate_mbps(3840, 2160, 120, 70, 700)

        self.assertGreater(at_60, 40)
        self.assertGreater(at_120, at_60)
        self.assertLessEqual(at_120, 200)

    def test_nvenc_command_is_low_latency_fragmented_mp4(self):
        capabilities = {"available": True, "executable": "ffmpeg"}

        cmd = video_encoder.command(capabilities, 60, 70, 3840, 2160, 700)
        joined = " ".join(cmd)

        self.assertIn("ddagrab=output_idx=0:framerate=60:draw_mouse=0", joined)
        self.assertIn("-c:v h264_nvenc", joined)
        self.assertIn("-tune ull", joined)
        self.assertIn("-bf 0", joined)
        self.assertIn("frag_every_frame", joined)
        self.assertEqual("pipe:1", cmd[-1])

    def test_nvenc_selects_requested_dxgi_device_and_output(self):
        capabilities = {"available": True, "executable": "ffmpeg"}

        cmd = video_encoder.command(
            capabilities, 120, 70, 2560, 1440, 700, output_idx=2, device_idx=1)
        joined = " ".join(cmd)

        self.assertIn("-init_hw_device d3d11va=grab:1", joined)
        self.assertIn("-filter_hw_device grab", joined)
        self.assertIn("ddagrab=output_idx=2:framerate=120", joined)

    def test_unavailable_encoder_rejects_command(self):
        with self.assertRaises(ValueError):
            video_encoder.command(video_encoder.unavailable("missing"), 60, 70, 1920, 1080)


if __name__ == "__main__":
    unittest.main()
