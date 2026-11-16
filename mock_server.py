#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perfect-loop 本地 mock 靶场。

模拟目标站点的接口形态（token → 题库 → 建树 → round → 榜单 → 属主删除），
让 perfect_loop.py 在无授权环境下先打本地靶场跑通全链路。

  python mock_server.py            # 监听 127.0.0.1:8899
  python perfect_loop.py --auto-tree --creator 测试树   # 默认 base 就是本靶场

仅在本地回环监听；不做任何持久化，重启即空。
"""
import json
import random
import string
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST, PORT = "127.0.0.1", 8899
NAME_MAX = 15
PREFIX = "孙笑川_"

_lock = threading.Lock()
_state = {
    "tokens": set(),       # 已发的 CSRF token
    "used_tokens": set(),  # 已消费（mock 简化：token 可重复用，不严格）
    "quizzes": {},         # quiz_id -> dict
    "next_num": 1,
}

QUESTIONS = []
for i in range(1, 21):
    n_ops = random.randint(3, 5)
    correct = random.randrange(n_ops)
    QUESTIONS.append({
        "id": "q%02d" % i,
        "text": "mock 题 %d：以下哪个选项是正确答案？" % i,
        "options": [{"index": j, "text": "选项 %d" % j, "correct": j == correct}
                    for j in range(n_ops)],
    })


def _new_id(n=8):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _leaderboard(qz):
    players = sorted(qz["players"].values(),
                     key=lambda p: (-p["score"], p["created"]))
    return players


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 安静模式

    # ---------- helpers ----------
    def _json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _csrf_ok(self):
        if self.command == "GET":
            return True
        # 非 GET 校验 X-CSRF-TOKEN；419 模拟 Laravel 场景
        return self.headers.get("X-CSRF-TOKEN") in _state["tokens"]

    def _user_key_ok(self, quiz_id):
        qz = _state["quizzes"].get(quiz_id)
        return qz is not None and self.headers.get("User-Key") == qz["userKey"]

    def _route(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        return parts, parse_qs(u.query)

    # ---------- GET ----------
    def do_GET(self):
        parts, qs = self._route()
        api = parts[:1] == ["tree"] and parts[1:2] == ["api"]
        p = [""] + parts[2:] if api else parts
        if p[:1] != [""]:
            p = [""] + parts  # 兼容无 /tree/api 前缀（不应出现）

        if len(p) >= 2 and p[1] == "token":
            tok = _new_id(40)
            with _lock:
                _state["tokens"].add(tok)
            return self._json(200, {"success": True, "token": tok})

        if len(p) >= 2 and p[1] == "questions":
            return self._json(200, {"success": True, "questions": QUESTIONS})

        if len(p) >= 4 and p[1] == "quizzes" and p[3] == "round":
            qz = _state["quizzes"].get(p[2])
            if not qz:
                return self._json(404, {"success": False, "message": "quiz not found"})
            return self._json(200, {
                "success": True,
                "questions": [dict(q, options=[dict(o) for o in q["options"]])
                              for q in qz["questions"]],
            })

        if len(p) >= 4 and p[1] == "quizzes" and p[3] == "players":
            qz = _state["quizzes"].get(p[2])
            if not qz:
                return self._json(404, {"success": False, "message": "quiz not found"})
            players = _leaderboard(qz)
            return self._json(200, {
                "success": True,
                "total": len(players),
                "holder": qz["holder"],
                "players": [{
                    "id": p2["id"], "name": p2["name"], "score": p2["score"],
                    "ending": p2["ending"], "perfect": p2["perfect"],
                } for p2 in players],
            })
        return self._json(404, {"success": False, "message": "no route"})

    # ---------- POST / DELETE ----------
    def do_POST(self):
        parts, _ = self._route()
        p = parts[2:] if parts[:2] == ["tree", "api"] else parts
        body = self._body()

        if len(p) >= 1 and p[0] == "quizzes" and len(p) == 1:
            if not self._csrf_ok():
                return self._json(419, {"success": False, "message": "CSRF token mismatch"})
            answers = body.get("answers") or []
            src = QUESTIONS[:10]
            if len(answers) < len(src):
                return self._json(422, {"success": False, "message": "answers required"})
            qid = _new_id(7)
            key = _new_id(32)
            with _lock:
                _state["quizzes"][qid] = {
                    "id": qid, "userKey": key,
                    "holder": body.get("userName") or "loop",
                    "questions": src,
                    "players": {}, "created": _state["next_num"],
                }
                _state["next_num"] += 1
            return self._json(200, {"success": True,
                                    "quiz": {"id": qid}, "userKey": key})

        if len(p) >= 3 and p[0] == "quizzes" and p[2] == "players":
            if not self._csrf_ok():
                return self._json(419, {"success": False, "message": "CSRF token mismatch"})
            qz = _state["quizzes"].get(p[1])
            if not qz:
                return self._json(404, {"success": False, "message": "quiz not found"})
            name = body.get("playerName") or ""
            if not name or len(name) > NAME_MAX:
                return self._json(422, {"success": False, "message": "bad name"})
            raw = body.get("answers") or []
            ans_map, ans_pos = {}, []
            for a in raw:                       # 兼容两种协议：
                if isinstance(a, dict):         #   [{quesID, optionIndex}]（建树）
                    ans_map[a.get("quesID")] = a.get("optionIndex")
                else:                           #   [index, ...]（按题序，/players）
                    ans_pos.append(a)
            score = 0
            for i, q in enumerate(qz["questions"]):
                pick = ans_map.get(q["id"],
                                   ans_pos[i] if i < len(ans_pos) else None)
                if pick is not None and pick == next(o["index"] for o in q["options"] if o["correct"]):
                    score += 1
            perfect = score == len(qz["questions"])
            pid = _new_id(10)
            with _lock:
                qz["players"][pid] = {
                    "id": pid, "name": name, "score": score,
                    "ending": "perfect" if perfect else "normal",
                    "perfect": perfect, "created": _state["next_num"],
                }
                _state["next_num"] += 1
            return self._json(200, {"success": True, "player": {
                "id": pid, "name": name, "score": score,
                "ending": qz["players"][pid]["ending"], "perfect": perfect,
            }})
        return self._json(404, {"success": False, "message": "no route"})

    def do_DELETE(self):
        parts, _ = self._route()
        p = parts[2:] if parts[:2] == ["tree", "api"] else parts
        if len(p) >= 4 and p[0] == "quizzes" and p[2] == "players":
            if not self._csrf_ok():
                return self._json(419, {"success": False, "message": "CSRF token mismatch"})
            qz = _state["quizzes"].get(p[1])
            if not qz:
                return self._json(404, {"success": False, "message": "quiz not found"})
            if not self._user_key_ok(p[1]):
                return self._json(403, {"success": False, "message": "owner only"})
            with _lock:
                existed = qz["players"].pop(p[3], None)
            return self._json(200 if existed else 404,
                              {"success": bool(existed)})
        return self._json(404, {"success": False, "message": "no route"})


if __name__ == "__main__":
    print("mock靶场: http://%s:%d/tree/api  (Ctrl+C 退出)" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
