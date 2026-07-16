FROM python:3.11-slim-bookworm

# Install system dependencies for PPP and GPIO
RUN apt-get update && apt-get install -y \
    ppp \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install python dependencies
COPY requirements.txt requirements-modem.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-modem.txt

COPY . .

# Expose the Flask port
EXPOSE 5050