# ThermalDeck

A local Linux dashboard for NVIDIA GPU fans and Gigabyte motherboard cooling.

GPU · CPU 라디에이터 팬 · 케이스 팬 · 펌프를 한 화면에서 확인하고 조절하는 GTK 앱입니다.
Ubuntu 24.04 / Python 3 / GTK 3를 기준으로 개발했습니다.

## 기능

- NVIDIA GPU를 실행 시 자동 검색하고 UUID로 개별 제어합니다. 카드의 모든 팬 채널에 같은 속도를 적용합니다.
- X870E AORUS XTREME AI TOP의 IT8696E / IT87952E 팬 단자를 보드 이름으로 표시합니다.
- 현재 온도, RPM, PWM 비율, 하드웨어 제어/수동 상태를 2초마다 읽습니다.
- 속도 슬라이더, 100% 버튼, 온도별 곡선, 개별 복귀, 전체 복귀를 제공합니다.
- 곡선은 CPU 또는 인식된 GPU의 온도를 선택하고 지점을 직접 편집할 수 있습니다.
- 곡선과 수동 설정은 **앱 실행 세션 동안만** 유지합니다. 시작할 때 설정을 자동 적용하지 않습니다.

## 설치와 실행

시스템 Python 및 GTK가 필요합니다. NVIDIA 팬 제어에는 설치된 NVIDIA 드라이버의 NVML이 필요합니다.

```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 pkexec
git clone https://github.com/JR-Yu6197/thermaldeck.git
cd thermaldeck
sudo ./scripts/install.sh
thermaldeck
```

설치기는 앱을 관리자 소유의 `/opt/thermaldeck`에 복사하고 앱 목록에 바로가기를 추가합니다.
첫 제어 때 Ubuntu 인증창이 뜹니다. 비밀번호를 앱에서 저장하거나 처리하지 않습니다.
업데이트는 앱을 닫고 `sudo ./scripts/install.sh`를 다시 실행합니다.

설치 전에도 소스에서 **읽기 전용 조회 및 GUI 확인**을 할 수 있습니다.

```bash
python3 -m thermaldeck status --json
python3 -m thermaldeck
```

메인보드 팬 제어는 별도 커뮤니티 `it87` 드라이버가 필요합니다.

```bash
sudo apt install dkms build-essential "linux-headers-$(uname -r)" curl mokutil
sudo ./scripts/install-it87.sh
```

드라이버는 검토한 커밋과 파일 SHA256으로 고정합니다. 설치 과정에서 커널 드라이버를 빌드하고 로드하며,
팬 자동 보정이나 속도 설정은 하지 않습니다. 실제 제어 검증 및 재부팅 후 로드는 [드라이버 안내](docs/driver.md)를 참고하세요.

## 지원 단자

| 칩 | 채널 | 보드 단자 |
| --- | --- | --- |
| IT8696E | 1 / 2 / 3 / 4 / 5 | CPU_FAN / SYS_FAN1 / SYS_FAN2 / SYS_FAN3 / CPU_OPT |
| IT87952E | 1 / 2 / 3 / 4 / 5 | SYS_FAN5_PUMP / SYS_FAN6_PUMP / SYS_FAN4 / SYS_FAN7_PUMP / SYS_FAN8_PUMP |

표시 이름은 **메인보드 단자 이름**입니다. 실제 펌프가 꽂힌 단자를 소프트웨어만으로 확정할 수는 없습니다.
RPM 0은 미연결·정지·회전 신호 없음 중 하나일 수 있습니다. 분배기에 연결된 팬은 보통 대표 RPM 하나만 보입니다.
GPU의 RPM과 %는 NVML이 보고하는 값이며 물리적인 팬 정지/고장 여부를 독립적으로 보증하지 않습니다.
여러 GPU 팬 채널 중 가장 높은 RPM과 %를 카드 요약에 표시합니다.
CPU_OPT와 PUMP 단자는 보수적으로 최소 70%, 다른 팬과 GPU는 최소 30%를 적용하며 드라이버 한도를 함께 지킵니다.
이는 모든 펌프의 작동을 보장하는 수치가 아니므로 실제 펌프 단자와 제조사 권장 설정을 확인하세요.
전압/DC/PWM 방식, 클럭, 전력 제한, RGB, AIO LCD는 변경하지 않습니다.

## 제어와 복구 동작

- `적용`을 누르기 전에는 슬라이더가 하드웨어에 영향을 주지 않습니다.
- CPU 90°C 또는 GPU 85°C 이상이면 앱이 소유한 관련 채널을 허용 최대 속도로 올립니다.
  온도 하락 시 속도 감소 간격은 최소 10초입니다. 임계값은 앱의 보수적 보호 정책이며 칩의 공식 최대 온도와 다릅니다.
- 온도 읽기 실패·설정 실패 시 해당 채널의 복귀를 시도합니다.
- 앱 종료, 연결 끊김 또는 12초간 통신 없음 시 **이 세션에서 건드린 채널만** 복귀를 시도합니다.
- GPU는 NVIDIA 기본 자동 제어로, 메인보드는 최초 제어 직전의 PWM/모드로 돌아갑니다.
- 관리 프로세스가 강제 종료되거나 커널/드라이버가 응답하지 않으면 복구를 보장할 수 없습니다.
  BIOS의 정상 냉각 설정을 기본으로 유지하세요. 동시에 다른 팬 제어 앱을 실행하지 마세요.
- 설정은 재실행 시 자동 복원하지 않습니다. 백그라운드 부팅 서비스도 설치하지 않습니다.

## 검증과 개발

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q thermaldeck
bash -n scripts/install.sh scripts/install-it87.sh
```

테스트는 모의 NVML / 임시 sysfs를 사용하여 검증 실패, 부분 쓰기 실패, 복귀, 장치 식별,
곡선 보간, 온도 보호를 확인합니다. 단위 테스트가 실제 팬 회전 검증을 대신하지는 않습니다.
실제 PC 검증 범위는 [검증 기록](docs/validation.md)에 별도로 기록합니다.

## 출처 및 라이선스

- [NVIDIA NVML](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
- [Linux hwmon ABI](https://docs.kernel.org/hwmon/sysfs-interface.html)
- [frankcrawford/it87](https://github.com/frankcrawford/it87), [보드별 매핑](https://github.com/frankcrawford/it87/blob/bc06d3488439e5fcd725c1bdcfcac994d6d95cac/Sensors%20configs/Gigabyte/configs/gigabyte-it87-amd.conf)

ThermalDeck 앱은 MIT 라이선스입니다. 별도로 다운로드되는 `it87`은 업스트림 GPL 라이선스를 따릅니다.
GIGABYTE 및 NVIDIA의 공식 제품이 아닙니다.
