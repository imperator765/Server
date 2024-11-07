import React, { useState, useEffect } from 'react';
import io from 'socket.io-client';
import axios from 'axios';
import './App.css'; // CSSでデザインを調整

const socket = io('http://localhost:5000');

const App = () => {
  const [status, setStatus] = useState({
    Alpha: 0,
    Bravo: 0,
    Charlie: 0,
    Delta: 0,
  });
  const [error, setError] = useState('');
  const [isConnected, setIsConnected] = useState(true);
  const [isDarkMode, setIsDarkMode] = useState(true);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const response = await axios.get('/api/status');
        setStatus(response.data.data);
        setIsConnected(true);
        setError('');
      } catch (err) {
        setIsConnected(false);
        setError('デバイスに接続できません');
      }
    };

    fetchStatus();
    const interval = setInterval(fetchStatus, 10000);

    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    socket.on('status_update', (data) => {
      setStatus(data.data);
      setIsConnected(true);
      setError('');
    });

    socket.on('error', () => {
      setIsConnected(false);
      setError('接続が切断されました');
    });

    return () => {
      socket.off('status_update');
      socket.off('error');
    };
  }, []);

  const handleSwitch = async (switchName) => {
    try {
      const desiredState = status[switchName] === 0 ? 1 : 0;
      await axios.post(`/api/set_switch?switch=${switchName}:${desiredState}`);
      setError('');
    } catch (err) {
      setError(`スイッチ ${switchName} の操作中にエラーが発生しました`);
    }
  };

  const toggleDarkMode = () => {
    setIsDarkMode(!isDarkMode);
  };

  return (
    <div className={`app ${isDarkMode ? 'dark-mode' : 'light-mode'}`}>
      <header className="header">
        <h1>XD Switch</h1>
        <button className="theme-toggle" onClick={toggleDarkMode}>
          {isDarkMode ? 'ライトモード' : 'ダークモード'}
        </button>
        {error && <div className="error-bar">{error}</div>}
      </header>
      <div className={`controls ${!isConnected ? 'disabled' : ''}`}>
        {Object.keys(status).map((switchName) => (
          <div key={switchName} className="switch-container">
            <label className="switch-label">{switchName}</label>
            <button
              className={`switch-button ${status[switchName] ? 'on' : 'off'}`}
              onClick={() => handleSwitch(switchName)}
              disabled={!isConnected}
            >
              {status[switchName] ? 'ON' : 'OFF'}
            </button>
            <div
              className={`status-lamp ${status[switchName] ? 'lamp-on' : 'lamp-off'}`}
            />
          </div>
        ))}
      </div>
    </div>
  );
};

export default App;
