"""세션별 주행 로그 — 실행 한 번 = 폴더 하나.

  <runs>/<YYYYmmdd_HHMMSS>/
    drive_log.csv    t_s,kind,value   (kind: key · tx · pot · state · note)
    serial_raw.log   보드에서 받은 원문 줄 (t_s<TAB>줄) — 펌웨어가 바뀌어도 다시 해석할 수 있게
    session.json     시작/끝 시각·포트·설정·git 커밋·펌웨어 해시·요약 (시작 때 한 번, 끝날 때 갱신)

t_s = 세션 시작부터의 초(단조 시계). 여러 스레드(키보드·시리얼 읽기·keepalive 타이머)가 같이 쓰므로 잠금.
ROS 없이 동작한다(테스트용).
"""
import csv
import datetime as _dt
import hashlib
import json
import os
import socket
import subprocess
import threading
import time


def _git_commit(root):
    if not root:
        return None
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=2)
        dirty = subprocess.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=2).stdout.strip()
        return out.stdout.strip() + ("+dirty" if dirty else "") if out.returncode == 0 else None
    except Exception:
        return None


def _file_sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None


class SessionLog:
    def __init__(self, runs_dir, meta=None, repo_root=None, now=None):
        start = now or _dt.datetime.now()
        name = start.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(os.path.expanduser(runs_dir), name)
        n = 1
        while os.path.exists(path):          # 같은 초에 두 번 실행
            n += 1
            path = os.path.join(os.path.expanduser(runs_dir), f"{name}_{n}")
        os.makedirs(path)
        self.dir = path
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._csv_f = open(os.path.join(path, "drive_log.csv"), "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_f)
        self._csv.writerow(["t_s", "kind", "value"])
        self._raw_f = open(os.path.join(path, "serial_raw.log"), "w", encoding="utf-8")
        self._closed = False
        # 요약용 집계
        self.counts = {"key": 0, "tx": 0, "pot": 0, "state": 0, "raw": 0}
        self.max_abs_pwm = 0
        self.pot_min = None
        self.pot_max = None
        self.first_rx_s = None
        fw = os.path.join(repo_root, "firmware", "car_controller", "car_controller.ino") if repo_root else None
        self.meta = {
            "session": os.path.basename(path),
            "start": start.isoformat(timespec="seconds"),
            "end": None,
            "host": socket.gethostname(),
            "git_commit": _git_commit(repo_root),
            "firmware_sha256_12": _file_sha(fw) if fw else None,
            **(meta or {}),
        }
        self._write_json()

    def t(self):
        return round(time.monotonic() - self._t0, 3)

    def event(self, kind, value):
        with self._lock:
            if self._closed:
                return
            self._csv.writerow([f"{self.t():.3f}", kind, value])
            if kind in self.counts:
                self.counts[kind] += 1
            if kind == "tx" and str(value).startswith("V"):
                try:
                    self.max_abs_pwm = max(self.max_abs_pwm, abs(int(str(value)[1:])))
                except ValueError:
                    pass
            elif kind == "pot":
                v = int(value)
                self.pot_min = v if self.pot_min is None else min(self.pot_min, v)
                self.pot_max = v if self.pot_max is None else max(self.pot_max, v)

    def raw(self, line):
        with self._lock:
            if self._closed:
                return
            t = self.t()
            if self.first_rx_s is None:
                self.first_rx_s = t
            self.counts["raw"] += 1
            self._raw_f.write(f"{t:.3f}\t{line}\n")

    def summary(self):
        return {
            "duration_s": self.t(),
            "first_board_reply_s": self.first_rx_s,
            "keys": self.counts["key"],
            "commands_sent": self.counts["tx"],
            "board_lines": self.counts["raw"],
            "pot_samples": self.counts["pot"],
            "pot_min": self.pot_min,
            "pot_max": self.pot_max,
            "max_abs_pwm": self.max_abs_pwm,
        }

    def close(self, end_reason="quit"):
        with self._lock:
            if self._closed:
                return self.meta
            self._closed = True
            self.meta["end"] = _dt.datetime.now().isoformat(timespec="seconds")
            self.meta["end_reason"] = end_reason
            self.meta["summary"] = self.summary()
            for f in (self._csv_f, self._raw_f):
                try:
                    f.close()
                except Exception:
                    pass
        self._write_json()
        return self.meta

    def flush(self):
        with self._lock:
            if not self._closed:
                self._csv_f.flush()
                self._raw_f.flush()

    def _write_json(self):
        tmp = os.path.join(self.dir, "session.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(self.dir, "session.json"))
