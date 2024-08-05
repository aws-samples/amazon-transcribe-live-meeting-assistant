
FROM public.ecr.aws/docker/library/python:3.12
USER root
WORKDIR /srv
COPY . /srv

RUN apt update
RUN pip3 install --upgrade pip
RUN pip3 install -r requirements.txt
RUN playwright install --with-deps chromium
RUN apt install portaudio19-dev -y
RUN apt install pulseaudio -y

RUN chmod +x /srv/entrypoint.sh
ENTRYPOINT ["/srv/entrypoint.sh"]
