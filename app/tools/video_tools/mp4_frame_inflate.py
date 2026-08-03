#!/usr/bin/env python3
"""
mp4_frame_inflate.py

Reproduces the "itzcrih"-style MP4 sample-table inflation trick: appends N
extra zero-cost sample-table entries (all pointing at a single reused
padding box) to a video track's stbl. This inflates the container's
reported sample/frame count (nb_frames, and derived average frame rate)
WITHOUT adding any real video data, re-encoding anything, or changing
what's actually visible on playback.

Mechanism (reverse-engineered from a real sample of this trick):
  - stsz (sample sizes)   : N new entries, each = 8 bytes
  - stco/co64 (chunk offs): N new entries, each = the SAME byte offset
                            (a tiny 'free' padding box appended at EOF)
  - stsc (sample->chunk)  : 1 new entry describing N new one-sample chunks
  - stts (time->sample)   : 1 new entry: N samples at the existing delta

Correctly handles BOTH common mp4 layouts:
  - "faststart" (moov before mdat): growing moov shifts mdat forward, so
    every existing chunk-offset entry in EVERY track (video, audio, ...)
    is bumped by the exact number of bytes inserted into moov.
  - moov-after-mdat: no shift needed since nothing before mdat changes.

No bytes of the original mdat (real video/audio data) are ever touched.

Usage:
    python3 mp4_frame_inflate.py input.mp4 output.mp4 [--multiplier 9]

WARNING: The resulting file contains phantom samples that most decoders
skip, or throw harmless "invalid NAL unit" errors on, past the real
content. This does not improve, restore, or add video quality/frames --
it only changes container metadata. Whether any given platform's ingest
pipeline reacts to this at all is unverified, can change at any time, and
using it may violate that platform's terms of service. Use on your own
files, at your own risk.
"""
import struct
import argparse


def read_box_header(data, off):
    size = struct.unpack('>I', data[off:off + 4])[0]
    btype = data[off + 4:off + 8]
    hdr_len = 8
    if size == 1:
        size = struct.unpack('>Q', data[off + 8:off + 16])[0]
        hdr_len = 16
    elif size == 0:
        size = len(data) - off
    return size, btype, hdr_len


def split_children(data):
    children = []
    off, n = 0, len(data)
    while off < n:
        size, btype, _ = read_box_header(data, off)
        children.append((btype, data[off:off + size]))
        off += size
    return children


def get_box_body(box_bytes):
    size, _btype, hdr_len = read_box_header(box_bytes, 0)
    return box_bytes[hdr_len:size]


def rebuild_box(btype, body):
    size = 8 + len(body)
    if size < 2 ** 32:
        return struct.pack('>I', size) + btype + body
    return struct.pack('>I', 1) + btype + struct.pack('>Q', size + 8) + body


def find_child(children, btype):
    for i, (t, _b) in enumerate(children):
        if t == btype:
            return i
    return -1


def parse_stsz(body):
    version_flags = body[0:4]
    sample_size, sample_count = struct.unpack('>II', body[4:12])
    sizes = None
    if sample_size == 0:
        sizes = list(struct.unpack('>%dI' % sample_count, body[12:12 + 4 * sample_count]))
    return version_flags, sample_size, sizes, sample_count


def build_stsz(version_flags, sizes):
    body = version_flags + struct.pack('>II', 0, len(sizes))
    body += struct.pack('>%dI' % len(sizes), *sizes)
    return body


def parse_stco(body):
    version_flags = body[0:4]
    entry_count = struct.unpack('>I', body[4:8])[0]
    offsets = list(struct.unpack('>%dI' % entry_count, body[8:8 + 4 * entry_count]))
    return version_flags, offsets


def build_stco(version_flags, offsets):
    body = version_flags + struct.pack('>I', len(offsets))
    body += struct.pack('>%dI' % len(offsets), *offsets)
    return body


def parse_co64(body):
    version_flags = body[0:4]
    entry_count = struct.unpack('>I', body[4:8])[0]
    offsets = list(struct.unpack('>%dQ' % entry_count, body[8:8 + 8 * entry_count]))
    return version_flags, offsets


def build_co64(version_flags, offsets):
    body = version_flags + struct.pack('>I', len(offsets))
    body += struct.pack('>%dQ' % len(offsets), *offsets)
    return body


def parse_stsc(body):
    version_flags = body[0:4]
    entry_count = struct.unpack('>I', body[4:8])[0]
    entries, off = [], 8
    for _ in range(entry_count):
        fc, spc, sdi = struct.unpack('>III', body[off:off + 12])
        entries.append((fc, spc, sdi))
        off += 12
    return version_flags, entries


def build_stsc(version_flags, entries):
    body = version_flags + struct.pack('>I', len(entries))
    for fc, spc, sdi in entries:
        body += struct.pack('>III', fc, spc, sdi)
    return body


def parse_stts(body):
    version_flags = body[0:4]
    entry_count = struct.unpack('>I', body[4:8])[0]
    entries, off = [], 8
    for _ in range(entry_count):
        cnt, delta = struct.unpack('>II', body[off:off + 8])
        entries.append((cnt, delta))
        off += 8
    return version_flags, entries


def build_stts(version_flags, entries):
    body = version_flags + struct.pack('>I', len(entries))
    for cnt, delta in entries:
        body += struct.pack('>II', cnt, delta)
    return body


def get_stbl(trak_box):
    trak_children = split_children(get_box_body(trak_box))
    mdia_i = find_child(trak_children, b'mdia')
    mdia_children = split_children(get_box_body(trak_children[mdia_i][1]))
    minf_i = find_child(mdia_children, b'minf')
    minf_children = split_children(get_box_body(mdia_children[minf_i][1]))
    stbl_i = find_child(minf_children, b'stbl')
    stbl_children = split_children(get_box_body(minf_children[stbl_i][1]))
    return trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children


def put_stbl(trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children):
    stbl_box = rebuild_box(b'stbl', b''.join(b for _, b in stbl_children))
    minf_children[stbl_i] = (b'stbl', stbl_box)
    minf_box = rebuild_box(b'minf', b''.join(b for _, b in minf_children))
    mdia_children[minf_i] = (b'minf', minf_box)
    mdia_box = rebuild_box(b'mdia', b''.join(b for _, b in mdia_children))
    trak_children[mdia_i] = (b'mdia', mdia_box)
    return rebuild_box(b'trak', b''.join(b for _, b in trak_children))


def shift_track_offsets(trak_box, delta):
    """Add `delta` to every existing chunk-offset entry in this track (any
    handler type). Used when moov grows and sits before mdat, so mdat's
    absolute file position moves forward by `delta` bytes."""
    parts = get_stbl(trak_box)
    trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children = parts
    stco_i = find_child(stbl_children, b'stco')
    co64_i = find_child(stbl_children, b'co64')
    if stco_i >= 0:
        vf, offs = parse_stco(get_box_body(stbl_children[stco_i][1]))
        offs = [o + delta for o in offs]
        stbl_children[stco_i] = (b'stco', rebuild_box(b'stco', build_stco(vf, offs)))
    elif co64_i >= 0:
        vf, offs = parse_co64(get_box_body(stbl_children[co64_i][1]))
        offs = [o + delta for o in offs]
        stbl_children[co64_i] = (b'co64', rebuild_box(b'co64', build_co64(vf, offs)))
    return put_stbl(trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children)


def find_video_trak(moov_children):
    for i, (t, box) in enumerate(moov_children):
        if t != b'trak':
            continue
        trak_children = split_children(get_box_body(box))
        mdia_i = find_child(trak_children, b'mdia')
        if mdia_i < 0:
            continue
        mdia_children = split_children(get_box_body(trak_children[mdia_i][1]))
        hdlr_i = find_child(mdia_children, b'hdlr')
        if hdlr_i < 0:
            continue
        hdlr_body = get_box_body(mdia_children[hdlr_i][1])
        if hdlr_body[8:12] == b'vide':
            return i
    return -1


def process(in_path, out_path, multiplier, fake_size=8):
    with open(in_path, 'rb') as f:
        data = f.read()

    top = split_children(data)
    moov_i = find_child(top, b'moov')
    mdat_i = find_child(top, b'mdat')
    if moov_i < 0:
        raise SystemExit("No moov box found")
    if mdat_i < 0:
        raise SystemExit("No mdat box found")

    # do we need to shift existing chunk offsets? only if moov precedes mdat
    moov_offset = sum(len(b) for _, b in top[:moov_i])
    mdat_offset = sum(len(b) for _, b in top[:mdat_i])
    faststart = moov_offset < mdat_offset

    moov_children = split_children(get_box_body(top[moov_i][1]))
    trak_i = find_video_trak(moov_children)
    if trak_i < 0:
        raise SystemExit("No video track found")

    parts = get_stbl(moov_children[trak_i][1])
    trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children = parts

    stsz_i = find_child(stbl_children, b'stsz')
    stsc_i = find_child(stbl_children, b'stsc')
    stts_i = find_child(stbl_children, b'stts')
    stco_i = find_child(stbl_children, b'stco')
    co64_i = find_child(stbl_children, b'co64')
    use_co64 = co64_i >= 0

    vf, sample_size, sizes, R = parse_stsz(get_box_body(stbl_children[stsz_i][1]))
    if sizes is None:
        sizes = [sample_size] * R

    if use_co64:
        co_vf, offsets = parse_co64(get_box_body(stbl_children[co64_i][1]))
    else:
        co_vf, offsets = parse_stco(get_box_body(stbl_children[stco_i][1]))
    orig_chunk_count = len(offsets)

    sc_vf, sc_entries = parse_stsc(get_box_body(stbl_children[stsc_i][1]))
    tt_vf, tt_entries = parse_stts(get_box_body(stbl_children[stts_i][1]))

    N = R * multiplier
    delta_pts = tt_entries[-1][1] if tt_entries else 1
    last_sdi = sc_entries[-1][2] if sc_entries else 1

    # bytes that will be inserted into moov by this whole operation
    grow = 4 * N                                  # stsz
    grow += (8 if use_co64 else 4) * N             # stco/co64
    grow += 12                                     # 1 new stsc entry
    grow += 8                                      # 1 new stts entry
    shift = grow if faststart else 0

    # if moov precedes mdat, EVERY track's existing offsets must move by `shift`
    if shift:
        for i, (t, box) in enumerate(moov_children):
            if t == b'trak':
                moov_children[i] = (b'trak', shift_track_offsets(box, shift))
        # re-fetch the (now-shifted) video stbl pieces
        parts = get_stbl(moov_children[trak_i][1])
        trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children = parts
        stsz_i = find_child(stbl_children, b'stsz')
        stsc_i = find_child(stbl_children, b'stsc')
        stts_i = find_child(stbl_children, b'stts')
        stco_i = find_child(stbl_children, b'stco')
        co64_i = find_child(stbl_children, b'co64')
        if use_co64:
            co_vf, offsets = parse_co64(get_box_body(stbl_children[co64_i][1]))
        else:
            co_vf, offsets = parse_stco(get_box_body(stbl_children[stco_i][1]))

    # the new trailing pad box lands right after the (now correctly sized) file
    pad_offset = len(data) + shift
    pad_box = struct.pack('>I', 32) + b'free' + b'\x00' * 24

    new_sizes = sizes + [fake_size] * N
    new_offsets = offsets + [pad_offset] * N
    new_sc_entries = sc_entries + [(orig_chunk_count + 1, 1, last_sdi)]
    new_tt_entries = tt_entries + [(N, delta_pts)]

    stbl_children[stsz_i] = (b'stsz', rebuild_box(b'stsz', build_stsz(vf, new_sizes)))
    stbl_children[stsc_i] = (b'stsc', rebuild_box(b'stsc', build_stsc(sc_vf, new_sc_entries)))
    stbl_children[stts_i] = (b'stts', rebuild_box(b'stts', build_stts(tt_vf, new_tt_entries)))
    if use_co64:
        stbl_children[co64_i] = (b'co64', rebuild_box(b'co64', build_co64(co_vf, new_offsets)))
    else:
        stbl_children[stco_i] = (b'stco', rebuild_box(b'stco', build_stco(co_vf, new_offsets)))

    trak_box = put_stbl(trak_children, mdia_i, mdia_children, minf_i, minf_children, stbl_i, stbl_children)
    moov_children[trak_i] = (b'trak', trak_box)
    moov_box = rebuild_box(b'moov', b''.join(b for _, b in moov_children))
    top[moov_i] = (b'moov', moov_box)

    file_final = b''.join(b for _, b in top) + pad_box

    with open(out_path, 'wb') as f:
        f.write(file_final)

    print(f"layout: {'faststart (moov before mdat)' if faststart else 'moov after mdat'}")
    print(f"real samples:        {R}")
    print(f"fake samples added:  {N} (size={fake_size}B each, offset={pad_offset})")
    print(f"reported sample count now: {R + N}")
    print(f"output size: {len(file_final):,} bytes (input was {len(data):,})")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input')
    ap.add_argument('output')
    ap.add_argument('--multiplier', type=int, default=9,
                     help='fake samples added = real_count * multiplier (default 9 -> 10x total)')
    args = ap.parse_args()
    process(args.input, args.output, args.multiplier)
