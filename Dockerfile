FROM python:3.13-alpine

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt

COPY . .

ENV \
  TOOLMAN_CACHE_PATH=/cache \
  TOOLMAN_COMMAND_SOCKET=/run/toolman.sock

CMD ["./server.py"]
VOLUME ["/cache"]
