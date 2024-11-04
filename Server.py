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
            return None

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
                return None
        except serial.SerialException as e:
            logger.error("デバイスとの通信エラー: %s", e)
            return None
        return self.get_status_dict()

    def set_switch_state(self, switch_name, desired_state):
        # スイッチ名をインデックスに変換
        if switch_name not in self.SWITCH_MAPPING:
            logger.error(f"無効なスイッチ名: {switch_name}")
            return None

        switch_index = list(self.SWITCH_MAPPING.keys()).index(switch_name)
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
            return None
        except Exception as e:
            logger.exception("スイッチ操作エラー")
            return None

    def get_status_dict(self):
        """現在のスイッチ状態を辞書

