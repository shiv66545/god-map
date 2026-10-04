FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m atlas && chown -R atlas /app
USER atlas
EXPOSE 8000
# 1 process + threads keeps the in-memory cache and rate limiter coherent
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:8000", "--workers", "1", "--threads", "16", "--timeout", "30", "--access-logfile", "-"]
