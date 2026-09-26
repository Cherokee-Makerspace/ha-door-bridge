FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY door_bridge ./door_bridge
# Home Assistant mounts the add-on's persistent /data (options.json + PIN key).
CMD ["python", "-m", "door_bridge"]
