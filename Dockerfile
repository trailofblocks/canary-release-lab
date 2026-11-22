FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /lab
COPY canary_lab/ ./canary_lab/
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "canary_lab.app"]
CMD ["serve"]
