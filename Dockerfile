FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries 10 -r requirements.txt
COPY server.py dashboard.html ./
RUN useradd -m app
USER app
CMD ["python", "server.py", "--http"]
