FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask flask-cors

COPY server.py .

EXPOSE 8085

CMD ["python", "server.py"]
