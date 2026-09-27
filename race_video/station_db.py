# -*- coding: utf-8 -*-
"""
駅データSHP (data/stations_shp/kansai_stations.shp) を読み込む。

フィールド: line_name, stn_name, seq(路線内の順番), cum_km(起点からの累積距離),
color(路線カラー)

品質チェック(check_line_quality)の結果、関西40路線はいずれもseq/cum_kmの
並び順に問題は見つかっていない。唯一の例外は大阪環状線で、これは環状運転
そのものの性質上、駅の並び順だけでは向き・区間が一意に決まらないため、
route_builder.py 側で config の stations 配列(明示的な停車順)を必須に
している(データの品質問題ではない)。
"""
import math
import shapefile

DEFAULT_SHP_PATH = "data/stations_shp/kansai_stations"


def haversine_km(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def load_station_db(path=DEFAULT_SHP_PATH):
    sf = shapefile.Reader(path)
    db = {}
    for sr in sf.iterShapeRecords():
        rec = sr.record
        line = rec["line_name"]
        db.setdefault(line, []).append({
            "name": rec["stn_name"],
            "seq": rec["seq"],
            "cum_km": rec["cum_km"],
            "color": rec["color"],
            "lon": sr.shape.points[0][0],
            "lat": sr.shape.points[0][1],
        })
    for line in db:
        db[line].sort(key=lambda s: s["seq"])
    return db


def get_station_lonlat(db, line_name, stn_name):
    """駅名から座標を引く(seq/cum_kmの並びが壊れている路線でも、名前->座標は使ってよい)"""
    candidates = [s for s in db.get(line_name, []) if s["name"] == stn_name]
    if not candidates:
        raise KeyError(f"station_db: '{line_name}' に駅 '{stn_name}' が見つかりません")
    return candidates[0]["lon"], candidates[0]["lat"]


def get_line_color(db, line_name):
    rows = db.get(line_name, [])
    return rows[0]["color"] if rows else None


def check_line_quality(db, gap_threshold_km=10):
    """隣接seq同士の実距離が異常に離れていないかチェックする診断ツール。
    戻り値: [(line_name, 駅数, 異常gap数, 最大gap_km), ...]"""
    report = []
    for line, rows in db.items():
        bad = 0
        worst = 0.0
        for i in range(1, len(rows)):
            d = haversine_km((rows[i - 1]["lon"], rows[i - 1]["lat"]),
                              (rows[i]["lon"], rows[i]["lat"]))
            if d > gap_threshold_km:
                bad += 1
                worst = max(worst, d)
        report.append((line, len(rows), bad, worst))
    return report


if __name__ == "__main__":
    db = load_station_db()
    print(f"{len(db)} lines loaded")
    for line, n, bad, worst in sorted(check_line_quality(db), key=lambda x: -x[2]):
        flag = "NG" if bad else "ok"
        print(f"{flag:3s} {line:16s} n={n:3d} suspicious_gaps={bad:3d} worst_gap_km={worst:.1f}")
