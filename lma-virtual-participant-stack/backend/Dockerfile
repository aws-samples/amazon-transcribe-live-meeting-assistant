FROM public.ecr.aws/docker/library/python:3.12
USER root
WORKDIR /srv
COPY . /srv

ENV PIP_ROOT_USER_ACTION=ignore
RUN pip3 install --upgrade pip
RUN pip3 install --default-timeout=20 -r requirements.txt
RUN playwright install chromium
RUN apt-get update && apt-get install -y portaudio19-dev pulseaudio

RUN chmod +x /srv/entrypoint.sh
ENTRYPOINT ["/srv/entrypoint.sh"]
