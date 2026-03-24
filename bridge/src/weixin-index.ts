#!/usr/bin/env node

import { homedir } from 'os';
import { join } from 'path';

import { WeixinBridgeServer } from './weixin.js';

const PORT = parseInt(process.env.BRIDGE_PORT || '3002', 10);
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.nanobot', 'weixin-auth');
const BASE_URL = process.env.WEIXIN_BASE_URL || 'https://ilinkai.weixin.qq.com';
const TOKEN = process.env.BRIDGE_TOKEN || undefined;
const LOGIN_MODE = process.env.WEIXIN_LOGIN === '1';
const CONFIG_PATH = process.env.WEIXIN_CONFIG_PATH || undefined;
const QR_PATH = process.env.WEIXIN_QR_PATH || join(homedir(), 'weixin-qr.png');
const NANOBOT_BIN = process.env.WEIXIN_NANOBOT_BIN || 'nanobot';
const GATEWAY_SERVICE = process.env.WEIXIN_GATEWAY_SERVICE || 'nanobot-gateway.service';

console.log('🐈 nanobot Weixin Bridge');
console.log('========================\n');

const server = new WeixinBridgeServer(
  PORT,
  AUTH_DIR,
  BASE_URL,
  LOGIN_MODE,
  TOKEN,
  CONFIG_PATH,
  QR_PATH,
  NANOBOT_BIN,
  GATEWAY_SERVICE,
);

process.on('SIGINT', async () => {
  console.log('\n\nShutting down...');
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

server.start().catch((error) => {
  console.error('Failed to start Weixin bridge:', error);
  process.exit(1);
});
