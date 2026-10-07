#!/usr/bin/env python3
"""Read-only MCP adapter for the local Xiaohongshu action service."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ACTION_URL = os.environ.get("XHS_ACTION_URL", "http://127.0.0.1:8273/xiaohongshu")
FFMPEG = os.environ.get("XHS_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("XHS_FFPROBE") or shutil.which("ffprobe")
WHISPER = os.environ.get("XHS_WHISPER") or shutil.which("whisper-cli")
WHISPER_MODEL = os.environ.get("XHS_WHISPER_MODEL")
MAX_VIDEO_BYTES = int(os.environ.get("XHS_MAX_VIDEO_BYTES", str(100 * 1024 * 1024)))
MAX_VIDEO_SECONDS = float(os.environ.get("XHS_MAX_VIDEO_SECONDS", "600"))


TOOLS = [
    {
        "name": "xhs_feed",
        "description": "读取已登录小红书账号的发现页。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 30}},
        },
    },
    {
        "name": "xhs_search",
        "description": "按关键词搜索小红书笔记。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 100},
                "n": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
        },
    },
    {
        "name": "xhs_read",
        "description": "读取小红书笔记的标题、正文、图片、作者与评论。支持 xhslink.com/.cn 分享短链。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "comments": {"type": "integer", "minimum": 0, "maximum": 40},
            },
            "required": ["url"],
        },
    },
    {
        "name": "xhs_watch_video",
        "description": "实际取得小红书视频，返回笔记资料、机器音频转写和八格抽样画面。转写可能误认专名，应与字幕和上下文核对。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "minLength": 1}},
            "required": ["url"],
        },
    },
    {
        "name": "xhs_profile",
        "description": "读取小红书用户资料与最近笔记。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "user": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "anyOf": [{"required": ["url"]}, {"required": ["user"]}],
        },
    },
]


def _action(action, **arguments):
    request = urllib.request.Request(
        ACTION_URL,
        data=json.dumps({"action": action, **arguments}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"小红书动作服务不可用：{exc}"}


def _safe_media_url(url):
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "xhscdn.com" or host.endswith(".xhscdn.com")
    ):
        raise ValueError("视频地址不在小红书 CDN 白名单内")
    return url


def _download_video(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
        _safe_media_url(response.geturl())
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > MAX_VIDEO_BYTES:
            raise ValueError("视频超过读取上限")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                raise ValueError("视频超过读取上限")
            output.write(chunk)


def _probe_duration(video):
    if not FFPROBE:
        raise ValueError("找不到 ffprobe；请安装 FFmpeg 或设置 XHS_FFPROBE")
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    duration = float(completed.stdout.strip())
    if duration > MAX_VIDEO_SECONDS:
        raise ValueError("视频超过时长上限")
    return duration


def _transcribe(video, workdir):
    if not (WHISPER and WHISPER_MODEL):
        return None, "未配置 whisper-cli 与模型；设置 XHS_WHISPER_MODEL 后可生成音频转写"
    if not (Path(WHISPER).is_file() and Path(WHISPER_MODEL).is_file()):
        return None, "Whisper 可执行文件或模型不存在，未生成音频转写"
    if not FFMPEG:
        return None, "找不到 ffmpeg，未生成音频转写"
    audio = workdir / "audio.wav"
    subprocess.run(
        [FFMPEG, "-loglevel", "error", "-y", "-i", str(video), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio)],
        timeout=90,
        check=True,
    )
    output = workdir / "transcript"
    subprocess.run(
        [WHISPER, "-m", WHISPER_MODEL, "-f", str(audio), "-l", "auto", "-t", "2", "-nt", "-otxt", "-of", str(output)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=300,
        check=True,
    )
    return output.with_suffix(".txt").read_text(encoding="utf-8").strip(), None


def _contact_sheet(video, workdir, duration):
    if not FFMPEG:
        raise ValueError("找不到 ffmpeg；请安装 FFmpeg 或设置 XHS_FFMPEG")
    output = workdir / "frames.jpg"
    interval = max(duration / 8.0, 0.25)
    subprocess.run(
        [FFMPEG, "-loglevel", "error", "-y", "-i", str(video), "-vf", f"fps=1/{interval:.3f},scale=320:-1,tile=4x2", "-frames:v", "1", str(output)],
        timeout=120,
        check=True,
    )
    return base64.b64encode(output.read_bytes()).decode("ascii")


def _text_result(value, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def _watch(arguments):
    metadata = _action("watch", url=arguments.get("url"), n=10)
    if not metadata.get("ok"):
        return _text_result(metadata, True)
    try:
        media_url = _safe_media_url((metadata.get("video") or {}).get("url"))
        with tempfile.TemporaryDirectory(prefix="xhs-mcp-") as raw_workdir:
            workdir = Path(raw_workdir)
            video = workdir / "video.mp4"
            _download_video(media_url, video)
            duration = _probe_duration(video)
            transcript, warning = _transcribe(video, workdir)
            image = _contact_sheet(video, workdir, duration)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _text_result({**metadata, "ok": False, "error": f"视频处理失败：{exc}"}, True)
    summary = {
        "ok": True,
        "note": metadata.get("note"),
        "comments": metadata.get("comments"),
        "video": {**(metadata.get("video") or {}), "url": "[redacted after download]"},
        "audio_transcript_raw": transcript,
        "transcript_warning": warning or "机器转写可能误认姓名、专名、方言和重叠语音，请结合抽样画面核对。",
        "frame_sampling": "8 frames across the full duration, arranged left-to-right then top-to-bottom",
    }
    return {
        "content": [
            {"type": "text", "text": json.dumps(summary, ensure_ascii=False, indent=2)},
            {"type": "image", "data": image, "mimeType": "image/jpeg"},
        ]
    }


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "xhs_feed":
        value = _action("feed", n=arguments.get("n", 10))
    elif name == "xhs_search":
        value = _action("search", query=arguments.get("query"), n=arguments.get("n", 10))
    elif name == "xhs_read":
        value = _action("read", url=arguments.get("url"), n=arguments.get("comments", 10))
    elif name == "xhs_profile":
        value = _action("profile", url=arguments.get("url"), user=arguments.get("user"), n=arguments.get("n", 10))
    elif name == "xhs_watch_video":
        return _watch(arguments)
    else:
        return _text_result({"ok": False, "error": f"未知工具：{name}"}, True)
    return _text_result(value, not value.get("ok", False))


def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": (message.get("params") or {}).get(
                    "protocolVersion", "2025-06-18"
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "xiaohongshu-browser", "version": "0.1.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": call_tool(params.get("name"), params.get("arguments")),
        }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
