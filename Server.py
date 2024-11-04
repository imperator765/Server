import toml
import logging
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import serial
import threading
import time
import os

# 設定ファイルを読み込む
config = toml.load('config.toml')

# Flaskアプリケーションの初期化
app = Flask(__name__, static_folder='static')  # 'static' フォルダから静的ファイルを提供
socketio = SocketIO(app)

# ロギングの設定
log_level = getattr(logging, config['logging']['log_level'].upper(), logging.INFO)
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config['logging']['log_file']),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DeviceStateManager:
    def __init__(self, com_port, baud_rate, timeout):
        self.current_status = None
        self.device_connected = True
        self.com_port = com_port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.ser = self.connect_to_device()
    
    def connect_to_device(self):
        try:
            ser = serial.Serial(self.com_port, self.baud_rate, timeout=self.timeout)
            logger.info("デバイスに接続しました")
            return ser
        except serial.SerialException as e:
            logger.error(f"COMポート接続エラー: {e}")
            return None

    def check_device_status(self):
        while True:
            if self.ser and self.ser.is_open:
                try:
                    # ステータス取得
                    self.ser.write(b'6')
                    response = self.ser.read()
                    if not response:
                        if self.device_connected:
                            self.device_connected = False
                            socketio.emit('error', {'message': 'デバイスが応答しません'})
                            logger.warning("デバイスが応答しません")
                        continue

                    # デバイスからの状態を解析
                    new_status = {
                        "switch_2": (response & 0b0001) > 0,
                        "switch_3": (response & 0b0010) > 0,
                        "switch_4": (response & 0b0100) > 0,
                        "switch_5": (response & 0b1000) > 0,
                    }

                    # 接続が再確立された場合
                    if not self.device_connected:
                        self.device_connected = True
                        socketio.emit('reconnected', {'message': 'デバイスとの接続が再確立されました'})
                        logger.info("デバイスとの接続が再確立されました")

                    # ステータスに変化があれば通知
                    if new_status != self.current_status:
                        self.current_status = new_status
                        socketio.emit('status_update', self.current_status)
                        logger.info("ステータス更新: %s", self.current_status)

                except serial.SerialException as e:
                    if self.device_connected:
                        self.device_connected = False
                        socketio.emit('error', {'message': 'COMポート接続エラーが発生しました'})
                        logger.error(f"COMポート接続エラー: {e}")

            else:
                if self.device_connected:
                    self.device_connected = False
                    socketio.emit('error', {'message': 'デバイスとの接続が失われました'})
                    logger.warning("デバイスとの接続が失われました")

                self.ser = self.connect_to_device()
                time.sleep(5)

            time.sleep(5)

    def get_current_status(self):
        return self.current_status

    def toggle_switch(self, switch_number, desired_state):
        if not self.ser or not self.ser.is_open:
            logger.warning("デバイス未接続で操作が試行されました")
            return None
        try:
            self.ser.write(bytes([switch_number]))
            self.ser.write(b'6')
            response = self.ser.read()
            self.current_status = {
                "switch_2": (response & 0b0001) > 0,
                "switch_3": (response & 0b0010) > 0,
                "switch_4": (response & 0b0100) > 0,
                "switch_5": (response & 0b1000) > 0,
            }
            logger.info(f"スイッチ {switch_number} を {desired_state} に変更")
            return self.current_status
        except serial.SerialTimeoutException:
            logger.error("デバイスが応答しません")
            return "timeout"
        except Exception as e:
            logger.exception("サーバー内部エラーが発生しました")
            return "error"

device_manager = DeviceStateManager(
    config['device']['com_port'],
    config['device']['baud_rate'],
    config['device']['timeout']
)

# Web UIのエンドポイント
@app.route('/')
def serve():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    status = device_manager.get_current_status()
    if status is None:
        logger.warning("COMポート未接続のためステータスを取得できません")
        return jsonify(error="COMポート未接続"), 503
    return jsonify(status)

@app.route('/api/toggle', methods=['POST'])
def toggle_switch():
    if not device_manager.ser or not device_manager.ser.is_open:
        logger.warning("COMポート未接続のためスイッチ操作に失敗しました")
        return jsonify(error="COMポート未接続"), 503

    for key, value in request.args.items():
        switch_number = int(key.split('_')[1])
        desired_state = int(value)
        result = device_manager.toggle_switch(switch_number, desired_state)
        if result == "timeout":
            return jsonify(error="デバイスが応答しません"), 408
        elif result == "error":
            return jsonify(error="サーバー内部エラーが発生しました"), 500

    return jsonify(device_manager.get_current_status())

@socketio.on('connect')
def handle_connect():
    emit('status_update', device_manager.get_current_status())

if __name__ == '__main__':
    threading.Thread(target=device_manager.check_device_status).start()
    socketio.run(app, host=config['server']['host'], port=config['server']['port'])
