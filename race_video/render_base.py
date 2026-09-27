# -*- coding: utf-8 -*-
"""
スタイリッシュな(箱根駅伝ルート紹介動画風の)ベースマップを1枚生成する。
どのレース設定(config)でも使えるように汎用化してある。
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .geo import (compute_projection, compute_fixed_projection, compute_route_fit_projection,
                   compute_geo_clip_bbox, project, unproject, CANVAS_W, CANVAS_H, MAP_BOTTOM, SAFE_TOP_Y)
from . import fonts as _fonts

FONT_BOLD, FONT_REGULAR, FONT_BLACK = _fonts.resolve()


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def load_all_lines(geojson_path):
    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for feat in data["features"]:
        geom = feat["geometry"]
        coords_list = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
        out.append((feat["properties"]["line_name"], coords_list))
    return out


def load_land_polygons(geojson_path):
    """海岸線(陸地の輪郭)データを読み込み、外周リングのリストを返す。
    元データに穴(湖など)は無いことを確認済みなので外周だけで良い。
    ファイルが無い場合は空リストを返し、海の表現なしで従来どおり動く。"""
    p = Path(geojson_path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    rings = []
    for feat in data["features"]:
        geom = feat["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            if poly:
                rings.append(poly[0])
    return rings


def _lerp_at_x(a, b, x):
    ax, ay = a
    bx, by = b
    t = 0.0 if bx == ax else (x - ax) / (bx - ax)
    return (x, ay + (by - ay) * t)


def _lerp_at_y(a, b, y):
    ax, ay = a
    bx, by = b
    t = 0.0 if by == ay else (y - ay) / (by - ay)
    return (ax + (bx - ax) * t, y)


def _clip_half_plane(points, keep, intersect):
    if not points:
        return points
    out = []
    prev = points[-1]
    prev_in = keep(prev)
    for cur in points:
        cur_in = keep(cur)
        if cur_in:
            if not prev_in:
                out.append(intersect(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(intersect(prev, cur))
        prev, prev_in = cur, cur_in
    return out


def clip_ring_to_bbox(ring, lon_min, lon_max, lat_min, lat_max):
    """Sutherland-Hodgman法で、矩形(表示範囲)の外にある陸地ポリゴンの
    大部分を先に切り落とす。海岸線データは日本全体規模なので、これを
    せずに描画しようとすると座標が巨大になり重く/不安定になるため。"""
    pts = ring
    pts = _clip_half_plane(pts, lambda p: p[0] >= lon_min, lambda a, b: _lerp_at_x(a, b, lon_min))
    pts = _clip_half_plane(pts, lambda p: p[0] <= lon_max, lambda a, b: _lerp_at_x(a, b, lon_max))
    pts = _clip_half_plane(pts, lambda p: p[1] >= lat_min, lambda a, b: _lerp_at_y(a, b, lat_min))
    pts = _clip_half_plane(pts, lambda p: p[1] <= lat_max, lambda a, b: _lerp_at_y(a, b, lat_max))
    return pts


def draw_land_and_sea(canvas, proj, land_rings, land_color=(23, 22, 20, 225),
                       coast_color=(150, 205, 230, 130), clip_bbox=None):
    """陸地を塗り、海岸線をうっすら光らせて、どこが海でどこが陸か
    分かるようにする(海=背景のグラデーションのまま)。

    clip_bbox に (lon_min, lon_max, lat_min, lat_max) を渡すと、
    その範囲の外にある陸地は(たとえ現在のズーム/オフセットで座標上は
    キャンバス内に収まってしまう場合でも)描画しない。map_extent="japan"
    で、東西に長い経路(東京〜広島など)のせいで縦方向に大きな余白が
    できたときに、そこへ北海道・東北・沖縄など無関係な地形が写り込む
    のを防ぐために使う。"""
    if not land_rings:
        return

    margin = 140
    corners = [
        unproject(proj, -margin, -margin),
        unproject(proj, CANVAS_W + margin, -margin),
        unproject(proj, -margin, CANVAS_H + margin),
        unproject(proj, CANVAS_W + margin, CANVAS_H + margin),
    ]
    lon_min = min(c[0] for c in corners)
    lon_max = max(c[0] for c in corners)
    lat_min = min(c[1] for c in corners)
    lat_max = max(c[1] for c in corners)

    if clip_bbox is not None:
        cb_lon_min, cb_lon_max, cb_lat_min, cb_lat_max = clip_bbox
        lon_min = max(lon_min, cb_lon_min)
        lon_max = min(lon_max, cb_lon_max)
        lat_min = max(lat_min, cb_lat_min)
        lat_max = min(lat_max, cb_lat_max)

    land_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(land_layer)
    coast_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(coast_layer)

    for ring in land_rings:
        clipped = clip_ring_to_bbox(ring, lon_min, lon_max, lat_min, lat_max)
        if len(clipped) < 3:
            continue
        pts = [project(proj, lon, lat) for lon, lat in clipped]
        ld.polygon(pts, fill=land_color)

        # 海岸線そのもの(切り取り範囲の縁ではなく実際の陸地の輪郭だけ)は、
        # 表示範囲内(ピクセル座標)かつ clip_bbox 範囲内(緯度経度)の
        # 両方を満たす区間だけを描く。
        full_pts = [project(proj, lon, lat) for lon, lat in ring]
        in_view = [
            (-80 <= x <= CANVAS_W + 80 and -80 <= y <= CANVAS_H + 80
             and lon_min <= lon <= lon_max and lat_min <= lat <= lat_max)
            for (x, y), (lon, lat) in zip(full_pts, ring)
        ]
        seg = []
        for pt, ok in zip(full_pts, in_view):
            if ok:
                seg.append(pt)
            elif seg:
                if len(seg) >= 2:
                    cd.line(seg, fill=coast_color, width=3, joint="curve")
                seg = []
        if len(seg) >= 2:
            cd.line(seg, fill=coast_color, width=3, joint="curve")

    canvas.alpha_composite(land_layer)
    coast_layer = coast_layer.filter(ImageFilter.GaussianBlur(1.2))
    canvas.alpha_composite(coast_layer)


def vertical_gradient(w, h, top_color, bottom_color):
    base = Image.new("RGB", (w, h), top_color)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / (h - 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return base


def draw_glow_polyline(img, pts, color, width, glow_width, glow_alpha=90):
    if len(pts) < 2:
        return
    glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.line(pts, fill=color + (glow_alpha,), width=glow_width, joint="curve")
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_width / 3))
    img.alpha_composite(glow_layer)

    line_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(line_layer)
    ld.line(pts, fill=color + (255,), width=width, joint="curve")
    for p in (pts[0], pts[-1]):
        r = width / 2
        ld.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color + (255,))
    img.alpha_composite(line_layer)


def render_base_map(config, paths, geojson_path="data/routes.geojson"):
    all_lines = load_all_lines(geojson_path) if geojson_path and Path(geojson_path).exists() else []
    route_list = list(paths.values())

    focus_coords = []
    for r in route_list:
        focus_coords.extend(r["polyline"])

    map_extent = config.get("map_extent", "route")  # "route"(既定) | "japan"(全国海岸線+出発地~到着地にフィット) | "japan_fixed"(常に日本全国を固定表示)
    geo_clip_bbox = None
    if map_extent == "japan_fixed":
        # 常に日本全国(北海道〜沖縄)を同じ縮尺で表示したい場合のみ使用。
        # 出発地・到着地がどこであっても画角は変わらない。
        proj = compute_fixed_projection()
    elif map_extent == "japan":
        # 全国海岸線を背景にしつつ、実際の出発地・到着地・経路がちょうど
        # 収まるように(北を上にしたまま、縦横比も保って)ズームレベルを
        # 毎回計算する(既定の全国表示)。東京〜広島のように出発地・
        # 到着地が東西一直線に近いペアは、縦長キャンバスの都合上どうしても
        # 上下に余白ができるが、その余白に北海道・東北・沖縄など無関係な
        # 地形が写り込まないよう、geo_clip_bbox で出発地・到着地の周辺
        # だけに描画範囲を絞る。
        proj = compute_route_fit_projection(focus_coords)
        geo_clip_bbox = compute_geo_clip_bbox(focus_coords)
    else:
        proj = compute_projection(focus_coords, pad_ratio=0.16)

    bg = vertical_gradient(CANVAS_W, CANVAS_H, (8, 12, 28), (2, 4, 12))
    canvas = bg.convert("RGBA")

    # 海と陸地: 背景のグラデーションをそのまま海として使い、陸地だけを
    # うっすら塗って海岸線を光らせる(データが無い場合は何も描かず、
    # 従来どおり全面が背景グラデーションのまま)。
    # map_extent="japan" のときは全国版の海岸線(行政区域データを統合し、
    # 市区町村境界を除去した「陸地の輪郭」のみ)を使う。
    coastline_name = "coastline_japan.geojson" if map_extent in ("japan", "japan_fixed") else "coastline.geojson"
    data_dir = Path(geojson_path).parent if geojson_path else Path("data")
    land_rings = load_land_polygons(str(data_dir / coastline_name))
    draw_land_and_sea(canvas, proj, land_rings, clip_bbox=geo_clip_bbox)

    # 背景テクスチャ: 全路線をうす暗いグレーで描画(表示範囲外は自然に切れる)
    # レース中の路線と見分けやすいよう、以前より少しだけ濃くしてある。
    faint = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    fd = ImageDraw.Draw(faint)
    for name, segs in all_lines:
        for seg in segs:
            pts = [project(proj, lon, lat) for lon, lat in seg]
            pts = [p for p in pts if -50 <= p[0] <= CANVAS_W + 50 and -50 <= p[1] <= CANVAS_H + 50]
            if len(pts) >= 2:
                fd.line(pts, fill=(150, 170, 210, 70), width=2, joint="curve")
    canvas.alpha_composite(faint)

    # 比較ルート(発光ライン)。後に描画した方が手前に見える。
    for route in reversed(route_list):
        color = tuple(route["color"])
        pts = [project(proj, lon, lat) for lon, lat in route["polyline"]]
        draw_glow_polyline(canvas, pts, color, width=6, glow_width=22, glow_alpha=90)

    draw = ImageDraw.Draw(canvas)
    f_station = font(FONT_BOLD, 26)

    def draw_station_dot(route, st, is_terminal):
        x, y = project(proj, st["lon"], st["lat"])
        r = 12 if is_terminal else 7
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255),
                     outline=tuple(route["color"]) + (255,), width=4)
        if is_terminal:
            draw.ellipse([x - r - 6, y - r - 6, x + r + 6, y + r + 6],
                         outline=(255, 255, 255, 140), width=2)

    start_name = config.get("start_name", route_list[0]["stations"][0]["name"])
    end_label = config.get("end_label", route_list[0]["stations"][-1]["name"])

    for route in route_list:
        for st in route["stations"]:
            if st["popup"]:
                is_term = st["name"] in (start_name, end_label)
                draw_station_dot(route, st, is_term)

    start_pt = route_list[0]["stations"][0]
    x, y = project(proj, start_pt["lon"], start_pt["lat"])
    draw.text((x, y - 34), "START", font=font(FONT_BLACK, 24, index=0), fill=(255, 215, 0, 255), anchor="mm")
    draw.text((x, y - 62), start_name, font=f_station, fill=(255, 255, 255, 255), anchor="mm")

    finish_pts = [project(proj, r["stations"][-1]["lon"], r["stations"][-1]["lat"]) for r in route_list]
    fx = sum(p[0] for p in finish_pts) / len(finish_pts)
    fy = min(p[1] for p in finish_pts)
    draw.text((fx, fy + 42), "FINISH", font=font(FONT_BLACK, 24, index=0), fill=(255, 215, 0, 255), anchor="mm")
    draw.text((fx, fy + 70), end_label, font=f_station, fill=(255, 255, 255, 255), anchor="mm")

    # プログレスバーを「実際に描画された路線のすぐ下」に動的配置できるよう、
    # 路線(+ FINISH表記)が画面上で一番下まで達しているY座標を控えておく。
    # ルートが短く画面上部〜中央付近に収まる構成のときほど、この値は
    # MAP_BOTTOM よりだいぶ小さくなる。
    all_line_ys = [
        project(proj, lon, lat)[1]
        for route in route_list
        for lon, lat in route["polyline"]
    ]
    max_line_y = max(all_line_ys) if all_line_ys else MAP_BOTTOM
    proj["content_bottom_y"] = max(max_line_y, fy + 90)

    # タイトル & 凡例
    f_title = font(FONT_BLACK, 60, index=0)
    f_sub = font(FONT_BOLD, 28)
    f_legend = font(FONT_BOLD, 28)

    # タイトル/凡例は、リールUIのセーフゾーン(画面上端から1/8=SAFE_TOP_Y)
    # より必ず下に来るように配置する(title_y1の文字上端がSAFE_TOP_Yより
    # 十分下になるよう余白を確保)。
    title_y1 = SAFE_TOP_Y + 50
    title_y2 = title_y1 + 58
    draw.text((CANVAS_W / 2, title_y1), config.get("title_line1", ""), font=f_title,
               fill=(255, 255, 255, 255), anchor="mm")
    draw.text((CANVAS_W / 2, title_y2), config.get("title_line2", ""), font=f_sub,
               fill=(255, 215, 0, 255), anchor="mm")

    n = len(route_list)
    total_w = 0
    swatches = []
    tmp = Image.new("RGBA", (10, 10))
    tmpd = ImageDraw.Draw(tmp)
    for r in route_list:
        w = tmpd.textlength(r["name"], font=f_legend) + 46
        swatches.append(w)
        total_w += w + 40
    total_w -= 40
    cx = CANVAS_W / 2 - total_w / 2
    ly = title_y2 + 60
    for r, w in zip(route_list, swatches):
        draw.ellipse([cx, ly - 12, cx + 24, ly + 12], fill=tuple(r["color"]) + (255,))
        draw.text((cx + 34, ly), r["name"], font=f_legend, fill=(255, 255, 255, 255), anchor="lm")
        cx += w + 40

    return canvas, proj
