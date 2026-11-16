#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""满分循环器 v2 · 自动建树取 User-Key，满分连发，榜单可查可清。

关键事实（先看清再跑）：
  * userKey 只有一个来源：POST /quizzes 的响应。没有别的接口能"领"key。
    所以"自动获取 userKey" == 自动建一棵自己的树。
  * POST /quizzes/{id}/players（答题上榜）本来就不需要 userKey —— 那是玩家入场券。
    需要 userKey 的是属主动作：删玩家、读逐题对错、推送订阅、预览图。
  * 因此本工具把 key 用在属主动作上（--verify / --cleanup），而不是用它去解锁写榜。

用法：
  python perfect_loop.py --auto-tree --creator 测试树            # 自动建树 + 拿 key + 连发
  python perfect_loop.py --quiz <id> --user-key <key>            # 已有自己的树
  python perfect_loop.py --quiz <id> --verify                    # 查榜（公开，无需 key）
  python perfect_loop.py --quiz <id> --user-key <key> --cleanup   # 清掉前缀玩家
  加 --live 才打 https://c.cloudz.fun/tree/api，默认打本地 mock。
"""
import argparse
import json
import os
import random
import string
import sys
import time

import requests

PREFIX = "孙笑川_"
NAME_MAX = 15
ALNUM = string.ascii_letters + string.digits
SUFFIX_POOL = string.ascii_lowercase + string.digits
CREDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json")

MOCK = "http://127.0.0.1:8899/tree/api"
LIVE = "https://c.cloudz.fun/tree/api"


def make_name(prefix=PREFIX):
    room = max(1, NAME_MAX - len(prefix))
    n = random.randint(4, min(room, 11))
    return prefix + "".join(random.choice(SUFFIX_POOL) for _ in range(n))


def make_sid():
    return "".join(random.choice(ALNUM) for _ in range(24))


class Runner:
    def __init__(self, base, quiz=None, user_key=None, locale="cn", timeout=20):
        self.base = base.rstrip("/")
        self.quiz = quiz
        self.user_key = user_key
        self.locale = locale
        self.timeout = timeout
        self.csrf = None
        self.s = self._new_session()

    @staticmethod
    def _new_session():
        session = requests.Session()
        session.headers.update({
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Origin": "https://c.cloudz.fun",
            "Referer": "https://c.cloudz.fun/tree/cn",
        })
        return session

    def reconnect(self):
        # 与重新启动脚本的初始化一致，不继承旧 cookies 或会话状态。
        self.s.close()
        self.s = self._new_session()
        self.csrf = None
        self.refresh_token()

    # ---------- 令牌 ----------
    def refresh_token(self):
        r = self.s.get(self.base + "/token", timeout=self.timeout)
        b = r.json()
        if r.status_code != 200 or not b.get("success"):
            raise RuntimeError("token failed: %s %s" % (r.status_code, r.text[:120]))
        self.csrf = b["token"]
        return self.csrf

    def _h(self, body=False):
        h = {}
        if self.csrf:
            h["X-CSRF-TOKEN"] = self.csrf
        if self.user_key:
            h["User-Key"] = self.user_key
        if body:
            h["Content-Type"] = "application/json"
        return h

    def _send(self, method, path, **kw):
        r = self.s.request(method, self.base + path, headers=self._h("json" in kw),
                           timeout=self.timeout, **kw)
        if r.status_code == 419 and method != "GET":
            self.refresh_token()
            r = self.s.request(method, self.base + path, headers=self._h("json" in kw),
                               timeout=self.timeout, **kw)
        if r.status_code == 429:
            wait = r.headers.get("Retry-After")
            if wait and wait.isdigit():
                time.sleep(min(int(wait), 120))
                r = self.s.request(method, self.base + path, headers=self._h("json" in kw),
                                   timeout=self.timeout, **kw)
        return r

    # ---------- 自动建树 = 拿到 userKey 的唯一途径 ----------
    def auto_create(self, creator="loop", gender="male", mood="movies"):
        qs = self._send("GET", "/questions", params={"gender": gender, "locale": self.locale}).json()
        src = qs.get("questions", [])[:10]
        if len(src) < 10:
            raise RuntimeError("题库不足 10 题")
        answers = [{"quesID": q["id"], "optionIndex": random.randrange(max(1, len(q.get("options") or [1])))}
                   for q in src]
        r = self._send("POST", "/quizzes", json={
            "userName": creator, "userGender": gender, "mood": mood, "answers": answers,
            "country": "CN", "locale": self.locale, "from": "", "submissionId": make_sid()})
        b = r.json()
        if not b.get("success"):
            raise RuntimeError("create failed: %s %s" % (r.status_code, b))
        self.quiz = b["quiz"]["id"]
        self.user_key = b["userKey"]
        return b

    # ---------- 读整局 ----------
    def fetch_round(self):
        r = self._send("GET", "/quizzes/%s/round" % self.quiz, params={"locale": self.locale})
        b = r.json()
        if not b.get("success", True):
            raise RuntimeError("round failed: %s %s" % (r.status_code, b))
        if b.get("csrf"):
            self.csrf = b["csrf"]
        return b

    @staticmethod
    def perfect_answers(rb):
        out = []
        for q in rb.get("questions", []):
            for o in q.get("options", []):
                if o.get("correct") is True:
                    out.append(o["index"])       # 必须用 index，不是数组下标
                    break
            else:
                raise RuntimeError("no correct option: %s" % q.get("id"))
        return out

    # ---------- 写 ----------
    def submit(self, name, answers):
        r = self._send("POST", "/quizzes/%s/players" % self.quiz,
                       json={"playerName": name, "answers": answers, "submissionId": make_sid()})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:160]}

    # ---------- 属主动作 ----------
    def leaderboard(self, page=1):
        r = self._send("GET", "/quizzes/%s/players" % self.quiz, params={"page": page})
        return r.status_code, r.json()

    def cleanup(self, prefix=PREFIX):
        code, b = self.leaderboard()
        if code != 200:
            return 0, "leaderboard %s" % code
        hit = [p for p in b.get("players", []) if p["name"].startswith(prefix)]
        gone = 0
        for p in hit:
            r = self._send("DELETE", "/quizzes/%s/players/%s" % (self.quiz, p["id"]))
            if r.status_code in (200, 404):
                gone += 1
        return gone, "deleted=%d/%d" % (gone, len(hit))


def load_creds(quiz):
    if not os.path.exists(CREDS):
        return None
    try:
        d = json.load(open(CREDS, encoding="utf-8"))
    except Exception:
        return None
    return d.get(quiz)


def save_creds(quiz, key, base):
    d = {}
    if os.path.exists(CREDS):
        try:
            d = json.load(open(CREDS, encoding="utf-8"))
        except Exception:
            d = {}
    d[quiz] = {"userKey": key, "base": base, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(CREDS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=MOCK)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--quiz")
    ap.add_argument("--user-key")
    ap.add_argument("--auto-tree", action="store_true", help="自动建树并取 userKey")
    ap.add_argument("--creator", default="loop", help="--auto-tree 时的树主名")
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--jitter", type=float, default=0.4)
    ap.add_argument("--burst", type=int, default=10,
                    help="每 N 轮提交后关闭旧连接并创建全新 HTTP 会话（0 = 不重连）")
    ap.add_argument("--rest", type=float, default=30.0,
                    help="兼容旧命令行参数，已忽略；批次之间改为重连")
    ap.add_argument("--locale", default="cn")
    ap.add_argument("--allow-public", action="store_true",
                    help="允许往非自有的树提交（/players 是站点对访客开放的入场接口）")
    ap.add_argument("--verify", action="store_true", help="打印榜单后退出")
    ap.add_argument("--cleanup", action="store_true", help="删除前缀匹配的玩家后退出")
    a = ap.parse_args()

    base = LIVE if a.live else a.base
    c0 = load_creds(a.quiz) if a.quiz else None
    if c0 and not a.live and a.base == MOCK:
        base = c0.get('base') or base          # 复用 creds 里记录的端点
    if len(a.prefix) >= NAME_MAX:
        raise SystemExit("[abort] 前缀过长，服务端 nameMax=%d" % NAME_MAX)

    # userKey 解析顺序：命令行 > creds.json > 自动建树
    quiz, key = a.quiz, a.user_key
    if quiz and not key:
        c = load_creds(quiz)
        if c:
            key = c["userKey"]
            print("[key] 从 creds.json 载入 %s (base=%s)" % (quiz, c.get("base")))

    r = Runner(base, quiz=quiz, user_key=key, locale=a.locale)
    r.refresh_token()

    if not quiz or a.auto_tree:
        if not a.live and not quiz:
            pass
        b = r.auto_create(creator=a.creator)
        save_creds(r.quiz, r.user_key, base)
        print("[tree] quiz  = %s" % r.quiz)
        print("[tree] key   = %s" % r.user_key)
        print("[tree] share = %s" % ("https://c.cloudz.fun/tree/f/" + r.quiz
                                     if a.live else "%s/quizzes/%s" % (base, r.quiz)))
        quiz = r.quiz
    elif a.live and not key and not a.allow_public and not a.verify:
        raise SystemExit(
            "[abort] --live 且没有该树的 userKey。\n"
            "        自有树：--auto-tree 自动建树拿 key，或 --user-key 指定。\n"
            "        公开榜：加 --allow-public（等同访客在页面上答题上榜，不带属主权限）。")

    if a.verify:
        code, b = r.leaderboard()
        print("leaderboard http=%s total=%s holder=%s" % (code, b.get("total"), b.get("holder")))
        for i, p in enumerate(b.get("players", []), 1):
            print("  %2d. %-16s score=%2d ending=%-5s perfect=%s" %
                  (i, p["name"], p["score"], p["ending"], p["perfect"]))
        return

    if a.cleanup:
        if not r.user_key:
            raise SystemExit("[abort] --cleanup 需要该树的 userKey（删玩家是属主动作）")
        n, msg = r.cleanup(a.prefix)
        print("[cleanup] %s" % msg)
        return

    print("target : %s" % base)
    print("quiz   : %s" % quiz)
    print("prefix : %s   interval: %.1fs   count: %s" %
          (a.prefix, a.interval, a.count or "∞"))
    if a.burst > 0:
        print("batching: 每 %d 轮提交后重建全新 HTTP 会话（--burst 0 关闭）" % a.burst)
    else:
        print("batching: 关闭")
    print("board  : %s" % ("https://c.cloudz.fun/tree/cn/visit?id=" + quiz if a.live
                           else "%s/quizzes/%s/players" % (base, quiz)))

    done = perfect = failed = reconnects = 0
    t0 = time.time()
    try:
        while True:
            t = time.time()
            try:
                rb = r.fetch_round()
                answers = r.perfect_answers(rb)
                name = make_name(a.prefix)
                code, res = r.submit(name, answers)
                if res.get("success") and res.get("player", {}).get("perfect") is True:
                    p = res["player"]
                    perfect += 1
                    print("[%5.1fs] %-16s score=%2d %-5s pid=%s" %
                          (t - t0, name, p["score"], p["ending"], p["id"]))
                else:
                    failed += 1
                    print("[%5.1fs] %-16s FAIL http=%s %s" % (t - t0, name, code, str(res)[:120]))
            except Exception as e:
                failed += 1
                print("[%5.1fs] ERROR %s" % (t - t0, e))
                time.sleep(min(30, a.interval * 2))
            done += 1
            if a.count and done >= a.count:
                break

            # 沿用提交轮次计数；最后一轮已退出，不再建立无用连接。
            if a.burst > 0 and done % a.burst == 0:
                print("[%5.1fs] ---- 已发 %d 个，重建全新会话（清空 cookies） ----" %
                      (time.time() - t0, done))
                r.reconnect()
                reconnects += 1

            time.sleep(max(0.2, a.interval + random.uniform(-a.jitter, a.jitter)))
    except KeyboardInterrupt:
        print("\n[stop] Ctrl+C")
    finally:
        r.s.close()
        batches = (done // a.burst) if a.burst > 0 else 0
        print("[summary] runs=%d perfect=%d failed=%d batches=%d reconnects=%d elapsed=%.1fs"
              % (done, perfect, failed, batches, reconnects, time.time() - t0))
        print("[next] 查榜: --quiz %s --verify   清理: --quiz %s --user-key %s --cleanup"
              % (quiz, quiz, r.user_key))


if __name__ == "__main__":
    main()
