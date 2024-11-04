// src/App.js
import React, { useState, useEffect } from 'react';
import socket from './socket';
import './App.css';

const App = () => {
  const [status, setStatus] = useState({});
  const [error, setError] = useState(null);
  const [isDarkMode, setIsDarkMode] = useState(true);

  useEffect(() => {
    // WebSocketでサーバーからのイベントをリッスン
    socket.on('status_update', (newStatus) => {
      setStatus(newStatus);
      setError(null); // エラーが解消された場合
    });

    socket.on('error', (errorData) => {
      setError(errorData.message);
    });

    socket.on('reconnected', () => {
      setError(null); // 接続回復時にエラーをクリア
    });

    return () => {
      // クリーンアップ
      socket.off('status_update');
      socket.off('error');
      socket.off('reconnected');
    };
  }, []);

  const handleToggle = async (switchNumber, desiredState) => {
    try {
      const response = await fetch(`/api/toggle?switch_${switchNumber}=${desiredState}`, { method: 'POST' });
      const data = await response.json();

      if (response.ok) {
        setStatus(data); // 成功時に新しいステータスを設定
      } else {
        setError(data.error || '不明なエラーが発生しました');
      }
    } catch {
      setError('ネットワークエラーが発生しました');
    }
  };

  const toggleTheme = () => setIsDarkMode(!isDarkMode);

  return (
    <div className={`app ${isDarkMode ? 'dark' : 'light'}`}>
      <div className="header">
        <h1>デバイス管理パネル</h1>
        <button onClick={toggleTheme} className="theme-toggle">
          {isDarkMode ? 'ライトモード' : 'ダークモード'}
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="status-container">
        {Object.keys(status).map((key) => (
          <div key={key} className="status-item">
            <span className="status-label">{key}: {status[key] ? 'ON' : 'OFF'}</span>
            <button 
              className={`toggle-button ${status[key] ? 'on' : 'off'}`} 
              onClick={() => handleToggle(key.split('_')[1], status[key] ? 0 : 1)}
            >
              {status[key] ? 'OFFにする' : 'ONにする'}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};

export default App;
