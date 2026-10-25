FROM python:3.12-slim
RUN useradd -m bot
WORKDIR /app
COPY freezebot/ freezebot/
COPY config.json .
RUN mkdir /data && chown bot /data
USER bot
VOLUME /data
# Data dir via env. Dry-run by default (stdout only); FREEZEBOT_LIVE=1 posts, FREEZEBOT_FLUX=1 enables
# multi-instance mode (docs/FLUX.md). Credentials come from env vars, never the image.
ENV FREEZEBOT_DATA_DIR=/data PYTHONUNBUFFERED=1
# Healthy while any chain finished a poll in the last 10 minutes (health file written in flux mode or
# when FREEZEBOT_HEALTH_FILE is set; otherwise the check only verifies the process can run).
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 CMD python -c "import os,sys,time;p=os.environ.get('FREEZEBOT_HEALTH_FILE') or os.path.join(os.environ.get('FREEZEBOT_DATA_DIR','/data'),'health');sys.exit(0 if (not os.path.exists(p) and os.environ.get('FREEZEBOT_FLUX','') in ('','0')) or (os.path.exists(p) and time.time()-os.path.getmtime(p)<600) else 1)"
ENTRYPOINT ["python", "-m", "freezebot"]
