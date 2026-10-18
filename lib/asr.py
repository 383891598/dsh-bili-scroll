#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asr.py —— 把一条 B站 视频（或本地音视频文件）转成文字。
给"这个视频没有字幕、但又值得判断"的情况用。

用法（在插件的 ASR 环境里跑）：
    .venv-asr/Scripts/python.exe asr.py BV1xxxxxxxxx          # Windows
    .venv-asr/bin/python       asr.py https://www.bilibili.com/video/BV1xx
    .venv-asr/Scripts/python.exe asr.py D:\\some\\clip.mp4 -o out.txt
    …还可以加 --model small / --model medium 换模型（默认 small）

设计上的两个要点：
  ① **模型走国内镜像**（HF_ENDPOINT=hf-mirror.com）—— 否则 HuggingFace 拉不动
  ② **音频不落盘到别处**：yt-dlp 下到临时目录，转写完就随系统清理
"""
import argparse
import os
import re
import sys
import tempfile

# ⭐ Windows 上 Python 默认按 GBK 编/解码标准输出 —— 中文或 emoji 打进管道会直接炸
#    （有用户反馈装依赖 / 跑转写时报 UnicodeDecodeError 或乱码）⇒ 这里钉死 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ⚠️ 临时目录必须待在家目录里：值班那一轮跑在沙箱里（只允许写工作区/cwd），
#    yt-dlp 默认往 %TEMP% 写、会被直接拒掉（2026-10-14 在主人机器上实证：
#    yt_dlp DownloadError: unable to open for writing: [Errno 13] Permission denied）
_HOME = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_HOME, 'tmp', 'asr')
try:
    os.makedirs(_TMP, exist_ok=True)
    tempfile.tempdir = _TMP
except Exception:
    pass

# [本地修复 2026-10-18] 宿主 no_proxy 里若带 [::1] 这类带方括号的 IPv6 字面量，
# Python 3.14 的 httpx 在解析时会把它当成 "Invalid port: ':1]'" 直接抛 InvalidURL
# ⇒ 下载 Whisper 模型必然失败（实测 asr-install.log）。这里只摘掉带方括号的条目。
for _k in ('no_proxy', 'NO_PROXY'):
    _v = os.environ.get(_k)
    if _v:
        try:
            _kept = [p for p in _v.split(',') if '[' not in p and ']' not in p]
            os.environ[_k] = ','.join(_kept)
        except Exception:
            pass

# ⭐ 必须在 import faster_whisper **之前**设好，否则它已经去连 HuggingFace 了
os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
# 模型缓存放本脚本所在目录（= 插件家）：默认的 ~/.cache 在本机拒绝写入
os.environ.setdefault('HF_HOME', os.path.join(os.path.dirname(os.path.abspath(__file__)), '.hf-cache'))
# HuggingFace 新版存储（Xet）国内镜像不代理 ⇒ 关掉它，走老 CDN 通道（否则 401）
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')


def to_url(target: str) -> str:
    t = target.strip()
    if t.upper().startswith('BV') and len(t) <= 20:
        return 'https://www.bilibili.com/video/' + t
    return t


def fetch_audio(url: str, tmp: str, tag: str) -> str:
    """用 yt-dlp 把音频拉下来（B站 是 DASH 音视频分离，取 bestaudio 即可，不需要 ffmpeg 合并）"""
    from yt_dlp import YoutubeDL
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(tmp, tag + '.%(ext)s'),   # ⭐ 用 BV 号当文件名：固定名会和别的轮次撞车（踩过）
        'paths': {'home': tmp, 'temp': tmp},   # 别让 yt-dlp 往 %TEMP% 写（沙箱会拒）
        'cachedir': False,                      # 顺手别去动用户目录里的缓存
        # ⭐ nopart：不写 .part 再改名，直接落最终文件。
        #    沙箱里"新建文件 + 改名"会被拒，而这条能绕开（2026-10-14 值班那一轮自己试出来的）
        'nopart': True,
        # [本地修复 2026-10-18 · 审计 H2] 进度条默认打到 stdout，调用方按 stdout 取正文时
        # 拿到的前 1KB 全是 [download] xx%（实测 1046~3488 B），且「输出非空=成功」恒真。
        'noprogress': True,
        'quiet': True,
        'no_warnings': True,
        # [本地修复 · 审计 H3] 残留文件不续传、直接覆盖：否则 yt-dlp 会尝试 Range 续传并撞 HTTP 416
        'overwrites': True,
        'continuedl': False,
        'noplaylist': True,
        'http_headers': {
            'Referer': 'https://www.bilibili.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        },
    }
    # [本地修复 · 审计 H3] 上一轮被 SIGKILL/超时杀掉时 finally 不执行，会留下全尺寸音频；
    # yt-dlp 看到同名文件会去续传，直接撞 HTTP 416（实测复现）。下载前先删干净。
    try:
        import glob as _glob
        for old in _glob.glob(os.path.join(tmp, tag + '.*')):
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


def main() -> int:
    ap = argparse.ArgumentParser(description='把一条视频转成文字（默认中文）')
    ap.add_argument('target', help='BV 号 / B站链接 / 本地音视频文件')
    ap.add_argument('-o', '--out', help='写到这个文件（不给就打到标准输出）')
    ap.add_argument('--max-chars', type=int, default=20000,
                    help='正文字符上限（默认 20000，超出截断并提示；0 = 不限）')
    ap.add_argument('--model', default=os.environ.get('BILI_DUTY_ASR_MODEL', 'small'),
                    help='Whisper 模型大小：tiny / base / small / medium（默认 small）')
    ap.add_argument('--lang', default='zh', help='语言（默认 zh；不给就自动判断请传 auto）')
    args = ap.parse_args()

    src = args.target
    # ⭐ 每个视频用**自己的**文件名：以前写死 audio.m4a ⇒ 两轮同时跑会互相覆盖，
    #    上一轮转出来的其实是另一个视频的内容（2026-10-14 值班那轮亲踩 ✗）
    tag = os.path.basename(src).rsplit('.', 1)[0] or 'clip'
    tag = re.sub(r'[^0-9A-Za-z_-]', '_', tag)[:40] or 'clip'
    # [本地修复 2026-10-18 · 审计 N1/M1/M2] 原来所有轮次共用 _TMP + 以 tag 为前缀的文件名：
    #   · 并发时 B 的「下载前预删」会把 A 正在写的文件 unlink 掉（实测 A 只转了半截却 exit 0）
    #   · 不同来源可能映射出同一个 tag（clip / clip.mp4）
    # ⇒ 改成「每 tag 一个子目录 + O_EXCL 锁」。不用 mkdtemp：沙箱只允许写启动前就存在的目录。
    tmp = os.path.join(_TMP, tag)
    try:
        os.makedirs(tmp, exist_ok=True)
    except Exception:
        tmp = _TMP          # 建不了就退回老路径，别因此跑不成
    lock_path = os.path.join(tmp, tag + '.lock')
    try:
        _lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(_lock_fd)
    except FileExistsError:
        print('[asr] 同一个内容正在被另一轮转写，先跳过（exit 6）', file=sys.stderr)
        return 6
    except Exception:
        lock_path = ''      # 做不了锁就别拦着干活
    # [本地修复 · 审计 M8] 输出目录不存在要在下载/转写**之前**就报，别白跑几分钟
    if args.out:
        _od = os.path.dirname(os.path.abspath(args.out))
        if _od and not os.path.isdir(_od):
            print('[asr] 输出目录不存在：' + _od, file=sys.stderr)
            return 4
    try:
        if src.lower().startswith('http') or src.upper().startswith('BV'):
            print('[asr] 先下音频…', file=sys.stderr)
            src = fetch_audio(to_url(src), tmp, tag)
        if not os.path.exists(src):
            print('[asr] 找不到文件：' + src, file=sys.stderr)
            return 2

        from faster_whisper import WhisperModel
        print('[asr] 加载模型 %s（第一次会自动下模型，之后就是本地读）…' % args.model, file=sys.stderr)
        model = WhisperModel(args.model, device='cpu', compute_type='int8')
        print('[asr] 开始转写…', file=sys.stderr)
        segments, _info = model.transcribe(
            src,
            language=None if args.lang == 'auto' else args.lang,
            vad_filter=True,          # 掐掉静音段，省时间也少出错
        )
        text = ''.join(seg.text for seg in segments).strip()
    finally:
        # 临时音频不留在磁盘上：整目录删（每 tag 一个子目录，不会误删别人的）
        try:
            if tmp != _TMP:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                import glob as _g
                for f in _g.glob(os.path.join(tmp, tag + '.*')):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if lock_path:
                os.remove(lock_path)
        except Exception:
            pass

    # [本地修复 · 审计 H1] 原来空结果也 return 0，调用方分不清「真没语音」和「跑挂了」
    # 退出码约定：0 = 有文字；3 = 跑通了但没转出文字；4 = 环境/参数问题
    if not text:
        print('[asr] 没转出任何文字：可能是纯音乐/静音，也可能是模型或音频有问题（exit 3）', file=sys.stderr)
    limit = int(getattr(args, 'max_chars', 0) or 0)
    if limit > 0 and len(text) > limit:
        print('[asr] 正文 %d 字，超过上限 %d，已截断' % (len(text), limit), file=sys.stderr)
        text = text[:limit] + '\n…（已截断）'
    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir and not os.path.isdir(out_dir):
            print('[asr] 输出目录不存在：' + out_dir, file=sys.stderr)
            return 4
        try:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(text)
        except Exception as e:
            print('[asr] 写文件失败：%s' % e, file=sys.stderr)
            return 4
        print('[asr] 已写入：' + args.out, file=sys.stderr)
    else:
        print(text)
    return 0 if text else 3


if __name__ == '__main__':
    raise SystemExit(main())
