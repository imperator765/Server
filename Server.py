import toml
import logging
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import serial
import threading
import time
from enum import Enum

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

# エラーを定義するEnum
class DeviceError(Enum):
    TIMEOUT = ("TIMEOUT", "デバイスが応答しません", 408)
    INTERNAL_ERROR = ("INTERNAL_ERROR", "サーバー内部エラーが発生しました", 500)
    INVALID_OPERATION = ("INVALID_OPERATION", "無効な操作です", 400)
    NOT_CONNECTED = ("NOT_CONNECTED", "デバイス未接続", 503)

    def __init__(self, error_code, message, http_status):
        self.error_code = error_code
        self.message = message
        self.http_status = http_status

# エラーレスポンス生成関数
def create_error_response(device_error):
    # 標準化されたエラーレスポンスを生成
    logger.error("[API_ERROR] %s - %s", device_error.error_code, device_error.message)
    response = {
        "status": "error",
        "error_code": device_error.error_code,
        "message": device_error.message
    }
    return jsonify(response), device_error.http_status

class DeviceStateManager:
    # スイッチ名とデバイス上の番号を対応付けるマッピング
    switch_map = {
        "Alpha": 2,
        "Bravo": 3,
        "Charlie": 4,
        "Delta": 5
    }

    def __init__(self, com_port, baud_rate, timeout, write_timeout):
        # デバイス接続とスイッチ状態の初期設定
        self.current_status = [0, 0, 0, 0]
        self.device_connected = True
        self.com_port = com_port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.ser = self.connect_to_device()
        self.lock = threading.Lock()
        self.last_update_time = time.time()

        # 初回のデバイス状態取得でキャッシュを最新化
        if self.device_connected:
            initial_status = self.get_device_status()
            if isinstance(initial_status, DeviceError):
                logger.warning(f"[INITIALIZATION_ERROR] 初期化時にエラーが発生: {initial_status.message}")
            else:
                logger.info("初期化時にデバイス状態を取得しました: %s", initial_status["data"])

    def connect_to_device(self):
        # デバイスに接続し、成功した場合はシリアルオブジェクトを返す
        try:
            ser = serial.Serial(
                self.com_port,
                self.baud_rate,
                timeout=self.timeout,           # 読み込み時のタイムアウト
                write_timeout=self.write_timeout # 書き込み時のタイムアウト
            )
            logger.info("デバイスに接続しました")
            return ser
        except serial.SerialException as e:
            logger.error(f"COMポート接続エラー: {e}")
            return None

    def get_device_status(self):
        # デバイスに6コマンドを送信して現在の状態を取得し、キャッシュを更新
        try:
            self.ser.write(b'6')
            response = self.ser.read()
            
            if not response:
                logger.error("[TIMEOUT] デバイスが応答しません")
                return DeviceError.TIMEOUT

            # 取得したデバイスの状態を配列形式で更新
            new_status = [
                (response & 0b0001) > 0,
                (response & 0b0010) > 0,
                (response & 0b0100) > 0,
                (response & 0b1000) > 0
            ]
            self.current_status = new_status
            logger.info("[DEVICE_STATUS] 現在のデバイス状態を取得しました: %s", new_status)
            return {"status": "success", "data": self.get_status_dict()}

        except serial.SerialTimeoutException:
            logger.error("[TIMEOUT] 状態取得時にタイムアウトが発生しました")
            return DeviceError.TIMEOUT
        except Exception as e:
            logger.critical("[INTERNAL_ERROR] 状態取得時にサーバー内部エラーが発生しました: %s", e)
            return DeviceError.INTERNAL_ERROR

    def update_status(self):
        # デバイスのステータスを定期的に更新し、状態の変化や接続状況を管理
        with self.lock:
            # 即時取得用の`get_device_status`を呼び出し、デバイス状態を取得
            status_check = self.get_device_status()

            # エラーが発生した場合、エラーログのみ記録して終了
            if isinstance(status_check, DeviceError):
                logger.warning(f"[ERROR] ポーリング中にエラーが発生: {status_check.message}")
                return  # エラー時は終了（エラーメッセージは既にログ済み）

            # 正常に取得できた場合、現在の状態と比較して変更があれば通知
            if status_check["data"] != self.get_status_dict():
                self.current_status = [
                    status_check["data"]["Alpha"],
                    status_check["data"]["Bravo"],
                    status_check["data"]["Charlie"],
                    status_check["data"]["Delta"]
                ]
                self.last_update_time = time.time()
                socketio.emit('status_update', self.get_status_dict())
                logger.info("ステータスが変更されました: %s", self.current_status)

    def get_status_dict(self):
        # キャッシュされたデバイス状態を辞書形式で返却
        return {name: int(state) for name, state in zip(self.switch_map.keys(), self.current_status)}

    def set_switch_state(self, switch_name, desired_state):
        # 指定されたスイッチの状態を要求通りに変更し、確認
        if switch_name not in self.switch_map:
            logger.warning("[INVALID_OPERATION] 無効なスイッチ名: %s", switch_name)
            return DeviceError.INVALID_OPERATION

        switch_number = self.switch_map[switch_name]
        current_index = list(self.switch_map.keys()).index(switch_name)

        if not self.ser or not self.ser.is_open:
            logger.warning("[CONNECTION] デバイス未接続で操作が試行されました")
            return DeviceError.NOT_CONNECTED

        with self.lock:
            # 切り替え前にデバイスの実際の状態を確認してキャッシュを更新
            status_check = self.get_device_status()
            if isinstance(status_check, DeviceError):
                return status_check

            # 更新したキャッシュに基づいて切り替えの必要性を確認
            if self.current_status[current_index] == desired_state:
                logger.info("[NO_CHANGE] スイッチ %s はすでに要求の状態 %s", switch_name, desired_state)
                return {
                    "status": "success",
                    "data": {
                        "switches": self.get_status_dict()
                    }
                }

            # スイッチの状態を変更
            try:
                self.ser.write(bytes([switch_number]))
                self.last_update_time = time.time()
                logger.info("[SUCCESS] スイッチ %s を %s に設定しました。確認中...", switch_name, desired_state)

                # 切り替え後の確認として共通関数で状態確認
                status_check = self.get_device_status()
                if isinstance(status_check, DeviceError):
                    return status_check

                # 切り替え後の状態が期待通りでない場合、エラーを返却
                if self.current_status[current_index] != desired_state:
                    logger.error("[SWITCH_ERROR] スイッチ %s の切り替えに失敗しました", switch_name)
                    return DeviceError.INTERNAL_ERROR

                # 成功時のデータ返却
                return {
                    "status": "success",
                    "data": {
                        "switches": self.get_status_dict()  # 現在の全スイッチの状態を返す
                    }
                }

            except serial.SerialTimeoutException:
                logger.error("[TIMEOUT] スイッチ %s への書き込みがタイムアウトしました", switch_name)
                return DeviceError.TIMEOUT
            except Exception as e:
                logger.critical("[INTERNAL_ERROR] サーバー内部エラーが発生しました: %s", e)
                return DeviceError.INTERNAL_ERROR

device_manager = DeviceStateManager(
    config['device']['com_port'],
    config['device']['baud_rate'],
    config['device']['timeout'],
    config['device']['write_timeout']
)

@app.route('/')
def serve():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    # キャッシュから最新の状態を返却。オプションでデバイスから即時更新
    update = request.args.get('update', 'false').lower() == 'true'
    
    with device_manager.lock:
        # `update`フラグがTrueの場合、`get_device_status`を使って即時の状態取得
        if update and device_manager.device_connected:
            status_check = device_manager.get_device_status()

            # エラーが発生した場合、`create_error_response`でレスポンスを生成
            if isinstance(status_check, DeviceError):
                return create_error_response(status_check)

            # 成功した場合、最新状態を返却
            return jsonify(status_check["data"])
        
        # デバイスが未接続の場合のエラーハンドリング
        if device_manager.device_connected:
            return jsonify(device_manager.get_status_dict())
        else:
            return create_error_response(DeviceError.NOT_CONNECTED)

@app.route('/api/set_switch', methods=['POST'])
def set_switch():
    # 指定されたスイッチの状態を変更
    if not device_manager.ser or not device_manager.ser.is_open:
        logger.warning("COMポート未接続のためスイッチ操作に失敗しました")
        return create_error_response(DeviceError.NOT_CONNECTED)

    responses = {}
    for switch_param in request.args.getlist('switch'):
        if ':' not in switch_param:
            return create_error_response(DeviceError.INVALID_OPERATION)

        name, state = switch_param.split(':')
        
        if name not in device_manager.switch_map:
            return create_error_response(DeviceError.INVALID_OPERATION)
        if state not in ['0', '1']:
            return create_error_response(DeviceError.INVALID_OPERATION)

        state = int(state)
        result = device_manager.set_switch_state(name, state)

        if isinstance(result, DeviceError):
            return create_error_response(result)
        
        responses[name] = result

    return jsonify({
        "status": "success",
        "data": {
            "switches": responses
        }
    })

@socketio.on('connect')
def handle_connect():
    # クライアント接続時に最新のステータスを送信
    with device_manager.lock:
        emit('status_update', device_manager.get_status_dict())

if __name__ == '__main__':
    threading.Thread(target=device_manager.update_status).start()
    socketio.run(app, host=config['server']['host'], port=config['server']['port'])
