"""投屏播放、切黑和设备搜索。"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dlna import (
    Device,
    DlnaControl,
    DlnaError,
    NetIf,
    discover,
    format_clock,
    ip_for_peer,
    local_interfaces,
    probe_host,
    same_subnet,
)
from media import SUBTITLE_LANGS, Library, drive_ready, locate_file, mime_for, mirror, subtitle_for

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


_caffeinate: subprocess.Popen | None = None


def keep_awake(on: bool) -> None:
    global _caffeinate
    if os.name == "nt":
        import ctypes

        flag = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flag)
        return
    if sys.platform != "darwin":
        return
    if on:
        if _caffeinate is None or _caffeinate.poll() is not None:
            _caffeinate = subprocess.Popen(["caffeinate", "-ims"])
        return
    if _caffeinate is not None and _caffeinate.poll() is None:
        _caffeinate.terminate()
    _caffeinate = None


class Bridge(QObject):
    devices = Signal(object)
    manual_device = Signal(object)
    scan_finished = Signal()
    log = Signal(str)
    status = Signal(str)
    track = Signal(str)
    progress = Signal(float, float)
    mode = Signal(str)
    linked = Signal(str)
    rate = Signal(float)
    relocated = Signal(str, str)


class Engine(threading.Thread):
    def __init__(self, bridge: Bridge, library: Library, port: int, ifaces: list[NetIf]):
        super().__init__(name="qingying-engine", daemon=True)
        self.bridge = bridge
        self.library = library
        self.port = port
        self.ifaces = ifaces
        self.commands: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.mode_name = "idle"
        self.playlist: list[dict] = []
        self.current_id = ""
        self.device: Device | None = None
        self.ctl: DlnaControl | None = None
        self.base = ""
        self.started = 0.0
        self.fails = 0
        self.stall = 0
        self.last_pos: float | None = None
        self.saw_progress = False
        self.black_pushed = 0.0
        self.rate = 1.0
        self.using_stream = False
        self.stream_origin = 0.0
        self.media_pos = 0.0
        self.anchor_pos = 0.0
        self.anchor_time = time.time()
        self.stream_done_at = 0.0
        self.pos_from_device = False
        self.disk_misses = 0
        self.resume_pos = 0.0
        self.resume_tries = 0
        self.cache_for = ""
        self.cache_noted = False
        self.cache_start_noted = False

    def command(self, name: str, *args) -> None:
        self.commands.put((name, args))

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                name, args = self.commands.get(timeout=0.9)
            except queue.Empty:
                try:
                    self._tick()
                except Exception as exc:
                    self.bridge.log.emit(f"读取投影仪状态失败：{exc}")
                continue
            try:
                getattr(self, f"cmd_{name}")(*args)
            except Exception as exc:
                self.bridge.log.emit(f"出错：{exc}")
                self.bridge.status.emit(str(exc))

    def _set_mode(self, name: str) -> None:
        self.mode_name = name
        self.bridge.mode.emit(name)
        keep_awake(name in ("playing", "paused", "blackout", "waiting"))

    def _index_of(self, media_id: str) -> int:
        for index, item in enumerate(self.playlist):
            if item["id"] == media_id:
                return index
        return -1

    def _current(self) -> dict | None:
        index = self._index_of(self.current_id)
        if index < 0:
            return None
        return self.playlist[index]

    def cmd_scan(self) -> None:
        self.bridge.status.emit("正在搜索电视或投影仪…")
        self.ifaces = local_interfaces() or self.ifaces
        if not self.ifaces:
            self.bridge.devices.emit([])
            self.bridge.scan_finished.emit()
            self.bridge.status.emit("没有检测到可用的局域网网卡")
            return
        labels = "、".join(iface.label for iface in self.ifaces)
        self.bridge.log.emit(f"搜索网段 {labels}")
        devices = discover(self.ifaces, timeout=4.8)
        self.bridge.devices.emit(devices)
        self.bridge.scan_finished.emit()
        if devices:
            self.bridge.status.emit(f"找到 {len(devices)} 台可投屏设备")
            self.bridge.log.emit("、".join(f"{item.name}（{item.host}）" for item in devices))
        else:
            self.bridge.status.emit("没有搜到投影仪")
            self.bridge.log.emit("没有收到投影仪的投屏响应")

    def cmd_manual(self, ip: str) -> None:
        self.bridge.status.emit(f"正在连接 {ip}…")
        self.ifaces = local_interfaces() or self.ifaces
        if self.ifaces and not same_subnet(ip, self.ifaces):
            self.bridge.status.emit("这个地址和电脑不在同一个网段")
            self.bridge.log.emit(f"{ip} 和本机不在同一网段，投影仪可能读不到视频")
        device = probe_host(ip)
        self.bridge.scan_finished.emit()
        if device is None:
            self.bridge.status.emit("这个地址没有打开投屏")
            self.bridge.log.emit(f"{ip} 没有返回 DLNA 描述")
            return
        self.bridge.manual_device.emit(device)

    def cmd_select(self, device: Device) -> None:
        previous = self.ctl
        was = self.mode_name
        current = self.current_id
        if previous is not None:
            try:
                previous.stop()
            except Exception:
                pass
        self.device = device
        self.ctl = DlnaControl(device.control_url)
        host_ip = ip_for_peer(device.host, self.ifaces)
        self.base = f"http://{host_ip}:{self.port}"
        self.bridge.log.emit(f"已选择 {device.name}  {device.host}")
        self.bridge.log.emit(f"视频将从 {self.base} 推送")
        try:
            state = self.ctl.transport_state()
            self.bridge.log.emit(f"投影仪当前状态 {state}")
        except DlnaError as exc:
            self.bridge.linked.emit("")
            self.bridge.status.emit(f"找到了设备，但控制失败：{exc}")
            self.bridge.log.emit(str(exc))
            return
        self.bridge.linked.emit(device.name)
        self.bridge.status.emit(f"已连接  {device.name}")
        if was in ("playing", "paused") and current:
            self.current_id = current
            index = self._index_of(current)
            if index >= 0:
                self.play_at(index)

    def cmd_playlist(self, items: list) -> None:
        self.playlist = list(items)
        if self.mode_name in ("playing", "paused") and self.current_id:
            if self._index_of(self.current_id) < 0:
                self.cmd_stop()
                self.bridge.status.emit("正在播的视频已从列表移除")

    def cmd_play(self, index: int) -> None:
        if self.ctl is None:
            self.bridge.status.emit("请先选择投影仪")
            return
        if not self.playlist:
            self.bridge.status.emit("请先把视频拖进来")
            return
        if index < 0 or index >= len(self.playlist):
            index = 0
        item = self.playlist[index]
        if self.mode_name == "paused" and item["id"] == self.current_id:
            self.cmd_resume()
            return
        self.fails = 0
        self.play_at(index)

    def cmd_pause(self) -> None:
        if self.ctl is None or self.mode_name != "playing":
            return
        try:
            self.ctl.pause()
        except DlnaError as exc:
            self.bridge.status.emit(f"暂停失败：{exc}")
            return
        self.anchor_pos = self.media_pos
        self.anchor_time = time.time()
        self._set_mode("paused")
        self.bridge.status.emit("已暂停")

    def cmd_resume(self) -> None:
        if self.ctl is None:
            return
        try:
            self.ctl.play()
        except DlnaError as exc:
            self.bridge.status.emit(f"继续播放失败：{exc}")
            return
        self.anchor_pos = self.media_pos
        self.anchor_time = time.time()
        self.started = time.time()
        self._set_mode("playing")
        item = self._current()
        self.bridge.status.emit(f"正在播放  {item['name']}" if item else "正在播放")

    def cmd_stop(self) -> None:
        if self.ctl is not None:
            try:
                self.ctl.stop()
            except Exception:
                pass
        self.current_id = ""
        self.media_pos = 0.0
        self.using_stream = False
        self.library.cancel_stream()
        self.library.clear_disk_cut()
        self._set_mode("idle")
        self.bridge.track.emit("")
        self.bridge.progress.emit(0, 0)
        self.bridge.status.emit("已停止")

    def cmd_blackout(self) -> None:
        self.push_black(announce=True)

    def play_at(self, index: int) -> None:
        if self.ctl is None or not self.base:
            self.bridge.status.emit("请先选择投影仪")
            self._set_mode("idle")
            return
        if index >= len(self.playlist):
            self.current_id = ""
            self.media_pos = 0.0
            self._set_mode("idle")
            self.bridge.track.emit("")
            self.bridge.progress.emit(0, 0)
            self.bridge.status.emit("列表已经播完")
            try:
                self.ctl.stop()
            except Exception:
                pass
            return
        if index < 0:
            index = 0
        item = self.playlist[index]
        if not self._source_ok(item):
            self.current_id = item["id"]
            if not drive_ready(item["path"]):
                self.media_pos = 0.0
                self.resume_pos = 0.0
                self._enter_wait(item)
                return
            self.bridge.log.emit(f"找不到文件，已跳过  {item['name']}")
            self._fail_skip()
            return
        self._begin(item, 0.0)

    def cmd_jump(self, delta: float) -> None:
        item = self._current()
        if self.ctl is None or item is None or self.mode_name not in ("playing", "paused"):
            self.bridge.status.emit("先播放，才能快进或后退")
            return
        dur = float(item.get("duration") or 0)
        target = max(0.0, self.media_pos + float(delta))
        self._go_to(item, target, float(delta), dur)

    def cmd_seek(self, target: float) -> None:
        item = self._current()
        if self.ctl is None or item is None or self.mode_name not in ("playing", "paused"):
            self.bridge.status.emit("先播放，才能调整进度")
            return
        try:
            target = float(target)
        except (TypeError, ValueError):
            return
        dur = float(item.get("duration") or 0)
        self._go_to(item, max(0.0, target), max(0.0, target) - self.media_pos, dur)

    def _go_to(self, item: dict, target: float, delta: float, dur: float) -> None:
        if self.ctl is None:
            return
        target = max(0.0, target)
        if dur > 8 and target >= dur - 3:
            if delta > 0:
                self._play_next()
                return
            target = max(0.0, dur - 3)
        if abs(delta) < 0.8:
            return
        if not self.using_stream and self.rate == 1.0:
            try:
                if self.mode_name == "paused":
                    self.ctl.play()
                self.ctl.seek(target)
            except DlnaError:
                self._begin(item, target, jumped=delta)
                return
            self.media_pos = target
            self.anchor_pos = target
            self.anchor_time = time.time()
            self.started = time.time()
            self.stall = 0
            self.last_pos = target
            self._set_mode("playing")
            self.bridge.progress.emit(target, dur)
            self.bridge.status.emit(self._jump_text(delta, target))
            return
        self._begin(item, target, jumped=delta)

    def _play_next(self) -> None:
        index = self._index_of(self.current_id)
        if index < 0:
            return
        self.fails = 0
        self.play_at(index + 1)

    def cmd_rate(self, rate: float) -> None:
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return
        if rate not in (1.0, 2.0, 3.0, 5.0):
            return
        same = rate == self.rate
        self.rate = rate
        self.bridge.rate.emit(rate)
        item = self._current()
        if self.ctl is None or item is None or self.mode_name not in ("playing", "paused"):
            self.bridge.status.emit(self._rate_text())
            return
        if same:
            self.bridge.status.emit(self._rate_text())
            return
        self._begin(item, self.media_pos)
        if self.mode_name == "playing":
            self.bridge.status.emit(f"{self._rate_text()}  ·  {format_clock(self.media_pos)}")

    def _uses_stream(self, item: dict) -> bool:
        if self.rate != 1.0:
            return True
        if self.library.quality in ("480", "720", "1080"):
            return True
        return subtitle_for(item["path"], self.library.subtitle_lang) is not None

    def _rate_text(self) -> str:
        return "1 倍速" if self.rate == 1.0 else f"{int(self.rate)} 倍速"

    def _jump_text(self, delta: float, target: float) -> str:
        verb = "已后退到" if float(delta) < 0 else "已快进到"
        text = f"{verb} {format_clock(target)}"
        if self.rate != 1.0:
            text += f"  ·  {self._rate_text()}"
        return text

    def _begin(self, item: dict, origin: float, jumped: float | None = None) -> None:
        if self.ctl is None or not self.base:
            self.bridge.status.emit("请先选择投影仪")
            return
        origin = max(0.0, float(origin))
        uses = self._uses_stream(item)
        quality = self.library.quality
        plan = subtitle_for(item["path"], self.library.subtitle_lang)
        label = "原画" if quality == "source" else f"{quality}p"
        if plan:
            label += " · 字幕"
        if self.rate != 1.0:
            label += f" · {int(self.rate)}倍"
        try:
            self.ctl.stop()
        except Exception:
            pass
        time.sleep(0.2)
        self.using_stream = uses
        self.stream_origin = origin if uses else 0.0
        self.media_pos = origin
        self.anchor_pos = origin
        self.anchor_time = time.time()
        self.stream_done_at = 0.0
        self.pos_from_device = False
        duration = float(item.get("duration") or 0)
        if uses:
            self.library.set_playback(origin, self.rate)
            self.library.open_stream()
            url = f"{self.base}/stream/{item['id']}.ts?v={int(time.time() * 1000)}"
            mime = "video/mp2t"
        else:
            url = f"{self.base}/media/{item['id']}"
            mime = mime_for(item["path"])
        try:
            self.ctl.set_uri(url, mime, item["name"], duration=duration, seekable=uses)
            self.ctl.play()
            if not uses and origin > 0.4:
                time.sleep(0.25)
                self.ctl.seek(origin)
        except DlnaError as exc:
            if not uses and origin > 0.4:
                try:
                    self.ctl.stop()
                except Exception:
                    pass
                self.using_stream = True
                self.stream_origin = origin
                self.library.set_playback(origin, self.rate)
                self.library.open_stream()
                url = f"{self.base}/stream/{item['id']}.ts?v={int(time.time() * 1000)}"
                try:
                    self.ctl.set_uri(url, "video/mp2t", item["name"], duration=duration, seekable=True)
                    self.ctl.play()
                except DlnaError as second:
                    self.bridge.log.emit(f"投屏失败  {item['name']}  {second}")
                    self.current_id = item["id"]
                    self._fail_skip()
                    return
            else:
                self.bridge.log.emit(f"投屏失败  {item['name']}  {exc}")
                self.current_id = item["id"]
                self._fail_skip()
                return
        index = self._index_of(item["id"])
        self.current_id = item["id"]
        self.started = time.time()
        self.anchor_pos = origin
        self.anchor_time = self.started
        self.stall = 0
        self.last_pos = origin
        self.saw_progress = False
        self._set_mode("playing")
        self.bridge.track.emit(item["id"])
        self.bridge.progress.emit(origin, float(item.get("duration") or 0))
        self.library.clear_disk_cut()
        self._note_cache(item)
        if jumped is None:
            self.bridge.status.emit(f"正在播放  {item['name']}")
            shown = index + 1 if index >= 0 else 1
            self.bridge.log.emit(f"播放 {label}  {shown}/{len(self.playlist)}  {item['name']}")
            if plan and plan[0] == "file":
                self.bridge.log.emit(f"字幕  {Path(plan[1]).name}")
        else:
            self.bridge.status.emit(self._jump_text(jumped, origin))

    def _source_position(self, device_pos: float | None) -> tuple[float, bool]:
        now = time.time()
        elapsed = now - self.started
        if not self.using_stream:
            self.pos_from_device = device_pos is not None
            if device_pos is None:
                return self.media_pos, False
            if elapsed < 6 and self.media_pos > 1 and abs(device_pos - self.media_pos) > 20:
                self.pos_from_device = False
                return self.media_pos, False
            return device_pos, device_pos > 1
        self.pos_from_device = False
        paced = max(self.rate, 1.0)
        if device_pos is not None and device_pos >= 0:
            limit = max(12.0, elapsed * paced + 15)
            early_glitch = elapsed < 8 and device_pos > elapsed * paced + 4
            if device_pos > 0.8 and device_pos <= limit and not early_glitch:
                pos = self.stream_origin + device_pos * self.rate
                self.anchor_pos = pos
                self.anchor_time = now
                self.pos_from_device = True
                return pos, True
        if not self.current_id or not self.library.was_fetched(self.current_id):
            return self.anchor_pos, False
        pos = self.anchor_pos + max(0.0, now - self.anchor_time) * self.rate
        return pos, False

    def cmd_subtitle(self, lang: str) -> None:
        if lang not in SUBTITLE_LANGS:
            return
        self.library.subtitle_lang = lang
        if self.mode_name in ("playing", "paused") and self.current_id:
            index = self._index_of(self.current_id)
            if index >= 0:
                self.play_at(index)

    def cmd_quality(self, quality: str) -> None:
        if quality not in ("480", "720", "1080", "source"):
            return
        changed = quality != self.library.quality
        self.library.quality = quality
        if changed and self.mode_name in ("playing", "paused") and self.current_id:
            index = self._index_of(self.current_id)
            if index >= 0:
                self.play_at(index)

    def _fail_skip(self) -> None:
        self.fails += 1
        if self.fails >= 3:
            self._set_mode("idle")
            self.bridge.track.emit("")
            self.bridge.status.emit("连续三次没有播出来，已停止")
            return
        index = self._index_of(self.current_id)
        self.play_at(index + 1)

    def push_black(self, announce: bool) -> None:
        if self.ctl is None or not self.base:
            self.bridge.status.emit("时间到了，但还没有连接投影仪")
            return
        try:
            self.ctl.stop()
        except Exception:
            pass
        time.sleep(0.15)
        try:
            if self.library.black_mp4:
                self.ctl.set_uri(f"{self.base}/black.mp4", "video/mp4", "息屏", "video")
            elif self.library.black_jpg:
                self.ctl.set_uri(f"{self.base}/black.jpg", "image/jpeg", "息屏", "image")
            else:
                self.bridge.status.emit("全黑画面还没准备好，请稍等几秒再试")
                return
            self.ctl.play()
        except DlnaError as exc:
            self.bridge.status.emit(f"无法切换全黑：{exc}")
            self.bridge.log.emit(str(exc))
            return
        self.current_id = ""
        self.library.cancel_stream()
        self.black_pushed = time.time()
        self._set_mode("blackout")
        self.bridge.track.emit("")
        self.bridge.progress.emit(0, 0)
        if announce:
            self.bridge.status.emit("投影已切换为全黑")
            self.bridge.log.emit("已发送全黑画面")

    def _note_cache(self, item: dict) -> None:
        if item["id"] != self.cache_for:
            self.cache_for = item["id"]
            self.cache_noted = False
            self.cache_start_noted = False
        state = mirror.start(item["path"], int(item.get("size") or 0))
        if state == "started" and not self.cache_start_noted:
            self.cache_start_noted = True
            self.bridge.log.emit("正在把这一集缓存到电脑。缓存完成前硬盘若断开，插回后会从断点继续")
        elif state == "ready" and not self.cache_noted:
            self.cache_noted = True
            self.bridge.log.emit("这一集已经缓存在电脑里，硬盘断开也能接着播")
        elif state == "space" and not self.cache_start_noted:
            self.cache_start_noted = True
            self.bridge.log.emit("本机空间不够缓存整集。硬盘断开后，插回会从断点继续")

    def _source_ok(self, item: dict) -> bool:
        path = item.get("path") or ""
        size = int(item.get("size") or 0)
        if mirror.ready(path, size):
            return True
        found = locate_file(path, size)
        if not found:
            return False
        try:
            found_size = os.path.getsize(found)
        except OSError:
            found_size = size
        if found_size > 0:
            item["size"] = found_size
        if os.path.normcase(os.path.abspath(found)) != os.path.normcase(os.path.abspath(path)):
            item["path"] = found
            self.library.replace(item["id"], found)
            self.bridge.relocated.emit(item["id"], found)
            self.bridge.log.emit(f"硬盘盘符变了，已改到 {found}")
        return True

    def _enter_wait(self, item: dict) -> None:
        if self.mode_name == "waiting":
            return
        self.resume_pos = max(0.0, self.media_pos)
        self.bridge.log.emit("移动硬盘断开了，正在等待重新连接")
        self.bridge.status.emit(f"硬盘断开了，插上后从 {format_clock(self.resume_pos)} 继续")
        if self.ctl is not None:
            try:
                self.ctl.stop()
            except Exception:
                pass
        self.library.cancel_stream()
        self._set_mode("waiting")
        self.bridge.progress.emit(self.resume_pos, float(item.get("duration") or 0))

    def _resume_from(self, item: dict, pos: float) -> None:
        if self.resume_tries >= 4:
            self.bridge.status.emit("反复续播没有成功，已停止")
            self._set_mode("idle")
            return
        self.resume_tries += 1
        self.fails = 0
        self.bridge.log.emit(f"从 {format_clock(pos)} 接着播")
        self._begin(item, max(0.0, pos))

    def _tick(self) -> None:
        if self.mode_name == "playing":
            self._tick_play()
        elif self.mode_name == "waiting":
            self._tick_wait()
        elif self.mode_name == "blackout":
            self._tick_black()

    def _tick_wait(self) -> None:
        item = self._current()
        if item is None or not self._source_ok(item):
            return
        self.disk_misses = 0
        self.resume_tries = 0
        pos = self.resume_pos
        self.bridge.log.emit("硬盘已连上，从刚才的进度继续")
        self.bridge.status.emit(f"硬盘已连上，从 {format_clock(pos)} 继续")
        self._begin(item, pos)

    def _tick_play(self) -> None:
        if self.ctl is None:
            return
        state = self.ctl.transport_state()
        device_pos, device_dur = self.ctl.position()
        item = self._current()
        file_dur = float(item.get("duration") or 0) if item else 0
        if file_dur > 1:
            use_dur = file_dur
        elif self.using_stream:
            use_dur = 0
        else:
            use_dur = device_dur or 0
        if self.using_stream:
            pending = self.library.take_pending_seek()
            if pending is not None:
                self.stream_origin = pending
                self.anchor_pos = pending
                self.anchor_time = time.time()
                self.started = time.time()
                self.stream_done_at = 0.0
                self.media_pos = pending
        pos, moved = self._source_position(device_pos)
        if item and not self._source_ok(item):
            self.disk_misses += 1
            self.media_pos = pos
            if self.disk_misses >= 2 or self.library.take_disk_cut(item["id"]):
                self._enter_wait(item)
            return
        self.disk_misses = 0
        if item and self.library.take_disk_cut(item["id"]):
            self.media_pos = pos
            shown = min(pos, use_dur) if use_dur > 1 else pos
            self.bridge.progress.emit(shown, use_dur)
            if use_dur > 8 and pos >= use_dur - 3:
                self._play_next()
            else:
                self._resume_from(item, pos)
            return
        if item and not self.cache_noted and mirror.ready(item["path"], int(item.get("size") or 0)):
            self.cache_noted = True
            self.bridge.log.emit("这一集已经缓存在电脑里，硬盘断开也能接着播")
        if use_dur > 1 and pos >= use_dur - 1.2 and (not self.using_stream or self.pos_from_device):
            self.media_pos = min(pos, use_dur)
            self.bridge.progress.emit(self.media_pos, use_dur)
            self._play_next()
            return
        if use_dur > 1:
            pos = min(pos, use_dur)
        self.media_pos = pos
        self.bridge.progress.emit(pos, use_dur)
        elapsed = time.time() - self.started
        if moved:
            self.saw_progress = True
            self.fails = 0
            self.resume_tries = 0
        if self.using_stream and self.library.stream_has_ended():
            if self.stream_done_at <= 0:
                self.stream_done_at = time.time()
            if state in ("STOPPED", "NO_MEDIA_PRESENT") or time.time() - self.stream_done_at >= 6:
                self._play_next()
                return
        elif self.stream_done_at:
            self.stream_done_at = 0.0
        if elapsed < 4:
            self.last_pos = pos
            return
        if state in ("STOPPED", "NO_MEDIA_PRESENT"):
            near_end = use_dur > 1 and self.media_pos >= max(15.0, use_dur - 20)
            played = self.using_stream and (self.media_pos - self.stream_origin) >= 20
            if self.saw_progress or near_end or played:
                self._play_next()
                return
        if state == "STOPPED" and not self.saw_progress and elapsed > 8:
            item_id = item["id"] if item else ""
            if item_id and not self.library.was_fetched(item_id):
                self.current_id = ""
                self._set_mode("idle")
                self.bridge.track.emit("")
                self.bridge.progress.emit(0, 0)
                self.bridge.log.emit("投影仪没有来电脑上取视频，所以画面会提示播放失败")
                self.bridge.status.emit("投影仪连不上这台电脑。请允许「清映」通过防火墙后再播放")
                return
            self.bridge.log.emit("投影仪取到了视频，但没有播出来。这个格式它可能不支持")
            self._fail_skip()
            return
        if (
            self.last_pos is not None
            and abs(pos - self.last_pos) < 0.3
            and use_dur > 1
            and pos > use_dur - 5
            and (not self.using_stream or self.pos_from_device or self.library.stream_has_ended())
        ):
            self.stall += 1
            if self.stall >= 4:
                self._play_next()
                return
        else:
            self.stall = 0
        self.last_pos = pos

    def _tick_black(self) -> None:
        if self.ctl is None or time.time() - self.black_pushed < 15:
            return
        try:
            state = self.ctl.transport_state()
            pos, dur = self.ctl.position()
        except DlnaError:
            self.push_black(announce=False)
            return
        near_end = bool(dur and dur > 1 and pos is not None and pos >= dur - 1.5)
        if state != "PLAYING" or near_end:
            self.push_black(announce=False)
