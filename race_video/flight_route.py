# -*- coding: utf-8 -*-
"""
飛行機 vs 電車(または他の交通手段)の比較用の汎用ルートビルダー。

既存の route_builder.build_route()(路線名+駅名から routes.geojson / 駅データSHP
を引いて実路線を組み立てる仕組み)とは別に、こちらは「区間(leg)ごとに、
実ルートの座標列・直線・大圏航路のいずれかを直接指定する」形で1つの
ルートを組み立てる、より汎用的なビルダー。station_db(駅データSHP)を
一切必要としないので、新幹線・電車・モノレール・バス・徒歩・飛行機を
自由に組み合わせられる。

想定する使い方: 実際の座標列(例: 国土数値情報の鉄道データから抽出した
シンプル化済みポリライン)・空港座標などは、あらかじめ用意した
data/flight_assets/*.json に入れておき、config の routes[].legs から
参照する。config自体は、Claude Artifact側(駅レースビルダーの飛行機版)で
組み立てて書き出す想定で、legの時刻(t_start/t_end、分・動画内の共通
タイムラインでの経過分)もアーティファクト側で計算済みの値が入る。

config の1ルート(mode: "legs")の形:
{
  "key": "route_b",
  "mode": "legs",
  "name": "飛行機(羽田 -> 福岡空港)",
  "short_name": "飛行機",
  "color": [79, 195, 247],
  "start_label": "東京",          # 最初のチェックポイント(t_min=0)のラベル
  "popup_start": true,
  "legs": [
    {"kind": "coords", "coords": [[lon,lat], ...], "icon": "train",
     "t_start": 0, "t_end": 35, "label": "羽田空港第1ターミナル", "popup": true},
    {"kind": "wait", "at": [lon,lat], "icon": "wait",
     "t_start": 35, "t_end": 70, "label": "羽田空港(搭乗待ち)"},
    {"kind": "great_circle", "from": [lon,lat], "to": [lon,lat], "icon": "plane",
     "t_start": 70, "t_end": 160, "label": "福岡空港", "popup": true},
    {"kind": "wait", "at": [lon,lat], "icon": "wait",
     "t_start": 160, "t_end": 174, "label": "福岡空港(降機後の待機)"},
    {"kind": "coords", "coords": [[lon,lat], ...], "icon": "train",
     "t_start": 174, "t_end": 186, "label": "博多", "popup": true}
  ]
}

"kind" の種類:
  - "coords":       あらかじめ用意された実ルートの座標列をそのまま使う
  - "straight":      from/to の2点を直線で結ぶ(バス・徒歩など、道路網
                     データが無い区間の近似)
  - "great_circle":  from/to の2点間の大圏航路(球面上の最短経路)を
                     azimuthal equidistant(正距方位図法)と同じ考え方で
                     補間する。飛行機区間に使う。
  - "wait":          その場に留まる(待機中)。位置は動かさず、アイコンだけ
                     待機アイコンになる。

各legの t_start/t_end は「動画内の共通タイムライン」上の経過分(分)。
複数ルート(電車 vs 飛行機 等)を比較する際、それぞれの出発時刻がずれて
いる場合(例: 飛行機の方が先に家を出る)も、Claude Artifact側で共通の
基準時刻からの経過分に変換して埋め込む想定なので、このモジュール自体は
時刻(HH:MM)を一切扱わない。
"""
import math

EARTH_R_KM = 6371.0


def haversine_km(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(h))


def great_circle_points(a, b, n=80):
    """2点間の大圏航路(球面上の最短経路)をn+1点に補間する。
    正距方位図法で描いた直線を地図に投影し直すのと同じ経路になる。"""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    d = 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 +
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    pts = []
    for i in range(n + 1):
        f = i / n
        if d == 0:
            pts.append((a[0], a[1]))
            continue
        A = math.sin((1 - f) * d) / math.sin(d)
        B = math.sin(f * d) / math.sin(d)
        x = A * math.cos(lat1) * math.cos(lon1) + B * math.cos(lat2) * math.cos(lon2)
        y = A * math.cos(lat1) * math.sin(lon1) + B * math.cos(lat2) * math.sin(lon2)
        z = A * math.sin(lat1) + B * math.sin(lat2)
        lat = math.atan2(z, math.sqrt(x * x + y * y))
        lon = math.atan2(y, x)
        pts.append((math.degrees(lon), math.degrees(lat)))
    return pts


def build_leg_route(route_cfg):
    """route_cfg (mode: "legs") から、route_builder.build_route() と互換の
    辞書(polyline / cum_dist / stations / total_min 等)を組み立てる。
    追加で "legs"(各区間の t_start/t_end/icon)も返し、animate.py が
    現在の経過分からアイコン種別(電車/飛行機/待機/バス/徒歩)を
    切り替えるのに使う。"""
    polyline = []
    cum = [0.0]
    stations = []
    legs_out = []

    def add_point(pt):
        polyline.append(pt)
        if len(polyline) > 1:
            cum.append(cum[-1] + haversine_km(polyline[-2], pt))

    start_pt = None
    for leg in route_cfg["legs"]:
        kind = leg["kind"]
        if kind == "wait":
            pt = tuple(leg["at"])
            if start_pt is None:
                start_pt = pt
                add_point(pt)
            add_point(pt)  # 同じ座標をもう一度足すことで「距離が進まない待機」を表現する
        else:
            if kind == "coords":
                coords = [tuple(c) for c in leg["coords"]]
            elif kind == "straight":
                coords = [tuple(leg["from"]), tuple(leg["to"])]
            elif kind == "great_circle":
                coords = great_circle_points(tuple(leg["from"]), tuple(leg["to"]), n=80)
            else:
                raise ValueError(f"未知のleg種別: {kind}")

            if start_pt is None:
                start_pt = coords[0]
                add_point(coords[0])
            for pt in coords[1:]:
                add_point(pt)

        stations.append({
            "name": leg.get("label", ""), "line": "",
            "lon": polyline[-1][0], "lat": polyline[-1][1],
            "t_min": leg["t_end"], "path_index": len(polyline) - 1, "cum_km": cum[-1],
            "popup": bool(leg.get("popup", False)),
            "transfer": bool(leg.get("transfer", False)),
            "direct_through": bool(leg.get("direct_through", False)),
            "transfer_note": leg.get("transfer_note", ""),
        })
        legs_out.append({
            "t_start": leg["t_start"], "t_end": leg["t_end"],
            "icon": leg.get("icon", "train"), "kind": kind,
            "label": leg.get("label", ""),
            # このleg専用の車両アイコン画像(透過PNG)。乗り換えで実際の
            # 車両(路線)が変わる区間や、飛行機・バス(市内移動)など、
            # legごとに見た目を変えたい場合に指定する。省略時はNone
            # (leg_icon_path_at() がルート全体の既定icon_pathやベクター
            # プレースホルダーにフォールバックする)。
            "icon_path": leg.get("icon_path"),
        })

    stations.insert(0, {
        "name": route_cfg.get("start_label", ""), "line": "",
        "lon": polyline[0][0], "lat": polyline[0][1],
        "t_min": 0, "path_index": 0, "cum_km": 0.0,
        "popup": bool(route_cfg.get("popup_start", True)),
        "transfer": False, "direct_through": False, "transfer_note": "",
    })

    total_min = stations[-1]["t_min"]
    return {
        "key": route_cfg["key"],
        "name": route_cfg["name"],
        "short_name": route_cfg.get("short_name", route_cfg["name"]),
        "color": route_cfg.get("color", [230, 230, 230]),
        "total_min": total_min,
        "polyline": polyline,
        "cum_dist": cum,
        "stations": stations,
        "legs": legs_out,
        # ルート全体の既定アイコン画像(省略可)。legに"icon_path"が無い
        # 場合のフォールバックとして使われる(train系のアイコンのみ)。
        "icon_path": route_cfg.get("icon_path"),
    }


def leg_icon_at(route, real_min):
    """このルートの real_min(経過分)時点でのアイコン種別を返す
    ("train" / "plane" / "wait" / "bus" / "walk" / "monorail")。
    "legs" を持たない(=従来の route_builder.build_route() で組み立てた)
    ルートに対しては None を返す(呼び出し側は従来通り単一アイコンを使う)。"""
    legs = route.get("legs")
    if not legs:
        return None
    for leg in legs:
        if real_min <= leg["t_end"]:
            return leg["icon"]
    return legs[-1]["icon"]


def leg_icon_path_at(route, real_min):
    """このルートの real_min 時点の leg に、専用の車両アイコン画像
    (config の leg."icon_path")が指定されていれば、そのパスを返す。
    乗り換えで実際の車両(路線)が変わる区間ごとに違う画像を使ったり、
    飛行機・バス(市内移動)などlegごとに見た目を変えたい場合に使う。
    指定が無い場合や "legs" を持たないルートに対しては None を返す
    (呼び出し側はルート全体の既定 icon_path やベクタープレースホルダーに
    フォールバックする)。"""
    legs = route.get("legs")
    if not legs:
        return None
    cur = legs[-1]
    for leg in legs:
        if real_min <= leg["t_end"]:
            cur = leg
            break
    return cur.get("icon_path")


# アイコン種別ごとの既定ステータス文言。"wait"(搭乗待ち・乗換待ち等)は
# 通常 config 側の leg.label に具体的な文言(例:「羽田空港(搭乗待ち)」)が
# 入っているのでそちらを優先し、これはlabelが無い場合のフォールバック。
_ICON_STATUS_DEFAULT = {
    "train": "鉄道移動中",
    "plane": "飛行中",
    "bus": "バス移動中",
    "walk": "徒歩移動中",
    "monorail": "モノレール移動中",
    "wait": "待機中",
}


def leg_status_at(route, real_min):
    """このルートの real_min 時点で、アイコンのそばに出す短いステータス
    文言を返す(例:「搭乗待ち」の代わりに leg.label があればそれを使い、
    通常の移動区間(kind=coords/straight/great_circle)は「鉄道移動中」
    「飛行中」のようなアイコン種別ごとの既定文言を使う)。"legs" を持たない
    ルートに対しては None を返す。"""
    legs = route.get("legs")
    if not legs:
        return None
    cur = legs[-1]
    for leg in legs:
        if real_min <= leg["t_end"]:
            cur = leg
            break
    if cur.get("kind") == "wait" and cur.get("label"):
        return cur["label"]
    return _ICON_STATUS_DEFAULT.get(cur.get("icon"), "")
