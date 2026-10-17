#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
王者荣耀返场投票 · 抓取器（fetcher）

新架构：不依赖常驻 Python 服务，抓取由 GitHub Actions 定时执行，
结果落盘为 data/latest.json + data/history.json + data/status.json，
前端（index.html）在 GitHub Pages 上直接读取这些静态 JSON ——
所以你在手机/任何设备上都能随时打开看板，电脑不用开机。

本脚本在「本地」和「GitHub Actions」里都能直接运行：
    python fetcher.py                     # 正常抓取一次
    python fetcher.py --import "文本"      # 手动导入（抓包失败时的兜底）

健壮性设计：
  - 未配置活动参数 → 不崩溃，status.json 写清原因，退出码 0
  - 网络请求超时 15 秒、失败自动重试 3 次（间隔递增）
  - 接口字段缺失/格式变化 → 容错扫描 + 明确错误信息
  - 票数没有变化 → 不重写数据文件，避免无意义的提交和 Pages 重建
  - status.json 只在状态真正变化时写入，让 Git 提交数最小化
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "数据源配置.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
STATUS_PATH = os.path.join(DATA_DIR, "status.json")

HISTORY_CAP = 500          # history.json 最多保留的快照数
HTTP_TIMEOUT = 15          # 单次请求超时（秒）
MAX_RETRY = 3              # 请求失败重试次数
RETRY_BACKOFF = (3, 6)     # 重试等待间隔（秒），按序递增

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

DEFAULT_CONFIG = {
    "_说明": "iActivityId / iFlowId / g_tk 每期活动都会变，必须从当期活动页抓包获取后填在这里，否则接口会返回「活动不存在」。",
    "source": {
        "type": "ams",
        "request_url": "https://smoba.ams.game.qq.com/ams/ame/amesvr",
        "query": {"ameVersion": "0.3", "sServiceType": "yxzj", "iActivityId": "", "iFlowId": "", "g_tk": ""},
        "body": {"iActivityId": "", "iFlowId": "", "g_tk": ""},
        "id_field": "sOutValue2",
        "vote_field": "sOutValue3",
    },
    "poll_interval_seconds": 600,
    "hero_map": {"_格式": '{"v_1": {"hero": "英雄名", "skin": "皮肤名"}}'},
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        cfg = {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    merged.update(cfg)
    merged["source"] = {**DEFAULT_CONFIG["source"], **(cfg.get("source") or {})}
    return merged


def ts_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def http_post(url, body):
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", "https://pvp.qq.com/")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _split_pipe(s):
    return [x for x in str(s).split("|") if x != ""]


def parse_ams(text, id_field="sOutValue2", vote_field="sOutValue3", hero_map=None):
    obj = json.loads(text)
    ret = str(obj.get("ret", ""))
    msg = obj.get("flowRet", {}).get("sMsg") or obj.get("msg") or ""
    modret = obj.get("modRet") or {}
    ids = modret.get(id_field)
    votes = modret.get(vote_field)

    if not ids or not votes:
        cand_ids, cand_votes = [], []
        for v in modret.values():
            if not isinstance(v, str) or "|" not in v:
                continue
            parts = _split_pipe(v)
            if parts and all(p.startswith("v_") or p.isdigit() for p in parts) and any(
                    p.startswith("v_") for p in parts):
                cand_ids.append(v)
            elif parts and all(p.replace(".", "").isdigit() for p in parts):
                cand_votes.append(v)
        if not ids and cand_ids:
            ids = cand_ids[0]
        if not votes and cand_votes:
            votes = cand_votes[0]

    if not ids or not votes:
        return None, "接口有响应但没解析到票数字段（ret=%s %s），原始响应：%s" % (ret, msg, text[:300])
    if ret not in ("0", ""):
        return None, "接口返回异常（ret=%s %s）：%s" % (ret, msg, text[:300])

    id_list, vote_list = _split_pipe(ids), _split_pipe(votes)
    if not id_list:
        return None, "接口返回的编号列表为空（ret=%s %s）：%s" % (ret, msg, text[:300])

    items = []
    for i, k in enumerate(id_list):
        m = (hero_map or {}).get(k) or {}
        items.append({
            "k": k,
            "hero": m.get("hero") or k,
            "skin": m.get("skin") or "",
            "votes": int(vote_list[i]) if i < len(vote_list) and vote_list[i].isdigit() else 0,
        })
    items.sort(key=lambda x: -x["votes"])
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return items, "解析成功"


def do_fetch(cfg):
    src = cfg.get("source") or {}
    url = src.get("request_url") or ""
    query = {k: v for k, v in (src.get("query") or {}).items() if str(v) != ""}
    body = {k: v for k, v in (src.get("body") or {}).items() if str(v) != ""}

    if not query.get("iActivityId"):
        return None, "还没填 iActivityId：请从当期活动页抓包拿到 iActivityId / iFlowId / g_tk 后填入数据源配置.json（抓包方法见 README）。"

    full = url + ("?" + urllib.parse.urlencode(query) if query else "")
    hero_map = {k: v for k, v in (cfg.get("hero_map") or {}).items() if not k.startswith("_")}
    last_err = ""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            text = http_post(full, body)
        except Exception as e:
            last_err = "请求失败（第 %d/%d 次）：%s %s" % (attempt, MAX_RETRY, type(e).__name__, e)
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)) - 1])
            continue
        try:
            items, msg = parse_ams(text, src.get("id_field") or "sOutValue2",
                                   src.get("vote_field") or "sOutValue3", hero_map)
            return items, msg
        except Exception as e:
            last_err = "解析失败：%s %s（原始响应 %s）" % (type(e).__name__, e, text[:500])
            break
    return None, last_err


def parse_manual(text, hero_map=None):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None, "没读到内容"
    hero_map = hero_map or {}

    pipe_ids = [ln for ln in lines if "|" in ln and any(p.startswith("v_") for p in _split_pipe(ln))]
    pipe_nums = [ln for ln in lines if "|" in ln and all(
        p.replace(".", "").isdigit() for p in _split_pipe(ln))]
    if pipe_ids and pipe_nums:
        id_list, vote_list = _split_pipe(pipe_ids[0]), _split_pipe(pipe_nums[0])
        items = []
        for i, k in enumerate(id_list):
            m = hero_map.get(k) or {}
            items.append({
                "k": k, "hero": m.get("hero") or k, "skin": m.get("skin") or "",
                "votes": int(vote_list[i]) if i < len(vote_list) and vote_list[i].isdigit() else 0,
            })
        items.sort(key=lambda x: -x["votes"])
        for i, it in enumerate(items, 1):
            it["rank"] = i
        return items, "按编号/票数串解析出 %d 条" % len(items)

    items = []
    for ln in lines:
        parts = [p for p in ln.replace(",", " ").replace("\t", " ").split() if p]
        if len(parts) < 2:
            continue
        num = None
        for p in reversed(parts):
            q = p.replace(",", "").replace("票", "")
            if q.isdigit():
                num = int(q)
                break
        if num is None:
            continue
        name_parts = parts[:-1] if parts[-1].replace(",", "").replace("票", "").isdigit() else parts
        if len(name_parts) >= 2:
            hero, skin = name_parts[0], " ".join(name_parts[1:])
        elif len(name_parts) == 1:
            hero, skin = name_parts[0], ""
        else:
            continue
        items.append({"k": "", "hero": hero, "skin": skin, "votes": num})
    if not items:
        return None, "没识别出「名称 + 票数」的行，检查一下格式"
    items.sort(key=lambda x: -x["votes"])
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return items, "按逐行文本解析出 %d 条" % len(items)


def build_status(ok, msg, data_ts, changed):
    return {
        "last_ok": ok,
        "last_msg": msg,
        "data_ts": data_ts,
        "changed": changed,
        "updated_at": ts_now(),
    }


def commit_snapshot(items, source):
    ts = ts_now()
    latest = read_json(LATEST_PATH, {"ts": None, "items": []})
    old_items = latest.get("items") or []
    same = len(old_items) == len(items) and all(
        (a.get("k") or a.get("hero")) == (b.get("k") or b.get("hero")) and
        int(a.get("votes") or 0) == int(b.get("votes") or 0)
        for a, b in zip(old_items, items))

    if same:
        return False, ts

    latest = {"ts": ts, "source": source, "items": items}
    write_json(LATEST_PATH, latest)

    history = read_json(HISTORY_PATH, [])
    history = [h for h in history if isinstance(h, dict)]
    history.append({"ts": ts, "items": items})
    if len(history) > HISTORY_CAP:
        history = history[-HISTORY_CAP:]
    write_json(HISTORY_PATH, history)
    return True, ts


def update_status(status):
    old = read_json(STATUS_PATH, {})
    if old != status:
        write_json(STATUS_PATH, status)


def main():
    cfg = load_config()
    manual_text = None
    if "--import" in sys.argv:
        i = sys.argv.index("--import")
        manual_text = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""

    data_ts = (read_json(LATEST_PATH, {}) or {}).get("ts")

    if manual_text is not None:
        hero_map = {k: v for k, v in (cfg.get("hero_map") or {}).items() if not k.startswith("_")}
        items, msg = parse_manual(manual_text, hero_map)
        if not items:
            update_status(build_status(False, msg, data_ts, False))
            print("[import] 失败：%s" % msg)
            return 0
        changed, ts = commit_snapshot(items, "手动导入")
        update_status(build_status(True, msg + "（手动导入）", ts, True))
        print("[import] %s，已存为快照 %s" % (msg, ts))
        return 0

    items, msg = do_fetch(cfg)
    if not items:
        update_status(build_status(False, msg, data_ts, False))
        print("[fetch] 未拿到数据：%s" % msg)
        return 0

    changed, ts = commit_snapshot(items, "AMS自动抓取")
    if changed:
        update_status(build_status(True, "抓取成功，票数已更新", ts, True))
        print("[fetch] 抓取成功，%d 项，已更新为快照 %s" % (len(items), ts))
    else:
        update_status(build_status(True, "票数无变化（最新数据 %s）" % data_ts, data_ts, False))
        print("[fetch] 抓取成功，%d 项，但与上次相比票数无变化，未重写数据文件" % len(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())