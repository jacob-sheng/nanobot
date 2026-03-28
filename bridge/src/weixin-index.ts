#!/usr/bin/env node

import { existsSync, readFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

import { WeixinBridgeServer } from './weixin.js';

function loadRouteTag(configPath: string): string | undefined {
  if (!existsSync(configPath)) return undefined;
  try {
    const raw = JSON.parse(readFileSync(configPath, 'utf-8')) as {
      channels?: { weixin?: { routeTag?: string | number } };
    };
    const routeTag = raw.channels?.weixin?.routeTag;
    if (routeTag === undefined || routeTag === null) return undefined;
    const normalized = String(routeTag).trim();
    return normalized || undefined;
  } catch {
    return undefined;
  }
}

const PORT = parseInt(process.env.BRIDGE_PORT || '3002', 10);
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.nanobot', 'weixin-auth');
const BASE_URL = process.env.WEIXIN_BASE_URL || 'https://ilinkai.weixin.qq.com';
const TOKEN = process.env.BRIDGE_TOKEN || undefined;
const LOGIN_MODE = process.env.WEIXIN_LOGIN === '1';
const CONFIG_PATH = process.env.WEIXIN_CONFIG_PATH || join(homedir(), '.nanobot', 'config.json');
const ROUTE_TAG = process.env.WEIXIN_ROUTE_TAG || loadRouteTag(CONFIG_PATH);
const QR_PATH = process.env.WEIXIN_QR_PATH || join(homedir(), 'weixin-qr.png');
const NANOBOT_BIN = process.env.WEIXIN_NANOBOT_BIN || 'nanobot';
const GATEWAY_SERVICE = process.env.WEIXIN_GATEWAY_SERVICE || 'nanobot-gateway.service';

function log(level: string, event: string, fields?: Record<string, unknown>): void {
  const suffix = fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : '';
  console.log(`${level} ${event}${suffix}`);
}

log('INFO', 'bridge.process_start', { port: PORT, authDir: AUTH_DIR, loginMode: LOGIN_MODE, configPath: CONFIG_PATH, routeTagEnabled: Boolean(ROUTE_TAG) });

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
  ROUTE_TAG,
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
