
FROM python:3.10 as requirements-stage

WORKDIR /tmp

RUN pip install poetry

COPY ./pyproject.toml ./poetry.lock* /tmp/


RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

FROM python:3.10

# Install dependencies
RUN apt-get update
RUN apt-get install -y nginx supervisor

WORKDIR /code

# Supervisor configurations
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Nginx configurations
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/default /etc/nginx/sites-enabled/default

COPY --from=requirements-stage /tmp/requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY . /code/

# Heroku uses PORT, Azure App Services uses WEBSITES_PORT, Fly.io uses 8080 by default
CMD ["/usr/bin/supervisord"]
