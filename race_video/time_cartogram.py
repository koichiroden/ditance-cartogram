# -*- coding: utf-8 -*-
"""
時間距離カルトグラムの縦動画(1080x1920)を作る。

「近いのに遠い？遠いのに近い？ 〇〇からの距離」
起点(例: 東京駅)から各都道府県の代表駅への「方角」はそのままに、
「距離」だけを所要時間で描き直す(基準 200km/h = 1時間で200km)。

- 背景の日本地図は、起点を中心とした正距方位図法で描く。
  つまり before(実際の距離)の点は、地図上の本当の位置にぴったり重なる。
- 1県ずつ(既定1秒ごと)線が「にゅっ」と伸び縮みし、効果音も鳴る。
- 動いた県には、元の位置(○)から新しい位置(●)への点線が残るので、
  最後のフレームで before → after が一目でわかる。

configは Web版ツール(時間距離カルトグラム)の「縦動画用JSONをコピー」で
作れる。configs/timecarto/ に47都道府県 × 鉄道/鉄道＋飛行機 のサンプルあり。

    python3 -m race_video.cli configs/timecarto/tokyo_rail.json
    python3 -m race_video.cli configs/timecarto/tokyo_rail.json --fast   # 10fpsプレビュー
"""
import json
import math
import os
import subprocess
import wave
from array import array
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter

from .geo import CANVAS_W, CANVAS_H, SAFE_TOP_Y, SAFE_BOTTOM_Y
from .render_base import vertical_gradient
from . import fonts as _fonts

FONT_BOLD, FONT_REGULAR, FONT_BLACK = _fonts.resolve()

FPS = 30
SR = 44100

# 色(Web版と同じ)
C_FAST = (88, 180, 255)     # 速い → 近くなる
C_SLOW = (255, 154, 74)     # 遅い → 遠くなる
C_NEU = (217, 222, 228)
C_GRAY = (95, 104, 116)
GOLD = (255, 215, 0)
WHITE = (255, 255, 255)
BG_TOP, BG_BOTTOM = (8, 12, 28), (2, 4, 12)
LAND = (46, 56, 76, 240)
COAST = (165, 215, 240, 170)

# レイアウト(上下1/8はリールUIのセーフゾーンなので文字を置かない)
# 左右のセーフエリア: 文字・情報はすべて画面端から SAFE_X 以上内側に置く
SAFE_X = 60
TITLE_Y1 = SAFE_TOP_Y + 52          # 近いのに遠い？遠いのに近い？
TITLE_Y2 = TITLE_Y1 + 96            # 東京からの『時間距離』
MAP_TOP, MAP_BOTTOM = TITLE_Y2 + 72, 1150
MAP_LEFT, MAP_RIGHT = 30, CANVAS_W - 30     # 地図(点・線)はここまで。文字は SAFE_X の内側
INFO_Y1, INFO_Y2 = 1200, 1278       # (旧)下部パネルの位置。いまは PANEL を使う
FIT_PAD = 50                        # 地図を画面に合わせる時の余白(描画ごとに決め直す)
PANEL = (SAFE_X, 1178, CANVAS_W - SAFE_X, 1330)   # 「東京 → 北海道」やまとめを出す枠(描画ごとに決め直す)
TABLE_TOP, TABLE_BOTTOM = 1340, SAFE_BOTTOM_Y - 6   # 判定結果の表
TABLE_COLS = 4                      # 表を何段組みにするか(46県 → 4段 × 12行)


def font(path, size):
    return ImageFont.truetype(path, size, index=_fonts.JP_INDEX)


# ---------------------------------------------------------------- 幾何
def aeqd(origin, lat, lon):
    """起点を中心とした正距方位図法(km)。x=東, y=北。距離と方位はWeb版と同じ計算。"""
    R = 6371.0
    p1, p2 = math.radians(origin["lat"]), math.radians(lat)
    dl = math.radians(lon - origin["lon"])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist = 2 * R * math.asin(min(1.0, math.sqrt(h)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = math.atan2(y, x)
    return dist, brg


def polar(dist, brg):
    return math.sin(brg) * dist, math.cos(brg) * dist


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_col(c1, c2, t):
    return tuple(int(round(lerp(a, b, t))) for a, b in zip(c1, c2))


def color_for_index(ix):
    if ix is None:
        return C_GRAY
    t = max(-1.0, min(1.0, math.log2(ix / 100.0) / 0.8))
    return lerp_col(C_NEU, C_FAST, t) if t >= 0 else lerp_col(C_NEU, C_SLOW, -t)


def ease_out_back(x, s=1.9):
    x = max(0.0, min(1.0, x))
    return 1 + (s + 1) * (x - 1) ** 3 + s * (x - 1) ** 2


def ease_nyuru(x, c1=0.9):
    """にゅるっと: ほんの少し溜めてからゆっくり動き出し、少し行き過ぎてなめらかに止まる(easeInOutBack)。"""
    x = max(0.0, min(1.0, x))
    c2 = c1 * 1.525
    if x < 0.5:
        return ((2 * x) ** 2 * ((c2 + 1) * 2 * x - c2)) / 2
    return ((2 * x - 2) ** 2 * ((c2 + 1) * (x * 2 - 2) + c2) + 2) / 2


def ease_in_out(x):
    x = max(0.0, min(1.0, x))
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def fmt_min(m):
    return f"{m // 60}時間{m % 60:02d}分" if m >= 60 else f"{m}分"


# ---------------------------------------------------------------- データ準備
def prepare(config):
    o = config["origin"]
    base = float(config.get("base_kmh", 200))
    dests = []
    for d in config["destinations"]:
        dist, brg = aeqd(o, d["lat"], d["lon"])
        m = d.get("minutes")
        if m is not None:
            r_after = m / 60.0 * base
            ix = dist / r_after * 100.0 if r_after > 0 else None
        else:
            r_after, ix = None, None
        dests.append({**d, "dist": dist, "brg": brg, "r_after": r_after, "ix": ix,
                      "color": color_for_index(ix)})
    order = config.get("order", "code")
    if order == "near":
        dests.sort(key=lambda d: d["dist"])
    elif order == "far":
        dests.sort(key=lambda d: -d["dist"])
    gap = float(config.get("gap_sec", 1.0))
    intro = float(config.get("intro_sec", 2.0))
    for k, d in enumerate(dests):
        d["t0"] = intro + k * gap
    return dests


def compute_view(dests, shrink=1.0):
    xs, ys = [0.0], [0.0]
    for d in dests:
        for r in (d["dist"], d["r_after"]):
            if r is None:
                continue
            x, y = polar(r, d["brg"])
            xs.append(x)
            ys.append(y)
    pad = FIT_PAD
    w = max(max(xs) - min(xs), 200.0)
    h = max(max(ys) - min(ys), 200.0)
    s = min((MAP_RIGHT - MAP_LEFT - 2 * pad) / w, (MAP_BOTTOM - MAP_TOP - 2 * pad) / h) * shrink
    cxk, cyk = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    cxp, cyp = (MAP_LEFT + MAP_RIGHT) / 2, (MAP_TOP + MAP_BOTTOM) / 2
    return {"s": s, "cxk": cxk, "cyk": cyk, "cxp": cxp, "cyp": cyp}


def to_px(v, x, y):
    return (v["cxp"] + (x - v["cxk"]) * v["s"], v["cyp"] - (y - v["cyk"]) * v["s"])


def choose_layout(dests):
    """点(before/after)の範囲が画面いっぱいになるよう地図を大きく取り、
    「東京 → ○○」やまとめの枠は、点も線も無い下の隅(左下 or 右下)に重ねて置く。
    隅が空かない時は、地図を枠と反対側へ寄せたり少しだけ縮めたりして再挑戦し、
    それでもだめなら従来どおり地図の下に横長の枠を置く。"""
    global MAP_BOTTOM, FIT_PAD, PANEL
    PW, PH = 560, 172

    def points(view):
        ox, oy = to_px(view, 0, 0)
        pts = [(ox, oy)]
        for dd in dests:
            for r_ in (dd["dist"], dd["r_after"]):
                if r_:
                    pts.append(to_px(view, *polar(r_, dd["brg"])))
        return pts

    def free(box, pts):
        ox, oy = pts[0]
        pad_box = (box[0] - 14, box[1] - 14, box[2] + 14, box[3] + 14)
        if any(pad_box[0] <= x <= pad_box[2] and pad_box[1] <= y <= pad_box[3] for x, y in pts):
            return False
        if any(seg_hits_box((ox, oy), q, pad_box) for q in pts[1:]):
            return False
        return not boxes_overlap(pad_box, (ox - 80, oy - 30, ox + 80, oy + 80))

    MAP_BOTTOM, FIT_PAD = TABLE_TOP - 10, 28
    for shrink in (1.0, 0.94, 0.88, 0.82, 0.76):
        base_view = compute_view(dests, shrink)
        pts0 = points(base_view)
        xs, ys = [p[0] for p in pts0], [p[1] for p in pts0]
        sl_left = min(xs) - (MAP_LEFT + FIT_PAD)
        sl_right = (MAP_RIGHT - FIT_PAD) - max(xs)
        sl_top = min(ys) - (MAP_TOP + FIT_PAD)
        corners = [
            ((SAFE_X, MAP_BOTTOM - PH, SAFE_X + PW, MAP_BOTTOM), (max(0, sl_right), -max(0, sl_top))),
            ((CANVAS_W - SAFE_X - PW, MAP_BOTTOM - PH, CANVAS_W - SAFE_X, MAP_BOTTOM), (-max(0, sl_left), -max(0, sl_top))),
        ]
        for box, (sx, sy) in corners:
            for fx, fy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                view = dict(base_view)
                view["cxp"] += sx * fx
                view["cyp"] += sy * fy
                if free(box, points(view)):
                    PANEL = box
                    return view
    MAP_BOTTOM, FIT_PAD = 1150, 50
    PANEL = (SAFE_X, 1178, CANVAS_W - SAFE_X, 1330)
    return compute_view(dests)


# ---------------------------------------------------------------- ベース画像
def load_japan(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    rings = []
    for feat in data["features"]:
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            if poly:
                rings.append(poly[0])
    return rings


def render_base(config, dests, view, japan_path):
    o = config["origin"]
    canvas = vertical_gradient(CANVAS_W, CANVAS_H, BG_TOP, BG_BOTTOM).convert("RGBA")

    # 日本地図(起点中心の正距方位図法 = before の位置関係そのもの)
    rings = load_japan(japan_path)
    land = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    coast = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld, cd = ImageDraw.Draw(land), ImageDraw.Draw(coast)
    for ring in rings:
        pts = [to_px(view, *polar(*aeqd(o, lat, lon))) for lon, lat in ring]
        if len(pts) >= 3:
            ld.polygon(pts, fill=LAND)
            cd.line(pts + [pts[0]], fill=COAST, width=2, joint="curve")
    # 描くのは「点(before/after)の上下左右の最遠地 + 少しの余白」の範囲だけ。
    # 石垣島や稚内など、県の点から離れた陸地はふわっと消す。
    pts_all = [to_px(view, 0, 0)]
    for dd in dests:
        for r_ in (dd["dist"], dd["r_after"]):
            if r_:
                pts_all.append(to_px(view, *polar(r_, dd["brg"])))
    margin = float(config.get("map_margin_px", 40))
    bx0 = min(p[0] for p in pts_all) - margin
    by0 = min(p[1] for p in pts_all) - margin
    bx1 = max(p[0] for p in pts_all) + margin
    by1 = max(p[1] for p in pts_all) + margin
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([bx0, by0, bx1, by1], radius=60, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(18))

    def clip(layer):
        r, g, b, a = layer.split()
        return Image.merge("RGBA", (r, g, b, ImageChops.multiply(a, mask)))

    canvas.alpha_composite(clip(land))
    canvas.alpha_composite(clip(coast.filter(ImageFilter.GaussianBlur(0.9))))

    # 同心円: 既定は「2時間 = 400km」ごと(ring_hours で変更可)。
    # ラベルは、県の点や線が少ない方角(海側など)を自動で選んで、円の上にまとめて並べる。
    ox, oy = to_px(view, 0, 0)
    base_kmh = float(config.get("base_kmh", 200))
    hours = float(config.get("ring_hours", 2))
    while hours * base_kmh * view["s"] < 110:   # 円どうしが詰まりすぎる時は間隔を倍に
        hours *= 2
    step = hours * base_kmh
    far = max(math.hypot(ox - x, oy - y) for x in (0, CANVAS_W) for y in (MAP_TOP, MAP_BOTTOM))
    radii = []
    km = step
    while km * view["s"] < far:
        radii.append(km)
        km += step

    # 点・線の方角(画面上の角度)を集め、いちばん空いている方角を探す
    dest_angles = []
    for dd in dests:
        for r in (dd["dist"], dd["r_after"]):
            if r:
                x, y = to_px(view, *polar(r, dd["brg"]))
                dest_angles.append(math.atan2(y - oy, x - ox))

    def label_pos(theta, r):
        return ox + math.cos(theta) * r, oy + math.sin(theta) * r

    def inside(x, y):
        return MAP_TOP + 30 < y < MAP_BOTTOM - 30

    origin_zone = (ox - 110, oy - 40, ox + 110, oy + 85)   # 起点の丸と起点名のあたりには置かない

    f_ring = font(FONT_BOLD, 26)
    _tmp = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    # before/after の点と、起点からそれらへの線(アニメーション中に通る範囲)
    pts_px = [to_px(view, *polar(r_, dd["brg"])) for dd in dests for r_ in (dd["dist"], dd["r_after"]) if r_]
    segs = [((ox, oy), q) for q in pts_px]

    def seg_dist(px, py, a, b):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        return math.hypot(px - (ax + u * dx), py - (ay + u * dy))

    def clearance(box):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        hw = (box[2] - box[0]) / 2
        d_pts = min((math.hypot(cx - x, cy - y) - hw for x, y in pts_px), default=999)
        d_seg = min((seg_dist(cx, cy, a_, b_) - 19 for a_, b_ in segs), default=999)
        return min(d_pts, d_seg)

    panel_pad = (PANEL[0] - 24, PANEL[1] - 24, PANEL[2] + 24, PANEL[3] + 24)
    # 距離ラベルは、各円の上で「点も線も無い、いちばん空いている所」に置く
    # (例: 東京発なら日本海側の空白)。円どうしでなるべく同じ方角にそろえる。
    circle_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    cd2 = ImageDraw.Draw(circle_layer)
    for km in radii:
        r = km * view["s"]
        cd2.ellipse([ox - r, oy - r, ox + r, oy + r], outline=(205, 220, 245, 70), width=2)
    canvas.alpha_composite(clip(circle_layer))
    ring_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring_layer)

    ring_boxes = []
    chosen = []
    prev_deg = None
    for km in radii:
        r = km * view["s"]
        lab = f"{km / base_kmh:g}時間｜{km:g}km"
        tw = _tmp.textlength(lab, font=f_ring)
        best = None
        for deg in range(0, 360, 4):
            th = math.radians(deg)
            x, y = ox + math.cos(th) * r, oy + math.sin(th) * r
            box = (x - tw / 2 - 12, y - 19, x + tw / 2 + 12, y + 19)
            if box[0] < SAFE_X or box[2] > CANVAS_W - SAFE_X or box[1] < MAP_TOP + 10 or box[3] > TABLE_TOP - 24:
                continue
            if not (bx0 + 10 < x < bx1 - 10 and by0 + 10 < y < by1 - 10):
                continue   # 地図を描いている範囲の外(ふわっと消えている所)には置かない
            if boxes_overlap(box, origin_zone) or boxes_overlap(box, panel_pad):
                continue
            if any(boxes_overlap(box, c[3]) for c in chosen):
                continue
            cl = clearance(box)
            if cl < 22:
                continue
            score = min(cl, 140)
            if prev_deg is not None:
                dd_ = abs((deg - prev_deg + 180) % 360 - 180)
                score -= dd_ * 0.6
            if best is None or score > best[0]:
                best = (score, lab, (x, y), box, deg)
        if best:
            _, lab, (x, y), box, deg = best
            chosen.append((lab, x, y, box))
            prev_deg = deg if prev_deg is None else prev_deg
    for lab, x, y, box in chosen:
        rd.rounded_rectangle(box, radius=19, fill=(10, 16, 34, 225), outline=(205, 220, 245, 120), width=2)
        rd.text((x, y), lab, font=f_ring, fill=(225, 235, 250, 255), anchor="mm")
        ring_boxes.append(box)
    # 地図の上下を背景色でフェードさせ、タイトルと下部パネルを読みやすくする
    fade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    fd = ImageDraw.Draw(fade)
    for y in range(0, MAP_TOP):
        a = int(235 * max(0.0, min(1.0, (MAP_TOP - y) / 90)))
        fd.line([(0, y), (CANVAS_W, y)], fill=BG_TOP + (a,))
    for y in range(MAP_BOTTOM, CANVAS_H):
        a = int(235 * max(0.0, min(1.0, (y - MAP_BOTTOM) / 60)))
        fd.line([(0, y), (CANVAS_W, y)], fill=BG_BOTTOM + (a,))
    canvas.alpha_composite(ring_layer)
    canvas.alpha_composite(fade)

    # タイトル(地図が動き出したら少し弱められるよう、別レイヤーに描く)
    title = Image.new("RGBA", (CANVAS_W, MAP_TOP), (0, 0, 0, 0))
    d = ImageDraw.Draw(title)
    d.text((CANVAS_W / 2, TITLE_Y1), config.get("title_line1", "近いのに遠い？遠いのに近い？"),
           font=font(FONT_BLACK, 50), fill=GOLD + (255,), anchor="mm")
    # 2行目「東京から鉄道で行くと…」: title_highlight の部分(鉄道で / 飛行機も使うと)だけ色を変える
    line2 = config.get("title_line2") or f"{o['pref']}からの『時間距離』"
    hl = config.get("title_highlight", "『時間距離』")
    segs = [(line2, WHITE)]
    if hl and hl in line2:
        a, b = line2.split(hl, 1)
        segs = [(a, WHITE), (hl, mode_color(config)), (b, WHITE)]
    size = 84
    while size > 40:
        f2 = font(FONT_BLACK, size)
        total = sum(d.textlength(t, font=f2) for t, _ in segs)
        if total <= CANVAS_W - 2 * SAFE_X:
            break
        size -= 2
    x = (CANVAS_W - total) / 2
    for t, c in segs:
        if t:
            d.text((x, TITLE_Y2), t, font=f2, fill=c + (255,), anchor="lm",
                   stroke_width=4, stroke_fill=(4, 8, 20, 255))
            x += d.textlength(t, font=f2)
    return canvas, title, ring_boxes


def mode_phrase(config):
    return "鉄道で" if config.get("mode") == "rail" else "飛行機も使うと"


def mode_color(config):
    return (120, 195, 255) if config.get("mode") == "best" else (255, 175, 110)


def draw_mode_icon(canvas, cx, cy, mode, color, h=34):
    """行頭の小さな乗り物アイコン(鉄道=新幹線の横顔 / 飛行機=機体シルエット)。回転はしない。"""
    d = ImageDraw.Draw(canvas, "RGBA")
    if mode == "rail":
        w = h * 1.9
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        # 車体(先頭が右に向かって流線形)
        d.polygon([(x0, y0 + h * 0.15), (x1 - h * 0.9, y0 + h * 0.15), (x1, y1 - h * 0.12),
                   (x1, y1), (x0, y1)], fill=color + (255,))
        d.rectangle([x0, y1 - h * 0.28, x1, y1 - h * 0.16], fill=(255, 255, 255, 255))
        for i in range(3):
            wx = x0 + h * 0.2 + i * h * 0.36
            d.rectangle([wx, y0 + h * 0.3, wx + h * 0.24, y0 + h * 0.5], fill=(20, 28, 48, 255))
        d.polygon([(x1 - h * 0.85, y0 + h * 0.25), (x1 - h * 0.45, y0 + h * 0.25), (x1 - h * 0.25, y0 + h * 0.5),
                   (x1 - h * 0.85, y0 + h * 0.5)], fill=(20, 28, 48, 255))
    else:
        s = h / 34
        pts = [(18, 0), (14, -3), (3, -3), (-6, -15), (-10, -15), (-5, -3), (-13, -3), (-17, -9), (-20, -9),
               (-17, 0), (-20, 9), (-17, 9), (-13, 3), (-5, 3), (-10, 15), (-6, 15), (3, 3), (14, 3)]
        d.polygon([(cx + x * s * 1.3, cy + y * s * 1.1) for x, y in pts], fill=color + (255,))


# ---------------------------------------------------------------- 描画パーツ
def dashed_line(draw, p0, p1, fill, width=2, dash=10, gap=8):
    x0, y0 = p0
    x1, y1 = p1
    L = math.hypot(x1 - x0, y1 - y0)
    if L < 1:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    t = 0.0
    while t < L:
        e = min(t + dash, L)
        draw.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * e, y0 + uy * e)], fill=fill, width=width)
        t = e + gap


def glow_line(canvas, p0, p1, color, width=7, glow=26):
    pad = glow + 10
    bx0, by0 = int(max(0, min(p0[0], p1[0]) - pad)), int(max(0, min(p0[1], p1[1]) - pad))
    bx1, by1 = int(min(CANVAS_W, max(p0[0], p1[0]) + pad)), int(min(CANVAS_H, max(p0[1], p1[1]) + pad))
    if bx1 <= bx0 or by1 <= by0:
        return
    lp0, lp1 = (p0[0] - bx0, p0[1] - by0), (p1[0] - bx0, p1[1] - by0)
    g = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
    ImageDraw.Draw(g).line([lp0, lp1], fill=color + (150,), width=glow)
    g = g.filter(ImageFilter.GaussianBlur(glow / 3))
    canvas.paste(g, (bx0, by0), g)
    ImageDraw.Draw(canvas, "RGBA").line([p0, p1], fill=color + (255,), width=width)


def boxes_overlap(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


# ---------------------------------------------------------------- 効果音「にゅ」
def synth_nyu(buf, start_sec, amt, seed=0, vol=0.55, stretch=1.0):
    """Web版と同じ考え方: +なら音程が上がる「にゅ↑」(伸びる)、-なら下がる「にゅ↓」(縮む)。
    変化がほとんど無い県も、小さな「にゅ」が鳴るよう最小量を持たせる。"""
    up = amt >= 0
    mag = min(1.6, max(0.12, abs(amt)))
    base = 330 * 2 ** ((((seed * 7) % 12) - 6) / 24)
    span = 2 ** (0.35 + mag * 0.9)
    f0, f1 = (base, base * span) if up else (base * span, base)
    length = (0.16 + mag * 0.1) * stretch
    n = int(length * SR)
    i0 = int(start_sec * SR)
    phase = phase2 = 0.0
    lp = 0.0
    glide = length * 0.75
    for i in range(n):
        t = i / SR
        u = min(1.0, t / glide)
        f = f0 * (f1 / f0) ** u
        phase = (phase + f / SR) % 1.0
        phase2 = (phase2 + 2 * f / SR) % 1.0
        tri = 4 * abs(phase - 0.5) - 1
        s = tri + 0.35 * math.sin(2 * math.pi * phase2)
        # 口の形(フォルマント)の代わりに、カットオフを動かす1次ローパス
        fc = (500 * (1300 / 500) ** (t / length)) if up else (1300 * (450 / 1300) ** (t / length))
        a = 1 - math.exp(-2 * math.pi * fc / SR)
        lp += a * (s - lp)
        # エンベロープ: 速い立ち上がり → 少し保持 → 減衰
        if t < 0.018:
            env = 0.0001 * (0.5 / 0.0001) ** (t / 0.018)
        elif t < length * 0.45:
            env = 0.5
        else:
            env = 0.5 * (0.0002 / 0.5) ** ((t - length * 0.45) / (length * 0.55))
        k = i0 + i
        if k < len(buf):
            buf[k] += lp * env * vol


def synth_chime(buf, start_sec, vol=0.22):
    """最後の before → after 表示に合わせた軽い「ポロン」。"""
    for j, f in enumerate((523.25, 659.25, 783.99, 1046.5)):
        s0 = start_sec + j * 0.07
        n = int(1.4 * SR)
        i0 = int(s0 * SR)
        for i in range(n):
            t = i / SR
            env = math.exp(-t * 3.2) * min(1.0, t / 0.01)
            k = i0 + i
            if k < len(buf):
                buf[k] += vol * env * (math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t))


def write_wav(path, buf):
    peak = max(1e-6, max(abs(x) for x in buf))
    g = 0.9 / peak if peak > 0.9 else 1.0
    pcm = array("h", (int(max(-1.0, min(1.0, x * g)) * 32767) for x in buf))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())




# ---------------------------------------------------------------- 判定と判定結果の表
def verdict_of(dd):
    """地図下のパネル・表・最後のまとめで共通に使う判定(5段階)。
    指数(= 直線距離 ÷ 200km/hで走った距離 × 100)で分ける:
      120以上 近くなった / 105以上 少し近くなった / 95〜105 ほぼそのまま / 80以上 少し遠くなった / 80未満 遠くなった"""
    ix = dd["ix"]
    if ix is None:
        return None
    if ix >= 120:
        return "近くなった"
    if ix >= 105:
        return "少し近くなった"
    if ix > 95:
        return "ほぼそのまま"
    if ix >= 80:
        return "少し遠くなった"
    return "遠くなった"


VERDICT_COLORS = {
    "近くなった": C_FAST,
    "少し近くなった": lerp_col(C_NEU, C_FAST, 0.55),
    "ほぼそのまま": C_NEU,
    "少し遠くなった": lerp_col(C_NEU, C_SLOW, 0.55),
    "遠くなった": C_SLOW,
}


def verdict_color(v):
    return VERDICT_COLORS.get(v, C_GRAY)


def draw_result_table(canvas, dests, t, active, mode):
    """画面下部の「都道府県｜判定結果」表(4段組み)。動いた県から順に判定が埋まっていく。"""
    d = ImageDraw.Draw(canvas, "RGBA")
    x0, y0, x1, y1 = SAFE_X, TABLE_TOP, CANVAS_W - SAFE_X, TABLE_BOTTOM
    d.rounded_rectangle([x0, y0, x1, y1], radius=16, fill=(8, 13, 30, 205), outline=(205, 220, 245, 60), width=2)
    pad = 12
    rows = math.ceil(len(dests) / TABLE_COLS)
    head_h = 30
    gw = (x1 - x0 - 2 * pad) / TABLE_COLS
    rh = (y1 - y0 - 2 * pad - head_h) / max(1, rows)
    f_head = font(FONT_BOLD, 16)
    f_row = font(FONT_BOLD, 20)
    f_row_b = font(FONT_BLACK, 20)
    pref_dx, verd_dx = 8, 78
    for c in range(TABLE_COLS):
        gx = x0 + pad + c * gw
        d.text((gx + pref_dx, y0 + pad + head_h / 2), "都道府県", font=f_head, fill=(150, 165, 188, 255), anchor="lm")
        d.text((gx + verd_dx, y0 + pad + head_h / 2), "判定結果", font=f_head, fill=(150, 165, 188, 255), anchor="lm")
        if c:
            d.line([(gx, y0 + pad), (gx, y1 - pad)], fill=(205, 220, 245, 40), width=1)
    d.line([(x0 + pad, y0 + pad + head_h), (x1 - pad, y0 + pad + head_h)], fill=(205, 220, 245, 70), width=1)
    for i, dd in enumerate(dests):
        c, r = divmod(i, rows)
        gx = x0 + pad + c * gw
        cy = y0 + pad + head_h + (r + 0.5) * rh
        judged = t >= dd["t0"]
        if active is dd:
            d.rounded_rectangle([gx + 3, cy - rh / 2 + 1, gx + gw - 3, cy + rh / 2 - 1], radius=6,
                                fill=verdict_color(verdict_of(dd)) + (48,))
        pref_col = (235, 240, 248, 255) if judged else (110, 122, 140, 255)
        d.text((gx + pref_dx, cy), dd["pref"], font=f_row, fill=pref_col, anchor="lm")
        if judged:
            v = verdict_of(dd)
            d.text((gx + verd_dx, cy), v or "—", font=f_row_b, fill=verdict_color(v) + (255,), anchor="lm")


# ---------------------------------------------------------------- ラベル配置
# 方針: 点(地点)は正確な位置のまま動かさず、文字だけを読みやすい場所へ逃がす。
#   - 文字どうしは絶対に重ねない
#   - 点・オレンジの線(スポーク)に重なる位置はコストを高くして避ける
#   - 離して置いた時は細い引き出し線で点と結ぶ
#   - 前の状態と同じ位置を優先し、1秒ごとの切り替えで文字が跳ね回らないようにする
LABEL_DIRS = [i * math.pi / 8 for i in range(16)]
LABEL_DISTS = [10, 30, 55, 85, 118]
LEADER_MIN = 34          # これ以上離したら引き出し線を引く


def seg_hits_box(p0, p1, box, pad=0):
    x0, y0, x1, y1 = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    if max(p0[0], p1[0]) < x0 or min(p0[0], p1[0]) > x1 or max(p0[1], p1[1]) < y0 or min(p0[1], p1[1]) > y1:
        return False
    # Liang-Barsky
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p0[0] - x0), (dx, x1 - p0[0]), (-dy, p0[1] - y0), (dy, y1 - p0[1])):
        if pp == 0:
            if qq < 0:
                return False
        else:
            r = qq / pp
            if pp < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                return False
    return True


def segs_cross(a, b, c, d):
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def circle_hits_box(cx, cy, r, box):
    nx = min(max(cx, box[0]), box[2])
    ny = min(max(cy, box[1]), box[3])
    return (nx - cx) ** 2 + (ny - cy) ** 2 < r * r


def place_labels(items, dots, spokes, soft_segs, reserved, prev, measure):
    """items: [{key, text, font, p:(x,y), r_dot, out:(ux,uy), bonus}] を優先順に。
    戻り値: key -> {box, text_xy, leader:(a,b)|None, cand}"""
    placed = {}
    boxes = list(reserved)
    leaders = []
    for it in items:
        w = measure(it["text"], it["font"]) + 8
        h = it.get("h", 32)
        px, py = it["p"]
        best = None
        for di, ang in enumerate(LABEL_DIRS):
            ux, uy = math.cos(ang), math.sin(ang)
            for dk, dist in enumerate(LABEL_DISTS):
                off = dist + it["r_dot"]
                cx = px + ux * off + ux * w / 2
                cy = py + uy * off + uy * h / 2
                box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                # 文字は必ずセーフエリア内(画面端の県も、文字だけ中央側へ逃がす)
                if box[0] < SAFE_X or box[2] > CANVAS_W - SAFE_X or box[1] < MAP_TOP - 10 or box[3] > MAP_BOTTOM + 8:
                    continue
                if any(boxes_overlap(box, b) for b in boxes):
                    continue
                cost = dist * 0.3
                # 外向き(起点から離れる向き)を少し優先
                cost += (1 - (ux * it["out"][0] + uy * it["out"][1])) * 5
                # 左右に置く方が読みやすい(真上・真下は少しだけ減点)
                cost += abs(uy) * 2
                for (dx_, dy_, dr, key) in dots:
                    if key != it["key"] and circle_hits_box(dx_, dy_, dr + 3, box):
                        cost += 40
                if circle_hits_box(px, py, it["r_dot"] + 2, box):
                    continue
                for (a, b, key) in spokes:
                    if seg_hits_box(a, b, box, pad=2):
                        cost += 14 if key != it["key"] else 30
                for (a, b) in soft_segs:
                    if seg_hits_box(a, b, box):
                        cost += 3
                leader = None
                if dist >= LEADER_MIN:
                    la = (px + ux * (it["r_dot"] + 3), py + uy * (it["r_dot"] + 3))
                    lb = (min(max(px, box[0]), box[2]), min(max(py, box[1]), box[3]))
                    if any(seg_hits_box(la, lb, b) for b in boxes):
                        continue
                    for (dx_, dy_, dr, key) in dots:
                        if key != it["key"] and seg_hits_box(la, lb, (dx_ - dr, dy_ - dr, dx_ + dr, dy_ + dr)):
                            cost += 12
                    leader = (la, lb)
                    # 引き出し線どうしが交差すると、どの点の文字か迷うので強く避ける
                    cost += 18 * sum(1 for (c1, c2) in leaders if segs_cross(la, lb, c1, c2))
                    for (a, b, key) in spokes:
                        if key != it["key"] and segs_cross(la, lb, a, b):
                            cost += 4
                # 文字のいちばん近くにある点が別の県だと紛らわしいので減点
                bcx, bcy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                d_own = math.hypot(bcx - px, bcy - py)
                if any(key != it["key"] and key != -1 and math.hypot(bcx - x_, bcy - y_) < d_own * 0.8
                       for (x_, y_, _, key) in dots):
                    cost += 22
                if prev.get(it["key"]) == (di, dk):
                    cost -= 9
                cost -= it.get("bonus", 0)
                if best is None or cost < best[0]:
                    best = (cost, box, leader, (di, dk))
        if best is None:
            continue
        _, box, leader, cand = best
        boxes.append(box)
        if leader:
            leaders.append(leader)
        placed[it["key"]] = {"box": box, "leader": leader, "cand": cand,
                             "text_xy": ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)}
    return placed


# ---------------------------------------------------------------- メイン
def render_time_cartogram(config, out_dir="output", fast_preview=False, out_name=None,
                          japan_path="data/japan_land.geojson"):
    slug = out_name or config.get("slug", "time_cartogram")
    os.makedirs(out_dir, exist_ok=True)
    fps = 10 if fast_preview else FPS
    move = float(config.get("move_sec", 0.6))
    gap = float(config.get("gap_sec", 1.0))
    outro = float(config.get("outro_hold_sec", 5.0))
    o = config["origin"]
    mode = config.get("mode", "rail")

    dests = prepare(config)
    for k, dd in enumerate(dests):
        dd["key"] = k
    view = choose_layout(dests)
    base, title_full, ring_boxes = render_base(config, dests, view, japan_path)
    # 地図が動いている間はタイトルを少し弱める(不透明度55%)
    r_, g_, b_, a_ = title_full.split()
    title_dim = Image.merge("RGBA", (r_, g_, b_, a_.point(lambda v: int(v * 0.55))))
    preview = base.copy()
    preview.alpha_composite(title_full, (0, 0))
    preview.convert("RGB").save(f"{out_dir}/{slug}_base_map.png")

    last_t0 = dests[-1]["t0"] if dests else 0
    # 最後の見せ場: 最後の県が動く → 2秒 → before の地図に一瞬で戻す → 全県いっせいに after へ
    hold_sec = float(config.get("final_hold_sec", 2.0))
    rewind_sec = 0.35
    before_hold = float(config.get("before_hold_sec", 1.0))
    all_move = float(config.get("all_move_sec", 2.0))
    rw_start = last_t0 + move + hold_sec
    rw_end = rw_start + rewind_sec
    all_start = rw_end + before_hold
    all_end = all_start + all_move
    outro_start = all_end + 0.4
    total = outro_start + outro
    n_frames = int(round(total * fps))

    # 「いちばん近く/遠くなった」は、隣の県のような150km未満の近距離は除いて選ぶ
    # (数十kmの区間は乗車時間より乗換・待ちの比重が大きく、指数が極端に小さくなるため)
    reach = [d for d in dests if d["ix"] is not None and d["dist"] >= 150] or \
            [d for d in dests if d["ix"] is not None]
    closer = max(reach, key=lambda d: d["ix"]) if reach else None
    farther = min(reach, key=lambda d: d["ix"]) if reach else None

    # ---- 音
    buf = array("f", bytes(4 * int((total + 0.5) * SR)))
    for k, d in enumerate(dests):
        if d["r_after"] is None:
            continue
        amt = math.log2((d["r_after"] + 1) / (d["dist"] + 1))
        synth_nyu(buf, d["t0"], amt, seed=k)
    # 全県いっせいの「にゅーん」: 伸びる音と縮む音を少しずつずらして重ねる
    amts = [math.log2((d["r_after"] + 1) / (d["dist"] + 1)) for d in dests if d["r_after"]]
    ups = sorted([a for a in amts if a >= 0], reverse=True)[:4]
    downs = sorted([a for a in amts if a < 0])[:4]
    for j, a in enumerate(ups + downs):
        synth_nyu(buf, all_start + j * 0.06, a * 1.3, seed=j * 3, vol=0.4, stretch=3.2)
    synth_chime(buf, outro_start)
    wav_path = Path(out_dir) / f"{slug}_audio.wav"
    write_wav(wav_path, buf)

    f_label = font(FONT_BOLD, 27)
    f_label_big = font(FONT_BLACK, 36)
    f_origin = font(FONT_BLACK, 38)
    f_pair = font(FONT_BLACK, 60)
    f_small = font(FONT_BOLD, 28)
    f_count = font(FONT_BOLD, 26)
    f_legend = font(FONT_BOLD, 30)

    ox, oy = to_px(view, 0, 0)
    _m = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    measure = lambda text, fnt: _m.textlength(text, font=fnt)
    real_px = {dd["key"]: to_px(view, *polar(dd["dist"], dd["brg"])) for dd in dests}
    after_px = {dd["key"]: (to_px(view, *polar(dd["r_after"], dd["brg"])) if dd["r_after"] else real_px[dd["key"]])
                for dd in dests}
    olab_w = measure(o["pref"], f_origin)
    olab_x = min(max(ox, SAFE_X + olab_w / 2), CANVAS_W - SAFE_X - olab_w / 2)
    origin_label_box = (olab_x - olab_w / 2 - 6, oy + 24, olab_x + olab_w / 2 + 6, oy + 76)
    count_box = (CANVAS_W - SAFE_X - 110, MAP_TOP - 4, CANVAS_W - SAFE_X, MAP_TOP + 36)
    reserved_base = [origin_label_box, count_box, (ox - 30, oy - 30, ox + 30, oy + 30), PANEL] + list(ring_boxes)

    # ---- 状態ごと(k県目まで動いた状態)のラベル配置を先に計算しておく
    def layout_for_state(k, prev, window=None):
        pos = {dd["key"]: (after_px[dd["key"]] if dd["key"] < k else real_px[dd["key"]]) for dd in dests}
        dots = [(x, y, 6, key) for key, (x, y) in pos.items()] + [(ox, oy, 28, -1)]
        spokes = [((ox, oy), pos[key], key) for key in pos]
        soft = [(real_px[key], after_px[key]) for key in range(k)]
        items = []
        for dd in dests:
            key = dd["key"]
            if window is not None and key not in window:
                continue
            moved = key < k
            px, py = pos[key]
            vx, vy = px - ox, py - oy
            L = math.hypot(vx, vy) or 1
            bonus = (abs(math.log2(dd["ix"] / 100)) * 2 if (moved and dd["ix"]) else 0)
            items.append({"key": key, "text": dd["pref"], "font": f_label, "p": (px, py), "r_dot": 6,
                          "out": (vx / L, vy / L), "bonus": bonus, "moved": moved})
        # 置きにくい所(起点の近く・密集地)から先に置くと全体が収まりやすい
        def density(it):
            px, py = it["p"]
            return -sum(1 for (x, y, _, _) in dots if abs(x - px) < 90 and abs(y - py) < 60)
        items.sort(key=lambda it: (not it["moved"], density(it)))
        return place_labels(items, dots, spokes, soft, reserved_base, prev, measure)

    # 県名は「いま動く県」の前後 label_window 県ぶんだけ出す(既定3。-1で全県)。
    # 表示する県が少ないので、文字を点のすぐそばに置きやすくなる。
    win_n = int(config.get("label_window", 3))
    print("laying out labels ...")
    layouts = []          # layouts[a + 1]: a番目の県が主役の時の配置(a = -1 は動き出す前)
    prev = {}
    for a in range(-1, len(dests)):
        if win_n < 0:
            window = None
        else:
            window = {i for i in range(a - win_n, a + win_n + 1) if 0 <= i < len(dests) and i != a}
        L_ = layout_for_state(a + 1, prev, window)
        prev.update({key: v["cand"] for key, v in L_.items()})
        layouts.append(L_)

    out_path = f"{out_dir}/{slug}.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{CANVAS_W}x{CANVAS_H}", "-r", str(fps), "-i", "-",
           "-i", str(wav_path),
           "-c:v", "libx264", "-preset", "ultrafast" if fast_preview else "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest",
           "-movflags", "+faststart", out_path]
    print(f"rendering {n_frames} frames ({total:.1f}s @ {fps}fps) ...")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    first_t0 = dests[0]["t0"] if dests else 0
    base_rgb = base.convert("RGB")
    last_frame = None
    for fi in range(n_frames):
        t = fi / fps
        # フレームはRGBで描く(ImageDraw の "RGBA" モードで半透明の線・塗りが正しく重なるように)
        canvas = base_rgb.copy()
        d = ImageDraw.Draw(canvas, "RGBA")
        in_outro = t >= outro_start

        # 現在「主役」の県(直近に動き始めた県)
        active = None
        if t < rw_start:
            for dd in dests:
                if dd["t0"] <= t:
                    active = dd
        # 各県の現在位置(フェーズ: 1県ずつ → 2秒待ち → before に戻す → いっせいに after)
        rewinding = rw_start <= t < rw_end
        before_view = rw_end <= t < all_start
        all_moving = all_start <= t < all_end
        states = []
        for dd in dests:
            if t < rw_start:
                u = (t - dd["t0"]) / move
                started = t >= dd["t0"]
                e = ease_out_back(u) if started else 0.0
            elif rewinding:
                started = False
                e = 1 - ease_in_out((t - rw_start) / rewind_sec)
                u = e
            elif before_view:
                started, e, u = False, 0.0, 0.0
            else:
                u = (t - all_start) / all_move
                started = True
                e = ease_nyuru(u)
            if dd["r_after"] is None:
                r = dd["dist"]
                col_t = 0.0
            else:
                r = lerp(dd["dist"], dd["r_after"], e)
                col_t = max(0.0, min(1.0, u))
            p = to_px(view, *polar(r, dd["brg"]))
            real = real_px[dd["key"]]
            if dd["r_after"] is None:
                col = C_GRAY if started else C_NEU
            else:
                col = lerp_col(C_NEU, dd["color"], col_t)
            states.append((dd, p, real, started, col))
        pos_now = {s[0]["key"]: s[1] for s in states}

        # before の位置(○)と、そこからの点線
        for dd, p, real, started, col in states:
            if started and dd["r_after"] is not None:
                a = 150 if in_outro else 105
                dashed_line(d, real, p, fill=(190, 200, 215, a), width=1)
                d.ellipse([real[0] - 4, real[1] - 4, real[0] + 4, real[1] + 4],
                          outline=(215, 225, 240, 190 if in_outro else 140), width=1)

        # 起点からの線(スポーク)
        for dd, p, real, started, col in states:
            if active is dd:
                continue
            if started:
                d.line([(ox, oy), p], fill=col + (230,), width=3)
            else:
                d.line([(ox, oy), p], fill=col + (60,), width=1)
        if active is not None:
            glow_line(canvas, (ox, oy), pos_now[active["key"]],
                      next(s[4] for s in states if s[0] is active), width=4, glow=18)
            d = ImageDraw.Draw(canvas, "RGBA")

        # ラベルの配置: 何県目まで動き終わったか(k)に応じた事前計算済みの配置を使う
        k_done = sum(1 for dd in dests if t >= dd["t0"] + move)
        if t >= rw_start:
            lay = {}          # 最後の見せ場(before → いっせいに after → まとめ)では県名を出さない
        elif rewinding or all_moving:
            lay = {}          # 全県が動いている間は県名を消す(点と文字がずれて見えるのを防ぐ)
        else:
            a_idx = active["key"] if active is not None else -1
            lay = layouts[a_idx + 1]

        # 引き出し線(点の下に描く)
        for dd, p, real, started, col in states:
            key = dd["key"]
            if active is dd or key not in lay:
                continue
            L_ = lay[key]
            if L_["leader"]:
                d.line(list(L_["leader"]), fill=(200, 210, 228, 170), width=1)

        # 点(●)
        for dd, p, real, started, col in states:
            is_act = active is dd
            if is_act:
                r = 7
                d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=col + (255,),
                          outline=(255, 255, 255, 255), width=2)
            else:
                r = 4 if started else 3
                d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=col + (255 if started else 170,))

        # 起点
        d.ellipse([ox - 16, oy - 16, ox + 16, oy + 16], fill=(255, 255, 255, 255), outline=(5, 8, 18, 255), width=4)
        d.ellipse([ox - 26, oy - 26, ox + 26, oy + 26], outline=GOLD + (220,), width=3)
        d.text((olab_x, oy + 50), o["pref"], font=f_origin, fill=GOLD + (255,), anchor="mm",
               stroke_width=4, stroke_fill=(4, 8, 20, 255))

        # 県名ラベル
        for dd, p, real, started, col in states:
            key = dd["key"]
            if active is dd or key not in lay:
                continue
            L_ = lay[key]
            fill = (255, 255, 255, 255) if started else (196, 204, 216, 255)
            if dd["r_after"] is None and started:
                fill = (150, 158, 170, 255)
            d.text(L_["text_xy"], dd["pref"], font=f_label, fill=fill, anchor="mm",
                   stroke_width=4, stroke_fill=(4, 8, 20, 255))

        # 主役の県のラベル(大きめ)。いまの点の位置に合わせて毎フレーム置き場所を探す
        if active is not None:
            key = active["key"]
            p = pos_now[key]
            vx, vy = p[0] - ox, p[1] - oy
            Ln = math.hypot(vx, vy) or 1
            others = [v["box"] for kk, v in lay.items() if kk != key]
            dots_now = [(x, y, 6, kk) for kk, (x, y) in pos_now.items()] + [(ox, oy, 28, -1)]
            one = place_labels([{"key": key, "text": active["pref"], "font": f_label_big, "p": p, "r_dot": 9,
                                 "out": (vx / Ln, vy / Ln), "h": 44}],
                               dots_now, [((ox, oy), p, key)], [], reserved_base[:2] + others,
                               {}, measure)
            if key in one:
                L_ = one[key]
                if L_["leader"]:
                    d.line(list(L_["leader"]), fill=(255, 255, 255, 220), width=2)
                bx = L_["box"]
                d.rounded_rectangle(bx, radius=10, fill=(10, 16, 34, 200))
                d.text(L_["text_xy"], active["pref"], font=f_label_big, fill=WHITE + (255,), anchor="mm",
                       stroke_width=3, stroke_fill=(4, 8, 20, 255))

        # 進み具合(右上)
        done = sum(1 for s in states if s[3])
        if t < rw_start:
          d.text((CANVAS_W - SAFE_X, MAP_TOP + 4), f"{done} / {len(dests)}", font=f_count,
               fill=(170, 185, 205, 255), anchor="ra")

        # タイトル: 地図が動いている間は少し弱め、最後のまとめで元に戻す
        if in_outro:
            k_t = 1.0 - min(1.0, (t - outro_start) / 0.5)
        else:
            k_t = min(1.0, max(0.0, (t - first_t0) / 0.5))
        if k_t <= 0:
            canvas.paste(title_full, (0, 0), title_full)
        elif k_t >= 1:
            canvas.paste(title_dim, (0, 0), title_dim)
        else:
            tl = Image.blend(title_full, title_dim, k_t)
            canvas.paste(tl, (0, 0), tl)
        d = ImageDraw.Draw(canvas, "RGBA")

        # 判定結果の表(補助情報)
        draw_result_table(canvas, dests, t, active, mode)
        d = ImageDraw.Draw(canvas, "RGBA")

        # 「東京 → ○○」・BEFORE/AFTER・まとめの枠(PANEL)。地図の空いた隅か、地図の下に置く
        px0, py0, px1, py1 = PANEL
        pcx, pw_in = (px0 + px1) / 2, (px1 - px0) - 36
        bg_a = 200
        if in_outro:
            bg_a = int(215 * min(1.0, (t - outro_start) / 0.5))
        d.rounded_rectangle([px0, py0, px1, py1], radius=22, fill=(8, 13, 30, bg_a),
                            outline=(205, 220, 245, int(bg_a * 0.35)), width=2)

        def fit(fnt_path, size, text, maxw):
            f_ = font(fnt_path, size)
            while measure(text, f_) > maxw and size > 16:
                size -= 2
                f_ = font(fnt_path, size)
            return f_

        if in_outro:
            a = int(255 * min(1.0, (t - outro_start) / 0.5))
            # 凡例「○ 実際の位置 → ● 時間で見た位置」(記号は図形で描く)
            ly = py0 + 30
            f_lg = font(FONT_BOLD, 26)
            parts = [("○", None), ("実際の位置", f_lg), ("→", f_lg), ("●", None), ("時間で見た位置", f_lg)]
            widths = [22 if fn is None else measure(tx, fn) for tx, fn in parts]
            gapw = 12
            x = pcx - (sum(widths) + gapw * (len(parts) - 1)) / 2
            for (tx, fn), w in zip(parts, widths):
                if tx == "○":
                    d.ellipse([x + 4, ly - 7, x + 18, ly + 7], outline=(220, 230, 245, a), width=2)
                elif tx == "●":
                    d.ellipse([x + 4, ly - 7, x + 18, ly + 7], fill=C_SLOW + (a,))
                else:
                    d.text((x, ly), tx, font=fn, fill=(225, 233, 245, a), anchor="lm",
                           stroke_width=3, stroke_fill=(4, 8, 20, a))
                x += w + gapw
            if closer and farther:
                # 表・地図の判定と食い違わないよう、判定が「近くなった」系でない県を
                # 「いちばん近くなった」とは書かない
                w1 = "近くなった" if verdict_of(closer) in ("近くなった", "少し近くなった") else "速かった"
                w2 = "遠くなった" if verdict_of(farther) in ("遠くなった", "少し遠くなった") else "遅かった"
                for (y, word, dd_, col) in ((py0 + 84, w1, closer, C_FAST), (py0 + 138, w2, farther, C_SLOW)):
                    k = 1.0
                    while True:
                        f_pre = font(FONT_BOLD, int(30 * k))
                        f_judge = font(FONT_BLACK, int(42 * k))
                        f_pref = font(FONT_BLACK, int(50 * k))
                        parts = [("いちばん", f_pre, (230, 236, 244)), (word, f_judge, col), (dd_["pref"], f_pref, col)]
                        icon_w = int(70 * k)
                        widths = [measure(tx, fn) for tx, fn, _ in parts]
                        total_w = icon_w + sum(widths) + 8 + 24
                        if total_w <= pw_in or k < 0.6:
                            break
                        k -= 0.05
                    x = pcx - total_w / 2
                    draw_mode_icon(canvas, x + 28 * k, y, mode, col, h=int(28 * k))
                    d = ImageDraw.Draw(canvas, "RGBA")
                    x += icon_w
                    for i, ((tx, fn, c), w) in enumerate(zip(parts, widths)):
                        d.text((x, y), tx, font=fn, fill=c + (a,), anchor="lm",
                               stroke_width=4, stroke_fill=(4, 8, 20, a))
                        x += w + (8 if i == 0 else 24)
        elif t >= rw_start:
            if rewinding or before_view:
                cap, sub, col = "BEFORE", "実際の距離", (225, 233, 245)
            else:
                cap, sub, col = "AFTER", "時間距離", mode_color(config)
            d.text((pcx, py0 + 58), cap, font=font(FONT_BLACK, 62), fill=col + (255,), anchor="mm",
                   stroke_width=4, stroke_fill=(4, 8, 20, 255))
            d.text((pcx, py0 + 126), sub, font=font(FONT_BLACK, 44), fill=WHITE + (255,), anchor="mm",
                   stroke_width=4, stroke_fill=(4, 8, 20, 255))
        elif active is not None:
            dd = active
            pop = ease_out_back(min(1.0, (t - dd["t0"]) / 0.25), s=2.2)
            txt = f"{o['pref']} → {dd['pref']}"
            f_full = fit(FONT_BLACK, 56, txt, pw_in)
            size = max(10, int(f_full.size * (0.7 + 0.3 * pop)))
            d.text((pcx, py0 + 55), txt, font=font(FONT_BLACK, size),
                   fill=WHITE + (255,), anchor="mm", stroke_width=4, stroke_fill=(4, 8, 20, 255))
            if dd["minutes"] is None:
                line = "鉄道では行けません"
                col = C_GRAY
            else:
                line = {"近くなった": "近くなった！", "遠くなった": "遠くなった…"}.get(verdict_of(dd), verdict_of(dd))
                col = verdict_color(verdict_of(dd))
            f_v = fit(FONT_BLACK, 48, line, pw_in - 50)
            tw = measure(line, f_v)
            x = pcx - tw / 2
            yv = py0 + 124
            if dd.get("air"):
                # 飛行機を使った県は、先頭に飛行機アイコン
                x += 26
                draw_mode_icon(canvas, x - 44, yv, "best", col, h=30)
                d = ImageDraw.Draw(canvas, "RGBA")
            d.text((x, yv), line, font=f_v, fill=col + (255,), anchor="lm",
                   stroke_width=4, stroke_fill=(4, 8, 20, 255))
        else:
            d.text((pcx, py0 + 58), "まずは実際の距離", font=fit(FONT_BLACK, 54, "まずは実際の距離", pw_in),
                   fill=WHITE + (255,), anchor="mm", stroke_width=4, stroke_fill=(4, 8, 20, 255))
            sub = "1時間 = 200km を基準に描き直すと…"
            d.text((pcx, py0 + 124), sub, font=fit(FONT_BOLD, 28, sub, pw_in),
                   fill=(200, 210, 224, 255), anchor="mm")

        frame = canvas.convert("RGB")
        proc.stdin.write(frame.tobytes())
        last_frame = frame
        if fi % 60 == 0:
            print(f"  frame {fi}/{n_frames}")

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg のエンコードに失敗しました")
    if last_frame is not None:
        last_frame.save(f"{out_dir}/{slug}_final.png")
    print("done:", out_path)
    return out_path
