# edge/vision/verify_stream.py
import requests
import time
import struct

ESP32CAM_URL = "http://172.20.10.4/stream"
BOUNDARY     = b"gc0p4Jq0M2Yt08jU534c0p"

def verify_mjpeg_stream(url, duration_sec=10):
    print(f"Connecting to {url} ...")

    try:
        resp = requests.get(url, stream=True, timeout=10)
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    print(f"HTTP status : {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('Content-Type')}")

    if resp.status_code != 200:
        print("Stream not available")
        return

    frame_count  = 0
    total_bytes  = 0
    start_time   = time.time()
    last_report  = start_time
    in_jpeg      = False
    jpeg_buf     = b""

    print(f"\nReceiving stream for {duration_sec} seconds...\n")

    for chunk in resp.iter_content(chunk_size=4096):
        if not chunk:
            continue

        total_bytes += len(chunk)

        # 检测 JPEG 开始标志 FF D8
        if b'\xff\xd8' in chunk and not in_jpeg:
            idx      = chunk.index(b'\xff\xd8')
            jpeg_buf = chunk[idx:]
            in_jpeg  = True

        elif in_jpeg:
            jpeg_buf += chunk

            # 检测 JPEG 结束标志 FF D9
            if b'\xff\xd9' in jpeg_buf:
                end_idx  = jpeg_buf.index(b'\xff\xd9') + 2
                frame    = jpeg_buf[:end_idx]
                jpeg_buf = b""
                in_jpeg  = False

                frame_count += 1

                # 验证 JPEG 头尾完整性
                valid = (frame[:2] == b'\xff\xd8' and
                         frame[-2:] == b'\xff\xd9')

                now = time.time()
                if now - last_report >= 2.0:
                    fps = frame_count / (now - start_time)
                    bps = total_bytes / (now - start_time) / 1024
                    print(f"  Frames: {frame_count:4d} | "
                          f"FPS: {fps:.1f} | "
                          f"Bandwidth: {bps:.1f} KB/s | "
                          f"Last frame: {len(frame)} bytes | "
                          f"Valid JPEG: {valid}")
                    last_report = now

        if time.time() - start_time >= duration_sec:
            break

    elapsed   = time.time() - start_time
    avg_fps   = frame_count / elapsed if elapsed > 0 else 0
    avg_bw    = total_bytes / elapsed / 1024 if elapsed > 0 else 0

    print(f"\n{'='*50}")
    print(f"Result:")
    print(f"  Total frames   : {frame_count}")
    print(f"  Duration       : {elapsed:.1f}s")
    print(f"  Average FPS    : {avg_fps:.2f}")
    print(f"  Total received : {total_bytes/1024:.1f} KB")
    print(f"  Avg bandwidth  : {avg_bw:.1f} KB/s")
    print(f"  Stream status  : {'OK' if frame_count > 0 else 'FAILED'}")
    print(f"{'='*50}")

    resp.close()

if __name__ == "__main__":
    verify_mjpeg_stream(ESP32CAM_URL, duration_sec=15)