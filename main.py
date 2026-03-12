"""
VidFlow Backend API
依赖: fastapi, uvicorn, yt-dlp, aiofiles, python-multipart
启动: uvicorn main:app --reload --port 8000
"""

import re
import os
import uuid
import asyncio
import tempfile
import traceback
import urllib.request
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="VidFlow API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境请限定为你的前端域名
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "vidflow_downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_url(text: str) -> str:
    """从分享文案中提取第一个 URL，自动处理抖音各种链接格式"""
    text = text.strip()
    if re.match(r'^https?://', text):
        raw_url = text.rstrip('/ ').rstrip()
    else:
        urls = re.findall(r'https?://[^\s\u4e00-\u9fff，。！？、]+', text)
        if not urls:
            raise ValueError("无法从输入中提取有效链接")
        raw_url = urls[0].rstrip('/')
    return _normalize_douyin_url(raw_url)


def _normalize_douyin_url(url: str) -> str:
    """将所有抖音链接统一转为 yt-dlp 可识别的标准格式"""
    if not any(d in url for d in ('douyin.com', 'iesdouyin.com')):
        return url
    # 直接从 URL 提取 video_id（15-20位数字）
    # 覆盖: iesdouyin.com/share/video/7615913229033704750/ 等所有格式
    import re as _re
    match = _re.search(r'(?:video[/_]|aweme_id=|item_ids=)(\d{15,20})', url)
    if match:
        return f'https://www.douyin.com/video/{match.group(1)}'
    # v.douyin.com 短链跟踪重定向
    if 'v.douyin.com' in url:
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15'
            })
            opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
            resp = opener.open(req, timeout=10)
            return _normalize_douyin_url(resp.url)
        except Exception:
            pass
    return url

def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "未知"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def bytes_to_human(b: Optional[int]) -> str:
    if not b:
        return "未知"
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} GB"


# ─── Schemas ──────────────────────────────────────────────────────────────────

class ParseRequest(BaseModel):
    url: str          # 可以是原始 URL 或含链接的分享文案

class DownloadRequest(BaseModel):
    url: str
    format_id: str    # yt-dlp format id，如 "bestvideo[height<=1080]+bestaudio/best"
    filename: Optional[str] = None

class TranscribeRequest(BaseModel):
    url: str
    language: Optional[str] = None   # "zh", "en", "ja" 或 None（自动）


# ─── 1. 解析视频信息 ──────────────────────────────────────────────────────────

@app.post("/api/parse")
async def parse_video(req: ParseRequest):
    """
    解析视频元数据 + 可用画质列表
    支持抖音分享文案直接粘贴
    """
    try:
        real_url = extract_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        # 抖音/TikTok 反爬：伪装成移动端浏览器
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
            ),
            "Referer": "https://www.douyin.com/",
        },
        "socket_timeout": 30,
    }

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: _extract_info(real_url, ydl_opts))
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"无法解析视频：{e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 整理可用格式
    formats = _build_format_list(info.get("formats", []))

    return {
        "title": info.get("title", "未知标题"),
        "author": info.get("uploader") or info.get("channel") or "未知作者",
        "duration": format_duration(info.get("duration")),
        "thumbnail": info.get("thumbnail"),
        "platform": info.get("extractor_key", "Unknown"),
        "original_url": real_url,
        "formats": formats,
    }


def _extract_info(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _build_format_list(formats: list) -> list:
    """整理成前端友好的格式列表"""
    seen_heights = set()
    result = []

    # 视频格式（按分辨率降序）
    video_formats = [
        f for f in formats
        if f.get("vcodec") != "none" and f.get("height")
    ]
    video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)

    for f in video_formats:
        h = f.get("height", 0)
        label = f"{h}p" if h < 2160 else "4K"
        if label in seen_heights:
            continue
        seen_heights.add(label)
        result.append({
            "format_id": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]",
            "label": label,
            "ext": "mp4",
            "filesize": bytes_to_human(f.get("filesize") or f.get("filesize_approx")),
            "type": "video",
        })

    # 音频格式
    audio_formats = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") != "none"
    ]
    if audio_formats:
        best_audio = sorted(audio_formats, key=lambda x: x.get("abr", 0) or 0, reverse=True)[0]
        result.append({
            "format_id": "bestaudio/best",
            "label": "MP3",
            "ext": "mp3",
            "filesize": bytes_to_human(best_audio.get("filesize") or best_audio.get("filesize_approx")),
            "type": "audio",
        })

    # 保底：没有拆分格式时
    if not result:
        result.append({
            "format_id": "best",
            "label": "最高画质",
            "ext": "mp4",
            "filesize": "未知",
            "type": "video",
        })

    return result


# ─── 2. 下载视频 ──────────────────────────────────────────────────────────────

@app.post("/api/download")
async def download_video(req: DownloadRequest, background_tasks: BackgroundTasks):
    """
    下载视频并返回文件流
    """
    try:
        real_url = extract_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    file_id = uuid.uuid4().hex
    out_dir = DOWNLOAD_DIR / file_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(title).80s.%(ext)s")

    is_audio = req.format_id == "bestaudio/best"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": req.format_id,
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
            ),
        },
        "socket_timeout": 60,
    }

    if is_audio:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _do_download(real_url, ydl_opts))
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"下载失败：{e}")

    # 找到下载的文件
    files = list(out_dir.iterdir())
    if not files:
        raise HTTPException(status_code=500, detail="下载完成但未找到文件")

    filepath = files[0]
    media_type = "audio/mpeg" if is_audio else "video/mp4"
    dl_name = filepath.name

    # 请求完成后删除临时文件
    background_tasks.add_task(_cleanup, out_dir)

    return FileResponse(
        path=str(filepath),
        media_type=media_type,
        filename=dl_name,
        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
    )


def _do_download(url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _cleanup(path: Path):
    import shutil
    try:
        shutil.rmtree(path)
    except Exception:
        pass


# ─── 3. 语音转文字（需要安装 openai-whisper 或 faster-whisper）────────────────

@app.post("/api/transcribe")
async def transcribe_video(req: TranscribeRequest):
    """
    下载视频音频 → Whisper 转录
    需额外安装: pip install openai-whisper
    或更快的:   pip install faster-whisper
    """
    # 检查 Whisper 是否可用
    whisper_available = _check_whisper()
    if not whisper_available:
        raise HTTPException(
            status_code=501,
            detail=(
                "Whisper 未安装。请运行: pip install openai-whisper\n"
                "或更快版本: pip install faster-whisper"
            )
        )

    try:
        real_url = extract_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    file_id = uuid.uuid4().hex
    out_dir = DOWNLOAD_DIR / file_id
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = str(out_dir / "audio.mp3")

    # 先下载音频
    ydl_opts = {
        "quiet": True,
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "audio.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
            ),
        },
        "socket_timeout": 60,
    }

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _do_download(real_url, ydl_opts))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"音频提取失败：{e}")

    # 找到音频文件
    audio_files = list(out_dir.iterdir())
    if not audio_files:
        raise HTTPException(status_code=500, detail="音频提取失败")
    audio_file = str(audio_files[0])

    # 调用 Whisper 转录
    try:
        result = await loop.run_in_executor(
            None, lambda: _whisper_transcribe(audio_file, req.language)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"转录失败：{e}")
    finally:
        _cleanup(out_dir)

    return {
        "text": result["text"],
        "language": result.get("language", "unknown"),
        "segments": [
            {
                "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "text": seg["text"].strip(),
            }
            for seg in result.get("segments", [])
        ],
    }


def _check_whisper() -> bool:
    try:
        import whisper  # noqa
        return True
    except ImportError:
        try:
            from faster_whisper import WhisperModel  # noqa
            return True
        except ImportError:
            return False


def _whisper_transcribe(audio_path: str, language: Optional[str]) -> dict:
    """优先使用 faster-whisper，回退到 openai-whisper"""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe(audio_path, language=language)
        seg_list = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
        full_text = " ".join(s["text"] for s in seg_list)
        return {"text": full_text, "language": info.language, "segments": seg_list}
    except ImportError:
        pass

    import whisper
    model = whisper.load_model("base")
    kwargs = {"language": language} if language else {}
    return model.transcribe(audio_path, **kwargs)


# ─── 4. 健康检查 ──────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    whisper_ok = _check_whisper()
    return {
        "status": "ok",
        "yt_dlp_version": yt_dlp.version.__version__,
        "whisper_available": whisper_ok,
        "download_dir": str(DOWNLOAD_DIR),
    }


# ─── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
