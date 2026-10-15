# The provisioner: Python (stdlib only) plus psql, for writing the machine inventory into the monitoring database, and
# tzdata for the digest times (DIGEST_TZ).
FROM python:3-alpine
RUN apk add --no-cache postgresql17-client tzdata
CMD ["python3", "-u", "/provisioner/provision.py"]
