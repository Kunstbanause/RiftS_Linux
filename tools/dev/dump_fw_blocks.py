#!/usr/bin/env python3
"""Read Rift S HMD firmware blocks over hidraw (read-only), like Monado's rift_s_read_firmware_block.

Usage: dump_fw_blocks.py /dev/hidrawN outdir [block_id_hex ...]
Monado must not be running (it holds the device).
"""
import fcntl
import os
import struct
import sys
import time


def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def hidioc_sfeature(size):
    return _ioc(3, "H", 0x06, size)


def hidioc_gfeature(size):
    return _ioc(3, "H", 0x07, size)


def set_feature(fd, data):
    buf = bytearray(data)
    fcntl.ioctl(fd, hidioc_sfeature(len(buf)), buf)


def get_feature(fd, report_id, length=64):
    buf = bytearray(length)
    buf[0] = report_id
    fcntl.ioctl(fd, hidioc_gfeature(length), buf)
    return bytes(buf)


def read_one(fd, block_id, pos, read_len):
    req = bytearray(64)
    req[0] = 0x4A
    req[2] = block_id
    struct.pack_into("<I", req, 3, pos)
    req[7] = read_len
    send = True
    for _ in range(40):
        if send:
            set_feature(fd, req)
        buf = get_feature(fd, 0x4A)
        if buf[:7] == bytes(req[:7]):
            return buf
        send = buf[1] == 0x00
        time.sleep(0.002)
    raise IOError(f"block {block_id:#x} pos {pos:#x}: no response")


def read_block(fd, block_id):
    hdr = read_one(fd, block_id, 0, 0xC)
    block_len = struct.unpack_from("<I", hdr, 16)[0]
    if block_len < 0xC or block_len == 0xFFFFFFFF:
        raise IOError(f"block {block_id:#x}: invalid length {block_len}")
    out = bytearray()
    for pos in range(0, block_len, 56):
        n = min(56, block_len - pos)
        buf = read_one(fd, block_id, pos + 0xC, n)
        out += buf[8 : 8 + n]
    return bytes(out)


def main():
    dev, outdir = sys.argv[1], sys.argv[2]
    blocks = [int(b, 16) for b in sys.argv[3:]] or [0x12, 0x10, 0x0B, 0x0F]
    os.makedirs(outdir, exist_ok=True)
    fd = os.open(dev, os.O_RDWR)
    try:
        for b in blocks:
            try:
                data = read_block(fd, b)
            except IOError as e:
                print(e)
                continue
            is_json = data[:1] == b"{"
            path = os.path.join(outdir, f"fw_block_{b:02x}.{'json' if is_json else 'bin'}")
            with open(path, "wb") as f:
                f.write(data.rstrip(b"\0") if is_json else data)
            print(f"block {b:#04x}: {len(data)} bytes -> {path}")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
