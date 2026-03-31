FROM python:3.11-slim

# Instala Lua
RUN apt-get update && apt-get install -y lua5.4 && apt-get clean

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

EXPOSE 5000

CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 30
