# -*- coding: utf-8 -*-
"""
ブラウザから動画生成を実行するための、ごく簡単なWebラッパー。
自分のPC上でも(`python3 web/app.py`)、Renderなどのホスティング上でも
同じように動く。

- 駅レースビルダー(Claude Artifact)で作ったJSONを貼り付け/ファイル選択で
  読み込み、バックグラウンドで `python3 -m race_video.cli configs/<slug>.json`
  を実行する。
- configs/*.json に保存済みの設定からも生成できる。
- 生成状況はポーリングで確認し、完了したらそのままブラウザから
  mp4をダウンロードできる。
- 出力ファイル名は、config の 'slug'(=保存先ファイル名)とは別に、
  生成のたびに自由に指定できる(空欄ならslugがそのまま使われる)。

ローカルで試す場合:
    pip install -r requirements.txt
    python3 web/app.py
    -> http://127.0.0.1:8080 を開く
"""
import json
import os
import sys
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file, send_from_directory

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 車両アイコン画像置き場。ここに置いたファイルは、駅レースビルダー側で何かを
# アップロードしなくても、このページ上のドロップダウンから選べるようになる
# (icon_path は "assets/<ファイル名>" という相対パスとして config JSON に入る)。
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
ALLOWED_ICON_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 自分のPCならもっと速く終わるはずだが、Renderの無料プラン(0.1 CPU)などの
# 非力な環境だとフルクオリティ(30fps)の生成に数十分かかることがあるため、
# 安全マージンを広めに取っておく。
JOB_TIMEOUT_SEC = 2700

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OUT_NAME_BAD_RE = re.compile(r"[\\/\x00-\x1f]")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # アイコン画像は8MBまで


@app.after_request
def _no_cache(resp):
    # このアプリは自分のPC上でapp.pyを書き換えて再起動する運用なので、
    # ブラウザがページ/JSONを古いままキャッシュしてしまうと「直したのに
    # 反映されない(機能が消えたように見える)」という混乱が起きやすい。
    # そのため常にキャッシュさせない。
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

jobs = {}
jobs_lock = threading.Lock()

# サーバーが生成の途中で再起動されてしまった場合(無料プランのメモリ/CPU制限
# など)、ジョブ情報はメモリ上の jobs dict ごと消えてしまい、ポーリング中の
# ブラウザには "not found" とだけ返ってしまって原因が分かりにくい。
# job_id -> {slug, file_name} の対応表だけは軽量なファイルに書き出しておき、
# jobsから消えていた場合でも「サーバーが再起動した可能性がある」という
# 分かりやすいメッセージと、出力ファイルが実際にできていれば復旧できるように
# しておく。
JOBS_STATE_PATH = OUTPUT_DIR / "_jobs_state.json"
_jobs_state_lock = threading.Lock()


def _load_jobs_state():
    try:
        with open(JOBS_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _remember_job(job_id, slug, file_name):
    with _jobs_state_lock:
        state = _load_jobs_state()
        state[job_id] = {"slug": slug, "file_name": file_name}
        if len(state) > 50:  # 肥大化防止に直近50件だけ残す
            for k in list(state.keys())[:-50]:
                state.pop(k, None)
        try:
            with open(JOBS_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass


def _sanitize_out_name(raw):
    """出力ファイル名(拡張子なし)として安全かどうかを確認する。
    戻り値は (name, error) のタプル。
    空欄は「指定なし(configのslugを使う)」という意味で ("", None) を返す。
    使えない文字が含まれる場合は (None, エラーメッセージ) を返す。"""
    name = (raw or "").strip()
    if not name:
        return "", None
    if len(name) > 100 or _OUT_NAME_BAD_RE.search(name) or ".." in name or name.strip(".") == "":
        return None, "出力ファイル名に使えない文字が含まれています(/ や \\、'..' は使えません)"
    return name, None


def list_configs():
    items = []
    for p in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            title = f"{cfg.get('title_line1', p.stem)} {cfg.get('title_line2', '')}".strip()
        except Exception:
            title = p.stem
        items.append({"slug": p.stem, "title": title})
    return items


def list_icon_assets():
    """assets/ にある画像ファイル名の一覧(ファイル名のみ、パスなし)を返す。"""
    items = []
    for p in sorted(ASSETS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in ALLOWED_ICON_EXT:
            items.append(p.name)
    return items


def run_job(job_id, slug, fast, out_name=None):
    cfg_path = CONFIGS_DIR / f"{slug}.json"
    file_stem = out_name or slug
    cmd = ["python3", "-m", "race_video.cli", str(cfg_path)]
    if fast:
        cmd.append("--fast")
    if out_name:
        cmd += ["--out-name", out_name]
    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=JOB_TIMEOUT_SEC,
        )
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        with jobs_lock:
            jobs[job_id]["log"] = log[-4000:]
            if proc.returncode == 0 and (OUTPUT_DIR / f"{file_stem}.mp4").exists():
                jobs[job_id]["status"] = "done"
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"終了コード {proc.returncode}(ログを参照)"
    except subprocess.TimeoutExpired as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["log"] = ((e.stdout or "") + "\n" + (e.stderr or ""))[-4000:]
            jobs[job_id]["error"] = (
                f"{JOB_TIMEOUT_SEC}秒以内に終わりませんでした。"
                f"CPUが非力な環境だと、フルクオリティの生成は間に合わないことがあります。"
                f"「低画質プレビュー(高速)」を試すか、より速いPC/有料プランを検討してください。"
            )
    except Exception as e:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


def _start_job(slug, fast, out_name_raw):
    """out_nameのバリデーション込みでジョブを開始する共通処理。
    戻り値は (response_dict, http_status)。"""
    out_name, err = _sanitize_out_name(out_name_raw)
    if err:
        return {"error": err}, 400
    file_name = f"{out_name or slug}.mp4"
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "slug": slug, "file_name": file_name, "log": "", "error": ""}
    _remember_job(job_id, slug, file_name)
    threading.Thread(target=run_job, args=(job_id, slug, fast, out_name or None), daemon=True).start()
    return {"job_id": job_id, "slug": slug, "file_name": file_name}, 200


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>電車最速バトル 動画生成</title>
<style>
  body { font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; color: #201c16; background: #f6f2ea; }
  h1 { font-size: 22px; }
  h2.section { font-size: 15px; margin: 28px 0 10px; }
  .note { font-size: 12.5px; color: #a3690a; background: #f8ecd6; border-radius: 7px;
          padding: 8px 10px; margin: 0 0 16px; }
  .card { background: #fff; border: 1px solid #ded2ba; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 16px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .title { font-weight: 600; }
  .slug { color: #766c5c; font-size: 12.5px; font-family: monospace; }
  button { font: inherit; padding: 8px 14px; border-radius: 7px; border: 1px solid #201c16;
           background: #201c16; color: #f6f2ea; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  label.fast { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 4px; }
  .status { font-size: 13px; margin-top: 8px; color: #766c5c; white-space: pre-wrap; }
  .status.ok { color: #21785a; }
  .status.err { color: #b23b3b; }
  a.dl { color: #1a56c9; font-weight: 600; text-decoration: none; }
  .empty { color: #9c907c; }
  textarea.jsonbox { width: 100%; min-height: 160px; font-family: monospace; font-size: 12.5px;
                      padding: 10px; border-radius: 7px; border: 1px solid #ded2ba; box-sizing: border-box;
                      background: #fbf8f2; color: #201c16; }
  .paste-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
  input.outname { font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px;
                   border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; width: 200px; }
  select.config-picker { font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px;
                          border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; }
  label.outname-label { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 6px; }
  input[type="file"] { font-size: 12.5px; }
  .icon-picker { margin-top: 12px; border-top: 1px dashed #ded2ba; padding-top: 4px; }
  .icon-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; flex-wrap: wrap; }
  .icon-row .rname { font-size: 13px; font-weight: 600; min-width: 180px; max-width: 260px; }
  .icon-row select { font: inherit; font-size: 12.5px; padding: 5px 6px; border-radius: 6px;
                      border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; }
  .icon-row img.thumb { width: 34px; height: 34px; border-radius: 6px; object-fit: contain;
                         background: #f0ead9; border: 1px solid #ded2ba; }
  .icon-row label.upload-lbl { font-size: 11.5px; color: #1a56c9; cursor: pointer; text-decoration: underline; }
  .icon-row input[type="file"] { display: none; }
</style>
</head>
<body>
  <h1>電車最速バトル — 動画生成</h1>
  <p class="note">自分のPCで動かす場合はフルクオリティでも数分で終わります。CPUが非力な環境(無料ホスティングなど)では、まず「低画質プレビュー(高速)」をおすすめします。</p>

  <h2 class="section">① JSONを用意して生成</h2>
  <div class="card">
    <p style="margin-top:0; font-size:13px; color:#766c5c;">
      駅レースビルダーで作ったconfig JSONを、<span class="slug">configs/</span>から選ぶか、ファイルから読み込むか、貼り付けて生成できます
      (新規の場合は <span class="slug">configs/&lt;slug&gt;.json</span> として保存されます)。
    </p>
    <div class="paste-actions" style="margin-top:0; margin-bottom:8px;">
      <select class="config-picker" id="config-picker">
        <option value="">— configs/ から選んで読み込む —</option>
        {% for c in configs %}
        <option value="{{ c.slug }}">{{ c.title }} ({{ c.slug }}.json)</option>
        {% endfor %}
      </select>
      <input type="file" id="file-input" accept=".json,application/json">
      <button id="clip-btn" type="button">📋 クリップボードから貼り付け</button>
      <span class="status" id="clip-status" style="margin-top:0;"></span>
    </div>
    <textarea class="jsonbox" id="paste-json" placeholder='{"slug": "...", "routes": [...] }'></textarea>

    <div class="icon-picker" id="icon-picker">
      <p class="empty" style="margin:10px 0 0; font-size:12.5px;">JSONを読み込むと、ここで区間(ノード)ごとに車両アイコンを選べます(未指定ならデフォルトのアイコンを使用します)。</p>
    </div>

    <div class="paste-actions">
      <label class="fast"><input type="checkbox" id="paste-fast" checked> 低画質プレビュー(高速)</label>
      <label class="outname-label">出力ファイル名(任意)
        <input type="text" class="outname" id="paste-outname" placeholder="省略時はslugを使用">
      </label>
      <button id="paste-btn">保存して生成する</button>
    </div>
    <div class="status" id="paste-status"></div>
  </div>

  <h2 class="section">② 保存済みの設定から生成</h2>
  {% if not configs %}
    <p class="empty">configs/ にJSONファイルが見つかりません。</p>
  {% endif %}
  {% for c in configs %}
  <div class="card" data-slug="{{ c.slug }}">
    <div class="row">
      <div>
        <div class="title">{{ c.title }}</div>
        <div class="slug">{{ c.slug }}.json</div>
      </div>
    </div>
    <div class="paste-actions">
      <label class="fast"><input type="checkbox" class="fast-cb" checked> 低画質プレビュー(高速)</label>
      <label class="outname-label">出力ファイル名(任意)
        <input type="text" class="outname" placeholder="省略時は {{ c.slug }} を使用">
      </label>
      <button class="gen-btn">生成する</button>
    </div>
    <div class="status"></div>
  </div>
  {% endfor %}

  <h2 class="section">③ 時間距離カルトグラム(近いのに遠い？遠いのに近い？)を47都道府県まとめて生成</h2>
  <div class="card" id="tc-card">
    <div class="paste-actions">
      <label>モード
        <select id="tc-mode">
          <option value="both">鉄道版＋飛行機版(93本)</option>
          <option value="rail">鉄道版だけ(46本)</option>
          <option value="best">鉄道＋飛行機版だけ(47本)</option>
        </select>
      </label>
      <label class="outname-label">県を絞る(任意・ローマ字カンマ区切り)
        <input type="text" id="tc-only" placeholder="例: tokyo,osaka,fukuoka(空欄なら47都道府県)">
      </label>
      <label class="fast"><input type="checkbox" id="tc-fast" checked> 低画質プレビュー(高速)</label>
      <button id="tc-btn">まとめて生成する</button>
    </div>
    <div class="status" id="tc-status"></div>
    <p class="empty" style="font-size:12px;margin:8px 0 0;">1本あたりフル画質で約2分(PCの速さによる)。93本だとかなり時間がかかるので、自分のPCで
      <code>python3 tools/render_timecarto_all.py</code> を実行するのがおすすめです。完成するとzipでダウンロードできます。</p>
  </div>

<script>
document.getElementById("tc-btn").addEventListener("click", () => {
  const btn = document.getElementById("tc-btn");
  const st = document.getElementById("tc-status");
  const body = new FormData();
  body.append("mode", document.getElementById("tc-mode").value);
  body.append("only", document.getElementById("tc-only").value);
  body.append("fast", document.getElementById("tc-fast").checked ? "1" : "0");
  btn.disabled = true; btn.textContent = "生成中…";
  st.className = "status"; st.textContent = "開始しました…";
  fetch("/timecarto_batch", {method: "POST", body}).then(r => r.json()).then(data => {
    if (!data.job_id) { st.className = "status err"; st.textContent = data.error || "開始できませんでした"; btn.disabled = false; btn.textContent = "まとめて生成する"; return; }
    const timer = setInterval(() => {
      fetch("/timecarto_batch_status/" + data.job_id).then(r => r.json()).then(j => {
        if (j.status === "running") {
          st.textContent = "生成中… " + (j.progress ? j.progress + " 本目まで完了" : "");
        } else {
          clearInterval(timer); btn.disabled = false; btn.textContent = "まとめて生成する";
          if (j.status === "done") {
            st.className = "status ok";
            st.innerHTML = '完成しました: <a href="/download_zip/' + data.file_name + '">' + data.file_name + ' をダウンロード</a>';
          } else {
            st.className = "status err";
            st.innerHTML = "";
            const msg = document.createElement("div"); msg.textContent = "エラー: " + (j.error || "");
            const pre = document.createElement("pre");
            pre.style.cssText = "white-space:pre-wrap;font-size:11px;max-height:260px;overflow:auto;background:#f3efe6;padding:8px;border-radius:6px;color:#201c16";
            pre.textContent = (j.log || "(ログなし)").split("\\n").slice(-40).join("\\n");
            st.appendChild(msg); st.appendChild(pre);
          }
        }
      });
    }, 4000);
  });
});
</script>

<script>
let knownIcons = {{ icons|tojson }};
let iconOverrides = {};  // ターゲットkey -> icon_path("" ならデフォルトアイコン)

const ICON_TYPE_LABEL = {plane: "飛行機", train: "電車・新幹線", bus: "バス", walk: "徒歩", monorail: "モノレール"};

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function configFromTextarea() {
  try {
    const cfg = JSON.parse(document.getElementById("paste-json").value);
    if (cfg && Array.isArray(cfg.routes)) return cfg;
  } catch (e) { /* JSONが不完全な間は何もしない */ }
  return null;
}

// 駅レースビルダー(ノード方式)で作ったJSONでは、1つのノード(区間)が
// 経由駅の分だけ複数の leg に分割されていることがある(例: 新幹線の
// 東京→岡山→博多は、東京→岡山・岡山→博多の2つの leg に分かれる)。
// これらは同じ leg.node_id を共有しているので、ここではその node_id で
// まとめて1行のアイコン選択にする(＝ユーザーがビルダーで追加した
// 「ノード」の単位と一致させる)。node_id を持たない古い形式のJSONは、
// leg 1つ1つを個別のターゲットとして扱う(従来通り)。
// legIndexes が null のターゲットは leg を持たない従来ルートで、
// route.icon_path をそのまま上書きする対象になる。
function buildLegTargets(cfg) {
  const targets = [];
  cfg.routes.forEach((r, ri) => {
    const routeName = r.short_name || r.name || ("ルート" + (ri + 1));
    if (Array.isArray(r.legs) && r.legs.length) {
      const groups = [];                 // [{nodeId, legIndexes:[...]}] 出現順
      const groupByNodeId = new Map();
      r.legs.forEach((leg, li) => {
        if (leg.kind === "wait" || leg.icon === "wait") return; // 待機中は対象外
        if (leg.node_id) {
          let g = groupByNodeId.get(leg.node_id);
          if (!g) {
            g = {nodeId: leg.node_id, legIndexes: []};
            groupByNodeId.set(leg.node_id, g);
            groups.push(g);
          }
          g.legIndexes.push(li);
        } else {
          groups.push({nodeId: null, legIndexes: [li]});
        }
      });
      groups.forEach((g, gi) => {
        const legs = g.legIndexes.map(li => r.legs[li]);
        const lastLeg = legs[legs.length - 1];
        const kindLabel = ICON_TYPE_LABEL[lastLeg.icon] || lastLeg.icon || "";
        const detail = lastLeg.label || kindLabel;
        const key = g.nodeId ? (ri + ":node:" + g.nodeId) : (ri + ":leg:" + g.legIndexes[0]);
        targets.push({
          key, routeIndex: ri, legIndexes: g.legIndexes,
          label: routeName + " " + (gi + 1) + (detail ? ": " + detail : ""),
        });
      });
    } else {
      // legsを持たない従来ルートは、ルート全体を1つのターゲットとして扱う
      targets.push({key: ri + ":route", routeIndex: ri, legIndexes: null, label: routeName});
    }
  });
  return targets;
}

function setThumb(imgEl, val) {
  if (val) { imgEl.src = "/" + val; imgEl.style.display = ""; }
  else { imgEl.removeAttribute("src"); imgEl.style.display = "none"; }
}

function renderIconPicker() {
  const wrap = document.getElementById("icon-picker");
  const cfg = configFromTextarea();
  const targets = cfg ? buildLegTargets(cfg) : [];
  if (!cfg || !targets.length) {
    wrap.innerHTML = '<p class="empty" style="margin:10px 0 0; font-size:12.5px;">JSONを読み込むと、ここで区間(ノード)ごとに車両アイコンを選べます(未指定ならデフォルトのアイコンを使用します)。</p>';
    return;
  }
  wrap.innerHTML = "";
  targets.forEach(t => {
    const obj = t.legIndexes === null ? cfg.routes[t.routeIndex] : cfg.routes[t.routeIndex].legs[t.legIndexes[0]];
    const original = (obj && obj.icon_path) || "";
    const current = Object.prototype.hasOwnProperty.call(iconOverrides, t.key) ? iconOverrides[t.key] : original;
    const row = document.createElement("div");
    row.className = "icon-row";

    const name = document.createElement("span");
    name.className = "rname";
    name.textContent = t.label;

    const thumb = document.createElement("img");
    thumb.className = "thumb";
    setThumb(thumb, current);

    const select = document.createElement("select");
    select.appendChild(new Option("デフォルトのアイコンを使用", ""));
    knownIcons.forEach(fn => select.appendChild(new Option(fn, "assets/" + fn)));
    const currentFile = current.replace(/^assets\//, "");
    if (current && !knownIcons.includes(currentFile)) {
      select.appendChild(new Option(currentFile + "(現在の設定)", current));
    }
    select.value = current;
    select.addEventListener("change", () => {
      iconOverrides[t.key] = select.value;
      setThumb(thumb, select.value);
    });

    const uploadLbl = document.createElement("label");
    uploadLbl.className = "upload-lbl";
    uploadLbl.textContent = "+ 新しい画像をアップロード";
    const fileInp = document.createElement("input");
    fileInp.type = "file";
    fileInp.accept = "image/*";
    uploadLbl.appendChild(fileInp);
    fileInp.addEventListener("change", async () => {
      const f = fileInp.files && fileInp.files[0];
      if (!f) return;
      uploadLbl.textContent = "アップロード中…";
      uploadLbl.appendChild(fileInp);
      const fd = new FormData();
      fd.append("icon", f);
      try {
        const res = await fetch("/upload_icon", { method: "POST", body: fd });
        const data = await res.json();
        if (data.icon_path) {
          if (!knownIcons.includes(data.filename)) knownIcons.push(data.filename);
          iconOverrides[t.key] = data.icon_path;
          renderIconPicker();
        } else {
          alert(data.error || "アップロードに失敗しました");
          uploadLbl.textContent = "+ 新しい画像をアップロード";
          uploadLbl.appendChild(fileInp);
        }
      } catch (e) {
        alert("アップロードに失敗しました");
        uploadLbl.textContent = "+ 新しい画像をアップロード";
        uploadLbl.appendChild(fileInp);
      }
    });

    row.append(name, thumb, select, uploadLbl);
    wrap.appendChild(row);
  });
}

function pollJob(jobId, statusEl, btn, doneLabel) {
  fetch("/status/" + jobId).then(r => r.json()).then(job => {
    if (job.status === "running") {
      statusEl.className = "status";
      statusEl.textContent = "生成中…(CPUが非力な環境だと数分~十数分かかることがあります)";
      setTimeout(() => pollJob(jobId, statusEl, btn, doneLabel), 3000);
    } else if (job.status === "done") {
      const fname = job.file_name || "output.mp4";
      statusEl.className = "status ok";
      statusEl.innerHTML = "完成しました → <a class='dl' href='/download/" +
        encodeURIComponent(fname.replace(/\\.mp4$/, "")) + "'>" + fname + " をダウンロード</a>";
      btn.disabled = false;
      btn.textContent = doneLabel;
    } else {
      statusEl.className = "status err";
      statusEl.textContent = "エラー: " + (job.error || "不明なエラー") + "\\n\\n" + (job.log || "");
      btn.disabled = false;
      btn.textContent = "再試行";
    }
  }).catch(() => setTimeout(() => pollJob(jobId, statusEl, btn, doneLabel), 4000));
}

document.querySelectorAll(".card[data-slug]").forEach(card => {
  const slug = card.dataset.slug;
  const btn = card.querySelector(".gen-btn");
  const statusEl = card.querySelector(".status");
  const fastCb = card.querySelector(".fast-cb");
  const outnameInp = card.querySelector(".outname");

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "生成中…";
    statusEl.className = "status";
    statusEl.textContent = "開始しています…";
    const body = new URLSearchParams();
    if (fastCb.checked) body.set("fast", "1");
    if (outnameInp.value.trim()) body.set("out_name", outnameInp.value.trim());
    fetch("/generate/" + slug, { method: "POST", body })
      .then(r => r.json())
      .then(data => {
        if (data.job_id) pollJob(data.job_id, statusEl, btn, "もう一度生成する");
        else { statusEl.className = "status err"; statusEl.textContent = data.error || "開始できませんでした"; btn.disabled = false; btn.textContent = "生成する"; }
      });
  });
});

document.getElementById("paste-json").addEventListener("input", debounce(renderIconPicker, 400));

document.getElementById("config-picker").addEventListener("change", async (e) => {
  const slug = e.target.value;
  const clipStatus = document.getElementById("clip-status");
  if (!slug) return;
  clipStatus.className = "status";
  clipStatus.textContent = "読み込んでいます…";
  try {
    const res = await fetch("/config_json/" + encodeURIComponent(slug));
    const text = await res.text();
    if (!res.ok) throw new Error(text);
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = slug + ".json を読み込みました";
    iconOverrides = {};
    renderIconPicker();
  } catch (err) {
    clipStatus.className = "status err";
    clipStatus.textContent = "読み込みに失敗しました";
  }
});

document.getElementById("file-input").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const clipStatus = document.getElementById("clip-status");
  file.text().then(text => {
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = file.name + " を読み込みました";
    iconOverrides = {};
    renderIconPicker();
  }).catch(() => {
    clipStatus.className = "status err";
    clipStatus.textContent = "ファイルの読み込みに失敗しました";
  });
});

document.getElementById("clip-btn").addEventListener("click", async () => {
  const clipStatus = document.getElementById("clip-status");
  clipStatus.className = "status";
  clipStatus.textContent = "";
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      throw new Error("unsupported");
    }
    const text = await navigator.clipboard.readText();
    if (!text || !text.trim()) {
      clipStatus.className = "status err";
      clipStatus.textContent = "クリップボードが空でした";
      return;
    }
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = "貼り付けました";
    iconOverrides = {};
    renderIconPicker();
  } catch (e) {
    clipStatus.className = "status err";
    clipStatus.textContent = "自動で読み取れませんでした。テキストエリアを選んで手動で貼り付けて(Ctrl+V / Cmd+V)ください";
  }
});

document.getElementById("paste-btn").addEventListener("click", () => {
  const btn = document.getElementById("paste-btn");
  const statusEl = document.getElementById("paste-status");
  let text = document.getElementById("paste-json").value;
  try {
    const cfg = JSON.parse(text);
    if (Array.isArray(cfg.routes)) {
      // 表示中と同じ規則(node_id単位のグルーピング)でターゲットを組み立て直し、
      // 選んだアイコンをそのグループに属する leg 全部(=1つのノード分)に適用する。
      const targetByKey = {};
      buildLegTargets(cfg).forEach(t => { targetByKey[t.key] = t; });
      Object.keys(iconOverrides).forEach(key => {
        const t = targetByKey[key];
        if (!t) return;
        const val = iconOverrides[key];
        const route = cfg.routes[t.routeIndex];
        if (!route) return;
        if (t.legIndexes === null) {
          if (val) route.icon_path = val;
          else delete route.icon_path;
        } else {
          t.legIndexes.forEach(li => {
            const leg = (route.legs || [])[li];
            if (!leg) return;
            if (val) leg.icon_path = val;
            else delete leg.icon_path;
          });
        }
      });
      text = JSON.stringify(cfg);
    }
  } catch (e) { /* JSON構文エラーはこの後のサーバー側チェックに任せる */ }
  const fast = document.getElementById("paste-fast").checked;
  const outName = document.getElementById("paste-outname").value.trim();
  btn.disabled = true;
  btn.textContent = "保存しています…";
  statusEl.className = "status";
  statusEl.textContent = "";
  const body = new URLSearchParams();
  body.set("json_text", text);
  if (fast) body.set("fast", "1");
  if (outName) body.set("out_name", outName);
  fetch("/generate_from_json", { method: "POST", body })
    .then(r => r.json())
    .then(data => {
      if (data.job_id) {
        btn.textContent = "生成中…";
        pollJob(data.job_id, statusEl, btn, "もう一度保存して生成する");
      } else {
        statusEl.className = "status err";
        statusEl.textContent = data.error || "開始できませんでした";
        btn.disabled = false;
        btn.textContent = "保存して生成する";
      }
    });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, configs=list_configs(), icons=list_icon_assets())


@app.route("/assets/<path:filename>")
def serve_asset(filename):
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/upload_icon", methods=["POST"])
def upload_icon():
    f = request.files.get("icon")
    if not f or not f.filename:
        return jsonify({"error": "ファイルが選択されていません"}), 400
    orig = Path(f.filename)
    ext = orig.suffix.lower()
    if ext not in ALLOWED_ICON_EXT:
        return jsonify({"error": "対応していない形式です(png / jpg / jpeg / webp / gif のみ)"}), 400
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", orig.stem).strip("_")[:60] or "icon"
    dest = ASSETS_DIR / f"{stem}{ext}"
    n = 1
    while dest.exists():
        dest = ASSETS_DIR / f"{stem}_{n}{ext}"
        n += 1
    f.save(dest)
    return jsonify({"filename": dest.name, "icon_path": f"assets/{dest.name}"})


@app.route("/config_json/<slug>")
def config_json(slug):
    valid = {c["slug"] for c in list_configs()}
    if slug not in valid:
        return jsonify({"error": "そのconfigは見つかりません"}), 404
    path = CONFIGS_DIR / f"{slug}.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return data, 200, {"Content-Type": "application/json; charset=utf-8"}


@app.route("/generate/<slug>", methods=["POST"])
def generate(slug):
    valid = {c["slug"] for c in list_configs()}
    if slug not in valid:
        return jsonify({"error": "そのconfigは見つかりません"}), 404
    fast = request.form.get("fast") == "1"
    data, status_code = _start_job(slug, fast, request.form.get("out_name", ""))
    return jsonify(data), status_code


@app.route("/generate_from_json", methods=["POST"])
def generate_from_json():
    raw = request.form.get("json_text", "")
    if not raw.strip():
        return jsonify({"error": "JSONを貼り付けてください"}), 400
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSONの構文エラー: {e}"}), 400

    is_carto = isinstance(config, dict) and config.get("type") == "time_cartogram"
    if is_carto and not config.get("destinations"):
        return jsonify({"error": "時間距離カルトグラムのconfigに 'destinations' がありません"}), 400
    if not is_carto and (not isinstance(config, dict) or not config.get("routes")):
        return jsonify({"error": "'routes' を含むconfigオブジェクトではないようです"}), 400

    slug = str(config.get("slug", "")).strip()
    if not slug:
        return jsonify({"error": "configに 'slug' が必要です"}), 400
    if not _SLUG_RE.match(slug):
        return jsonify({"error": "slugは英数字とハイフン・アンダースコアのみ使えます(64文字以内)"}), 400

    cfg_path = CONFIGS_DIR / f"{slug}.json"
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return jsonify({"error": f"保存に失敗しました: {e}"}), 500

    fast = request.form.get("fast") == "1"
    data, status_code = _start_job(slug, fast, request.form.get("out_name", ""))
    return jsonify(data), status_code


# ---------------------------------------------------------------------------
# 時間距離カルトグラム: 47都道府県ぶんを一括生成(tools/render_timecarto_all.py を実行)
# ---------------------------------------------------------------------------
batch_jobs = {}


def _run_timecarto_batch(job_id, mode, only, fast, zip_name):
    # Webサーバーと同じPython(仮想環境を含む)で実行する
    cmd = [sys.executable, "tools/render_timecarto_all.py", "--mode", mode,
           "--zip", str(OUTPUT_DIR / zip_name), "--skip-existing"]
    if only:
        cmd += ["--only", only]
    if fast:
        cmd.append("--fast")
    try:
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            with jobs_lock:
                job = batch_jobs[job_id]
                job["log"] = (job["log"] + line)[-6000:]
                if line.startswith("[") and "/" in line.split("]")[0]:
                    job["progress"] = line.split("]")[0].strip("[")
        proc.wait()
        with jobs_lock:
            ok = proc.returncode == 0 and (OUTPUT_DIR / zip_name).exists()
            batch_jobs[job_id]["status"] = "done" if ok else "error"
            if not ok:
                batch_jobs[job_id]["error"] = f"終了コード {proc.returncode}(ログを参照)"
    except Exception as e:  # noqa: BLE001
        with jobs_lock:
            batch_jobs[job_id]["status"] = "error"
            batch_jobs[job_id]["error"] = str(e)


@app.route("/timecarto_batch", methods=["POST"])
def timecarto_batch():
    mode = request.form.get("mode", "both")
    if mode not in ("rail", "best", "both"):
        return jsonify({"error": "mode は rail / best / both のどれかです"}), 400
    only = re.sub(r"[^a-z,]", "", request.form.get("only", "").lower())
    fast = request.form.get("fast") == "1"
    zip_name = f"timecarto_{mode}_{uuid.uuid4().hex[:6]}.zip"
    job_id = str(uuid.uuid4())
    with jobs_lock:
        batch_jobs[job_id] = {"status": "running", "file_name": zip_name, "log": "", "error": "", "progress": ""}
    threading.Thread(target=_run_timecarto_batch, args=(job_id, mode, only, fast, zip_name), daemon=True).start()
    return jsonify({"job_id": job_id, "file_name": zip_name})


@app.route("/timecarto_batch_status/<job_id>")
def timecarto_batch_status(job_id):
    with jobs_lock:
        job = batch_jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "ジョブが見つかりません(サーバーが再起動された可能性があります)"}), 404
    return jsonify(job)


@app.route("/download_zip/<name>")
def download_zip(name):
    if not re.fullmatch(r"timecarto_[a-z]+_[0-9a-f]{6}\.zip", name):
        return "不正なファイル名です", 400
    path = OUTPUT_DIR / name
    if not path.exists():
        return "まだ生成されていません", 404
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job:
        return jsonify(job)

    # jobsに無い = 生成中にプロセスが再起動された可能性がある(メモリ/CPU制限
    # など)。job_id -> {slug, file_name} の対応表(ファイル)が残っていれば、
    # 出力ファイルの有無から状況を推測して分かりやすいメッセージを返す。
    rec = _load_jobs_state().get(job_id)
    if rec:
        file_name = rec.get("file_name") or f"{rec['slug']}.mp4"
        if (OUTPUT_DIR / file_name).exists():
            return jsonify({"status": "done", "slug": rec["slug"], "file_name": file_name, "log": "", "error": ""})
        return jsonify({
            "status": "error", "slug": rec["slug"], "file_name": file_name, "log": "",
            "error": (
                "生成の途中でサーバーが再起動された可能性があります"
                "(メモリ・CPU制限によるものと考えられます)。"
                "お手数ですが、もう一度「低画質プレビュー(高速)」で試してみてください。"
            ),
        })
    return jsonify({"error": "not found"}), 404


@app.route("/download/<name>")
def download(name):
    path = OUTPUT_DIR / f"{name}.mp4"
    if not path.exists():
        return "まだ生成されていません", 404
    return send_file(path, as_attachment=True, download_name=f"{name}.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
