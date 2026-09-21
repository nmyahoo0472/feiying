"""成品 mp4 里的坏块定位 + 无损剪掉。

TG 偶尔某个文件的某一块永远下不下来(存那块的节点坏了,换 bot 重发同一文件也一样),
带洞转封装后那 4MB 对应的样本在成品里是全零——压缩过的视频/音频里不可能出现整段零
(HEVC/H.264 有防竞争字节,AAC 也不会),所以逐样本看零就能精确定位到坏的样本(和时间)。
再用 moov 的样本表把字节换算成时间,ffmpeg -c copy 切成「坏段之前」+「坏段之后第一个关键帧起」两段拼回去,
观感是跳过几秒,而不是花屏几秒。全程不重编码。

用法:python -m app.holecut 文件.mp4 [--dry]
"""
import os, struct, subprocess, sys, tempfile
from . import mp4probe

def _read_moov(path):
    """faststart 后 moov 在文件头附近;顺着顶层 box 找到它整个读出来(带 box 头)。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        off = 0
        while off + 8 <= size:
            f.seek(off)
            hdr = f.read(16)
            bsize = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8]
            if bsize == 1:
                bsize = struct.unpack(">Q", hdr[8:16])[0]
            elif bsize == 0:
                bsize = size - off
            if typ == b"moov":
                f.seek(off)
                return f.read(bsize)
            if bsize < 8:
                break
            off += bsize
    return None


def _s32(x):
    return x - (1 << 32) if x >= (1 << 31) else x


def _track_samples(buf, tb, te):
    """一条 trak 的全部样本:[(offset, size, dts, pts, keyframe)],加 kind/timescale。"""
    mdia = mp4probe._find(buf, tb, te, b"mdia")
    if not mdia:
        return None
    kind = mp4probe._handler(buf, *mdia)
    mdhd = mp4probe._find(buf, mdia[0], mdia[1], b"mdhd")
    if not kind or not mdhd:
        return None
    ver = buf[mdhd[0]]
    timescale = struct.unpack(">I", buf[mdhd[0] + (20 if ver == 1 else 12):
                                        mdhd[0] + (24 if ver == 1 else 16)])[0]
    minf = mp4probe._find(buf, mdia[0], mdia[1], b"minf")
    stbl = mp4probe._find(buf, minf[0], minf[1], b"stbl") if minf else None
    if not stbl or not timescale:
        return None
    sb, se = stbl
    f = lambda name: mp4probe._find(buf, sb, se, name)
    stts, stsc, stsz, stss, ctts = f(b"stts"), f(b"stsc"), f(b"stsz"), f(b"stss"), f(b"ctts")
    stco = mp4probe._chunk_offsets(buf, sb, se)
    if not (stts and stsc and stsz and stco):
        return None
    # 样本大小
    fixed = struct.unpack(">I", buf[stsz[0] + 4:stsz[0] + 8])[0]
    count = struct.unpack(">I", buf[stsz[0] + 8:stsz[0] + 12])[0]
    if fixed:
        sizes = [fixed] * count
    else:
        p = stsz[0] + 12
        count = min(count, max(0, (stsz[1] - p) // 4))
        sizes = list(struct.unpack(">%dI" % count, buf[p:p + 4 * count]))
    # 每块几个样本 → 每个样本的偏移
    stsc_e = [(x[0], x[1]) for x in mp4probe._u32s(buf, *stsc, per=3)]
    offsets, si, k, per = [], 0, 0, 0
    for ci, coff in enumerate(stco, start=1):
        while k < len(stsc_e) and stsc_e[k][0] <= ci:      # 交错好的文件 stsc 条目能有几万条,别每块从头扫
            per = stsc_e[k][1]
            k += 1
        o = coff
        for _ in range(per):
            if si >= len(sizes):
                break
            offsets.append(o)
            o += sizes[si]
            si += 1
    n = min(len(offsets), len(sizes))
    # 时间
    dts, t = [], 0
    for cnt, delta in mp4probe._u32s(buf, *stts, per=2):
        for _ in range(cnt):
            if len(dts) >= n:
                break
            dts.append(t)
            t += delta
    while len(dts) < n:
        dts.append(t)
    cts = [0] * n
    if ctts:
        i = 0
        for cnt, off in mp4probe._u32s(buf, *ctts, per=2):
            for _ in range(cnt):
                if i >= n:
                    break
                cts[i] = _s32(off)
                i += 1
    keys = None
    if stss:
        keys = set(x[0] for x in mp4probe._u32s(buf, *stss, per=1))
    samples = [(offsets[i], sizes[i], dts[i], dts[i] + cts[i], (keys is None) or ((i + 1) in keys))
               for i in range(n)]
    return {"kind": kind.decode("latin1", "ignore"), "timescale": timescale, "samples": samples}


def tracks(moov_buf):
    s, e = mp4probe._moov_body(moov_buf)
    out = []
    for typ, b, en in mp4probe._boxes(moov_buf, s, e):
        if typ == b"trak":
            t = _track_samples(moov_buf, b, en)
            if t and t["samples"]:
                out.append(t)
    return out


def bad_intervals(path, zero_ratio=0.6, merge_gap=2.0):
    """逐个样本读出来看是不是全零,返回按时间合并后的坏区间 [(起, 止)](秒)。
    成品是交错好的,坏样本和好音频包混在一起,整块扫零扫不到,必须按样本看。1.4GB 要读一遍,几十秒。"""
    moov = _read_moov(path)
    if not moov:
        raise RuntimeError("找不到 moov")
    trs = tracks(moov)
    if not any(t["kind"] == "vide" for t in trs):
        raise RuntimeError("没有视频轨")
    spans = []
    with open(path, "rb") as f:
        for tr in trs:
            ts = tr["timescale"]
            for off, sz, dts, pts, key in tr["samples"]:
                if sz <= 0:
                    continue
                f.seek(off)
                b = f.read(sz)
                if b.count(0) / sz >= zero_ratio:
                    a, e = min(dts, pts) / ts, (max(dts, pts) + 1) / ts
                    spans.append((a, e))
    if not spans:
        return [], trs
    spans.sort()
    merged = [list(spans[0])]
    for a, e in spans[1:]:
        if a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([a, e])
    return [(a, e) for a, e in merged], trs


def plan(path):
    """算出要剪掉的时间段列表。返回 ([(t1, t2)], 说明) 或 None(没坏块)。
    每段 t1 = 坏区起点稍前;t2 = 坏区之后视频第一个关键帧的 pts(-1 表示到片尾)。"""
    bad, trs = bad_intervals(path)
    if not bad:
        return None
    vid = next(t for t in trs if t["kind"] == "vide")
    ts = vid["timescale"]
    keyframes = [(dts / ts, pts / ts) for off, sz, dts, pts, key in vid["samples"] if key]
    cuts = []
    for a, e in bad:
        t2 = next((kp for kd, kp in keyframes if kd > e), -1)
        t1 = max(0.0, a - 0.05)
        if cuts and t1 <= cuts[-1][1]:            # 和上一段重叠就并起来
            cuts[-1] = (cuts[-1][0], t2)
        else:
            cuts.append((t1, t2))
    lost = sum((t2 if t2 >= 0 else e) - t1 for (t1, t2), (a, e) in zip(cuts, bad))
    desc = "坏区 %d 段(%s),剪掉 %s,共约 %.1f 秒" % (
        len(bad), ", ".join("%.1f~%.1fs" % x for x in bad),
        ", ".join("%.2f~%s" % (t1, ("%.2fs" % t2) if t2 >= 0 else "片尾") for t1, t2 in cuts), lost)
    return cuts, desc


def _run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(" ".join(cmd[:3]) + " 失败: " + p.stderr[-400:].decode("utf-8", "ignore"))


def cut(path, cuts, dst=None):
    """无损剪掉若干 [t1, t2) 段,写回 path(或 dst)。返回输出路径。"""
    dst = dst or path
    d = tempfile.mkdtemp(prefix="holecut_", dir=os.path.dirname(path) or ".")
    parts, tmpfiles = [], []
    try:
        pos = 0.0
        for i, (t1, t2) in enumerate(cuts):
            seg = os.path.join(d, "seg%d.mp4" % i)
            cmd = ["ffmpeg", "-y", "-v", "error"]
            if pos > 0:
                cmd += ["-ss", "%.3f" % (pos + 0.001)]
            cmd += ["-i", path, "-t", "%.3f" % (t1 - pos), "-c", "copy", "-avoid_negative_ts", "make_zero", seg]
            if t1 - pos > 0.5:
                _run(cmd)
                parts.append(seg)
                tmpfiles.append(seg)
            if t2 < 0:
                pos = -1
                break
            pos = t2
        if pos >= 0:
            seg = os.path.join(d, "tail.mp4")
            _run(["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % (pos + 0.001), "-i", path,
                  "-c", "copy", "-avoid_negative_ts", "make_zero", seg])
            parts.append(seg)
            tmpfiles.append(seg)
        lst = os.path.join(d, "list.txt")
        with open(lst, "w") as f:
            for p in parts:
                f.write("file '%s'\n" % p.replace("'", r"'\''"))
        tmpfiles.append(lst)
        out = os.path.join(d, "out.mp4")
        tmpfiles.append(out)
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
              "-c", "copy", "-movflags", "+faststart", out])
        if os.path.getsize(out) < os.path.getsize(path) * 0.5:
            raise RuntimeError("输出只有原来一半不到,不对劲,不覆盖")
        os.replace(out, dst)
        try:
            os.chmod(dst, 0o664)
        except OSError:
            pass
        return dst
    finally:
        for p in tmpfiles:
            try: os.remove(p)
            except OSError: pass
        try: os.rmdir(d)
        except OSError: pass


def fix(path):
    """一步到位:有坏块就剪。返回说明字符串(没坏块返回 None)。"""
    r = plan(path)
    if not r:
        return None
    cuts, desc = r
    cut(path, cuts)
    return desc


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    dry = "--dry" in sys.argv
    for p in args:
        r = plan(p)
        if not r:
            print(p, "没发现坏块")
            continue
        cuts, desc = r
        print(p, desc)
        if not dry:
            before = os.path.getsize(p)
            cut(p, cuts)
            print("已剪:%d MB → %d MB" % (before // 1048576, os.path.getsize(p) // 1048576))
