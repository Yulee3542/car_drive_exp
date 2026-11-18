#!/usr/bin/env bash
# 실차 노트북(Ubuntu + ROS 2): git pull 후 ./run.sh — 빌드(매번, 몇 초) → WASD 주행 노드 실행.
#   ./run.sh                                   포트 자동, 주행 노드만
#   ./run.sh -p port:=/dev/ttyACM0 -p max_speed:=120
#   ./run.sh --twin / --monitor                [더미] 트윈·모니터 자리 (기본 꺼짐)
ROOT="$(cd "$(dirname "$0")" && pwd)"

ROS_SETUP="$(ls /opt/ros/*/setup.bash 2>/dev/null | sort | tail -1)"
if [ -z "$ROS_SETUP" ]; then echo "ROS 2가 설치돼 있지 않음 (/opt/ros/*/setup.bash 없음)"; exit 1; fi
# shellcheck disable=SC1090
source "$ROS_SETUP"

command -v colcon >/dev/null || { echo "colcon 없음: sudo apt install -y python3-colcon-common-extensions"; exit 1; }
python3 -c "import serial" 2>/dev/null || { echo "pyserial 없음: sudo apt install -y python3-serial"; exit 1; }
if ! id -nG | grep -qw dialout; then
  echo "[경고] $USER 가 dialout 그룹이 아님 → 포트 권한 오류가 나면: sudo usermod -aG dialout $USER 후 재로그인"
fi

TWIN=0; MONITOR=0; ROS_ARGS=()
for a in "$@"; do
  case "$a" in
    --twin) TWIN=1 ;;
    --monitor) MONITOR=1 ;;
    *) ROS_ARGS+=("$a") ;;
  esac
done

cd "$ROOT" || exit 1
mkdir -p "$ROOT/log"
echo "[run] 빌드 중 ($(basename "$(dirname "$ROS_SETUP")"))"
if ! colcon build --symlink-install --packages-select car_drive > "$ROOT/log/last_build.txt" 2>&1; then
  cat "$ROOT/log/last_build.txt"; echo "[run] 빌드 실패"; exit 1
fi
# shellcheck disable=SC1091
source "$ROOT/install/setup.bash"

PIDS=()
if [ "$TWIN" = 1 ]; then ros2 run car_drive twin_stub & PIDS+=($!); fi
if [ "$MONITOR" = 1 ]; then ros2 run car_drive monitor_stub & PIDS+=($!); fi
trap 'for p in "${PIDS[@]}"; do kill -INT "$p" 2>/dev/null; done' EXIT

if [ ${#ROS_ARGS[@]} -gt 0 ]; then
  ros2 run car_drive wasd_drive --ros-args "${ROS_ARGS[@]}"
else
  ros2 run car_drive wasd_drive
fi
