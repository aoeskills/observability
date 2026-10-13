# The provisioner: Python (stdlib only) plus psql, for writing the machine inventory into the monitoring database.
FROM python:3-alpine
RUN apk add --no-cache postgresql17-client
CMD ["python3", "-u", "/provisioner/provision.py"]
