# Use the official Python slim image for a smaller footprint
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies required for psycopg2 and other packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend application code
COPY . .

# Expose port 10000 (Render default)
EXPOSE 10000

# Script to run migrations and start gunicorn
# We use a shell command to ensure migrations run before the app starts
CMD flask db upgrade && gunicorn run:app --bind 0.0.0.0:10000 --workers 2 --threads 4 --timeout 120
