"""把本机视频通过 HTTP 提供给投影仪，并准备全黑画面。"""

from __future__ import annotations

import hashlib
import heapq
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def runtime_dir() -> Path:
    """开发时用源码目录，打包后用 exe 所在目录，这样设置和缓存能留下来。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", runtime_dir()))
    return Path(__file__).resolve().parent


def tool_path(name: str) -> str | None:
    """优先用放在程序旁边的 ffmpeg，换电脑时不用再单独安装。"""
    for candidate in (runtime_dir() / f"{name}.exe", runtime_dir() / name):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


CACHE_DIR = runtime_dir() / "cache"

SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
SUBTITLE_LANGS = (
    "中文",
    "英语",
    "日语",
    "韩语",
    "法语",
    "德语",
    "西班牙语",
    "意大利语",
    "葡萄牙语",
    "俄语",
    "阿拉伯语",
    "丹麦语",
    "荷兰语",
    "芬兰语",
    "挪威语",
    "瑞典语",
    "波兰语",
    "匈牙利语",
)
_LANG_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("中英", ("中英", "中英文", "中英双语", "双语", "chseng", "chieng", "zhen")),
    ("中文", ("中文", "简体", "简中", "繁体", "繁中", "中字", "国语", "汉语", "亚洲", "chinese", "chi", "zho", "zh", "chs", "cht", "sc", "tc")),
    ("英语", ("英语", "英文", "english", "eng", "en")),
    ("日语", ("日语", "日文", "japanese", "jpn", "ja")),
    ("韩语", ("韩语", "韩文", "korean", "kor", "ko")),
    ("法语", ("法语", "法文", "french", "fre", "fra", "fr")),
    ("德语", ("德语", "德文", "german", "ger", "deu", "de")),
    ("西班牙语", ("西班牙语", "西语", "spanish", "spa", "es")),
    ("意大利语", ("意大利语", "意语", "italian", "ita", "it")),
    ("葡萄牙语", ("葡萄牙语", "葡语", "portuguese", "por", "pt")),
    ("俄语", ("俄语", "俄文", "russian", "rus", "ru")),
    ("阿拉伯语", ("阿拉伯语", "arabic", "ara", "ar")),
    ("丹麦语", ("丹麦语", "danish", "dan")),
    ("荷兰语", ("荷兰语", "dutch", "dut", "nld", "nl")),
    ("芬兰语", ("芬兰语", "finnish", "fin")),
    ("挪威语", ("挪威语", "norwegian", "nor", "nob")),
    ("瑞典语", ("瑞典语", "swedish", "swe")),
    ("波兰语", ("波兰语", "polish", "pol")),
    ("匈牙利语", ("匈牙利语", "hungarian", "hun")),
)
_SUB_PLAN: dict[str, tuple[float, tuple[str, str] | None]] = {}

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".vob",
}

MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/avi",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".webm": "video/webm",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".vob": "video/mpeg",
}


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def mime_for(path: str) -> str:
    return MIME.get(Path(path).suffix.lower(), "video/mp4")


def fmt_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def fmt_clock(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def collect_videos(paths: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        full = os.path.normcase(os.path.abspath(path))
        if full in seen:
            return
        if Path(path).suffix.lower() not in VIDEO_EXTS:
            return
        seen.add(full)
        found.append(os.path.abspath(path))

    for raw in paths:
        path = os.path.abspath(raw)
        if os.path.isfile(path):
            add(path)
            continue
        if not os.path.isdir(path):
            continue
        for root, dirs, names in os.walk(path):
            dirs.sort(key=natural_key)
            for name in sorted(names, key=natural_key):
                if name.startswith("."):
                    continue
                add(os.path.join(root, name))
    return found


def _compact(text: str) -> str:
    return re.sub(r"[\s._+\-]+", "", text).casefold()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text.casefold())


def _attached(longer: str, shorter: str) -> bool:
    if not longer.startswith(shorter):
        return False
    if len(longer) == len(shorter):
        return True
    return not longer[len(shorter)].isdigit()


def detect_subtitle_lang(extra: str) -> str | None:
    if not extra:
        return None
    tokens = set(_tokens(extra))
    blob = extra.casefold()
    matched: list[str] = []
    for label, tags in _LANG_TAGS:
        for tag in tags:
            folded = tag.casefold()
            hit = folded in tokens if folded.isascii() and len(folded) <= 3 else folded in tokens or folded in blob
            if hit:
                matched.append(label)
                break
    if "中英" in matched or ("中文" in matched and "英语" in matched):
        return "中英"
    return matched[0] if matched else None


def matching_subtitles(video: str) -> list[tuple[str | None, str]]:
    """同目录里和这部视频对得上的 srt。语言来自文件名，例如 `地球守护者.中文.srt`。"""
    path = Path(video)
    if not path.is_file():
        return []
    stem = _compact(path.stem)
    if not stem:
        return []
    found: list[tuple[str | None, str]] = []
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return []
    for candidate in entries:
        if not candidate.is_file() or candidate.suffix.lower() != ".srt":
            continue
        sub = _compact(candidate.stem)
        if not sub:
            continue
        if sub == stem:
            extra = ""
        elif _attached(sub, stem):
            extra = sub[len(stem) :]
        else:
            continue
        found.append((detect_subtitle_lang(extra), str(candidate)))
    return found


def find_subtitle(video: str, prefer: str = "中文") -> str | None:
    items = matching_subtitles(video)
    if not items:
        return None
    order: list[str] = []
    for lang in ("中英", prefer, "中文", "英语"):
        if not lang or lang in order:
            continue
        if lang != "中英" and lang not in SUBTITLE_LANGS:
            continue
        order.append(lang)
    for lang in order:
        for item_lang, item_path in items:
            if item_lang == lang:
                return item_path
    if len(items) == 1 and items[0][0] is None:
        return items[0][1]
    return None


def subtitle_for(video: str, prefer: str = "中文") -> tuple[str, str] | None:
    path = Path(video)
    try:
        stamp = path.stat().st_mtime + path.parent.stat().st_mtime
    except OSError:
        return None
    key = f"{path}\0{prefer}"
    cached = _SUB_PLAN.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    found = find_subtitle(video, prefer)
    plan: tuple[str, str] | None = ("file", found) if found else None
    _SUB_PLAN[key] = (stamp, plan)
    return plan


def prepare_subtitle(source: str) -> str:
    """转成 UTF-8，避免 GBK 字幕叠出来是乱码。"""
    raw = Path(source).read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    suffix = Path(source).suffix.lower()
    if suffix == ".srt":
        text = re.sub(r"</?font[^>]*>", "", text, flags=re.IGNORECASE)
    if suffix not in SUB_EXTS:
        suffix = ".srt"
    digest = hashlib.sha1(raw).hexdigest()[:16]
    folder = CACHE_DIR / "subs"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{digest}-u2{suffix}"
    if not dest.exists():
        dest.write_text(text, encoding="utf-8")
    return str(dest)


def escape_filter_path(path: str) -> str:
    text = Path(path).resolve().as_posix()
    for char in ("\\", ":", "'", ",", ";", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def font_dir() -> Path | None:
    """只放一款中文字体。把整个 Windows 字体目录交给字幕滤镜时，加载会卡住。"""
    folder = CACHE_DIR / "fonts"
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.glob("*.tt[cf]")) or any(folder.glob("*.otf")):
        return folder
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc"):
        source = windir / name
        if not source.is_file():
            continue
        target = folder / name
        try:
            shutil.copyfile(source, target)
        except OSError:
            continue
        return folder
    return None


def subtitle_filter(video: str, height: str | None, prefer: str = "中文") -> str | None:
    plan = subtitle_for(video, prefer)
    if plan is None:
        return None
    size = {"480": "18", "720": "24", "1080": "32"}.get(height or "", "28")
    target = prepare_subtitle(plan[1])
    body = f"subtitles='{escape_filter_path(target)}'"
    fonts = font_dir()
    if fonts is not None:
        body += f":fontsdir='{escape_filter_path(fonts)}'"
    body += (
        f":force_style='FontName=Microsoft YaHei,FontSize={size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,MarginV=18'"
    )
    return body


def probe_duration(path: str) -> float | None:
    ffprobe = tool_path("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            creationflags=CREATE_NO_WINDOW,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        value = float(out.decode().strip())
    except Exception:
        return None
    return value if value > 0 else None


def ensure_black(cache: Path) -> tuple[Path | None, Path | None]:
    cache.mkdir(parents=True, exist_ok=True)
    bundled = bundle_dir() / "cache"
    if bundled.resolve() != cache.resolve():
        for name in ("black.mp4", "black.jpg"):
            src = bundled / name
            dst = cache / name
            if src.is_file() and not dst.exists():
                shutil.copyfile(src, dst)
    jpg = cache / "black.jpg"
    mp4 = cache / "black.mp4"
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        return (mp4 if mp4.exists() else None), (jpg if jpg.exists() else None)
    if not jpg.exists() or jpg.stat().st_size < 1000:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1280x720",
                "-frames:v",
                "1",
                str(jpg),
            ],
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    if not mp4.exists() or mp4.stat().st_size < 10000:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1280x720:r=8",
                "-t",
                "90",
                "-c:v",
                "libx264",
                "-profile:v",
                "baseline",
                "-level",
                "3.1",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-tune",
                "stillimage",
                "-an",
                "-movflags",
                "+faststart",
                str(mp4),
            ],
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    return (mp4 if mp4.exists() else None), (jpg if jpg.exists() else None)


STREAM_PRESETS = {
    "480": ("480", "1200k", "2400k", "96k"),
    "720": ("720", "2800k", "5600k", "128k"),
    "1080": ("1080", "5000k", "10000k", "160k"),
}


def choose_encoder() -> str:
    """挑一个这台电脑真正能用的 H.264 编码器，优先用显卡。"""
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        return "libx264"
    for name in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"):
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x240:r=25",
                    "-t",
                    "0.2",
                    "-c:v",
                    name,
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return name
    return "libx264"


def _encoder_args(name: str, bitrate: str, bufsize: str) -> list[str]:
    if name == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-bufsize",
            bufsize,
            "-bf",
            "0",
        ]
    if name == "h264_qsv":
        return [
            "-c:v",
            "h264_qsv",
            "-preset",
            "veryfast",
            "-profile:v",
            "main",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-bufsize",
            bufsize,
            "-bf",
            "0",
        ]
    if name == "h264_amf":
        return [
            "-c:v",
            "h264_amf",
            "-quality",
            "speed",
            "-profile:v",
            "main",
            "-rc",
            "vbr_peak",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-bf",
            "0",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-profile:v",
        "main",
        "-level",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        bitrate,
        "-maxrate",
        bitrate,
        "-bufsize",
        bufsize,
        "-bf",
        "0",
    ]


def parse_npt(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    if ":" not in text:
        try:
            return max(0.0, float(text))
        except ValueError:
            return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return max(0.0, int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
        if len(parts) == 2:
            return max(0.0, int(parts[0]) * 60 + float(parts[1]))
    except ValueError:
        return None
    return None


THUMB_STEP = 8


def local_cache_dir() -> Path:
    """缓存在系统盘，外接硬盘拔掉时这里还在。"""
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path.home() / "AppData" / "Local"
    folder = base / "清映" / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def drive_roots() -> list[str]:
    if os.name != "nt":
        return []
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{chr(65 + index)}:\\" for index in range(26) if mask & (1 << index)]


def drive_listed(path: str) -> bool:
    anchor = Path(path).anchor
    if len(anchor) < 2:
        return bool(path) and os.path.exists(path)
    letter = anchor[0].upper()
    if not ("A" <= letter <= "Z"):
        return False
    return f"{letter}:\\" in {item.upper() for item in drive_roots()}


def path_ok(path: str) -> bool:
    if not path or not drive_listed(path):
        return False
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def drive_ready(path: str) -> bool:
    return drive_listed(path)


def locate_file(path: str, size: int = 0) -> str | None:
    """原路径还在就用原路径。盘符变了的话，按同样的文件夹位置再找一遍。"""
    if not path:
        return None
    candidates = [path]
    anchor = Path(path).anchor
    tail = path[len(anchor):] if anchor else ""
    if tail:
        for root in drive_roots():
            if anchor and root.upper() == anchor.upper():
                continue
            candidates.append(root + tail)
    for candidate in candidates:
        if not drive_listed(candidate):
            continue
        try:
            if not os.path.isfile(candidate):
                continue
            found_size = os.path.getsize(candidate)
        except OSError:
            continue
        if size > 0 and found_size != size:
            continue
        if found_size <= 0:
            continue
        return candidate
    return None


def _file_key(name: str, size: int) -> str:
    return hashlib.sha1(f"{name}|{size}".encode("utf-8", "replace")).hexdigest()[:20]


class FileMirror:
    """把正在播的视频复制到系统盘。复制完成前硬盘断了就等它回来，复制完就能直接接着播。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.threads: dict[str, threading.Thread] = {}
        self.cancel: set[str] = set()

    def key(self, path: str, size: int) -> str:
        return _file_key(Path(path).name, size)

    def dest(self, path: str, size: int) -> Path:
        suffix = Path(path).suffix.lower()
        if suffix not in VIDEO_EXTS:
            suffix = ".bin"
        return local_cache_dir() / "mirror" / f"{self.key(path, size)}{suffix}"

    def ready(self, path: str, size: int = 0) -> bool:
        if size <= 0:
            try:
                size = os.path.getsize(path)
            except OSError:
                return False
        dest = self.dest(path, size)
        try:
            return dest.is_file() and dest.stat().st_size == size
        except OSError:
            return False

    def start(self, path: str, size: int = 0) -> str:
        if size <= 0:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        if size <= 0:
            return "missing"
        located = locate_file(path, size)
        if located:
            path = located
        elif not path_ok(path):
            return "missing"
        cache_root = local_cache_dir()
        if Path(path).anchor.lower() == cache_root.anchor.lower():
            return "local"
        if self.ready(path, size):
            return "ready"
        key = self.key(path, size)
        dest = self.dest(path, size)
        with self.lock:
            self.cancel.discard(key)
            running = self.threads.get(key)
            if running is not None and running.is_alive():
                return "running"
            for other in list(self.threads):
                if other != key:
                    self.cancel.add(other)
            self._make_room(dest, size)
            free = shutil.disk_usage(cache_root).free
            partial = dest.stat().st_size if dest.exists() else 0
            if free + partial < size + 1024 ** 3:
                return "space"
            thread = threading.Thread(
                target=self._copy,
                args=(key, path, size, dest),
                name="qingying-mirror",
                daemon=True,
            )
            self.threads[key] = thread
            thread.start()
            return "started"

    def _make_room(self, keep: Path, size: int) -> None:
        folder = keep.parent
        folder.mkdir(parents=True, exist_ok=True)
        need = size + 1024 ** 3
        others = []
        for item in folder.iterdir():
            if not item.is_file():
                continue
            try:
                if item.resolve() == keep.resolve():
                    continue
            except OSError:
                continue
            try:
                others.append((item.stat().st_mtime, item))
            except OSError:
                continue
        others.sort()
        for _mtime, item in others:
            if shutil.disk_usage(folder).free >= need:
                break
            try:
                item.unlink()
            except OSError:
                continue

    def _copy(self, key: str, path: str, size: int, dest: Path) -> None:
        time.sleep(3)
        dest.parent.mkdir(parents=True, exist_ok=True)
        while key not in self.cancel:
            if dest.exists():
                try:
                    if dest.stat().st_size == size:
                        return
                except OSError:
                    pass
            source = locate_file(path, size)
            if not source:
                time.sleep(1)
                continue
            try:
                have = dest.stat().st_size if dest.exists() else 0
                if have > size:
                    dest.unlink(missing_ok=True)
                    have = 0
                with open(source, "rb") as src, open(dest, "r+b" if have else "wb") as out:
                    src.seek(have)
                    if have:
                        out.seek(have)
                    pending = 0
                    while have < size and key not in self.cancel:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        have += len(chunk)
                        pending += len(chunk)
                        if pending >= 2 * 1024 * 1024:
                            out.flush()
                            pending = 0
                            time.sleep(0.12)
            except OSError:
                time.sleep(1)
                continue
            try:
                if dest.exists() and dest.stat().st_size == size:
                    return
            except OSError:
                pass
            time.sleep(1)


mirror = FileMirror()


def resolve_playable(path: str, size: int = 0) -> str:
    if not path:
        return path
    if size <= 0:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    if size > 0 and mirror.ready(path, size):
        return str(mirror.dest(path, size))
    located = locate_file(path, size)
    return located or path


def thumb_key(name: str, size: int) -> str:
    return _file_key(name, size)


def thumb_dir(path: str, size: int) -> Path:
    return local_cache_dir() / "thumbs" / thumb_key(Path(path).name, size)


class ThumbHub:
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.heap: list[tuple[int, int, str, int, int]] = []
        self.seq = 0
        self.ready: dict[tuple[str, int], str] = {}
        self.pending: set[tuple[str, int]] = set()
        self.fails: dict[tuple[str, int], int] = {}
        self.on_ready = None
        threading.Thread(target=self._loop, name="qingying-thumbs", daemon=True).start()

    def schedule(self, path: str, duration: float, size: int = 0) -> None:
        if size <= 0:
            try:
                size = os.path.getsize(path)
            except OSError:
                return
        if size <= 0 or duration <= 1:
            return
        self._prime(path, size)
        second = 0
        while second < duration:
            self._enqueue(path, second, size, 5)
            second += THUMB_STEP

    def request(self, path: str, size: int, second: float) -> tuple[int, str] | None:
        if size <= 0:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        if size <= 0:
            return None
        slot = int(max(0.0, second) // THUMB_STEP) * THUMB_STEP
        self._prime(path, size)
        self._enqueue(path, slot, size, 0, force=True)
        self._enqueue(path, max(0, slot - THUMB_STEP), size, 2)
        self._enqueue(path, slot + THUMB_STEP, size, 2)
        return self.nearest(path, size, second)

    def nearest(self, path: str, size: int, second: float) -> tuple[int, str] | None:
        key = thumb_key(Path(path).name, size)
        target = int(max(0.0, second))
        best: tuple[int, int, str] | None = None
        with self.cv:
            for (item_key, item_second), jpg in self.ready.items():
                if item_key != key:
                    continue
                dist = abs(item_second - target)
                if best is None or dist < best[0]:
                    best = (dist, item_second, jpg)
        if best is None or best[0] > THUMB_STEP:
            return None
        return best[1], best[2]

    def _prime(self, path: str, size: int) -> None:
        folder = thumb_dir(path, size)
        if not folder.is_dir():
            return
        key = thumb_key(Path(path).name, size)
        with self.cv:
            for image in folder.glob("*.jpg"):
                try:
                    second = int(image.stem)
                except ValueError:
                    continue
                self.ready[(key, second)] = str(image)

    def _enqueue(self, path: str, second: int, size: int, priority: int, force: bool = False) -> None:
        second = max(0, int(second))
        key = thumb_key(Path(path).name, size)
        with self.cv:
            slot = (key, second)
            if slot in self.ready:
                return
            if slot in self.pending and not force:
                return
            self.pending.add(slot)
            self.seq += 1
            heapq.heappush(self.heap, (priority, self.seq, path, second, size))
            self.cv.notify()

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.heap:
                    self.cv.wait()
                priority, _seq, path, second, size = heapq.heappop(self.heap)
                key = thumb_key(Path(path).name, size)
                self.pending.discard((key, second))
                if (key, second) in self.ready:
                    continue
            dest = thumb_dir(path, size) / f"{second:07d}.jpg"
            if dest.is_file() and dest.stat().st_size > 0:
                self._store(key, second, dest)
                continue
            source = resolve_playable(path, size)
            if not path_ok(source):
                self._retry(path, second, size, key)
                continue
            if self._extract(source, second, dest):
                self._store(key, second, dest)
                if priority > 0:
                    time.sleep(0.8)
            else:
                self._retry(path, second, size, key)

    def _store(self, key: str, second: int, dest: Path) -> None:
        with self.cv:
            self.ready[(key, second)] = str(dest)
            self.fails.pop((key, second), None)
        callback = self.on_ready
        if callback:
            callback(key, second, str(dest))

    def _retry(self, path: str, second: int, size: int, key: str) -> None:
        with self.cv:
            count = self.fails.get((key, second), 0) + 1
            self.fails[(key, second)] = count
            if count > 4:
                return
            self.pending.add((key, second))
            self.seq += 1
            heapq.heappush(self.heap, (6, self.seq, path, second, size))
        time.sleep(0.8)

    def _extract(self, source: str, second: int, dest: Path) -> bool:
        ffmpeg = tool_path("ffmpeg")
        if not ffmpeg:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(".part.jpg")
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(second),
                "-i",
                source,
                "-frames:v",
                "1",
                "-vf",
                "scale=192:-2",
                "-q:v",
                "7",
                str(temp),
            ],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not temp.is_file() or temp.stat().st_size <= 0:
            temp.unlink(missing_ok=True)
            return False
        temp.replace(dest)
        return True


thumbs = ThumbHub()


def stream_command(
    source: str,
    quality: str,
    encoder: str,
    subtitle_lang: str = "中文",
    start: float = 0.0,
    speed: float = 1.0,
    media_input: str | None = None,
) -> list[str] | None:
    ffmpeg = tool_path("ffmpeg")
    if not source or not ffmpeg:
        return None
    try:
        start = max(0.0, float(start))
    except (TypeError, ValueError):
        start = 0.0
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    if speed not in (1.0, 2.0, 3.0, 5.0):
        speed = 1.0
    if quality == "source":
        height, bitrate, bufsize, audio = None, "8000k", "16000k", "192k"
        overlay = subtitle_filter(source, None, subtitle_lang)
    else:
        preset = STREAM_PRESETS.get(quality)
        if preset is None:
            return None
        height, bitrate, bufsize, audio = preset
        overlay = subtitle_filter(source, height, subtitle_lang)
    if height:
        video_filter = f"scale=-2:'min({height},ih)'"
    else:
        video_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if overlay:
        video_filter = f"{video_filter},{overlay}"
    if speed == 1.0:
        video_filter += ",setsar=1,setpts=PTS-STARTPTS,format=yuv420p"
        audio_filter = "asetpts=PTS-STARTPTS"
    else:
        video_filter += f",setsar=1,setpts=(PTS-STARTPTS)/{speed:g},format=yuv420p"
        audio_filter = f"asetpts=PTS-STARTPTS,atempo={speed:g}"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if start >= 0.4:
        command += ["-ss", f"{start:.3f}", "-copyts"]
    command += [
        "-probesize",
        "2000000",
        "-analyzeduration",
        "2000000",
        "-filter_threads",
        "1",
        "-fflags",
        "+genpts",
        "-i",
        media_input or source,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-vf",
        video_filter,
        *_encoder_args(encoder, bitrate, bufsize),
        "-af",
        audio_filter,
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-b:a",
        audio,
        "-g",
        "60",
        "-flush_packets",
        "1",
        "-muxpreload",
        "0",
        "-muxdelay",
        "0",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "mpegts",
        "pipe:1",
    ]
    return command


class Library:
    def __init__(self):
        self.lock = threading.Lock()
        self.files: dict[str, str] = {}
        self.black_mp4: str | None = None
        self.black_jpg: str | None = None
        self.on_log = None
        self._announced: set[str] = set()
        self.fetched: set[str] = set()
        self.quality = "720"
        self.subtitle_lang = "中文"
        self.encoder = "libx264"
        self.stream_start = 0.0
        self.playback_rate = 1.0
        self.stream_generation = 0
        self.stream_clients = 0
        self.stream_complete = False
        self.stream_ended = 0
        self.pending_seek: float | None = None
        self.disk_cut = ""

    def add(self, path: str) -> str:
        media_id = uuid.uuid4().hex
        with self.lock:
            self.files[media_id] = path
        return media_id

    def discard(self, media_id: str) -> None:
        with self.lock:
            self.files.pop(media_id, None)
            self._announced.discard(media_id)

    def replace(self, media_id: str, path: str) -> None:
        with self.lock:
            if media_id in self.files:
                self.files[media_id] = path

    def mark_disk_cut(self, media_id: str) -> None:
        with self.lock:
            if media_id:
                self.disk_cut = media_id

    def take_disk_cut(self, media_id: str) -> bool:
        with self.lock:
            if media_id and self.disk_cut == media_id:
                self.disk_cut = ""
                return True
            return False

    def clear_disk_cut(self) -> None:
        with self.lock:
            self.disk_cut = ""

    def get(self, media_id: str) -> str | None:
        with self.lock:
            return self.files.get(media_id)

    def set_playback(self, start: float, rate: float) -> None:
        with self.lock:
            self.stream_start = max(0.0, float(start))
            self.playback_rate = float(rate) if float(rate) in (1.0, 2.0, 3.0, 5.0) else 1.0

    def playback(self) -> tuple[float, float]:
        with self.lock:
            return self.stream_start, self.playback_rate

    def open_stream(self) -> int:
        with self.lock:
            self.stream_generation += 1
            self.stream_clients = 0
            self.stream_complete = False
            self.stream_ended = 0
            self.pending_seek = None
            return self.stream_generation

    def cancel_stream(self) -> None:
        self.open_stream()

    def current_generation(self) -> int:
        with self.lock:
            return self.stream_generation

    def attach_stream(self, generation: int) -> bool:
        with self.lock:
            if generation <= 0 or generation != self.stream_generation:
                return False
            self.stream_clients += 1
            return True

    def detach_stream(self, generation: int, ended: bool) -> None:
        with self.lock:
            if generation != self.stream_generation:
                return
            self.stream_clients = max(0, self.stream_clients - 1)
            if ended:
                self.stream_complete = True
            if self.stream_clients == 0 and self.stream_complete:
                self.stream_ended = generation

    def stream_has_ended(self) -> bool:
        with self.lock:
            return self.stream_generation > 0 and self.stream_ended == self.stream_generation

    def note_seek(self, start: float) -> None:
        with self.lock:
            self.stream_start = max(0.0, float(start))
            self.pending_seek = self.stream_start
            self.stream_complete = False
            self.stream_ended = 0

    def take_pending_seek(self) -> float | None:
        with self.lock:
            value = self.pending_seek
            self.pending_seek = None
            return value

    def mark_fetched(self, media_id: str) -> None:
        with self.lock:
            self.fetched.add(media_id)

    def was_fetched(self, media_id: str) -> bool:
        with self.lock:
            return media_id in self.fetched

    def note(self, media_id: str, label: str) -> None:
        with self.lock:
            self.fetched.add(media_id)
            if media_id in self._announced:
                return
            self._announced.add(media_id)
        if self.on_log:
            self.on_log(label)


class Handler(BaseHTTPRequestHandler):
    library: Library
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_HEAD(self) -> None:
        path = self.path.split("?", 1)[0]
        if re.fullmatch(r"/stream/[0-9a-f]{32}\.ts", path):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self._stream_headers(0.0)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self._serve(head=True)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        match = re.fullmatch(r"/stream/([0-9a-f]{32})\.ts", path)
        if match:
            self._serve_stream(match.group(1))
            return
        self._serve(head=False)

    def _npt_request(self) -> float | None:
        raw = self.headers.get("TimeSeekRange.dlna.org") or ""
        match = re.search(r"npt\s*=\s*([0-9:.]+)", raw, re.I)
        if not match:
            return None
        return parse_npt(match.group(1))

    def _stream_headers(self, start: float) -> None:
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header(
            "contentFeatures.dlna.org",
            "DLNA.ORG_OP=10;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000",
        )
        self.send_header("TimeSeekRange.dlna.org", f"npt={max(0.0, start):.3f}-")

    def _serve_stream(self, media_id: str) -> None:
        source = self.library.get(media_id) or ""
        try:
            known_size = os.path.getsize(source) if source else 0
        except OSError:
            known_size = 0
        playable = resolve_playable(source, known_size)
        start, speed = self.library.playback()
        requested = self._npt_request()
        if requested is not None:
            start = requested
        generation = self.library.current_generation()
        command = stream_command(
            source,
            self.library.quality,
            self.library.encoder,
            self.library.subtitle_lang,
            start,
            speed,
            playable,
        )
        if not source or command is None or not self.library.attach_stream(generation):
            self.send_error(404)
            return
        if requested is not None:
            self.library.note_seek(start)
        quality = self.library.quality
        label = "原画" if quality == "source" else f"{quality}p"
        if subtitle_for(source, self.library.subtitle_lang):
            label += " · 字幕"
        self.library.note(media_id, f"正在转成 {label}  {Path(source).name}")
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError:
            self.library.detach_stream(generation, False)
            self.send_error(500)
            return
        disconnected = False
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self._stream_headers(start)
            self.send_header("Connection", "close")
            self.end_headers()
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            disconnected = True
        finally:
            if disconnected and proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            clean = (not disconnected) and proc.returncode == 0 and (path_ok(playable) or path_ok(source))
            if not clean and not path_ok(source) and generation == self.library.current_generation():
                self.library.mark_disk_cut(media_id)
            self.library.detach_stream(generation, clean)

    def _serve(self, head: bool) -> None:
        path = self.path.split("?", 1)[0]
        target, media_id, label = self._resolve(path)
        if not target:
            self.send_error(404)
            return
        try:
            size = os.path.getsize(target)
        except OSError:
            original = self.library.get(media_id) if media_id else ""
            if original and not path_ok(original):
                self.library.mark_disk_cut(media_id)
            self.send_error(404)
            return
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0].strip()
            try:
                if spec.startswith("-"):
                    start = max(0, size - int(spec[1:] or "0"))
                else:
                    left, _, right = spec.partition("-")
                    start = int(left) if left else 0
                    end = int(right) if right else size - 1
            except ValueError:
                self.send_error(416)
                return
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self._finish_headers(0)
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        mime = "video/mp4" if target.endswith(".mp4") else "image/jpeg" if target.endswith(".jpg") else mime_for(target)
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header(
            "contentFeatures.dlna.org",
            "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000",
        )
        self._finish_headers(length)
        if media_id and start == 0:
            self.library.note(media_id, label)
        if head:
            return
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except OSError:
            original = self.library.get(media_id) if media_id else ""
            if original and not path_ok(original):
                self.library.mark_disk_cut(media_id)
            return

    def _finish_headers(self, _length: int) -> None:
        self.send_header("Connection", "close")
        self.send_header("Server", "Qingying/1.0")
        self.end_headers()

    def _resolve(self, path: str) -> tuple[str | None, str, str]:
        library = self.library
        if path == "/black.mp4" and library.black_mp4:
            return library.black_mp4, "black", "投影仪开始显示全黑画面"
        if path == "/black.jpg" and library.black_jpg:
            return library.black_jpg, "black-jpg", "投影仪开始显示全黑画面"
        match = re.fullmatch(r"/media/([0-9a-f]{32})", path)
        if not match:
            return None, "", ""
        media_id = match.group(1)
        file_path = library.get(media_id)
        if not file_path:
            return None, "", ""
        try:
            known_size = os.path.getsize(file_path)
        except OSError:
            known_size = 0
        return resolve_playable(file_path, known_size), media_id, f"投影仪正在读取  {Path(file_path).name}"


def ensure_firewall() -> None:
    """允许投影仪连回这台电脑。规则已存在时不会重复添加。"""
    if os.name != "nt":
        return
    commands = []
    port_probe = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", "name=清映投屏端口"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    if port_probe.returncode != 0:
        commands.append(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                "name=清映投屏端口",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                "localport=18765-18784",
                "enable=yes",
                "profile=private,domain",
            ]
        )
    if getattr(sys, "frozen", False):
        program_probe = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=清映投屏"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
        if program_probe.returncode != 0:
            commands.append(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    "name=清映投屏",
                    "dir=in",
                    "action=allow",
                    f"program={sys.executable}",
                    "enable=yes",
                    "profile=private,domain",
                ]
            )
    for command in commands:
        subprocess.run(command, capture_output=True, creationflags=CREATE_NO_WINDOW)


def serve(library: Library, port: int = 18765) -> tuple[ThreadingHTTPServer, int]:
    Handler.library = library

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    last_error: Exception | None = None
    for candidate in range(port, port + 20):
        try:
            httpd = Server(("0.0.0.0", candidate), Handler)
        except OSError as exc:
            last_error = exc
            continue
        thread = threading.Thread(target=httpd.serve_forever, name="qingying-http", daemon=True)
        thread.start()
        return httpd, candidate
    raise RuntimeError(f"投屏端口被占用：{last_error}")
