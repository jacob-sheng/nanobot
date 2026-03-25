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

function log(level: string, event: string, fields?: Record<string, unknown>): void {
  const suffix = fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : '';
  console.log(`${level} ${event}${suffix}`);
}

log('INFO', 'bridge.process_start', { port: PORT, authDir: AUTH_DIR, loginMode: LOGIN_MODE });

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
  log('INFO', 'bridge.signal', { signal: 'SIGINT' });
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  log('INFO', 'bridge.signal', { signal: 'SIGTERM' });
  await server.stop();
  process.exit(0);
});

process.on('unhandledRejection', (reason) => {
  log('ERROR', 'bridge.unhandled_rejection', { reason: String(reason) });
  process.exit(1);
});

process.on('uncaughtException', (error) => {
  log('ERROR', 'bridge.uncaught_exception', {
    name: error.name,
    message: error.message,
  });
  process.exit(1);
});

server.start().catch((error) => {
  log('ERROR', 'bridge.start_failed', {
    name: error instanceof Error ? error.name : 'Error',
    message: error instanceof Error ? error.message : String(error),
  });
  process.exit(1);
});
