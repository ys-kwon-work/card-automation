FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY main.py .

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "120", "main:app"]
