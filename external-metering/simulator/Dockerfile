FROM registry.access.redhat.com/ubi9/python-312-minimal:latest

WORKDIR /opt/app-root/src

COPY simulator.py ./simulator.py

ENV PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

USER 1001

CMD ["python3", "simulator.py"]
