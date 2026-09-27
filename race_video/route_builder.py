# -*- coding: utf-8 -*-
"""
レース設定(config)から、実際の路線形状に沿ったポリライン + 駅位置 + 累積距離
を組み立てる。どの2駅間・どの路線の組み合わせでも使えるように汎用化してある。

config の1ルートは以下の形:
{
  "key": "route_a",
  "name": "阪神本線",
  "color": [230,20,20],           # 省略時は station_db の色を使う
  "stations": [
     {"line": "阪神本線", "name": "大阪梅田", "t_min": 0, "popup": true},
     {"line": "阪神本線", "name": "尼崎", "t_min": 8, "popup": true},
     ...
     {"line": "阪神本線", "name": "神戸三宮", "t_min": 27, "popup": true},
  ]
}
駅の並び順(= どの駅をどの順で通るか)は、この stations 配列の順序で明示的に
指定する。station_db 側のseq/cum_kmが壊れている路線があっても、
駅名->座標の引き当てさえ合っていれば問題なく使える(関西40路線では
そうした問題は見つかっていないが、環状運転の大阪環状線だけは駅の並び順
だけでは向き・区間が一意に決まらないため、常にこの明示指定が必要)。

各駅の "line" が routes.geojson に無い路線名の場合(駅データSHPには
あるが線形状データが無い路線)は、前後の駅を直線で結ぶフォールバックになる。
"""
import json
import math

from .station_db import haversine_km, get_station_lonlat

# 隣接区間の進行方位(bearing)がこの角度(度)以上急変したら「逆走キンク」と
# みなして頂点を除去する。緩やかな本物のカーブ(せいぜい数十度)は残し、
# ほぼ真逆になる異常な頂点だけを狙い撃ちする値。
BACKTRACK_BEARING_DEG = 120

# station_db / routes.geojson には存在しない「愛称レベル」の路線名を、
# 実際に座標・線形データを持つ路線名に読み替えるための対応表。
# (例: ある路線の一部区間だけが別の愛称で呼ばれていて、駅データ上は
# もう1つの路線名の中に混線して含まれている、というようなケース)
# 関西40路線は現状すべて実際の登録路線名のままconfigに書けば見つかるので、
# このリポジトリでは空のままにしてある。将来そうした愛称路線を追加する
# 場合はここに追記する。
LINE_ALIASES = {}


def resolve_line_name(line_name):
    return LINE_ALIASES.get(line_name, line_name)


def load_line_geoms(geojson_path):
    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)
    lines = {}
    for feat in data["features"]:
        name = feat["properties"]["line_name"]
        geom = feat["geometry"]
        if geom["type"] == "LineString":
            lines[name] = [geom["coordinates"]]
        else:  # MultiLineString
            lines[name] = geom["coordinates"]
    return lines


def nearest_index(coords, pt, start=0, prefer_last=False):
    """coords[start:] の中から pt に最も近い点のインデックス(coords全体基準)を返す。

    大江戸線のような「環状+尻尾」路線では、同じ駅(例: 都庁前)の座標が
    1本のLineString中に複数回登場する(環を一周して戻ってくるため)。
    その場合、同じ最短距離を与える点が複数あり得るので、prefer_last=True
    にすると、その中で最も後ろ(=進行方向により先)のインデックスを選ぶ。
    既定(prefer_last=False)は従来通り、最初に見つかった最短点を選ぶ。
    """
    best_i, best_d = start, float("inf")
    cmp = (lambda d, best_d: d <= best_d) if prefer_last else (lambda d, best_d: d < best_d)
    for i in range(start, len(coords)):
        d = haversine_km(coords[i], pt)
        if cmp(d, best_d):
            best_d = d
            best_i = i
    return best_i, best_d


def pick_best_segment(segments, pt):
    best = None
    for seg in segments:
        i, d = nearest_index(seg, pt)
        if best is None or d < best[2]:
            best = (seg, i, d)
    return best


def best_segment_match(segments, pt, prefer_last=False):
    """全セグメント(MultiLineStringの各枝)の中から pt に最も近い点を探し、
    (segment_index, point_index, dist_km) を返す。"""
    best = None
    for si, seg in enumerate(segments):
        i, d = nearest_index(seg, pt, prefer_last=prefer_last)
        if best is None or d < best[2]:
            best = (si, i, d)
    return best


def find_junction(seg_a, seg_b):
    """2つの枝(seg_a, seg_b)がどこで接続しているかを探す。

    湘南新宿ライン・上野東京ラインのように、複数の実在路線が合流・分岐する
    路線は、routes.geojson 上では複数本のLineString(MultiLineString)として
    格納されており、それぞれの支線は本線から分岐する地点(駅、例: 大宮)を
    始点/終点として作られている。そのため、両セグメントの「端点同士」の
    組み合わせだけを調べれば接続点が見つかる(全点同士の総当りは不要)。
    戻り値: (idx_in_seg_a, idx_in_seg_b, dist_km)
    """
    candidates = []
    for ia, pa in ((0, seg_a[0]), (len(seg_a) - 1, seg_a[-1])):
        ib, d = nearest_index(seg_b, pa)
        candidates.append((ia, ib, d))
    for ib, pb in ((0, seg_b[0]), (len(seg_b) - 1, seg_b[-1])):
        ia, d = nearest_index(seg_a, pb)
        candidates.append((ia, ib, d))
    return min(candidates, key=lambda c: c[2])


def cumulative_dist(coords):
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_km(coords[i - 1], coords[i]))
    return cum


def _bearing_deg(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    y = math.sin(dlon) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360


def _points_close(a, b, tol=1e-5):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def collapse_revisits(coords, tol_km=0.05, lookback=800, min_gap=3, max_iterations=50):
    """
    routes.geojson にはまれに「ある地点まで進んで、そこから先の経路のどこかで
    ほぼ同じ地点をもう一度通ってしまう」往復・ループ状の重複が混じっている
    (例: 湘南新宿ラインの横浜駅付近。複数の物理経路をデータ生成時に結合した
    際の重複と見られる)。そのまま描画すると、動画内で車両アイコンが一瞬だけ
    後ろに戻っているように見えてしまう。

    座標が(既定50m以内に)再訪されている最も早いペアを見つけ、その間の
    区間をまるごと畳み込む(=1回だけ通ったことにする)。入れ子や複数箇所の
    重複があっても、無くなるまで繰り返し処理する。
    戻り値は (処理後の座標列, 警告文リスト)。
    """
    coords = list(coords)
    warnings = []
    for _ in range(max_iterations):
        n = len(coords)
        found = None
        for i in range(n):
            limit = min(n, i + lookback)
            for j in range(i + min_gap, limit):
                if haversine_km(coords[i], coords[j]) < tol_km:
                    found = (i, j)
                    break
            if found:
                break
        if not found:
            break
        i, j = found
        warnings.append(
            f"経路が同じ地点(座標 {coords[i]} 付近)を再訪していたため、"
            f"{j - i}点分のループを畳み込みました。"
        )
        del coords[i + 1:j + 1]
    else:
        warnings.append("collapse_revisits: max_iterations に達しました。ループが残っている可能性があります。")
    return coords, warnings


def remove_single_vertex_noise(coords, bearing_jump_deg=BACKTRACK_BEARING_DEG,
                                single_vertex_km=0.5, max_passes=20):
    """
    collapse_revisits では拾えない、ごく短い範囲だけ寄り道してすぐ戻る
    1頂点だけのノイズ(GPS/デジタイズ誤差)を取り除く。進行方位(bearing)が
    急反転(既定120度以上)し、かつその寄り道の余分な距離(detour - chord)が
    小さい(既定0.5km未満)頂点だけを間引く。大きい場合は本物の急カーブと
    みなして触らない。戻り値は (処理後の座標列, 警告文リスト)。
    """
    coords = list(coords)
    warnings = []
    confirmed_genuine = set()

    def rk(p):
        return (round(p[0], 6), round(p[1], 6))

    for _ in range(max_passes):
        if len(coords) < 5:
            break
        bearings = [_bearing_deg(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
        tip = None
        for i in range(1, len(bearings)):
            if rk(coords[i]) in confirmed_genuine:
                continue
            d = abs((bearings[i] - bearings[i - 1] + 180) % 360 - 180)
            if d >= bearing_jump_deg:
                tip = i
                break
        if tip is None:
            break

        chord = haversine_km(coords[tip - 1], coords[tip + 1])
        detour = haversine_km(coords[tip - 1], coords[tip]) + haversine_km(coords[tip], coords[tip + 1])
        if detour - chord < single_vertex_km:
            warnings.append(
                f"孤立したノイズ頂点を1点間引きました(座標 {coords[tip]} 付近、"
                f"寄り道距離 {detour - chord:.2f}km)。"
            )
            del coords[tip]
            continue

        confirmed_genuine.add(rk(coords[tip]))
        warnings.append(
            f"進行方向が急反転する頂点がありますが、寄り道が大きいため保持しました"
            f"(座標 {coords[tip]} 付近)。"
        )
    return coords, warnings


def remove_backtracking(coords, skip_collapse=False):
    """collapse_revisits -> remove_single_vertex_noise の順に適用するショートカット。

    skip_collapse=True の場合は collapse_revisits(同じ地点の再訪を「ループ状の
    デジタイズ誤差」とみなして畳み込む処理)を飛ばす。大江戸線のように、
    路線そのものが本当に環状(尻尾+ループ)になっていて、同じ駅(都庁前)の
    座標を意図的に2回通る場合、collapse_revisits がこれを誤検出して
    ループ区間をまるごと消してしまうため。
    """
    if skip_collapse:
        w1 = []
    else:
        coords, w1 = collapse_revisits(coords)
    coords, w2 = remove_single_vertex_noise(coords)
    return coords, w1 + w2


# 路線そのものが環状(+尻尾)になっていて、同じ駅の座標を1本のLineString内で
# 意図的に2回通る路線。build_route() はこれらの路線が絡むルートについて、
# collapse_revisits(ループを「デジタイズ誤差」とみなして畳み込む処理)を
# スキップする。
LOOP_LINES = {"大江戸線", "大阪環状線"}


def _fill_missing_t_min(station_records, route_label):
    """
    途中で乗り換えも停車もしない「通過駅」は、config側で t_min を
    空欄(None)のままにしてよい。ここでは、最初の駅と最後の駅にだけ
    実際の所要時間(t_min)が入っていることを必須とし、その間の空欄は、
    実際に組み立てたポリライン上の距離(cum_km)に比例して自動的に
    補間する。

    最初/最後の駅が空欄の場合は、補間のしようがないため、
    分かりやすいエラーメッセージで止める。
    """
    n = len(station_records)
    known = [(i, r["t_min"]) for i, r in enumerate(station_records) if r["t_min"] is not None]
    if not known or known[0][0] != 0 or known[-1][0] != n - 1:
        raise ValueError(
            f"[{route_label}] 最初の駅と最後の駅には所要時間(t_min)を必ず指定してください"
            "(その間の通過駅は空欄のままでよく、自動的に補完されます)。"
        )
    warnings = []
    for (i0, t0), (i1, t1) in zip(known, known[1:]):
        if i1 - i0 <= 1:
            continue
        k0 = station_records[i0]["cum_km"]
        k1 = station_records[i1]["cum_km"]
        span = k1 - k0
        names = [station_records[i]["name"] for i in range(i0 + 1, i1)]
        warnings.append(
            f"{route_label}: {names} の所要時間は未入力のため、"
            f"{station_records[i0]['name']}({t0}分)-{station_records[i1]['name']}({t1}分) "
            "の間で距離に応じて自動補完しました。"
        )
        for i in range(i0 + 1, i1):
            if span <= 0:
                frac = (i - i0) / (i1 - i0)
            else:
                frac = (station_records[i]["cum_km"] - k0) / span
            station_records[i]["t_min"] = round(t0 + (t1 - t0) * frac, 2)
    return warnings


def build_route(route_cfg, line_geoms, station_db):
    """1ルート分のポリライン・駅情報を組み立てる。戻り値と warnings のタプル。"""
    stations_cfg = route_cfg["stations"]
    warnings = []

    resolved = []
    for s in stations_cfg:
        real_line = resolve_line_name(s["line"])
        try:
            lon, lat = get_station_lonlat(station_db, real_line, s["name"])
        except KeyError as e:
            raise KeyError(
                f"[{route_cfg.get('name', route_cfg.get('key'))}] {e}. "
                f"station_db.load_station_db() のline_name表記(路線名)と "
                f"config内の'line'(または、その愛称のLINE_ALIASES変換先)が "
                f"一致しているか確認してください。"
            )
        resolved.append({**s, "lon": lon, "lat": lat})

    # 連続して同じ路線の駅が続く区間ごとにグループ化する
    seg_ranges = []
    cur_line = resolved[0]["line"]
    start_i = 0
    for i in range(1, len(resolved)):
        if resolved[i]["line"] != cur_line:
            seg_ranges.append((cur_line, start_i, i - 1))
            cur_line = resolved[i]["line"]
            start_i = i - 1  # 前の駅を継ぎ目として重複させる
    seg_ranges.append((cur_line, start_i, len(resolved) - 1))

    polyline = []
    for line_name, si, ei in seg_ranges:
        pt_start = (resolved[si]["lon"], resolved[si]["lat"])
        pt_end = (resolved[ei]["lon"], resolved[ei]["lat"])

        segments = line_geoms.get(resolve_line_name(line_name))
        if not segments:
            warnings.append(
                f"'{line_name}' の線形状データが routes.geojson に無いため、"
                f"{resolved[si]['name']}-{resolved[ei]['name']} 間は直線で結びます。"
            )
            piece = [pt_start, pt_end]
        else:
            seg_i0, idx_start, d0 = best_segment_match(segments, pt_start)
            # pt_end 側は prefer_last=True にして、環状路線(大江戸線など)で
            # 同じ駅の座標が複数回登場する場合に「より進行方向側」の
            # (=環を一周した後の)出現を選ぶ。通常の路線では同じ座標の
            # 重複が無いため、この変更による挙動の変化はない。
            seg_i1, idx_end, d1 = best_segment_match(segments, pt_end, prefer_last=True)
            if d0 > 1.0:
                warnings.append(f"{resolved[si]['name']} のスナップ誤差 {d0:.2f}km")
            if d1 > 1.0:
                warnings.append(f"{resolved[ei]['name']} のスナップ誤差 {d1:.2f}km")

            if seg_i0 == seg_i1:
                seg_coords = segments[seg_i0]
                if idx_start <= idx_end:
                    piece = seg_coords[idx_start:idx_end + 1]
                else:
                    piece = list(reversed(seg_coords[idx_end:idx_start + 1]))
            else:
                # 湘南新宿ライン・上野東京ラインのように、始点と終点が別々の
                # 支線(MultiLineStringの別要素)に属している場合、支線同士が
                # 実際に繋がっている地点(junction)を探して、そこで2本の
                # ポリラインを繋ぎ合わせる。
                seg_a, seg_b = segments[seg_i0], segments[seg_i1]
                ja, jb, jd = find_junction(seg_a, seg_b)
                if jd > 1.0:
                    warnings.append(
                        f"'{line_name}' の支線接続点にズレがあります({jd:.2f}km): "
                        f"{resolved[si]['name']}-{resolved[ei]['name']} 間"
                    )
                part_a = seg_a[idx_start:ja + 1] if idx_start <= ja else list(reversed(seg_a[ja:idx_start + 1]))
                part_b = seg_b[jb:idx_end + 1] if jb <= idx_end else list(reversed(seg_b[idx_end:jb + 1]))
                if part_a and part_b and part_a[-1] == part_b[0]:
                    piece = part_a[:-1] + part_b
                else:
                    piece = part_a + part_b

        if polyline and piece and polyline[-1] == piece[0]:
            piece = piece[1:]
        polyline.extend(piece)

    involves_loop_line = any(resolve_line_name(line_name) in LOOP_LINES for line_name, _, _ in seg_ranges)
    polyline, back_warnings = remove_backtracking(polyline, skip_collapse=involves_loop_line)
    warnings.extend(back_warnings)

    cum = cumulative_dist(polyline)

    # 駅ごとに polyline 上の位置(path_index)を探す際、前の駅より後ろ側だけを
    # 探索するようにする(search_from)。これにより、大江戸線の都庁前のように
    # 同じ駅の座標が polyline 内に複数回登場する場合でも、config内の並び順
    # (=実際に通る順番)通りに、それぞれ正しい方の出現位置に対応付けられる。
    # 通常の(座標の重複が無い)路線では、この制約があっても結果は変わらない。
    station_records = []
    search_from = 0
    for s in resolved:
        idx, d = nearest_index(polyline, (s["lon"], s["lat"]), start=search_from)
        search_from = idx
        station_records.append({
            "name": s["name"],
            "line": s["line"],
            "lon": polyline[idx][0],
            "lat": polyline[idx][1],
            "t_min": s.get("t_min"),  # 途中駅はNone(未入力)を許容し、下で自動補完する
            "path_index": idx,
            "cum_km": cum[idx],
            "popup": bool(s.get("popup", False)),
            "transfer": bool(s.get("transfer", False)),
            "direct_through": bool(s.get("direct_through", False)),
            "transfer_note": s.get("transfer_note", ""),
        })

    idxs = [s["path_index"] for s in station_records]
    if idxs != sorted(idxs):
        warnings.append(
            f"{route_cfg.get('name')}: 駅の path_index が単調増加していません: {idxs} "
            f"(config内の駅の並び順が実際の進行方向と逆になっている可能性があります)"
        )

    fill_warnings = _fill_missing_t_min(station_records, route_cfg.get("name", route_cfg.get("key")))
    warnings.extend(fill_warnings)

    total_min = station_records[-1]["t_min"]
    color = route_cfg.get("color") or [230, 230, 230]

    result = {
        "key": route_cfg["key"],
        "name": route_cfg["name"],
        "short_name": route_cfg.get("short_name", route_cfg["name"]),
        "color": color,
        "total_min": total_min,
        "polyline": polyline,
        "cum_dist": cum,
        "stations": station_records,
        # ルート全体の既定アイコン画像(省略可、legsモードのicon_pathと同じ
        # 役割)。駅データSHPベースのこの従来モードには "legs" が無いため、
        # 乗り換えごとに画像を切り替えることはできない(常にこの1枚)。
        "icon_path": route_cfg.get("icon_path"),
    }
    return result, warnings


def build_all_routes(config, geojson_path="data/routes.geojson",
                      station_db=None):
    from .station_db import load_station_db
    from .flight_route import build_leg_route

    paths = {}
    all_warnings = []
    needs_station_db = any(route_cfg.get("mode") != "legs" for route_cfg in config["routes"])
    line_geoms = None
    if needs_station_db:
        if station_db is None:
            station_db = load_station_db()
        line_geoms = load_line_geoms(geojson_path)

    for route_cfg in config["routes"]:
        if route_cfg.get("mode") == "legs":
            # 飛行機・バス・徒歩等を組み合わせた区間ベースのルート
            # (station_db / routes.geojson を使わない, flight_route.py 参照)
            route = build_leg_route(route_cfg)
        else:
            route, warnings = build_route(route_cfg, line_geoms, station_db)
            all_warnings.extend(warnings)
        paths[route_cfg["key"]] = route
    return paths, all_warnings


if __name__ == "__main__":
    import sys
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/osaka_kyoto.json"
    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)
    paths, warnings = build_all_routes(config)
    for w in warnings:
        print("[warn]", w)
    for key, route in paths.items():
        print(key, route["name"], "points:", len(route["polyline"]),
              "total_km:", round(route["cum_dist"][-1], 2), "total_min:", route["total_min"])
        for s in route["stations"]:
            print("   ", s["name"], "idx", s["path_index"], "km", round(s["cum_km"], 2), "t", s["t_min"])
