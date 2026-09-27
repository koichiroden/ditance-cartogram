# -*- coding: utf-8 -*-
"""
日本語フォント(Noto Sans CJK)を探す。
Ubuntu/Debian系では `apt-get install fonts-noto-cjk` で以下のパスに入る。
別環境(mac/Render等)でパスが違う場合は、環境変数 RACE_VIDEO_FONT_DIR で
フォントディレクトリを上書きできる。
"""
import os

_CANDIDATE_DIRS = [
    os.environ.get("RACE_VIDEO_FONT_DIR", ""),
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/local/share/fonts",
    "/System/Library/Fonts/Supplemental",  # macOS (代替フォントになる場合あり)
]

_FILES = {
    "bold": "NotoSansCJK-Bold.ttc",
    "regular": "NotoSansCJK-Regular.ttc",
    "black": "NotoSansCJK-Black.ttc",
}

# 上記ttcの中で日本語(JP)書体が入っているface index (fc-scan で確認済み)
JP_INDEX = 0


def _find(filename, required=True):
    for d in _CANDIDATE_DIRS:
        if not d:
            continue
        p = os.path.join(d, filename)
        if os.path.exists(p):
            return p
    if not required:
        return None
    raise FileNotFoundError(
        f"日本語フォント '{filename}' が見つかりません。"
        f"Ubuntu/Debianなら `sudo apt-get install -y fonts-noto-cjk` を実行するか、"
        f"環境変数 RACE_VIDEO_FONT_DIR に .ttc の入ったディレクトリを設定してください。"
    )


FONT_BOLD = None
FONT_REGULAR = None
FONT_BLACK = None


def resolve():
    """
    Bold/Regular/Black の.ttcパスを返す。
    Debianの `apt install fonts-noto-cjk` パッケージは環境によって
    Regular/Boldしか入っておらず、Black相当のウェイトが無いことがある
    (Render上のDockerビルドで確認)。Blackが無ければBoldで、Boldも無ければ
    Regularで代用する(見た目の太さが少し変わるだけで、レイアウトは崩れない)。
    Regularだけは必須(1つも無ければ日本語が描画できないため例外を出す)。
    """
    global FONT_BOLD, FONT_REGULAR, FONT_BLACK
    if FONT_REGULAR is None:
        FONT_REGULAR = _find(_FILES["regular"])
        FONT_BOLD = _find(_FILES["bold"], required=False) or FONT_REGULAR
        FONT_BLACK = _find(_FILES["black"], required=False) or FONT_BOLD
    return FONT_BOLD, FONT_REGULAR, FONT_BLACK
