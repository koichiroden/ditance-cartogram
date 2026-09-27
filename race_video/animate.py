# -*- coding: utf-8 -*-
"""
電車最速バトル動画レンダラー(汎用版)。
ベースマップの上に、毎フレーム: 進行済みルートのハイライト, 非回転の車両
アイコン, 通過駅ポップアップ, 実況テロップ, スコアボードを描画する。
フレームをPNG連番で書き出し、最後にffmpegでmp4にエンコードする。
"""
import base64
import binascii
import io
import math
import os
import re
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .geo import project, CANVAS_W, CANVAS_H, SAFE_BOTTOM_Y, MAP_TOP, MAP_BOTTOM
from .motion import RouteMotion
from .commentary import build_events, write_script_files
from .flight_route import leg_icon_at, leg_status_at, leg_icon_path_at
from . import fonts as _fonts

FONT_BOLD, _FONT_REGULAR, FONT_BLACK = _fonts.resolve()

FPS = 30
OUTRO_HOLD_SEC = 4.0


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def ease_out_back(x):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


_icon_image_cache = {}


_DATA_URI_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,")


def load_icon_image(path, size=54):
    """任意の画像を、指定サイズ(幅 size*1.6)にリサイズして読み込む。
    "path" には2種類を指定できる:
      1) ファイルパス(透過PNG, 進行方向=右向き推奨) — 従来通り
      2) "data:image/png;base64,...." のような data URI — ブラウザの
         config作成ツール(レースビルダー)で画像ファイルを直接選んだ場合、
         `assets/` に置かず、選んだ画像をそのままconfigのJSONに埋め込める
         ようにするためのもの。
    同じ(path, size)の組み合わせはキャッシュを使い回す(動画は数百
    フレームあるため、毎フレーム開き直さない/デコードし直さない)。
    path が空/不正/存在しない場合はNoneを返す(呼び出し側は描画
    プレースホルダーにフォールバックする)。"""
    if not path:
        return None
    key = (path, size)
    if key in _icon_image_cache:
        return _icon_image_cache[key]
    img = None
    raw = None
    if _DATA_URI_RE.match(path):
        b64_part = path.split(",", 1)[1] if "," in path else ""
        try:
            raw_bytes = base64.b64decode(b64_part, validate=False)
            raw = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        except (binascii.Error, ValueError, OSError):
            raw = None
    elif os.path.exists(path):
        raw = Image.open(path).convert("RGBA")
    if raw is not None:
        w = size * 1.6
        h = w * raw.height / raw.width
        img = raw.resize((max(1, int(w)), max(1, int(h))))
    _icon_image_cache[key] = img
    return img


def load_icon(route_cfg, size=54):
    """config で icon_path (透過PNG, 進行方向=右向き推奨)が指定されていれば読み込む。
    無ければ簡易な非回転プレースホルダーアイコンを描く。どちらも回転はさせない
    (要件: 車両アイコンは回転しない)。"""
    return load_icon_image(route_cfg.get("icon_path"), size=size)


def resolve_icon_img(route, real_min, kind, size):
    """このルートの real_min 時点で使う車両アイコン画像を解決する。
    優先順位: (1) 現在のleg専用のicon_path(乗り換えで路線が変わる区間、
    飛行機、バス=市内移動など、legごとに見た目を変えたい場合) →
    (2) ルート全体の既定icon_path(train/未指定のアイコンのみに適用。
    飛行機やバス等、明示的に別の種別を選んでいるのに既定画像を流用すると
    見た目が合わないため) → (3) None(呼び出し側がベクターの
    プレースホルダーアイコンを描く)。"""
    leg_path = leg_icon_path_at(route, real_min)
    if leg_path:
        return load_icon_image(leg_path, size=size)
    if kind in (None, "train"):
        return load_icon_image(route.get("icon_path"), size=size)
    return None


def draw_train_icon(canvas_rgba, cx, cy, color, icon_img=None, size=54):
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return

    w, h = size, int(size * 0.62)
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    d.rounded_rectangle([x0 + 3, y0 + 6, x1 + 3, y1 + 6], radius=h / 2, fill=(0, 0, 0, 90))
    d.rounded_rectangle([x0, y0, x1, y1], radius=h / 2, fill=color + (255,), outline=(255, 255, 255, 230), width=3)
    win_w = w * 0.16
    gap = w * 0.06
    start = x0 + w * 0.16
    for i in range(3):
        wx0 = start + i * (win_w + gap)
        d.rounded_rectangle([wx0, y0 + h * 0.22, wx0 + win_w, y0 + h * 0.6], radius=3,
                             fill=(235, 245, 255, 255))
    d.ellipse([x1 - h * 0.22, y0 + h * 0.62, x1 - h * 0.02, y0 + h * 0.9], fill=(255, 240, 150, 255))
    canvas_rgba.alpha_composite(layer)


def draw_plane_icon(canvas_rgba, cx, cy, color, icon_img=None, size=54):
    """飛行機アイコン(進行方向には回転させない。要件どおり車両アイコンは
    常に同じ向きで表示する、という既存仕様を飛行機にもそのまま適用)。
    icon_img(config の icon_path で指定した透過PNG)があれば、それを
    そのまま貼り付ける。"""
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return
    w = size
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - w * 0.16, cy - w * 0.16 + 4, cx + w * 0.16, cy + w * 0.16 + 4], fill=(0, 0, 0, 70))
    body = [(cx + w * 0.55, cy), (cx - w * 0.22, cy - w * 0.19),
            (cx - w * 0.08, cy), (cx - w * 0.22, cy + w * 0.19)]
    d.polygon(body, fill=color + (255,), outline=(255, 255, 255, 230))
    wing_top = [(cx, cy - w * 0.04), (cx - w * 0.1, cy - w * 0.42),
                (cx - w * 0.22, cy - w * 0.34), (cx - w * 0.1, cy + w * 0.02)]
    wing_bot = [(cx, cy + w * 0.04), (cx - w * 0.1, cy + w * 0.42),
                (cx - w * 0.22, cy + w * 0.34), (cx - w * 0.1, cy - w * 0.02)]
    d.polygon(wing_top, fill=color + (255,), outline=(255, 255, 255, 200))
    d.polygon(wing_bot, fill=color + (255,), outline=(255, 255, 255, 200))
    canvas_rgba.alpha_composite(layer)


def draw_wait_icon(canvas_rgba, cx, cy, color, icon_img=None, size=44):
    """待機中(搭乗待ち・降機後の待機など)のアイコン。時計のシンプルな形。
    icon_imgがあればそれを貼り付ける。"""
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = size * 0.32
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color + (255,), width=5,
               fill=(15, 18, 30, 210))
    d.line([(cx, cy), (cx, cy - r * 0.6)], fill=color + (255,), width=4)
    d.line([(cx, cy), (cx + r * 0.4, cy + r * 0.2)], fill=color + (255,), width=4)
    canvas_rgba.alpha_composite(layer)


def draw_bus_icon(canvas_rgba, cx, cy, color, icon_img=None, size=48):
    """バス(市内移動など)のアイコン。icon_imgがあればそれを貼り付ける。"""
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    w, h = size, size * 0.55
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    d.rounded_rectangle([x0, y0, x1, y1], radius=8, fill=color + (255,), outline=(255, 255, 255, 230), width=3)
    win_w = w * 0.22
    for i, wx in enumerate([x0 + w * 0.14, x0 + w * 0.46]):
        d.rounded_rectangle([wx, y0 + h * 0.18, wx + win_w, y0 + h * 0.55], radius=3, fill=(235, 245, 255, 255))
    d.ellipse([x0 + w * 0.12, y1 - h * 0.12, x0 + w * 0.28, y1 + h * 0.12], fill=(20, 20, 25, 255))
    d.ellipse([x1 - w * 0.28, y1 - h * 0.12, x1 - w * 0.12, y1 + h * 0.12], fill=(20, 20, 25, 255))
    canvas_rgba.alpha_composite(layer)


def draw_walk_icon(canvas_rgba, cx, cy, color, icon_img=None, size=40):
    """徒歩のアイコン。icon_imgがあればそれを貼り付ける。"""
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    s = size / 40.0
    r = 6 * s
    d.ellipse([cx - r, cy - 18 * s - r, cx + r, cy - 18 * s + r], fill=color + (255,))
    d.line([(cx, cy - 12 * s), (cx, cy + 4 * s)], fill=color + (255,), width=int(5 * s))
    d.line([(cx, cy - 6 * s), (cx - 11 * s, cy - 2 * s)], fill=color + (255,), width=int(4 * s))
    d.line([(cx, cy - 6 * s), (cx + 11 * s, cy - 2 * s)], fill=color + (255,), width=int(4 * s))
    d.line([(cx, cy + 4 * s), (cx - 9 * s, cy + 18 * s)], fill=color + (255,), width=int(4 * s))
    d.line([(cx, cy + 4 * s), (cx + 7 * s, cy + 18 * s)], fill=color + (255,), width=int(4 * s))
    canvas_rgba.alpha_composite(layer)


def draw_monorail_icon(canvas_rgba, cx, cy, color, icon_img=None, size=54):
    """モノレールのアイコン。icon_imgがあればそれを貼り付ける。"""
    if icon_img is not None:
        w, h = icon_img.size
        canvas_rgba.alpha_composite(icon_img, (int(cx - w / 2), int(cy - h / 2)))
        return
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    w, h = size, size * 0.5
    d.line([(cx - w * 0.6, cy + h * 0.5), (cx + w * 0.6, cy + h * 0.5)],
           fill=color + (140,), width=3)
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h * 0.2
    d.rounded_rectangle([x0, y0, x1, y1], radius=h * 0.55, fill=color + (255,),
                         outline=(255, 255, 255, 230), width=3)
    d.rectangle([cx - 2, y1, cx + 2, cy + h * 0.5], fill=color + (255,))
    canvas_rgba.alpha_composite(layer)


def draw_icon_by_kind(kind, canvas_rgba, cx, cy, color, icon_img=None, size=54):
    """アイコン種別(train/plane/wait/bus/walk/monorail)ごとの描画を振り分ける。
    icon_img(resolve_icon_img() で解決したconfigのicon_path画像)が
    あれば、種別を問わずその画像をそのまま貼り付ける(乗り換えでの
    アイコン切り替え・飛行機/バス=市内移動の画像選択に対応するため)。
    無ければ従来通り、種別ごとのベクタープレースホルダーを描く。"""
    if kind == "plane":
        draw_plane_icon(canvas_rgba, cx, cy, color, icon_img=icon_img, size=size)
    elif kind == "wait":
        draw_wait_icon(canvas_rgba, cx, cy, color, icon_img=icon_img, size=size)
    elif kind == "bus":
        draw_bus_icon(canvas_rgba, cx, cy, color, icon_img=icon_img, size=size)
    elif kind == "walk":
        draw_walk_icon(canvas_rgba, cx, cy, color, icon_img=icon_img, size=size)
    elif kind == "monorail":
        draw_monorail_icon(canvas_rgba, cx, cy, color, icon_img=icon_img, size=size)
    else:  # "train" / None / 未知の種別はプレースホルダー電車アイコンに揃える
        draw_train_icon(canvas_rgba, cx, cy, color, icon_img=icon_img, size=size)


def draw_progress_route(canvas_rgba, proj, route, motion, real_min, color):
    km_now = motion.km_at(real_min)
    cd = route["cum_dist"]
    poly = route["polyline"]
    pts = []
    for i, d in enumerate(cd):
        if d > km_now:
            break
        pts.append(project(proj, poly[i][0], poly[i][1]))
    lon, lat = motion.lonlat_at(real_min)
    pts.append(project(proj, lon, lat))
    if len(pts) >= 2:
        # GaussianBlurはピクセル数に比例して重いので、キャンバス全体(1080x1920)
        # ではなく、線の外接矩形+余白だけを切り出してぼかす(見た目は同じまま、
        # CPUの弱い環境でも1フレームあたりのコストを大きく減らせる)。
        cw, ch = canvas_rgba.size
        pad = 60  # ぼかし半径8 + 線幅22 を考慮した余裕
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bx0 = max(0, int(min(xs) - pad))
        by0 = max(0, int(min(ys) - pad))
        bx1 = min(cw, int(max(xs) + pad))
        by1 = min(ch, int(max(ys) + pad))
        if bx1 <= bx0 or by1 <= by0:
            return
        local_pts = [(x - bx0, y - by0) for x, y in pts]

        crop = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
        gd = ImageDraw.Draw(crop)
        gd.line(local_pts, fill=color + (140,), width=22, joint="curve")
        crop = crop.filter(ImageFilter.GaussianBlur(8))
        canvas_rgba.alpha_composite(crop, (bx0, by0))

        crop2 = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
        ld = ImageDraw.Draw(crop2)
        ld.line(local_pts, fill=(255, 255, 255, 235), width=6, joint="curve")
        canvas_rgba.alpha_composite(crop2, (bx0, by0))


def draw_popup(canvas_rgba, proj, station, elapsed, color):
    x, y = project(proj, station["lon"], station["lat"])
    if elapsed < 0.18:
        t = elapsed / 0.18
        scale = 0.3 + 1.9 * ease_out_back(t)
        alpha = int(255 * min(1.0, t * 1.3))
    elif elapsed < 0.42:
        t = (elapsed - 0.18) / 0.24
        scale = 2.2 - 1.2 * t
        alpha = 255
    elif elapsed < 1.25:
        scale = 1.0
        alpha = 255
    elif elapsed < 1.6:
        t = (elapsed - 1.25) / 0.35
        scale = 1.0
        alpha = int(255 * (1 - t))
    else:
        return

    f = font(FONT_BLACK, int(34 * scale), index=0)
    text = station["name"]
    bbox = f.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 22 * scale, 12 * scale
    bx0, by0 = x - tw / 2 - pad_x, y - 70 * scale - th - pad_y
    bx1, by1 = x + tw / 2 + pad_x, y - 70 * scale + pad_y

    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([bx0, by0, bx1, by1], radius=14 * scale,
                          fill=(15, 18, 30, int(230 * (alpha / 255))),
                          outline=color + (alpha,), width=3)
    ld.polygon([(x - 10 * scale, by1), (x + 10 * scale, by1), (x, by1 + 16 * scale)],
               fill=(15, 18, 30, int(230 * (alpha / 255))))
    ld.text((x, (by0 + by1) / 2), text, font=f, fill=(255, 255, 255, alpha), anchor="mm")
    canvas_rgba.alpha_composite(layer)


GOAL_POPUP_FADE_OUT = 0.35  # 全ルートがゴールした後、Goal!ポップアップが消えるまでのフェード時間(秒)


def draw_goal_popup(canvas_rgba, x, y, color, local_elapsed, global_fade_elapsed, icon_size=54):
    """そのルートがゴール(目的地)に到達した際、アイコンの真上に
    「Goal!」というポップアップを表示する。draw_popup() の通過駅
    ポップアップとは別物で、(1) 文字色は白固定ではなく、そのルートの線の色
    (color)に合わせ、(2) 先にゴールしたルートのポップアップは、まだゴール
    していない(遅い)ルートがゴールするまで消さずに表示し続け、全ルートが
    ゴールした時点で一斉にフェードアウトする(「遅いほうがつくまで出続け
    ていたほうがいい」という要望に対応)。スコアボード側の既存の " GOAL"
    文字表示(draw_scoreboard)とも別物。

    local_elapsed: このルート自身がゴールしてからの経過秒(ポップアップの
        登場アニメーションに使う)。
    global_fade_elapsed: 全ルートの中で最後にゴールした時刻からの経過秒
        (負の値なら、まだ他のルートがゴールしていない=フェードアウト前)。
        全ルート共通のこの値でフェードアウトのタイミングを揃える。
    """
    if local_elapsed < 0.15:
        t = local_elapsed / 0.15
        scale = 0.3 + 1.9 * ease_out_back(t)
        alpha_in = min(1.0, t * 1.3)
    elif local_elapsed < 0.35:
        t = (local_elapsed - 0.15) / 0.20
        scale = 2.2 - 1.2 * t
        alpha_in = 1.0
    else:
        scale = 1.0
        alpha_in = 1.0

    if global_fade_elapsed <= 0:
        alpha_out = 1.0
    elif global_fade_elapsed < GOAL_POPUP_FADE_OUT:
        alpha_out = 1.0 - (global_fade_elapsed / GOAL_POPUP_FADE_OUT)
    else:
        return  # 全ルートがゴールしてから既に十分時間が経ち、フェードアウト完了

    alpha = int(255 * alpha_in * alpha_out)
    if alpha <= 0:
        return

    f = font(FONT_BLACK, int(30 * scale), index=0)
    text = "Goal!"
    bbox = f.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 18 * scale, 10 * scale
    top_offset = icon_size * 0.62 + th + pad_y * 2 + 6
    bx0, by0 = x - tw / 2 - pad_x, y - top_offset - pad_y
    bx1, by1 = x + tw / 2 + pad_x, y - top_offset + th + pad_y

    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([bx0, by0, bx1, by1], radius=12 * scale,
                          fill=(15, 18, 30, int(230 * (alpha / 255))),
                          outline=color + (alpha,), width=3)
    ld.polygon([(x - 9 * scale, by1), (x + 9 * scale, by1), (x, by1 + 14 * scale)],
               fill=(15, 18, 30, int(230 * (alpha / 255))))
    ld.text((x, (by0 + by1) / 2), text, font=f, fill=color + (alpha,), anchor="mm")
    canvas_rgba.alpha_composite(layer)


def draw_leg_status(canvas_rgba, x, y, text, color, icon_size=54):
    """アイコンのすぐそばに、現在の状態(「搭乗待ち」「鉄道移動中」等)を
    常時(フェードなしで)表示する小さなラベル。draw_popup() の通過駅
    ポップアップとは別物で、legsモードの区間(leg)を持つルートの
    アイコンの上に、そのアイコンが今何をしている最中かを示す。
    "legs" を持たない従来ルート、または text が空の場合は何も描かない
    (呼び出し側でチェック済みだが、防御的に空文字はここでも無視する)。"""
    if not text:
        return
    f = font(FONT_BOLD, 22)
    bbox = f.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 14, 8
    cy = y - icon_size * 0.62 - th - pad_y
    bx0, by0 = x - tw / 2 - pad_x, cy - pad_y
    bx1, by1 = x + tw / 2 + pad_x, cy + th + pad_y

    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([bx0, by0, bx1, by1], radius=11,
                          fill=(15, 18, 30, 215), outline=color + (255,), width=2)
    ld.text((x, (by0 + by1) / 2), text, font=f, fill=(255, 255, 255, 255), anchor="mm")
    canvas_rgba.alpha_composite(layer)


def marker_offset(cx, cy, index, n_routes, distance=100):
    """実際に地図上を動く点(cx, cy)から、アイコン+ステータスを表示する
    位置までのオフセット先(ix, iy)を計算する。複数ルートが同じ場所
    (レース開始直後など)にいても重ならないよう、ルートの並び順(index)
    に応じて真上を中心に扇状に角度を振り分ける。ステータスラベルの
    横幅(最大で「羽田空港(搭乗待ち)」程度)は横だけの間隔では足りない
    場合があるため、引き出し線の長さも交互に変えて高さもずらし、
    ラベル同士が重ならないようにしている。"""
    if n_routes <= 1:
        angle_deg = -90.0
        dist = distance
    else:
        spread = 70.0
        angle_deg = -90.0 + (index - (n_routes - 1) / 2) * spread
        dist = distance + (index % 2) * 55
    rad = math.radians(angle_deg)
    ix = cx + dist * math.cos(rad)
    iy = cy + dist * math.sin(rad)
    # アイコン+ステータスラベルがキャンバス端や出発地・到着地の近くで
    # 見切れないよう、描画可能な範囲内に収める。
    ix = min(max(ix, 130), CANVAS_W - 130)
    iy = min(max(iy, MAP_TOP + 95), MAP_BOTTOM - 20)
    return ix, iy


def draw_moving_marker(canvas_rgba, x, y, icon_x, icon_y, kind, color,
                        icon_img=None, icon_size=54, dot_radius=9):
    """地図上を実際に動くのは小さなドットのみにし、ドットから引き出し線を
    伸ばした先には、現在の状態を表すアイコン画像(電車/飛行機/待機中/バス/
    モノレール等)だけを表示する。ステータスの文言(「搭乗待ち」等)は
    ここでは表示せず、スコアボード側(交通手段名とプログレスバーの間)に
    表示する。"""
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.line([(x, y), (icon_x, icon_y)], fill=color + (190,), width=3)
    r = dot_radius
    ld.ellipse([x - r, y - r, x + r, y + r], fill=color + (255,),
               outline=(255, 255, 255, 240), width=3)
    canvas_rgba.alpha_composite(layer)

    draw_icon_by_kind(kind, canvas_rgba, icon_x, icon_y, color, icon_img=icon_img, size=icon_size)


def draw_caption(canvas_rgba, text, speaker, color, alpha=255):
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    y0, y1 = 1690, 1770
    d.rectangle([0, y0, CANVAS_W, y1], fill=(0, 0, 0, int(150 * alpha / 255)))
    f_tag = font(FONT_BOLD, 24)
    f_text = font(FONT_BOLD, 30)
    tag_w = max(60, int(len(speaker) * 15 + 30))
    d.rounded_rectangle([26, y0 + 12, 26 + tag_w, y0 + 44], radius=10, fill=tuple(color) + (alpha,))
    d.text((26 + tag_w / 2, y0 + 28), speaker, font=f_tag, fill=(10, 10, 15, alpha), anchor="mm")
    max_chars = 22
    lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)][:2]
    ty = y0 + 60
    for ln in lines:
        d.text((CANVAS_W / 2, ty), ln, font=f_text, fill=(255, 255, 255, alpha), anchor="mm")
        ty += 34
    canvas_rgba.alpha_composite(layer)


def draw_scoreboard(canvas_rgba, route_list, motions, real_mins, bar_top=None):
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f_name = font(FONT_BOLD, 30)
    f_time = font(FONT_BLACK, 30, index=0)
    f_status = font(FONT_BOLD, 19)
    # 交通手段名(短縮名)とプログレスバーの間に、ステータス文言(legsモード
    # のみ、例:「鉄道移動中」「搭乗待ち」)を表示する。以前はここに小さな
    # アイコン画像(電車/飛行機/バス等)も並べて表示していたが、ステータス
    # 文言の横にアイコンが並ぶと見た目がうるさいため廃止し、テキストのみに
    # している(車両アイコン自体は地図上を動くマーカーの方に表示される)。
    # 文言の開始位置は「JR新快速」のように短縮名が長いルートでも文字と
    # 重ならないよう、実際のテキスト幅から動的に計算する。
    name_x, min_status_x = 40, 168
    bar_x0, bar_x1 = 400, 820
    n_routes = len(route_list)
    gap = 70 if n_routes <= 2 else 50
    if bar_top is None:
        # render() から bar_top が渡されない呼び出し(単体テスト等)向けの
        # フォールバック。SAFE_BOTTOM_Y(リールUIのセーフゾーン境界)より
        # 絶対に下がらない位置を、render() 側と同じ式で逆算する。
        bar_top = SAFE_BOTTOM_Y - 15 - 15 - gap * (n_routes - 1)
    row_ys = [bar_top + i * gap for i in range(n_routes)]
    for route, y in zip(route_list, row_ys):
        color = tuple(route["color"])
        m = motions[route["key"]]
        rmin = real_mins[route["key"]]
        frac = m.progress(rmin)
        finished = m.finished(rmin)
        d.text((name_x, y), route["short_name"], font=f_name, fill=(255, 255, 255, 255), anchor="lm")
        name_bbox = f_name.getbbox(route["short_name"])
        status_x0 = max(min_status_x, name_x + (name_bbox[2] - name_bbox[0]) + 26)
        d.rounded_rectangle([bar_x0, y - 10, bar_x1, y + 10], radius=10, fill=(255, 255, 255, 40))
        fill_x = bar_x0 + (bar_x1 - bar_x0) * frac
        if fill_x > bar_x0:
            d.rounded_rectangle([bar_x0, y - 10, fill_x, y + 10], radius=10, fill=color + (255,))
        label = f"{min(rmin, route['total_min']):.0f}分" + (" GOAL" if finished else "")
        d.text((1040, y), label, font=f_time, fill=(255, 255, 255, 255), anchor="rm")
        # 交通手段名とバーの間に、(legsモードなら)状態文言を表示する
        # (到着後は表示しない)。
        if not finished:
            status_text = leg_status_at(route, rmin)
            if status_text:
                max_w = bar_x0 - 16 - status_x0
                st = status_text
                while st and f_status.getbbox(st)[2] > max_w:
                    st = st[:-1]
                if st != status_text and len(st) > 0:
                    st = st[:-1] + "…"
                d.text((status_x0, y), st, font=f_status, fill=(230, 230, 240, 255), anchor="lm")
    canvas_rgba.alpha_composite(layer)


RESULT_TIE_EPSILON_MIN = 1e-6


def draw_result_panel(canvas_rgba, config, route_list, alpha):
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(0, 0, 0, int(140 * alpha / 255)))
    cx, cy = CANVAS_W / 2, CANVAS_H / 2 - 60

    best_time = min(r["total_min"] for r in route_list)
    winners = [r for r in route_list if abs(r["total_min"] - best_time) <= RESULT_TIE_EPSILON_MIN]
    is_tie = len(winners) >= 2

    f_big = font(FONT_BLACK, 76, index=0)
    f_mid = font(FONT_BOLD, 40)
    f_small = font(FONT_BOLD, 32)

    d.text((cx, cy - 180), "RESULT", font=f_big, fill=(255, 215, 0, alpha), anchor="mm")
    if is_tie:
        winners_label = "・".join(r["short_name"] for r in winners)
        d.text((cx, cy - 90), f"{winners_label} 同着!", font=f_mid, fill=(255, 255, 255, alpha), anchor="mm")
        d.text((cx, cy - 40), "( 引き分け )", font=f_mid, fill=(255, 215, 0, alpha), anchor="mm")
    else:
        winner = winners[0]
        other_times = [r["total_min"] for r in route_list if r is not winner]
        diff = min(other_times) - best_time
        d.text((cx, cy - 90), f"{winner['short_name']} の勝ち!", font=f_mid, fill=(255, 255, 255, alpha), anchor="mm")
        d.text((cx, cy - 40), f"( 差 {diff:.0f} 分 )", font=f_mid, fill=tuple(winner["color"]) + (alpha,), anchor="mm")

    n = len(route_list)
    xs = [cx + (i - (n - 1) / 2) * 300 for i in range(n)]
    for r, x in zip(route_list, xs):
        d.text((x, cy + 60), r["short_name"], font=f_small, fill=tuple(r["color"]) + (alpha,), anchor="mm")
        d.text((x, cy + 105), f"{r['total_min']}分", font=f_mid, fill=(255, 255, 255, alpha), anchor="mm")

    canvas_rgba.alpha_composite(layer)


def render(config, paths, base_map_rgba, proj, out_dir="output", frames_dir="frames",
           fps=FPS, fast_preview=False, out_name=None):
    # out_name を指定すると、出力ファイル名を config の slug と切り離して
    # 自由に決められる(configのslugはあくまで configs/<slug>.json という
    # 保存先ファイル名としてのみ使われる)。
    slug = out_name or config.get("slug", "race")
    route_list = list(paths.values())
    motions = {r["key"]: RouteMotion(r) for r in route_list}

    intro_sec = config.get("intro_sec", 2.0)
    outro_hold = config.get("outro_hold_sec", OUTRO_HOLD_SEC)

    # 動画の長さは「実1分を動画何秒に圧縮するか(compress_sec_per_min)」を
    # 直接指定する古い方式と、「動画全体を何秒にしたいか(video_duration_sec、
    # デフォルト30秒)」を指定してこちらから比率を逆算する新しい方式の
    # どちらでも設定できる。config に compress_sec_per_min が明示されていれば
    # そちらを優先する(過去のconfigとの互換性のため)。
    if "compress_sec_per_min" in config:
        ratio = config["compress_sec_per_min"]
    else:
        video_duration_sec = config.get("video_duration_sec", 30.0)
        longest_total_min = max(r["total_min"] for r in route_list) if route_list else 1.0
        # 末尾のRESULTテロップ(2.6秒)ぶんを差し引いた残りを、
        # レース区間(イントロ〜ゴール+1分)に均等に割り当てる。
        available = video_duration_sec - intro_sec - outro_hold - 2.6
        ratio = max(0.05, available / max(longest_total_min + 1, 0.001))

    timeline = build_events(config, paths, intro_sec, ratio)
    write_script_files(timeline, config, paths, out_dir=out_dir)
    # 実況テロップを動画内に焼き込むかどうか。台本(.txt)・字幕(.srt)ファイルは
    # show_captions の設定に関わらず常に output/ に書き出される
    # (ナレーション収録や動画編集ソフトでの字幕付けに使える)。
    show_captions = bool(config.get("show_captions", False))

    total_video_sec = timeline[-1]["t_end"] + outro_hold
    fps = 10 if fast_preview else fps

    if os.path.exists(frames_dir):
        shutil.rmtree(frames_dir)
    os.makedirs(frames_dir)

    n_frames = int(total_video_sec * fps)
    print(f"rendering {n_frames} frames ({total_video_sec:.1f}s @ {fps}fps) ...")

    result_start = max(m.total_min for m in motions.values()) * ratio + intro_sec + 1.0
    color_by_key = {r["key"]: tuple(r["color"]) for r in route_list}
    # 最後(最も遅い)ルートがゴールする時刻。Goal!ポップアップは、先にゴール
    # したルートも含めて全ルートがこの時刻に達するまで表示し続け、ここから
    # 一斉にフェードアウトする。
    last_goal_t = max(intro_sec + r["total_min"] * ratio for r in route_list)

    # プログレスバーの表示位置: 路線の描画がコンパクトで画面上部〜中央寄りに
    # 収まっている場合ほど、路線のすぐ下(+少し余白)まで詰める。
    # ただし、リールUIのセーフゾーン(画面下端から1/8 = SAFE_BOTTOM_Y より下)
    # には、どんな場合も絶対にプログレスバーがかからないようにする
    # (詰める方向にのみ動く安全な調整で、この上限を超えて下げることはない)。
    n_routes = len(route_list)
    bar_gap = 70 if n_routes <= 2 else 50
    # 最終行のテキスト(高さの半分約15px)がSAFE_BOTTOM_Yより上に収まるよう、
    # 余白15pxを加えて逆算した「これ以上下げられない」上限。
    default_bar_top = SAFE_BOTTOM_Y - 15 - 15 - bar_gap * (n_routes - 1)
    content_bottom_y = proj.get("content_bottom_y")
    if content_bottom_y is None:
        bar_top = None
    else:
        bar_top = content_bottom_y + 50
        bar_top = min(bar_top, default_bar_top)

    for fi in range(n_frames):
        t = fi / fps
        canvas = base_map_rgba.copy()

        real_mins = {}
        for r in route_list:
            m = motions[r["key"]]
            race_t = t - intro_sec
            real_min = 0.0 if race_t < 0 else race_t / ratio
            real_mins[r["key"]] = min(real_min, m.total_min)

        for r in reversed(route_list):
            draw_progress_route(canvas, proj, r, motions[r["key"]], real_mins[r["key"]], color_by_key[r["key"]])

        n_routes_marker = len(route_list)
        marker_icon_pos = {}
        for idx, r in enumerate(reversed(route_list)):
            marker_index = n_routes_marker - 1 - idx  # route_list本来の並び順で扇状に配置する
            lon, lat = motions[r["key"]].lonlat_at(real_mins[r["key"]])
            x, y = project(proj, lon, lat)
            ix, iy = marker_offset(x, y, marker_index, n_routes_marker)
            kind = leg_icon_at(r, real_mins[r["key"]])  # "legs"が無いルートはNone(=従来通り)
            marker_icon_img = resolve_icon_img(r, real_mins[r["key"]], kind, size=54)
            # 地図上を実際に動くのは小さなドットのみ。引き出し線の先には
            # アイコン画像だけを表示する(ステータス文言はスコアボード側)。
            draw_moving_marker(canvas, x, y, ix, iy, kind, color_by_key[r["key"]],
                                icon_img=marker_icon_img)
            marker_icon_pos[r["key"]] = (ix, iy)

        # ゴール(目的地)に到達したルートのアイコンの真上に、そのルートの
        # 線の色で「Goal!」をポップアップ表示する。通過駅の draw_popup() や
        # スコアボードの " GOAL" 文字表示(draw_scoreboard)とは別物。先に
        # ゴールしたルートのポップアップも、まだの(遅い)ルートがゴールする
        # まで表示し続け、全ルートがゴールしたら一斉にフェードアウトする。
        global_fade_elapsed = t - last_goal_t
        if global_fade_elapsed < GOAL_POPUP_FADE_OUT:
            for r in route_list:
                m = motions[r["key"]]
                if m.finished(real_mins[r["key"]]):
                    t_reach_goal = intro_sec + r["total_min"] * ratio
                    local_elapsed = t - t_reach_goal
                    if local_elapsed >= 0:
                        ix, iy = marker_icon_pos[r["key"]]
                        draw_goal_popup(canvas, ix, iy, color_by_key[r["key"]],
                                         local_elapsed, global_fade_elapsed)

        for r in route_list:
            for st in r["stations"]:
                if not st["popup"]:
                    continue
                t_reach = intro_sec + st["t_min"] * ratio
                elapsed = t - t_reach
                if 0 <= elapsed <= 1.6:
                    draw_popup(canvas, proj, st, elapsed, color_by_key[r["key"]])

        draw_scoreboard(canvas, route_list, motions, real_mins, bar_top=bar_top)

        if show_captions:
            for ev in timeline:
                if ev["t_start"] <= t <= ev["t_end"]:
                    fade = 1.0
                    if t - ev["t_start"] < 0.15:
                        fade = (t - ev["t_start"]) / 0.15
                    elif ev["t_end"] - t < 0.15:
                        fade = (ev["t_end"] - t) / 0.15
                    color = color_by_key.get(ev["speaker_key"], (255, 215, 0))
                    draw_caption(canvas, ev["text"], ev["speaker"], color, alpha=int(255 * max(0, fade)))
                    break

        if t >= result_start:
            a = min(1.0, (t - result_start) / 0.8)
            draw_result_panel(canvas, config, route_list, alpha=int(255 * a))

        # PNG保存はデフォルト圧縮だとCPUコストが大きく、フレーム書き出し全体の
        # 最大のボトルネックになっていた(プロファイルで確認済み)。この連番PNG
        # はffmpegに渡した後すぐ捨てる中間ファイルなので、圧縮率より速度を優先
        # する(compress_level=1)。CPUが弱い環境(Renderの無料プランなど)での
        # 生成時間を大きく縮められる。
        canvas.convert("RGB").save(f"{frames_dir}/frame_{fi:05d}.png", compress_level=1)
        if fi % 60 == 0:
            print(f"  frame {fi}/{n_frames}")

    print("encoding mp4 ...")
    out_path = f"{out_dir}/{slug}.mp4"
    cmd = [
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", f"{frames_dir}/frame_%05d.png",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-vf", "format=yuv420p",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    print("done:", out_path)
    return out_path
