#!/usr/bin/env python3
"""
直播串流碼率分析工具（需要 ffmpeg / ffprobe）

錄下一段直播串流（不重新編碼，原樣存檔），再逐幀分析：
  - 每秒實際碼率的變化（看碼率有沒有一直被往下壓）
  - 實際 FPS、關鍵幀間隔、I 幀 / 非 I 幀大小
  - 時間戳斷層（掉幀、斷流）
  - 每像素分到的位元數（不同解析度之間比較用）
  - 擷取幾張截圖，方便對照臉部清晰度

用法（開播時執行，連結從「串流連結」區塊按「複製」取得）：

  python stream_probe.py "如如=http://pull-flv-....flv?..."
  python stream_probe.py -t 300 "如如-藍光=URL1" "如如-超清=URL2" "對照=URL3"
  python stream_probe.py 已錄好的檔案.flv          # 只分析，不錄

多條連結會「同時」錄，確保比較的是同一段時間。
輸出在 probe_YYYYmmdd_HHMMSS/ 資料夾：report.html、每秒碼率 CSV、截圖、錄下的 .flv。
"""

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ROLL = 5  # 平滑視窗（秒）


# ---------------------------------------------------------------- 基本工具

def die(msg):
    print(f"錯誤：{msg}", file=sys.stderr)
    sys.exit(1)


def run_json(args):
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe 執行失敗")
    return json.loads(out.stdout or "{}")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_") or "stream"


def parse_target(arg, idx):
    """'名稱=URL'、'URL'、或本機檔案路徑"""
    if os.path.exists(arg):
        return os.path.splitext(os.path.basename(arg))[0], arg, False
    if re.match(r"^(https?|rtmps?)://", arg, re.I):
        return f"stream{idx}", arg, True
    if "=" in arg:
        name, src = arg.split("=", 1)
        if "://" not in name:
            return name.strip(), src.strip(), not os.path.exists(src.strip())
    die(f"看不懂這個參數：{arg[:80]}（要是 名稱=URL、URL、或檔案路徑）")


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ---------------------------------------------------------------- 錄製

def record(name, url, out_path, duration, result):
    is_hls = ".m3u8" in url.lower()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-user_agent", UA,
           "-headers", "Referer: https://live.douyin.com/\r\n",
           "-rw_timeout", "15000000",
           "-i", url, "-t", str(duration), "-map", "0:v:0?", "-map", "0:a:0?",
           "-c", "copy"]
    if is_hls:
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-f", "flv", out_path]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=duration + 90)
        err = p.stderr.strip()
        ok = p.returncode == 0 or (os.path.exists(out_path)
                                   and os.path.getsize(out_path) > 0)
    except subprocess.TimeoutExpired:
        err, ok = "錄製逾時（串流可能卡住）", os.path.exists(out_path)
    result[name] = {"ok": ok, "err": err, "wall": time.time() - t0}


# ---------------------------------------------------------------- 分析

def analyze(path):
    info = run_json(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                     "-of", "json", path])
    vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    astreams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not vstreams:
        raise RuntimeError("檔案裡沒有視訊軌")
    vs = vstreams[0]

    def packets(sel):
        d = run_json(["ffprobe", "-v", "error", "-select_streams", sel,
                      "-show_entries", "packet=pts_time,dts_time,size,flags",
                      "-of", "json", path])
        pk = []
        for p in d.get("packets", []):
            t = p.get("dts_time") or p.get("pts_time")
            if t in (None, "N/A"):
                continue
            pk.append((float(t), int(p.get("size", 0)), "K" in p.get("flags", "")))
        pk.sort(key=lambda x: x[0])
        return pk

    vp = packets("v:0")
    ap = packets("a:0") if astreams else []
    if len(vp) < 10:
        raise RuntimeError("視訊封包太少，錄到的內容不夠分析")

    t0 = vp[0][0]
    dts = [p[0] - t0 for p in vp]
    deltas = [b - a for a, b in zip(dts, dts[1:]) if b > a]
    frame_iv = statistics.median(deltas) if deltas else 1 / 25
    duration = dts[-1] + frame_iv

    total_v = sum(p[1] for p in vp)
    total_a = sum(p[1] for p in ap)

    # 每秒碼率（最後不滿一秒的不算）
    nsec = int(duration)
    per_sec = [0] * max(nsec, 1)
    frames_sec = [0] * max(nsec, 1)
    for t, size, _ in vp:
        i = int(t - t0)
        if i < nsec:
            per_sec[i] += size
            frames_sec[i] += 1
    kbps = [b * 8 / 1000 for b in per_sec[:nsec]]
    roll = []
    for i in range(len(kbps)):
        w = kbps[max(0, i - ROLL + 1): i + 1]
        roll.append(sum(w) / len(w))
    sk = sorted(kbps)
    sr = sorted(roll[ROLL - 1:] or roll)
    med = statistics.median(sr) if sr else 0

    # 碼率被壓低的時段（5 秒平均低於中位數 70%）
    low_secs = [i for i, v in enumerate(roll) if i >= ROLL - 1 and v < med * 0.7]
    low_runs = []
    for i in low_secs:
        if low_runs and i == low_runs[-1][1] + 1:
            low_runs[-1][1] = i
        else:
            low_runs.append([i, i])

    # 關鍵幀
    key_t = [p[0] - t0 for p in vp if p[2]]
    gop = statistics.mean([b - a for a, b in zip(key_t, key_t[1:])]) if len(key_t) > 1 else None
    i_sizes = [p[1] for p in vp if p[2]]
    p_sizes = [p[1] for p in vp if not p[2]]

    # 時間戳斷層（> 3 倍正常幀間隔）
    gaps = [(dts[i], dts[i + 1] - dts[i]) for i in range(len(dts) - 1)
            if dts[i + 1] - dts[i] > frame_iv * 3]

    w, h = int(vs.get("width", 0)), int(vs.get("height", 0))
    fps = len(vp) / duration if duration else 0
    v_kbps = total_v * 8 / 1000 / duration
    a_kbps = total_a * 8 / 1000 / duration if ap else 0
    bpp = v_kbps * 1000 / (w * h * fps) if w and h and fps else 0

    return {
        "codec": vs.get("codec_name"), "profile": vs.get("profile"),
        "width": w, "height": h, "pix_fmt": vs.get("pix_fmt"),
        "nominal_fps": vs.get("avg_frame_rate") or vs.get("r_frame_rate"),
        "duration": duration, "frames": len(vp), "fps": fps,
        "v_kbps": v_kbps, "a_kbps": a_kbps, "bpp": bpp,
        "sec_min": sk[0] if sk else 0, "sec_max": sk[-1] if sk else 0,
        "roll_min": sr[0] if sr else 0, "roll_p10": pct(sr, 0.10),
        "roll_med": med, "roll_p90": pct(sr, 0.90), "roll_max": sr[-1] if sr else 0,
        "low_pct": len(low_secs) / max(len(sr), 1) * 100, "low_runs": low_runs,
        "gop": gop, "keyframes": len(key_t),
        "i_kb": statistics.mean(i_sizes) / 1024 if i_sizes else 0,
        "p_kb": statistics.mean(p_sizes) / 1024 if p_sizes else 0,
        "gaps": gaps, "gap_total": sum(g for _, g in gaps),
        "kbps": kbps, "roll": roll, "frames_sec": frames_sec[:nsec],
    }


def verdict(r, wall=None):
    """給人看的判讀（經驗值，僅供參考）"""
    out = []
    pixels = r["width"] * r["height"]
    if pixels >= 1000 * 1800 and r["roll_med"] < 1800:
        out.append(f"1080p 只有約 {r['roll_med']:.0f} kbps，對直播來說偏低，臉部細節容易被壓掉。")
    if r["bpp"] and r["bpp"] < 0.045:
        out.append(f"每像素位元 {r['bpp']:.3f} 偏低（一般直播 1080p 約 0.05–0.08），"
                   "同樣碼率下解析度越高、每個像素分到越少。")
    if r["low_pct"] >= 15:
        out.append(f"有 {r['low_pct']:.0f}% 的時間碼率掉到中位數 70% 以下，"
                   "像是自適應碼率在往下調，最常見的原因是主播上傳網路不穩。")
    elif r["roll_p90"] and r["roll_p10"] / r["roll_p90"] > 0.8:
        out.append("碼率很平穩（接近固定碼率），不像是網路造成的波動；如果畫面還是糊，"
                   "原因比較可能在推流端的碼率設定，或美顏、打光。")
    if r["gaps"]:
        out.append(f"有 {len(r['gaps'])} 次時間戳斷層、共 {r['gap_total']:.1f} 秒，"
                   "代表有掉幀或卡頓（推流端網路或 CDN）。")
    try:
        num, den = (r["nominal_fps"] or "0/1").split("/")
        nominal = float(num) / float(den) if float(den) else 0
    except ValueError:
        nominal = 0
    if nominal and r["fps"] < nominal * 0.9:
        out.append(f"實際 FPS {r['fps']:.1f} 低於標示的 {nominal:.0f}，有掉幀。")
    if wall and r["duration"] and wall > r["duration"] * 1.15 + 5:
        out.append(f"錄了 {wall:.0f} 秒只拿到 {r['duration']:.0f} 秒內容，下載期間有卡住"
                   "（可能是你這端網路，也可能是來源）。")
    if not out:
        out.append("沒有明顯異常。")
    return out


# ---------------------------------------------------------------- 輸出

def snapshots(path, out_dir, base, duration, n):
    files = []
    if n <= 0:
        return files
    for k in range(n):
        t = duration * (k + 1) / (n + 1)
        fn = f"{base}_snap{k + 1}.jpg"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-q:v", "2",
                        os.path.join(out_dir, fn)], capture_output=True)
        if os.path.exists(os.path.join(out_dir, fn)):
            files.append((t, fn))
    return files


def write_csv(path, r):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["秒", "視訊kbps", f"{ROLL}秒平均kbps", "幀數"])
        for i, (k, ro, fr) in enumerate(zip(r["kbps"], r["roll"], r["frames_sec"])):
            w.writerow([i, round(k), round(ro), fr])


def text_report(name, r, wall):
    lines = [f"\n==== {name} ====",
             f"  {r['codec']} {r['profile']}  {r['width']}×{r['height']}  {r['pix_fmt']}",
             f"  長度 {r['duration']:.1f}s  幀數 {r['frames']}  實際 FPS {r['fps']:.2f}"
             f"（標示 {r['nominal_fps']}）",
             f"  視訊平均 {r['v_kbps']:.0f} kbps   音訊 {r['a_kbps']:.0f} kbps   "
             f"每像素位元 {r['bpp']:.3f}",
             f"  {ROLL} 秒平均碼率：最低 {r['roll_min']:.0f} / P10 {r['roll_p10']:.0f} / "
             f"中位 {r['roll_med']:.0f} / P90 {r['roll_p90']:.0f} / 最高 {r['roll_max']:.0f} kbps",
             f"  關鍵幀間隔 {r['gop']:.2f}s" if r["gop"] else "  關鍵幀間隔 -",
             f"  I 幀平均 {r['i_kb']:.1f} KB  非 I 幀平均 {r['p_kb']:.1f} KB",
             f"  時間戳斷層 {len(r['gaps'])} 次（共 {r['gap_total']:.1f}s）"]
    if r["low_runs"]:
        runs = ", ".join(f"{a - ROLL + 1}-{b}s" for a, b in r["low_runs"][:8])
        lines.append(f"  碼率偏低時段：{runs}{' …' if len(r['low_runs']) > 8 else ''}")
    # 終端機小圖：每格 = 一段時間的平均碼率
    if r["kbps"]:
        bars = "▁▂▃▄▅▆▇█"
        step = max(1, len(r["kbps"]) // 60)
        chunks = [statistics.mean(r["kbps"][i:i + step]) for i in range(0, len(r["kbps"]), step)]
        top = max(chunks) or 1
        lines.append(f"  碼率走勢（每格 {step}s，最高 {top:.0f} kbps）：")
        lines.append("  " + "".join(bars[min(7, int(c / top * 7.999))] for c in chunks))
    lines.append("  判讀：")
    lines += [f"   - {v}" for v in verdict(r, wall)]
    return "\n".join(lines)


COLORS = ["#e8590c", "#1c7ed6", "#2f9e44", "#ae3ec9", "#f59f00", "#0c8599"]


def svg_chart(results):
    W, H, L, B, T, R = 900, 320, 56, 34, 14, 14
    maxlen = max(len(r["roll"]) for _, r, _ in results) or 1
    top = max(max(r["kbps"] or [0]) for _, r, _ in results)
    top = max(500, (int(top * 1.1) // 500 + 1) * 500)
    x = lambda i: L + (W - L - R) * i / max(maxlen - 1, 1)
    y = lambda v: T + (H - T - B) * (1 - v / top)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="每秒碼率">']
    for k in range(0, top + 1, 500 if top <= 4000 else 1000):
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{y(k):.1f}" y2="{y(k):.1f}" class="grid"/>'
                     f'<text x="{L - 6}" y="{y(k) + 4:.1f}" class="ax" text-anchor="end">{k}</text>')
    tick = max(10, (maxlen // 8 // 10) * 10 or 10)
    for s in range(0, maxlen, tick):
        parts.append(f'<text x="{x(s):.1f}" y="{H - 12}" class="ax" text-anchor="middle">{s}s</text>')
    for n, (name, r, _) in enumerate(results):
        c = COLORS[n % len(COLORS)]
        raw = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(r["kbps"]))
        sm = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(r["roll"]))
        parts.append(f'<polyline points="{raw}" fill="none" stroke="{c}" stroke-opacity=".25" stroke-width="1"/>')
        parts.append(f'<polyline points="{sm}" fill="none" stroke="{c}" stroke-width="2.2"/>')
    parts.append("</svg>")
    return "".join(parts)


def html_report(path, results, when):
    e = html.escape
    legend = "".join(f'<span class="lg"><i style="background:{COLORS[n % len(COLORS)]}"></i>{e(name)}</span>'
                     for n, (name, _, _) in enumerate(results))
    rows = [("解析度", lambda r: f"{r['width']}×{r['height']}"),
            ("實際 FPS", lambda r: f"{r['fps']:.1f}"),
            ("視訊平均", lambda r: f"{r['v_kbps']:.0f} kbps"),
            (f"{ROLL}s 平均 最低 / 中位 / 最高", lambda r: f"{r['roll_min']:.0f} / {r['roll_med']:.0f} / {r['roll_max']:.0f}"),
            ("P10 ~ P90", lambda r: f"{r['roll_p10']:.0f} ~ {r['roll_p90']:.0f} kbps"),
            ("碼率偏低時間占比", lambda r: f"{r['low_pct']:.0f}%"),
            ("每像素位元", lambda r: f"{r['bpp']:.3f}"),
            ("關鍵幀間隔", lambda r: f"{r['gop']:.2f}s" if r["gop"] else "-"),
            ("I 幀 / 非 I 幀平均", lambda r: f"{r['i_kb']:.1f} / {r['p_kb']:.1f} KB"),
            ("時間戳斷層", lambda r: f"{len(r['gaps'])} 次，{r['gap_total']:.1f}s"),
            ("音訊", lambda r: f"{r['a_kbps']:.0f} kbps")]
    head = "".join(f"<th>{e(n)}</th>" for n, _, _ in results)
    body = "".join("<tr><td>" + e(label) + "</td>" + "".join(f"<td>{e(fn(r))}</td>" for _, r, _ in results) + "</tr>"
                   for label, fn in rows)
    cards = ""
    for name, r, extra in results:
        vs = "".join(f"<li>{e(v)}</li>" for v in verdict(r, extra.get("wall")))
        snaps = "".join(f'<figure><a href="{e(fn)}" target="_blank"><img src="{e(fn)}" loading="lazy"></a><figcaption>{t:.0f}s</figcaption></figure>'
                        for t, fn in extra.get("snaps", []))
        cards += f"<section><h2>{e(name)}</h2><ul>{vs}</ul><div class=snaps>{snaps}</div></section>"
    doc = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>串流碼率分析</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1d2128;--mut:#69707d;--card:#fff;--line:#e3e6ea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14161a;--fg:#e8eaed;--mut:#9aa1ad;--card:#1d2026;--line:#2e333b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Microsoft JhengHei","PingFang TC",sans-serif}}
main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} .mut{{color:var(--mut);font-size:13px}}
.box,section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}}
svg{{width:100%;height:auto}} .grid{{stroke:var(--line)}} .ax{{fill:var(--mut);font-size:11px}}
.lg{{margin-right:16px;font-size:13px}} .lg i{{display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
td:first-child,th:first-child{{text-align:left;color:var(--mut)}}
h2{{font-size:17px;margin:0 0 8px}} ul{{margin:0;padding-left:20px}}
.snaps{{display:flex;gap:10px;overflow-x:auto;margin-top:12px}} figure{{margin:0;flex:0 0 auto}}
figure img{{height:320px;border-radius:6px;display:block}} figcaption{{color:var(--mut);font-size:12px;text-align:center}}
</style></head><body><main>
<h1>串流碼率分析</h1><div class="mut">{e(when)} · 粗線為 {ROLL} 秒平均，淡線為每秒值</div>
<div class="box">{legend}{svg_chart(results)}</div>
<div class="box tw"><table><tr><th></th>{head}</tr>{body}</table></div>
{cards}
<p class="mut">判讀為經驗值，僅供參考。截圖點開可看原尺寸，建議放大看臉部細節。</p>
</main></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


# ---------------------------------------------------------------- 主程式

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="直播串流碼率分析（需要 ffmpeg）")
    ap.add_argument("targets", nargs="+", help='"名稱=URL"、URL、或已錄好的檔案')
    ap.add_argument("-t", "--duration", type=int, default=120, help="錄製秒數（預設 120）")
    ap.add_argument("-o", "--out", help="輸出資料夾（預設 probe_日期時間）")
    ap.add_argument("--snapshots", type=int, default=4, help="每條串流擷取幾張截圖（預設 4，0=不截）")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"找不到 {tool}，請先安裝 ffmpeg 並加入 PATH（Windows：winget install Gyan.FFmpeg）")

    targets = [parse_target(a, i + 1) for i, a in enumerate(args.targets)]
    names = [n for n, _, _ in targets]
    if len(set(names)) != len(names):
        die("名稱重複了，請用 名稱=URL 分別命名")

    now = dt.datetime.now()
    out_dir = args.out or f"probe_{now:%Y%m%d_%H%M%S}"
    os.makedirs(out_dir, exist_ok=True)

    files, rec = {}, {}
    threads = []
    for name, src, is_url in targets:
        if is_url:
            fp = os.path.join(out_dir, safe_name(name) + ".flv")
            files[name] = fp
            th = threading.Thread(target=record, args=(name, src, fp, args.duration, rec))
            th.start()
            threads.append(th)
        else:
            files[name] = src
    if threads:
        print(f"同時錄製 {len(threads)} 條串流 {args.duration} 秒，請稍候…")
        t_start = time.time()
        while any(t.is_alive() for t in threads):
            el = int(time.time() - t_start)
            print(f"\r  已過 {el}s / {args.duration}s", end="", flush=True)
            time.sleep(1)
        print()

    results = []
    for name, _, _ in targets:
        info = rec.get(name, {})
        if info and not info["ok"]:
            print(f"\n[{name}] 錄製失敗：{info['err'][:300]}")
            print("  可能是：主播沒在播、連結過期（要重新複製）、或網址沒加引號。")
            continue
        try:
            r = analyze(files[name])
        except Exception as ex:
            print(f"\n[{name}] 分析失敗：{ex}")
            continue
        base = safe_name(name)
        write_csv(os.path.join(out_dir, base + "_per_second.csv"), r)
        snaps = snapshots(files[name], out_dir, base, r["duration"], args.snapshots)
        wall = info.get("wall")
        print(text_report(name, r, wall))
        results.append((name, r, {"wall": wall, "snaps": snaps}))

    if not results:
        die("沒有可用的結果")
    rp = os.path.join(out_dir, "report.html")
    html_report(rp, results, now.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"\n報告：{os.path.abspath(rp)}")


if __name__ == "__main__":
    main()
