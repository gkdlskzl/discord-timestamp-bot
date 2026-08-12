FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# data/ 는 볼륨으로 마운트 (docker-compose 참고)
CMD ["python", "-u", "bot.py"]
