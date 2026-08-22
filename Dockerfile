FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY pagecapture ./pagecapture
RUN pip install --no-cache-dir .

EXPOSE 8080
CMD ["uvicorn", "pagecapture.main:app", "--host", "0.0.0.0", "--port", "8080"]
