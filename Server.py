import toml
import logging
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit
import serial
import threading
import time

# DeviceStateManager クラスの定義
class DeviceStateManager:
    # スイッチ名と番号の固定マッピング
    SWITCH_MAPPING = {
        "Alpha": 2,
        "Blabo": 3,
        "Charlie": 4,
        "Delta": 5
    }

    def __init__(self, com_port, baud_rate, timeout):
        self.current_status = [0] * 4  # スイッチの状態を配列で管理、初期はすべてOFF
        self.device_connected = True
        self.ser = self.connect_to_device(com_port, baud_rate, timeout)
    
    def connect_to_device(self, com_port, baud_rate, timeout):
        try:
            return serial.Serial(com_port, baud_rate, timeout=timeout)
        except serial.SerialException as e:
            logger.error(f"COMポート接続エラー: {e}")
            return None

    def fetch_status_from_device(self):
        """6コマンドでデバイスから現在のステータスを取得し、current_statusを更新する"""
        if not self.ser or not self.ser.is_open:
            logger.error("デバイスに接続されていません")
            return "device_disconnected"

        try:
            self.ser.write(b'6')  # 6コマンドでステータス取得要求を送信
            response = self.ser.read()
            
            # 受信データをビット演算で解釈し、スイッチの状態を更新
            if response:
                self.current_status[0] = (response & 0b0001) > 0  # Alpha
                self.current_status[1] = (response & 0b0010) > 0  # Blabo
                self.current_status[2] = (response & 0b0100) > 0  # Charlie
                self.current_status[3] = (response & 0b1000) > 0  # Delta
                logger.info("デバイスからの最新ステータスを取得しました: %s", self.current_status)
            else:
                logger.warning("デバイスが応答しません")
                return "no_response"
        except serial.SerialException as e:
            logger.error("デバイスとの通信エラー: %s", e)
            return "communication_error"
        return self.get_status_dict()

    def set_switch_state(self, switch_name, desired_state):
        # スイッチ名を番号に変換
        switch_index = list(self.SWITCH_MAPPING.keys()).index(switch_name)
        if switch_index is None:
            logger.error(f"無効なスイッチ名: {switch_name}")
            return "invalid_switch"

        # 現在のステータスと比較して、変更が必要か確認
        current_state = self.current_status[switch_index]
        if current_state == desired_state:
            logger.info(f"スイッチ {switch_name} はすでに指定の状態です: {desired_state}")
            return self.fetch_status_from_device()  # 状態変更不要でも最新ステータスを取得して返す

        # 状態が異なる場合のみ切り替えコマンドを送信
        try:
            self.ser.write(bytes([self.SWITCH_MAPPING[switch_name]]))  # スイッチ操作コマンド送信
            self.current_status[switch_index] = desired_state  # 状態を更新
            logger.info(f"スイッチ {switch_name} を {desired_state} に設定しました")
            return self.fetch_status_from_device()  # 全スイッチの最新状態を返す
        except serial.SerialTimeoutException:
            logger.error("デバイスが応答しません")
            return "timeout"
        except Exception as e:
            logger.exception("スイッチ操作エラー")
            return "error"

    def get_status_dict(self):
        """現在のスイッチ状態を辞書形式で返す"""
        return {
            "Alpha": self.current_status[0],
            "Blabo": self.current_status[1],
            "Charlie": self.current_status[2],
            "Delta": self.current_status[3]
        }

    def get_current_status(self):
        # デバイスから最新のステータスを取得して返す
        return self.fetch_status_from_device()

# FlaskおよびSocket.IOを利用したサーバーの設定とエンドポイントの定義
app = Flask(__name__)
socketio = SocketIO(app)

@app.route('/api/status', methods=['GET'])
def get_status():
    # デバイスから最新のスイッチの状態を取得
    return jsonify(device_manager.get_current_status())

@app.route('/api/switch', methods=['POST'])
def switch_state():
    # 複数のスイッチをAND条件で設定
    results = {}
    for switch_name, desired_state in request.args.items():
        result = device_manager.set_switch_state(switch_name, int(desired_state))
        if result in ["timeout", "error", "invalid_switch"]:
            return jsonify(error=f"スイッチ {switch_name} の操作に失敗しました: {result}"), 500
        results[switch_name] = result

    # 全ステータスを返すために、最新のステータスを取得して返却
    return jsonify(device_manager.get_current_status())

@socketio.on('connect')
def handle_connect():
    emit('status_update', device_manager.get_current_status())

# メイン関数の定義
def main():
    global device_manager, logger

    # 設定ファイルの読み込み
    config = toml.load('config.toml')

    # ロギングの設定
    log_level = getattr(logging, config['logging']['log_level'].upper(), logging.INFO)
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    # DeviceStateManagerのインスタンス作成
    device_manager = DeviceStateManager(
        config['device']['com_port'],
        config['device']['baud_rate'],
        config['device']['timeout']
    )

    # サーバーの起動
    socketio.run(app, host=config['server']['host'], port=config['server']['port'])

# スクリプトが直接実行されたときだけmain関数を実行
if __name__ == '__main__':
    main()
