FROM python:3.12-slim
RUN useradd -m bot
WORKDIR /app
COPY freezebot/ freezebot/
COPY config.json .
RUN mkdir /data && chown bot /data
USER bot
VOLUME /data
# Dry-run by default (stdout only). Credentials for future adapters come from env vars, never the image.
ENTRYPOINT ["python", "-m", "freezebot", "--db", "/data/freezebot.sqlite3"]
