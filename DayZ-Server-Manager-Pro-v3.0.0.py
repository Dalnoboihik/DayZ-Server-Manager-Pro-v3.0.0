#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DayZ Server Manager Pro v3.0.0
Оптимизировано для сервера DayZ
"""

import socket
import struct
import threading
import time
import json
import os
import re
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Optional
from flask import Flask, render_template_string, request, jsonify
import secrets

# ================== Конфигурация сервера ==================
SERVER_IP = "12.11.22.33"
GAME_PORT = 2302
QUERY_PORT = 2303
RCON_PORT = 2910
RCON_PASSWORD = "1232"

VERSION = "3.0.0"

# ================== Классы данных ==================
@dataclass
class DayZServerInfo:
    """Информация о сервере DayZ"""
    name: str
    host: str
    game_port: int
    query_port: int
    rcon_port: int
    rcon_password: str
    last_update: Optional[datetime] = None
    players: List[Dict] = None
    server_status: Dict = None
    mods: List[str] = None
    online: bool = False
    
    def __post_init__(self):
        if self.players is None:
            self.players = []
        if self.mods is None:
            self.mods = []
        if self.server_status is None:
            self.server_status = {}

@dataclass
class BanInfo:
    """Информация о бане"""
    player_name: str
    steam_id: str
    ip: str
    reason: str
    admin: str
    timestamp: datetime
    server: str

# ================== ИСПРАВЛЕННЫЙ BattlEye RCON клиент ==================
class BattlEyeRCONClient:
    """
    Правильный RCON клиент для DayZ (BattlEye протокол)
    Порт RCON:(как на вашем сервере)
    """
    
    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.socket = None
        self.authenticated = False
        self.buffer = b""
        self.last_command_time = 0
        self.command_delay = 0.3  # BattlEye требует задержку

    def connect(self) -> bool:
        """Подключение к BattlEye RCON"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)
            self.socket.connect((self.host, self.port))
            
            # BattlEye логин: просто отправляем пароль
            self.socket.send(self.password.encode('utf-8') + b'\n')
            time.sleep(0.5)
            
            # Получаем приветствие
            response = self._receive_response()
            if response and ("rcon" in response.lower() or "welcome" in response.lower()):
                self.authenticated = True
                print(f"✅ BattlEye RCON connected to {self.host}:{self.port}")
                return True
            else:
                print(f"❌ RCON auth failed: {response}")
                return False
        except Exception as e:
            print(f"❌ RCON connection error: {e}")
            return False

    def send_command(self, command: str) -> Optional[str]:
        """Отправка команды в BattlEye"""
        if not self.authenticated:
            if not self.connect():
                return "Ошибка подключения к RCON"

        # Задержка между командами
        now = time.time()
        if now - self.last_command_time < self.command_delay:
            time.sleep(self.command_delay)
        self.last_command_time = now

        try:
            # BattlEye команды могут быть с # или без
            if not command.startswith('#') and command not in ['players', 'say']:
                cmd = command
            else:
                cmd = command
                
            self.socket.send(cmd.encode('utf-8') + b'\n')
            
            # BattlEye может отправлять несколько строк
            time.sleep(0.2)  # Даем время на ответ
            response = self._receive_response()
            
            return response if response else "✅ Команда выполнена"
            
        except Exception as e:
            print(f"❌ RCON command error: {e}")
            self.authenticated = False
            return f"Ошибка: {e}"

    def _receive_response(self) -> Optional[str]:
        """Получение ответа от BattlEye"""
        responses = []
        try:
            while True:
                try:
                    data = self.socket.recv(8192)
                    if not data:
                        break
                    
                    # Добавляем к буферу
                    self.buffer += data
                    
                    # Разбиваем на строки
                    lines = self.buffer.split(b'\n')
                    self.buffer = lines[-1]
                    
                    for line in lines[:-1]:
                        try:
                            decoded = line.decode('utf-8', errors='ignore').strip()
                            if decoded:
                                responses.append(decoded)
                        except:
                            pass
                except socket.timeout:
                    break
        except:
            pass
            
        return '\n'.join(responses) if responses else None

    def disconnect(self):
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        self.authenticated = False

# ================== ИСПРАВЛЕННЫЙ Query клиент ==================
class DayZQueryClient:
    """
    Клиент для Source Query (A2S)
    Порт Query: (как на вашем сервере)
    """
    
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.socket = None

    def get_server_info(self) -> Optional[Dict]:
        """Получение информации о сервере"""
        try:
            # A2S_INFO запрос
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.settimeout(3)
            
            # Формируем правильный запрос
            request = b'\xFF\xFF\xFF\xFF\x54Source Engine Query\x00'
            self.socket.sendto(request, (self.host, self.port))
            
            # Получаем ответ
            data, _ = self.socket.recvfrom(4096)
            
            # Проверяем заголовок (должен быть 0x49)
            if data[4] == 0x49:
                info = self._parse_info(data)
                
                # Получаем список игроков
                players = self._get_players()
                if players:
                    info['players_list'] = players
                
                return info
        except Exception as e:
            print(f"❌ Query error: {e}")
        finally:
            if self.socket:
                self.socket.close()
        
        return None

    def _get_players(self) -> List[Dict]:
        """Получение списка игроков через A2S_PLAYER"""
        try:
            # Сначала получаем challenge
            challenge_req = b'\xFF\xFF\xFF\xFF\x55\xFF\xFF\xFF\xFF'
            self.socket.sendto(challenge_req, (self.host, self.port))
            data, _ = self.socket.recvfrom(4096)
            
            if data[4] == 0x41:  # 'A' - ответ с challenge
                challenge = struct.unpack('<l', data[5:9])[0]
                
                # Запрос игроков с challenge
                player_req = struct.pack('<4sBl', b'\xFF\xFF\xFF\xFF', 0x55, challenge)
                self.socket.sendto(player_req, (self.host, self.port))
                data, _ = self.socket.recvfrom(8192)
                
                if data[4] == 0x44:  # 'D' - ответ с игроками
                    return self._parse_players(data)
        except:
            pass
        return []

    def _parse_info(self, data: bytes) -> Dict:
        """Парсинг A2S_INFO ответа"""
        info = {}
        offset = 5
        
        try:
            info['protocol'] = data[offset]
            offset += 1
            
            info['name'] = self._read_string(data, offset)
            offset += len(info['name']) + 1
            
            info['map'] = self._read_string(data, offset)
            offset += len(info['map']) + 1
            
            info['folder'] = self._read_string(data, offset)
            offset += len(info['folder']) + 1
            
            info['game'] = self._read_string(data, offset)
            offset += len(info['game']) + 1
            
            info['game_id'] = struct.unpack('<h', data[offset:offset+2])[0]
            offset += 2
            
            info['players'] = data[offset]
            offset += 1
            
            info['max_players'] = data[offset]
            offset += 1
            
            info['bots'] = data[offset]
            offset += 1
            
            info['server_type'] = chr(data[offset])
            offset += 1
            
            info['environment'] = chr(data[offset])
            offset += 1
            
            info['visibility'] = data[offset]
            offset += 1
            
            info['vac'] = data[offset]
            offset += 1
            
            if offset < len(data):
                info['version'] = self._read_string(data, offset)
                
        except Exception as e:
            print(f"Parse info error: {e}")
            
        return info

    def _parse_players(self, data: bytes) -> List[Dict]:
        """Парсинг A2S_PLAYER ответа"""
        players = []
        offset = 5
        
        try:
            count = data[offset]
            offset += 1
            
            for i in range(count):
                player = {}
                
                # Индекс игрока
                player['index'] = data[offset]
                offset += 1
                
                # Имя игрока
                player['name'] = self._read_string(data, offset)
                offset += len(player['name']) + 1
                
                # Счет (в DayZ это время игры в минутах)
                player['score'] = struct.unpack('<l', data[offset:offset+4])[0]
                offset += 4
                
                # Время подключения в секундах
                player['duration'] = struct.unpack('<f', data[offset:offset+4])[0]
                offset += 4
                
                players.append(player)
        except:
            pass
            
        return players

    def _read_string(self, data: bytes, offset: int) -> str:
        """Чтение строки с нулевым окончанием"""
        end = offset
        while end < len(data) and data[end] != 0:
            end += 1
        return data[offset:end].decode('utf-8', errors='ignore')

# ================== Менеджер серверов ==================
class DayZServerManager:
    """Управление серверами с автоочисткой"""
    
    def __init__(self):
        self.servers: Dict[str, DayZServerInfo] = {}
        self.rcon_clients: Dict[str, BattlEyeRCONClient] = {}
        self.bans: List[BanInfo] = []
        self.config_file = "dayz_servers.json"
        self.bans_file = "dayz_bans.json"
        self.lock = threading.Lock()
        self.load_servers()
        self.load_bans()
        
        # Запускаем автоочистку каждый час
        self.start_cleanup()

    def add_server(self, server: DayZServerInfo):
        """Добавление сервера"""
        with self.lock:
            self.servers[server.name] = server
            self.save_servers()

    def remove_server(self, name: str):
        """Удаление сервера"""
        with self.lock:
            if name in self.servers:
                if name in self.rcon_clients:
                    self.rcon_clients[name].disconnect()
                    del self.rcon_clients[name]
                del self.servers[name]
                self.save_servers()
                return True
        return False

    def cleanup_old_data(self):
        """Очистка старых данных (запускается автоматически)"""
        with self.lock:
            # Очищаем неактивные RCON соединения
            for name in list(self.rcon_clients.keys()):
                if name not in self.servers:
                    self.rcon_clients[name].disconnect()
                    del self.rcon_clients[name]
            
            # Очищаем старые баны (старше 30 дней)
            thirty_days_ago = datetime.now() - timedelta(days=30)
            self.bans = [b for b in self.bans if b.timestamp > thirty_days_ago]
            self.save_bans()
            
            print("🧹 Автоочистка выполнена")

    def start_cleanup(self):
        """Запуск периодической очистки"""
        def cleanup_loop():
            while True:
                time.sleep(3600)  # Каждый час
                self.cleanup_old_data()
        
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()

    def get_server(self, name: str) -> Optional[DayZServerInfo]:
        return self.servers.get(name)

    def get_all_servers(self) -> List[Dict]:
        """Возвращает список серверов"""
        result = []
        for s in self.servers.values():
            data = {
                'name': s.name,
                'host': s.host,
                'game_port': s.game_port,
                'query_port': s.query_port,
                'rcon_port': s.rcon_port,
                'online': s.online,
                'players': s.players,
                'server_status': s.server_status,
                'mods': s.mods,
                'last_update': s.last_update.isoformat() if s.last_update else None
            }
            result.append(data)
        return result

    def update_server_info(self, name: str) -> Optional[Dict]:
        """Обновление информации о сервере"""
        server = self.get_server(name)
        if not server:
            return None

        # Query запрос
        query = DayZQueryClient(server.host, server.query_port)
        info = query.get_server_info()

        with self.lock:
            if info:
                server.players = info.get('players_list', [])
                server.server_status = info
                server.last_update = datetime.now()
                server.online = True
                
                # Пробуем получить Steam ID через RCON
                if server.rcon_password:
                    try:
                        rcon = self._get_rcon_client(name)
                        if rcon:
                            players_detail = rcon.send_command('players')
                            if players_detail:
                                self._parse_player_details(server, players_detail)
                    except:
                        pass
            else:
                server.online = False

        return info

    def _get_rcon_client(self, server_name: str) -> Optional[BattlEyeRCONClient]:
        """Получение RCON клиента"""
        server = self.get_server(server_name)
        if not server or not server.rcon_password:
            return None

        if server_name not in self.rcon_clients:
            self.rcon_clients[server_name] = BattlEyeRCONClient(
                server.host, server.rcon_port, server.rcon_password
            )
        return self.rcon_clients[server_name]

    def _parse_player_details(self, server: DayZServerInfo, players_output: str):
        """Парсинг детальной информации об игроках из команды 'players'"""
        try:
            lines = players_output.strip().split('\n')
            for line in lines:
                # BattlEye формат: "0 76561198012345678 192.168.1.100:2304 45 120 PlayerName"
                parts = line.strip().split()
                if len(parts) >= 6:
                    steam_id = parts[1]
                    ip_port = parts[2]
                    ping = parts[3]
                    score = parts[4]
                    name = ' '.join(parts[5:])
                    
                    ip = ip_port.split(':')[0] if ':' in ip_port else ip_port
                    
                    # Ищем игрока и обновляем информацию
                    for player in server.players:
                        if player['name'].lower() == name.lower():
                            player['steam_id'] = steam_id
                            player['ip'] = ip
                            player['ping'] = int(ping) if ping.isdigit() else 0
                            player['battleye_score'] = int(score) if score.isdigit() else 0
                            break
        except:
            pass

    def send_rcon_command(self, server_name: str, command: str) -> Optional[str]:
        """Отправка RCON команды"""
        client = self._get_rcon_client(server_name)
        if not client:
            return "❌ Ошибка: RCON не настроен или неверный пароль"
        
        return client.send_command(command)

    def kick_player(self, server_name: str, player_name: str, reason: str = "") -> Optional[str]:
        """Кик игрока через BattlEye"""
        if reason:
            command = f"kick {player_name} {reason}"
        else:
            command = f"kick {player_name}"
        return self.send_rcon_command(server_name, command)

    def ban_player(self, server_name: str, player_name: str, reason: str = "", duration: int = 0) -> Optional[str]:
        """Бан игрока через BattlEye"""
        if duration > 0:
            command = f"ban {player_name} {duration} {reason}"
        else:
            command = f"ban {player_name} {reason}"
        
        result = self.send_rcon_command(server_name, command)
        
        # Сохраняем в список банов
        if result and "banned" in result.lower():
            server = self.get_server(server_name)
            player_info = None
            for p in server.players:
                if p['name'].lower() == player_name.lower():
                    player_info = p
                    break
            
            ban = BanInfo(
                player_name=player_name,
                steam_id=player_info.get('steam_id', '') if player_info else '',
                ip=player_info.get('ip', '') if player_info else '',
                reason=reason,
                admin="WebInterface",
                timestamp=datetime.now(),
                server=server_name
            )
            with self.lock:
                self.bans.append(ban)
                self.save_bans()
        
        return result

    def send_message(self, server_name: str, message: str, target: str = "-1") -> Optional[str]:
        """Отправка сообщения (всем или конкретному игроку)"""
        command = f"say {target} {message}"
        return self.send_rcon_command(server_name, command)

    def save_servers(self):
        """Сохранение серверов"""
        try:
            data = []
            for s in self.servers.values():
                data.append({
                    'name': s.name,
                    'host': s.host,
                    'game_port': s.game_port,
                    'query_port': s.query_port,
                    'rcon_port': s.rcon_port,
                    'rcon_password': s.rcon_password
                })
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"❌ Error saving servers: {e}")

    def load_servers(self):
        """Загрузка серверов"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        server = DayZServerInfo(
                            name=item['name'],
                            host=item['host'],
                            game_port=item.get('game_port', 2302),
                            query_port=item.get('query_port', 2303),
                            rcon_port=item.get('rcon_port', 2910),
                            rcon_password=item.get('rcon_password', '')
                        )
                        self.servers[server.name] = server
                print(f"✅ Загружено {len(self.servers)} серверов")
            except Exception as e:
                print(f"❌ Error loading servers: {e}")
        
        # Если нет серверов, добавляем тестовый с вашими параметрами
        if not self.servers:
            test_server = DayZServerInfo(
                name="Мой DayZ сервер",
                host=SERVER_IP,
                game_port=GAME_PORT,
                query_port=QUERY_PORT,
                rcon_port=RCON_PORT,
                rcon_password=RCON_PASSWORD
            )
            self.servers[test_server.name] = test_server
            self.save_servers()
            print(f"✅ Добавлен тестовый сервер: {SERVER_IP}")

    def save_bans(self):
        """Сохранение банов"""
        try:
            data = []
            for ban in self.bans:
                data.append({
                    'player_name': ban.player_name,
                    'steam_id': ban.steam_id,
                    'ip': ban.ip,
                    'reason': ban.reason,
                    'admin': ban.admin,
                    'timestamp': ban.timestamp.isoformat(),
                    'server': ban.server
                })
            with open(self.bans_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"❌ Error saving bans: {e}")

    def load_bans(self):
        """Загрузка банов"""
        if os.path.exists(self.bans_file):
            try:
                with open(self.bans_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        ban = BanInfo(
                            player_name=item['player_name'],
                            steam_id=item.get('steam_id', ''),
                            ip=item.get('ip', ''),
                            reason=item['reason'],
                            admin=item['admin'],
                            timestamp=datetime.fromisoformat(item['timestamp']),
                            server=item['server']
                        )
                        self.bans.append(ban)
            except Exception as e:
                print(f"❌ Error loading bans: {e}")

# ================== Flask веб-интерфейс ==================
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
manager = DayZServerManager()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DayZ Server Manager Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #0a0a0a;
            color: #e0e0e0;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        
        h1 { 
            color: #8B0000; 
            margin-bottom: 30px; 
            border-bottom: 2px solid #8B0000; 
            padding-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .server-card {
            background: #1a1a1a;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            border-left: 4px solid #8B0000;
            box-shadow: 0 4px 6px rgba(0,0,0,0.5);
        }
        
        .server-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #333;
        }
        
        .server-name {
            font-size: 1.4em;
            font-weight: bold;
            color: #8B0000;
        }
        
        .server-ip {
            color: #888;
            font-size: 0.9em;
            margin-left: 10px;
        }
        
        .status-badge {
            padding: 4px 12px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 0.9em;
        }
        
        .online { background: #2e7d32; color: white; }
        .offline { background: #b71c1c; color: white; }
        
        .info-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .info-item {
            background: #2a2a2a;
            padding: 12px;
            border-radius: 6px;
        }
        
        .info-label {
            color: #888;
            font-size: 0.85em;
            margin-bottom: 5px;
        }
        
        .info-value {
            font-size: 1.3em;
            font-weight: bold;
            color: #8B0000;
        }
        
        .players-table {
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
            background: #2a2a2a;
            border-radius: 6px;
            overflow: hidden;
        }
        
        .players-table th {
            background: #8B0000;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
        }
        
        .players-table td {
            padding: 10px 12px;
            border-bottom: 1px solid #3a3a3a;
        }
        
        .players-table tr:hover {
            background: #3a3a3a;
        }
        
        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9em;
            margin: 2px;
            transition: all 0.2s;
            font-weight: 500;
        }
        
        .btn-sm { padding: 4px 8px; font-size: 0.85em; }
        
        .btn-primary { background: #8B0000; color: white; }
        .btn-primary:hover { background: #a00000; transform: translateY(-1px); }
        
        .btn-secondary { background: #4a4a4a; color: white; }
        .btn-secondary:hover { background: #5a5a5a; }
        
        .btn-danger { background: #b71c1c; color: white; }
        .btn-danger:hover { background: #c62828; }
        
        .btn-success { background: #2e7d32; color: white; }
        .btn-success:hover { background: #3a8b3e; }
        
        .btn-warning { background: #ff6f00; color: white; }
        .btn-warning:hover { background: #ff8f00; }
        
        .command-section {
            margin-top: 20px;
            padding-top: 20px;
            border-top: 1px solid #333;
        }
        
        .command-input {
            width: 100%;
            padding: 12px;
            background: #2a2a2a;
            border: 1px solid #444;
            color: white;
            border-radius: 6px;
            margin-bottom: 10px;
            font-family: monospace;
        }
        
        .command-input:focus {
            outline: none;
            border-color: #8B0000;
        }
        
        .quick-commands {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin: 15px 0;
        }
        
        .messages {
            background: #0a0a0a;
            border-radius: 6px;
            padding: 15px;
            max-height: 200px;
            overflow-y: auto;
            font-family: monospace;
            margin-top: 15px;
            border: 1px solid #333;
        }
        
        .message {
            padding: 4px 0;
            border-bottom: 1px solid #222;
            color: #0f0;
            font-size: 0.9em;
        }
        
        .message.error { color: #ff6b6b; }
        
        .add-form {
            background: #1a1a1a;
            border-radius: 8px;
            padding: 25px;
            margin-top: 30px;
            border-left: 4px solid #8B0000;
        }
        
        .form-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }
        
        .form-grid input {
            padding: 10px;
            background: #2a2a2a;
            border: 1px solid #444;
            color: white;
            border-radius: 4px;
        }
        
        .form-grid input:focus {
            outline: none;
            border-color: #8B0000;
        }
        
        .delete-btn {
            background: none;
            border: none;
            color: #b71c1c;
            font-size: 1.2em;
            cursor: pointer;
            margin-left: 10px;
            padding: 5px 10px;
        }
        
        .delete-btn:hover {
            color: #ff0000;
            transform: scale(1.1);
        }
        
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        
        .tab {
            padding: 10px 20px;
            cursor: pointer;
            border-radius: 6px;
            background: #2a2a2a;
        }
        
        .tab.active {
            background: #8B0000;
            color: white;
        }
        
        .hidden { display: none; }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .stat-card {
            background: #2a2a2a;
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }
        
        .stat-value {
            font-size: 2em;
            font-weight: bold;
            color: #8B0000;
        }
        
        .stat-label {
            color: #888;
            margin-top: 5px;
        }
        
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        
        .loading {
            animation: pulse 1.5s infinite;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            🎮 DayZ Server Manager Pro v3.0.0
            <span style="font-size: 14px; color: #888;">Gorbunovski v3.0.0</span>
        </h1>
        
        <!-- Вкладки -->
        <div class="tabs">
            <div class="tab active" onclick="switchTab('servers')">📋 Серверы</div>
            <div class="tab" onclick="switchTab('bans')">🔨 Баны</div>
            <div class="tab" onclick="switchTab('stats')">📊 Статистика</div>
        </div>
        
        <!-- Вкладка Серверы -->
        <div id="tab-servers">
            <div id="servers"></div>
            
            <!-- Форма добавления сервера -->
            <div class="add-form">
                <h3 style="color: #8B0000; margin-bottom: 15px;">➕ Добавить сервер</h3>
                <div class="form-grid">
                    <input type="text" id="new-name" placeholder="Название" value="DayZ Server">
                    <input type="text" id="new-host" placeholder="IP адрес" value="5.42.211.136">
                    <input type="number" id="new-game-port" placeholder="Game порт" value="2302">
                    <input type="number" id="new-query-port" placeholder="Query порт" value="2303">
                    <input type="number" id="new-rcon-port" placeholder="RCON порт" value="2910">
                    <input type="text" id="new-rcon-password" placeholder="RCON пароль" value="3xby13xby1">
                </div>
                <button class="btn btn-success" onclick="addServer()">💾 Сохранить сервер</button>
            </div>
        </div>
        
        <!-- Вкладка Баны -->
        <div id="tab-bans" class="hidden">
            <div class="server-card">
                <h3 style="color: #8B0000; margin-bottom: 15px;">🔨 Список банов</h3>
                <div id="bans-list"></div>
            </div>
        </div>
        
        <!-- Вкладка Статистика -->
        <div id="tab-stats" class="hidden">
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value" id="stat-servers">0</div>
                    <div class="stat-label">Серверов</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="stat-players">0</div>
                    <div class="stat-label">Игроков онлайн</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="stat-bans">0</div>
                    <div class="stat-label">Всего банов</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="stat-uptime">-</div>
                    <div class="stat-label">Время работы</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let startTime = Date.now();
        
        function switchTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            
            document.getElementById('tab-servers').classList.add('hidden');
            document.getElementById('tab-bans').classList.add('hidden');
            document.getElementById('tab-stats').classList.add('hidden');
            
            document.getElementById(`tab-${tab}`).classList.remove('hidden');
            
            if (tab === 'bans') loadBans();
            if (tab === 'stats') updateStats();
        }
        
        function loadServers() {
            fetch('/api/servers')
                .then(res => res.json())
                .then(servers => {
                    const container = document.getElementById('servers');
                    container.innerHTML = '';
                    
                    servers.forEach(server => {
                        const statusClass = server.online ? 'online' : 'offline';
                        const statusText = server.online ? '🟢 ONLINE' : '🔴 OFFLINE';
                        const players = server.players || [];
                        
                        let playersHtml = '';
                        if (players.length > 0) {
                            playersHtml = `
                                <table class="players-table">
                                    <tr>
                                        <th>Игрок</th>
                                        <th>Steam ID</th>
                                        <th>IP</th>
                                        <th>Пинг</th>
                                        <th>Время</th>
                                        <th>Счет</th>
                                        <th>Действия</th>
                                    </tr>
                                    ${players.map(p => `
                                        <tr>
                                            <td><strong>${escapeHtml(p.name) || 'Неизвестно'}</strong></td>
                                            <td>${p.steam_id || '...'}</td>
                                            <td>${p.ip || '...'}</td>
                                            <td>${p.ping || '?'} ms</td>
                                            <td>${formatDuration(p.duration)}</td>
                                            <td>${p.score || 0}</td>
                                            <td>
                                                <button class="btn btn-sm btn-secondary" onclick="sendMessage('${server.name}', '${escapeHtml(p.name)}')" title="Личное сообщение">💬</button>
                                                <button class="btn btn-sm btn-warning" onclick="kickPlayer('${server.name}', '${escapeHtml(p.name)}')" title="Кик">⛔</button>
                                                <button class="btn btn-sm btn-danger" onclick="banPlayer('${server.name}', '${escapeHtml(p.name)}')" title="Бан">🔨</button>
                                            </td>
                                        </tr>
                                    `).join('')}
                                </table>
                            `;
                        } else {
                            playersHtml = '<p style="text-align: center; padding: 20px; color: #888;">👥 Нет игроков онлайн</p>';
                        }
                        
                        const serverHtml = `
                            <div class="server-card">
                                <div class="server-header">
                                    <div>
                                        <span class="server-name">${escapeHtml(server.name)}</span>
                                        <span class="server-ip">${escapeHtml(server.host)}:${server.game_port}</span>
                                    </div>
                                    <div>
                                        <span class="status-badge ${statusClass}">${statusText}</span>
                                        <button class="delete-btn" onclick="deleteServer('${server.name}')" title="Удалить сервер">🗑️</button>
                                    </div>
                                </div>
                                
                                <div class="info-grid">
                                    <div class="info-item">
                                        <div class="info-label">🗺️ Карта</div>
                                        <div class="info-value">${escapeHtml(server.server_status?.map) || 'N/A'}</div>
                                    </div>
                                    <div class="info-item">
                                        <div class="info-label">👥 Игроки</div>
                                        <div class="info-value">${server.server_status?.players || 0}/${server.server_status?.max_players || 0}</div>
                                    </div>
                                    <div class="info-item">
                                        <div class="info-label">🎮 Игра</div>
                                        <div class="info-value">${escapeHtml(server.server_status?.game) || 'DayZ'}</div>
                                    </div>
                                    <div class="info-item">
                                        <div class="info-label">📦 Версия</div>
                                        <div class="info-value">${escapeHtml(server.server_status?.version) || 'N/A'}</div>
                                    </div>
                                </div>
                                
                                <h3 style="margin: 20px 0 10px 0; color: #8B0000;">👥 Игроки онлайн (${players.length})</h3>
                                ${playersHtml}
                                
                                <div class="command-section">
                                    <input type="text" id="cmd-${server.name}" class="command-input" placeholder="Введите команду (например: players, #lock, say -1 Привет)" onkeypress="handleKeyPress(event, '${server.name}')">
                                    <button class="btn btn-primary" onclick="sendCommand('${server.name}')">📨 Отправить</button>
                                    
                                    <div class="quick-commands">
                                        <button class="btn btn-sm btn-secondary" onclick="quickCommand('${server.name}', 'players')">👥 Игроки</button>
                                        <button class="btn btn-sm btn-secondary" onclick="quickCommand('${server.name}', '#get time')">⏰ Время</button>
                                        <button class="btn btn-sm btn-secondary" onclick="quickCommand('${server.name}', '#lock')">🔒 Закрыть</button>
                                        <button class="btn btn-sm btn-secondary" onclick="quickCommand('${server.name}', '#unlock')">🔓 Открыть</button>
                                        <button class="btn btn-sm btn-success" onclick="quickCommand('${server.name}', 'save')">💾 Сохранить</button>
                                        <button class="btn btn-sm btn-danger" onclick="quickCommand('${server.name}', '#restart')">🔄 Рестарт</button>
                                        <button class="btn btn-sm btn-primary" onclick="refreshServer('${server.name}')">🔄 Обновить</button>
                                    </div>
                                    
                                    <div id="messages-${server.name}" class="messages"></div>
                                </div>
                            </div>
                        `;
                        
                        container.innerHTML += serverHtml;
                    });
                    
                    updateStats();
                });
        }
        
        function loadBans() {
            fetch('/api/bans')
                .then(res => res.json())
                .then(bans => {
                    const container = document.getElementById('bans-list');
                    if (bans.length === 0) {
                        container.innerHTML = '<p style="text-align: center; padding: 20px;">🔨 Нет активных банов</p>';
                        return;
                    }
                    
                    let html = '<table class="players-table">';
                    html += '<tr><th>Игрок</th><th>Steam ID</th><th>IP</th><th>Причина</th><th>Сервер</th><th>Дата</th></tr>';
                    
                    bans.forEach(ban => {
                        html += `
                            <tr>
                                <td><strong>${escapeHtml(ban.player_name)}</strong></td>
                                <td>${ban.steam_id || 'N/A'}</td>
                                <td>${ban.ip || 'N/A'}</td>
                                <td>${escapeHtml(ban.reason)}</td>
                                <td>${ban.server}</td>
                                <td>${new Date(ban.timestamp).toLocaleString()}</td>
                            </tr>
                        `;
                    });
                    
                    html += '</table>';
                    container.innerHTML = html;
                });
        }
        
        function updateStats() {
            fetch('/api/servers')
                .then(res => res.json())
                .then(servers => {
                    document.getElementById('stat-servers').textContent = servers.length;
                    
                    let totalPlayers = 0;
                    servers.forEach(s => {
                        totalPlayers += (s.players || []).length;
                    });
                    document.getElementById('stat-players').textContent = totalPlayers;
                    
                    const uptime = Math.floor((Date.now() - startTime) / 1000);
                    const hours = Math.floor(uptime / 3600);
                    const minutes = Math.floor((uptime % 3600) / 60);
                    document.getElementById('stat-uptime').textContent = `${hours}ч ${minutes}м`;
                });
            
            fetch('/api/bans')
                .then(res => res.json())
                .then(bans => {
                    document.getElementById('stat-bans').textContent = bans.length;
                });
        }
        
        function escapeHtml(text) {
            if (!text) return '';
            return String(text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }
        
        function formatDuration(seconds) {
            if (!seconds) return '0м';
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            return h > 0 ? `${h}ч ${m}м` : `${m}м`;
        }
        
        function handleKeyPress(event, serverName) {
            if (event.key === 'Enter') {
                sendCommand(serverName);
            }
        }
        
        function addMessage(serverName, text, isError = false) {
            const msgArea = document.getElementById(`messages-${serverName}`);
            if (msgArea) {
                const msgDiv = document.createElement('div');
                msgDiv.className = `message ${isError ? 'error' : ''}`;
                msgDiv.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
                msgArea.appendChild(msgDiv);
                msgArea.scrollTop = msgArea.scrollHeight;
            }
        }
        
        function sendCommand(serverName) {
            const input = document.getElementById(`cmd-${serverName}`);
            const command = input.value.trim();
            if (!command) return;
            
            addMessage(serverName, `> ${command}`);
            
            fetch('/api/command', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({server: serverName, command: command})
            })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    addMessage(serverName, data.result || '✅ Команда выполнена');
                } else {
                    addMessage(serverName, `❌ ${data.error}`, true);
                }
                input.value = '';
            })
            .catch(err => {
                addMessage(serverName, `❌ Ошибка соединения`, true);
            });
        }
        
        function quickCommand(serverName, command) {
            document.getElementById(`cmd-${serverName}`).value = command;
            sendCommand(serverName);
        }
        
        function refreshServer(serverName) {
            addMessage(serverName, '🔄 Обновление...');
            fetch(`/api/refresh/${encodeURIComponent(serverName)}`)
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        loadServers();
                        addMessage(serverName, '✅ Информация обновлена');
                    } else {
                        addMessage(serverName, '❌ Ошибка обновления', true);
                    }
                });
        }
        
        function deleteServer(serverName) {
            if (confirm(`Удалить сервер ${serverName}?`)) {
                fetch(`/api/servers/${encodeURIComponent(serverName)}`, {
                    method: 'DELETE'
                })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        loadServers();
                    }
                });
            }
        }
        
        function sendMessage(serverName, playerName) {
            const message = prompt(`Сообщение для ${playerName}:`);
            if (message) {
                fetch('/api/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        server: serverName,
                        target: playerName,
                        message: message
                    })
                })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        addMessage(serverName, `💬 Сообщение для ${playerName}: "${message}"`);
                    } else {
                        alert('Ошибка при отправке');
                    }
                });
            }
        }
        
        function kickPlayer(serverName, playerName) {
            const reason = prompt(`Причина кика для ${playerName}:`, 'Нарушение правил');
            if (reason !== null) {
                fetch('/api/kick', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        server: serverName,
                        player: playerName,
                        reason: reason
                    })
                })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        addMessage(serverName, `⛔ Игрок ${playerName} кикнут (${reason})`);
                        refreshServer(serverName);
                    } else {
                        alert('Ошибка при кике');
                    }
                });
            }
        }
        
        function banPlayer(serverName, playerName) {
            const reason = prompt(`Причина бана для ${playerName}:`, 'Нарушение правил');
            if (reason !== null) {
                const duration = prompt('Длительность бана (0 - перманентно, 60 - 1 час, 1440 - 1 день):', '0');
                fetch('/api/ban', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        server: serverName,
                        player: playerName,
                        reason: reason,
                        duration: parseInt(duration) || 0
                    })
                })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        addMessage(serverName, `🔨 Игрок ${playerName} забанен (${reason})`);
                        refreshServer(serverName);
                    } else {
                        alert('Ошибка при бане');
                    }
                });
            }
        }
        
        function addServer() {
            const name = document.getElementById('new-name').value.trim();
            const host = document.getElementById('new-host').value.trim();
            const gamePort = parseInt(document.getElementById('new-game-port').value);
            const queryPort = parseInt(document.getElementById('new-query-port').value);
            const rconPort = parseInt(document.getElementById('new-rcon-port').value);
            const rconPassword = document.getElementById('new-rcon-password').value;
            
            if (!name || !host) {
                alert('Заполните название и хост');
                return;
            }
            
            fetch('/api/servers', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    name: name,
                    host: host,
                    game_port: gamePort,
                    query_port: queryPort,
                    rcon_port: rconPort,
                    rcon_password: rconPassword
                })
            })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    loadServers();
                } else {
                    alert('Ошибка: ' + data.error);
                }
            });
        }
        
        // Автообновление каждые 15 секунд
        setInterval(() => {
            loadServers();
            updateStats();
        }, 15000);
        
        window.onload = () => {
            loadServers();
            loadBans();
            updateStats();
        };
    </script>
</body>
</html>
"""

# ================== Flask маршруты ==================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/servers', methods=['GET'])
def get_servers():
    return jsonify(manager.get_all_servers())

@app.route('/api/servers', methods=['POST'])
def add_server():
    data = request.json
    try:
        server = DayZServerInfo(
            name=data['name'],
            host=data['host'],
            game_port=int(data.get('game_port', 2302)),
            query_port=int(data.get('query_port', 2303)),
            rcon_port=int(data.get('rcon_port', 2910)),
            rcon_password=data.get('rcon_password', '')
        )
        manager.add_server(server)
        threading.Thread(target=manager.update_server_info, args=(server.name,)).start()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/servers/<name>', methods=['DELETE'])
def delete_server(name):
    try:
        success = manager.remove_server(name)
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/refresh/<name>', methods=['GET'])
def refresh_server(name):
    try:
        info = manager.update_server_info(name)
        return jsonify({'success': True, 'info': info})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/command', methods=['POST'])
def command():
    data = request.json
    try:
        result = manager.send_rcon_command(data['server'], data['command'])
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/kick', methods=['POST'])
def kick():
    data = request.json
    try:
        result = manager.kick_player(data['server'], data['player'], data.get('reason', ''))
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/ban', methods=['POST'])
def ban():
    data = request.json
    try:
        result = manager.ban_player(
            data['server'], 
            data['player'], 
            data.get('reason', 'Нарушение правил'),
            data.get('duration', 0)
        )
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/message', methods=['POST'])
def message():
    data = request.json
    try:
        result = manager.send_message(data['server'], data['message'], data.get('target', '-1'))
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/bans', methods=['GET'])
def get_bans():
    bans = []
    for ban in manager.bans:
        bans.append({
            'player_name': ban.player_name,
            'steam_id': ban.steam_id,
            'ip': ban.ip,
            'reason': ban.reason,
            'admin': ban.admin,
            'timestamp': ban.timestamp.isoformat(),
            'server': ban.server
        })
    return jsonify(bans)

# Фоновое обновление
def background_updater():
    while True:
        try:
            for server_name in list(manager.servers.keys()):
                try:
                    manager.update_server_info(server_name)
                except:
                    pass
            time.sleep(15)
        except:
            time.sleep(15)

if __name__ == '__main__':
    print("=" * 60)
    print("  DayZ Server Manager Pro v3.0.0")
    print("  Оптимизировано для сервера: Ваш сервер")
    print("=" * 60)
    print("  Параметры подключения:")
    print(f"  • Game порт: Ваши данные")
    print(f"  • Query порт: Ваши данные")
    print(f"  • RCON порт: Ваши данные")
    print(f"  • RCON пароль: Ваши данные")
    print("=" * 60)
    print("  Доступ по адресам:")
    print("  • Локально: http://127.0.0.1:5000")
    print(f"  • В сети: http://{socket.gethostbyname(socket.gethostname())}:5000")
    print("=" * 60)
    print("  Команды DayZ:")
    print("  • players           - список игроков с деталями")
    print("  • say -1 [текст]    - глобальное сообщение")
    print("  • say [игрок] [текст] - личное сообщение")
    print("  • kick [игрок]      - кикнуть игрока")
    print("  • ban [игрок]       - забанить игрока")
    print("  • #lock             - закрыть сервер")
    print("  • #unlock           - открыть сервер")
    print("  • #set time [время] - установить время")
    print("  • #restart          - перезапустить сервер")
    print("=" * 60)
    
    # Запускаем фоновое обновление
    updater = threading.Thread(target=background_updater, daemon=True)
    updater.start()
    
    # Запускаем Flask
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
