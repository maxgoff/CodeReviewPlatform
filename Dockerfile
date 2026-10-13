FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY reviewbot ./reviewbot
RUN pip install --no-cache-dir .
COPY samples ./samples
ENV PYTHONUNBUFFERED=1 REVIEWBOT_HOST=0.0.0.0 REVIEWBOT_PORT=27183
EXPOSE 27183
CMD ["reviewbot-server"]
