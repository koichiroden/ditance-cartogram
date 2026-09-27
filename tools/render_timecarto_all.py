# -*- coding: utf-8 -*-
"""
時間距離カルトグラム動画「近いのに遠い？遠いのに近い？〇〇からの『時間距離』」を
47都道府県ぶん(鉄道版 / 鉄道＋飛行機版)まとめて書き出す。

使い方(リポジトリのルートで実行):
    # 47都道府県 × 鉄道版・飛行機版 すべて(沖縄の鉄道版は行ける県が無いので除く = 93本)
    python3 tools/render_timecarto_all.py

    # 飛行機版だけ / 鉄道版だけ
    python3 tools/render_timecarto_all.py --mode best
    python3 tools/render_timecarto_all.py --mode rail

    # 一部の県だけ(ローマ字。configs/timecarto/<県>_<mode>.json の <県> 部分)
    python3 tools/render_timecarto_all.py --only tokyo,osaka,fukuoka

    # 並列数を指定(既定: CPUコア数の半分)。既にできている動画は飛ばす
    python3 tools/render_timecarto_all.py --workers 4 --skip-existing

    # 10fpsの確認用プレビューで全部 / 最後にzipにまとめる
    python3 tools/render_timecarto_all.py --fast --zip output/timecarto_all.zip

出力: output/timecarto_<県>_<mode>.mp4 と、サムネイル用の最終フレーム
      output/timecarto_<県>_<mode>_final.png
"""
import argparse
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 県番号順(北海道 → 沖縄)
PREFS = ["hokkaido", "aomori", "iwate", "miyagi", "akita", "yamagata", "fukushima", "ibaraki", "tochigi",
         "gunma", "saitama", "chiba", "tokyo", "kanagawa", "niigata", "toyama", "ishikawa", "fukui",
         "yamanashi", "nagano", "gifu", "shizuoka", "aichi", "mie", "shiga", "kyoto", "osaka", "hyogo",
         "nara", "wakayama", "tottori", "shimane", "okayama", "hiroshima", "yamaguchi", "tokushima",
         "kagawa", "ehime", "kochi", "fukuoka", "saga", "nagasaki", "kumamoto", "oita", "miyazaki",
         "kagoshima", "okinawa"]


def render_one(cfg_path, out_dir, fast, keep_temp):
    # 子プロセス内で読み込む(フォント・地図データの読み込みを各プロセスで行う)
    os.chdir(ROOT)
    from race_video.time_cartogram import render_time_cartogram
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    t0 = time.time()
    out = render_time_cartogram(cfg, out_dir=out_dir, fast_preview=fast)
    slug = cfg.get("slug")
    if not keep_temp:
        for suffix in ("_audio.wav", "_base_map.png"):
            p = Path(out_dir) / f"{slug}{suffix}"
            if p.exists():
                p.unlink()
    return out, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description="時間距離カルトグラム動画を47都道府県ぶんまとめて生成")
    ap.add_argument("--mode", choices=["rail", "best", "both"], default="both",
                    help="rail=鉄道版, best=鉄道＋飛行機版, both=両方(既定)")
    ap.add_argument("--only", default="", help="カンマ区切りで県を絞る(例: tokyo,osaka)")
    ap.add_argument("--configs", default="configs/timecarto", help="configのフォルダ")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--fast", action="store_true", help="10fpsの確認用プレビュー")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="同時に生成する本数(既定: CPUコア数の半分)")
    ap.add_argument("--skip-existing", action="store_true", help="既に mp4 がある県は飛ばす")
    ap.add_argument("--keep-temp", action="store_true", help="音声wav・ベースマップpngも残す")
    ap.add_argument("--zip", default="", help="できた mp4 と最終フレームpngをこのzipにまとめる")
    args = ap.parse_args()

    os.chdir(ROOT)
    os.makedirs(args.out_dir, exist_ok=True)

    # 事前チェック(ここで止まれば、原因がそのまま表示される)
    import shutil
    problems = []
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg が見つかりません → sudo apt-get install -y ffmpeg")
    if not Path("race_video/time_cartogram.py").exists():
        problems.append("race_video/time_cartogram.py がありません(アップロード漏れ)")
    if not Path(args.configs).exists():
        problems.append(f"{args.configs}/ がありません(configs/timecarto のアップロード漏れ)")
    try:
        import PIL  # noqa: F401
    except ImportError:
        problems.append(f"Pillow が入っていません({sys.executable}) → pip install -r requirements.txt")
    try:
        from race_video import fonts as _f
        _f.resolve()
    except Exception as e:  # noqa: BLE001
        problems.append(f"日本語フォント: {e}")
    if problems:
        print("生成を始める前に問題が見つかりました:")
        for p in problems:
            print("  -", p)
        return 1
    modes = ["rail", "best"] if args.mode == "both" else [args.mode]
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    prefs = [p for p in PREFS if not only or p in only]
    unknown = [s for s in only if s not in PREFS]
    if unknown:
        print("知らない県名があります:", ", ".join(unknown), "(例: tokyo, osaka)")
        return 2

    jobs = []
    for pref in prefs:
        for mode in modes:
            cfg_path = Path(args.configs) / f"{pref}_{mode}.json"
            if not cfg_path.exists():
                print(f"[skip] {cfg_path} がありません(沖縄の鉄道版など)")
                continue
            mp4 = Path(args.out_dir) / f"timecarto_{pref}_{mode}.mp4"
            if args.skip_existing and mp4.exists():
                print(f"[skip] {mp4} は作成済み")
                continue
            jobs.append(cfg_path)

    print(f"{len(jobs)} 本を生成します(並列 {args.workers}、{'10fps プレビュー' if args.fast else '30fps'})")
    t_start = time.time()
    done, failed, outputs = 0, [], []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(render_one, str(c), args.out_dir, args.fast, args.keep_temp): c for c in jobs}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                out, sec = fut.result()
                done += 1
                outputs.append(out)
                print(f"[{done + len(failed)}/{len(jobs)}] OK   {out}  ({sec:.0f}秒)", flush=True)
            except Exception as e:  # noqa: BLE001
                failed.append((c, e))
                print(f"[{done + len(failed)}/{len(jobs)}] FAIL {c}: {type(e).__name__}: {e}", flush=True)
                tb = getattr(e, "__cause__", None)
                if tb is not None:
                    print(str(tb)[-1500:], flush=True)

    if args.zip:
        zpath = Path(args.zip)
        zpath.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as zf:
            for out in sorted(outputs):
                zf.write(out, Path(out).name)
                png = Path(out).with_name(Path(out).stem + "_final.png")
                if png.exists():
                    zf.write(png, png.name)
        print("zip:", zpath)

    mins = (time.time() - t_start) / 60
    print(f"完了: 成功 {done} 本 / 失敗 {len(failed)} 本 / {mins:.1f}分")
    for c, e in failed:
        print("  失敗:", c, e)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
