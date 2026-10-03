# REG — префикс реестра образов Docker Hub; установщик подставляет зеркало (mirror.gcr.io/),
# если Docker Hub с сервера недоступен.
ARG REG=docker.io/
FROM ${REG}denoland/deno:bin AS deno
FROM ${REG}mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM ${REG}library/python:3.12-slim
# ffmpeg — теги и перекодирование (aac, libmp3lame); статическая сборка вместо пакета Debian:
# тот тянет ~400 зависимостей, и сборка на слабом сервере шла больше 10 минут.
# deno — yt-dlp решает им подпись YouTube. ca-certificates уже есть в python:slim.
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY webapp ./webapp

# без root: файлы в библиотеке принадлежат 1000:1000 (владельца задаёт установщик)
ENV PYTHONUNBUFFERED=1 HUB_DATA=/data MUSIC_DIR=/music HOME=/tmp XDG_CACHE_HOME=/tmp/.cache
USER 1000:1000
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8081/healthz', timeout=3)"
CMD ["python", "main.py"]
