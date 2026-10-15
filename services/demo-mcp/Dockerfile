FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home app && mkdir /data && chown 10001 /data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER 10001
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "app.server:factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
