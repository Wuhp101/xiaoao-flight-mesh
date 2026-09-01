FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY xiaoao_mesh ./xiaoao_mesh
EXPOSE 8789
CMD ["python", "-m", "xiaoao_mesh.server"]
