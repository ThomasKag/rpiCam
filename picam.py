#!/usr/bin/env python3
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
from datetime import datetime
from flask import jsonify


import adafruit_dht
import board



# Load font, fallback to default
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
except:
    font = ImageFont.load_default()
from flask import Flask, Response, render_template
import subprocess
import signal
import threading
import time
import requests
import os
import csv

CAPTURE_DIR = "/home/thomas/captures"
CSV_PATH = "/home/thomas/stream/sensorData.csv"
CSV_OLD_PATH = "/home/thomas/stream/sensorData.csv.old"

# Ensure capture directory exists
os.makedirs(CAPTURE_DIR, exist_ok=True)

# Prusa Connect configuration

HTTP_URL = "https://connect.prusa3d.com/c/snapshot"
DELAY_SECONDS = 10
LONG_DELAY_SECONDS = 60
FINGERPRINT = "123456789012345678"
CAMERA_TOKEN = "PIcmHpVfNtr9DLBWNP2V"

# DHT22 configuration

DHT_SENSOR = adafruit_dht.DHT22(board.D2)  # GPIO4 example

# Globals
latest_frame = None
frame_lock = threading.Lock()
clients = 0
sensor_temperature = None
sensor_humidity = None
sensor_lock = threading.Lock()


app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

# Camera thread 
def camera_worker():
    global latest_frame

    cmd = [
        "rpicam-vid",
        "-t", "0",
        "--width", "1920",
        "--height", "1080",
        "--framerate", "15",
        "--codec", "mjpeg",
        "--quality", "80",
        "--flush",
        "-o", "-"
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0
    )

    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"
    buffer = b""

    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break

            buffer += chunk

            while True:
                start = buffer.find(SOI)
                end = buffer.find(EOI)

                if start != -1 and end != -1 and end > start:
                    frame = buffer[start:end + 2]
                    buffer = buffer[end + 2:]

                    with frame_lock:
                        latest_frame = frame
                else:
                    break
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait()

# Sensor worker
def dht_worker():
    global sensor_temperature, sensor_humidity

    while True:
        try:
            temperature = DHT_SENSOR.temperature
            humidity = DHT_SENSOR.humidity

            if temperature is not None and humidity is not None:
                with sensor_lock:
                    sensor_temperature = temperature
                    sensor_humidity = humidity

        except RuntimeError:
            pass  # expected sometimes

        time.sleep(3)  # IMPORTANT: do not go faster

# Sensor status endpoint
@app.route("/status")
def status():
    with sensor_lock:
        temperature = sensor_temperature
        humidity = sensor_humidity

    return jsonify({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": None if temperature is None else round(temperature, 1),
        "humidity": None if humidity is None else round(humidity, 1)
    })

# csv rename during startup
def init_sensor_csv():
    # Remove old backup if it exists
    if os.path.exists(CSV_OLD_PATH):
        os.remove(CSV_OLD_PATH)

    # Rename current CSV to .old if it exists
    if os.path.exists(CSV_PATH):
        os.rename(CSV_PATH, CSV_OLD_PATH)

    # Create new CSV with header
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "temperature", "humidity"])

#csv appender
def store_sensor_reading(temperature, humidity):
    if temperature is None or humidity is None:
        return  # store ONLY valid readings

    timestamp = int(time.time())

    try:
        with open(CSV_PATH, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp,
                f"{temperature:.2f}",
                f"{humidity:.2f}"
            ])
    except Exception:
        pass  # never break the service because of logging

# MJPEG stream endpoint
def mjpeg_stream():
    while True:
        with frame_lock:
            frame = latest_frame

        if frame is None:
            time.sleep(0.05)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " +
            str(len(frame)).encode() +
            b"\r\n\r\n" +
            frame +
            b"\r\n"
        )

        time.sleep(0.05)  # limit bandwidth

@app.route("/video_feed")
def video_feed():
    return Response(
        mjpeg_stream(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no"
        },
        direct_passthrough=True
    )

# Prusa uploader thread
def prusa_uploader():
    delay = DELAY_SECONDS

    while True:
        time.sleep(delay)

        with frame_lock:
            frame = latest_frame

        if frame is None:
            delay = LONG_DELAY_SECONDS
            continue

        # --- Read temperature & humidity ---
        #humidity, temperature = Adafruit_DHT.read_retry(DHT_SENSOR, DHT_PIN)

        with sensor_lock:
            temperature = sensor_temperature
            humidity = sensor_humidity

            temp_str = f"{temperature:.1f}°C" if temperature is not None else "N/A"
            hum_str = f"{humidity:.1f}%" if humidity is not None else "N/A"
            if temperature is not None and humidity is not None:
                store_sensor_reading(temperature, humidity)


        # --- Add overlay ---
        from io import BytesIO
        from datetime import datetime
        
        try:
            img = Image.open(BytesIO(frame)).convert("RGB")
            draw = ImageDraw.Draw(img)
        
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            overlay_text = f"{timestamp}\nTemp: {temp_str}\nHumidity: {hum_str}"
            lines = overlay_text.split("\n")
        
            box_margin = 8
            line_spacing = 6
        
            # Measure text
            widths = []
            heights = []
        
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                widths.append(bbox[2] - bbox[0])
                heights.append(bbox[3] - bbox[1])
        
            text_width = max(widths)
            text_height = sum(heights) + line_spacing * (len(lines) - 1)
        
            # Draw background box
            draw.rectangle(
                [
                    0,
                    0,
                    text_width + box_margin * 2,
                    text_height + box_margin * 2
                ],
                fill=(0, 0, 0)
            )
        
            # Draw text
            y = box_margin
            for line, h in zip(lines, heights):
                draw.text(
                    (box_margin, y),
                    line,
                    fill=(255, 255, 255),
                    font=font
                )
                y += h + line_spacing
        
            # Encode JPEG
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=80)
            frame_with_overlay = buf.getvalue()
        
        except Exception as e:
            print("Overlay failed:", e)
            frame_with_overlay = frame
        

        # --- Upload to Prusa Connect ---
        try:
            response = requests.put(
                HTTP_URL,
                headers={
                    "accept": "*/*",
                    "content-type": "image/jpg",
                    "fingerprint": FINGERPRINT,
                    "token": CAMERA_TOKEN
                },
                data=frame_with_overlay,
                timeout=10,
                verify=False
            )

            if response.status_code in (200, 201, 204):
                delay = DELAY_SECONDS
            else:
                delay = LONG_DELAY_SECONDS

        except Exception:
            delay = LONG_DELAY_SECONDS

# Main
if __name__ == "__main__":
    init_sensor_csv()
    threading.Thread(
        target=camera_worker,
        daemon=True
    ).start()
    
    threading.Thread(
        target=dht_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=prusa_uploader,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=8081,
        threaded=True,
        debug=False
    )


