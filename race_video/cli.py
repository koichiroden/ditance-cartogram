# -*- coding: utf-8 -*-
"""
使い方:
    python -m race_video.cli configs/osaka_kyoto.json
    python -m race_video.cli configs/osaka_kyoto.json --fast   # 低fpsプレビュー

configファイルの書き方は configs/osaka_kyoto.json と README を参照。
"""
import argparse
import json
import sys

from .station_db import load_station_db, check_line_quality
from .route_builder import build_all_routes
from .render_base import render_base_map
from .animate import render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="レース設定JSONのパス")
    ap.add_argument("--geojson", default="data/routes.geojson")
    ap.add_argument("--shp", default="data/stations_shp/kansai_stations")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--frames-dir", default="frames")
    ap.add_argument("--fast", action="store_true", help="10fpsの低画質プレビューを高速生成")
    ap.add_argument("--out-name", default=None,
                     help="出力ファイル名(拡張子なし)。省略時はconfigの'slug'を使う")
    ap.add_argument("--check-stations", action="store_true",
                     help="駅データSHPの品質チェックだけ実行して終了する")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    # 時間距離カルトグラム(近いのに遠い？遠いのに近い？)は駅データ・路線データを使わない別レンダラー
    if config.get("type") == "time_cartogram":
        from .time_cartogram import render_time_cartogram
        out = render_time_cartogram(config, out_dir=args.out_dir, fast_preview=args.fast,
                                    out_name=args.out_name)
        print("done ->", out)
        return

    # config側で geojson_path / stations_shp_path を指定すればCLI引数より優先する。
    # 全区間が mode:"legs"(飛行機・バス等の組み合わせ)のconfigは、関西の
    # routes.geojson / kansai_stations を全く使わないので、背景にKansai各線が
    # 薄く映り込まないよう、空のgeojsonを指定できるようにするため。
    geojson_path = config.get("geojson_path", args.geojson)
    shp_path = config.get("stations_shp_path", args.shp)

    station_db = load_station_db(shp_path)

    if args.check_stations:
        for line, n, bad, worst in sorted(check_line_quality(station_db), key=lambda x: -x[2]):
            flag = "NG" if bad else "ok"
            print(f"{flag:3s} {line:16s} n={n:3d} suspicious_gaps={bad:3d} worst_gap_km={worst:.1f}")
        return

    print(f"=== building routes for '{config.get('title_line1', config.get('slug'))}' ===")
    paths, warnings = build_all_routes(config, geojson_path=geojson_path, station_db=station_db)
    for w in warnings:
        print("[warn]", w)
    for key, route in paths.items():
        print(f"  {key}: {route['name']}  {len(route['polyline'])}pts  "
              f"{route['cum_dist'][-1]:.1f}km  {route['total_min']}min")

    print("=== rendering base map ===")
    base_map, proj = render_base_map(config, paths, geojson_path=geojson_path)
    base_map.convert("RGB").save(f"{args.out_dir}/{config.get('slug','race')}_base_map.png")

    print("=== rendering video ===")
    out_path = render(config, paths, base_map, proj,
                       out_dir=args.out_dir, frames_dir=args.frames_dir,
                       fast_preview=args.fast, out_name=args.out_name)
    print("done ->", out_path)


if __name__ == "__main__":
    sys.exit(main())
