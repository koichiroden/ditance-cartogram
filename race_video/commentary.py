# -*- coding: utf-8 -*-
"""
競馬実況風・ドーパミン全開の実況テキストをテンプレートから汎用生成する。

どの駅名・どの路線名の組み合わせが来ても使えるように、"通過" "乗換" "到着"
"結果" のイベント種別ごとに文面テンプレートのプールを持ち、そこから
(路線・駅ごとに決定的に)選んで埋め込む方式にしている。
凝った特定路線専用の一文を書きたい場合は、config の各駅に
"line_override_comment" を足すか、テンプレートを増やせばよい。

出力:
    output/<slug>_commentary.txt  ... 台本(音声収録用、タイムコード付き)
    output/<slug>_commentary.srt  ... 字幕ファイル
    (動画内テロップ焼き込み用のタイムラインは辞書として返す)
"""
import random

START_TEMPLATES = [
    "さぁ来ました{start}駅!{matchup}、{end}までの最速対決、スタートです!!",
    "{start}駅に{matchup}が並びました!{end}までどちらが早いのか、レーススタート!!",
]
PASS_TEMPLATES = [
    "{route}、{station}を通過!",
    "{route}、{station}通過!このペースは速い!",
    "{route}も{station}を通過!互角の展開だ!",
    "{route}、{station}を通過、勢いそのままに突き進む!",
    "{route}、{station}通過!観客もどよめくスピード!",
    "{route}、{station}を通過!ここまで危なげない走り!",
]
TRANSFER_TEMPLATES = [
    "{route}は{station}で乗り換え!{note}ここが勝負の分かれ目かー!?",
    "{route}、{station}に到着、乗り換えです。{note}タイムロスなるかー!?",
]
DIRECT_THROUGH_TEMPLATES = [
    "{route}、{station}を通過!{note}勢いを落とさず走り抜ける!",
    "{route}、{station}通過!{note}ここが強みだ!",
]
FINISH_TEMPLATES = [
    "{route}、{end}到着―!!タイムは{time}分!圧巻の走りでゴール!",
    "{route}、ついに{end}到着!記録は{time}分!",
]
RESULT_TEMPLATES = [
    "勝者は{winner}!その差、わずか{diff}分!{start}→{end}、最速の切符はどっちだ!?",
]
RESULT_TIE_TEMPLATES = [
    "なんという結末だ、{winners}が同時到着!{start}→{end}、まさかの引き分けです!!",
]

# 所要時間(分)がこの差以内なら「同時刻」= 引き分けとみなす
TIE_EPSILON_MIN = 1e-6


def _pick(pool, key, salt=""):
    """路線名+駅名+salt から決定的にテンプレートを選ぶ(毎回同じ結果になる)"""
    rnd = random.Random(f"{key}:{salt}")
    return rnd.choice(pool)


def _format_matchup(names):
    """3路線以上でも自然な文になるように、路線名の並びを1つの句にする。
    2つなら「AとB」、3つ以上なら「A、B、C」のように読点でつなぐ。"""
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]}と{names[1]}"
    return "、".join(names)


def build_events(config, paths, intro_sec, ratio_sec_per_min):
    routes = list(paths.values())
    start_name = config.get("start_name", routes[0]["stations"][0]["name"])
    end_label = config.get("end_label", routes[0]["stations"][-1]["name"])

    events = []  # (real_min, speaker_key, speaker_label, text)

    matchup = _format_matchup([r["name"] for r in routes])
    events.append((0, "system", "実況",
                    START_TEMPLATES[0].format(start=start_name, matchup=matchup, end=end_label)))

    for route in routes:
        for st in route["stations"]:
            if st["t_min"] == 0 or st["popup"] is False:
                continue
            is_finish = st is route["stations"][-1]
            if is_finish:
                tmpl = _pick(FINISH_TEMPLATES, route["key"], st["name"])
                text = tmpl.format(route=route["name"], end=st["name"], time=int(route["total_min"]))
                events.append((st["t_min"], route["key"], route["short_name"], text))
            elif st["transfer"] or st.get("direct_through"):
                pool = DIRECT_THROUGH_TEMPLATES if st.get("direct_through") else TRANSFER_TEMPLATES
                tmpl = _pick(pool, route["key"], st["name"])
                note = st["transfer_note"] + ("" if st["transfer_note"].endswith(("!", "。")) else "。") \
                    if st["transfer_note"] else ""
                text = tmpl.format(route=route["name"], station=st["name"], note=note)
                events.append((st["t_min"], route["key"], route["short_name"], text))
            else:
                tmpl = _pick(PASS_TEMPLATES, route["key"], st["name"])
                text = tmpl.format(route=route["name"], station=st["name"])
                events.append((st["t_min"], route["key"], route["short_name"], text))

    best_time = min(r["total_min"] for r in routes)
    winners = [r for r in routes if abs(r["total_min"] - best_time) <= TIE_EPSILON_MIN]
    result_min = max(r["total_min"] for r in routes) + 1
    if len(winners) >= 2:
        winners_text = "、".join(r["name"] for r in winners)
        events.append((result_min, "result", "RESULT",
                        RESULT_TIE_TEMPLATES[0].format(winners=winners_text,
                                                        start=start_name, end=end_label)))
    else:
        winner = winners[0]
        other_times = [r["total_min"] for r in routes if r is not winner]
        diff = min(other_times) - best_time
        events.append((result_min, "result", "RESULT",
                        RESULT_TEMPLATES[0].format(winner=winner["name"], diff=int(diff),
                                                    start=start_name, end=end_label)))

    events.sort(key=lambda e: e[0])

    timeline = []
    for real_min, speaker_key, speaker_label, text in events:
        tv = intro_sec + real_min * ratio_sec_per_min
        dur = 2.6
        timeline.append({
            "t_start": tv, "t_end": tv + dur,
            "speaker_key": speaker_key, "speaker": speaker_label, "text": text,
        })
    return timeline


def srt_timecode(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_script_files(timeline, config, paths, out_dir="output"):
    slug = config.get("slug", "race")
    routes = list(paths.values())
    txt_path = f"{out_dir}/{slug}_commentary.txt"
    srt_path = f"{out_dir}/{slug}_commentary.srt"

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"{config.get('title_line1', slug)} 実況台本\n")
        f.write("=" * 40 + "\n")
        for r in routes:
            f.write(f"{r['name']}: 実要{r['total_min']}分\n")
        f.write("\n")
        for ev in timeline:
            f.write(f"[動画{ev['t_start']:5.1f}秒] ({ev['speaker']}) {ev['text']}\n")

    with open(srt_path, "w", encoding="utf-8") as f:
        lines = []
        for i, ev in enumerate(timeline, start=1):
            lines.append(str(i))
            lines.append(f"{srt_timecode(ev['t_start'])} --> {srt_timecode(ev['t_end'])}")
            lines.append(f"({ev['speaker']}) {ev['text']}")
            lines.append("")
        f.write("\n".join(lines))

    return txt_path, srt_path
