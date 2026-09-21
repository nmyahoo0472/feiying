"""坏交错片源的预处理。

有些压制版把音频和视频各自成块地写(同一时刻能隔几百 MB)。这种片子走 .strm+HTTP
时播放器每跳一次就要重建一次 TCP,吞吐喂不饱码率,看两秒卡一下;而放本地文件毫无问题
(寻道免费)。所以入库时先只取 moov 判一下,坏的就不发 .strm,改成后台下满 + 转封装成
媒体库里的真 mp4,好一集出现一集。正常片源(实测占绝大多数)完全不走这条路。
"""
import asyncio, os, subprocess
import json
from . import state, mp4probe, library, holecut
from .config import CACHE_DIR, DATA_DIR

PENDING_PATH = os.path.join(DATA_DIR, "prepare_pending.json")   # 后台队列落盘:重启后续上,不然缓存满了也没人转封装

GAP_LIMIT = 32 * 1024 * 1024      # 同一时刻音视频超过这么远就算坏交错
_ALIGN = 4096                     # TG 取文件要求偏移按 4096 对齐
_queue = None
_worker = None
status = {}                       # (show, season) -> 给前端看的一句话


async def _fetch(msg, off, n):
    """从 TG 直接取一小段,不落缓存(判交错只要几 MB,不值得建整个稀疏文件)。"""
    base = (off // _ALIGN) * _ALIGN
    skip = off - base
    need = skip + n
    buf = bytearray()
    async for chunk in state.client.iter_download(msg, offset=base, request_size=256 * 1024):
        buf += chunk
        if len(buf) >= need:
            break
    return bytes(buf[skip:need])


async def probe(msg):
    """只取 moov 判交错,返回最大间隔字节数;判不了返回 -1。"""
    size = msg.file.size
    head = await _fetch(msg, 0, 4096)
    r = mp4probe.moov_range(head, size)
    if not r:
        return -1
    off, ln = r
    if ln is None:                                  # moov 在 mdat 后面,去那儿读头
        hb = mp4probe.box_header(await _fetch(msg, off, 16))
        if not hb or hb[0] != b"moov":
            return -1
        ln = hb[1]
    if ln > 64 * 1024 * 1024:                       # moov 大得离谱,不折腾
        return -1
    moov = head[off:off + ln] if off + ln <= len(head) else await _fetch(msg, off, ln)
    try:
        return mp4probe.interleave_gap(moov)
    except Exception as e:
        print("[prepare] 解析 moov 失败", repr(e), flush=True)
        return -1


MAX_HOLES = 3             # 一集最多容忍几个 4MB 坏块(带洞转封装,那几秒花屏);再多就放弃


async def _cache_fully(ch, mid, msg):
    """把整集下满(复用缓存层的预取)。返回 (是否可转封装, 坏块集合)。"""
    c = state.cache.get_cacher((ch, mid), msg.file.size, msg, chain=False)
    c.demand = 0
    idle = 0
    last = -1
    while c.cached_blocks() < c.nblocks:
        c.start_prefetch()                       # 预取任务万一挂了这里会重新拉起
        await asyncio.sleep(3)
        got = c.cached_blocks()
        if c.bad and got + len(c.bad) >= c.nblocks:      # 只剩 TG 给不出来的块了
            return len(c.bad) <= MAX_HOLES, set(c.bad)
        idle = idle + 1 if got == last else 0
        last = got
        if idle > 100:                           # 5 分钟一点没动:放弃
            return False, set(c.bad)
    return True, set()


async def _remux(src, dst):
    """无损转封装:重排成正常交错,顺带把 moov 挪到文件头。"""
    tmp = dst + ".part"
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-i", src, "-c", "copy",
        "-movflags", "+faststart", "-f", "mp4", tmp,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
        print("[prepare] 转封装失败", err[-300:].decode("utf-8", "ignore"), flush=True)
        try: os.remove(tmp)
        except OSError: pass
        return False
    os.replace(tmp, dst)
    try:
        os.chmod(dst, 0o664)
    except OSError:
        pass
    return True


async def _do_one(show, season, ch, ep, mid):
    """下满一集 → 转封装进媒体库 → 删掉缓存里的原始块(库里已有成品,留着白占地)。"""
    try:
        d = os.path.join(state.cfg.media_dir, show, "Season %02d" % season)
        dst = os.path.join(d, "%s - S%02dE%02d.mp4" % (show, season, ep))
        if os.path.exists(dst) and os.path.getsize(dst) > 1024 * 1024:
            # 重启/重新入库后别把已转好的集再下一遍
            print("[prepare] %s E%02d 库里已有成品,跳过" % (show, ep), flush=True)
            return True
        msg = await state.cache.get_msg(ch, mid)
        if not msg or not msg.file:
            return False
        ok, holes = await _cache_fully(ch, mid, msg)
        if not ok:
            print("[prepare] %s E%02d 下载卡住%s,跳过" % (show, ep, "(%d 个坏块)" % len(holes) if holes else ""), flush=True)
            return False
        if holes:
            print("[prepare] %s E%02d 有 %d 个 4MB 块 TG 那边一直给不出来,带洞转封装(对应几秒会花屏),块号 %s"
                  % (show, ep, len(holes), sorted(holes)), flush=True)
        src = os.path.join(CACHE_DIR, "%s_%d.bin" % (ch, mid))
        os.makedirs(d, exist_ok=True)
        if not await _remux(src, dst):
            return False
        if holes:
            # 坏块对应的样本是全零,播放器解到那会花屏/爆音;把那几秒无损剪掉,观感只是跳一下
            try:
                desc = await asyncio.to_thread(holecut.fix, dst)
                print("[prepare] %s E%02d 已剪掉坏段: %s" % (show, ep, desc), flush=True)
            except Exception as e:
                print("[prepare] %s E%02d 剪坏段失败(先留着带洞的版本) %r" % (show, ep, e), flush=True)
        strm_path = dst[:-4] + ".strm"
        if os.path.exists(strm_path):
            try: os.remove(strm_path)          # 同一集别在库里出现两次
            except OSError: pass
        for p in (src, src[:-4] + ".bm"):
            try: os.remove(p)
            except OSError: pass
        state.cache.cachers.pop((ch, mid), None)
        print("[prepare] %s E%02d 已重新封装入库" % (show, ep), flush=True)
        return True
    except Exception as e:
        print("[prepare] %s E%02d 预处理出错 %r" % (show, ep, e), flush=True)
        return False


_pending = {}      # {"show|season": {"show","season","channel","eps":[{ep,mid}]}} 还没做完的


def _pending_load():
    try:
        return json.load(open(PENDING_PATH, encoding="utf-8"))
    except Exception:
        return {}


def _pending_save():
    try:
        tmp = PENDING_PATH + ".tmp"
        json.dump(_pending, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, PENDING_PATH)
    except OSError:
        pass


def _pending_key(show, season):
    return "%s|%d" % (show, season)


def _pending_drop_ep(show, season, ep):
    k = _pending_key(show, season)
    it = _pending.get(k)
    if not it:
        return
    it["eps"] = [e for e in it["eps"] if e["ep"] != ep]
    if not it["eps"]:
        _pending.pop(k, None)
    _pending_save()


async def _run():
    while True:
        show, season, ch, eps = await _queue.get()
        key = (show, season)
        done = 0
        for i, e in enumerate(eps):
            status[key] = "正在准备第 %d/%d 集(这版片源要重新封装才不卡)" % (i + 1, len(eps))
            if await _do_one(show, season, ch, e["ep"], e["mid"]):
                done += 1
                _pending_drop_ep(show, season, e["ep"])
        status[key] = "已准备好 %d/%d 集" % (done, len(eps))
        print("[prepare] 《%s》完成 %d/%d 集" % (show, done, len(eps)), flush=True)
        if done == len(eps):
            _pending.pop(_pending_key(show, season), None)      # 全成了才算了结;失败的留着下次启动再试
            _pending_save()


def enqueue(show, season, channel, episodes, start_ep=1, persist=True):
    """排队后台准备。默认第一集排最前,她最可能先点它;
    追到一半重新入库时传 start_ep,从那集起优先,前面没转的排最后补。"""
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_run())
    eps = sorted(episodes, key=lambda e: (e["ep"] < start_ep, e["ep"]))
    status[(show, season)] = "排队中(%d 集)" % len(eps)
    if persist:
        _pending[_pending_key(show, season)] = {"show": show, "season": season, "channel": channel,
                                                 "eps": [{"ep": e["ep"], "mid": e["mid"]} for e in eps]}
        _pending_save()
    _queue.put_nowait((show, season, channel, eps))


def resume_pending():
    """启动时把上次没做完的后台准备续上(已转好的集 _do_one 自己会跳过)。返回续了几部。"""
    global _pending
    _pending = _pending_load()
    n = 0
    for it in list(_pending.values()):
        if not it.get("eps"):
            continue
        print("[prepare] 续上《%s》后台准备 %d 集" % (it["show"], len(it["eps"])), flush=True)
        enqueue(it["show"], it.get("season", 1), it["channel"], it["eps"], persist=False)
        n += 1
    return n


async def check_and_route(show, season, channel, episodes, start_ep=1):
    """入库时调用。返回 (是否坏片源, 最大间隔字节)。
    坏的话这里会把后台准备排上队,调用方就别再写 .strm 了。"""
    try:
        first = sorted(episodes, key=lambda e: e["ep"])[0]
        msg = await state.cache.get_msg(channel, first["mid"])
        if not msg or not msg.file:
            return False, -1
        gap = await probe(msg)
    except Exception as e:
        print("[prepare] 探测失败,按正常片源走", repr(e), flush=True)
        return False, -1
    if gap > GAP_LIMIT:
        print("[prepare] 《%s》音视频最远隔 %d MB,判为坏交错,转后台预处理"
              % (show, gap // 1048576), flush=True)
        enqueue(show, season, channel, episodes, start_ep)
        return True, gap
    print("[prepare] 《%s》交错正常(最大 %.1f MB),照常入库"
          % (show, max(gap, 0) / 1048576.0), flush=True)
    return False, gap

def _library_ep(show, season, ep):
    """库里这一集现在的 (channel, mid),没有返回 (None, None)。"""
    it = library._find(library.items(), "s:%s:%d" % (show, season))
    if not it:
        return None, None
    for e in it.get("episodes", []):
        if e.get("ep") == ep:
            return it.get("channel"), e.get("mid")
    return None, None


def _drop_cache(ch, mid):
    """停掉某集的缓存下载并删掉它的缓存文件(换源后旧的没用了)。"""
    c = state.cache.cachers.pop((ch, mid), None)
    if c and c.prefetch_task and not c.prefetch_task.done():
        c.prefetch_task.cancel()
    for p in (os.path.join(CACHE_DIR, "%s_%d.bin" % (ch, mid)),
              os.path.join(CACHE_DIR, "%s_%d.bm" % (ch, mid))):
        try: os.remove(p)
        except OSError: pass


async def reroll(show, season, ep, dry=False, mid=None, channel=None):
    """给某一集换个片源重下:TG 那边偶尔有个别文件块永远下不下来,整集就卡死,换一个上传版本即可。
    不进队列、立刻单独跑;成功后库里该集换成新 mid,旧缓存删掉。dry=True 只列候选不动手。
    返回给前端看的一句话。"""
    from . import finder
    key = (show, season)
    old_ch, old_mid = _library_ep(show, season, ep)
    if mid:                                               # 指定了消息:不搜,直接拿这条下(续上已缓存的块)
        ch = channel or old_ch
        library.add_series(show, ch, [{"ep": ep, "mid": mid, "filename": ""}], season)
        status[key] = "第 %d 集指定源下载中" % ep
        ok = await _do_one(show, season, ch, ep, mid)
        status[key] = "第 %d 集%s" % (ep, "已入库" if ok else "指定源也失败")
        return "第 %d 集 %s/%d %s" % (ep, ch, mid, "已入库" if ok else "失败")
    old_size = None
    if old_ch and old_mid:
        try:
            m = await state.cache.get_msg(old_ch, old_mid)
            old_size = m.file.size if m and m.file else None
        except Exception:
            pass
    cands = await finder.alternatives(show, ep)
    if dry:
        return "E%02d 候选 %d 个(当前 mid=%s size=%s): %s" % (
            ep, len(cands), old_mid, old_size, " | ".join(c["title"] for c in cands))
    if not cands:
        return "没搜到第 %d 集的其它片源" % ep
    status[key] = "第 %d 集换源中(%d 个候选)" % (ep, len(cands))
    tried, leftovers, same_ok = 0, [], False
    for c in cands + [None]:
        if c is None:                                     # 正常候选试完了,回头试大小相同的
            if not leftovers:
                break
            same_ok = True
            cands_left = leftovers
            leftovers = []
            for f in cands_left:
                c = {"title": f["filename"], "mid": f["mid"], "channel": f["channel"], "size": f["size"]}
                ch, mid = f["channel"], f["mid"]
                print("[prepare] %s E%02d 换源(同大小重发) → %s/%d" % (show, ep, ch, mid), flush=True)
                if old_ch and old_mid and (old_ch, old_mid) != (ch, mid):
                    _drop_cache(old_ch, old_mid)
                library.add_series(show, ch, [{"ep": ep, "mid": mid, "filename": f.get("filename", "")}], season)
                if await _do_one(show, season, ch, ep, mid):
                    status[key] = "第 %d 集已换源入库" % ep
                    return "第 %d 集已换源并入库(%s)" % (ep, c["title"])
                old_ch, old_mid = ch, mid
            break
        if c.get("mid"):                                  # 频道直传:不用兑换
            f = {"channel": c["channel"], "mid": c["mid"], "filename": c["title"], "size": c.get("size", 0)}
        else:
            f = await finder.redeem(c["bot"], c["token"])
        if not f:
            continue
        tried += 1
        if old_size and f["size"] == old_size and not same_ok:
            # 大小一模一样 = 多半是同一个文件;还有别的候选就先跳过,都试完了再回头拿它赌一把
            # (bot 重发一条新消息,TG 那边有时换了存储节点就能下了)
            print("[prepare] %s E%02d 候选『%s』和当前大小相同,留到最后" % (show, ep, c["title"]), flush=True)
            leftovers.append(f)
            continue
        ch, mid = f["channel"], f["mid"]
        print("[prepare] %s E%02d 换源 → %s/%d 『%s』%d MB" % (show, ep, ch, mid, c["title"], f["size"] // 1048576), flush=True)
        if old_ch and old_mid:
            _drop_cache(old_ch, old_mid)
        library.add_series(show, ch, [{"ep": ep, "mid": mid, "filename": f.get("filename", "")}], season)
        ok = await _do_one(show, season, ch, ep, mid)
        if ok:
            status[key] = "第 %d 集已换源入库" % ep
            return "第 %d 集已换源并入库(%s)" % (ep, c["title"])
        old_ch, old_mid, old_size = ch, mid, f["size"]    # 这个也坏,继续换下一个
    status[key] = "第 %d 集换源失败" % ep
    return "第 %d 集试了 %d 个候选都没成" % (ep, tried)

def requeue_from_library(show, season=1, start_ep=1):
    """不搜 TG,直接按库里记的 mid 把这部剧重新排进后台准备队列(容器重启后队列在内存里没了,用这个续上;
    已转好的集 _do_one 会自己跳过)。返回排了几集。"""
    it = library._find(library.items(), "s:%s:%d" % (show, season))
    if not it or not it.get("episodes"):
        return 0
    eps = [{"ep": e["ep"], "mid": e["mid"]} for e in it["episodes"] if e.get("mid")]
    enqueue(show, season, it["channel"], eps, start_ep)
    return len(eps)
