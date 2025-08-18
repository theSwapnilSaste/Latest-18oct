import os
import re
import time
import mmap
import datetime
import aiohttp
import aiofiles
import asyncio
import logging
import requests
import tgcrypto
import subprocess
import concurrent.futures
from math import ceil
from utils import progress_bar
from pyrogram import Client, filters
from pyrogram.types import Message
from io import BytesIO
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from base64 import b64decode

# =============================================================================
# Tunables (can also be overridden with env vars)
# =============================================================================
PARALLEL_CONNECTIONS = int(os.getenv("DL_CONNECTIONS", 8))   # simultaneous HTTP range requests
PART_SIZE_MB          = int(os.getenv("DL_PART_MB", 4))      # each part size in MB (range length)
PART_SIZE             = 1024 * 1024 * PART_SIZE_MB

# ⚡ Speed Patch for Pyrogram uploads
try:
    import pyrogram
    # Force upload chunk size to 4 MB (default in pyrogram is 256 KB)
    if hasattr(pyrogram.client, "DEFAULT_CHUNK_SIZE"):
        pyrogram.client.DEFAULT_CHUNK_SIZE = 1024 * 1024 * 4
        print(f"[Patch] Pyrogram upload chunk size set to {pyrogram.client.DEFAULT_CHUNK_SIZE // 1024} KB")
except Exception as e:
    print(f"[Patch] Could not patch Pyrogram chunk size: {e}")


# =============================================================================
# Utilities
# =============================================================================

def duration(filename):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", filename],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    return float(result.stdout)


def get_mps_and_keys(api_url):
    response = requests.get(api_url)
    response_json = response.json()
    mpd = response_json.get('MPD')
    keys = response_json.get('KEYS')
    return mpd, keys


def exec(cmd):
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = process.stdout.decode()
    print(output)
    return output


def pull_run(work, cmds):
    with concurrent.futures.ThreadPoolExecutor(max_workers=work) as executor:
        print("Waiting for tasks to complete")
        _ = executor.map(exec, cmds)


async def aio(url, name):
    k = f'{name}.pdf'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                f = await aiofiles.open(k, mode='wb')
                await f.write(await resp.read())
                await f.close()
                return k


async def download(url, name):
    ka = f'{name}.pdf'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                f = await aiofiles.open(ka, mode='wb')
                await f.write(await resp.read())
                await f.close()
                return ka


# =============================================================================
# High-speed parallel range downloader
# Falls back to single-stream if server doesn't support ranges.
# =============================================================================
async def _single_stream_download(url: str, file_name: str, chunk_size: int = PART_SIZE):
    if os.path.exists(file_name):
        os.remove(file_name)
    r = requests.get(url, allow_redirects=True, stream=True, timeout=(10, 300))
    with open(file_name, 'wb') as fd:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                fd.write(chunk)
    return file_name


async def _parallel_range_download(url: str, file_name: str,
                                   connections: int = PARALLEL_CONNECTIONS,
                                   part_size: int = PART_SIZE):
    # discover file size & range support
    async with aiohttp.ClientSession() as session:
        try:
            async with session.head(url) as head:
                size = int(head.headers.get("Content-Length", "0"))
                accept_ranges = head.headers.get("Accept-Ranges", "")
        except Exception:
            size, accept_ranges = 0, ""

        # If HEAD didn't help, try a small GET
        if size == 0:
            async with session.get(url) as resp:
                size = int(resp.headers.get("Content-Length", "0"))
                accept_ranges = resp.headers.get("Accept-Ranges", accept_ranges)

        # Fallback if no size or no byte ranges
        if size <= 0 or "bytes" not in accept_ranges.lower():
            return await _single_stream_download(url, file_name, chunk_size=part_size)

        # Pre-create file with total size
        if os.path.exists(file_name):
            os.remove(file_name)
        with open(file_name, "wb") as f:
            f.truncate(size)

        # Build ranges
        ranges = [(start, min(start + part_size - 1, size - 1))
                  for start in range(0, size, part_size)]

        sem = asyncio.Semaphore(connections)

        async def fetch_and_write(idx: int, start: int, end: int):
            headers = {"Range": f"bytes={start}-{end}"}
            async with sem:
                async with session.get(url, headers=headers) as resp:
                    # write directly to correct offset
                    offset = start
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        if not chunk:
                            continue
                        # each task opens its own handle & writes to offset
                        with open(file_name, "r+b") as fp:
                            fp.seek(offset)
                            fp.write(chunk)
                        offset += len(chunk)

        await asyncio.gather(*[
            fetch_and_write(i, s, e) for i, (s, e) in enumerate(ranges)
        ])

    return file_name


# Public wrappers used by the rest of the code (names unchanged)
async def pdf_download(url, file_name, chunk_size: int = PART_SIZE):
    return await _parallel_range_download(url, file_name, PARALLEL_CONNECTIONS, PART_SIZE)


def parse_vid_info(info):
    info = info.strip().split("\n")
    new_info = []
    temp = []
    for i in info:
        i = str(i)
        if "[" not in i and '---' not in i:
            while "  " in i:
                i = i.replace("  ", " ")
            i.strip()
            i = i.split("|")[0].split(" ", 2)
            try:
                if "RESOLUTION" not in i[2] and i[2] not in temp and "audio" not in i[2]:
                    temp.append(i[2])
                    new_info.append((i[0], i[2]))
            except Exception:
                pass
    return new_info


def vid_info(info):
    info = info.strip().split("\n")
    new_info = dict()
    temp = []
    for i in info:
        i = str(i)
        if "[" not in i and '---' not in i:
            while "  " in i:
                i = i.replace("  ", " ")
            i.strip()
            i = i.split("|")[0].split(" ", 3)
            try:
                if "RESOLUTION" not in i[2] and i[2] not in temp and "audio" not in i[2]:
                    temp.append(i[2])
                    new_info.update({f'{i[2]}': f'{i[0]}'})
            except Exception:
                pass
    return new_info


async def decrypt_and_merge_video(mpd_url, keys_string, output_path, output_name, quality="720"):
    try:
        output_path = Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)

        cmd1 = (
            f'yt-dlp -f "bv[height<={quality}]+ba/b" '
            f'-o "{output_path}/file.%(ext)s" '
            f'--allow-unplayable-format --no-check-certificate '
            f'--external-downloader aria2c "{mpd_url}"'
        )
        print(f"Running command: {cmd1}")
        os.system(cmd1)

        avDir = list(output_path.iterdir())
        print(f"Downloaded files: {avDir}")
        print("Decrypting")

        video_decrypted = False
        audio_decrypted = False

        for data in avDir:
            if data.suffix == ".mp4" and not video_decrypted:
                cmd2 = f'mp4decrypt {keys_string} --show-progress "{data}" "{output_path}/video.mp4"'
                print(f"Running command: {cmd2}")
                os.system(cmd2)
                if (output_path / "video.mp4").exists():
                    video_decrypted = True
                data.unlink()
            elif data.suffix == ".m4a" and not audio_decrypted:
                cmd3 = f'mp4decrypt {keys_string} --show-progress "{data}" "{output_path}/audio.m4a"'
                print(f"Running command: {cmd3}")
                os.system(cmd3)
                if (output_path / "audio.m4a").exists():
                    audio_decrypted = True
                data.unlink()

        if not video_decrypted or not audio_decrypted:
            raise FileNotFoundError("Decryption failed: video or audio file not found.")

        cmd4 = f'ffmpeg -i "{output_path}/video.mp4" -i "{output_path}/audio.m4a" -c copy "{output_path}/{output_name}.mp4"'
        print(f"Running command: {cmd4}")
        os.system(cmd4)

        if (output_path / "video.mp4").exists():
            (output_path / "video.mp4").unlink()
        if (output_path / "audio.m4a").exists():
            (output_path / "audio.m4a").unlink()

        filename = output_path / f"{output_name}.mp4"
        if not filename.exists():
            raise FileNotFoundError("Merged video file not found.")

        cmd5 = f'ffmpeg -i "{filename}" 2>&1 | grep "Duration"'
        duration_info = os.popen(cmd5).read()
        print(f"Duration info: {duration_info}")

        return str(filename)
    except Exception as e:
        print(f"Error during decryption and merging: {str(e)}")
        raise


async def run(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    print(f'[{cmd!r} exited with {proc.returncode}]')
    if proc.returncode == 1:
        return False
    if stdout:
        return f'[stdout]\n{stdout.decode()}'
    if stderr:
        return f'[stderr]\n{stderr.decode()}'


# Legacy wrapper kept for compatibility; now uses the fast parallel downloader
def old_download(url, file_name, chunk_size: int = PART_SIZE):
    # Run the async downloader from a sync context if needed
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # schedule a task the caller can await; for strict sync callers, fallback
        return asyncio.ensure_future(_parallel_range_download(url, file_name, PARALLEL_CONNECTIONS, PART_SIZE))
    else:
        return asyncio.run(_parallel_range_download(url, file_name, PARALLEL_CONNECTIONS, PART_SIZE))


def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if size < 1024.0 or unit == 'PB':
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"


def time_name():
    date = datetime.date.today()
    now = datetime.datetime.now()
    current_time = now.strftime("%H%M%S")
    return f"{date} {current_time}.mp4"


async def download_video(url, cmd, name):
    download_cmd = (
        f'{cmd} -R 25 --fragment-retries 25 '
        f'--external-downloader aria2c '
        f'--downloader-args "aria2c: -x 16 -j 32"'
    )
    global failed_counter
    print(download_cmd)
    logging.info(download_cmd)
    k = subprocess.run(download_cmd, shell=True)

    if "visionias" in cmd and k.returncode != 0 and failed_counter <= 10:
        failed_counter += 1
        await asyncio.sleep(5)
        await download_video(url, cmd, name)
    failed_counter = 0

    try:
        if os.path.isfile(name):
            return name
        elif os.path.isfile(f"{name}.webm"):
            return f"{name}.webm"
        name = name.split(".")[0]
        if os.path.isfile(f"{name}.mkv"):
            return f"{name}.mkv"
        elif os.path.isfile(f"{name}.mp4"):
            return f"{name}.mp4"
        elif os.path.isfile(f"{name}.mp4.webm"):
            return f"{name}.mp4.webm"
        return name
    except FileNotFoundError:
        return os.path.isfile.splitext[0] + "." + "mp4"


# =============================================================================
# Senders
# =============================================================================
async def send_doc(bot: Client, m: Message, cc, ka, cc1, prog, count, name, channel_id):
    reply = await bot.send_message(channel_id, f"Downloading pdf:\n`{name}`")
    await asyncio.sleep(0.2)
    start_time = time.time()
    await bot.send_document(ka, caption=cc1)
    count += 1
    await reply.delete(True)
    await asyncio.sleep(0.2)
    os.remove(ka)


def decrypt_file(file_path, key):
    if not os.path.exists(file_path):
        return False
    with open(file_path, "r+b") as f:
        num_bytes = min(28, os.path.getsize(file_path))
        with mmap.mmap(f.fileno(), length=num_bytes, access=mmap.ACCESS_WRITE) as mmapped_file:
            for i in range(num_bytes):
                mmapped_file[i] ^= ord(key[i]) if i < len(key) else i
    return True


async def download_and_decrypt_video(url, cmd, name, key):
    video_path = await download_video(url, cmd, name)
    if video_path:
        decrypted = decrypt_file(video_path, key)
        if decrypted:
            print(f"File {video_path} decrypted successfully.")
            return video_path
        else:
            print(f"Failed to decrypt {video_path}.")
            return None


async def send_vid(bot: Client, m: Message, cc, filename, thumb, name, prog, channel_id):
    subprocess.run(f'ffmpeg -i "{filename}" -ss 00:00:10 -vframes 1 "{filename}.jpg"', shell=True)
    await prog.delete(True)
    reply1 = await bot.send_message(channel_id, f"**📩 Uploading Video 📩:-**\n**{name}**")
    reply = await m.reply_text(f"**Generate Thumbnail:**\n**{name}**")
    try:
        if thumb == "/d":
            thumbnail = f"{filename}.jpg"
        else:
            thumbnail = thumb
    except Exception as e:
        await m.reply_text(str(e))

    dur = int(duration(filename))
    start_time = time.time()

    try:
        await bot.send_video(
            channel_id,
            filename,
            caption=cc,
            supports_streaming=True,
            height=720,
            width=1280,
            thumb=thumbnail,
            duration=dur,
            progress=progress_bar,
            progress_args=(reply, start_time)
        )
    except Exception:
        await bot.send_document(
            channel_id,
            filename,
            caption=cc,
            progress=progress_bar,
            progress_args=(reply, start_time)
        )

    os.remove(filename)
    await reply.delete(True)
    await reply1.delete(True)
    os.remove(f"{filename}.jpg")
