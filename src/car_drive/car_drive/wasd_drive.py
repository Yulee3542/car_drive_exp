"""WASD 주행 노드 — 실차 아두이노를 키보드로 직접 몬다 (Unity·모니터 없음).

  ros2 run car_drive wasd_drive                       # 포트 자동
  ros2 run car_drive wasd_drive --ros-args -p port:=/dev/ttyACM0 -p max_speed:=120

발행 (나중에 세션 로그가 그대로 받아 쓰게):
  /car/pwm            Int16   지금 보내는 속도 명령(-255..255, 음수=후진)
  /car/steer_cmd      String  보낸 조향 명령 L/R/F
  /car/steering_pot   Int32   조향 POT ADC (펌웨어 P 줄, 약 20 Hz)
  /car/state          Int8    펌웨어 상태 0 정지 / 1 전진 / 2 후진
"""
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


class _PublishingLink:
    """Drive가 보내는 명령을 시리얼로 쓰면서 토픽으로도 남긴다."""

    def __init__(self, link, node):
        self._link, self._node = link, node

    def write(self, text):
        self._link.write(text)
        cmd = text.strip()
        if cmd.startswith("V"):
            self._node.pub_pwm.publish(Int16(data=int(cmd[1:])))
        elif cmd in ("L", "R", "F"):
            self._node.pub_steer.publish(String(data=cmd))


class WasdDrive(Node):
    def __init__(self):
        super().__init__("wasd_drive")
        self.declare_parameter("port", "")
        self.declare_parameter("baud", BAUD)
        self.declare_parameter("step", 20)
        self.declare_parameter("max_speed", 150)
        self.declare_parameter("keepalive_hz", 10.0)

        port = self.get_parameter("port").value or find_port()
        if not port:
            raise RuntimeError("아두이노 포트를 못 찾음 — USB 연결 확인, 또는 -p port:=/dev/ttyACM0")
        self.get_logger().info(f"{port} 연결 중 (보드 리셋 2초 대기)")
        self.serial = SerialLink(port, self.get_parameter("baud").value)

        self.pub_pwm = self.create_publisher(Int16, "/car/pwm", 10)
        self.pub_steer = self.create_publisher(String, "/car/steer_cmd", 10)
        self.pub_pot = self.create_publisher(Int32, "/car/steering_pot", 10)
        self.pub_state = self.create_publisher(Int8, "/car/state", 10)

        self.drive = Drive(_PublishingLink(self.serial, self),
                           step=self.get_parameter("step").value,
                           max_speed=self.get_parameter("max_speed").value)
        self.drive.start()
        self.create_timer(1.0 / float(self.get_parameter("keepalive_hz").value), self.drive.keepalive)

        self._reading = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.get_logger().info(f"연결됨 · 주행 허용 보냄 · 최고 속도 {self.drive.max_speed}")

    def _read_loop(self):
        while self._reading:
            try:
                parsed = parse_line(self.serial.readline())
            except Exception as e:   # USB 뽑힘 등
                if self._reading:
                    self.get_logger().error(f"시리얼 읽기 실패: {e}")
                return
            if parsed is None:
                continue
            kind, value = parsed
            if kind == "pot":
                self.pub_pot.publish(Int32(data=value))
            else:
                self.pub_state.publish(Int8(data=value))

    def shutdown(self):
        self._reading = False
        try:
            self.drive.stop()
        finally:
            self.serial.close()


def _keyboard_loop(node):
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
            if not k or not node.drive.key(k):
                break
            if node.drive.speed != last:
                last = node.drive.speed
                print(f"\r속도 {last:+4d}   ", end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()


def main(args=None):
    if not sys.stdin.isatty():
        print("키보드 입력이 필요함 — 터미널에서 직접 실행 (./run.sh)", file=sys.stderr)
        return 1
    rclpy.init(args=args)
    node, executor, spin, code = None, None, None, 0
    try:
        node = WasdDrive()
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        spin = threading.Thread(target=_spin, args=(executor,), daemon=True)
        spin.start()
        _keyboard_loop(node)
    except KeyboardInterrupt:
        pass
    except Exception as e:      # 포트 없음 · 권한(dialout) · 장치 사용 중
        print(f"[wasd_drive] {e}", file=sys.stderr)
        code = 1
    finally:
        # 순서: spin(keepalive 타이머) 정지 → 정지 명령 → 시리얼 닫기 → 노드 정리.
        # spin이 도는 채로 노드를 없애면 C++ 쪽이 abort하고, 정지 뒤에 keepalive가 끼어들 수 있다.
        if executor is not None:
            executor.shutdown(timeout_sec=1.0)
        if spin is not None:
            spin.join(timeout=2.0)
        if node is not None:
            node.shutdown()     # V0 · F · S — 차를 세우고 주행 게이트를 닫는다
            print("정지 명령 보냄 · 종료")
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
