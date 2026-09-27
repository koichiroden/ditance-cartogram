# -*- coding: utf-8 -*-
"""駅の到達時刻(実分)から、任意の実分における線路上の位置(lon,lat)を求める"""
import bisect


class RouteMotion:
    def __init__(self, route):
        self.route = route
        self.t_mins = [s["t_min"] for s in route["stations"]]
        self.cum_kms = [s["cum_km"] for s in route["stations"]]
        self.polyline = route["polyline"]
        self.cum_dist = route["cum_dist"]
        self.total_min = route["total_min"]
        self.total_km = self.cum_dist[-1]

    def km_at(self, real_min):
        real_min = max(0.0, min(real_min, self.total_min))
        ts, ks = self.t_mins, self.cum_kms
        if real_min <= ts[0]:
            return ks[0]
        if real_min >= ts[-1]:
            return ks[-1]
        i = bisect.bisect_right(ts, real_min) - 1
        i = max(0, min(i, len(ts) - 2))
        t0, t1 = ts[i], ts[i + 1]
        k0, k1 = ks[i], ks[i + 1]
        if t1 == t0:
            return k0
        frac = (real_min - t0) / (t1 - t0)
        return k0 + (k1 - k0) * frac

    def lonlat_at(self, real_min):
        km = self.km_at(real_min)
        cd = self.cum_dist
        if km <= cd[0]:
            return tuple(self.polyline[0])
        if km >= cd[-1]:
            return tuple(self.polyline[-1])
        i = bisect.bisect_right(cd, km) - 1
        i = max(0, min(i, len(cd) - 2))
        d0, d1 = cd[i], cd[i + 1]
        p0, p1 = self.polyline[i], self.polyline[i + 1]
        if d1 == d0:
            return tuple(p0)
        frac = (km - d0) / (d1 - d0)
        lon = p0[0] + (p1[0] - p0[0]) * frac
        lat = p0[1] + (p1[1] - p0[1]) * frac
        return (lon, lat)

    def progress(self, real_min):
        if self.total_km <= 0:
            return 0.0
        return max(0.0, min(1.0, self.km_at(real_min) / self.total_km))

    def finished(self, real_min):
        return real_min >= self.total_min
