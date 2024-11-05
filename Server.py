import toml
import logging
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import serial
import threading
import time

# 設定ファイルの読み込み
config = toml.load('config.toml')

# FlaskとSocketIOの初期化
app = Flask(__name__, static_folder='static')
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
    # スイッチ名とデバイス上の番号を対応付けるマッピング
    switch_map = {
        "Alpha": 2,
        "Bravo": 3,
        "Charlie": 4,
        "Delta": 5
    }

    def __init__(self, com_port, baud_rate, timeout):
        # デバイス接続とスイッチ状態の初期設定
        self.current_status = [0, 0, 0, 0]
        self.device_connected = True
        self.com_port = com_port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.ser = self.connect_to_device()
        self.lock = threading.Lock()
        self.last_update_time = time.time()

        # 初回のデバイス状態取得でキャッシュを最新化
        if self.device_connected:
            self.update_status()

    def connect_to_device(self):
        """デバイスに接続し、成功した場合はシリアルオブジェクトを返す"""
        try:
            ser = serial.Serial(self.com_port, self.baud_rate, timeout=self.timeout)
            logger.info("デバイスに接続しました")
            return ser
        except serial.SerialException as e:
            logger.error(f"COMポート接続エラー: {e}")
            return None

    def update_status(self):
        """デバイスのステータスを定期的に更新し、状態の変化や接続状況を管理"""
        while True:
            with self.lock:
                if self.ser and self.ser.is_open:
                    try:
                        self.ser.write(b'6')  # ステータス取得コマンド
                        response = self.ser.read()
                        if not response:
                            # デバイスが応答しない場合、接続エラーとして扱い、エラーメッセージを送信
                            if self.device_connected:
                                self.device_connected = False
                                socketio.emit('error', {'message': 'デバイスが応答しません'})
                                logger.warning("デバイスが応答しません")
                            continue

                        # 取得したデバイスの状態を配列形式で更新し、変化があれば通知
                        new_status = [
                            (response & 0b0001) > 0,
                            (response & 0b0010) > 0,
                            (response & 0b0100) > 0,
                            (response & 0b1000) > 0
                        ]
                        
                        if new_status != self.current_status:
                            self.current_status = new_status
                            self.last_update_time = time.time()
                            socketio.emit('status_update', self.get_status_dict())
                            logger.info("ステータスが変更されました: %s", self.current_status)

                    except serial.SerialException as e:
                        # シリアル例外が発生した場合は接続エラーとして扱い、エラーメッセージを送信
                        if self.device_connected:
                            self.device_connected = False
                            socketio.emit('error', {'message': 'COMポート接続エラーが発生しました'})
                            logger.error(f"COMポート接続エラー: {e}")
                else:
                    # デバイス未接続の場合は再接続を試み、接続が回復した場合に通知
                    if self.device_connected:
                        self.device_connected = False
                        socketio.emit('error', {'message': 'デバイスとの接続が失われました'})
                        logger.warning("デバイスとの接続が失われました")

                    # 再接続の試行
                    self.ser = self.connect_to_device()
                    if self.ser and self.ser.is_open:
                        self.device_connected = True
                        socketio.emit('reconnected', {'message': 'デバイスとの接続が再確立されました'})
                        logger.info("デバイスとの接続が再確立されました")

            # 次回の更新まで待機
            time.sleep(5)

    def get_status_dict(self):
        """キャッシュされたデバイス状態を辞書形式で返却"""
        return {name: int(state) for name, state in zip(self.switch_map.keys(), self.current_status)}

    def set_switch_state(self, switch_name, desired_state):
        """指定されたスイッチの状態を要求通りに変更"""
        if switch_name not in self.switch_map:
            logger.warning("無効なスイッチ名: %s", switch_name)
            return None

        switch_number = self.switch_map[switch_name]
        current_index = list(self.switch_map.keys()).index(switch_name)

        if not self.ser or not self.ser.is_open:
            logger.warning("デバイス未接続で操作が試行されました")
            return None

        # ロック機構で排他制御し、状態に変化がある場合のみ操作
        with self.lock:
            if self.current_status[current_index] != desired_state:
                try:
                    self.ser.write(bytes([switch_number]))
                    self.current_status[current_index] = desired_state
                    self.last_update_time = time.time()
                    logger.info(f"スイッチ {switch_name} を {desired_state} に設定し、キャッシュを更新しました")
                    return self.get_status_dict()

                except serial.SerialTimeoutException:
                    logger.error("デバイスが応答しません")
                    return "timeout"
                except Exception as e:
                    logger.exception("サーバー内部エラーが発生しました")
                    return "error"
            else:
                logger.info(f"スイッチ {switch_name} はすでに要求の状態 {desired_state}")
                return self.get_status_dict()

device_manager = DeviceStateManager(
    config['device']['com_port'],
    config['device']['baud_rate'],
    config['device']['timeout']
)

@app.route('/')
def serve():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    """キャッシュから最新の状態を返却。オプションでデバイスから即時更新"""
    update = request.args.get('update', 'false').lower() == 'true'
    
    with device_manager.lock:
        if update and device_manager.device_connected:
            # キャッシュを即時更新するためデバイスから直接状態を取得
            device_manager.update_status()
        
        # デバイスが未接続の場合のエラーハンドリング
        if device_manager.device_connected:
            return jsonify(device_manager.get_status_dict())
        else:
            return jsonify(error="COMポート未接続"), 503

@app.route('/api/set_switch', methods=['POST'])
def set_switch():
    """指定されたスイッチの状態を変更"""
    if not device_manager.ser or not device_manager.ser.is_open:
        logger.warning("COMポート未接続のためスイッチ操作に失敗しました")
        return jsonify(error="COMポート未接続"), 503

    responses = {}
    for switch_param in request.args.getlist('switch'):
        # パラメータのフォーマット検証
        if ':' not in switch_param:
            return jsonify(error="無効なパラメータ形式。'スイッチ名:状態'の形式で指定してください。"), 400

        name, state = switch_param.split(':')
        
        # スイッチ名と状態値の妥当性検証
        if name not in device_manager.switch_map:
            return jsonify(error=f"無効なスイッチ名: {name}"), 400
        if state not in ['0', '1']:
            return jsonify(error=f"無効な状態値: {state}。0または1を指定してください。"), 400

        # 状態値を整数に変換
        state = int(state)
        result = device_manager.set_switch_state(name, state)
        
        if result == "timeout":
            return jsonify(error="デバイスが応答しません"), 408
        elif result == "error":
            return jsonify(error="サーバー内部エラーが発生しました"), 500
        responses[name] = result

    return jsonify(device_manager.get_status_dict())

@socketio.on('connect')
def handle_connect():
    """クライアント接続時に最新のステータスを送信"""
    with device_manager.lock:
        emit('status_update', device_manager.get_status_dict())

if __name__ == '__main__':
    threading.Thread(target=device_manager.update_status).start()
    socketio.run(app, host=config['server']['host'], port=config['server']['port'])

