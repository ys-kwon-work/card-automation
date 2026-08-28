FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY main.py .

ENV PORT=8080
EXPOSE 8080

# --timeout은 배포 시 gcloud run --timeout(기본 180) 이상으로 맞춰, 느린 한 건
# (삼성카드 페이지네이션 + AI 재시도 등)이 워커 강제 종료로 502가 되지 않게 함.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "180", "main:app"]
