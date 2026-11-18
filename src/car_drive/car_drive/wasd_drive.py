"""WASD 주행 노드 — 실차 아두이노를 키보드로 직접 몬다 (Unity·모니터 없음).

  ros2 run car_drive wasd_drive                       # 포트 자동
  ros2 run car_drive wasd_drive --ros-args -p port:=/dev/ttyACM0 -p max_speed:=120

실행할 때마다 세션 폴더 하나에 기록한다 (session_log.py, 끄려면 -p record:=false):
  <runs_dir>/<YYYYmmdd_HHMMSS>/ drive_log.csv · serial_raw.log · session.json
  runs_dir 기본 = $CAR_DRIVE_RUNS (run.sh가 <레포>/runs로 설정) 또는 ~/car_drive_runs

발행:
  /car/pwm            Int16   지금 보내는 속도 명령(-255..255, 음수=후진)
  /car/steer_cmd      String  보낸 조향 명령 L/R/F
  /car/steering_pot   Int32   조향 POT ADC (펌웨어 P 줄, 약 20 Hz)
  /car/state          Int8    펌웨어 상태 0 정지 / 1 전진 / 2 후진
"""
import os
import select
import sys
import termios
import threading
import tty

import rclpy
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Int8, Int16, Int32, String

from car_drive.drive import KEYS_HELP, Drive
from car_drive.serial_link import BAUD, SerialLink, find_port, parse_line
from car_drive.session_log import SessionLog

BOARD_REPLY_TIMEOUT_S = 3.0   # 펌웨어는 50 ms마다 P 줄을 보낸다 — 3초 동안 한 줄도 없으면 통신 실패
KEY_NAMES = {" ": "space", "\x03": "ctrl+c", "\x04": "ctrl+d", "\x1b": "esc", "\n": "enter"}


class _NullLog:
    dir = None

    def event(self, *_):
        pass

    def raw(self, *_):
        pass

    def flush(self):
        pass

    def close(self, *_):
        return None


class _LinkTap:
    """Drive가 보내는 명령: 시리얼로 먼저 쓰고 → 세션 로그 → 토픽(실패해도 무시)."""

    def __init__(self, link, node):
        self._link, self._node = link, node

    def write(self, text):
        self._link.write(text)          # 차로 가는 명령이 먼저
        cmd = text.strip()
        self._node.log.event("tx", cmd)
        try:
            if cmd.startswith("V"):
                self._node.pub_pwm.publish(Int16(data=int(cmd[1:])))
            elif cmd in ("L", "R", "F"):
                self._node.pub_steer.publish(String(data=cmd))
        except Exception:
            # Ctrl+C면 ROS 컨텍스트가 먼저 닫혀 발행이 실패한다 — 이때도 정지 명령(V0·F·S)은 끝까지 보내야 한다
            pass


class WasdDrive(Node):
    def __init__(self):
        super().__init__("wasd_drive")
        self.declare_parameter("port", "")
        self.declare_parameter("baud", BAUD)
        self.declare_parameter("step", 20)
        self.declare_parameter("max_speed", 150)
        self.declare_parameter("keepalive_hz", 10.0)
        self.declare_parameter("record", True)
        self.declare_parameter("runs_dir", os.environ.get("CAR_DRIVE_RUNS", "~/car_drive_runs"))
        p = {n: self.get_parameter(n).value for n in
             ("port", "baud", "step", "max_speed", "keepalive_hz", "record", "runs_dir")}

        port = p["port"] or find_port()
        if not port:
            raise RuntimeError("아두이노 포트를 못 찾음 — USB 연결 확인, 또는 -p port:=/dev/ttyACM0")

        self.log = _NullLog()
        if p["record"]:
            self.log = SessionLog(p["runs_dir"], repo_root=os.environ.get("CAR_DRIVE_ROOT"),
                                  meta={"port": port, "params": p})
            print(f"● 기록 시작: {self.log.dir}", flush=True)

        self.get_logger().info(f"{port} 연결 중 (보드 리셋 2초 대기)")
        try:
            self.serial = SerialLink(port, p["baud"])
        except Exception as e:      # 권한(dialout)·장치 사용 중 — 실패한 실행도 세션에 이유를 남긴다
            meta = self.log.close(f"error: {e}")
            if meta:
                print(f"● 기록 저장(실패한 실행): {self.log.dir}", flush=True)
            raise

        self.pub_pwm = self.create_publisher(Int16, "/car/pwm", 10)
        self.pub_steer = self.create_publisher(String, "/car/steer_cmd", 10)
        self.pub_pot = self.create_publisher(Int32, "/car/steering_pot", 10)
        self.pub_state = self.create_publisher(Int8, "/car/state", 10)

        self._first_rx = threading.Event()
        self._reading = True
        threading.Thread(target=self._read_loop, daemon=True).start()

        self.drive = Drive(_LinkTap(self.serial, self), step=p["step"], max_speed=p["max_speed"])
        self.drive.start()
        self.create_timer(1.0 / float(p["keepalive_hz"]), self.drive.keepalive)
        self.create_timer(1.0, self.log.flush)                  # 비정상 종료에도 1초 전까지는 남게
        self._reply_timer = self.create_timer(BOARD_REPLY_TIMEOUT_S, self._check_board_reply)
        self.get_logger().info(f"포트 열림 · 주행 허용 보냄 · 최고 속도 {self.drive.max_speed} · 보드 응답 확인 중")

    def _check_board_reply(self):
        self._reply_timer.cancel()
        if self._first_rx.is_set():
            self.get_logger().info("보드 응답 확인 (펌웨어 줄 수신 중)")
        else:
            self.log.event("note", "no_board_reply")
            self.get_logger().error(
                f"{BOARD_REPLY_TIMEOUT_S:.0f}초 동안 보드 응답 없음 — 펌웨어(./flash.sh)·baud(9600) 확인. 차는 움직이지 않을 수 있음")

    def _read_loop(self):
        while self._reading:
            try:
                line = self.serial.readline()
            except Exception as e:   # USB 뽑힘 등
                if self._reading:
                    self.log.event("note", f"serial_error: {e}")
                    self.get_logger().error(f"시리얼 읽기 실패: {e}")
                return
            if not line:
                continue
            self._first_rx.set()
            self.log.raw(line)
            parsed = parse_line(line)
            if parsed is None:
                continue
            kind, value = parsed
            self.log.event(kind, value)
            try:
                if kind == "pot":
                    self.pub_pot.publish(Int32(data=value))
                else:
                    self.pub_state.publish(Int8(data=value))
            except Exception:
                pass

    def shutdown(self):
        self._reading = False
        try:
            self.drive.stop()
        finally:
            self.serial.close()


def _keyboard_loop(node):
    """종료 이유를 돌려준다: q · ctrl+c · eof."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    print(KEYS_HELP, flush=True)
    last = None
    try:
        tty.setcbreak(fd)
        while rclpy.ok():
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not ready:
                continue
            k = sys.stdin.read(1)
            if not k:
                return "eof"
            node.log.event("key", KEY_NAMES.get(k, k))
            if not node.drive.key(k):
                return KEY_NAMES.get(k, k)
            if node.drive.speed != last:
                last = node.drive.speed
                print(f"\r속도 {last:+4d}   ", end="", flush=True)
        return "ctrl+c"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()


def _print_summary(meta):
    if not meta:
        return
    s = meta.get("summary", {})
    pot = f"{s.get('pot_min')}~{s.get('pot_max')}" if s.get("pot_samples") else "없음"
    reply = f"{s['first_board_reply_s']:.1f}초에 첫 응답" if s.get("first_board_reply_s") is not None else "응답 없음 ⚠"
    print(f"● 기록 저장: {meta['_dir']}\n"
          f"  주행 {s.get('duration_s', 0):.0f}초 · 키 {s.get('keys')}번 · 최고 |PWM| {s.get('max_abs_pwm')} · "
          f"조향 POT {pot} ({s.get('pot_samples')}개) · 보드 {reply} · 종료 {meta.get('end_reason')}\n"
          f"  파일: drive_log.csv · serial_raw.log · session.json", flush=True)


def main(args=None):
    if not sys.stdin.isatty():
        print("키보드 입력이 필요함 — 터미널에서 직접 실행 (./run.sh)", file=sys.stderr)
        return 1
    rclpy.init(args=args)
    node, executor, spin, code, reason = None, None, None, 0, "ctrl+c"
    try:
        node = WasdDrive()
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        spin = threading.Thread(target=_spin, args=(executor,), daemon=True)
        spin.start()
        reason = _keyboard_loop(node)
    except KeyboardInterrupt:
        reason = "ctrl+c"
    except Exception as e:      # 포트 없음 · 권한(dialout) · 장치 사용 중
        print(f"[wasd_drive] {e}", file=sys.stderr)
        code, reason = 1, f"error: {e}"
    finally:
        # 순서: spin(keepalive 타이머) 정지 → 정지 명령 → 시리얼 닫기 → 기록 닫기 → 노드 정리.
        # spin이 도는 채로 노드를 없애면 C++ 쪽이 abort하고, 정지 뒤에 keepalive가 끼어들 수 있다.
        if executor is not None:
            executor.shutdown(timeout_sec=1.0)
        if spin is not None:
            spin.join(timeout=2.0)
        if node is not None:
            if hasattr(node, "serial"):
                node.shutdown()     # V0 · F · S — 차를 세우고 주행 게이트를 닫는다
                print("정지 명령 보냄 · 종료")
            meta = node.log.close(reason)
            if meta:
                _print_summary(dict(meta, _dir=node.log.dir))
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return code


def _spin(executor):
    try:
        executor.spin()
    except (ExternalShutdownException, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    sys.exit(main())
