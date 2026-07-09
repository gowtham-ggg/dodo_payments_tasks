FROM python:3.12-slim

WORKDIR /app

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/false --create-home appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["python", "app.py"]
