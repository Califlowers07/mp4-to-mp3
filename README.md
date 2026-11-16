# MP4 转 MP3 · 音频提取小工具

从 MP4 视频中提取音频并保存为 MP3。全程在本机用 ffmpeg 处理，不上传任何文件。
支持单个文件、批量文件夹、拖放和图形界面。

![界面预览](gui_preview.png)

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `mp4_to_mp3.py` | 主程序，同时提供图形界面和命令行 |
| `拖放MP4转MP3.bat` | 把视频拖到它上面就能转换 |
| `gui_preview.png` | 界面预览图 |
| `README.md` | 使用说明 |

## 环境要求

- Windows 10 / 11
- Python 3.8 或更高版本
- ffmpeg（需要同时有 `ffmpeg.exe` 和 `ffprobe.exe`）

检查是否已安装：

```powershell
ffmpeg -version
ffprobe -version
```

脚本会自动在以下位置查找 ffmpeg：

1. 系统 `PATH`
2. `C:\Program Files\ffmpeg\bin`
3. 脚本所在目录（或其中的 `ffmpeg\bin`）
4. 环境变量 `FFMPEG_HOME` 指向的目录 / bin 目录

安装 ffmpeg 的两种方式：

```powershell
winget install Gyan.FFmpeg
```

或从 <https://www.gyan.dev/ffmpeg/builds/> 下载压缩包，解压后把 `bin` 目录放到
`C:\Program Files\ffmpeg\bin`。

## 用法

### 1. 图形界面（推荐）

双击 `mp4_to_mp3.py`，或者在命令行运行：

```powershell
python mp4_to_mp3.py
```

如果不想看到背后的命令行窗口，可以改用 `pythonw`：

```powershell
pythonw mp4_to_mp3.py
```

操作顺序：

1. 点“添加文件…”或“添加文件夹…”（文件夹会扫描子文件夹）。
2. 选择输出位置：源文件旁边，或指定文件夹。
3. 选择码率（默认 192 kbps），需要覆盖旧文件时勾选“覆盖已存在的 MP3”。
4. 点“开始转换”，进度和日志会显示在窗口下方。

### 2. 拖放

把一个或多个 MP4 文件（也可以直接拖文件夹）拖到 `拖放MP4转MP3.bat` 上。
转换后的 MP3 默认保存在源文件旁边。

### 3. 命令行

```powershell
# 单个文件，输出到源文件旁边
python mp4_to_mp3.py "D:\视频\旅行.mp4"

# 指定输出文件夹和码率
python mp4_to_mp3.py "D:\视频\旅行.mp4" -o "D:\音频" -b 320

# 批量转换文件夹（含子文件夹）
python mp4_to_mp3.py "D:\视频" -r -o "D:\音频"

# 覆盖已存在的 MP3
python mp4_to_mp3.py "D:\视频" -o "D:\音频" -f

# 使用通配符
python mp4_to_mp3.py *.mp4 -o "D:\音频"
```

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `inputs` | 视频文件、文件夹或通配符，可以写多个 |
| `-o, --outdir` | 输出文件夹；默认与源文件同目录 |
| `-b, --bitrate` | MP3 码率，32–320 kbps，默认 `192k` |
| `-r, --recursive` | 递归扫描子文件夹 |
| `-f, --force` | 覆盖已经存在的 MP3 |
| `--track N` | 使用第 N 条音轨（从 0 开始），默认第一条 |
| `--gui` | 打开图形界面 |

## 输出规则

- 输出文件名与视频同名，扩展名换成 `.mp3`。
- 默认输出到源文件所在文件夹；`-o` 可指定统一目录。
- 目标 MP3 已存在时默认**跳过**，加 `-f` 覆盖。
- 同一批文件输出重名时（例如不同子文件夹里有同名视频），会自动加 `(2)`、`(3)`。
- 视频没有音频轨道时会提示“文件里没有找到音频轨道”，不会生成空文件。
- 转换时先写临时 `.part` 文件，成功后才改成正式文件名，中断不会留下半成品。
- 视频里的标题、艺术家等元数据会尽量写入 MP3 的 ID3 标签。

## 常见问题

**提示找不到 ffmpeg / ffprobe？**

确认 `ffmpeg -version` 能在 PowerShell 里运行。如果不行，重新打开终端让 PATH 生效，
或把 ffmpeg 的 `bin` 目录放到 `C:\Program Files\ffmpeg\bin`，或设置 `FFMPEG_HOME`。

**转换失败，提示没有音频轨道？**

该 MP4 本身是无声视频（例如屏幕录制未开麦克风）。可以用播放器确认一下。

**码率选多少？**

- 128 kbps：语音、播客，文件小
- 192 kbps：默认，音乐和日常视频足够
- 256 / 320 kbps：对音质要求高，文件更大

**支持其他视频格式吗？**

以 MP4 为主，同时兼容 m4v、mov、mkv、avi、webm、flv、wmv、ts 等常见容器。
只要 ffmpeg 能解码出音频即可。

**转换很慢？**

转换速度主要取决于视频时长和电脑性能；音频提取通常比视频转码快很多。
