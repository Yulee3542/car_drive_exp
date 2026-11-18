# car_drive_exp — 실차 WASD 주행 (실험용)

실차 노트북(Ubuntu + ROS 2)에서 **주행 노드 하나만** 띄운다. Unity·실시간 모니터는 기본으로 뜨지 않는다.

## 처음 한 번

```bash
git clone https://github.com/Yulee3542/car_drive_exp.git ~/car_drive_exp
cd ~/car_drive_exp
sudo apt install -y python3-serial python3-colcon-common-extensions
sudo usermod -aG dialout $USER     # 그 뒤 로그아웃·로그인
./flash.sh                         # 실차 메가에 firmware/car_controller 업로드 (메가를 USB로 꽂은 채)
```

## 실행

```bash
cd ~/car_drive_exp && git pull && ./run.sh
```

| 키 | 동작 |
|---|---|
| W | 전진 가속 (+20, 최고 150) |
| S | 감속 → 0 아래로 내려가면 후진 |
| A / D | 좌 / 우 조향 (펄스 120 ms, 누르고 있으면 계속) |
| Space | 정지 (속도 0 + 조향 정지) |
| Q | 종료 (정지 명령 보내고 끝) |

- 주행 허용(G)·정지(S)는 노드가 알아서 보낸다. 시작할 때 속도 0으로 시작한다.
- 노트북이나 노드가 죽으면 펌웨어 워치독이 **0.5초 안에** 차를 세운다(노드가 0.1초마다 속도를 다시 보냄).
- 옵션: `./run.sh -p port:=/dev/ttyACM0 -p max_speed:=120 -p step:=10`
- 처음 시험은 **바퀴를 띄운 채로**.

## 토픽 (나중에 세션 로그가 받아 쓸 것)

| 토픽 | 타입 | 내용 |
|---|---|---|
| `/car/pwm` | Int16 | 보내는 속도 명령 −255..255 (음수 = 후진) |
| `/car/steer_cmd` | String | 보낸 조향 명령 L / R / F |
| `/car/steering_pot` | Int32 | 조향 POT ADC (약 20 Hz) |
| `/car/state` | Int8 | 펌웨어 상태 0 정지 / 1 전진 / 2 후진 |

## 펌웨어

`firmware/car_controller/car_controller.ino` = autodrive_skku `main`(a300036)의 `arduino/car_controller/car_controller.ino` 사본.
9600 bps · `G` 주행 허용 · `S` 정지 · `V<-255..255>` 속도 · `L`/`R` 조향 펄스 · `F` 조향 정지 · 출력 `0/1/2` 상태 · `P <adc>`.
초음파 출력은 없다(필요하면 이 사본에 추가).

## 더미 (기본 꺼짐)

자리만 남겨 둔 것. 주행 노드와 같이 뜨지 않는다.

- `./run.sh --twin` — `twin_stub`: Unity 연결 없이 가짜 `/twin/car/steering_pot`만 낸다
- `./run.sh --monitor` — `monitor_stub`: 2초마다 `/car/*` 한 줄 요약 (웹 모니터 없음)

## 구조

```
run.sh  flash.sh  firmware/car_controller/
src/car_drive/car_drive/
  wasd_drive.py   ROS 2 노드 (키보드 → 시리얼, 토픽 발행)
  drive.py        키 → 펌웨어 명령 (V0 → G 순서, 워치독 keepalive)
  serial_link.py  포트 찾기·연결·줄 해석
  extras/         twin_stub.py · monitor_stub.py (더미)
src/car_drive/test/test_drive.py   python3 -m pytest src/car_drive/test
```
