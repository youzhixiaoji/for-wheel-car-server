#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import cv2
import atexit
import time
import serial
import threading
from threading import Lock
from flask import Flask, Response, request, jsonify, render_template
from collections import deque
import json
import logging

app = Flask(__name__)

# ====== 串口配置 ======
SERIAL_PORT = "/dev/ttyUSB0"  # 根据实际情况修改
SERIAL_BAUDRATE = 115200
SERIAL_TIMEOUT = 1

_serial_port = None
_serial_lock = Lock()
_serial_messages = deque(maxlen=100)  # 保存最近100条消息

def open_serial():
    global _serial_port
    with _serial_lock:
        if _serial_port is None:
            try:
                ser = serial.Serial(SERIAL_PORT, SERIAL_BAUDRATE, timeout=SERIAL_TIMEOUT)
                _serial_port = ser
                print(f"串口 {SERIAL_PORT} 打开成功")
                # 启动串口读取线程
                read_thread = threading.Thread(target=read_serial_thread, daemon=True)
                read_thread.start()
            except Exception as e:
                print(f"无法打开串口 {SERIAL_PORT}: {e}")
                _serial_port = None

def close_serial():
    global _serial_port
    with _serial_lock:
        if _serial_port is not None:
            try:
                _serial_port.close()
            except Exception:
                pass
            _serial_port = None

def read_serial_thread():
    """串口读取线程"""
    while _serial_port and _serial_port.is_open:
        try:
            if _serial_port.in_waiting > 0:
                line = _serial_port.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    timestamp = time.strftime("%H:%M:%S")
                    message = f"[{timestamp}] 接收: {line}"
                    _serial_messages.append(message)
                    print(message)
        except Exception as e:
            print(f"串口读取错误: {e}")
            time.sleep(0.1)
        time.sleep(0.01)

def send_serial_command(command):
    """发送串口命令"""
    open_serial()
    with _serial_lock:
        if _serial_port and _serial_port.is_open:
            try:
                _serial_port.write((command).encode('utf-8'))
                _serial_port.flush()
                timestamp = time.strftime("%H:%M:%S")
                message = f"[{timestamp}] 发送: {command}"
                _serial_messages.append(message)
                return True
            except Exception as e:
                print(f"串口发送错误: {e}")
                return False
        else:
            print("串口未打开")
            return False

# ====== 摄像头配置 ======
VIDEO_SOURCE = 1        # 默认摄像头，可以改为 /dev/video0
FRAME_WIDTH  = 640
FRAME_HEIGHT = 480
FPS          = 30

_cap = None
_cap_lock = Lock()

def open_camera():
    global _cap
    with _cap_lock:
        if _cap is None:
            cap = cv2.VideoCapture(VIDEO_SOURCE, cv2.CAP_ANY)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS,         FPS)
            if not cap.isOpened():
                raise RuntimeError(f"无法打开摄像头：{VIDEO_SOURCE}")
            _cap = cap

def close_camera():
    global _cap
    with _cap_lock:
        if _cap is not None:
            try:
                _cap.release()
            except Exception:
                pass
            _cap = None

atexit.register(close_camera)
atexit.register(close_serial)


def mjpeg_generator():
    open_camera()
    target_interval = 1.0 / max(FPS, 1)
    while True:
        t0 = time.time()
        with _cap_lock:
            if _cap is None:
                break
            ok, frame = _cap.read()
        if not ok or frame is None:
            time.sleep(0.05)
            continue
        frame = cv2.flip(frame, -1)  # 水平+垂直（等效旋转180°）
        ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            continue
        jpg = buf.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n'
               b'Content-Length: ' + str(len(jpg)).encode() + b'\r\n\r\n' +
               jpg + b'\r\n')
        elapsed = time.time() - t0
        if elapsed < target_interval:
            time.sleep(target_interval - elapsed)


# ====== 路由 ======
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/video_feed")
def video_feed():
    return Response(mjpeg_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/drive", methods=["POST"])
def drive():
    data = request.get_json(silent=True) or {}
    print("[DRIVE]", data)
    
    # 处理小车控制指令
    direction = data.get('direction')
    speed = data.get('set_speed')
    action_type = data.get('action_type', 'press')  # press 或 release
    
    if direction:
        # 方向控制
        command_map = {
            'forward': 'w',
            'backward': 's', 
            'turnLeft': 'a',
            'turnRight': 'd',
            'stopMove': 'r'
        }
        if direction in command_map:
            # 停止按钮只在按下时发送r，其他按钮在按下时发送方向字符，松开时发送k
            if direction == 'stopMove':
                send_serial_command(command_map[direction])
            else:
                if action_type == 'press':
                    send_serial_command(command_map[direction])
                elif action_type == 'release':
                    send_serial_command('m')
    
    if speed:
        # 速度控制
        speed_map = {
            'high': 't',
            'mid': 'g',
            'low': 'b'
        }
        if speed in speed_map:
            send_serial_command(speed_map[speed])
    
    return jsonify({"ok": True, "echo": data})

@app.route("/camera", methods=["POST"])
def camera():
    data = request.get_json(silent=True) or {}
    print("[CAMERA]", data)
    
    # 处理云台控制指令
    direction = data.get('direction')
    speed = data.get('set_speed')
    action_type = data.get('action_type')  # press 或 release
    if direction:
        command_map = {
            'up': 'i',
            'down': 'k',
            'left': 'j', 
            'right': 'l'           
        }
        if direction in command_map:
            #按钮在按下时发送云台转动字符，松开时发送p
            if action_type == 'press':
                send_serial_command(command_map[direction])
            elif action_type == 'release':
                send_serial_command('p')
    return jsonify({"ok": True, "echo": data})

@app.route("/serial_messages", methods=["GET"])
def get_serial_messages():
    """获取串口消息"""
    messages = list(_serial_messages)
    return jsonify({"messages": messages})

@app.route("/clear_messages", methods=["POST"])
def clear_messages():
    """清空消息"""
    _serial_messages.clear()
    return jsonify({"ok": True})


class NoSerialMessagesLogging(logging.Filter):
    def filter(self, record):
        # 过滤掉 /serial_messages 的GET请求日志
        return not ('GET /serial_messages' in record.getMessage() and record.levelname == 'INFO')

if __name__ == "__main__":
    # 只屏蔽 /serial_messages 的GET请求日志
    log = logging.getLogger('werkzeug')
    log.addFilter(NoSerialMessagesLogging())
    
    try:
        open_camera()
    except Exception as e:
        print("警告：启动时未能打开摄像头 ->", e)
    
    try:
        open_serial()
    except Exception as e:
        print("警告：启动时未能打开串口 ->", e)
    
    print("服务器启动在 http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)
