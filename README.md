# 清映

把电脑里的视频，按顺序投到同一局域网里的 DLNA 投影仪。米家、小米、峰米，以及其他支持 DLNA 的投影都可以。

播放时电脑要保持开着。电脑上的画面是控制台，片子在投影仪上播。

## 打赏

<p align="center">
  <img src="docs/afdian-qr.jpg" width="280" alt="爱发电：萝卜君">
</p>

请扫上面的爱发电二维码。这是爱发电赞助页，由平台代收。不要改用个人微信或支付宝收款码：陌生人的钱会一笔一笔直接进银行卡，笔数一多可能触发风控，绑定的储蓄卡会被冻结。爱发电把赞助记到创作者余额，你再自己提现。平台大约收取 6%（含支付通道费），其余次月可提现。

## 它能做什么

- 在同一路由器下搜索投影仪，搜不到时可以手填 IP。
- 拖入视频或文件夹。文件夹按文件名排序，列表可以再拖动调整。
- 播放、暂停、上一个、下一个、停止，后退或快进 30 秒。
- 倍速：1、2、3、5 倍。
- 拖动进度条时，会显示附近的画面。
- 画质可选 480、720、1080 或原画。转码后多数投影仪更好解。
- 自动找同目录字幕。有中英双语就优先用；否则用你选的语言，没有再试中文，再没有试英语。
- 到点息屏：倒计时和定时互不影响。到点会停掉影片，并把投影切成全黑。

支持的视频：mp4、mkv、avi、mov、wmv、flv、webm、m4v、ts、m2ts、mpg、mpeg。  
支持的字幕：srt、ass、ssa、vtt。

## 使用前

1. 电脑和投影仪连到同一台路由器。有线和 Wi-Fi 也要在同一个网里，不能一个在主路由、一个在访客网络。
2. 在投影仪里打开投屏（DLNA / 无线投屏）。
3. 播放期间不要让电脑睡眠。清映播放时会尽量阻止系统休眠。
4. 转码、字幕、进度预览和全黑画面需要 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)。把 `ffmpeg.exe` 放在程序旁边，或加入系统 PATH。只播投影仪能直接解的原画 mp4 时，可以先不装。

## 直接运行

需要 Windows 和 Python 3.10 或更新版本。

```bat
启动清映.bat
```

第一次运行如果缺少界面组件，脚本会按 `requirements.txt` 安装。也可以自己执行：

```bat
py -3 -m pip install -r requirements.txt
py -3 main.py
```

程序会尝试放行本机端口 `18765–18784`，让投影仪连回电脑。如果搜索得到设备但一直投不出去，用管理员身份运行一次。

## 打包成程序

安装 PyInstaller 后，在本目录执行：

```bat
py -3 -m PyInstaller 清映.spec
```

生成的程序在 `dist\清映\清映.exe`。把 `ffmpeg.exe` 放在这个 exe 旁边。`启动清映.bat` 发现这个程序后，会直接打开它。

打包结果、本机缓存和上次使用的投影仪地址不会进入仓库。

## 投不出去时

- 投影仪已开机，投屏开关是开的。
- 电脑显示的网段和投影仪在同一段。界面上看不到设备时，用投影仪的 IP 手动连接。
- 原画播不动时改成 720 或 1080，让清映转码后再送出去。
- 字幕乱码或没有字幕：确认字幕文件和视频在同一目录，文件名接近。
- 到点没有变成全黑：检查 ffmpeg 是否可用。

---

# Qingying

Cast videos from a Windows PC, in playlist order, to a DLNA projector on the same LAN. This includes Mi Home, Xiaomi, Formovie, and other DLNA renderers.

The PC stays on as the remote. The movie plays on the projector.

## Tips

<p align="center">
  <img src="docs/afdian-qr.jpg" width="280" alt="Afdian: 萝卜君">
</p>

Scan the Afdian code above. Afdian collects the sponsorship as a platform. Do not switch to a personal WeChat or Alipay collection code: those payments land directly on a bank card, and enough transfers from strangers can freeze the linked debit card. Afdian credits a creator balance, and you withdraw that balance yourself. The platform keeps about 6% (including payment-channel fees). The rest can be withdrawn the following month.

## What it does

- Finds projectors on the same router. If discovery fails, type the projector IP.
- Accepts dropped videos or folders. A folder is sorted by file name, and the playlist can be reordered by dragging.
- Play, pause, previous, next, stop, and jump backward or forward 30 seconds.
- Speeds: 1×, 2×, 3×, and 5×.
- Dragging the timeline shows a nearby frame.
- Picture size: 480, 720, 1080, or original. Transcoding is easier for most projectors to decode.
- Picks up subtitle files in the same folder. A Chinese-English subtitle is preferred. Otherwise it uses the selected language, then Chinese, then English.
- Sleep timer: a countdown and a clock time work independently. At the chosen time, playback stops and the projector switches to a black frame.

Video: mp4, mkv, avi, mov, wmv, flv, webm, m4v, ts, m2ts, mpg, mpeg.  
Subtitles: srt, ass, ssa, vtt.

## Before you start

1. Put the PC and the projector on the same router. Ethernet and Wi-Fi must be the same network, not a guest network beside the main LAN.
2. Turn on casting on the projector (DLNA / wireless display).
3. Leave the PC awake while a video is playing. Qingying asks Windows not to sleep during playback.
4. Transcoding, subtitles, seek preview, and the black frame need [ffmpeg](https://www.gyan.dev/ffmpeg/builds/). Place `ffmpeg.exe` next to the program, or add it to PATH. Original mp4 files that the projector can decode may play without it.

## Run from source

Windows, with Python 3.10 or newer.

```bat
启动清映.bat
```

On the first launch, the script installs the UI packages from `requirements.txt` if they are missing. Or install and start it yourself:

```bat
py -3 -m pip install -r requirements.txt
py -3 main.py
```

Qingying tries to allow local TCP ports `18765–18784` so the projector can connect back. If a device is found but playback never starts, run it once as administrator.

## Build an app

With PyInstaller installed, from this directory:

```bat
py -3 -m PyInstaller 清映.spec
```

The app is `dist\清映\清映.exe`. Keep `ffmpeg.exe` beside that exe. `启动清映.bat` launches the built app when it is present.

Build output, local cache, and the last projector address are not part of this repository.

## If casting fails

- The projector is on, and its cast switch is enabled.
- The PC and the projector are on the same subnet. If the list stays empty, connect with the projector’s IP.
- If the original file will not play, switch to 720 or 1080 so Qingying transcodes it.
- Missing or garbled subtitles: keep the subtitle file next to the video, with a similar file name.
- The screen never goes black at the timer: check that ffmpeg runs.
