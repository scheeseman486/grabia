FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py database.py downloader.py ia_client.py ./
COPY static/ ./static/
COPY templates/ ./templates/

RUN mkdir -p /app/data /app/downloads

ENV GRABIA_HOST=0.0.0.0
ENV GRABIA_PORT=5000
ENV GRABIA_DATA_DIR=/app/data

EXPOSE 5000

CMD ["python", "app.py"]
