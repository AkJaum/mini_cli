FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache \
	git \
	bash \
	make \
	gcc \
	g++ \
	musl-dev \
	wget

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY manage.py ./
COPY runner_service ./runner_service

# Render grants read access to runtime secret files through group 1000.
RUN addgroup -S -g 1000 app && adduser -S app -G app && chown -R app:app /app

ENV PORT=8080
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
USER app
EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn runner_service.wsgi:application --workers 1 --threads 8 --bind 0.0.0.0:${PORT}"]
