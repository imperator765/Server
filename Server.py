import functools
import sys
import toml
import logging
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import serial
import threading
import time
from enum import Enum

# 設定ファイルの読み込み
try:
    config = toml.load('config.toml')
except (toml.TomlDecodeError, FileNotFoundError) as e:
    print(f"設定ファイルの読み込みに失敗しました: {e}")
    sys.exit(1)

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
    TIMEOUT = ("TIMEOUT", "デバイスへの操作がタイムアウトしました", 408)
    INTERNAL_ERROR = ("INTERNAL_ERROR", "サーバー内部エラーが発生しました", 500)
    INVALID_OPERATION = ("INVALID_OPERATION", "無効な操作です", 400)
    CONNECTION_ERROR = ("CONNECTION_ERROR", "デバイス接続に関するエラーが発生しました", 503)

    def __init__(self, error_code, message, http_status):
        self.error_code = error_code
        self.message = message
        self.http_status = http_status

# カスタム例外クラスの定義
class DeviceException(Exception):
    def __init__(self, device_error):
        self.device_error = device_error
        super().__init__(device_error.message)

# エラーレスポンス生成関数
def create_error_response(device_error):
    # 標準化されたエラーレスポンスを生成
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

    def __init__(self, config):
        # 設定の読み込みと初期値の設定
        self.com_port = config.get('com_port', 'COM1')
        self.baud_rate = config.get('baud_rate', 9600)
        self.timeout = config.get('timeout', 1)
        self.write_timeout = config.get('write_timeout', 1)
        self.min_retry_interval = config.get('min_retry_interval', 10)
        self.max_retry_interval = config.get('max_retry_interval', 300)
        self.poll_interval = config.get('poll_interval', 5)
        self.failure_reset_interval = config.get('failure_reset_interval', 600)
        self.max_failure_threshold = config.get('max_failure_threshold', 5)

        self.current_status = [0, 0, 0, 0]
        self.device_connected = False
        self.ser = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_failure_time = None
        self.failure_count = 0

        # デバイス接続
        self.ser = self.connect_to_device()

        # 初回のデバイス状態取得でステータスを最新化
        if self.device_connected:
        try:
            initial_status = self.update_status(notify_clients=False)
            logger.info("初期化時にデバイス状態を取得しました: %s", initial_status)
        except DeviceException as e:
            logger.error("初期化時にエラーが発生: %s", e.device_error.message)

    def connect_to_device(self):
        # 既存の接続をクローズする
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
                logger.info("既存のシリアル接続を閉じました")
            except Exception as e:
                logger.error("シリアル接続のクローズ時に予期しないエラーが発生しました: %s", e)
        
        # 新しい接続を作成
        try:
            ser = serial.Serial(
                self.com_port,
                self.baud_rate,
                timeout=self.timeout,
                write_timeout=self.write_timeout
            )
            self.device_connected = True
            logger.info("デバイスに接続しました")
            return ser
        except serial.SerialException as e:
            self.device_connected = False
            logger.error("COMポート接続エラー: %s", e)
            return None
        except Exception as e:
            self.device_connected = False
            logger.critical("デバイス接続時に予期しないエラーが発生しました: %s", e)
            return None
    
    def stop_monitoring(self):
        self.stop_event.set()  # 終了フラグをセット
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
                logger.info("デバイス接続を閉じました")
            except Exception as e:
                logger.error("シリアル接続のクローズ時に予期しないエラーが発生しました: %s", e)

    def failure_count_check(self):
        if self.last_failure_time and (time.time() - self.last_failure_time > self.failure_reset_interval):
            logger.info("接続エラー・タイムアウト回数がリセットされました")
            self.failure_count = 0
            self.last_failure_time = None

        if self.failure_count >= self.max_failure_threshold:
            logger.error("接続エラー・タイムアウト回数が閾値を超えました。")
            self.device_connected = False
            self.failure_count = 0
            self.last_failure_time = None

    def attempt_reconnection(self):
    # 再試行処理を管理し、接続が回復するまで一定の間隔で試行する
        retry_interval = self.min_retry_interval

        while not self.device_connected and not self.stop_event.is_set():
            new_connection = self.connect_to_device()
            if new_connection:
                with self.lock:
                    self.ser = new_connection
                logger.info("デバイスとの接続が回復しました")
                socketio.emit('device_reconnected', {"data": self.get_status_dict()}, broadcast=True)
                return
            else:
                logger.error("再接続に失敗しました。次の試行まで %d 秒待機します。", retry_interval)
                self.stop_event.wait(retry_interval)
                retry_interval = min(retry_interval * 2, self.max_retry_interval)  # インターバルを倍に、上限はself.max_retry_interval


    def monitor_device_status(self):
    # デバイスの状態を定期的に確認し、未接続時は再接続を試みる
        while not self.stop_event.is_set():
            self.failure_count_check()

            if not self.device_connected:
                logger.error("デバイスが未接続です。再接続を試みます...")
                socketio.emit('error', {"error_status": "not connected"}, broadcast=True)
                self.attempt_reconnection()
            else:
                try:
                    self.update_status(notify_clients=True)
                except DeviceException as e:
                    logger.error("状態取得中にエラーが発生しました: %s", e)

            # ポーリングの待機
            self.stop_event.wait(self.poll_interval)

    def safe_serial_operation(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            with self.lock:
                try:
                    return func(self, *args, **kwargs)
                except serial.SerialTimeoutException as e:
                    self.failure_count += 1
                    self.last_failure_time = time.time()
                    logger.error("デバイスへの操作がタイムアウトしました: %s", e)
                    raise DeviceException(DeviceError.TIMEOUT)
                except serial.SerialException as e:
                    self.failure_count += 1
                    self.last_failure_time = time.time()
                    logger.error("シリアル通信エラー: %s", e)
                    raise DeviceException(DeviceError.CONNECTION_ERROR)
                except Exception as e:
                    logger.exception("予期しないエラーが発生しました")
                    raise DeviceException(DeviceError.INTERNAL_ERROR)
        return wrapper

    @safe_serial_operation
    def update_status(self, notify_clients=True):
        # デバイスのステータスを更新し、必要に応じてクライアントに通知
        if not self.ser or not self.ser.is_open:
            self.device_connected = False
            raise DeviceException(DeviceError.CONNECTION_ERROR)
            
        self.ser.write(b'6\n')
        response = self.ser.readline().strip()
            
        if not response:
            logger.error("デバイスへの操作がタイムアウトしました")
            self.failure_count += 1
            self.last_failure_time = time.time()
            raise DeviceException(DeviceError.TIMEOUT)
        
        try:
            # レスポンスを整数に変換
            response = int(response.decode('utf-8'))
        except ValueError:
            logger.error("不正なデータを受信しました: %s", response)
            raise DeviceException(DeviceError.INTERNAL_ERROR)

        # 取得したデバイスの状態を配列形式で更新
        new_status = [
            (response & 0b0001) > 0,
            (response & 0b0010) > 0,
            (response & 0b0100) > 0,
            (response & 0b1000) > 0
        ]

        # 状態が変わった場合のみキャッシュを更新し、クライアントに通知
        if new_status != self.current_status:
            self.current_status = new_status
            if notify_clients:
                socketio.emit('status_update', {"data": self.get_status_dict()}, broadcast=True)
                logger.info("クライアントにステータス更新を通知しました: %s", self.current_status)

        logger.debug("現在のデバイス状態を取得: %s", new_status)
        return self.get_status_dict()

    def get_status_dict(self):
        """ キャッシュされたデバイス状態を辞書形式で取得 """
        return {name: int(state) for name, state in zip(self.switch_map.keys(), self.current_status)}

    @safe_serial_operation
    def set_switch_state(self, switch_states):
        # 指定されたスイッチの状態を要求通りに変更
        if not self.ser or not self.ser.is_open:
            self.device_connected = False
            raise DeviceException(DeviceError.CONNECTION_ERROR)
        
        # スイッチ名の事前チェック
        invalid_switches = [name for name in switch_states if name not in self.switch_map]
        if invalid_switches:
            logger.error("無効なスイッチ名: %s", ", ".join(invalid_switches))
            raise DeviceException(DeviceError.INVALID_OPERATION)

        commands = ""
        for switch_name, desired_state in switch_states.items():
            current_index = list(self.switch_map.keys()).index(switch_name)
            if self.current_status[current_index] == desired_state:
                logger.info("スイッチ %s はすでに要求の状態 %s", switch_name, desired_state)
                continue

            # スイッチの状態を変更するコマンドを作成（整数を文字列形式で追加）
            switch_number = self.switch_map[switch_name]
            commands += f"{switch_number}\n"

        if commands:
            self.ser.write(commands.encode('utf-8'))

        
        status_check = self.update_status(notify_clients=True)

        # 要求された状態と一致するかを確認
        for switch_name, desired_state in switch_states.items():
            current_index = list(self.switch_map.keys()).index(switch_name)
            if self.current_status[current_index] != desired_state:
                logger.error("スイッチの要求状態と送信後の状態が一致しません: %s, 要求状態: %s", switch_name, desired_state)
                raise DeviceException(DeviceError.INTERNAL_ERROR)

        # 操作完了を通知
        logger.info("スイッチ操作が完了しました: %s", switch_states)
        # 成功時のデータ返却
        return self.get_status_dict()


device_manager = DeviceStateManager(config['device'])

@app.route('/')
def serve():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    # キャッシュから最新の状態を返却。オプションでデバイスから即時更新
    update = request.args.get('update', 'false').lower() == 'true'
    if not device_manager.device_connected:
        return create_error_response(DeviceError.CONNECTION_ERROR)

    if update:
        # デバイスから最新の情報を取得
        try:
            data = device_manager.update_status(notify_clients=False)
        except DeviceException as e:
            return create_error_response(e.device_error)
    else:
        # キャッシュから状態を返す
        data = device_manager.get_status_dict()

    return jsonify({"status": "success", "data": data})

@app.route('/api/set_switch', methods=['POST'])
def set_switch():
    # 指定されたスイッチの状態を変更
    if not device_manager.device_connected:
        return create_error_response(DeviceError.CONNECTION_ERROR)

    switch_states = {}
    for switch_param in request.args.getlist('switch'):
        if ':' not in switch_param:
            return create_error_response(DeviceError.INVALID_OPERATION)

        name, state = switch_param.split(':')
            
        if not isinstance(name, str) or not state.isdigit() or int(state) not in [0, 1]:
            return create_error_response(DeviceError.INVALID_OPERATION)

        switch_states[name] = int(state)

    try:
        result = device_manager.set_switch_state(switch_states)
    except DeviceException as e:
        return create_error_response(e.device_error)

    return jsonify({"status": "success", "data": result})

@socketio.on('connect')
def handle_connect():
    emit('status_update', device_manager.get_status_dict())

if __name__ == '__main__':
    try:
        # ポーリングスレッドの起動
        polling_thread = threading.Thread(target=device_manager.monitor_device_status, daemon=True)
        polling_thread.start()
        logger.info("ポーリングスレッドが起動しました")

        socketio.run(app, host=config['server']['host'], port=config['server']['port'])
    except KeyboardInterrupt:
        logger.info("アプリケーションの終了を検知しました。クリーンアップを実行します。")
        device_manager.stop_monitoring()
        polling_thread.join()  # スレッドの終了を待つ
        logger.info("ポーリングスレッドが正常に終了しました")
