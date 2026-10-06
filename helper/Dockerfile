FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home app
WORKDIR /app
COPY autoschool_helper.py ./
COPY webapp ./webapp/
RUN mkdir /data && chown app:app /data
USER app
EXPOSE 8787 8790
HEALTHCHECK --interval=30s CMD curl -fsS http://127.0.0.1:8787/health || exit 1
CMD ["python3", "autoschool_helper.py"]
