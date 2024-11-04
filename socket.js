// src/socket.js
import io from 'socket.io-client';
import config from './config';

const socket = io(config.serverUrl); // サーバーURLを使用して接続

export default socket;
