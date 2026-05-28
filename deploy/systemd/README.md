# Stockpia systemd units

## Installation (EC2 1회)

```bash
sudo cp /home/ubuntu/stockpia/deploy/systemd/stockpia-briefing@.service       /etc/systemd/system/
sudo cp /home/ubuntu/stockpia/deploy/systemd/stockpia-briefing@morning.timer  /etc/systemd/system/
sudo cp /home/ubuntu/stockpia/deploy/systemd/stockpia-briefing@evening.timer  /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now stockpia-briefing@morning.timer
sudo systemctl enable --now stockpia-briefing@evening.timer
```

## 동작 확인

```bash
# 타이머 다음 발사 시각
systemctl list-timers stockpia-briefing@*.timer

# 즉시 1회 발사 (테스트)
sudo systemctl start stockpia-briefing@morning.service

# 로그
sudo journalctl -u stockpia-briefing@morning.service -n 50 --no-pager
```

## 비활성

```bash
sudo systemctl disable --now stockpia-briefing@morning.timer
sudo systemctl disable --now stockpia-briefing@evening.timer
```

## 주의

- 시스템 timezone 이 `Asia/Seoul` 이어야 OnCalendar 가 KST 로 동작 (EC2 는 이미 KST).
- `Persistent=true` 라 EC2 가 꺼져 있던 동안 놓친 schedule 은 부팅 시 즉시 1회 catch-up 발사.
  반복 catch-up 은 없음.
- `EnvironmentFile=/home/ubuntu/stockpia/.env` 에 `TELEGRAM_BOT_TOKEN` / `OPENAI_API_KEY` 필요.
