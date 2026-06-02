from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import yt_dlp
import re
import os
import json
import traceback
from dotenv import load_dotenv
from openai import OpenAI
from datetime import datetime
import threading
import uuid
import shutil
import subprocess

load_dotenv(override=True)

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')  # optional proxy / compatible API
OBSIDIAN_VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
OBSIDIAN_SUBFOLDER = os.getenv('OBSIDIAN_SUBFOLDER', '视频笔记')
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'history.json')

def get_bilibili_cookie():
    load_dotenv(override=True)
    return os.getenv('BILIBILI_COOKIE', '')

def get_openai_client(custom_api_key: str = None, custom_base_url: str = None):
    load_dotenv(override=True)
    api_key = custom_api_key or os.getenv('OPENAI_API_KEY')
    base_url = custom_base_url or os.getenv('OPENAI_BASE_URL')
    if not api_key:
        return None
    client_kwargs = {'api_key': api_key}
    if base_url:
        client_kwargs['base_url'] = base_url
    return OpenAI(**client_kwargs)


def get_transcription_client(custom_api_key: str = None, custom_base_url: str = None):
    load_dotenv(override=True)
    api_key = custom_api_key or os.getenv('OPENAI_TRANSCRIPTION_API_KEY') or os.getenv('OPENAI_API_KEY')
    base_url = custom_base_url or os.getenv('OPENAI_TRANSCRIPTION_BASE_URL') or os.getenv('OPENAI_BASE_URL')
    if not api_key:
        return None
    if not custom_api_key and os.getenv('OPENAI_BASE_URL') and not base_url and not os.getenv('OPENAI_TRANSCRIPTION_API_KEY'):
        return None

    client_kwargs = {'api_key': api_key}
    if base_url:
        client_kwargs['base_url'] = base_url
    return OpenAI(**client_kwargs)


def get_transcription_model():
    load_dotenv(override=True)
    return os.getenv('OPENAI_TRANSCRIPTION_MODEL', 'whisper-1')


def transcription_config_error():
    base_url = os.getenv('OPENAI_BASE_URL') or 'OpenAI 官方默认接口'
    return (
        f"语音转文字未配置或当前接口不支持。当前 OPENAI_BASE_URL={base_url}；"
        "文本总结接口可以用 DeepSeek，但语音转文字需要 OpenAI 官方或支持 /audio/transcriptions 的兼容服务。"
        "请在 .env 中设置 OPENAI_TRANSCRIPTION_API_KEY，并按需设置 OPENAI_TRANSCRIPTION_BASE_URL / OPENAI_TRANSCRIPTION_MODEL。"
    )

# ---------------------------------------------------------------------------
# Helpers – video ID and page extraction
# ---------------------------------------------------------------------------

def detect_platform(url: str):
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'youtube'
    if 'bilibili.com' in url or 'b23.tv' in url:
        return 'bilibili'
    return None


def extract_video_id(url: str, platform: str):
    if platform == 'youtube':
        for pattern in [r'(?:v=|\/shorts\/|\/embed\/|youtu\.be\/)([0-9A-Za-z_-]{11})']:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
    elif platform == 'bilibili':
        m = re.search(r'(BV[0-9A-Za-z]+)', url)
        if m:
            return m.group(1)
    return None


def extract_page_num(url: str):
    m = re.search(r'[?&]p=(\d+)', url)
    if m:
        return int(m.group(1))
    return 1

# ---------------------------------------------------------------------------
# Video metadata (Bilibili API / yt-dlp fallback)
# ---------------------------------------------------------------------------

def fetch_bilibili_meta_api(bvid: str, page_num: int = 1):
    try:
        import requests as _req
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/"
        }
        bili_cookie = get_bilibili_cookie()
        if bili_cookie:
            headers["Cookie"] = bili_cookie
        resp = _req.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") == 0:
                data = res_data.get("data", {})
                title = data.get("title", "")
                uploader = data.get("owner", {}).get("name", "")
                thumbnail = data.get("pic", "")
                description = data.get("desc", "")[:500]
                
                pages = data.get("pages", [])
                duration = data.get("duration", 0)
                page_title = ""
                cid = None
                
                if pages:
                    target_page = None
                    for pg in pages:
                        if pg.get("page") == page_num:
                            target_page = pg
                            break
                    if not target_page:
                        target_page = pages[0]
                        page_num = target_page.get("page", 1)
                    
                    cid = target_page.get("cid")
                    page_title = target_page.get("part", "")
                    duration = target_page.get("duration", 0)
                    
                full_title = title
                if page_title and len(pages) > 1:
                    full_title = f"{title} (P{page_num} - {page_title})"
                
                return {
                    'title': full_title,
                    'duration': duration,
                    'thumbnail': thumbnail,
                    'uploader': uploader,
                    'description': description,
                    'cid': cid,
                    'pages': [{'page': p.get('page'), 'part': p.get('part'), 'cid': p.get('cid')} for p in pages]
                }
    except Exception as e:
        print(f"[Bilibili Meta API] Error: {e}")
    return None


def fetch_video_meta(url: str, platform: str | None = None):
    """Return dict with title, duration, thumbnail, uploader."""
    if not platform:
        platform = detect_platform(url)
        
    if platform == 'bilibili':
        bvid = extract_video_id(url, 'bilibili')
        page_num = extract_page_num(url)
        if bvid:
            meta = fetch_bilibili_meta_api(bvid, page_num)
            if meta:
                return meta
                
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                'title': info.get('title', ''),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', ''),
                'description': (info.get('description') or '')[:500],
                'pages': [],
                'cid': None
            }
    except Exception:
        return {'title': '', 'duration': 0, 'thumbnail': '', 'uploader': '', 'description': '', 'pages': [], 'cid': None}

# ---------------------------------------------------------------------------
# Subtitle extraction
# ---------------------------------------------------------------------------

def get_youtube_subtitles(video_id: str):
    """Use youtube-transcript-api with broad language fallback."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi()
        # Try preferred languages first
        preferred = ['zh-Hans', 'zh-Hant', 'zh', 'zh-CN', 'zh-TW', 'en', 'en-US', 'ja', 'ko']
        transcript = ytt_api.fetch(video_id, languages=preferred)
        return ' '.join([entry.text for entry in transcript])
    except Exception as e1:
        # Fallback: use yt-dlp to get subtitles
        try:
            return _ytdlp_subtitles(f'https://www.youtube.com/watch?v={video_id}')
        except Exception as e2:
            print(f'[YouTube] transcript-api error: {e1}')
            print(f'[YouTube] yt-dlp fallback error: {e2}')
            return None


def get_bilibili_subtitles_api(bvid: str, cid: int):
    try:
        import requests as _req
        api_url = f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/"
        }
        bili_cookie = get_bilibili_cookie()
        if bili_cookie:
            headers["Cookie"] = bili_cookie
        resp = _req.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") == 0:
                sub_list = res_data.get("data", {}).get("subtitle", {}).get("subtitles", [])
                if not sub_list:
                    sub_list = res_data.get("data", {}).get("subtitle", {}).get("list", [])
                
                if sub_list:
                    cid_text = str(cid)
                    sub_list = [
                        sub for sub in sub_list
                        if cid_text in (sub.get("subtitle_url") or "")
                    ]
                    if not sub_list:
                        print(f"[Bilibili Subtitle API] No subtitle URL matched cid={cid}")
                        return None

                    target_sub = None
                    for sub in sub_list:
                        lan = sub.get("lan", "")
                        if "zh" in lan:
                            target_sub = sub
                            break
                    if not target_sub:
                        target_sub = sub_list[0]
                    
                    sub_url = target_sub.get("subtitle_url")
                    if sub_url:
                        if sub_url.startswith("//"):
                            sub_url = "https:" + sub_url
                        sub_resp = _req.get(sub_url, headers=headers, timeout=10)
                        if sub_resp.status_code == 200:
                            sub_data = sub_resp.json()
                            texts = []
                            for item in sub_data.get("body", []):
                                content = item.get("content", "").strip()
                                if content:
                                    texts.append(content)
                            if texts:
                                return " ".join(texts)
    except Exception as e:
        print(f"[Bilibili Subtitle API] Error: {e}")
    return None


def get_bilibili_subtitles(video_id: str, page_num: int = 1):
    """Extract Bilibili CC subtitles via yt-dlp."""
    try:
        url = f'https://www.bilibili.com/video/{video_id}'
        if page_num > 1:
            url += f'?p={page_num}'
        return _ytdlp_subtitles(url)
    except Exception as e:
        print(f'[Bilibili] subtitle error: {e}')
        traceback.print_exc()
        return None


def _ytdlp_subtitles(url: str):
    """Generic subtitle extraction via yt-dlp (works for both platforms)."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com/',
    }
    bili_cookie = get_bilibili_cookie()
    if bili_cookie:
        headers['Cookie'] = bili_cookie
        
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['zh-Hans', 'zh-Hant', 'zh', 'en', 'ja', 'ko'],
        'subtitlesformat': 'json3/srv3/vtt/best',
        'http_headers': headers
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        subs = info.get('subtitles', {})
        auto_subs = info.get('automatic_captions', {})
        all_subs = {**auto_subs, **subs}
        for lang in ['zh-Hans', 'zh-Hant', 'zh', 'en', 'ja', 'ko']:
            if lang in all_subs:
                entries = all_subs[lang]
                json3 = [e for e in entries if e.get('ext') == 'json3']
                if json3:
                    import requests as _req
                    resp = _req.get(json3[0]['url'], timeout=30)
                    data = resp.json()
                    texts = []
                    for ev in data.get('events', []):
                        segs = ev.get('segs', [])
                        line = ''.join(s.get('utf8', '') for s in segs).strip()
                        if line and line != '\n':
                            texts.append(line)
                    if texts:
                        return ' '.join(texts)
                vtt = [e for e in entries if e.get('ext') in ('vtt', 'srv3')]
                if vtt:
                    import requests as _req
                    resp = _req.get(vtt[0]['url'], timeout=30)
                    resp.encoding = 'utf-8'
                    lines = resp.text.split('\n')
                    text_lines = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.isdigit() and '-->' not in line and not line.startswith('WEBVTT') and not line.startswith('Kind:') and not line.startswith('Language:'):
                            clean = re.sub(r'<[^>]+>', '', line)
                            if clean:
                                text_lines.append(clean)
                    if text_lines:
                        deduped = [text_lines[0]]
                        for t in text_lines[1:]:
                            if t != deduped[-1]:
                                deduped.append(t)
                        return ' '.join(deduped)
    return None


def _title_relevance_terms(meta: dict | None):
    if not meta:
        return set()

    title = meta.get('title', '')
    terms = set()
    generic_terms = {
        '视频', '合集', '完整', '高清', '字幕', '教程', '课程', '第一', '第二',
        '第三', '第四', '第五', '第六', '第七', '第八', '第九', '第十',
    }

    for seq in re.findall(r'[\u4e00-\u9fff]{2,}', title):
        for n in (4, 3, 2):
            if len(seq) < n:
                continue
            for i in range(len(seq) - n + 1):
                term = seq[i:i + n]
                if term not in generic_terms:
                    terms.add(term)

    for token in re.findall(r'[A-Za-z][A-Za-z0-9_-]{3,}', title):
        terms.add(token.lower())

    return terms


def detect_suspicious_bilibili_subtitle(text: str, meta: dict | None):
    if not text:
        return ''

    stripped = text.strip()
    duration = meta.get('duration', 0) if meta else 0
    if duration > 120 and len(stripped) < 100:
        return "提取的字幕字符数与视频时长严重不符"
    if duration > 300 and len(stripped) < duration * 0.3:
        return "提取的字幕字符数与视频时长严重不符"

    if any(kw in stripped for kw in ["巨神兵", "卧槽什么声音", "三星叛徒"]):
        return "检测到B站防爬重定向诱饵内容"

    terms = _title_relevance_terms(meta)
    if duration > 300 and len(stripped) > 500 and len(terms) >= 5:
        sample = stripped[:6000].lower()
        if not any(term.lower() in sample for term in terms):
            return "提取的字幕与视频标题/分P标题关键词完全不匹配，疑似B站返回了串台内容"

    return ''


def get_bilibili_subtitles_checked(video_id: str, cid: int | None, page_num: int, meta: dict | None, attempts: int = 3):
    suspicious_reason = ''

    if cid:
        for _ in range(attempts):
            text = get_bilibili_subtitles_api(video_id, cid)
            if not text:
                continue

            suspicious_reason = detect_suspicious_bilibili_subtitle(text, meta)
            if not suspicious_reason:
                return text, ''
            print(f"[Bilibili Check] Suspicious API subtitle discarded: {suspicious_reason}")

    text = get_bilibili_subtitles(video_id, page_num)
    if text:
        suspicious_reason = detect_suspicious_bilibili_subtitle(text, meta)
        if not suspicious_reason:
            return text, ''
        print(f"[Bilibili Check] Suspicious yt-dlp subtitle discarded: {suspicious_reason}")

    return None, suspicious_reason

# ---------------------------------------------------------------------------
# Audio downloading, transcoding, and Whisper transcription
# ---------------------------------------------------------------------------

TRANSCRIPTION_TASKS = {} # task_id: { 'status': 'pending', 'progress': '', 'result': '', 'error': '' }

def get_bilibili_audio_url(bvid: str, cid: int):
    try:
        import requests as _req
        url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=16&fnval=16"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/"
        }
        bili_cookie = get_bilibili_cookie()
        if bili_cookie:
            headers["Cookie"] = bili_cookie
        resp = _req.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_data = resp.json()
            if res_data.get("code") == 0:
                dash_data = res_data.get("data", {}).get("dash", {})
                audio_list = dash_data.get("audio", [])
                if audio_list:
                    return audio_list[0].get("baseUrl")
    except Exception as e:
        print(f"[Bilibili Audio URL] Error: {e}")
    return None


def download_audio_stream(url: str, platform: str, video_id: str, page_num: int, cid: int | None, temp_dir: str):
    os.makedirs(temp_dir, exist_ok=True)
    
    # 1. Try Bilibili direct download first
    if platform == 'bilibili' and cid:
        audio_url = get_bilibili_audio_url(video_id, cid)
        if audio_url:
            try:
                import requests as _req
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                bili_cookie = get_bilibili_cookie()
                if bili_cookie:
                    headers["Cookie"] = bili_cookie
                resp = _req.get(audio_url, headers=headers, stream=True, timeout=60)
                if resp.status_code == 200:
                    filepath = os.path.join(temp_dir, "raw_audio.m4s")
                    with open(filepath, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                    return filepath
            except Exception as e:
                print(f"[Direct Audio Download] Failed, falling back to yt-dlp: {e}")
                
    # 2. yt-dlp download (for YouTube or Bilibili fallback)
    download_url = url
    if platform == 'bilibili' and page_num > 1:
        download_url = f"https://www.bilibili.com/video/{video_id}?p={page_num}"
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com/',
    }
    bili_cookie = get_bilibili_cookie()
    if bili_cookie:
        headers['Cookie'] = bili_cookie
        
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(temp_dir, 'raw_audio.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'http_headers': headers
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(download_url, download=True)
        ext = info.get('ext', 'm4a')
        return os.path.join(temp_dir, f"raw_audio.{ext}")


def transcode_and_segment(input_path: str, temp_dir: str):
    chunk_pattern = os.path.join(temp_dir, "chunk_%03d.mp3")
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-f", "segment", "-segment_time", "900", # 15 minutes = 900 seconds
        "-ar", "16000", "-ac", "1", "-b:a", "32k",
        chunk_pattern
    ]
    subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    chunks = []
    for f in os.listdir(temp_dir):
        if f.startswith("chunk_") and f.endswith(".mp3"):
            chunks.append(os.path.join(temp_dir, f))
    chunks.sort()
    return chunks


def run_transcription_async(task_id, url, platform, video_id, page_num, cid, custom_api_key=None, custom_base_url=None):
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp_audio', task_id)
    try:
        TRANSCRIPTION_TASKS[task_id] = {
            'status': 'running',
            'progress': '正在准备下载音频...',
            'result': '',
            'error': ''
        }
        
        # 1. Download
        TRANSCRIPTION_TASKS[task_id]['progress'] = '正在下载视频音频流...'
        audio_path = download_audio_stream(url, platform, video_id, page_num, cid, temp_dir)
        
        # 2. Transcode & segment
        TRANSCRIPTION_TASKS[task_id]['progress'] = '正在转换格式并切片 (ffmpeg)...'
        chunks = transcode_and_segment(audio_path, temp_dir)
        
        if not chunks:
            raise Exception("音频切片失败，未生成有效音频文件。")
            
        # 3. Transcribe chunks
        total_chunks = len(chunks)
        transcripts = []
        ai_client = get_transcription_client(custom_api_key, custom_base_url)
        if not ai_client:
            raise Exception(transcription_config_error())
        transcription_model = get_transcription_model()
        for idx, chunk_file in enumerate(chunks):
            TRANSCRIPTION_TASKS[task_id]['progress'] = f'正在进行语音识别 (第 {idx+1}/{total_chunks} 段)...'
            with open(chunk_file, "rb") as f:
                response = ai_client.audio.transcriptions.create(
                    model=transcription_model,
                    file=f
                )
                text = response.text.strip()
                if text:
                    transcripts.append(text)
                    
        final_text = "\n\n".join(transcripts)
        
        TRANSCRIPTION_TASKS[task_id] = {
            'status': 'completed',
            'progress': '转录完成！',
            'result': final_text,
            'error': ''
        }
        
    except Exception as e:
        traceback.print_exc()
        TRANSCRIPTION_TASKS[task_id] = {
            'status': 'failed',
            'progress': '转录失败',
            'result': '',
            'error': str(e)
        }
    finally:
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception as e:
            print(f"Failed to cleanup temp dir {temp_dir}: {e}")

# ---------------------------------------------------------------------------
# AI Summarization
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = r"""你是一个专业的视频内容分析助手。请对以下视频字幕内容进行深度总结，输出结构化的 Markdown 笔记。

⚠️ 必须遵守的格式与排版规范：
1. **LaTeX 数学公式**：
   - 视频中涉及的所有数学公式、物理方程、数学符号、变量、极限、积分、矩阵等，必须使用标准的 LaTeX 数学公式语法插入。
   - 行内公式（如变量名、简短表达式）必须使用单美元符号包围，例如：$E = mc^2$ 或 $\theta$。
   - 独立行公式（如重要方程、推导步骤）必须使用双美元符号包围，例如：
     $$f(x) = \int_{-\infty}^{\infty} e^{-x^2} dx$$
   - 严禁使用纯文本或普通斜体代替 LaTeX 符号。

2. **来源与章节组织（按照来源链接）**：
   - 总结内容应按视频的具体章节、段落或来源链接（如果字幕中包含时间戳或分段信息）进行层级化组织。
   - 每一部分开头应明确标注其对应的视频来源、小节或时间范围（若有时间戳，可使用类似 `[05:12](来源链接)` 或 `[分段标题](视频链接)` 形式进行关联）。

3. **知识点 + 例题一一对应结构**：
   - 在每个分段/小节的详细笔记中，必须严格采用“**知识点 + 对应例题**”的一一对应结构。
   - 首先阐述一个具体的知识点/定理/概念（注明定义、公式及解释），紧接着给出该知识点对应的例题、解析或应用步骤，然后才是下一个知识点。
   - 每个知识点和对应的例题应有清晰的排版关联，让读者能直观看出它们是成对出现的。

请按照以下结构输出 Markdown 笔记：

## 📌 概述
用 2-3 句话概括视频的核心主题、主要研究对象和结论。如果涉及主要公式，请用 LaTeX 呈现。

## 🔑 核心要点
- 列出 3-8 个关键要点，每个要点一句话。

## 📝 详细笔记与例题解析
按照视频的章节、分段或时间顺序组织。对每个小节：
### [小节标题/来源链接/时间戳]
- **知识点 1**：[知识点名称/概念阐述，包含 LaTeX 公式定义]
  - **对应例题 1**：[视频中与此知识点对应的具体例题描述、推导或解析步骤，使用 LaTeX 公式]
- **知识点 2**：[知识点名称/概念阐述...]
  - **对应例题 2**：[对应例题或实际应用案例...]

## 💡 关键引用 / 金句
如果字幕中有值得记录的原话或金句，列出 2-5 条。

## 🏷️ 建议标签
列出 3-5 个适合作为 Obsidian 标签的关键词（用 #tag 格式）。

请使用中文输出（除非字幕是纯英文内容则用英文）。保持专业、简洁、有条理。"""


def summarize_text(text: str, model: str | None = None, max_chars: int = 15000,
                   custom_api_key: str = None, custom_base_url: str = None):
    ai_client = get_openai_client(custom_api_key, custom_base_url)
    if not ai_client:
        return '❌ 未配置 OpenAI API Key，请在 .env 中设置 OPENAI_API_KEY 或在前端页面中输入'

    use_model = model
    if not use_model:
        return '❌ 错误：未指定模型。请在前端选择模型。'

    if len(text) > max_chars:
        return _chunked_summarize(text, use_model, max_chars, custom_api_key, custom_base_url)

    try:
        response = ai_client.chat.completions.create(
            model=use_model,
            messages=[
                {'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
                {'role': 'user', 'content': text},
            ],
            temperature=0.4,
            max_tokens=3000,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f'❌ AI 总结出错: {str(e)}'


def _chunked_summarize(text: str, model: str, chunk_size: int = 12000,
                       custom_api_key: str = None, custom_base_url: str = None):
    """Split long text into chunks, summarize each, then merge."""
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i:i + chunk_size])

    ai_client = get_openai_client(custom_api_key, custom_base_url)
    if not ai_client:
        return '❌ 未配置 OpenAI API Key'

    partial_summaries = []
    for idx, chunk in enumerate(chunks):
        try:
            resp = ai_client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': f'你是一个专业的视频字幕总结助手。这是第 {idx+1}/{len(chunks)} 段字幕。请提取该段的核心知识点与例题，所有公式/符号必须使用 LaTeX 格式（行内用 $...$，独立行用 $$...$$），按照“知识点+对应例题”的结构用简洁的中文列出。'},
                    {'role': 'user', 'content': chunk},
                ],
                temperature=0.4,
                max_tokens=1500,
            )
            partial_summaries.append(resp.choices[0].message.content)
        except Exception as e:
            partial_summaries.append(f'(第{idx+1}段总结失败: {e})')

    merged_input = '\n\n---\n\n'.join(partial_summaries)
    try:
        final = ai_client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
                {'role': 'user', 'content': f'以下是一个长视频字幕的分段摘要，请合并整理为一份完整的结构化笔记：\n\n{merged_input}'},
            ],
            temperature=0.4,
            max_tokens=3000,
        )
        return final.choices[0].message.content
    except Exception as e:
        return f'❌ 合并总结出错: {str(e)}\n\n分段摘要:\n{merged_input}'

# ---------------------------------------------------------------------------
# Obsidian save
# ---------------------------------------------------------------------------

def save_to_obsidian(title: str, summary: str, url: str, platform: str,
                     meta: dict | None = None, include_subtitles: bool = False,
                     subtitles: str = '', output_dir: str | None = None,
                     subfolder: str | None = None, filename_tpl: str | None = None,
                     video_id: str = ''):
    vault_path = output_dir or OBSIDIAN_VAULT_PATH
    if not vault_path:
        return None, '未设置 Obsidian 库路径，请在 .env 中设置 OBSIDIAN_VAULT_PATH 或在页面中指定'

    folder_name = subfolder if subfolder is not None else OBSIDIAN_SUBFOLDER
    if folder_name:
        save_dir = os.path.join(vault_path, folder_name)
    else:
        save_dir = vault_path
    os.makedirs(save_dir, exist_ok=True)

    safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:80]
    now = datetime.now()
    date_str = now.strftime('%Y%m%d')
    time_str = now.strftime('%H%M%S')
    datetime_str = now.strftime('%Y%m%d_%H%M%S')

    tpl = filename_tpl or '{datetime}_{platform}_{title}'
    try:
        filename = tpl.format(
            date=date_str,
            time=time_str,
            datetime=datetime_str,
            platform=platform,
            title=safe_title,
            id=video_id or platform,
        )
    except (KeyError, ValueError):
        filename = f'{datetime_str}_{platform}_{safe_title}'
    filename = re.sub(r'[\\/*?:"<>|]', '_', filename)
    if not filename.endswith('.md'):
        filename += '.md'
    filepath = os.path.join(save_dir, filename)

    tags_line = f'  - {platform}\n  - 视频笔记'
    duration_str = ''
    uploader_str = ''
    if meta:
        if meta.get('duration'):
            mins = meta['duration'] // 60
            secs = meta['duration'] % 60
            duration_str = f'\nduration: "{mins}分{secs}秒"'
        if meta.get('uploader'):
            uploader_str = f'\nuploader: "{meta["uploader"]}"'

    content = f"""---
title: "{title}"
source: "{url}"
platform: {platform}
date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{duration_str}{uploader_str}
tags:
{tags_line}
---

# {title}

> **原始链接:** {url}
> **平台:** {platform.upper()}
> **生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

{summary}
"""
    if include_subtitles and subtitles:
        content += f"""
---

## 📜 原始字幕

<details>
<summary>点击展开完整字幕</summary>

{subtitles}

</details>
"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    return filepath, None

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history_entry(entry: dict):
    history = load_history()
    history.insert(0, entry)
    history = history[:50]
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/extract', methods=['POST'])
def extract():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': '请输入视频链接'}), 400

    platform = detect_platform(url)
    if not platform:
        return jsonify({'error': '不支持的平台，目前仅支持 YouTube 和 Bilibili'}), 400

    video_id = extract_video_id(url, platform)
    if not video_id:
        return jsonify({'error': '无法解析视频 ID，请检查链接格式'}), 400

    meta = fetch_video_meta(url, platform)
    cid = meta.get('cid')
    page_num = extract_page_num(url)

    text = None
    suspicious_reason = ""
    if platform == 'bilibili':
        text, suspicious_reason = get_bilibili_subtitles_checked(video_id, cid, page_num, meta)
    else:
        text = get_youtube_subtitles(video_id)

    has_subtitles = text is not None and len(text.strip()) > 0

    return jsonify({
        'text': text or '',
        'has_subtitles': has_subtitles,
        'platform': platform,
        'video_id': video_id,
        'page_num': page_num,
        'meta': meta,
        'char_count': len(text) if text else 0,
        'is_suspicious': bool(suspicious_reason),
        'suspicious_reason': suspicious_reason,
    })


@app.route('/transcribe', methods=['POST'])
def transcribe():
    data = request.json
    custom_api_key = data.get('api_key')
    custom_base_url = data.get('base_url')
    ai_client = get_transcription_client(custom_api_key, custom_base_url)
    if not ai_client:
        return jsonify({'error': transcription_config_error()}), 400
        
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': '请输入视频链接'}), 400
        
    platform = detect_platform(url)
    if not platform:
        return jsonify({'error': '不支持的平台'}), 400
        
    video_id = extract_video_id(url, platform)
    if not video_id:
        return jsonify({'error': '无法解析视频 ID'}), 400
        
    page_num = extract_page_num(url)
    cid = data.get('cid')
    
    task_id = str(uuid.uuid4())
    
    t = threading.Thread(target=run_transcription_async, args=(task_id, url, platform, video_id, page_num, cid, custom_api_key, custom_base_url))
    t.daemon = True
    t.start()
    
    return jsonify({'task_id': task_id})


@app.route('/transcribe/status/<task_id>', methods=['GET'])
def transcribe_status(task_id):
    task = TRANSCRIPTION_TASKS.get(task_id)
    if not task:
        return jsonify({'status': 'not_found', 'error': '未找到此转录任务'}), 404
    return jsonify(task)


@app.route('/summarize', methods=['POST'])
def summarize():
    data = request.json
    text = data.get('text', '')
    model = data.get('model')
    custom_api_key = data.get('api_key')
    custom_base_url = data.get('base_url')
    if not text:
        return jsonify({'error': '无字幕文本'}), 400
    summary = summarize_text(text, model=model, custom_api_key=custom_api_key, custom_base_url=custom_base_url)
    return jsonify({'summary': summary})


@app.route('/test_connection', methods=['POST'])
def test_connection():
    data = request.json
    custom_api_key = data.get('api_key')
    custom_base_url = data.get('base_url')
    model = data.get('model')
    if not model:
        return jsonify({'success': False, 'error': '未指定测试模型。请在前端选择具体的模型。'}), 400
    
    ai_client = get_openai_client(custom_api_key, custom_base_url)
    if not ai_client:
        return jsonify({'success': False, 'error': 'API Key 未设置，无法进行测试。请检查环境变量或页面配置。'}), 400
        
    try:
        response = ai_client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'user', 'content': 'Ping'},
            ],
            temperature=0.1,
            max_tokens=10,
        )
        content = response.choices[0].message.content.strip()
        return jsonify({
            'success': True,
            'message': '连接成功！',
            'response': content
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 200


@app.route('/save', methods=['POST'])
def save():
    data = request.json
    title = data.get('title', '视频总结')
    summary = data.get('summary', '')
    url = data.get('url', '')
    platform = data.get('platform', '')
    meta = data.get('meta')
    output_dir = data.get('output_dir') or None
    subfolder = data.get('subfolder')
    include_subtitles = data.get('include_subtitles', False)
    subtitles = data.get('subtitles', '')

    if not summary:
        return jsonify({'error': '无总结内容'}), 400

    filepath, err = save_to_obsidian(title, summary, url, platform, meta,
                                      include_subtitles, subtitles, output_dir,
                                      subfolder,
                                      data.get('filename_tpl'),
                                      data.get('video_id', ''))
    if err:
        return jsonify({'error': err}), 400

    save_history_entry({
        'title': title,
        'url': url,
        'platform': platform,
        'filepath': filepath,
        'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })

    return jsonify({'filepath': filepath})


@app.route('/auto', methods=['POST'])
def auto():
    """One-click: extract → summarize → save."""
    data = request.json
    url = data.get('url', '').strip()
    model = data.get('model')
    output_dir = data.get('output_dir') or None
    subfolder = data.get('subfolder')
    include_subtitles = data.get('include_subtitles', False)

    custom_api_key = data.get('api_key')
    custom_base_url = data.get('base_url')

    if not url:
        return jsonify({'error': '请输入视频链接'}), 400

    platform = detect_platform(url)
    if not platform:
        return jsonify({'error': '不支持的平台'}), 400

    video_id = extract_video_id(url, platform)
    if not video_id:
        return jsonify({'error': '无法解析视频 ID'}), 400

    page_num = extract_page_num(url)
    meta = fetch_video_meta(url, platform)
    cid = meta.get('cid')

    text = None
    if platform == 'bilibili':
        text, suspicious_reason = get_bilibili_subtitles_checked(video_id, cid, page_num, meta)
    else:
        text = get_youtube_subtitles(video_id)
        
    if not text:
        ai_client = get_transcription_client(custom_api_key, custom_base_url)
        if ai_client:
            transcription_model = get_transcription_model()
            temp_id = f"auto_{uuid.uuid4()}"
            temp_dir = os.path.join(os.path.dirname(__file__), 'temp_audio', temp_id)
            try:
                audio_path = download_audio_stream(url, platform, video_id, page_num, cid, temp_dir)
                chunks = transcode_and_segment(audio_path, temp_dir)
                transcripts = []
                for chunk_file in chunks:
                    with open(chunk_file, "rb") as f:
                        response = ai_client.audio.transcriptions.create(
                            model=transcription_model,
                            file=f
                        )
                        t_text = response.text.strip()
                        if t_text:
                            transcripts.append(t_text)
                text = "\n\n".join(transcripts)
            except Exception as e:
                return jsonify({'error': f'无字幕且语音转录失败: {str(e)}'}), 500
            finally:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
        else:
            return jsonify({'error': f'未找到可用字幕，且{transcription_config_error()}'}), 400

    summary = summarize_text(text, model=model, custom_api_key=custom_api_key, custom_base_url=custom_base_url)

    title = meta.get('title') or f'{platform}_{video_id}'
    filepath, err = save_to_obsidian(title, summary, url, platform, meta,
                                      include_subtitles, text, output_dir,
                                      subfolder,
                                      data.get('filename_tpl'),
                                      video_id)

    save_history_entry({
        'title': title,
        'url': url,
        'platform': platform,
        'filepath': filepath,
        'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })

    return jsonify({
        'text': text,
        'summary': summary,
        'filepath': filepath,
        'meta': meta,
        'save_error': err,
    })


def update_env_variable(key: str, value: str):
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write("")
            
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        lines = []
        
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            replaced = True
            break
            
    if not replaced:
        lines.append(f"{key}={value}\n")
        
    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


@app.route('/bilibili/qrcode', methods=['GET'])
def bilibili_qrcode():
    try:
        import requests as _req
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = _req.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({'error': '无法获取二维码'}), resp.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/bilibili/qrcode/poll', methods=['GET'])
def bilibili_qrcode_poll():
    qrcode_key = request.args.get('qrcode_key')
    if not qrcode_key:
        return jsonify({'error': '缺少 qrcode_key 参数'}), 400
        
    try:
        import requests as _req
        url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/"
        }
        resp = _req.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_data = resp.json()
            code = res_data.get('code')
            if code == 0:
                cookies_dict = resp.cookies.get_dict()
                if cookies_dict:
                    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
                    update_env_variable("BILIBILI_COOKIE", cookie_str)
                    
                    if 'data' not in res_data or res_data['data'] is None:
                        res_data['data'] = {}
                    res_data['data']['cookies'] = cookie_str
            return jsonify(res_data)
        return jsonify({'error': '请求轮询状态失败'}), resp.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/history', methods=['GET'])
def history():
    return jsonify(load_history())


if __name__ == '__main__':
    app.run(debug=True, port=5000)
