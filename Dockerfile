FROM python:3.12-slim
WORKDIR /app
COPY app.py build_db.py fetch.py index.html ./
# DATA_DIR is the mounted volume; TZ as a POSIX string (IST) needs no tzdata package.
ENV DATA_DIR=/data HOST=0.0.0.0 REFRESH_AT=06:00 TZ=IST-5:30 PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
