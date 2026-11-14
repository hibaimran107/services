FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir flask gunicorn
COPY app.py .
RUN useradd -m appuser && mkdir /data && chown appuser /data
USER appuser
ENV DATABASE_PATH=/data/expenses.db
EXPOSE 8000
# --preload creates the schema once, before workers fork.
CMD ["sh", "-c", "gunicorn --preload -w 2 --threads 4 -b 0.0.0.0:${PORT:-8000} 'app:create_app()'"]
