import { spawnSync } from 'child_process';
import { mkdir, writeFile } from 'fs/promises';
import { homedir } from 'os';
import { basename, extname, join } from 'path';
import qrcode from 'qrcode-terminal';
import QRCode from 'qrcode';
import { WebSocketServer, WebSocket } from 'ws';

import { WeixinAccountStore, SavedWeixinAccount } from './weixin-auth.js';
import { downloadMedia, fetchQrCode, getUpdates, pollQrStatus, sendMessage, WeixinMessage, WeixinMessageItem } from './weixin-api.js';

interface BridgeEvents {
  onMessage: (msg: InboundWeixinMessage) => void;
  onQR: (accountId: string | null, qr: string) => void;
  onStatus: (status: string, detail?: string, accountId?: string) => void;
}

export interface InboundWeixinMessage {
  id: string;
  accountId: string;
  sender: string;
  content: string;
  timestamp: number;
  contextToken?: string;
  media?: string[];
}

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

interface BridgeHeartbeat {
  type: 'heartbeat';
  timestamp: number;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractText(itemList?: WeixinMessageItem[]): string {
  if (!itemList?.length) return '';
  for (const item of itemList) {
    if (item.type === 1 && item.text_item?.text) return item.text_item.text;
    if (item.type === 3 && item.voice_item?.text) return item.voice_item.text;
  }
  return '';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function sanitizePathPart(value: string): string {
  return value.replace(/[^a-zA-Z0-9_.-]+/g, '_').slice(0, 80) || 'msg';
}

function looksLikeImageCandidate(url: string, keyPath: string): boolean {
  const path = keyPath.toLowerCase();
  if (/(^|\.)(image|img|pic|photo|thumb|cdn|download|url)(\.|$)/.test(path)) return true;
  try {
    const ext = extname(new URL(url).pathname).toLowerCase();
    return ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic'].includes(ext);
  } catch {
    return false;
  }
}

function collectUrlCandidates(
  value: unknown,
  keyPath = '',
  out: Array<{ url: string; keyPath: string }> = [],
): Array<{ url: string; keyPath: string }> {
  if (typeof value === 'string') {
    if (/^https?:\/\//i.test(value)) {
      out.push({ url: value, keyPath });
    }
    return out;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => collectUrlCandidates(item, `${keyPath}[${index}]`, out));
    return out;
  }
  if (!isRecord(value)) return out;
  for (const [key, nested] of Object.entries(value)) {
    const nextPath = keyPath ? `${keyPath}.${key}` : key;
    collectUrlCandidates(nested, nextPath, out);
  }
  return out;
}

function guessImageExtension(url: string, contentType: string | null): string {
  const content = (contentType || '').toLowerCase().split(';', 1)[0];
  const typeMap: Record<string, string> = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/bmp': '.bmp',
    'image/heic': '.heic',
  };
  if (content in typeMap) return typeMap[content];
  try {
    const ext = extname(new URL(url).pathname).toLowerCase();
    if (ext) return ext;
  } catch {
    // Ignore malformed URL path, fall back below.
  }
  return '.jpg';
}

export class WeixinBridgeServer {
  private static readonly HEARTBEAT_INTERVAL_MS = 15000;
  private wss: WebSocketServer | null = null;
  private clients = new Set<WebSocket>();
  private store: WeixinAccountStore;
  private accounts = new Map<string, SavedWeixinAccount>();
  private contextTokens = new Map<string, string>();
  private monitorTasks = new Map<string, Promise<void>>();
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private stopped = false;

  constructor(
    private readonly port: number,
    authDir: string,
    private readonly baseUrl: string,
    private readonly loginMode: boolean,
    private readonly token?: string,
    private readonly configPath?: string,
    private readonly qrPath?: string,
    private readonly nanobotBin?: string,
    private readonly gatewayService?: string,
  ) {
    this.store = new WeixinAccountStore(authDir);
  }

  async start(): Promise<void> {
    await this.store.init();
    this.wss = new WebSocketServer({ host: '127.0.0.1', port: this.port });
    this.logInfo('bridge.start', { url: `ws://127.0.0.1:${this.port}` });
    if (this.token) this.logInfo('bridge.auth_enabled');
    this.startHeartbeatLoop();

    this.wss.on('connection', (ws) => {
      this.logInfo('bridge.client_connecting', { remote: this.describeRemote(ws) });
      if (this.token) {
        const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000);
        ws.once('message', (data) => {
          clearTimeout(timeout);
          try {
            const msg = JSON.parse(data.toString()) as { type?: string; token?: string };
            if (msg.type === 'auth' && msg.token === this.token) {
              this.setupClient(ws);
            } else {
              ws.close(4003, 'Invalid token');
            }
          } catch {
            ws.close(4003, 'Invalid auth message');
          }
        });
      } else {
        this.setupClient(ws);
      }
    });

    let saved = await this.store.list();
    if (this.loginMode || saved.length === 0) {
      const account = await this.loginViaQr();
      saved = [account, ...saved.filter((item) => item.accountId !== account.accountId)];
    }

    for (const account of saved) {
      this.accounts.set(account.accountId, account);
      this.startMonitor(account);
    }
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws);
    this.logInfo('bridge.client_connected', { clients: this.clients.size });
    this.sendHeartbeat(ws);

    ws.on('message', async (data) => {
      try {
        const cmd = JSON.parse(data.toString()) as SendCommand;
        if (cmd.type === 'send') {
          await this.handleSend(cmd);
          ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
        }
      } catch (error) {
        this.logError('bridge.client_command_failed', { error: this.errorDetail(error) });
        ws.send(JSON.stringify({ type: 'error', error: String(error) }));
      }
    });

    ws.on('close', (code, reason) => {
      this.clients.delete(ws);
      this.logWarning('bridge.client_closed', {
        clients: this.clients.size,
        code,
        reason: reason.toString(),
      });
    });
    ws.on('error', (error) => {
      this.clients.delete(ws);
      this.logWarning('bridge.client_error', {
        clients: this.clients.size,
        error: this.errorDetail(error),
      });
    });
  }

  private broadcast(payload: Record<string, unknown>): void {
    const data = JSON.stringify(payload);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  private async loginViaQr(): Promise<SavedWeixinAccount> {
    this.logInfo('weixin.login_start');
    const initial = await fetchQrCode(this.baseUrl);
    this.broadcast({ type: 'qr', qr: initial.qrcodeUrl });
    await this.writeQrPng(initial.qrcodeUrl);
    qrcode.generate(initial.qrcodeUrl, { small: true });
    this.logInfo('weixin.login_waiting_for_scan');

    let currentQr = initial.qrcode;
    let refreshes = 0;
    while (!this.stopped) {
      const result = await pollQrStatus(this.baseUrl, currentQr);
      if (result.status === 'scaned') {
        this.logInfo('weixin.login_scanned');
      }
      if (result.status === 'expired') {
        refreshes += 1;
        if (refreshes > 3) {
          throw new Error('二维码多次过期，请重新运行登录命令');
        }
        this.logWarning('weixin.login_qr_expired', { refreshes, maxRefreshes: 3 });
        const next = await fetchQrCode(this.baseUrl);
        currentQr = next.qrcode;
        this.broadcast({ type: 'qr', qr: next.qrcodeUrl });
        await this.writeQrPng(next.qrcodeUrl);
        qrcode.generate(next.qrcodeUrl, { small: true });
        continue;
      }
      if (result.status === 'confirmed' && result.bot_token && result.ilink_bot_id) {
        const account = await this.store.save(result.ilink_bot_id, {
          token: result.bot_token,
          userId: result.ilink_user_id,
          baseUrl: result.baseurl || this.baseUrl,
        });
        if (result.ilink_user_id) {
          try {
            this.finalizeLogin(account, result.ilink_user_id);
            this.logInfo('weixin.login_finalized', {
              gatewayService: this.gatewayService || 'nanobot-gateway.service',
            });
          } catch (error) {
            this.logError('weixin.login_finalize_failed', { error: this.errorDetail(error) });
          }
        } else {
          this.logError('weixin.login_missing_user_id');
        }
        this.logInfo('weixin.login_confirmed', { accountId: account.accountId });
        this.broadcast({
          type: 'status',
          status: 'connected',
          accountId: account.accountId,
          detail: 'login_confirmed',
        });
        return account;
      }
      await sleep(1200);
    }
    throw new Error('login aborted');
  }

  private async writeQrPng(qrContent: string): Promise<void> {
    if (!this.qrPath) return;
    await QRCode.toFile(this.qrPath, qrContent, {
      type: 'png',
      margin: 2,
      width: 512,
      color: {
        dark: '#111111',
        light: '#ffffff',
      },
    });
    this.logInfo('weixin.qr_written', { path: this.qrPath });
  }

  private finalizeLogin(account: SavedWeixinAccount, userId: string): void {
    if (!this.nanobotBin) {
      throw new Error('WEIXIN_NANOBOT_BIN is not set');
    }
    if (!this.configPath) {
      throw new Error('WEIXIN_CONFIG_PATH is not set');
    }

    const result = spawnSync(
      this.nanobotBin,
      [
        'weixin',
        'finalize-login',
        '--config',
        this.configPath,
        '--user-id',
        userId,
        '--base-url',
        account.baseUrl,
        '--state-dir',
        this.store.rootDir,
        '--gateway-service',
        this.gatewayService || 'nanobot-gateway.service',
      ],
      {
        stdio: 'inherit',
      },
    );
    if (result.status !== 0) {
      throw new Error(`finalize-login exited with status ${result.status}`);
    }
  }

  private async monitorAccount(account: SavedWeixinAccount): Promise<void> {
    this.broadcast({ type: 'status', status: 'connected', accountId: account.accountId, detail: 'monitor_started' });
    this.logInfo('weixin.monitor_started', { accountId: account.accountId });
    let cursor = await this.store.loadSyncCursor(account.accountId);
    let timeoutMs = 35000;

    while (!this.stopped) {
      try {
        const response = await getUpdates(account.baseUrl, account.token, cursor, timeoutMs);
        if (response.get_updates_buf) {
          cursor = response.get_updates_buf;
          await this.store.saveSyncCursor(account.accountId, cursor);
        }
        if (response.longpolling_timeout_ms && response.longpolling_timeout_ms > 0) {
          timeoutMs = response.longpolling_timeout_ms;
        }
        if ((response.ret ?? 0) !== 0 || (response.errcode ?? 0) !== 0) {
          const detail = `${response.errmsg || response.errcode || response.ret}`;
          this.logWarning('weixin.getupdates_failed', { accountId: account.accountId, detail });
          this.broadcast({
            type: 'error',
            error: `weixin getupdates failed for ${account.accountId}: ${detail}`,
          });
          await sleep(2000);
          continue;
        }

        for (const msg of response.msgs ?? []) {
          await this.handleInboundMessage(account, msg);
        }
      } catch (error) {
        this.logWarning('weixin.monitor_error', {
          accountId: account.accountId,
          error: this.errorDetail(error),
        });
        this.broadcast({
          type: 'status',
          status: 'disconnected',
          accountId: account.accountId,
          detail: String(error),
        });
        await sleep(5000);
      }
    }
    this.logInfo('weixin.monitor_stopped', { accountId: account.accountId });
  }

  private extractMediaUrls(msg: WeixinMessage, kind: 'image' | 'file'): string[] {
    const candidates = collectUrlCandidates([
      msg,
      msg.image_item,
      ...(msg.item_list ?? []),
      kind === 'file' ? msg.file_item : undefined,
    ]);
    const filtered = candidates.filter(({ url, keyPath }) => {
      if (kind === 'image') return looksLikeImageCandidate(url, keyPath);
      return /(file|doc|download|url)/i.test(keyPath);
    });
    return [...new Set(filtered.map((item) => item.url))];
  }

  private mediaDir(): string {
    return join(process.env.HOME || homedir(), '.nanobot', 'media', 'weixin');
  }

  private async downloadInboundImages(
    account: SavedWeixinAccount,
    sender: string,
    msg: WeixinMessage,
  ): Promise<string[]> {
    const urls = this.extractMediaUrls(msg, 'image');
    if (!urls.length) return [];

    this.logInfo('weixin.image_candidates', { accountId: account.accountId, sender, count: urls.length });
    await mkdir(this.mediaDir(), { recursive: true });

    const saved: string[] = [];
    const stamp = sanitizePathPart(String(msg.message_id ?? msg.client_id ?? Date.now()));
    const accountPart = sanitizePathPart(account.accountId);
    const senderPart = sanitizePathPart(sender);

    for (const [index, url] of urls.entries()) {
      try {
        const { data, contentType } = await downloadMedia(url, account.token);
        const ext = guessImageExtension(url, contentType);
        const filename = `${accountPart}_${senderPart}_${stamp}_${index + 1}${ext}`;
        const fullPath = join(this.mediaDir(), filename);
        await writeFile(fullPath, data);
        this.logInfo('weixin.image_saved', { sender, path: fullPath });
        saved.push(fullPath);
      } catch (error) {
        this.logWarning('weixin.image_download_failed', { sender, error: this.errorDetail(error) });
      }
    }

    return saved;
  }

  private async handleInboundMessage(account: SavedWeixinAccount, msg: WeixinMessage): Promise<void> {
    const sender = (msg.from_user_id || '').trim();
    if (!sender) return;

    const content = extractText(msg.item_list);
    const media = await this.downloadInboundImages(account, sender, msg);
    const placeholders = media.map((path) => `[image: ${basename(path)}]`);
    const finalContent = [content, ...placeholders].filter(Boolean).join('\n').trim();
    if (!finalContent && media.length === 0) return;

    if (msg.context_token) {
      this.contextTokens.set(`${account.accountId}:${sender}`, msg.context_token);
    }

    if (media.length) {
      this.logInfo('weixin.forwarding_media', { accountId: account.accountId, sender, count: media.length });
    }
    this.broadcast({
      type: 'message',
      id: String(msg.message_id ?? msg.client_id ?? `${Date.now()}`),
      accountId: account.accountId,
      sender,
      content: finalContent,
      timestamp: msg.create_time_ms ?? Date.now(),
      contextToken: msg.context_token,
      media,
    });
  }

  private async handleSend(cmd: SendCommand): Promise<void> {
    const sepIndex = cmd.to.indexOf('|');
    if (sepIndex === -1) {
      throw new Error(`Invalid Weixin recipient: ${cmd.to}`);
    }

    const accountId = cmd.to.slice(0, sepIndex);
    const toUserId = cmd.to.slice(sepIndex + 1);
    const account = this.accounts.get(accountId);
    if (!account) {
      throw new Error(`Unknown Weixin account: ${accountId}`);
    }

    const contextToken = this.contextTokens.get(`${accountId}:${toUserId}`);
    if (!contextToken) {
      throw new Error(`No Weixin context token cached for ${toUserId} on ${accountId}`);
    }

    this.logInfo('weixin.send_attempt', { accountId, toUserId });
    await sendMessage(account.baseUrl, account.token, toUserId, cmd.text, contextToken);
    this.logInfo('weixin.send_success', { accountId, toUserId });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();
    await Promise.allSettled([...this.monitorTasks.values()]);
    this.monitorTasks.clear();
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }
    this.logInfo('bridge.stopped');
  }

  private startHeartbeatLoop(): void {
    this.heartbeatTimer = setInterval(() => {
      const payload: BridgeHeartbeat = { type: 'heartbeat', timestamp: Date.now() };
      this.broadcast(payload as unknown as Record<string, unknown>);
    }, WeixinBridgeServer.HEARTBEAT_INTERVAL_MS);
  }

  private sendHeartbeat(client: WebSocket): void {
    if (client.readyState !== WebSocket.OPEN) return;
    const payload: BridgeHeartbeat = { type: 'heartbeat', timestamp: Date.now() };
    client.send(JSON.stringify(payload));
  }

  private startMonitor(account: SavedWeixinAccount): void {
    const task = this.monitorAccount(account)
      .catch((error) => {
        this.logError('weixin.monitor_crashed', {
          accountId: account.accountId,
          error: this.errorDetail(error),
        });
        if (!this.stopped) {
          setTimeout(() => this.startMonitor(account), 5000);
        }
      })
      .finally(() => {
        if (this.monitorTasks.get(account.accountId) === task) {
          this.monitorTasks.delete(account.accountId);
        }
      });
    this.monitorTasks.set(account.accountId, task);
  }

  private describeRemote(ws: WebSocket): string {
    const socket = (ws as WebSocket & { _socket?: { remoteAddress?: string; remotePort?: number } })._socket;
    const address = socket?.remoteAddress || 'unknown';
    const port = socket?.remotePort || 'unknown';
    return `${address}:${port}`;
  }

  private errorDetail(error: unknown): string {
    if (error instanceof Error) return `${error.name}: ${error.message}`;
    return String(error);
  }

  private logInfo(event: string, fields?: Record<string, unknown>): void {
    this.log('INFO', event, fields);
  }

  private logWarning(event: string, fields?: Record<string, unknown>): void {
    this.log('WARN', event, fields);
  }

  private logError(event: string, fields?: Record<string, unknown>): void {
    this.log('ERROR', event, fields);
  }

  private log(level: string, event: string, fields?: Record<string, unknown>): void {
    const suffix = fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : '';
    console.log(`${level} ${event}${suffix}`);
  }
}
