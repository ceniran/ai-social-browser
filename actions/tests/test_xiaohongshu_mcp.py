import importlib.util
import unittest
from pathlib import Path


SERVER = Path(__file__).parents[1] / "xiaohongshu-mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_mcp", SERVER)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class XiaohongshuMcpTests(unittest.TestCase):
    def test_lists_read_only_tools(self):
        response = MODULE.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {"xhs_feed", "xhs_search", "xhs_read", "xhs_watch_video", "xhs_profile"})

    def test_rejects_non_xiaohongshu_media(self):
        with self.assertRaisesRegex(ValueError, "白名单"):
            MODULE._safe_media_url("https://example.com/video.mp4")

    def test_accepts_xiaohongshu_cdn_media(self):
        url = "https://sns-video-zl.xhscdn.com/stream/example.mp4"
        self.assertEqual(MODULE._safe_media_url(url), url)

    def test_initialize_declares_tools(self):
        response = MODULE.handle({"jsonrpc": "2.0", "id": 7, "method": "initialize"})
        self.assertEqual(response["id"], 7)
        self.assertIn("tools", response["result"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
