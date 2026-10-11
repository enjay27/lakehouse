# The Polaris log batch (ADR-014, ADR-036): Python's standard library only, so nothing is
# installed. The base is the Python image the gate pins by digest (gates/pins.json). The tag is
# a hash of this file and the script (install/release.py batch_image).
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY polaris_log_batch.py /app/polaris_log_batch.py

# Polaris 1.6.0 writes its log files as uid 10000, gid 10001; the batch moves them and writes
# next to them on the same claim.
USER 10000:10001

ENTRYPOINT ["python3", "/app/polaris_log_batch.py"]
