#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
王者荣耀 周年庆限定皮肤返场投票 · 实时票数监控台（后端）

零依赖：只用 Python 3 标准库。

启动：
    python3 server.py
然后浏览器打开：
    http://127.0.0.1:8787

职责：
  1. 按配置定时向「腾讯 AMS 活动系统」接口抓取票数
  2. 每次抓取结果落盘为一条快照（SQLite），供趋势图与回溯使用
  3. 对外提供 REST 接口给前端看板
  4. 提供「手动导入」兜底通道：抓不到时可粘贴文本，自动解析成排名
"""

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "votes.db")
CONFIG_PATH = os.path.join(BASE_DIR, "数据源配置.json")
HTML_NAME = "投票监控台.html"
PORT = int(os.environ.get("VOTE_PORT", "8787"))

DEFAULT_CONFIG = {
    "_说明": "iActivityId / iFlowId / g_tk 每期活动都会变，必须从当期活动页抓包获取后填在这里，否则接口会返回「活动不存在」。",
    "source": {
        "type": "ams",
        "request_url": "https://smoba.ams.game.qq.com/ams/ame/amesvr",
        "query": {
            "ameVersion": "0.3",
            "sServiceType": "yxzj",
            "iActivityId": "",
            "iFlowId": "",
            "g_tk": ""
        },
        "body": {
            "iActivityId": "",
            "iFlowId": "",
            "g_tk": ""
        },
        "id_field": "sOutValue2",
        "vote_field": "sOutValue3"
    },
    "poll_interval_seconds": 300,
    "hero_map": {
        "_格式": "{\"v_1\": {\"hero\": \"英雄名\", \"skin\": \"皮肤名\"}}"
    }
}

_lock = threading.Lock()
STATUS = {
    "last_run": None,
    "last_ok": None,
    "last_msg": "尚未抓取",
    "last_raw": "",
    "runs": 0,
    "ok_runs": 0,
    "scheduler": "off",
    "next_run_ts": None
}


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    merged = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    merged.update(cfg or {})
    merged["source"] = {**DEFAULT_CONFIG["source"], **(cfg.get("source") or {})}
    return merged


def save_config(cfg):
    safe = json.loads(json.dumps(cfg, ensure_ascii=False))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)
    return safe


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshot(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS vote(
                snapshot_id INTEGER NOT NULL,
                k TEXT,
                hero TEXT,
                skin TEXT,
                votes INTEGER,
                rank INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_vote_snap ON vote(snapshot_id);
            """
        )


def save_snapshot(items, source, note=""):
    """items: [{k, hero, skin, votes}]，按票数降序存一条快照"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = sorted(items, key=lambda x: -int(x.get("votes") or 0))
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO snapshot(ts, source, note) VALUES(?,?,?)", (ts, source, note)
        )
        sid = cur.lastrowid
        for i, it in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO vote(snapshot_id,k,hero,skin,votes,rank) VALUES(?,?,?,?,?,?)",
                (sid, str(it.get("k") or ""), it.get("hero") or "", it.get("skin") or "",
                 int(it.get("votes") or 0), i),
            )
        conn.commit()
    return sid, ts, rows


def latest_snapshot():
    with db() as conn:
        snap = conn.execute("SELECT * FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
        if not snap:
            return None
        rows = conn.execute(
            "SELECT k,hero,skin,votes,rank FROM vote WHERE snapshot_id=? ORDER BY rank",
            (snap["id"],),
        ).fetchall()
    return {"ts": snap["ts"], "source": snap["source"], "note": snap["note"],
            "items": [dict(r) for r in rows]}


def history(limit=300):
    with db() as conn:
        snaps = conn.execute(
            "SELECT id,ts FROM snapshot ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for s in reversed(snaps):
            rows = conn.execute(
                "SELECT k,hero,skin,votes,rank FROM vote WHERE snapshot_id=? ORDER BY rank",
                (s["id"],),
            ).fetchall()
            out.append({"ts": s["ts"], "items": [dict(r) for r in rows]})
    return out


# --------------------------------------------------------------------------
# 抓取：腾讯 AMS 活动系统
# --------------------------------------------------------------------------
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")


def http_post(url, body, timeout=15):
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", "https://pvp.qq.com/")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _split_pipe(s):
    return [x for x in str(s).split("|") if x != ""]


def parse_ams(text, id_field="sOutValue2", vote_field="sOutValue3"):
    """
    解析 AMS 返回。
    正常形如：{"ret":"0","modRet":{"sOutValue2":"v_1|v_2|...","sOutValue3":"123|456|..."}}
    sOutValue2 与 sOutValue3 是「编号串 / 票数串」，用竖线分隔、按下标一一对应。
    """
    obj = json.loads(text)
    ret = str(obj.get("ret", ""))
    msg = obj.get("flowRet", {}).get("sMsg") or obj.get("msg") or ""

    modret = obj.get("modRet") or {}
    ids = modret.get(id_field) if id_field in modret else None
    votes = modret.get(vote_field) if vote_field in modret else None

    # 容错：字段名变了就扫一遍，挑出「含 v_ 的竖线串」和「纯数字的竖线串」
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
        return None, f"接口有响应但没解析到票数字段（ret={ret} {msg}），原始响应：{text[:300]}"

    id_list, vote_list = _split_pipe(ids), _split_pipe(votes)
    if ret not in ("0", "") or not id_list:
        return None, f"接口返回异常（ret={ret} {msg}）：{text[:300]}"

    cfg = load_config()
    hero_map = {k: v for k, v in (cfg.get("hero_map") or {}).items() if not k.startswith("_")}

    items = []
    for i, k in enumerate(id_list):
        m = hero_map.get(k) or {}
        items.append({
            "k": k,
            "hero": m.get("hero") or k,
            "skin": m.get("skin") or "",
            "votes": int(vote_list[i]) if i < len(vote_list) and vote_list[i].isdigit() else 0,
        })
    return items, "解析成功"


def do_fetch(manual_cfg=None):
    """执行一次抓取，返回 (items, message, raw)"""
    cfg = manual_cfg or load_config()
    src = cfg.get("source") or {}
    url = src.get("request_url") or ""
    query = {k: v for k, v in (src.get("query") or {}).items() if str(v) != ""}
    body = {k: v for k, v in (src.get("body") or {}).items() if str(v) != ""}

    if not query.get("iActivityId"):
        return None, "还没填 iActivityId：请从当期活动页抓包拿到参数后填入数据源配置。", ""

    full = url + ("?" + urllib.parse.urlencode(query) if query else "")
    try:
        text = http_post(full, body)
    except Exception as e:
        return None, f"请求失败：{type(e).__name__} {e}", ""

    try:
        items, msg = parse_ams(text, src.get("id_field") or "sOutValue2",
                               src.get("vote_field") or "sOutValue3")
    except Exception as e:
        return None, f"解析失败：{type(e).__name__} {e}", text[:500]
    return items, msg, text[:500]


# --------------------------------------------------------------------------
# 手动导入：把粘贴的文本解析成排名
# --------------------------------------------------------------------------
def parse_manual(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None, "没读到内容"

    cfg = load_config()
    hero_map = {k: v for k, v in (cfg.get("hero_map") or {}).items() if not k.startswith("_")}
    items = []

    # 形态一：两行「编号串 / 票数串」
    pipe_ids = [ln for ln in lines if "|" in ln and any(p.startswith("v_") for p in _split_pipe(ln))]
    pipe_nums = [ln for ln in lines if "|" in ln and all(
        p.replace(".", "").isdigit() for p in _split_pipe(ln))]
    if pipe_ids and pipe_nums:
        id_list, vote_list = _split_pipe(pipe_ids[0]), _split_pipe(pipe_nums[0])
        for i, k in enumerate(id_list):
            m = hero_map.get(k) or {}
            items.append({
                "k": k, "hero": m.get("hero") or k, "skin": m.get("skin") or "",
                "votes": int(vote_list[i]) if i < len(vote_list) and vote_list[i].isdigit() else 0,
            })
        return items, f"按编号/票数串解析出 {len(items)} 条"

    # 形态二：每行「英雄 皮肤 票数」（分隔符支持空格 / 逗号 / 制表符）
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
    return items, f"按逐行文本解析出 {len(items)} 条"


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------
def run_once(note=""):
    items, msg, raw = do_fetch()
    with _lock:
        STATUS["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATUS["runs"] += 1
        STATUS["last_msg"] = msg
        STATUS["last_raw"] = raw
        STATUS["last_ok"] = bool(items)
        if items:
            STATUS["ok_runs"] += 1
    if items:
        save_snapshot(items, "AMS自动抓取", note)
    return items, msg, raw


def scheduler_loop():
    while True:
        cfg = load_config()
        interval = max(30, int(cfg.get("poll_interval_seconds") or 300))
        with _lock:
            STATUS["scheduler"] = "on"
            STATUS["next_run_ts"] = time.time() + interval
        run_once()
        time.sleep(interval)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "VoteMonitor/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        body = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path in ("/", "/index.html"):
            fp = os.path.join(BASE_DIR, HTML_NAME)
            if not os.path.exists(fp):
                return self._send(404, "找不到 " + HTML_NAME, "text/plain; charset=utf-8")
            with open(fp, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if path == "/api/state":
            cfg = load_config()
            cfg["source"]["body"] = {**cfg["source"].get("body", {}), **{}}
            return self._send(200, {
                "config": cfg,
                "status": STATUS,
                "latest": latest_snapshot(),
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        if path == "/api/ranking":
            latest = latest_snapshot()
            return self._send(200, {"latest": latest, "status": STATUS})

        if path == "/api/history":
            try:
                limit = int((qs.get("limit") or ["300"])[0])
            except Exception:
                limit = 300
            return self._send(200, {"history": history(limit)})

        if path == "/api/status":
            return self._send(200, STATUS)

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        payload = self._body()

        if path == "/api/config":
            cfg = payload.get("config") or {}
            saved = save_config(cfg)
            return self._send(200, {"ok": True, "config": saved})

        if path == "/api/fetch":
            items, msg, raw = do_fetch()
            if items:
                sid, ts, rows = save_snapshot(items, "AMS手动抓取")
                with _lock:
                    STATUS["last_run"] = ts
                    STATUS["last_ok"] = True
                    STATUS["last_msg"] = msg
                    STATUS["last_raw"] = raw
                    STATUS["runs"] += 1
                    STATUS["ok_runs"] += 1
                return self._send(200, {"ok": True, "message": msg, "raw": raw,
                                        "ts": ts, "items": rows})
            with _lock:
                STATUS["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                STATUS["last_ok"] = False
                STATUS["last_msg"] = msg
                STATUS["last_raw"] = raw
                STATUS["runs"] += 1
            return self._send(200, {"ok": False, "message": msg, "raw": raw})

        if path == "/api/import":
            items, msg = parse_manual(payload.get("text") or "")
            if not items:
                return self._send(200, {"ok": False, "message": msg})
            sid, ts, rows = save_snapshot(items, "手动导入", payload.get("note") or "")
            return self._send(200, {"ok": True, "message": msg, "ts": ts, "items": rows})

        if path == "/api/clear":
            with db() as conn:
                conn.executescript("DELETE FROM vote; DELETE FROM snapshot;")
                conn.commit()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def main():
    init_db()
    cfg = load_config()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 58)
    print(" 王者荣耀返场投票 · 实时票数监控台")
    print(f" 看板地址：  http://127.0.0.1:{PORT}")
    print(f" 数据源：    {cfg['source']['request_url']}")
    print(f" 轮询间隔：  {cfg.get('poll_interval_seconds')} 秒")
    print(" 停止服务：  Ctrl + C")
    print("=" * 58)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    sys.exit(main())
#（注：内容由AI生成）
