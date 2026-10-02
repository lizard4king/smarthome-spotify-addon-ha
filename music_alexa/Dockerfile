FROM python:3.12.15-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt
COPY companion.py security.py /app/
COPY run.sh /app/run.sh
RUN chmod 755 /app/run.sh
EXPOSE 5000 5001
CMD ["/app/run.sh"]
